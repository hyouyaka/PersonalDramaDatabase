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


class GeneratedMissevanCvIdTests(unittest.TestCase):
    def test_allocator_skips_map_and_registry_collisions(self) -> None:
        reservations = iter([0, 330002])
        upstash = Mock(side_effect=lambda command: next(reservations))
        randbelow = Mock(side_effect=[0, 0])
        allocator = cvid_map_tools.UpstashGeneratedMissevanCvIdAllocator(upstash=upstash, randbelow=randbelow)

        generated = allocator({"已有": {"cvId": 330000, "missevanCvId": 330000}}, "新CV")

        self.assertEqual(generated, 330002)
        self.assertEqual(
            upstash.call_args_list,
            [
                unittest.mock.call(
                    [
                        "EVAL",
                        cvid_map_tools.GENERATED_MISSEVAN_CVID_RESERVE_SCRIPT,
                        1,
                        cvid_map_tools.GENERATED_MISSEVAN_CVID_REGISTRY_KEY,
                        "name:新cv",
                        "id:330001",
                        "330001",
                        "新cv",
                    ]
                ),
                unittest.mock.call(
                    [
                        "EVAL",
                        cvid_map_tools.GENERATED_MISSEVAN_CVID_RESERVE_SCRIPT,
                        1,
                        cvid_map_tools.GENERATED_MISSEVAN_CVID_REGISTRY_KEY,
                        "name:新cv",
                        "id:330002",
                        "330002",
                        "新cv",
                    ]
                ),
            ],
        )

    def test_seed_registry_uses_only_generated_missevan_ids(self) -> None:
        def fake_upstash(command):
            if command[0] == "SMEMBERS":
                return []
            if command[0] == "EVAL":
                return 2
            if command[0] == "HSETNX":
                return 1
            raise AssertionError(command)

        upstash = Mock(side_effect=fake_upstash)

        seeded = cvid_map_tools.seed_generated_missevan_cvid_registry(
            {
                "生成": {"missevanCvId": 331111},
                "真实": {"missevanCvId": 3946},
                "漫播": {"missevanCvId": None, "manboCvId": 123456789},
            },
            {
                "100": {
                    "dramaId": 100,
                    "maincvs": [332222, 3946],
                    "cvnames": {"332222": "源CV", "3946": "真实CV"},
                }
            },
            upstash=upstash,
        )

        self.assertEqual(seeded, 4)
        self.assertEqual(upstash.call_args_list[0], unittest.mock.call(["SMEMBERS", cvid_map_tools.LEGACY_GENERATED_MISSEVAN_CVID_REGISTRY_KEY]))
        self.assertTrue(all(call.args[0][0] != "HSETNX" for call in upstash.call_args_list[1:]))

    def test_seed_registry_fails_on_atomic_bidirectional_conflict(self) -> None:
        upstash = Mock(side_effect=[[], -1])

        with self.assertRaisesRegex(RuntimeError, "registry conflict"):
            cvid_map_tools.seed_generated_missevan_cvid_registry(
                {"甲": {"missevanCvId": 331111}},
                {},
                upstash=upstash,
            )

        self.assertEqual(upstash.call_args_list[1].args[0][0], "EVAL")

    def test_allocator_reuses_name_mapping_reserved_before_map_save(self) -> None:
        upstash = Mock(return_value=335214)
        allocator = cvid_map_tools.UpstashGeneratedMissevanCvIdAllocator(
            upstash=upstash,
            randbelow=Mock(side_effect=[0, 0]),
        )

        generated = allocator({}, "林风")

        self.assertEqual(generated, 335214)
        self.assertEqual(upstash.call_count, 1)

    def test_load_generated_replacements_ignores_other_registry_fields(self) -> None:
        upstash = Mock(
            return_value=[
                "name:林风",
                "335214",
                "id:335214",
                "林风",
                "upgrade:335214",
                "1234",
            ]
        )

        replacements = cvid_map_tools.load_generated_missevan_cvid_replacements(upstash=upstash)

        self.assertEqual(replacements, {335214: 1234})

    def test_generated_map_id_upgrades_to_real_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "map.json"
            map_path.write_text(
                '{"林风":{"cvId":331111,"missevanCvId":331111,"displayName":"林风","aliases":[],"avatar":""}}',
                encoding="utf-8",
            )
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                stats = cvid_map_tools.update_combined_cvid_map(
                    {"94602": {"dramaId": 94602, "maincvs": [1234], "cvnames": {"1234": "林风"}}},
                    {"records": []},
                )
                saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(saved["林风"]["missevanCvId"], 1234)
        self.assertEqual(saved["林风"]["source"], "observed")
        self.assertIn("331111", saved["林风"]["notes"])
        self.assertEqual(stats["missevan_generated_replacements"], {331111: 1234})

    def test_persisted_upgrade_repairs_map_without_new_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "map.json"
            map_path.write_text(
                '{"林风":{"cvId":331111,"missevanCvId":331111,"displayName":"林风","aliases":[],"avatar":""}}',
                encoding="utf-8",
            )
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                stats = cvid_map_tools.update_combined_cvid_map(
                    {},
                    {"records": []},
                    persistent_generated_replacements={331111: 1234},
                )
                saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(saved["林风"]["missevanCvId"], 1234)
        self.assertEqual(stats["missevan_generated_replacements"], {331111: 1234})


class UpdateCombinedMapAvatarTests(unittest.TestCase):
    def test_generated_observation_marks_existing_name_only_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "map.json"
            map_path.write_text(
                '{"林风":{"cvId":null,"missevanCvId":null,"displayName":"林风","aliases":[],"avatar":"","source":"observed","notes":"旧"}}',
                encoding="utf-8",
            )
            with patch.object(cvid_map_tools, "COMBINED_CVID_MAP_PATH", map_path):
                cvid_map_tools.update_combined_cvid_map(
                    {"94602": {"dramaId": 94602, "maincvs": [335214], "cvnames": {"335214": "林风"}}},
                    {"records": []},
                )
                saved = cvid_map_tools.load_json(map_path, {})

        self.assertEqual(saved["林风"]["source"], "missevan_generated")
        self.assertIn("335214", saved["林风"]["notes"])

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
