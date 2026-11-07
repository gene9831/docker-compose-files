import errno
import http.client
import io
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import server


class ServerTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.data = Path(temporary).resolve()
        self.world = self.data / "worlds" / "default.wld"
        self.world.parent.mkdir()
        self.world.write_bytes(b"original world")
        self.cache = self.data / "server"
        self.current = self.cache / "current"
        self.observed = self.data / "observed-world"
        self.previous = self.executable(
            "1457", f"Path({str(self.observed)!r}).write_bytes(Path({str(self.world)!r}).read_bytes())"
        )
        self.current.symlink_to(self.previous.parent.name)
        self.enterContext(patch.object(server, "Path", side_effect=lambda path: self.data if path == "/data" else Path(path)))
        self.enterContext(patch.dict("os.environ", {"WORLD_FILE": "", "WORLD_SIZE": "", "WORLD_DIFFICULTY": ""}))

    def executable(self, version, script):
        binary = self.cache / version / server.SERVER_BINARY
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n{script}\n")
        binary.chmod(0o755)
        return binary

    def latest(self):
        return io.BytesIO(b'["terraria-server-1458.zip"]')

    def assert_previous_served(self):
        self.assertEqual(self.current.resolve(), self.previous.parent)
        self.assertEqual(self.observed.read_bytes() if self.observed.exists() else None, b"original world")

    def test_unlaunchable_candidate_keeps_serving_previous_version(self):
        candidate = self.executable("1458", "pass")
        candidate.write_bytes(b"invalid executable")
        with patch.object(server, "open_official_url", side_effect=lambda *a, **kw: self.latest()):
            for _ in range(2):
                try:
                    self.assertEqual(server.main(), 0)
                except OSError as error:
                    self.fail(f"candidate launch error reached caller: {error}")
                self.assert_previous_served()

    def test_candidate_that_exits_before_ready_restores_world_before_fallback(self):
        sidecar = self.world.with_suffix(".wld.bak")
        sidecar.write_bytes(b"previous backup")
        self.executable(
            "1458",
            f"Path({str(self.world)!r}).write_bytes(b'changed by candidate')\n"
            f"Path({str(sidecar)!r}).write_bytes(b'changed backup')\n"
            "raise SystemExit(0)",
        )
        with patch.object(server, "open_official_url", return_value=self.latest()):
            self.assertEqual(server.main(), 0)
        self.assert_previous_served()
        self.assertEqual(sidecar.read_bytes(), b"previous backup")

    def test_candidate_commits_after_ready_and_keeps_its_world(self):
        candidate = self.executable(
            "1458",
            "import time\n"
            f"current = Path({str(self.current)!r})\n"
            "assert current.resolve().name == '1457'\n"
            f"Path({str(self.world)!r}).write_bytes(b'upgraded world')\n"
            "print(': Server ', end='', flush=True)\n"
            "time.sleep(0.03)\n"
            "print('started', flush=True)\n"
            "deadline = time.monotonic() + 3\n"
            "while current.resolve().name != '1458' and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "assert current.resolve().name == '1458'",
        )
        with patch.object(server, "open_official_url", return_value=self.latest()):
            self.assertEqual(server.main(), 0)
        self.assertEqual(self.current.resolve(), candidate.parent)
        self.assertEqual(self.world.read_bytes(), b"upgraded world")

    def test_hung_candidate_times_out_and_returns_to_cached_release(self):
        late_write = self.data / "late-write"
        self.executable(
            "1458",
            "import time\n"
            "time.sleep(0.8)\n"
            f"Path({str(late_write)!r}).touch()",
        )
        with (
            patch.object(server, "open_official_url", return_value=self.latest()),
            patch.object(server, "STARTUP_TIMEOUT", 0.4),
        ):
            self.assertEqual(server.main(), 0)
        self.assert_previous_served()
        self.assertFalse(late_write.exists(), "the expired candidate was still able to write files")

    def test_interrupted_trial_restores_world_on_next_start(self):
        self.executable("1458", "pass")

        def interrupt(*args, **kwargs):
            self.world.write_bytes(b"partially upgraded world")
            raise KeyboardInterrupt

        with patch.object(server, "open_official_url", return_value=self.latest()), patch.object(server, "run_server", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                server.main()
        with patch.object(server, "open_official_url", side_effect=OSError("offline")):
            self.assertEqual(server.main(), 0)
        self.assert_previous_served()

    def test_truncated_http_response_uses_cached_release(self):
        class Socket:
            def makefile(self, *_):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n["terraria-server-1458.zip"')

        response = http.client.HTTPResponse(Socket())
        response.begin()
        with patch.object(server, "open_official_url", return_value=response):
            try:
                self.assertEqual(server.main(), 0)
            except http.client.IncompleteRead as error:
                self.fail(f"truncated response escaped cache fallback: {error}")
        self.assert_previous_served()

    def test_corrupt_deflate_data_uses_cached_release(self):
        stream = io.BytesIO()
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(f"1458/Linux/{server.SERVER_BINARY}", b"candidate")
        payload = bytearray(stream.getvalue())
        name_length, extra_length = struct.unpack_from("<HH", payload, 26)
        payload[30 + name_length + extra_length] = 0b111
        with patch.object(server, "open_official_url", side_effect=[self.latest(), io.BytesIO(payload)]):
            self.assertEqual(server.main(), 0)
        self.assert_previous_served()

    def test_interrupted_backup_copy_leaves_no_published_backup(self):
        self.executable("1458", "pass")

        def fail_copy(source, target):
            target.write_bytes(b"part")
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(server, "open_official_url", return_value=self.latest()), patch.object(server.shutil, "copy2", side_effect=fail_copy):
            self.assertEqual(server.main(), 0)
        self.assert_previous_served()
        self.assertEqual(list((self.data / "backups").glob("*/default.wld")), [])

    def test_world_directory_cannot_redirect_saves_outside_data(self):
        with TemporaryDirectory() as directory:
            self.world.unlink()
            self.world.parent.rmdir()
            self.world.parent.symlink_to(directory)
            with self.assertRaisesRegex(ValueError, "World directory must be inside"):
                server.world_settings(self.data, "")


if __name__ == "__main__":
    unittest.main()
