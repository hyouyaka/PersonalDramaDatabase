import unittest
from unittest.mock import Mock, patch

import refresh_platform_metadata


class ManboCvFallbackTests(unittest.TestCase):
    def test_build_manbo_record_falls_back_to_nickname_when_map_missing(self) -> None:
        payload = {
                "data": {
                    "title": "测试剧",
                    "cover": "https://cover.test/manbo.jpg",
                    "category": 1,
                    "categoryLabels": [{"name": "纯爱"}],
                "cvRespList": [
                    {
                        "dramaRoleType": 2,
                        "platUid": 1001,
                        "cvResp": {"id": 1001, "nickname": "映射昵称"},
                        "cvNickname": "接口昵称甲",
                        "role": "饰:甲",
                    },
                    {
                        "dramaRoleType": 2,
                        "platUid": 1002,
                        "cvResp": {"id": 1002, "nickname": "接口昵称乙"},
                        "cvNickname": "接口昵称乙",
                        "role": "饰:乙",
                    },
                ],
                "setRespList": [],
            }
        }

        record = refresh_platform_metadata.build_manbo_record(
            {"dramaId": "drama-1"},
            payload,
            {1001: "规范名甲"},
        )

        self.assertEqual(record["mainCvNames"], ["规范名甲", "接口昵称乙"])


class MissevanCoverTests(unittest.TestCase):
    def test_build_missevan_base_node_keeps_cover(self) -> None:
        node, _entries = refresh_platform_metadata.build_missevan_base_node(
            {
                "drama": {
                    "id": 93605,
                    "name": "猫耳测试剧",
                    "cover": "https://cover.test/missevan.jpg",
                    "catalog": 89,
                    "pay_type": 1,
                    "price": 1,
                },
                "cvs": [],
                "episodes": {"episode": []},
            },
            4,
        )

        self.assertEqual(node["cover"], "https://cover.test/missevan.jpg")


class MissevanCvMapEntryTests(unittest.TestCase):
    def test_upsert_missevan_cv_map_entry_initializes_avatar(self) -> None:
        combined_map = {}

        refresh_platform_metadata.upsert_missevan_cv_map_entry(combined_map, "CV A", 11)

        self.assertIn("avatar", combined_map["CV A"])
        self.assertEqual(combined_map["CV A"]["avatar"], "")

    def test_intro_search_miss_creates_name_only_cv_entry(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map") as save_map:
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"maincvs": []},
                "94602",
                [{"role_name": "季南溪", "display_name": "林风"}],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["fallbackCvNames"], ["林风"])
        self.assertEqual(updated["fallbackCvRoles"], {"林风": "季南溪"})
        self.assertIsNone(combined_map["林风"]["missevanCvId"])
        self.assertEqual(combined_map["林风"]["notes"], "猫耳搜索未命中，以显示名称识别")
        save_map.assert_called_once_with(combined_map)

    def test_intro_fallback_merges_numeric_and_name_only_main_cv(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [11],
                    "cvnames": {"11": "已知CV"},
                    "cvroles": {"11": "角色甲"},
                },
                "drama-1",
                [
                    {"role_name": "角色甲", "display_name": "已知CV"},
                    {"role_name": "角色乙", "display_name": "未知CV"},
                ],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["maincvs"], [11])
        self.assertEqual(updated["cvroles"], {"11": "角色甲"})
        self.assertEqual(updated["fallbackCvNames"], ["未知CV"])
        self.assertEqual(updated["fallbackCvRoles"], {"未知CV": "角色乙"})
        self.assertNotIn("已知CV", combined_map)
        self.assertIn("未知CV", combined_map)

    def test_partial_structured_main_cvs_still_trigger_intro_fallback(self) -> None:
        self.assertTrue(
            refresh_platform_metadata.should_try_missevan_intro_cv_fallback(
                {"maincvs": [11], "cvnames": {"11": "已知CV"}},
                {"cvs": [{"cv_info": {"id": 11}}]},
                [{"cvs": [{"cv_info": {"id": 11}}]}],
                ["preview-1"],
                required_main_cvs=2,
            )
        )

    def test_intro_fallback_keeps_numeric_cv_when_multiple_names_are_unresolved(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [11],
                    "cvnames": {"11": "已知CV"},
                    "cvroles": {"11": "角色甲"},
                },
                "drama-1",
                [
                    {"role_name": "角色乙", "display_name": "未知CV乙"},
                    {"role_name": "角色丙", "display_name": "未知CV丙"},
                ],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["maincvs"], [11])
        self.assertEqual(updated["fallbackCvNames"], ["未知CV乙"])
        self.assertNotIn("未知CV丙", updated["fallbackCvRoles"])


class MissevanIntroCvCandidateTests(unittest.TestCase):
    def test_intro_fallback_sound_ids_include_regular_sound_without_preview(self) -> None:
        sound_ids = refresh_platform_metadata.missevan_intro_fallback_sound_ids(
            [],
            "12712741",
            "12712741",
        )

        self.assertEqual(sound_ids, ["12712741"])

    def test_intro_fallback_sound_ids_preserve_preview_priority_and_dedupe(self) -> None:
        sound_ids = refresh_platform_metadata.missevan_intro_fallback_sound_ids(
            ["preview-1", "preview-2"],
            "preview-1",
            "episode-1",
        )

        self.assertEqual(sound_ids, ["preview-1", "preview-2", "episode-1"])

    def test_extracts_candidates_from_plain_cv_section_titles(self) -> None:
        for title in ("配音组", "配音：", "CAST", "CV"):
            with self.subTest(title=title):
                intro = f"""
                <p>{title}</p>
                <p>角色甲：甲声优@alias</p>
                <p>角色乙：乙声优</p>
                """

                candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

                self.assertEqual(
                    candidates,
                    [
                        {"role_name": "角色甲", "display_name": "甲声优"},
                        {"role_name": "角色乙", "display_name": "乙声优"},
                    ],
                )

    def test_extracts_candidates_from_decorated_cv_section_title(self) -> None:
        intro = """
        <p>ﾟ ˖◛⁺配音组☁ ﾟ</p>
        <p>张沉：孙睿扬@Sun睿扬</p>
        <p>程声：云惟一@-云惟一</p>
        <p>参与配音：阿步、姜贺</p>
        <p>ﾟ💦 制作组 ﾟ ˖◛⁺</p>
        <p>配音导演：张馨月</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "张沉", "display_name": "孙睿扬"},
                {"role_name": "程声", "display_name": "云惟一"},
            ],
        )

    def test_filters_staff_lines_inside_cv_section(self) -> None:
        intro = """
        <p>配音组</p>
        <p>谢岫：吴晛@吴晛Hsien</p>
        <p>配音导演：张馨月</p>
        <p>录音棚：九紫声优团</p>
        <p>霍无归：云惟一@-云惟一</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "谢岫", "display_name": "吴晛"},
                {"role_name": "霍无归", "display_name": "云惟一"},
            ],
        )

    def test_does_not_extract_role_lines_without_cv_section_title(self) -> None:
        intro = """
        <p>普通简介</p>
        <p>张沉：孙睿扬@Sun睿扬</p>
        <p>配音导演：张馨月</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
