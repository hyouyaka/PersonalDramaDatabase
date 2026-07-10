import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cvid_map_tools


class RemoteCombinedMapTests(unittest.TestCase):
    def test_missing_remote_and_missing_local_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_backup = Path(tmp) / "missevan&manbo-cvid-map.json"
            upstash = Mock(return_value=None)

            with (
                patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", missing_backup),
                self.assertRaises(RuntimeError),
            ):
                cvid_map_tools.load_remote_combined_map(upstash=upstash)

        upstash.assert_called_once_with(["GET", "cvid-map:v1"])


class AvatarHelperTests(unittest.TestCase):
    def test_normalize_avatar_url_strips_query_and_fragment(self) -> None:
        self.assertEqual(
            cvid_map_tools.normalize_avatar_url("https://img.kilamanbo.com/a.png?t=0#frag"),
            "https://img.kilamanbo.com/a.png",
        )


class UpdateCombinedMapAvatarTests(unittest.TestCase):
    def test_name_only_cv_is_created_idempotently_and_can_gain_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "missevan&manbo-cvid-map.json"
            map_path.write_text("{}", encoding="utf-8")
            name_only_store = {
                "94602": {
                    "dramaId": "94602",
                    "fallbackCvNames": ["林风"],
                    "fallbackCvRoles": {"林风": "季南溪"},
                }
            }
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                first = cvid_map_tools.update_combined_cvid_map(name_only_store, {"records": []})
                second = cvid_map_tools.update_combined_cvid_map(name_only_store, {"records": []})
                upgraded = cvid_map_tools.update_combined_cvid_map(
                    {"94602": {"dramaId": "94602", "maincvs": [777], "cvnames": {"777": "林风"}}},
                    {"records": []},
                )
                saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(upgraded["updated"], 1)
        self.assertEqual(saved["林风"]["missevanCvId"], 777)

    def test_created_missevan_cv_gets_avatar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "missevan&manbo-cvid-map.json"
            map_path.write_text("{}", encoding="utf-8")
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                stats = cvid_map_tools.update_combined_cvid_map(
                    {
                        "100": {
                            "dramaId": "100",
                            "maincvs": [11],
                            "cvnames": {"11": "CV A"},
                        }
                    },
                    {"records": []},
                    missevan_drama_ids={"100"},
                    avatar_lookup=lambda platform, cv_id: f"https://avatar.test/{platform}-{cv_id}.jpg",
                )

            saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(stats["created"], 1)
        self.assertEqual(saved["CV A"]["avatar"], "https://avatar.test/猫耳-11.jpg")

    def test_existing_avatar_is_not_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "missevan&manbo-cvid-map.json"
            map_path.write_text(
                '{"CV A":{"displayName":"CV A","missevanCvId":11,"aliases":[],"avatar":"old"}}',
                encoding="utf-8",
            )
            avatar_lookup = Mock(return_value="new")
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                cvid_map_tools.update_combined_cvid_map(
                    {
                        "100": {
                            "dramaId": "100",
                            "maincvs": [11],
                            "cvnames": {"11": "CV A"},
                        }
                    },
                    {"records": []},
                    missevan_drama_ids={"100"},
                    avatar_lookup=avatar_lookup,
                )

            saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(saved["CV A"]["avatar"], "old")
        avatar_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
