import fcntl
import hashlib
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
SAVE_TIMEOUT = 120
SERVER_READY = re.compile(rb"(?:^|[\r\n])(?:: )?Server started[\r\n]")
# Terraria has written and validated the primary world before rotating its backups.
SAVE_COMPLETE = re.compile(rb"(?:^|[\r\n])(?:: )?Backing up world file[\r\n]")
NO_PLAYERS = re.compile(rb"(?:^|[\r\n])(?:: )?No players connected\.[\r\n]")
PLAYERS_CHECKED = re.compile(rb"(?:^|[\r\n])(?:: )?Terraria Server v[0-9.]+[\r\n]")
BACKUP_STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}(?:\.[0-9]+)?"
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


def backup_settings():
    values = []
    for name, default in (("SAVE_INTERVAL_MINUTES", "10"), ("BACKUP_KEEP", "5")):
        value = os.environ.get(name) or default
        if not value.isascii() or not value.isdecimal() or int(value) < 1:
            raise ValueError(f"{name} must be a positive integer")
        values.append(int(value))
    minutes, keep = values
    return minutes * 60, keep


def backup_destination(backups, stamp, *, extension=""):
    sequence = 0
    for path in backups.glob(f"{stamp}.*{extension}"):
        suffix = path.name[len(stamp) + 1:].removesuffix(extension)
        if suffix.isascii() and suffix.isdecimal():
            sequence = max(sequence, int(suffix))
    base = backups / f"{stamp}{extension}"
    if sequence or base.exists() or base.is_symlink():
        return backups / f"{stamp}.{sequence + 1}{extension}"
    return base


def create_backup(files, backups):
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    destination = backup_destination(backups, stamp)
    with TemporaryDirectory(dir=backups, prefix=".partial-") as temporary:
        stage = Path(temporary) / destination.name
        stage.mkdir()
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Cannot back up a non-regular world file: {path}")
            shutil.copy2(path, stage / path.name)
        stage.rename(destination)
    return destination


def migrate_backups(world, backups):
    old = world.parent.parent / "backups" / "periodic" / world.name
    backups.mkdir(parents=True, exist_ok=True)
    if old.is_dir() and not old.is_symlink():
        for snapshot in sorted(old.iterdir()):
            if not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", snapshot.name) or snapshot.is_symlink():
                continue
            if not snapshot.is_dir() or not (snapshot / world.name).is_file():
                continue
            instant = datetime.strptime(snapshot.name, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
            stamp = instant.astimezone().strftime("%Y-%m-%d_%H-%M")
            snapshot.rename(backup_destination(backups, stamp))
        if not any(old.iterdir()):
            old.rmdir()
        if not any(old.parent.iterdir()):
            old.parent.rmdir()

    for snapshot in sorted(backups.iterdir()):
        if not re.fullmatch(BACKUP_STAMP, snapshot.name) or not snapshot.is_dir() or snapshot.is_symlink():
            continue
        source = snapshot / world.name
        if source.is_symlink() or not source.is_file() or len(list(snapshot.iterdir())) != 1:
            continue
        destination = backups / f"{snapshot.name}.wld"
        if destination.exists() or destination.is_symlink():
            minute = snapshot.name.partition(".")[0]
            destination = backup_destination(backups, minute, extension=".wld")
        source.rename(destination)
        snapshot.rmdir()


def backup_world(world, keep):
    backups = world.parent.parent / "backups" / f"{world.name}.d"
    migrate_backups(world, backups)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    destination = backup_destination(backups, stamp, extension=".wld")
    if world.is_symlink():
        raise ValueError(f"Cannot back up a non-regular world file: {world}")
    with TemporaryDirectory(dir=backups, prefix=".partial-") as temporary:
        stage = Path(temporary) / world.name
        shutil.copy2(world, stage)
        stage.rename(destination)
    snapshots = sorted(
        (path for path in backups.iterdir()
         if re.fullmatch(BACKUP_STAMP + r"\.wld", path.name)
         and path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stem.partition(".")[0], int(path.stem.partition(".")[2] or 0)),
    )
    for snapshot in snapshots[:-keep]:
        snapshot.unlink()
    print(f"World backup saved: {destination}", flush=True)
    return destination


def world_digest(world):
    with world.open("rb") as file:
        return hashlib.file_digest(file, "sha256").digest()


def begin_trial(data, world, pending, version):
    files = [path for path in world_files(world) if path.exists() or path.is_symlink()]
    backup_name = None
    if files:
        backup_name = create_backup(files, data / "backups").name

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


def write_server_output(output, *, quiet_query=False):
    if quiet_query:
        output = b"".join(
            line for line in output.splitlines(keepends=True)
            if not NO_PLAYERS.search(line) and not PLAYERS_CHECKED.search(line)
            and line.strip(b": \t\r\n")
        )
    if output:
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()


def monitor_server(child, stopping, on_ready, world, save_interval, backup_keep):
    deadline = monotonic() + STARTUP_TIMEOUT
    ready = False
    tail = b""
    next_save = None
    saving_until = None
    checking_players = False
    players_absent = False
    last_backup_digest = None
    last_skipped = False
    query_output = b""
    timeout_error = "Terraria did not report readiness before the startup timeout"
    while True:
        now = monotonic()
        if not ready and on_ready is not None and not stopping() and now >= deadline:
            raise TimeoutError(timeout_error)
        if not stopping() and next_save is not None and now >= next_save:
            # version is a response boundary after the complete playing result.
            child.stdin.write(b"playing\nversion\n")
            child.stdin.flush()
            next_save = None
            saving_until = now + SAVE_TIMEOUT
            checking_players = True
            players_absent = False
            query_output = b""
            tail = b""
        if saving_until is not None and now >= saving_until:
            write_server_output(query_output)
            query_output = b""
            last_skipped = False
            print("Scheduled backup response was not confirmed. Skipping this backup.", file=sys.stderr)
            saving_until = None
            checking_players = False
            next_save = now + save_interval
        deadlines = []
        if not stopping():
            if not ready and on_ready is not None:
                deadlines.append(deadline)
            deadlines.extend(value for value in (next_save, saving_until) if value is not None)
        timeout = max(0, min(deadlines) - monotonic()) if deadlines else None
        if not select.select([child.stdout], [], [], timeout)[0]:
            continue
        chunk = os.read(child.stdout.fileno(), 65536)
        if not chunk:
            break
        if checking_players:
            query_output += chunk
        else:
            write_server_output(chunk)
        output = tail + chunk
        if not ready and not stopping():
            if SERVER_READY.search(output) and child.poll() is None:
                if on_ready is not None:
                    on_ready()
                ready = True
                if world is not None:
                    last_backup_digest = world_digest(world)
                    next_save = monotonic() + save_interval
        if saving_until is not None and checking_players and not stopping():
            players_absent = players_absent or NO_PLAYERS.search(output) is not None
            if PLAYERS_CHECKED.search(output):
                checking_players = False
                try:
                    needs_save = not players_absent or world_digest(world) != last_backup_digest
                except OSError as error:
                    print(f"Cannot inspect world for scheduled backup: {error}", file=sys.stderr)
                    last_skipped = False
                    needs_save = False
                if needs_save:
                    write_server_output(query_output)
                    query_output = b""
                    last_skipped = False
                    child.stdin.write(b"save\n")
                    child.stdin.flush()
                    saving_until = monotonic() + SAVE_TIMEOUT
                    print(f"Requested scheduled world save: {world.name}", flush=True)
                    # Discard pre-request output, including native autosaves.
                    tail = b""
                    continue
                saving_until = None
                next_save = monotonic() + save_interval
                write_server_output(query_output, quiet_query=last_skipped)
                query_output = b""
                if not last_skipped:
                    print(f"Skipped backup of unchanged empty world: {world.name}", flush=True)
                last_skipped = True
        if saving_until is not None and not checking_players and not stopping() and SAVE_COMPLETE.search(output):
            try:
                destination = backup_world(world, backup_keep)
                last_backup_digest = world_digest(destination)
            except (OSError, ValueError) as error:
                print(f"Scheduled world backup failed: {error}", file=sys.stderr)
            saving_until = None
            next_save = monotonic() + save_interval
        tail = output[-128:]
    write_server_output(query_output)
    if not ready and on_ready is not None and not stopping():
        raise RuntimeError("Terraria closed its console before reporting readiness")
    return child.wait()


def run_server(binary, config, *, on_ready=None, world=None, save_interval=600, backup_keep=5):
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
                stdout=subprocess.PIPE,
            ) as child:
                process = child
                if stopping:
                    stopping = False
                    stop_server()
                try:
                    return monitor_server(child, lambda: stopping, on_ready, world, save_interval, backup_keep)
                finally:
                    if child.poll() is None:
                        child.kill()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def run_cached(active, config, error, world, save_interval, backup_keep):
    if not active.is_file():
        raise RuntimeError(f"Cannot install Terraria and no cached server is available: {error}") from error
    print(f"Terraria update failed ({error}). Using cached release {active.parent.name}.", file=sys.stderr)
    return run_server(active, config, world=world, save_interval=save_interval, backup_keep=backup_keep)


def serve(data, world, config, current, pending, save_interval, backup_keep):
    active = current.resolve() / SERVER_BINARY
    try:
        candidate = install_candidate(current.parent)
        if candidate != active:
            begin_trial(data, world, pending, candidate.parent.name)
    except (OSError, ValueError, HTTPException) as error:
        recover_trial(data, current, pending)
        return run_cached(active, config, error, world, save_interval, backup_keep)
    if candidate == active:
        print(f"Terraria {active.parent.name} is already installed", flush=True)
        return run_server(active, config, world=world, save_interval=save_interval, backup_keep=backup_keep)

    try:
        result = run_server(
            candidate, config,
            on_ready=lambda: activate_candidate(current, pending, candidate.parent.name),
            world=world, save_interval=save_interval, backup_keep=backup_keep,
        )
    except (OSError, RuntimeError) as error:
        if current.resolve() == candidate.parent:
            raise
        recover_trial(data, current, pending)
        # The previous world may have been absent, so retain its original creation config.
        return run_cached(active, config, error, world, save_interval, backup_keep)
    recover_trial(data, current, pending)
    return result


def main():
    data = Path("/data")
    save_interval, backup_keep = backup_settings()
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
        return serve(data, world, config, current, pending, save_interval, backup_keep)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
