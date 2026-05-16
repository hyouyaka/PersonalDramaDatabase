import unittest

import refresh_platform_metadata


class ManboCvFallbackTests(unittest.TestCase):
    def test_build_manbo_record_falls_back_to_nickname_when_map_missing(self) -> None:
        payload = {
            "data": {
                "title": "测试剧",
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
