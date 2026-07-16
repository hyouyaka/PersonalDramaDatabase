import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call

import sync_new_drama_ids


class RemoteJsonBackupTests(unittest.TestCase):
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
            upstash = Mock(side_effect=[None, None, "OK"])

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
        self.assertEqual(upstash.call_args_list[2].args[0][:2], ["SET", sync_new_drama_ids.CVID_MAP_KEY])
        self.assertEqual(json.loads(upstash.call_args_list[2].args[0][2]), payload)

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
        upstash = Mock(side_effect=[json.dumps(current_remote, ensure_ascii=False), "OK"])

        sync_new_drama_ids.upload_json_payload(sync_new_drama_ids.CVID_MAP_KEY, candidate, upstash=upstash)

        self.assertEqual(upstash.call_args_list[0].args[0], ["GET", sync_new_drama_ids.CVID_MAP_KEY])
        self.assertEqual(upstash.call_args_list[1].args[0][:2], ["SET", sync_new_drama_ids.CVID_MAP_KEY])


class WatchcountSyncTests(unittest.TestCase):
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

    def test_upload_watchcount_file_writes_date_and_latest_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-06-12T04:17:39+00:00"}, "counts": {"100": {"view_count": 9}}},
            )
            snapshot = path.read_text(encoding="utf-8")
            upstash = Mock(side_effect=[
                "OK", "OK", [], None,
                ["0", ["missevan:watchcount:2026-06-12"]], [snapshot], 1, "OK",
            ])

            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        self.assertEqual(upstash.call_args_list[0].args[0][0:2], ["SET", "missevan:watchcount:2026-06-12"])
        self.assertEqual(upstash.call_args_list[1].args[0][0:2], ["SET", "missevan:watchcount:latest"])
        self.assertEqual(json.loads(upstash.call_args_list[0].args[0][2])["counts"]["100"]["view_count"], 9)
        history = upstash.call_args_list[6].args[0]
        self.assertEqual(history[:2], ["HSET", "missevan:watchcount:history"])
        self.assertEqual(json.loads(history[3]), {"name": "", "points": [["2026-06-12", 9]]})
        index = json.loads(upstash.call_args_list[7].args[0][2])
        self.assertEqual(index["dates"], ["2026-06-12"])

    def test_upload_watchcount_file_backfills_existing_dates_on_first_index_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {}},
            )
            snapshots = [
                json.dumps({"_meta": {"updated_at": f"{date}T04:06:41+00:00"}, "counts": {"100": {"name": "剧", "view_count": count}}})
                for date, count in (("2026-06-19", 10), ("2026-06-26", 20), ("2026-07-03", 30), ("2026-07-10", 40))
            ]
            upstash = Mock(
                side_effect=[
                    "OK",
                    "OK",
                    [],
                    None,
                    [
                        "0",
                        [
                            "missevan:watchcount:2026-07-03",
                            "missevan:watchcount:2026-06-19",
                            "missevan:watchcount:2026-07-10",
                            "missevan:watchcount:2026-06-26",
                            "missevan:watchcount:latest",
                        ],
                    ],
                    snapshots,
                    1,
                    "OK",
                ]
            )

            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        self.assertEqual(
            [call.args[0][:2] for call in upstash.call_args_list],
            [
                ["SET", "missevan:watchcount:2026-07-10"],
                ["SET", "missevan:watchcount:latest"],
                ["HGETALL", "missevan:watchcount:history"],
                ["GET", "missevan:watchcount:index"],
                ["SCAN", "0"],
                ["MGET", "missevan:watchcount:2026-06-19"],
                ["HSET", "missevan:watchcount:history"],
                ["SET", "missevan:watchcount:index"],
            ],
        )
        history = json.loads(upstash.call_args_list[-2].args[0][3])
        self.assertEqual(history["points"], [["2026-06-19", 10], ["2026-06-26", 20], ["2026-07-03", 30], ["2026-07-10", 40]])
        index = json.loads(upstash.call_args_list[-1].args[0][2])
        self.assertEqual(index, {
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-07-10T04:06:41Z",
            "dates": ["2026-06-19", "2026-06-26", "2026-07-03", "2026-07-10"],
        })

    def test_upload_watchcount_file_prunes_old_dates_only_after_index_write(self) -> None:
        dates = [f"2026-05-{day:02d}" for day in range(1, 32)] + ["2026-06-01"]
        current_index = json.dumps({
            "version": 1,
            "platform": "manbo",
            "updated_at": "2026-07-03T04:06:41Z",
            "dates": dates,
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {"100": {"name": "剧", "view_count": 99}}},
            )
            history = json.dumps({
                "name": "剧",
                "points": [[date_text, index] for index, date_text in enumerate(dates)],
            })
            all_keys = [f"manbo:watchcount:{date}" for date in dates] + ["manbo:watchcount:2026-07-10"]
            upstash = Mock(side_effect=["OK", "OK", ["100", history], current_index, 1, "OK", 0, ["0", all_keys], 1])

            sync_new_drama_ids.upload_watchcount_file("manbo", path, upstash=upstash)

        self.assertEqual(upstash.call_args_list[5].args[0][0:2], ["SET", "manbo:watchcount:index"])
        staged_history = json.loads(upstash.call_args_list[4].args[0][3])
        trimmed_history = json.loads(upstash.call_args_list[6].args[0][3])
        self.assertEqual(len(staged_history["points"]), 33)
        self.assertEqual(len(trimmed_history["points"]), 32)
        self.assertEqual(staged_history["points"][0][0], "2026-05-01")
        self.assertEqual(trimmed_history["points"][0][0], "2026-05-02")
        self.assertEqual(
            upstash.call_args_list[8].args[0],
            ["DEL", "manbo:watchcount:2026-05-01"],
        )
        index = json.loads(upstash.call_args_list[5].args[0][2])
        self.assertEqual(index["dates"], dates[1:] + ["2026-07-10"])

    def test_index_write_failure_stops_before_snapshot_deletion(self) -> None:
        dates = [f"2026-05-{day:02d}" for day in range(1, 32)] + ["2026-06-01"]
        current_index = json.dumps({
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-07-03T04:06:41Z",
            "dates": dates,
        })
        existing_history = json.dumps({
            "name": "剧",
            "points": [[date_text, index] for index, date_text in enumerate(dates)],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {"100": {"name": "剧", "view_count": 99}}},
            )
            upstash = Mock(side_effect=["OK", "OK", ["100", existing_history], current_index, 1, "NO"])

            with self.assertRaisesRegex(RuntimeError, "index"):
                sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        staged_history = json.loads(upstash.call_args_list[4].args[0][3])
        self.assertEqual(len(staged_history["points"]), 33)
        self.assertEqual(staged_history["points"][0][0], "2026-05-01")
        self.assertEqual(staged_history["points"][-1], ["2026-07-10", 99])
        self.assertNotIn("DEL", [call.args[0][0] for call in upstash.call_args_list])

    def test_history_trim_failure_happens_after_index_commit_and_before_delete(self) -> None:
        dates = [f"2026-05-{day:02d}" for day in range(1, 32)] + ["2026-06-01"]
        current_index = json.dumps({
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-06-01T04:06:41Z",
            "dates": dates,
        })
        existing_history = json.dumps({
            "name": "剧",
            "points": [[date_text, index] for index, date_text in enumerate(dates)],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {"100": {"name": "剧", "view_count": 99}}},
            )
            upstash = Mock(side_effect=["OK", "OK", ["100", existing_history], current_index, 1, "OK", "NO"])

            with self.assertRaisesRegex(RuntimeError, "trim history"):
                sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        self.assertEqual(upstash.call_args_list[5].args[0][:2], ["SET", "missevan:watchcount:index"])
        self.assertEqual(upstash.call_args_list[6].args[0][:2], ["HSET", "missevan:watchcount:history"])
        self.assertNotIn("DEL", [call.args[0][0] for call in upstash.call_args_list])

    def test_history_update_is_idempotent_and_keeps_zero(self) -> None:
        current_index = json.dumps({
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-07-03T04:06:41Z",
            "dates": ["2026-07-03", "2026-07-10"],
        })
        existing_history = [
            "100",
            json.dumps({
                "name": "旧名称",
                "points": [["2026-07-03", 12], ["2026-07-10", 8]],
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {"100": {"name": "", "view_count": 0}, "bad": {"view_count": "NaN"}}},
            )
            upstash = Mock(side_effect=["OK", "OK", existing_history, current_index, 0, "OK"])

            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        history_command = upstash.call_args_list[4].args[0]
        history = json.loads(history_command[3])
        self.assertEqual(history, {"name": "旧名称", "points": [["2026-07-03", 12], ["2026-07-10", 0]]})

    def test_history_write_failure_stops_before_index_and_snapshot_deletion(self) -> None:
        current_index = json.dumps({
            "version": 1,
            "platform": "manbo",
            "updated_at": "2026-07-03T04:06:41Z",
            "dates": ["2026-07-03"],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_cache(
                tmp,
                {"_meta": {"updated_at": "2026-07-10T04:06:41+00:00"}, "counts": {"100": {"view_count": 1}}},
            )
            upstash = Mock(side_effect=["OK", "OK", ["100", json.dumps({"name": "剧", "points": [["2026-07-03", 1]]})], current_index, "NO"])

            with self.assertRaisesRegex(RuntimeError, "history"):
                sync_new_drama_ids.upload_watchcount_file("manbo", path, upstash=upstash)

        self.assertNotIn("SET", [call.args[0][0] for call in upstash.call_args_list[4:]])
        self.assertNotIn("DEL", [call.args[0][0] for call in upstash.call_args_list])

    def test_load_watchcount_snapshot_dates_prefers_index_without_scan(self) -> None:
        index = json.dumps({
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-07-10T04:06:41Z",
            "dates": ["2026-06-19", "2026-06-26"],
        })
        sync_new_drama_ids.clear_watchcount_scan_cache()
        upstash = Mock(return_value=index)

        dates = sync_new_drama_ids.load_watchcount_snapshot_dates("missevan", upstash=upstash)

        self.assertEqual(dates, ["2026-06-19", "2026-06-26"])
        upstash.assert_called_once_with(["GET", "missevan:watchcount:index"])

    def test_load_watchcount_snapshot_dates_uses_cached_scan_when_index_missing(self) -> None:
        sync_new_drama_ids.clear_watchcount_scan_cache()
        upstash = Mock(side_effect=[None, ["0", [
            "manbo:watchcount:2026-07-03",
            "manbo:watchcount:latest",
        ]], None])

        first = sync_new_drama_ids.load_watchcount_snapshot_dates("manbo", upstash=upstash)
        second = sync_new_drama_ids.load_watchcount_snapshot_dates("manbo", upstash=upstash)

        self.assertEqual(first, ["2026-07-03"])
        self.assertEqual(second, first)
        self.assertEqual(
            [call.args[0][0] for call in upstash.call_args_list],
            ["GET", "SCAN", "GET"],
        )

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


if __name__ == "__main__":
    unittest.main()
