import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile
from zlib import error as DecompressionError


VERSION_URL = "https://terraria.org/api/get/dedicated-servers-names"
DOWNLOAD_URL = "https://terraria.org/api/download/pc-dedicated-server"
SERVER_BINARY = "TerrariaServer.bin.x86_64"
STARTUP_TIMEOUT = 600
SERVER_READY = re.compile(rb"(?:^|[\r\n])(?:: )?Server started[\r\n]")
WORLD_SIZES = {"small": 1, "medium": 2, "large": 3}
WORLD_DIFFICULTIES = {"classic": 0, "expert": 1, "master": 2, "journey": 3}


def open_official_url(url, *, timeout):
    # The official endpoints reject urllib's default User-Agent.
    request = Request(url, headers={"User-Agent": "Terraria-Dedicated-Server-Updater/1.0"})
    return urlopen(request, timeout=timeout)


def world_path(data, name):
    if (
        name != name.strip()
        or not name.endswith(".wld")
        or len(name) == 4
        or any(character in "/\\=" or ord(character) < 32 for character in name)
    ):
        raise ValueError("WORLD_FILE must be a .wld filename inside data/worlds")

    worlds = data / "worlds"
    if not worlds.resolve().is_relative_to(data.resolve()):
        raise ValueError(f"World directory must be inside {data}: {worlds}")
    worlds.mkdir(exist_ok=True)
    world = worlds / name
    if world.is_symlink() or (world.exists() and not world.is_file()):
        raise ValueError(f"World must be a regular file: {world}")
    return world


def world_settings(data, world_file, *, size="medium", difficulty="classic"):
    world = world_path(data, world_file or "default.wld")
    if world_file and not world.is_file():
        raise ValueError(f"Configured world file not found: {world}")

    config = f"world={world}\nworldpath={world.parent}\nport=7777\nupnp=0\n"
    if not world.exists():
        size_code = WORLD_SIZES.get(size)
        if size_code is None:
            raise ValueError(f"WORLD_SIZE must be one of: {', '.join(WORLD_SIZES)}")
        difficulty_code = WORLD_DIFFICULTIES.get(difficulty)
        if difficulty_code is None:
            raise ValueError(f"WORLD_DIFFICULTY must be one of: {', '.join(WORLD_DIFFICULTIES)}")
        config += f"autocreate={size_code}\ndifficulty={difficulty_code}\nworldname=default\n"
    return world, config


def install_candidate(cache):
    with open_official_url(VERSION_URL, timeout=10) as response:
        names = json.load(response)
    if not isinstance(names, list) or not names or not isinstance(names[0], str):
        raise ValueError("Invalid official version response")
    filename = names[0]
    match = re.fullmatch(r"terraria-server-([0-9]+)\.zip", filename)
    if not match:
        raise ValueError(f"Invalid PC server archive name: {filename!r}")

    version = match[1]
    release = cache / version
    binary = release / SERVER_BINARY
    if binary.is_file():
        return binary

    with TemporaryDirectory(dir=cache) as temporary:
        stage = Path(temporary)
        download = stage / filename
        with open_official_url(f"{DOWNLOAD_URL}/{filename}", timeout=30) as source:
            with download.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        prefix = f"{version}/Linux/"
        try:
            with ZipFile(download) as archive:
                archive.extractall(stage, [name for name in archive.namelist() if name.startswith(prefix)])
        except (BadZipFile, DecompressionError, RuntimeError) as error:
            raise ValueError(f"Cannot extract official server archive: {error}") from error
        linux = stage / prefix
        executable = linux / SERVER_BINARY
        with executable.open("rb") as file:
            header = file.read(20)
        if not header.startswith(b"\x7fELF\x02\x01") or header[18:20] != b"\x3e\x00":
            raise ValueError("Official archive has no usable Linux x86-64 server")
        executable.chmod(0o755)
        linux.rename(release)
    return binary


def world_files(world):
    return (world, world.with_suffix(".wld.bak"), world.with_suffix(".wld.bak2"))


def begin_trial(data, world, pending, version):
    files = [path for path in world_files(world) if path.exists() or path.is_symlink()]
    backup_name = None
    if files:
        backups = data / "backups"
        backups.mkdir(exist_ok=True)
        backup_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with TemporaryDirectory(dir=backups, prefix=".partial-") as temporary:
            stage = Path(temporary) / backup_name
            stage.mkdir()
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Cannot back up a non-regular world file: {path}")
                shutil.copy2(path, stage / path.name)
            stage.rename(backups / backup_name)

    trial = {"version": version, "world": world.name, "backup": backup_name, "files": [path.name for path in files]}
    with TemporaryDirectory(dir=pending.parent) as temporary:
        journal = Path(temporary) / pending.name
        journal.write_text(json.dumps(trial), encoding="utf-8")
        journal.replace(pending)


def recover_trial(data, current, pending):
    if not pending.exists():
        return
    trial = json.loads(pending.read_text(encoding="utf-8"))
    # The current link is the commit point, including after an interrupted startup.
    if current.resolve() != current.parent / trial["version"]:
        world = world_path(data, trial["world"])
        files = world_files(world)
        saved = set(trial["files"])
        with TemporaryDirectory(dir=world.parent) as temporary:
            stage = Path(temporary)
            for path in files:
                if path.name in saved:
                    shutil.copy2(data / "backups" / trial["backup"] / path.name, stage / path.name)
            for path in files:
                if path.name in saved:
                    (stage / path.name).replace(path)
                else:
                    path.unlink(missing_ok=True)
    pending.unlink()


def activate_candidate(current, pending, version):
    with TemporaryDirectory(dir=current.parent) as temporary:
        link = Path(temporary) / current.name
        link.symlink_to(version, target_is_directory=True)
        link.replace(current)
    pending.unlink()
    print(f"Activated Terraria {version}", flush=True)


def wait_for_candidate(child, stopping, on_ready):
    deadline = monotonic() + STARTUP_TIMEOUT
    ready = False
    tail = b""
    timeout_error = "Terraria did not report readiness before the startup timeout"
    while True:
        timeout = None if ready or stopping() else deadline - monotonic()
        if timeout is not None and timeout <= 0:
            raise TimeoutError(timeout_error)
        if not select.select([child.stdout], [], [], timeout)[0]:
            continue
        chunk = os.read(child.stdout.fileno(), 65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if not ready and not stopping():
            output = tail + chunk
            if SERVER_READY.search(output) and child.poll() is None:
                on_ready()
                ready = True
            tail = output[-64:]
    if not ready and not stopping():
        raise RuntimeError("Terraria closed its console before reporting readiness")
    return child.wait()


def run_server(binary, config, *, on_ready=None):
    process = None
    stopping = False

    def stop_server(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        if process is not None:
            try:
                process.stdin.write(b"exit\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass

    previous_term = signal.signal(signal.SIGTERM, stop_server)
    previous_int = signal.signal(signal.SIGINT, stop_server)
    try:
        with TemporaryDirectory() as temporary:
            settings = Path(temporary) / "serverconfig.txt"
            settings.write_text(config, encoding="utf-8")
            if stopping:
                return 0
            with subprocess.Popen(
                [str(binary), "-config", str(settings)],
                cwd=binary.parent,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE if on_ready is not None else None,
            ) as child:
                process = child
                if stopping:
                    stopping = False
                    stop_server()
                if on_ready is not None:
                    try:
                        return wait_for_candidate(child, lambda: stopping, on_ready)
                    finally:
                        if child.poll() is None:
                            child.kill()
                return child.wait()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def run_cached(active, config, error):
    if not active.is_file():
        raise RuntimeError(f"Cannot install Terraria and no cached server is available: {error}") from error
    print(f"Terraria update failed ({error}). Using cached release {active.parent.name}.", file=sys.stderr)
    return run_server(active, config)


def serve(data, world, config, current, pending):
    active = current.resolve() / SERVER_BINARY
    try:
        candidate = install_candidate(current.parent)
        if candidate != active:
            begin_trial(data, world, pending, candidate.parent.name)
    except (OSError, ValueError, HTTPException) as error:
        recover_trial(data, current, pending)
        return run_cached(active, config, error)
    if candidate == active:
        print(f"Terraria {active.parent.name} is already installed", flush=True)
        return run_server(active, config)

    try:
        result = run_server(candidate, config, on_ready=lambda: activate_candidate(current, pending, candidate.parent.name))
    except (OSError, RuntimeError) as error:
        if current.resolve() == candidate.parent:
            raise
        recover_trial(data, current, pending)
        # The previous world may have been absent, so retain its original creation config.
        return run_cached(active, config, error)
    recover_trial(data, current, pending)
    return result


def main():
    data = Path("/data")
    with (data / ".terraria.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Terraria server is using this data directory") from None

        cache = data.resolve() / "server"
        cache.mkdir(exist_ok=True)
        current = cache / "current"
        pending = cache / "pending.json"
        recover_trial(data, current, pending)
        world, config = world_settings(
            data,
            os.environ.get("WORLD_FILE", ""),
            size=os.environ.get("WORLD_SIZE") or "medium",
            difficulty=os.environ.get("WORLD_DIFFICULTY") or "classic",
        )
        return serve(data, world, config, current, pending)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
