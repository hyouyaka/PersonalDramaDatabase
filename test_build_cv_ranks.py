import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_cv_ranks


class BuildCvRanksTests(unittest.TestCase):
    def write_json(self, tmp: str, name: str, payload: dict) -> Path:
        path = Path(tmp) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_builds_platform_rankings_and_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(
                tmp,
                "missevan.json",
                {
                    "100": {
                        "dramaId": 100,
                        "title": "猫耳免费剧",
                        "cover": "m-cover",
                        "needpay": False,
                        "maincvs": [11],
                        "cvnames": {"11": "猫耳名"},
                    },
                    "101": {
                        "dramaId": 101,
                        "title": "无播放量",
                        "cover": "missing-count",
                        "maincvs": [11],
                        "cvnames": {"11": "猫耳名"},
                    },
                },
            )
            manbo_info = self.write_json(
                tmp,
                "manbo.json",
                {
                    "version": 1,
                    "records": [
                        {
                            "dramaId": "200",
                            "name": "漫播剧",
                            "cover": "mb-cover",
                            "needpay": True,
                            "mainCvIds": [22],
                            "mainCvNicknames": ["漫播名"],
                        }
                    ],
                },
            )
            missevan_counts = self.write_json(
                tmp,
                "missevan-counts.json",
                {"_meta": {}, "counts": {"100": {"view_count": 100}, "101": {"view_count": None}}},
            )
            manbo_counts = self.write_json(
                tmp,
                "manbo-counts.json",
                {"_meta": {}, "counts": {"200": {"view_count": 60}}},
            )
            cvid_map = self.write_json(tmp, "map.json", {})
            output = Path(tmp) / "ranks-cv.json"
            remote_map = {
                "Canonical CV": {
                    "displayName": "Canonical CV",
                    "missevanCvId": 11,
                    "manboCvId": 22,
                    "avatar": "https://avatar.test/canonical.jpg",
                }
            }
            upstash = Mock(side_effect=[json.dumps(remote_map, ensure_ascii=False), "OK", "OK"])

            payload = build_cv_ranks.build_and_publish_cv_ranks(
                missevan_info_path=missevan_info,
                manbo_info_path=manbo_info,
                missevan_counts_path=missevan_counts,
                manbo_counts_path=manbo_counts,
                cvid_map_path=cvid_map,
                output_path=output,
                upstash=upstash,
                generated_at="2026-06-10T12:00:00+00:00",
                upload=True,
            )

            self.assertEqual(payload["date"], "2026-06-10")
            self.assertEqual(payload["generated_at"], "2026-06-10T12:00:00+00:00")
            self.assertEqual(payload["source"]["scope"], "all")
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["missevanDramaCount"], 2)
            self.assertEqual(payload["manboDramaCount"], 1)
            self.assertEqual(set(payload["rankings"]), {"missevan", "manbo"})

            missevan_cv = payload["rankings"]["missevan"][0]
            self.assertEqual(missevan_cv["cvName"], "Canonical CV")
            self.assertEqual(missevan_cv["avatar"], "https://avatar.test/canonical.jpg")
            self.assertEqual(missevan_cv["totalViewCount"], 100)
            self.assertEqual(missevan_cv["workCount"], 1)
            self.assertEqual([work["dramaId"] for work in missevan_cv["works"]], ["100"])
            self.assertEqual(missevan_cv["works"][0]["cover"], "m-cover")

            manbo_cv = payload["rankings"]["manbo"][0]
            self.assertEqual(manbo_cv["cvName"], "Canonical CV")
            self.assertEqual(manbo_cv["avatar"], "https://avatar.test/canonical.jpg")
            self.assertEqual(manbo_cv["totalViewCount"], 60)
            self.assertEqual(manbo_cv["workCount"], 1)
            self.assertEqual([work["dramaId"] for work in manbo_cv["works"]], ["200"])
            self.assertEqual(manbo_cv["works"][0]["cover"], "mb-cover")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(upstash.call_args_list[0].args[0], ["GET", "cvid-map:v1"])
            self.assertEqual(upstash.call_args_list[1].args[0][0:2], ["SET", "ranks:cv:2026-06-10"])
            self.assertEqual(upstash.call_args_list[2].args[0][0:2], ["SET", "ranks:cv:latest"])

    def test_no_upload_still_downloads_remote_cvid_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(
                tmp,
                "missevan.json",
                {
                    "100": {
                        "dramaId": 100,
                        "title": "猫耳剧",
                        "cover": "m-cover",
                        "maincvs": [11],
                        "cvnames": {"11": "猫耳名"},
                    }
                },
            )
            manbo_info = self.write_json(tmp, "manbo.json", {"version": 1, "records": []})
            missevan_counts = self.write_json(
                tmp,
                "missevan-counts.json",
                {"_meta": {}, "counts": {"100": {"view_count": 100}}},
            )
            manbo_counts = self.write_json(tmp, "manbo-counts.json", {"_meta": {}, "counts": {}})
            cvid_map = self.write_json(tmp, "map.json", {})
            remote_map = {"Remote CV": {"displayName": "Remote CV", "missevanCvId": 11, "avatar": "remote-avatar"}}
            upstash = Mock(return_value=json.dumps(remote_map, ensure_ascii=False))

            payload = build_cv_ranks.build_and_publish_cv_ranks(
                missevan_info_path=missevan_info,
                manbo_info_path=manbo_info,
                missevan_counts_path=missevan_counts,
                manbo_counts_path=manbo_counts,
                cvid_map_path=cvid_map,
                output_path=Path(tmp) / "ranks-cv.json",
                upstash=upstash,
                generated_at="2026-06-10T12:00:00+00:00",
                upload=False,
            )

            self.assertEqual(upstash.call_args_list[0].args[0], ["GET", "cvid-map:v1"])
            self.assertEqual(json.loads(cvid_map.read_text(encoding="utf-8")), remote_map)
            self.assertEqual(payload["rankings"]["missevan"][0]["cvName"], "Remote CV")
            self.assertEqual(payload["rankings"]["missevan"][0]["avatar"], "remote-avatar")

    def test_generation_time_uses_latest_watch_count_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(
                tmp,
                "missevan.json",
                {
                    "100": {
                        "dramaId": 100,
                        "title": "猫耳剧",
                        "cover": "m-cover",
                        "maincvs": [11],
                        "cvnames": {"11": "猫耳名"},
                    }
                },
            )
            manbo_info = self.write_json(
                tmp,
                "manbo.json",
                {
                    "version": 1,
                    "records": [
                        {
                            "dramaId": "200",
                            "name": "漫播剧",
                            "cover": "mb-cover",
                            "mainCvIds": [22],
                            "mainCvNicknames": ["漫播名"],
                        }
                    ],
                },
            )
            missevan_counts = self.write_json(
                tmp,
                "missevan-counts.json",
                {"_meta": {"updated_at": "2026-06-10T08:00:00+00:00"}, "counts": {"100": {"view_count": 100}}},
            )
            manbo_counts = self.write_json(
                tmp,
                "manbo-counts.json",
                {"_meta": {"updated_at": "2026-06-10T09:30:00+00:00"}, "counts": {"200": {"view_count": 60}}},
            )
            cvid_map = self.write_json(tmp, "map.json", {})

            with patch.object(build_cv_ranks, "sync_remote_cvid_map", return_value={}):
                payload = build_cv_ranks.build_and_publish_cv_ranks(
                    missevan_info_path=missevan_info,
                    manbo_info_path=manbo_info,
                    missevan_counts_path=missevan_counts,
                    manbo_counts_path=manbo_counts,
                    cvid_map_path=cvid_map,
                    output_path=Path(tmp) / "ranks-cv.json",
                    upload=False,
                )

        self.assertEqual(payload["generated_at"], "2026-06-10T09:30:00+00:00")
        self.assertEqual(payload["date"], "2026-06-10")

    def test_keeps_top_30_per_platform(self) -> None:
        cvid_map = {}
        missevan_store = {}
        manbo_store = {"records": []}
        missevan_counts = {}
        manbo_counts = {}
        for idx in range(35):
            drama_id = str(1000 + idx)
            cv_id = 2000 + idx
            missevan_store[drama_id] = {
                "dramaId": drama_id,
                "title": f"猫耳剧{idx:02d}",
                "cover": f"cover-{idx}",
                "maincvs": [cv_id],
                "cvnames": {str(cv_id): f"猫耳CV{idx:02d}"},
            }
            missevan_counts[drama_id] = {"view_count": 1000 - idx}

            manbo_id = str(3000 + idx)
            manbo_cv_id = 4000 + idx
            manbo_store["records"].append(
                {
                    "dramaId": manbo_id,
                    "name": f"漫播剧{idx:02d}",
                    "cover": f"mb-cover-{idx}",
                    "mainCvIds": [manbo_cv_id],
                    "mainCvNicknames": [f"漫播CV{idx:02d}"],
                }
            )
            manbo_counts[manbo_id] = {"view_count": 2000 - idx}

        payload = build_cv_ranks.build_cv_ranks_payload(
            missevan_store=missevan_store,
            manbo_store=manbo_store,
            missevan_counts=missevan_counts,
            manbo_counts=manbo_counts,
            cvid_map=cvid_map,
            generated_at="2026-06-10T12:00:00+00:00",
        )

        self.assertEqual(len(payload["rankings"]["missevan"]), 30)
        self.assertEqual(len(payload["rankings"]["manbo"]), 30)
        self.assertEqual(payload["rankings"]["missevan"][0]["cvName"], "猫耳CV00")
        self.assertEqual(payload["rankings"]["manbo"][0]["cvName"], "漫播CV00")


if __name__ == "__main__":
    unittest.main()
