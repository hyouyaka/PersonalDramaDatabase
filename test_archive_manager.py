import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import archive_manager


class FakeUpstash:
    def __init__(self, *, strings=None, hashes=None, conflict_once=False):
        self.strings = dict(strings or {})
        self.hashes = {key: dict(value) for key, value in (hashes or {}).items()}
        self.conflict_once = conflict_once
        self.move_attempts = 0

    @staticmethod
    def token(raw):
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() if isinstance(raw, str) else "__missing__"

    def __call__(self, command):
        op = command[0]
        if op == "GET":
            return self.strings.get(command[1])
        if op == "HMGET":
            return [self.hashes.get(command[1], {}).get(field) for field in command[2:]]
        if op != "EVAL":
            raise AssertionError(command)

        script = command[1]
        if script == archive_manager.ARCHIVE_MERGE_SCRIPT:
            key, expected, encoded = command[3:6]
            if self.token(self.strings.get(key)) != expected:
                return 0
            self.strings[key] = encoded
            return 1

        if script != archive_manager.ARCHIVE_MOVE_SCRIPT:
            raise AssertionError(command)
        self.move_attempts += 1
        keys = command[3:10]
        args = command[10:]
        if self.conflict_once and self.move_attempts == 1:
            active = json.loads(self.strings[keys[0]])
            active["101"]["title"] = "并发更新标题"
            self.strings[keys[0]] = json.dumps(active, ensure_ascii=False, separators=(",", ":"))
            return 0
        for index in range(4):
            if self.token(self.strings.get(keys[index])) != args[index]:
                return 0
        count = int(args[8])
        drama_ids = [str(value) for value in args[9 : 9 + count]]
        history_expected = args[9 + count : 9 + count * 2]
        for drama_id, expected in zip(drama_ids, history_expected):
            current = self.hashes.get(keys[6], {}).get(drama_id)
            if (current if isinstance(current, str) else "__missing__") != expected:
                return 0
        encoded_watch_archive = args[9 + count * 2]
        self.strings[keys[0]] = args[4]
        self.strings[keys[4]] = args[5]
        if keys[5] in self.strings:
            self.strings[keys[5]] = args[4]
        self.strings[keys[1]] = args[6]
        self.strings[keys[2]] = args[7]
        self.strings[keys[3]] = encoded_watch_archive
        for drama_id in drama_ids:
            self.hashes.get(keys[6], {}).pop(drama_id, None)
        return 1


class LocalArchiveMigrationTests(unittest.TestCase):
    def test_legacy_missevan_archive_is_migrated_and_embedded_watchcount_is_split(self):
        legacy = {
            "旧剧": {
                "season1": {
                    "dramaId": 100,
                    "title": "旧剧",
                    "archivedAt": "2026-01-01T00:00:00+00:00",
                    "archivedReason": "HTTP_403",
                    "archivedWatchCount": {
                        "name": "旧剧",
                        "view_count": 10,
                        "fetched_at": "2025-12-31T00:00:00+00:00",
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            info_path = Path(tmp) / "missevan-archived-drama.json"
            watch_path = Path(tmp) / "missevan-archived-watch-counts.json"
            info_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            with (
                patch.dict(archive_manager.ARCHIVE_INFO_PATHS, {"missevan": info_path}),
                patch.dict(archive_manager.ARCHIVE_WATCHCOUNT_PATHS, {"missevan": watch_path}),
                patch.object(archive_manager, "backup_local_json_file"),
            ):
                info, watch = archive_manager.load_local_archives("missevan")

            self.assertEqual(info["records"]["100"]["record"]["title"], "旧剧")
            self.assertNotIn("archivedWatchCount", info["records"]["100"]["record"])
            self.assertEqual(watch["records"]["100"]["latest"]["view_count"], 10)
            self.assertIsNone(watch["records"]["100"]["history"])
            self.assertEqual(json.loads(info_path.read_text(encoding="utf-8")), info)
            self.assertEqual(json.loads(watch_path.read_text(encoding="utf-8")), watch)

    def test_missing_remote_keys_are_initialized_from_local_without_overwrite(self):
        local_info = {
            "version": 1,
            "platform": "manbo",
            "updatedAt": "2026-07-29T00:00:00+00:00",
            "records": {
                "200": {
                    "archivedAt": "2026-07-29T00:00:00+00:00",
                    "archivedReason": "MANBO_CODE_400_作品已下架",
                    "record": {"dramaId": "200", "name": "已归档"},
                }
            },
        }
        local_watch = {
            "version": 1,
            "platform": "manbo",
            "updatedAt": "2026-07-29T00:00:00+00:00",
            "records": {},
        }
        fake = FakeUpstash()
        with tempfile.TemporaryDirectory() as tmp:
            info_path = Path(tmp) / "manbo-archived-drama.json"
            watch_path = Path(tmp) / "manbo-archived-watch-counts.json"
            info_path.write_text(json.dumps(local_info, ensure_ascii=False), encoding="utf-8")
            watch_path.write_text(json.dumps(local_watch, ensure_ascii=False), encoding="utf-8")
            with (
                patch.dict(archive_manager.ARCHIVE_INFO_PATHS, {"manbo": info_path}),
                patch.dict(archive_manager.ARCHIVE_WATCHCOUNT_PATHS, {"manbo": watch_path}),
            ):
                archive_manager.ensure_remote_archive_keys("manbo", upstash=fake)

        self.assertEqual(
            json.loads(fake.strings["manbo:info:archive:v1"])["records"]["200"]["record"]["name"],
            "已归档",
        )
        self.assertEqual(
            json.loads(fake.strings["manbo:watchcount:archive:v1"])["records"],
            {},
        )


class RemoteArchivePublishTests(unittest.TestCase):
    def build_remote(self, *, conflict_once=False):
        active = {
            "100": {"dramaId": 100, "title": "待归档"},
            "101": {"dramaId": 101, "title": "保留"},
        }
        latest = {
            "_meta": {"updated_at": "2026-07-29T00:00:00+00:00"},
            "counts": {
                "100": {"name": "待归档", "view_count": 10, "fetched_at": "2026-07-29"},
                "101": {"name": "保留", "view_count": 20, "fetched_at": "2026-07-29"},
            },
        }
        empty = {
            "version": 1,
            "platform": "missevan",
            "updatedAt": None,
            "records": {},
        }
        history = json.dumps({"name": "待归档", "points": [["2026-07-22", 9], ["2026-07-29", 10]]})
        dated = json.dumps(latest, ensure_ascii=False, separators=(",", ":"))
        return FakeUpstash(
            strings={
                "missevan:info:v2": json.dumps(active, ensure_ascii=False, separators=(",", ":")),
                "missevan:info:v1": json.dumps(active, ensure_ascii=False, separators=(",", ":")),
                "missevan:watchcount:latest": json.dumps(latest, ensure_ascii=False, separators=(",", ":")),
                "missevan:info:archive:v1": json.dumps(empty, ensure_ascii=False, separators=(",", ":")),
                "missevan:watchcount:archive:v1": json.dumps(empty, ensure_ascii=False, separators=(",", ":")),
                "missevan:watchcount:2026-07-29": dated,
            },
            hashes={"missevan:watchcount:history": {"100": history}},
            conflict_once=conflict_once,
        )

    def test_archive_move_updates_active_and_archive_resources_atomically(self):
        fake = self.build_remote()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    archive_manager.ACTIVE_INFO_PATHS,
                    {"missevan": Path(tmp) / "missevan-info.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ACTIVE_WATCHCOUNT_PATHS,
                    {"missevan": Path(tmp) / "missevan-watch.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ARCHIVE_INFO_PATHS,
                    {"missevan": Path(tmp) / "missevan-info-archive.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ARCHIVE_WATCHCOUNT_PATHS,
                    {"missevan": Path(tmp) / "missevan-watch-archive.json"},
                )
            )
            stats = archive_manager.publish_archive_candidates(
                "missevan",
                {
                    "100": {
                        "archivedAt": "2026-07-29T01:00:00+00:00",
                        "archivedReason": "HTTP_403",
                    }
                },
                upstash=fake,
            )

        active = json.loads(fake.strings["missevan:info:v2"])
        latest = json.loads(fake.strings["missevan:watchcount:latest"])
        info_archive = json.loads(fake.strings["missevan:info:archive:v1"])
        watch_archive = json.loads(fake.strings["missevan:watchcount:archive:v1"])
        self.assertEqual(stats, {"archived": 1})
        self.assertEqual(set(active), {"101"})
        self.assertEqual(set(latest["counts"]), {"101"})
        self.assertEqual(info_archive["records"]["100"]["record"]["title"], "待归档")
        self.assertEqual(watch_archive["records"]["100"]["latest"]["view_count"], 10)
        self.assertEqual(len(watch_archive["records"]["100"]["history"]["points"]), 2)
        self.assertNotIn("100", fake.hashes["missevan:watchcount:history"])
        self.assertIn("100", json.loads(fake.strings["missevan:watchcount:2026-07-29"])["counts"])
        self.assertEqual(json.loads(fake.strings["missevan:info:v1"]), active)

    def test_concurrent_active_info_change_is_preserved_on_retry(self):
        fake = self.build_remote(conflict_once=True)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    archive_manager.ACTIVE_INFO_PATHS,
                    {"missevan": Path(tmp) / "missevan-info.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ACTIVE_WATCHCOUNT_PATHS,
                    {"missevan": Path(tmp) / "missevan-watch.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ARCHIVE_INFO_PATHS,
                    {"missevan": Path(tmp) / "missevan-info-archive.json"},
                )
            )
            stack.enter_context(
                patch.dict(
                    archive_manager.ARCHIVE_WATCHCOUNT_PATHS,
                    {"missevan": Path(tmp) / "missevan-watch-archive.json"},
                )
            )
            archive_manager.publish_archive_candidates(
                "missevan",
                {
                    "100": {
                        "archivedAt": "2026-07-29T01:00:00+00:00",
                        "archivedReason": "HTTP_403",
                    }
                },
                upstash=fake,
            )

        self.assertEqual(fake.move_attempts, 2)
        self.assertEqual(json.loads(fake.strings["missevan:info:v2"])["101"]["title"], "并发更新标题")

    def test_manbo_archive_removes_record_and_preserves_other_records(self):
        active = {
            "version": 1,
            "updatedAt": "2026-07-29T00:00:00+00:00",
            "records": [
                {"dramaId": "200", "name": "待归档"},
                {"dramaId": "201", "name": "保留"},
            ],
        }
        latest = {
            "_meta": {"updated_at": "2026-07-29T00:00:00+00:00"},
            "counts": {"200": {"name": "待归档", "view_count": 10}},
        }
        empty = {"version": 1, "platform": "manbo", "updatedAt": None, "records": {}}
        fake = FakeUpstash(
            strings={
                "manbo:info:v2": json.dumps(active, ensure_ascii=False, separators=(",", ":")),
                "manbo:info:v1": json.dumps(active, ensure_ascii=False, separators=(",", ":")),
                "manbo:watchcount:latest": json.dumps(latest, ensure_ascii=False, separators=(",", ":")),
                "manbo:info:archive:v1": json.dumps(empty, ensure_ascii=False, separators=(",", ":")),
                "manbo:watchcount:archive:v1": json.dumps(empty, ensure_ascii=False, separators=(",", ":")),
            },
            hashes={"manbo:watchcount:history": {}},
        )
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for mapping, filename in (
                (archive_manager.ACTIVE_INFO_PATHS, "manbo-info.json"),
                (archive_manager.ACTIVE_WATCHCOUNT_PATHS, "manbo-watch.json"),
                (archive_manager.ARCHIVE_INFO_PATHS, "manbo-info-archive.json"),
                (archive_manager.ARCHIVE_WATCHCOUNT_PATHS, "manbo-watch-archive.json"),
            ):
                stack.enter_context(patch.dict(mapping, {"manbo": Path(tmp) / filename}))
            archive_manager.publish_archive_candidates(
                "manbo",
                {
                    "200": {
                        "archivedAt": "2026-07-29T01:00:00+00:00",
                        "archivedReason": "MANBO_CODE_400_作品已下架",
                    }
                },
                upstash=fake,
            )

        saved = json.loads(fake.strings["manbo:info:v2"])
        archived = json.loads(fake.strings["manbo:info:archive:v1"])
        self.assertEqual([record["dramaId"] for record in saved["records"]], ["201"])
        self.assertEqual(archived["records"]["200"]["record"]["name"], "待归档")


if __name__ == "__main__":
    unittest.main()
