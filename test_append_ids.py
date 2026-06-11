import unittest
from unittest.mock import Mock, call, patch

import append_manbo_ids
import append_missevan_ids


class AppendInfoBackupTests(unittest.TestCase):
    def test_append_missevan_downloads_only_missevan_info(self) -> None:
        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file") as download,
            patch.object(
                append_missevan_ids,
                "upsert_missevan_drama_ids",
                return_value={"processed": 1, "request_count": 0, "last_backoff_seconds": 0},
            ),
            patch.object(append_missevan_ids, "load_json", return_value={}),
            patch.object(
                append_missevan_ids,
                "update_combined_cvid_map",
                return_value={"updated": 0, "created": 0, "unchanged": 0, "ambiguous_count": 0, "ambiguous_samples": []},
            ),
            patch.object(append_missevan_ids, "merge_and_upload_info_file"),
            patch("builtins.print"),
        ):
            self.assertEqual(append_missevan_ids.main(["append_missevan_ids.py", "100"]), 0)

        download.assert_called_once_with(append_missevan_ids.MISSEVAN_INFO_KEY, append_missevan_ids.MISSEVAN_INFO_PATH)

    def test_append_manbo_downloads_only_manbo_info(self) -> None:
        with (
            patch.object(append_manbo_ids, "load_env_file"),
            patch.object(append_manbo_ids, "download_info_file") as download,
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={"processed": 1}),
            patch.object(append_manbo_ids, "load_json", return_value={"records": []}),
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

    def test_append_missevan_continues_when_avatar_lookup_hits_418(self) -> None:
        def fake_update(*args, avatar_lookup=None, **kwargs):
            self.assertIsNotNone(avatar_lookup)
            self.assertEqual(avatar_lookup("猫耳", 11), "")
            return {"updated": 0, "created": 1, "unchanged": 0, "ambiguous_count": 0, "ambiguous_samples": []}

        with (
            patch.object(append_missevan_ids, "load_env_file"),
            patch.object(append_missevan_ids, "download_info_file"),
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


if __name__ == "__main__":
    unittest.main()
