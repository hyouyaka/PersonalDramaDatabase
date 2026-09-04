import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import build_weekly_growth_ranks as growth


def history_raw(entries: dict[str, dict]) -> list[str]:
    result: list[str] = []
    for drama_id, entry in entries.items():
        result.extend([drama_id, json.dumps(entry, ensure_ascii=False)])
    return result


def entry(name: str, *points: tuple[str, int]) -> dict:
    return {"name": name, "points": [list(point) for point in points]}


class FakeReadUpstash:
    def __init__(self, *, missevan_history: dict, manbo_history: dict, missevan_info: dict, manbo_info: dict):
        self.values = {
            ("HGETALL", "missevan:watchcount:history"): history_raw(missevan_history),
            ("HGETALL", "manbo:watchcount:history"): history_raw(manbo_history),
            ("GET", "missevan:info:v2"): json.dumps(missevan_info, ensure_ascii=False),
            ("GET", "manbo:info:v2"): json.dumps(manbo_info, ensure_ascii=False),
        }
        self.commands: list[list[object]] = []

    def __call__(self, command: list[object]):
        self.commands.append(command)
        return self.values[tuple(command)]


def base_fake() -> FakeReadUpstash:
    common_dates = (("2026-07-31", 100), ("2026-08-21", 200), ("2026-08-28", 300))
    missevan_history = {
        "1": entry("history title", *common_dates),
        "2": entry("missing time", ("2026-08-28", 50)),
        "3": entry("current month", ("2026-08-28", 60)),
        "4": entry("previous month", ("2026-08-28", 70)),
        "5": entry("old", ("2026-08-28", 500)),
        "6": entry("invalid", ("2026-08-28", 600)),
        "7": entry("different strategy", ("2026-07-31", 200), ("2026-08-21", 500), ("2026-08-28", 400)),
        "8": entry("zero", ("2026-07-31", 10), ("2026-08-21", 20), ("2026-08-28", 20)),
    }
    missevan_info = {
        drama_id: {
            "dramaId": drama_id,
            "seriesTitle": f"系列 {drama_id}",
            "title": f"标题 {drama_id}",
            "catalog": 89,
            "needpay": True,
            "is_member": True,
            "createTime": create_time,
            "maincvs": [101],
            "cvnames": {"101": "猫耳主役"},
        }
        for drama_id, create_time in {
            "1": "2025.01",
            "2": None,
            "3": "2026.08",
            "4": "2026.07",
            "5": "2026.06",
            "6": "not-a-date",
            "7": "2025.01",
            "8": "2025.01",
        }.items()
    }
    for drama_id in range(1000, 1092):
        missevan_info[str(drama_id)] = {
            "dramaId": str(drama_id),
            "title": f"无历史猫耳 {drama_id}",
        }
    manbo_history = {
        "900000000000000001": entry(
            "漫播历史标题",
            ("2026-07-31", 10),
            ("2026-08-21", 30),
            ("2026-08-28", 80),
        )
    }
    manbo_info = {
        "records": [
            {
                "dramaId": "900000000000000001",
                "name": "漫播标题",
                "catalogName": "有声书",
                "needpay": False,
                "createTime": "2024.12",
                "mainCvNames": ["漫播主役"],
                "mainCvNicknames": [],
            },
            *[
                {"dramaId": str(drama_id), "name": f"无历史漫播 {drama_id}"}
                for drama_id in range(2000, 2049)
            ],
        ]
    }
    return FakeReadUpstash(
        missevan_history=missevan_history,
        manbo_history=manbo_history,
        missevan_info=missevan_info,
        manbo_info=manbo_info,
    )


class WeeklyGrowthRankTests(unittest.TestCase):
    def test_builds_four_rankings_only_from_history_and_info(self) -> None:
        fake = base_fake()

        payload = growth.build_payload(
            upstash=fake,
            expected_end_date="2026-08-28",
            generated_at="2026-08-28T12:00:00+00:00",
        )

        self.assertEqual(
            fake.commands,
            [
                ["HGETALL", "missevan:watchcount:history"],
                ["HGETALL", "manbo:watchcount:history"],
                ["GET", "missevan:info:v2"],
                ["GET", "manbo:info:v2"],
            ],
        )
        self.assertFalse(any("watchcount:latest" in str(command) for command in fake.commands))
        self.assertEqual(payload["date"], "2026-08-28")
        self.assertEqual(payload["missevanDramaCount"], 100)
        self.assertEqual(payload["manboDramaCount"], 50)
        self.assertEqual(payload["statisticsPeriods"]["weekly"]["missevan"]["startDate"], "2026-08-21")
        self.assertEqual(payload["statisticsPeriods"]["fourWeek"]["missevan"]["startDate"], "2026-07-31")

        weekly = payload["rankings"]["weekly"]["missevan"]
        four_week = payload["rankings"]["fourWeek"]["missevan"]
        self.assertEqual([item["dramaId"] for item in weekly], ["1", "4", "3", "2"])
        self.assertEqual([item["dramaId"] for item in four_week], ["7", "1", "4", "3", "2", "8"])
        self.assertEqual(next(item for item in weekly if item["dramaId"] == "4")["newReason"], "createdInEndMonth")
        self.assertEqual(next(item for item in weekly if item["dramaId"] == "2")["newReason"], "missingCreateTime")
        self.assertFalse(next(item for item in weekly if item["dramaId"] == "1")["isNew"])
        self.assertNotIn("5", {item["dramaId"] for item in weekly})
        self.assertNotIn("6", {item["dramaId"] for item in weekly})
        self.assertNotIn("7", {item["dramaId"] for item in weekly})
        first_normal = next(item for item in weekly if item["dramaId"] == "1")
        self.assertEqual(first_normal["title"], "系列 1")
        self.assertEqual(first_normal["mainCvs"], ["猫耳主役"])
        self.assertEqual(first_normal["catalogName"], "广播剧")
        self.assertEqual(first_normal["payStatus"], "会员")
        manbo = payload["rankings"]["weekly"]["manbo"][0]
        self.assertEqual(manbo["dramaId"], "900000000000000001")
        self.assertEqual(manbo["catalogName"], "有声剧")
        self.assertEqual(manbo["payStatus"], "免费")
        self.assertEqual(manbo["mainCvs"], ["漫播主役"])

    def test_expected_date_rejects_stale_or_mismatched_platform(self) -> None:
        fake = base_fake()
        with self.assertRaisesRegex(RuntimeError, "expected 2026-08-29"):
            growth.build_payload(upstash=fake, expected_end_date="2026-08-29")

        fake = base_fake()
        fake.values[("HGETALL", "manbo:watchcount:history")] = history_raw(
            {"9": entry("old", ("2026-08-21", 1))}
        )
        with self.assertRaisesRegex(RuntimeError, "manbo=2026-08-21"):
            growth.build_payload(upstash=fake, expected_end_date="2026-08-28")

    def test_truncated_info_v2_is_rejected_before_rank_generation(self) -> None:
        fake = base_fake()
        fake.values[("GET", "missevan:info:v2")] = "{}"

        with self.assertRaisesRegex(RuntimeError, "only 0 records found"):
            growth.build_payload(upstash=fake)

    def test_baseline_tolerance_prefers_exact_then_earlier(self) -> None:
        available = {"2026-08-20", "2026-08-21", "2026-08-22"}
        self.assertEqual(
            growth.select_baseline_date(date(2026, 8, 28), 7, available).isoformat(),
            "2026-08-21",
        )
        available.remove("2026-08-21")
        self.assertEqual(
            growth.select_baseline_date(date(2026, 8, 28), 7, available).isoformat(),
            "2026-08-20",
        )

    def test_invalid_non_empty_create_time_is_not_new(self) -> None:
        end = date(2026, 8, 28)
        self.assertIsNone(growth.missing_baseline_new_reason("2026.08.99", end))
        self.assertIsNone(growth.missing_baseline_new_reason("2026.08-extra", end))
        self.assertEqual(growth.missing_baseline_new_reason("", end), "missingCreateTime")

    def test_top_50_and_numeric_id_tie_break(self) -> None:
        history = {
            str(drama_id): entry("x", ("2026-08-21", 0), ("2026-08-28", 100))
            for drama_id in range(1, 53)
        }
        info = {
            str(drama_id): {"dramaId": str(drama_id), "name": str(drama_id), "createTime": "2020.01"}
            for drama_id in range(1, 53)
        }
        result = growth.build_period_ranking(
            "manbo",
            history,
            info,
            end_date=date(2026, 8, 28),
            period_days=7,
        )
        self.assertEqual(len(result), 50)
        self.assertEqual([item["dramaId"] for item in result[:3]], ["1", "2", "3"])
        self.assertEqual(result[-1]["rank"], 50)

    def test_each_drama_selects_its_own_tolerated_baseline(self) -> None:
        history = {
            "1": entry("exact", ("2026-08-21", 100), ("2026-08-28", 150)),
            "2": entry("earlier", ("2026-08-20", 200), ("2026-08-28", 280)),
            "3": entry("later", ("2026-08-22", 300), ("2026-08-28", 390)),
        }
        info = {
            drama_id: {"dramaId": drama_id, "name": drama_id, "createTime": "2020.01"}
            for drama_id in history
        }

        result = growth.build_period_ranking(
            "manbo",
            history,
            info,
            end_date=date(2026, 8, 28),
            period_days=7,
        )

        self.assertEqual([item["dramaId"] for item in result], ["3", "2", "1"])
        self.assertEqual([item["viewCountIncrease"] for item in result], [90, 80, 50])
        self.assertTrue(all(not item["isNew"] for item in result))

    def test_main_no_upload_writes_local_only(self) -> None:
        fake = base_fake()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "growth.json"
            with patch.object(growth, "publish_rank_string") as publish:
                result = growth.main(["--no-upload"], upstash=fake, output_path=output)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["kind"], "weeklyViewGrowth")
            publish.assert_not_called()

    def test_main_publishes_latest_key_with_own_resource_scope(self) -> None:
        fake = base_fake()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "growth.json"
            with patch.object(growth, "publish_rank_string") as publish:
                growth.main([], upstash=fake, output_path=output)
            publish.assert_called_once()
            args, kwargs = publish.call_args
            self.assertEqual(args[0], "ranks:weekly-growth:latest")
            self.assertEqual(kwargs["scope"], "watchcountGrowth")
            self.assertIs(kwargs["upstash"], fake)


if __name__ == "__main__":
    unittest.main()
