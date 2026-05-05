import unittest
from unittest.mock import patch

import append_manbo_ids as append_manbo
import append_missevan_ids as append_missevan


class AppendIdsRemoteSyncTests(unittest.TestCase):
    def test_append_missevan_downloads_remote_then_uploads_only_requested_ids(self) -> None:
        calls = []

        def fake_download(key, path):
            calls.append(("download", key, path))

        def fake_upsert(drama_ids, *, force):
            calls.append(("upsert", list(drama_ids), force))
            return {"processed": 1, "request_count": 2, "last_backoff_seconds": 0, "count_entries_updated": 1}

        def fake_upload(key, path, drama_ids):
            calls.append(("upload", key, path, list(drama_ids)))

        with (
            patch.object(append_missevan, "load_env_file") as load_env,
            patch.object(append_missevan, "download_info_file", side_effect=fake_download),
            patch.object(append_missevan, "upsert_missevan_drama_ids", side_effect=fake_upsert),
            patch.object(append_missevan, "merge_and_upload_info_file", side_effect=fake_upload),
            patch.object(
                append_missevan,
                "update_combined_cvid_map",
                return_value={"updated": 0, "created": 0, "unchanged": 1, "ambiguous_count": 0, "ambiguous_samples": []},
            ),
        ):
            self.assertEqual(append_missevan.main(["append_missevan_ids.py", "93169"]), 0)

        load_env.assert_called_once_with(append_missevan.ROOT / ".env")
        self.assertEqual(
            calls,
            [
                ("download", append_missevan.MISSEVAN_INFO_KEY, append_missevan.MISSEVAN_INFO_PATH),
                ("upsert", ["93169"], True),
                ("upload", append_missevan.MISSEVAN_INFO_KEY, append_missevan.MISSEVAN_INFO_PATH, ["93169"]),
            ],
        )

    def test_append_manbo_downloads_remote_then_uploads_only_requested_ids(self) -> None:
        calls = []

        def fake_download(key, path):
            calls.append(("download", key, path))

        def fake_upsert(drama_ids, *, force):
            calls.append(("upsert", list(drama_ids), force))
            return {"processed": 1}

        def fake_upload(key, path, drama_ids):
            calls.append(("upload", key, path, list(drama_ids)))

        with (
            patch.object(append_manbo, "load_env_file") as load_env,
            patch.object(append_manbo, "download_info_file", side_effect=fake_download),
            patch.object(append_manbo, "upsert_manbo_drama_ids", side_effect=fake_upsert),
            patch.object(append_manbo, "merge_and_upload_info_file", side_effect=fake_upload),
            patch.object(
                append_manbo,
                "load_json",
                return_value={"records": [{"dramaId": "201", "mainCvNames": ["CV A"]}]},
            ),
            patch.object(
                append_manbo,
                "update_combined_cvid_map",
                return_value={"updated": 0, "created": 0, "unchanged": 1, "ambiguous_count": 0, "ambiguous_samples": []},
            ),
        ):
            self.assertEqual(append_manbo.main(["append_manbo_ids.py", "201"]), 0)

        load_env.assert_called_once_with(append_manbo.ROOT / ".env")
        self.assertEqual(
            calls,
            [
                ("download", append_manbo.MANBO_INFO_KEY, append_manbo.MANBO_INFO_PATH),
                ("upsert", ["201"], True),
                ("upload", append_manbo.MANBO_INFO_KEY, append_manbo.MANBO_INFO_PATH, ["201"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
