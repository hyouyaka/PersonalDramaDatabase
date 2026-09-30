import json
import unittest
from datetime import date, timedelta

from plan_known_stale_trend_repair import build_known_stale_repair_candidate


class KnownStaleTrendRepairPlanTests(unittest.TestCase):
    def test_hash_export_candidate_nulls_only_exact_confirmed_values_and_dates(self) -> None:
        first_samples = {
            (date(2026, 8, 17) + timedelta(days=offset)).isoformat(): {
                "metrics": {"view_count": 855745, "danmaku_uid_count": 185, "pay_count": 2659}
            }
            for offset in range(40)
        }
        second_samples = {
            f"2026-09-{day:02d}": {"metrics": {"view_count": 67348, "danmaku_uid_count": 107, "pay_count": 0}}
            for day in range(22, 26)
        }
        payload = {
            "__meta__": json.dumps({"version": 2, "dates": ["2026-09-29"]}),
            "2149856931232088128": json.dumps({"id": "2149856931232088128", "samples": first_samples}),
            "2230801998016413748": json.dumps({"id": "2230801998016413748", "samples": second_samples}),
        }

        candidate, report = build_known_stale_repair_candidate(payload)

        self.assertEqual(len(report["changed"]), 44)
        self.assertEqual(report["missing"], [])
        first = json.loads(candidate["2149856931232088128"])
        second = json.loads(candidate["2230801998016413748"])
        self.assertIsNone(first["samples"]["2026-08-17"]["metrics"]["view_count"])
        self.assertIsNone(first["samples"]["2026-09-15"]["metrics"]["view_count"])
        self.assertIsNone(first["samples"]["2026-09-15"]["metrics"]["danmaku_uid_count"])
        self.assertIsNone(first["samples"]["2026-09-15"]["metrics"]["pay_count"])
        self.assertIsNone(first["samples"]["2026-09-25"]["metrics"]["view_count"])
        self.assertIsNone(second["samples"]["2026-09-22"]["metrics"]["view_count"])
        self.assertEqual(payload["2149856931232088128"], json.dumps({"id": "2149856931232088128", "samples": first_samples}))

    def test_value_mismatch_is_reported_and_left_unchanged(self) -> None:
        payload = {
            "dramas": {
                "2149856931232088128": {
                    "samples": {
                        "2026-09-15": {"metrics": {"view_count": 855745}},
                        "2026-09-16": {"metrics": {"view_count": 123, "danmaku_uid_count": 185, "pay_count": 2659}},
                    }
                }
            }
        }

        candidate, report = build_known_stale_repair_candidate(payload)

        self.assertEqual(report["changed"], [])
        self.assertIn({"drama_id": "2149856931232088128", "date": "2026-09-15"}, report["missing"])
        self.assertEqual(report["value_mismatch"], [{
            "drama_id": "2149856931232088128",
            "date": "2026-09-16",
            "expected": {"view_count": 855745, "danmaku_uid_count": 185, "pay_count": 2659},
            "actual": {"view_count": 123, "danmaku_uid_count": 185, "pay_count": 2659},
        }])
        self.assertEqual(candidate["dramas"]["2149856931232088128"]["samples"]["2026-09-16"]["metrics"]["view_count"], 123)


if __name__ == "__main__":
    unittest.main()
