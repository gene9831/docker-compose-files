import errno
import http.client
import io
import struct
import sys
import unittest
from datetime import datetime, timezone
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
        self.enterContext(patch.dict("os.environ", {
            "WORLD_FILE": "", "WORLD_SIZE": "", "WORLD_DIFFICULTY": "",
            "SAVE_INTERVAL_MINUTES": "", "BACKUP_KEEP": "",
        }))

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

    def test_backup_settings_default_to_ten_minutes_and_five_snapshots(self):
        with patch.dict("os.environ", {"SAVE_INTERVAL_MINUTES": "", "BACKUP_KEEP": ""}):
            self.assertEqual(server.backup_settings(), (600, 5))
        with patch.dict("os.environ", {"SAVE_INTERVAL_MINUTES": "3", "BACKUP_KEEP": "8"}):
            self.assertEqual(server.backup_settings(), (180, 8))
        for variable in ("SAVE_INTERVAL_MINUTES", "BACKUP_KEEP"):
            for invalid in ("0", "-1", "1.5", "invalid"):
                with self.subTest(variable=variable, value=invalid), patch.dict("os.environ", {variable: invalid}):
                    with self.assertRaisesRegex(ValueError, variable):
                        server.backup_settings()

    def test_periodic_backups_keep_latest_five_per_world(self):
        upgrade = self.data / "backups" / "upgrade-copy"
        upgrade.mkdir(parents=True)
        (upgrade / self.world.name).write_bytes(b"upgrade world")
        other = self.world.with_name("Other World.wld")
        other.write_bytes(b"other world")
        other_backup = server.backup_world(other, 5)
        for revision in range(12):
            self.world.write_text(f"world {revision}")
            latest = server.backup_world(self.world, 5)
        snapshots = sorted(latest.parent.iterdir())
        self.assertEqual(len(snapshots), 5)
        self.assertEqual({snapshot.read_text() for snapshot in snapshots},
                         {f"world {revision}" for revision in range(7, 12)})
        self.assertEqual(other_backup.read_bytes(), b"other world")
        self.assertEqual(upgrade.joinpath(self.world.name).read_bytes(), b"upgrade world")

    def test_backup_names_use_world_directory_and_system_local_minute(self):
        with patch.object(server, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 15, 0, 30)
            first = server.backup_world(self.world, 5)
            self.assertEqual(first, self.data / "backups/default.wld.d/2026-11-15_00-30.wld")
            self.world.write_bytes(b"second snapshot")
            second = server.backup_world(self.world, 5)
        root = self.data / "backups" / "default.wld.d"
        self.assertEqual(first, root / "2026-11-15_00-30.wld")
        self.assertEqual(second, root / "2026-11-15_00-30.1.wld")
        self.assertEqual(first.read_bytes(), b"original world")
        self.assertEqual(second.read_bytes(), b"second snapshot")

    def test_old_periodic_snapshots_migrate_without_changing_manual_backups(self):
        old_stamp = "20261114T154801412283Z"
        old = self.data / "backups" / "periodic" / self.world.name / old_stamp
        old.mkdir(parents=True)
        (old / self.world.name).write_bytes(b"previous scheduled snapshot")
        manual = self.data / "worlds.bak"
        manual.mkdir()
        (manual / self.world.name).write_bytes(b"manual backup")
        latest = server.backup_world(self.world, 5)
        local_stamp = datetime.strptime(old_stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d_%H-%M")
        migrated = self.data / "backups" / f"{self.world.name}.d" / f"{local_stamp}.wld"
        self.assertTrue(migrated.is_file())
        self.assertEqual(migrated.read_bytes(), b"previous scheduled snapshot")
        self.assertEqual(latest.read_bytes(), b"original world")
        self.assertFalse(old.exists())
        self.assertEqual((manual / self.world.name).read_bytes(), b"manual backup")

    def test_failed_periodic_backup_preserves_retained_snapshots(self):
        latest = server.backup_world(self.world, 1)
        self.world.write_bytes(b"new save")

        def fail_copy(source, destination):
            destination.write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(server.shutil, "copy2", side_effect=fail_copy):
            with self.assertRaises(OSError):
                server.backup_world(self.world, 1)
        self.assertEqual(list(latest.parent.iterdir()), [latest])
        self.assertEqual(latest.read_bytes(), b"original world")

    def test_nested_snapshots_are_flattened_without_overwriting_existing_files(self):
        root = self.data / "backups" / f"{self.world.name}.d"
        nested = root / "2026-11-14_23-48"
        nested.mkdir(parents=True)
        (nested / self.world.name).write_bytes(b"previous nested snapshot")
        existing = root / "2026-11-14_23-48.wld"
        existing.write_bytes(b"previous flat snapshot")
        server.backup_world(self.world, 5)
        self.assertEqual(existing.read_bytes(), b"previous flat snapshot")
        self.assertEqual((root / "2026-11-14_23-48.1.wld").read_bytes(), b"previous nested snapshot")
        self.assertFalse(nested.exists())

    def test_scheduled_save_creates_snapshot_only_after_save_completion(self):
        root = self.data / "backups" / f"{self.world.name}.d"
        binary = self.executable(
            "1457",
            "import sys, time\n"
            f"world = Path({str(self.world)!r})\n"
            f"backups = Path({str(root)!r})\n"
            "print(': Server started', flush=True)\n"
            "for command in sys.stdin:\n"
            "    if command.strip() == 'playing':\n"
            "        print('1 players connected.', flush=True)\n"
            "    elif command.strip() == 'version':\n"
            "        print(': Terraria Server v1.4.5.8', flush=True)\n"
            "    if command.strip() == 'save':\n"
            "        world.write_bytes(b'partially saved')\n"
            "        print('Saving world data: 50%', flush=True)\n"
            "        time.sleep(0.04)\n"
            "        assert not backups.exists() or not list(backups.iterdir())\n"
            "        world.write_bytes(b'completed scheduled save')\n"
            "        print(': Backing up world file', flush=True)\n"
            "        deadline = time.monotonic() + 2\n"
            "        while not backups.exists() and time.monotonic() < deadline:\n"
            "            time.sleep(0.01)\n"
            "        while not list(backups.glob('*.wld')) and time.monotonic() < deadline:\n"
            "            time.sleep(0.01)\n"
            "        break",
        )
        with (
            patch.object(server, "open_official_url", return_value=io.BytesIO(b'["terraria-server-1457.zip"]')),
            patch.object(server, "backup_settings", return_value=(0.1, 5)),
        ):
            self.assertEqual(server.main(), 0)
        snapshot, = root.glob("*.wld")
        self.assertEqual(snapshot.read_bytes(), b"completed scheduled save")

    def test_unconfirmed_save_skips_backup_and_retries_next_interval(self):
        binary = self.executable(
            "1457",
            "import sys\n"
            "print(': Server started', flush=True)\n"
            "saves = 0\n"
            "for command in sys.stdin:\n"
            "    if command.strip() == 'playing':\n"
            "        print('1 players connected.', flush=True)\n"
            "        continue\n"
            "    if command.strip() == 'version':\n"
            "        print(': Terraria Server v1.4.5.8', flush=True)\n"
            "        continue\n"
            "    assert command.strip() == 'save'\n"
            "    saves += 1\n"
            f"    Path({str(self.world)!r}).write_text(f'save request {{saves}}')\n"
            "    if saves == 2:\n"
            "        break",
        )
        with patch.object(server, "SAVE_TIMEOUT", 0.05):
            self.assertEqual(server.run_server(binary, "", world=self.world, save_interval=0.05), 0)
        self.assertEqual(self.world.read_text(), "save request 2")
        self.assertEqual(list((self.data / "backups").glob("*.wld.d/*.wld")), [])

    def test_unchanged_empty_server_does_not_replace_played_history(self):
        historical = server.backup_world(self.world, 1)
        binary = self.executable(
            "1457",
            "import sys\n"
            "print(': Server started', flush=True)\n"
            "checks = 0\n"
            "for command in sys.stdin:\n"
            "    if command.strip() == 'playing':\n"
            "        print(': No players connected.', flush=True)\n"
            "    elif command.strip() == 'version':\n"
            "        print(': Terraria Server v1.4.5.8', flush=True)\n"
            "        checks += 1\n"
            "        if checks == 3:\n"
            "            break\n"
            "    else:\n"
            "        raise AssertionError(f'Unexpected empty-server command: {command}')",
        )
        self.assertEqual(server.run_server(binary, "", world=self.world, save_interval=0.05, backup_keep=1), 0)
        self.assertEqual(list(historical.parent.iterdir()), [historical])
        self.assertEqual(historical.read_bytes(), b"original world")

    def test_final_saved_progress_is_backed_up_once_after_players_leave(self):
        root = self.data / "backups" / f"{self.world.name}.d"
        binary = self.executable(
            "1457",
            "import sys\n"
            f"world = Path({str(self.world)!r})\n"
            "print(': Server started', flush=True)\n"
            "checks = 0\n"
            "saves = 0\n"
            "for command in sys.stdin:\n"
            "    if command.strip() == 'playing':\n"
            "        if checks == 0:\n"
            "            world.write_bytes(b'final progress before leaving')\n"
            "        print(': No players connected.', flush=True)\n"
            "    elif command.strip() == 'version':\n"
            "        checks += 1\n"
            "        print(': Terraria Server v1.4.5.8', flush=True)\n"
            "        if checks == 3:\n"
            "            assert saves == 1\n"
            "            break\n"
            "    elif command.strip() == 'save':\n"
            "        saves += 1\n"
            "        assert saves == 1, 'Empty server received duplicate saves'\n"
            "        world.write_bytes(b'final scheduled save')\n"
            "        print('Backing up world file', flush=True)",
        )
        self.assertEqual(server.run_server(binary, "", world=self.world, save_interval=0.05), 0)
        snapshot, = root.glob("*.wld")
        self.assertEqual(snapshot.read_bytes(), b"final scheduled save")


if __name__ == "__main__":
    unittest.main()
