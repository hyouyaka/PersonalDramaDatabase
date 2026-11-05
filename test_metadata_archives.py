import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import archive_manager
import refresh_platform_metadata as metadata
from test_archive_manager import FakeUpstash


class MetadataArchiveTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.info_path = root / "info.json"
        self.counts_path = root / "counts.json"
        self.info_archive_path = root / "info-archive.json"
        self.watch_archive_path = root / "watch-archive.json"
        self.stack.enter_context(patch.object(metadata, "MISSEVAN_INFO_PATH", self.info_path))
        self.stack.enter_context(patch.object(metadata, "MISSEVAN_COUNTS_PATH", self.counts_path))
        for paths, path in (
            (archive_manager.ACTIVE_INFO_PATHS, self.info_path),
            (archive_manager.ACTIVE_WATCHCOUNT_PATHS, self.counts_path),
            (archive_manager.ARCHIVE_INFO_PATHS, self.info_archive_path),
            (archive_manager.ARCHIVE_WATCHCOUNT_PATHS, self.watch_archive_path),
        ):
            self.stack.enter_context(patch.dict(paths, {"missevan": path}))
        self.stack.enter_context(patch.dict(metadata.ARCHIVE_ENABLED, {"missevan": True}))
        self.stack.enter_context(patch.object(metadata, "ARCHIVE_RETRY_DELAYS", (0, 0, 0)))
        self.stack.enter_context(patch("builtins.print"))
        self.store = {
            "100": {"dramaId": 100, "title": "待归档", "maincvs": [123],
                    "cvnames": {"123": "主役"}, "cvroles": {"123": "角色"}},
            "101": {"dramaId": 101, "title": "保留", "seriesTitle": "保留"},
        }
        self.cache = {
            "_meta": {"updated_at": "2026-11-01T00:00:00+00:00"},
            "counts": {
                "100": {"name": "待归档", "view_count": 10, "fetched_at": "2026-11-01"},
                "101": {"name": "保留", "view_count": 20, "fetched_at": "2026-11-01"},
            },
        }
        self.old_info = {
            "archivedAt": "2026-10-01T00:00:00+00:00", "archivedReason": "HTTP_403",
            "record": {"dramaId": 99, "title": "旧归档"},
        }
        self.old_watch = {
            "archivedAt": self.old_info["archivedAt"], "archivedReason": "HTTP_403",
            "latest": {"name": "旧归档", "view_count": 5},
            "history": {"name": "旧归档", "points": [["2026-10-01", 5]]},
        }
        for path, value in (
            (self.info_path, self.store), (self.counts_path, self.cache),
            (self.info_archive_path, self.archive({"99": self.old_info})),
            (self.watch_archive_path, self.archive({"99": self.old_watch})),
        ):
            self.write(path, value)

    @staticmethod
    def archive(records):
        return {"version": 1, "platform": "missevan", "updatedAt": None, "records": records}

    @staticmethod
    def write(path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def http_403():
        error = RuntimeError("HTTP 403")
        error.response = Mock(status_code=403)
        return error

    def refresh(self, *, update_counts=True, payloads=None, target_ids=None):
        requester = Mock(request_count=4, last_backoff_seconds=0)
        requester.request_json.side_effect = payloads if payloads is not None else [self.http_403() for _ in range(4)]
        with patch.object(metadata, "MissevanRequester", return_value=requester):
            result = metadata.refresh_missevan(
                target_drama_ids=target_ids or {"100"}, update_counts=update_counts,
            )
        self.assertEqual(requester.request_json.call_count, 4)
        return result

    def assert_saved_archive(self):
        # Use the actual consumer, so an old top-level archive cannot pass.
        info, watch = archive_manager.load_local_archives("missevan")
        self.assertEqual(set(info), {"version", "platform", "updatedAt", "records"})
        self.assertEqual(set(watch), {"version", "platform", "updatedAt", "records"})
        self.assertEqual(set(info["records"]), {"99", "100"})
        self.assertEqual(info["records"]["100"]["record"], self.store["100"])
        self.assertEqual(watch["records"]["100"]["latest"], self.cache["counts"]["100"])
        self.assertEqual(info["records"]["99"], self.old_info)
        self.assertEqual(watch["records"]["99"], self.old_watch)
        for entry in [info["records"]["100"], watch["records"]["100"]]:
            self.assertEqual(entry["archivedReason"], "HTTP_403")
            self.assertTrue(entry["archivedAt"])
        self.assertEqual(self.read(self.info_path), {"101": self.store["101"]})
        self.assertEqual(self.read(self.counts_path)["counts"], {"101": self.cache["counts"]["101"]})
        return info, watch

    def test_four_403s_save_metadata_and_latest_in_unified_archives(self):
        result = self.refresh()
        info, _watch = self.assert_saved_archive()
        self.assertEqual((result["processed"], result["archived"], result["failed"]), (0, 1, 0))
        self.assertEqual(result["archive_candidates"], {
            "100": {key: info["records"]["100"][key] for key in ["archivedAt", "archivedReason"]},
        })

    def test_archive_preserves_existing_count_when_count_refresh_disabled(self):
        result = self.refresh(update_counts=False)
        self.assert_saved_archive()
        self.assertEqual(result["count_entries_updated"], 0)
        self.assertEqual(result["archived"], 1)

    def test_archiving_preserves_existing_history_for_same_drama(self):
        history = {"name": "待归档", "points": [["2026-10-01", 9]]}
        existing = {**self.old_watch, "history": history}
        self.write(self.watch_archive_path, self.archive({"99": self.old_watch, "100": existing}))
        self.refresh()
        _info, watch = self.assert_saved_archive()
        self.assertEqual(watch["records"]["100"]["history"], history)

    def test_completed_archive_survives_later_418_interruption(self):
        calls = {"100": 0, "101": 0}

        def request(url):
            drama_id = url.rsplit("=", 1)[1]
            calls[drama_id] += 1
            if drama_id == "101" and calls[drama_id] == 4:
                raise RuntimeError("HTTP_418")
            raise self.http_403()

        with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
            self.refresh(payloads=request, target_ids={"100", "101"})
        self.assert_saved_archive()
        self.assertEqual(calls, {"100": 4, "101": 4})

    def test_metadata_candidate_publishes_latest_and_history_without_clobbering_fresh_local_data(self):
        original_info = self.info_path.read_text(encoding="utf-8")
        original_cache = self.counts_path.read_text(encoding="utf-8")
        history = {"name": "待归档", "points": [["2026-10-25", 9], ["2026-11-01", 10]]}
        fake = FakeUpstash(strings={
            "missevan:info:v2": original_info,
            "missevan:watchcount:latest": original_cache,
            "missevan:info:archive:v1": self.info_archive_path.read_text(encoding="utf-8"),
            "missevan:watchcount:archive:v1": self.watch_archive_path.read_text(encoding="utf-8"),
        }, hashes={"missevan:watchcount:history": {"100": json.dumps(history)}})
        result = self.refresh()
        fresh_info = self.read(self.info_path)
        fresh_info["101"]["title"] = "本轮刷新后的标题"
        fresh_cache = self.read(self.counts_path)
        fresh_cache["counts"]["101"]["view_count"] = 25
        self.write(self.info_path, fresh_info)
        self.write(self.counts_path, fresh_cache)
        archive_manager.publish_archive_candidates(
            "missevan", result["archive_candidates"], upstash=fake,
            sync_local_info=False, sync_local_watchcount=False,
        )
        self.assertEqual(self.read(self.info_path), fresh_info)
        self.assertEqual(self.read(self.counts_path), fresh_cache)
        info, watch = archive_manager.load_local_archives("missevan")
        self.assertEqual(info["records"]["100"]["record"], self.store["100"])
        self.assertEqual(watch["records"]["100"]["latest"], self.cache["counts"]["100"])
        self.assertEqual(watch["records"]["100"]["history"], history)
        self.assertEqual(watch["records"]["99"], self.old_watch)
        self.assertEqual(set(json.loads(fake.strings["missevan:info:v2"])), {"101"})
        self.assertEqual(set(json.loads(fake.strings["missevan:watchcount:latest"])["counts"]), {"101"})
        self.assertNotIn("100", fake.hashes["missevan:watchcount:history"])


if __name__ == "__main__":
    unittest.main()
