import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class NameOnlyCvAggregationTests(unittest.TestCase):
    def test_name_only_cv_is_included_in_sqlite_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "MISSEVAN_INFO_PATH": root / "missevan.json",
                "MANBO_INFO_PATH": root / "manbo.json",
                "MISSEVAN_COUNTS_PATH": root / "missevan-counts.json",
                "MANBO_COUNTS_PATH": root / "manbo-counts.json",
                "COMBINED_CVID_MAP_PATH": root / "map.json",
            }
            payloads = {
                "MISSEVAN_INFO_PATH": {
                    "94602": {
                        "dramaId": 94602,
                        "title": "测试剧",
                        "type": 4,
                        "catalog": 89,
                        "needpay": True,
                        "fallbackCvNames": ["林风"],
                        "fallbackCvRoles": {"林风": "季南溪"},
                    }
                },
                "MANBO_INFO_PATH": {"records": []},
                "MISSEVAN_COUNTS_PATH": {"_meta": {}, "counts": {"94602": {"view_count": 100}}},
                "MANBO_COUNTS_PATH": {"_meta": {}, "counts": {}},
                "COMBINED_CVID_MAP_PATH": {"林风": {"displayName": "林风", "missevanCvId": None}},
            }
            for name, path in paths.items():
                path.write_text(json.dumps(payloads[name], ensure_ascii=False), encoding="utf-8")

            with (
                patch.object(rebuild, "MISSEVAN_INFO_PATH", paths["MISSEVAN_INFO_PATH"]),
                patch.object(rebuild, "MANBO_INFO_PATH", paths["MANBO_INFO_PATH"]),
                patch.object(rebuild, "MISSEVAN_COUNTS_PATH", paths["MISSEVAN_COUNTS_PATH"]),
                patch.object(rebuild, "MANBO_COUNTS_PATH", paths["MANBO_COUNTS_PATH"]),
                patch.object(rebuild, "COMBINED_CVID_MAP_PATH", paths["COMBINED_CVID_MAP_PATH"]),
            ):
                rows = rebuild.build_rows()

        self.assertEqual(rows[0]["cv_name"], "林风")
        self.assertEqual(rows[0]["role_names"], "季南溪")

if __name__ == "__main__":
    unittest.main()
