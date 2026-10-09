import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_retired_upstash as archive


class FakeRedis:
    def __init__(self):
        self.fingerprint = archive.endpoint_fingerprint("https://example.upstash.io")
        self.values = {key: json.dumps({"中文": key}, ensure_ascii=False, indent=2) for key in archive.KEYS}
        self.ttls = {key: -1 for key in archive.KEYS}
        self.commands = []
        self.on_command = None

    def command(self, args):
        self.commands.append(args)
        if self.on_command:
            self.on_command(args)
        op, key = args[:2]
        if op == "TYPE":
            return "string" if key in self.values else "none"
        if op == "GET":
            return self.values.get(key)
        if op == "TTL":
            return self.ttls.get(key, -2) if key in self.values else -2
        if op == "SET":
            if key in self.values:
                return None
            self.values[key] = args[2]
            self.ttls[key] = args[args.index("EX") + 1] if "EX" in args else -1
            return "OK"
        raise AssertionError(args)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.redis = FakeRedis()

    def make_archive(self):
        return archive.backup(self.redis, self.root)

    def assert_no_writes(self):
        self.assertTrue(all(cmd[0] in archive.READ_COMMANDS for cmd in self.redis.commands))

    def test_backup_is_exact_read_only_and_independent(self):
        folder = self.make_archive()
        manifest, entries = archive.validate_archive(folder)
        self.assertTrue(manifest["complete"])
        for entry, data in entries:
            self.assertEqual(data, self.redis.values[entry["key"]].encode())
        self.assertTrue((folder / archive.RESTORE_NAME).exists())
        self.assertNotEqual(folder, self.make_archive())
        self.assert_no_writes()

    def test_missing_type_json_and_ttl_fail(self):
        for mode in ("missing", "type", "json", "ttl"):
            with self.subTest(mode=mode):
                self.redis = FakeRedis()
                key = archive.KEYS[0]
                if mode == "missing":
                    del self.redis.values[key]
                elif mode == "type":
                    original = self.redis.command
                    self.redis.command = lambda args: "hash" if args == ["TYPE", key] else original(args)
                elif mode == "json":
                    self.redis.values[key] = "bad JSON"
                else:
                    self.redis.ttls[key] = 0
                with self.assertRaises(ValueError):
                    self.make_archive()
                self.assert_no_writes()
        for path in self.root.glob("*/manifest.json"):
            self.assertFalse(json.loads(path.read_text())["complete"])

    def test_remote_change_aborts_archive(self):
        count = 0
        def change(args):
            nonlocal count
            if args == ["GET", archive.KEYS[0]]:
                count += 1
                if count == 2:
                    self.redis.values[archive.KEYS[0]] = '{}'
        self.redis.on_command = change
        with self.assertRaisesRegex(ValueError, "changed during backup"):
            self.make_archive()
        self.assert_no_writes()

    def test_write_failure_leaves_incomplete_manifest(self):
        with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.make_archive()
        manifest = next(self.root.glob("*/manifest.json"))
        self.assertFalse(json.loads(manifest.read_text())["complete"])

    def test_dry_run_and_identical_skip_never_set(self):
        folder = self.make_archive()
        self.redis.commands.clear()
        archive.restore(self.redis, folder, report=lambda _: None)
        self.assert_no_writes()
        archive.restore(self.redis, folder, apply=True, report=lambda _: None)
        self.assert_no_writes()

    def test_invalid_archive_and_endpoint_stop_before_remote_access(self):
        for mode in ("digest", "endpoint", "path", "keys", "incomplete", "ttl"):
            with self.subTest(mode=mode):
                folder = self.make_archive()
                path = folder / 'manifest.json'
                manifest = json.loads(path.read_text())
                if mode == 'digest':
                    (folder / manifest['keys'][0]['file']).write_text('{}')
                elif mode == 'endpoint':
                    manifest['endpointFingerprint'] = '0' * 64
                elif mode == 'path':
                    manifest['keys'][0]['file'] = '../escape.json'
                elif mode == 'keys':
                    manifest['keys'][0]['key'] = 'missevan:info:v2'
                elif mode == 'incomplete':
                    manifest['complete'] = False
                else:
                    manifest['keys'][0]['ttl'] = -2
                path.write_text(json.dumps(manifest))
                self.redis.commands.clear()
                with self.assertRaises(ValueError):
                    archive.restore(self.redis, folder, apply=True, report=lambda _: None)
                self.assertEqual(self.redis.commands, [])

    def test_existing_conflict_preflight_prevents_all_writes(self):
        folder = self.make_archive()
        del self.redis.values[archive.KEYS[0]]
        self.redis.values[archive.KEYS[-1]] = '{}'
        self.redis.commands.clear()
        with self.assertRaisesRegex(ValueError, "conflicts"):
            archive.restore(self.redis, folder, apply=True, report=lambda _: None)
        self.assert_no_writes()
        self.assertNotIn(archive.KEYS[0], self.redis.values)

    def test_positive_ttl_restore_and_retry_after_partial_failure(self):
        self.redis.ttls[archive.KEYS[0]] = 120
        folder = self.make_archive()
        expected = dict(self.redis.values)
        self.redis.values.clear()
        def fail(args):
            if args[:2] == ['SET', archive.KEYS[2]]:
                raise OSError('interrupted')
        self.redis.on_command = fail
        messages = []
        with self.assertRaises(OSError):
            archive.restore(self.redis, folder, apply=True, report=messages.append)
        self.assertEqual(len(self.redis.values), 2)
        self.assertIn('verified completed keys', messages[-1])
        self.redis.on_command = None
        archive.restore(self.redis, folder, apply=True, report=lambda _: None)
        self.assertEqual(self.redis.values, expected)
        self.assertEqual(self.redis.ttls[archive.KEYS[0]], 120)
        for cmd in self.redis.commands:
            self.assertIn(cmd[1], archive.KEYS)
            if cmd[0] == 'SET':
                self.assertIn('NX', cmd)

    def test_set_nx_race_preserves_concurrent_value(self):
        folder = self.make_archive()
        key = archive.KEYS[0]
        del self.redis.values[key]
        def race(args):
            if args[:2] == ['SET', key]:
                self.redis.values[key] = '{"concurrent":true}'
        self.redis.on_command = race
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            archive.restore(self.redis, folder, apply=True, report=lambda _: None)
        self.assertEqual(self.redis.values[key], '{"concurrent":true}')


if __name__ == '__main__':
    unittest.main()
