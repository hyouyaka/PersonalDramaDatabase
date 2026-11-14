import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import append_manbo_ids
import append_missevan_ids
import refresh_platform_metadata as metadata
import sync_new_drama_ids as sync
from test_upstash_v2 import FakeUpstash


class AppendInfoBackupTests(unittest.TestCase):
    def test_blocked_producer_is_deleted_through_refresh_merge_and_cas_publication(self):
        records = [{"dramaId": str(i), "name": f"existing {i}"} for i in range(100, 152)]
        remote = FakeUpstash()
        remote.strings[sync.MANBO_INFO_KEY] = json.dumps({"version": 1, "records": records})
        cache = {"counts": {"123": {"view_count": 42}}}

        def detail(url):
            drama_id = url.rsplit("=", 1)[-1]
            if drama_id == "123":
                # A concurrent addition before the merge read must be preserved.
                current = json.loads(remote.strings[sync.MANBO_INFO_KEY])
                current["records"].append({"dramaId": "999", "name": "remote only"})
                remote.strings[sync.MANBO_INFO_KEY] = json.dumps(current)
                return {"data": {"title": "blocked", "category": 5, "ownerId": 3509860831285}}
            if drama_id == "124":
                return {"data": {}}  # An inconclusive response must not authorize deletion.
            return {"data": {"title": "updated", "category": 5, "watchCount": 10}}

        with tempfile.TemporaryDirectory() as tmp:
            info_path = Path(tmp) / "info.json"
            map_path = Path(tmp) / "cv-map.json"
            missevan_path = Path(tmp) / "missevan.json"
            info_path.write_text(remote.strings[sync.MANBO_INFO_KEY], encoding="utf-8")
            map_path.write_text("{}", encoding="utf-8")
            missevan_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(append_manbo_ids, "MANBO_INFO_PATH", info_path),
                patch.object(append_manbo_ids, "COMBINED_CVID_MAP_PATH", map_path),
                patch.object(append_manbo_ids, "MISSEVAN_INFO_PATH", missevan_path),
                patch.object(append_manbo_ids, "load_env_file"),
                patch.object(append_manbo_ids, "download_info_file"),
                patch.object(append_manbo_ids, "download_support_files"),
                patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
                patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements", return_value={}),
                patch.object(append_manbo_ids, "UpstashGeneratedManboCvIdAllocator"),
                patch.object(append_manbo_ids, "update_combined_cvid_map", return_value={
                    "updated": 0, "created": 0, "unchanged": 0,
                    "ambiguous_count": 0, "ambiguous_samples": [],
                }),
                patch.object(metadata, "MANBO_INFO_PATH", info_path),
                patch.object(metadata, "load_cache", return_value=cache),
                patch.object(metadata, "save_cache"),
                patch.object(metadata, "load_combined_map", return_value={}),
                patch.object(metadata, "build_manbo_cv_name_map", return_value={}),
                patch.object(metadata, "request_manbo_json", side_effect=detail),
                patch.object(metadata, "build_manbo_record", side_effect=lambda r, p, *a, **kw:
                             {**r, "name": p["data"]["title"], "catalog": 5}),
                patch.object(sync, "upstash_request", remote),
                patch("builtins.print"),
            ):
                self.assertEqual(append_manbo_ids.main(["append.py", "123", "124", "125"]), 0)
            published = json.loads(remote.strings[sync.MANBO_INFO_KEY])
            by_id = {r["dramaId"]: r for r in published["records"]}
            self.assertNotIn("123", by_id)
            self.assertEqual(by_id["124"]["name"], "existing 124")
            self.assertEqual(by_id["125"]["name"], "updated")
            self.assertEqual(by_id["999"]["name"], "remote only")
            self.assertEqual(len(by_id), 52)
            self.assertNotIn("123", cache["counts"])
            self.assertEqual(json.loads(info_path.read_text(encoding="utf-8"))["records"], published["records"])
            meta = json.loads(remote.strings["manbo:info:meta:v2"])
            self.assertEqual(meta["recordCount"], 52)
            self.assertEqual(meta["contentSha1"], hashlib.sha1(remote.strings[sync.MANBO_INFO_KEY].encode("utf-8")).hexdigest())
            self.assertEqual(meta["bytes"], len(remote.strings[sync.MANBO_INFO_KEY].encode("utf-8")))

    def test_missevan_publishes_metadata_archives_before_merging_fresh_info(self) -> None:
        candidates = {"100": {"archivedAt": "2026-11-05T00:00:00+00:00", "archivedReason": "HTTP_403"}}
        calls = Mock()
        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file"),
            patch.object(append_missevan_ids, "download_support_files"),
            patch.object(append_missevan_ids, "seed_generated_missevan_cvid_registry", return_value=0),
            patch.object(append_missevan_ids, "load_generated_missevan_cvid_replacements", return_value={}),
            patch.object(append_missevan_ids, "upsert_missevan_drama_ids", return_value={
                "processed": 1, "request_count": 5, "last_backoff_seconds": 0,
                "archive_candidates": candidates,
            }),
            patch.object(append_missevan_ids, "publish_archive_candidates") as publish,
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(append_missevan_ids, "update_combined_cvid_map", return_value={
                "updated": 0, "created": 0, "unchanged": 0, "ambiguous_count": 0,
                "ambiguous_samples": [], "missevan_generated_replacements": {},
            }),
            patch.object(append_missevan_ids, "merge_and_upload_info_file") as merge,
            patch("builtins.print"),
        ):
            calls.attach_mock(publish, "publish")
            calls.attach_mock(merge, "merge")
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100", "101"]), 0)

        self.assertEqual(calls.mock_calls, [
            call.publish("missevan", candidates, upstash=append_missevan_ids.upstash_request,
                         sync_local_watchcount=False, sync_local_info=False),
            call.merge(append_missevan_ids.MISSEVAN_INFO_KEY, append_missevan_ids.MISSEVAN_INFO_PATH,
                       ["100", "101"]),
        ])

    def test_append_manbo_rejects_non_numeric_id_before_any_remote_work(self) -> None:
        with (
            patch.object(append_manbo_ids, "load_env_file") as load_env,
            patch.object(append_manbo_ids, "download_info_file") as download,
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids") as upsert,
            patch("builtins.print"),
        ):
            result = append_manbo_ids.main(["append_manbo_ids.py", "drama-1"])

        self.assertEqual(result, 1)
        load_env.assert_not_called()
        download.assert_not_called()
        upsert.assert_not_called()

    def test_append_missevan_downloads_info_and_current_cv_map(self) -> None:
        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file") as download,
            patch.object(append_missevan_ids, "download_support_files") as download_support,
            patch.object(append_missevan_ids, "seed_generated_missevan_cvid_registry", return_value=0),
            patch.object(append_missevan_ids, "load_generated_missevan_cvid_replacements", return_value={}),
            patch.object(
                append_missevan_ids,
                "upsert_missevan_drama_ids",
                return_value={"processed": 1, "request_count": 0, "last_backoff_seconds": 0},
            ),
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(
                append_missevan_ids,
                "update_combined_cvid_map",
                return_value={
                    "updated": 0,
                    "created": 0,
                    "unchanged": 0,
                    "ambiguous_count": 0,
                    "ambiguous_samples": [],
                    "missevan_generated_replacements": {},
                },
            ),
            patch.object(append_missevan_ids, "merge_and_upload_info_file"),
            patch("builtins.print"),
        ):
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100"]), 0)

        download.assert_called_once_with(append_missevan_ids.MISSEVAN_INFO_KEY, append_missevan_ids.MISSEVAN_INFO_PATH)
        download_support.assert_called_once_with()

    def test_append_manbo_downloads_manbo_info_and_current_cv_map(self) -> None:
        with (
            patch.object(append_manbo_ids, "load_env_file"),
            patch.object(append_manbo_ids, "download_info_file") as download,
            patch.object(append_manbo_ids, "download_support_files") as download_support,
            patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry", return_value=0),
            patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements", return_value={}),
            patch.object(append_manbo_ids, "UpstashGeneratedManboCvIdAllocator"),
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={"processed": 1}),
            patch.object(append_manbo_ids, "load_json", side_effect=lambda path, _default:
                         {"records": []} if path == append_manbo_ids.MANBO_INFO_PATH else {}),
            patch.object(
                append_manbo_ids,
                "update_combined_cvid_map",
                return_value={"updated": 0, "created": 0, "unchanged": 0, "ambiguous_count": 0, "ambiguous_samples": []},
            ),
            patch.object(append_manbo_ids, "merge_and_upload_info_file"),
            patch("builtins.print"),
        ):
            self.assertEqual(append_manbo_ids.main(["append_manbo_ids.py", "200"]), 0)

        download.assert_called_once_with(append_manbo_ids.MANBO_INFO_KEY, append_manbo_ids.MANBO_INFO_PATH)
        download_support.assert_called_once_with()

    def test_append_missevan_continues_when_avatar_lookup_hits_418(self) -> None:
        def fake_update(*args, avatar_lookup=None, **kwargs):
            self.assertIsNotNone(avatar_lookup)
            self.assertEqual(avatar_lookup("猫耳", 11), "")
            return {
                "updated": 0,
                "created": 1,
                "unchanged": 0,
                "ambiguous_count": 0,
                "ambiguous_samples": [],
                "missevan_generated_replacements": {},
            }

        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file"),
            patch.object(append_missevan_ids, "download_support_files"),
            patch.object(append_missevan_ids, "seed_generated_missevan_cvid_registry", return_value=0),
            patch.object(append_missevan_ids, "load_generated_missevan_cvid_replacements", return_value={}),
            patch.object(
                append_missevan_ids,
                "upsert_missevan_drama_ids",
                return_value={"processed": 1, "request_count": 0, "last_backoff_seconds": 0},
            ),
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(append_missevan_ids, "update_combined_cvid_map", side_effect=fake_update),
            patch.object(append_missevan_ids, "merge_and_upload_info_file") as merge,
            patch.object(append_missevan_ids, "CvAvatarLookup", return_value=Mock(side_effect=RuntimeError("HTTP_418"))),
            patch("builtins.print"),
        ):
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100"]), 0)

        merge.assert_called_once_with(append_missevan_ids.MISSEVAN_INFO_KEY, append_missevan_ids.MISSEVAN_INFO_PATH, ["100"])

    def test_append_missevan_uploads_all_dramas_affected_by_generated_id_upgrade(self) -> None:
        map_stats = {
            "updated": 1,
            "created": 0,
            "unchanged": 0,
            "ambiguous_count": 0,
            "ambiguous_samples": [],
            "missevan_generated_replacements": {331111: 1234},
        }
        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file"),
            patch.object(append_missevan_ids, "download_support_files"),
            patch.object(append_missevan_ids, "seed_generated_missevan_cvid_registry", return_value=0),
            patch.object(append_missevan_ids, "load_generated_missevan_cvid_replacements", return_value={}),
            patch.object(
                append_missevan_ids,
                "upsert_missevan_drama_ids",
                return_value={"processed": 1, "request_count": 0, "last_backoff_seconds": 0},
            ),
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(append_missevan_ids, "update_combined_cvid_map", return_value=map_stats),
            patch.object(append_missevan_ids, "replace_missevan_main_cv_ids", return_value={"101"}) as migrate,
            patch.object(append_missevan_ids, "save_missevan_store"),
            patch.object(append_missevan_ids, "merge_and_upload_info_file") as merge,
            patch("builtins.print"),
        ):
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100"]), 0)

        migrate.assert_called_once_with({}, {331111: 1234})
        merge.assert_called_once_with(
            append_missevan_ids.MISSEVAN_INFO_KEY,
            append_missevan_ids.MISSEVAN_INFO_PATH,
            ["100", "101"],
        )

    def test_append_missevan_retries_persisted_generated_id_migration(self) -> None:
        map_stats = {
            "updated": 0,
            "created": 0,
            "unchanged": 0,
            "ambiguous_count": 0,
            "ambiguous_samples": [],
            "missevan_generated_replacements": {331111: 1234},
        }
        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file"),
            patch.object(append_missevan_ids, "download_support_files"),
            patch.object(append_missevan_ids, "seed_generated_missevan_cvid_registry", return_value=0),
            patch.object(
                append_missevan_ids,
                "load_generated_missevan_cvid_replacements",
                return_value={331111: 1234},
            ),
            patch.object(
                append_missevan_ids,
                "upsert_missevan_drama_ids",
                return_value={"processed": 1, "request_count": 0, "last_backoff_seconds": 0},
            ),
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(append_missevan_ids, "update_combined_cvid_map", return_value=map_stats),
            patch.object(append_missevan_ids, "replace_missevan_main_cv_ids", return_value={"101"}) as migrate,
            patch.object(append_missevan_ids, "save_missevan_store"),
            patch.object(append_missevan_ids, "merge_and_upload_info_file") as merge,
            patch("builtins.print"),
        ):
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100"]), 0)

        migrate.assert_called_once_with({}, {331111: 1234})
        merge.assert_called_once_with(
            append_missevan_ids.MISSEVAN_INFO_KEY,
            append_missevan_ids.MISSEVAN_INFO_PATH,
            ["100", "101"],
        )


if __name__ == "__main__":
    unittest.main()
