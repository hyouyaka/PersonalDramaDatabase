import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import sync_new_drama_ids


class RemoteJsonBackupTests(unittest.TestCase):
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
            upstash = Mock(return_value="OK")

            sync_new_drama_ids.upload_watchcount_file("missevan", path, upstash=upstash)

        self.assertEqual(upstash.call_args_list[0].args[0][0:2], ["SET", "missevan:watchcount:2026-06-12"])
        self.assertEqual(upstash.call_args_list[1].args[0][0:2], ["SET", "missevan:watchcount:latest"])
        self.assertEqual(json.loads(upstash.call_args_list[0].args[0][2])["counts"]["100"]["view_count"], 9)

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


if __name__ == "__main__":
    unittest.main()
