import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call

import sync_new_drama_ids


class WatchcountFakeUpstash:
    def __init__(self, platform: str = "missevan", *, conflict_once: bool = False) -> None:
        self.platform = platform
        self.latest_key = f"{platform}:watchcount:latest"
        self.history_key = f"{platform}:watchcount:history"
        self.strings = {
            self.latest_key: json.dumps({
                "_meta": {"updated_at": "2026-07-03T04:06:41+00:00"},
                "counts": {"100": {"name": "旧名称", "view_count": 12}},
            }, ensure_ascii=False, separators=(",", ":")),
        }
        self.hashes = {
            self.history_key: {
                "100": json.dumps({
                    "name": "旧名称",
                    "points": [[f"2026-06-{day:02d}", day] for day in range(1, 31)]
                    + [["2026-07-03", 12]],
                }, ensure_ascii=False, separators=(",", ":")),
                "200": json.dumps({"name": "归档剧", "points": [["2026-07-03", 9]]}),
            },
        }
        self.commands: list[list[object]] = []
        self.conflict_once = conflict_once

    def __call__(self, command: list[object]) -> object:
        self.commands.append(command)
        operation = str(command[0]).upper()
        key = str(command[1]) if len(command) > 1 else ""
        if operation == "GET":
            return self.strings.get(key)
        if operation == "HGETALL":
            result: list[str] = []
            for field, value in self.hashes.get(key, {}).items():
                result.extend([field, value])
            return result
        if operation != "EVAL" or command[1] != sync_new_drama_ids.WATCHCOUNT_PUBLISH_SCRIPT:
            raise AssertionError(command)
        if self.conflict_once:
            self.conflict_once = False
            return 0
        latest_key = str(command[3])
        history_key = str(command[4])
        expected_latest = str(command[5])
        current_latest = self.strings.get(latest_key, "")
        if sync_new_drama_ids.hashlib.sha1(current_latest.encode("utf-8")).hexdigest() != expected_latest:
            return 0
        upsert_count = int(command[7])
        offset = 8
        history = self.hashes.setdefault(history_key, {})
        for index in range(upsert_count):
            field, expected = str(command[offset + index * 3]), str(command[offset + index * 3 + 1])
            if history.get(field, "__missing__") != expected:
                return 0
        delete_offset = offset + upsert_count * 3
        delete_count = int(command[delete_offset])
        for index in range(delete_count):
            field, expected = str(command[delete_offset + 1 + index * 2]), str(command[delete_offset + 2 + index * 2])
            if history.get(field) != expected:
                return 0
        self.strings[latest_key] = str(command[6])
        for index in range(upsert_count):
            field = str(command[offset + index * 3])
            history[field] = str(command[offset + index * 3 + 2])
        for index in range(delete_count):
            history.pop(str(command[delete_offset + 1 + index * 2]), None)
        return 1


class RemoteJsonBackupTests(unittest.TestCase):
    def test_local_backup_reuses_identical_content_and_keeps_changed_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "store.json"
            source.parent.mkdir()
            source.write_text('{"version": 1}', encoding="utf-8")
            with unittest.mock.patch.object(sync_new_drama_ids, "ROOT", root):
                first = sync_new_drama_ids.backup_local_json_file(source)
                second = sync_new_drama_ids.backup_local_json_file(source)
                source.write_text('{"version": 2}', encoding="utf-8")
                changed = sync_new_drama_ids.backup_local_json_file(source)

            backups = list((root / "recovery_backups").glob("*.json"))
            self.assertEqual(second, first)
            self.assertNotEqual(changed, first)
            self.assertEqual(len(backups), 2)

    def test_local_backup_does_not_deduplicate_different_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "first.json"
            second_source = root / "second.json"
            first_source.write_text("{}", encoding="utf-8")
            second_source.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(sync_new_drama_ids, "ROOT", root):
                first = sync_new_drama_ids.backup_local_json_file(first_source)
                second = sync_new_drama_ids.backup_local_json_file(second_source)

            self.assertNotEqual(first, second)
            self.assertEqual(len(list((root / "recovery_backups").glob("*.json"))), 2)

    def test_load_queue_filters_non_numeric_ids(self) -> None:
        payload = {"manbo": ["200", "drama-1"], "missevan": ["100", "bad"]}
        with unittest.mock.patch.object(
            sync_new_drama_ids,
            "upstash_request",
            return_value=json.dumps(payload),
        ), unittest.mock.patch("builtins.print"):
            queue = sync_new_drama_ids.load_queue()

        self.assertEqual(queue, {"manbo": ["200"], "missevan": ["100"]})

    def test_remote_missing_initializes_cv_map_from_local_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cvid-map.json"
            payload = {"CV A": {"displayName": "CV A", "aliases": []}}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            upstash = Mock(side_effect=[None, None])

            with unittest.mock.patch.object(sync_new_drama_ids, "publish_cvid_map") as publish:
                loaded = sync_new_drama_ids.load_remote_json_or_backup(
                    sync_new_drama_ids.CVID_MAP_KEY,
                    path,
                    {},
                    upstash=upstash,
                    upload_backup_if_missing=True,
                )

        self.assertEqual(loaded, payload)
        self.assertEqual(upstash.call_args_list[0].args[0], ["GET", sync_new_drama_ids.CVID_MAP_KEY])
        self.assertEqual(upstash.call_args_list[1].args[0], ["GET", sync_new_drama_ids.CVID_MAP_KEY])
        publish.assert_called_once_with(payload, upstash=upstash, source_encoded=None)

    def test_remote_invalid_falls_back_to_local_without_uploading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.json"
            fallback = {"series": {"dramaIds": ["1"]}}
            path.write_text(json.dumps(fallback, ensure_ascii=False), encoding="utf-8")
            upstash = Mock(return_value="{bad json")

            loaded = sync_new_drama_ids.load_remote_json_or_backup(
                sync_new_drama_ids.SERIES_INFO_KEY,
                path,
                {},
                upstash=upstash,
            )

        self.assertEqual(loaded, fallback)
        upstash.assert_called_once_with(["GET", sync_new_drama_ids.SERIES_INFO_KEY])

    def test_remote_invalid_does_not_upload_even_when_missing_upload_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cvid-map.json"
            fallback = {"CV A": {"displayName": "CV A", "aliases": []}}
            path.write_text(json.dumps(fallback, ensure_ascii=False), encoding="utf-8")
            upstash = Mock(return_value="{bad json")

            loaded = sync_new_drama_ids.load_remote_json_or_backup(
                sync_new_drama_ids.CVID_MAP_KEY,
                path,
                {},
                upstash=upstash,
                upload_backup_if_missing=True,
            )

        self.assertEqual(loaded, fallback)
        upstash.assert_called_once_with(["GET", sync_new_drama_ids.CVID_MAP_KEY])


class UploadJsonValidationTests(unittest.TestCase):
    def test_info_upload_requires_the_original_downloaded_body(self) -> None:
        payload = {
            str(index): {"dramaId": index, "title": f"剧目 {index}"}
            for index in range(1, 101)
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missevan-info.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            upstash = Mock()

            with self.assertRaisesRegex(RuntimeError, "original downloaded body"):
                sync_new_drama_ids.upload_json_file(
                    sync_new_drama_ids.MISSEVAN_INFO_KEY,
                    path,
                    upstash=upstash,
                )

        upstash.assert_not_called()

    def test_upload_cv_map_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cvid-map.json"
            path.write_text("{bad json", encoding="utf-8")
            upstash = Mock(return_value="OK")

            with self.assertRaises(RuntimeError):
                sync_new_drama_ids.upload_json_file(sync_new_drama_ids.CVID_MAP_KEY, path, upstash=upstash)

        upstash.assert_not_called()

    def test_upload_series_info_rejects_non_object_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.json"
            path.write_text("[]", encoding="utf-8")
            upstash = Mock(return_value="OK")

            with self.assertRaises(RuntimeError):
                sync_new_drama_ids.upload_json_file(sync_new_drama_ids.SERIES_INFO_KEY, path, upstash=upstash)

        upstash.assert_not_called()

    def test_upload_series_info_rejects_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.json"
            path.write_text("{}", encoding="utf-8")
            upstash = Mock(return_value="OK")

            with self.assertRaises(RuntimeError):
                sync_new_drama_ids.upload_json_file(sync_new_drama_ids.SERIES_INFO_KEY, path, upstash=upstash)

        upstash.assert_not_called()

    def test_upload_cv_map_rejects_less_than_half_of_remote_count(self) -> None:
        current_remote = {
            "CV A": {"displayName": "CV A"},
            "CV B": {"displayName": "CV B"},
            "CV C": {"displayName": "CV C"},
            "CV D": {"displayName": "CV D"},
        }
        too_small = {"CV A": {"displayName": "CV A"}}
        upstash = Mock(return_value=json.dumps(current_remote, ensure_ascii=False))

        with self.assertRaises(RuntimeError):
            sync_new_drama_ids.upload_json_payload(sync_new_drama_ids.CVID_MAP_KEY, too_small, upstash=upstash)

        upstash.assert_called_once_with(["GET", sync_new_drama_ids.CVID_MAP_KEY])

    def test_upload_cv_map_allows_at_least_half_of_remote_count(self) -> None:
        current_remote = {
            "CV A": {"displayName": "CV A"},
            "CV B": {"displayName": "CV B"},
            "CV C": {"displayName": "CV C"},
            "CV D": {"displayName": "CV D"},
        }
        candidate = {
            "CV A": {"displayName": "CV A"},
            "CV B": {"displayName": "CV B"},
        }
        upstash = Mock(return_value=json.dumps(current_remote, ensure_ascii=False))

        with unittest.mock.patch.object(sync_new_drama_ids, "publish_cvid_map") as publish:
            sync_new_drama_ids.upload_json_payload(sync_new_drama_ids.CVID_MAP_KEY, candidate, upstash=upstash)

        self.assertEqual(upstash.call_args_list[0].args[0], ["GET", sync_new_drama_ids.CVID_MAP_KEY])
        publish.assert_called_once_with(
            candidate,
            upstash=upstash,
            source_encoded=json.dumps(current_remote, ensure_ascii=False),
        )


class WatchcountSyncTests(unittest.TestCase):
    def test_null_latest_does_not_create_a_false_zero_history_point(self) -> None:
        existing = {
            "100": {
                "name": "剧",
                "points": [["2026-09-13", 123]],
            }
        }
        payload = {
            "_meta": {"updated_at": "2026-09-20T04:00:00+00:00"},
            "counts": {"100": {"name": "剧", "view_count": None}},
        }

        merged = sync_new_drama_ids.merge_watchcount_history(
            existing,
            payload,
            "2026-09-20",
            ["2026-09-13", "2026-09-20"],
        )

        self.assertEqual(merged["100"]["points"], [["2026-09-13", 123]])

    def write_cache(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / "watch-counts.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_remote_newer_watchcount_overwrites_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = {"_meta": {"updated_at": "2026-06-10T00:00:00+00:00"}, "counts": {"100": {"view_count": 1}}}
            remote = {"_meta": {"updated_at": "2026-06-11T00:00:00+00:00"}, "counts": {"100": {"view_count": 2}}}
            path = self.write_cache(tmp, local)
            upstash = Mock(return_value=json.dumps(remote, ensure_ascii=False))

            downloaded = sync_new_drama_ids.sync_remote_watchcount_if_newer("missevan", path, upstash=upstash)
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(downloaded)
            self.assertEqual(saved, remote)
            upstash.assert_called_once_with(["GET", "missevan:watchcount:latest"])

    def test_remote_older_watchcount_keeps_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = {"_meta": {"updated_at": "2026-06-12T00:00:00+00:00"}, "counts": {"100": {"view_count": 3}}}
            remote = {"_meta": {"updated_at": "2026-06-11T00:00:00+00:00"}, "counts": {"100": {"view_count": 2}}}
            path = self.write_cache(tmp, local)
            upstash = Mock(return_value=json.dumps(remote, ensure_ascii=False))

            downloaded = sync_new_drama_ids.sync_remote_watchcount_if_newer("missevan", path, upstash=upstash)
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertFalse(downloaded)
            self.assertEqual(saved, local)

    def test_force_downloads_watchcount_even_when_remote_is_older(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = {"_meta": {"updated_at": "2026-06-12T00:00:00+00:00"}, "counts": {"100": {"view_count": 3}}}
            remote = {"_meta": {"updated_at": "2026-06-11T00:00:00+00:00"}, "counts": {"100": {"view_count": 2}}}
            path = self.write_cache(tmp, local)
            upstash = Mock(return_value=json.dumps(remote, ensure_ascii=False))

            downloaded = sync_new_drama_ids.sync_remote_watchcount_if_newer(
                "missevan",
                path,
                upstash=upstash,
                force=True,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(downloaded)
            self.assertEqual(saved, remote)

    def test_history_only_publish_is_atomic_trims_points_and_keeps_zero(self) -> None:
        fake = WatchcountFakeUpstash()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(tmp, {
                "_meta": {"updated_at": "2026-07-10T04:06:41+00:00"},
                "counts": {"100": {"name": "", "view_count": 0}},
            })
            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=fake)

        history = json.loads(fake.hashes[fake.history_key]["100"])
        self.assertEqual(len(history["points"]), 32)
        self.assertEqual(history["points"][-1], ["2026-07-10", 0])
        self.assertEqual(history["name"], "旧名称")
        self.assertEqual([command[0] for command in fake.commands], ["GET", "HGETALL", "EVAL", "GET", "HGETALL"])
        forbidden = {"SET", "MGET", "SCAN", "DEL"}
        self.assertFalse(any(command[0] in forbidden for command in fake.commands))

    def test_history_only_publish_removes_excluded_ids_in_same_transaction(self) -> None:
        fake = WatchcountFakeUpstash()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(tmp, {
                "_meta": {"updated_at": "2026-07-10T04:06:41+00:00"},
                "counts": {
                    "100": {"name": "剧", "view_count": 20},
                    "200": {"name": "归档剧", "view_count": 99},
                },
            })
            sync_new_drama_ids.upload_watchcount_file(
                "missevan", path, upstash=fake, excluded_drama_ids={"200"}
            )

        self.assertNotIn("200", fake.hashes[fake.history_key])
        self.assertNotIn("200", json.loads(fake.strings[fake.latest_key])["counts"])
        self.assertEqual(sum(command[0] == "EVAL" for command in fake.commands), 1)

    def test_history_only_publish_retries_cas_conflict(self) -> None:
        fake = WatchcountFakeUpstash(conflict_once=True)
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch("builtins.print"):
            path = self.write_cache(tmp, {
                "_meta": {"updated_at": "2026-07-10T04:06:41+00:00"},
                "counts": {"100": {"name": "剧", "view_count": 20}},
            })
            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=fake)

        self.assertEqual(sum(command[0] == "EVAL" for command in fake.commands), 2)

    def test_history_only_publish_fails_before_write_when_history_is_missing(self) -> None:
        fake = WatchcountFakeUpstash()
        fake.hashes[fake.history_key] = {}
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(tmp, {
                "_meta": {"updated_at": "2026-07-10T04:06:41+00:00"},
                "counts": {"100": {"view_count": 20}},
            })
            with self.assertRaisesRegex(RuntimeError, "history.*empty or missing"):
                sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=fake)

        self.assertFalse(any(command[0] == "EVAL" for command in fake.commands))

    def test_remote_watchcount_missing_counts_is_rejected_before_overwriting_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = {"_meta": {"updated_at": "2026-06-10T00:00:00+00:00"}, "counts": {"100": {"view_count": 1}}}
            remote = {"_meta": {"updated_at": "2026-06-11T00:00:00+00:00"}}
            path = self.write_cache(tmp, local)
            upstash = Mock(return_value=json.dumps(remote, ensure_ascii=False))

            with self.assertRaisesRegex(RuntimeError, "missing counts object"):
                sync_new_drama_ids.sync_remote_watchcount_if_newer("missevan", path, upstash=upstash)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), local)


class QueueReadyTests(unittest.TestCase):
    def test_unknown_cv_marker_completes_both_platforms(self) -> None:
        missevan = {
            "title": "猫耳剧",
            "type": 4,
            "catalog": 89,
            "author": "作者",
            "createTime": "",
            "cover": "https://example.test/m.jpg",
            "is_member": False,
            "fallbackCvNames": ["主役未知"],
        }
        manbo = {
            "name": "漫播剧",
            "catalog": 1,
            "createTime": "2026-08",
            "genre": "广播剧",
            "cover": "https://example.test/b.jpg",
            "vipFree": 0,
            "mainCvNames": ["主役未知"],
            "mainCvIds": [],
        }

        self.assertTrue(sync_new_drama_ids.is_missevan_ready(missevan))
        self.assertTrue(sync_new_drama_ids.is_manbo_ready(manbo))

        missevan["cover"] = ""
        manbo["genre"] = ""
        self.assertFalse(sync_new_drama_ids.is_missevan_ready(missevan))
        self.assertFalse(sync_new_drama_ids.is_manbo_ready(manbo))

    def test_prune_queue_consumes_ids_rejected_by_detail_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan.json"
            manbo_path = root / "manbo.json"
            missevan_path.write_text("{}", encoding="utf-8")
            manbo_path.write_text('{"records": []}', encoding="utf-8")
            queue = {"manbo": ["201"], "missevan": ["99999"]}

            with (
                unittest.mock.patch.object(sync_new_drama_ids, "MISSEVAN_INFO_PATH", missevan_path),
                unittest.mock.patch.object(sync_new_drama_ids, "MANBO_INFO_PATH", manbo_path),
            ):
                remaining = sync_new_drama_ids.prune_queue(queue)

        self.assertEqual(remaining, {"manbo": [], "missevan": []})

    def test_prune_queue_keeps_existing_incomplete_record_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missevan_path = root / "missevan.json"
            manbo_path = root / "manbo.json"
            missevan_path.write_text("{}", encoding="utf-8")
            manbo_path.write_text(
                json.dumps({"records": [{"dramaId": "123", "name": "待完善剧集"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                unittest.mock.patch.object(sync_new_drama_ids, "MISSEVAN_INFO_PATH", missevan_path),
                unittest.mock.patch.object(sync_new_drama_ids, "MANBO_INFO_PATH", manbo_path),
            ):
                remaining = sync_new_drama_ids.prune_queue({"manbo": ["123"], "missevan": []})

        self.assertEqual(remaining, {"manbo": ["123"], "missevan": []})

    def test_missevan_ready_requires_cover(self) -> None:
        base = {
            "title": "猫耳剧",
            "type": 0,
            "catalog": "现代",
            "createTime": "2026-06-10",
            "cover": "https://example.test/cover.jpg",
            "is_member": False,
            "maincvs": [1, 2],
        }

        self.assertTrue(sync_new_drama_ids.is_missevan_ready(base))

        without_cover = dict(base)
        without_cover["cover"] = ""
        self.assertFalse(sync_new_drama_ids.is_missevan_ready(without_cover))

    def test_missevan_ready_counts_name_only_main_cv(self) -> None:
        record = {
            "title": "猫耳剧",
            "type": 4,
            "catalog": 89,
            "author": "作者",
            "createTime": "",
            "cover": "https://example.test/cover.jpg",
            "is_member": True,
            "maincvs": [3946],
            "cvnames": {"3946": "辰朔"},
            "fallbackCvNames": ["林风"],
        }

        self.assertTrue(sync_new_drama_ids.is_missevan_ready(record))

    def test_manbo_ready_requires_cover(self) -> None:
        base = {
            "name": "漫播剧",
            "catalog": 1,
            "createTime": "2026-06-10",
            "genre": "广播剧",
            "cover": "https://example.test/cover.jpg",
            "vipFree": False,
            "mainCvNicknames": ["甲", "乙"],
        }

        self.assertTrue(sync_new_drama_ids.is_manbo_ready(base))
        without_cover = dict(base)
        without_cover["cover"] = ""
        self.assertFalse(sync_new_drama_ids.is_manbo_ready(without_cover))

    def test_required_remote_watchcount_rejects_missing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "counts.json"
            path.write_text('{"_meta":{"updated_at":"2026-07-10T00:00:00+00:00"},"counts":{}}', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "empty or missing"):
                sync_new_drama_ids.sync_remote_watchcount_if_newer(
                    "missevan",
                    path,
                    upstash=Mock(return_value=None),
                    force=True,
                    require_remote=True,
                )


class InvalidManboIdCleanupTests(unittest.TestCase):
    def test_cleanup_removes_invalid_ids_from_active_remote_stores_and_local_copies(self) -> None:
        remote = {
            sync_new_drama_ids.QUEUE_KEY: json.dumps(
                {"manbo": ["200", "drama-1"], "missevan": ["100"]}, ensure_ascii=False
            ),
            sync_new_drama_ids.MANBO_INFO_KEY: json.dumps(
                {"version": 1, "records": [{"dramaId": "200"}, {"dramaId": "drama-1"}]}, ensure_ascii=False
            ),
            "manbo:watchcount:latest": json.dumps(
                {"_meta": {}, "counts": {"200": {"view_count": 1}, "drama-1": {"view_count": None}}},
                ensure_ascii=False,
            ),
        }

        def fake_upstash(command):
            if command[0] == "GET":
                return remote[command[1]]
            if command[0] == "EVAL":
                if command[1] == sync_new_drama_ids.publish_info_v2.__globals__["INFO_SOURCE_COMPARE_AND_PUBLISH_SCRIPT"]:
                    remote[command[3]] = command[6]
                    remote[command[4]] = command[7]
                    return 1
                key = command[3]
                remote[key] = command[5]
                return 1
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as tmp:
            info_path = Path(tmp) / "manbo-info.json"
            counts_path = Path(tmp) / "manbo-counts.json"
            stats = sync_new_drama_ids.cleanup_invalid_manbo_ids(
                upstash=fake_upstash,
                info_path=info_path,
                counts_path=counts_path,
                backup_dir=Path(tmp) / "backups",
            )

            self.assertEqual(stats, {"queue": 1, "info": 1, "watchcount": 1})
            self.assertEqual(json.loads(remote[sync_new_drama_ids.QUEUE_KEY])["manbo"], ["200"])
            self.assertEqual(json.loads(remote[sync_new_drama_ids.MANBO_INFO_KEY])["records"], [{"dramaId": "200"}])
            self.assertEqual(list(json.loads(remote["manbo:watchcount:latest"])["counts"]), ["200"])
            self.assertEqual(json.loads(info_path.read_text(encoding="utf-8"))["records"], [{"dramaId": "200"}])
            self.assertEqual(list(json.loads(counts_path.read_text(encoding="utf-8"))["counts"]), ["200"])
            self.assertEqual(len(list((Path(tmp) / "backups").glob("*.json"))), 3)

    def test_cleanup_stops_on_compare_and_set_conflict(self) -> None:
        queue = json.dumps({"manbo": ["drama-1"], "missevan": []})
        upstash = Mock(side_effect=[queue, 0])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "changed concurrently"):
            sync_new_drama_ids.cleanup_invalid_manbo_ids(upstash=upstash, backup_dir=Path(tmp))

        self.assertEqual(upstash.call_args_list[1].args[0][0], "EVAL")


class NonTargetPurgeTests(unittest.TestCase):
    def test_rank_publish_retries_meta_race_and_preserves_cv_resources(self) -> None:
        original = json.dumps(
            {
                "_meta": {"updated_at": "2026-07-22T00:00:00+00:00"},
                "missevan": {"dramas": {}, "ranks": {}},
                "manbo": {"dramas": {}, "ranks": {}},
            },
            ensure_ascii=False,
        )
        strings = {
            "ranks:latest": original,
            sync_new_drama_ids.RANK_META_KEY: json.dumps(
                {
                    "normal": {"resources": {}},
                    "cv": {"resources": {"ranks:cv:latest": {"contentSha1": "old"}}},
                },
                separators=(",", ":"),
            ),
        }
        eval_count = 0

        def fake_upstash(command):
            nonlocal eval_count
            if command[0] == "GET":
                return strings.get(command[1])
            if command[0] == "EVAL":
                eval_count += 1
                if eval_count == 1:
                    current = json.loads(strings[sync_new_drama_ids.RANK_META_KEY])
                    current["cv"]["resources"]["ranks:cv:latest"] = {"contentSha1": "raced"}
                    strings[sync_new_drama_ids.RANK_META_KEY] = json.dumps(
                        current,
                        separators=(",", ":"),
                    )
                    return -2
                strings[command[3]] = command[6]
                strings[command[4]] = command[7]
                return 1
            raise AssertionError(command)

        payload = json.loads(original)
        payload["missevan"]["dramas"] = {"100": {"name": "保留"}}
        sync_new_drama_ids._publish_rank_latest_cas(original, payload, upstash=fake_upstash)

        self.assertEqual(eval_count, 2)
        final_meta = json.loads(strings[sync_new_drama_ids.RANK_META_KEY])
        self.assertEqual(
            final_meta["cv"]["resources"]["ranks:cv:latest"]["contentSha1"],
            "raced",
        )
        self.assertIn("ranks:latest", final_meta["normal"]["resources"])

    def test_purge_backup_directories_are_unique_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = sync_new_drama_ids.create_purge_backup_dir(root=root)
            second = sync_new_drama_ids.create_purge_backup_dir(root=root)

        self.assertNotEqual(first, second)
        self.assertIn("purge_non_target_records", first.name)

    def test_rank_cleanup_removes_only_id_references_not_numeric_metrics(self) -> None:
        payload = {
            "missevan": {
                "dramas": {"94774": {"name": "自传"}, "100": {"view_count": 201}},
                "ranks": {
                    "hot": {
                        "items": [
                            {"dramaId": "94774", "position": 1},
                            {"dramaId": "100", "view_count": 201},
                        ]
                    }
                },
            },
            "manbo": {"dramas": {}, "ranks": {}},
        }

        removed = sync_new_drama_ids.purge_rank_store(payload, sync_new_drama_ids.PURGE_TARGETS)

        self.assertEqual(removed["missevan_dramas"], 1)
        self.assertEqual(payload["missevan"]["ranks"]["hot"]["items"], [{"dramaId": "100", "view_count": 201}])

    def test_rank_cleanup_filters_multi_drama_items(self) -> None:
        rank = {"items": [{"name": "系列", "dramaIds": ["94774", "100"]}]}

        removed = sync_new_drama_ids._purge_rank_items(rank, {"94774"})

        self.assertEqual(removed, 1)
        self.assertEqual(rank["items"][0]["dramaIds"], ["100"])

    def test_purge_cli_is_dry_run_without_apply(self) -> None:
        args = sync_new_drama_ids.parse_args(["--purge-non-target-records"])

        self.assertTrue(args.purge_non_target_records)
        self.assertFalse(args.apply)

    def test_purge_validation_treats_absent_targets_as_already_removed(self) -> None:
        found, missing = sync_new_drama_ids._validate_purge_targets({}, {"records": []})

        self.assertEqual(found, [])
        self.assertEqual(len(missing), 16)
        self.assertEqual({item["dramaId"] for item in missing}, set().union(*sync_new_drama_ids.PURGE_TARGETS.values()))


if __name__ == "__main__":
    unittest.main()
