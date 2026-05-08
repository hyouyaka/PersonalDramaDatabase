import unittest

import rebuild_sqlite_from_libraries as rebuild


class BaseSeriesTitleTests(unittest.TestCase):
    def test_fanwai_titles_merge_to_main_series(self) -> None:
        cases = {
            "撒野 番外": "撒野",
            "迪奥先生 独家番外": "迪奥先生",
            "奇洛李维斯回信 番外集": "奇洛李维斯回信",
            "奇洛李维斯回信 番外篇": "奇洛李维斯回信",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(rebuild.base_series_title(raw), expected)

    def test_existing_season_rules_still_work(self) -> None:
        self.assertEqual(rebuild.base_series_title("撒野 第四季"), "撒野")
        self.assertEqual(rebuild.base_series_title("某剧 第二季 番外篇"), "某剧")
        self.assertEqual(rebuild.base_series_title("标题（CV：甲乙）"), "标题")

if __name__ == "__main__":
    unittest.main()
