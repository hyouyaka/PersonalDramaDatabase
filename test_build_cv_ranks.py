import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_cv_ranks


class BuildCvRanksTests(unittest.TestCase):
    def test_name_only_cv_gets_own_ranking_bucket(self) -> None:
        buckets: dict[str, dict] = {}
        paid_buckets: dict[str, dict] = {}
        cvid_map = {"林风": {"displayName": "林风", "missevanCvId": None, "avatar": ""}}
        missevan_ids, manbo_ids, name_index, avatar_index = build_cv_ranks.build_map_indexes(cvid_map)

        build_cv_ranks.collect_missevan_works(
            buckets,
            paid_buckets=paid_buckets,
            store={
                "94602": {
                    "dramaId": 94602,
                    "title": "测试剧",
                    "needpay": True,
                    "maincvs": [3946],
                    "cvnames": {"3946": "辰朔"},
                    "fallbackCvNames": ["林风"],
                }
            },
            counts={"94602": {"view_count": 100}},
            missevan_ids=missevan_ids,
            manbo_ids=manbo_ids,
            name_index=name_index,
            avatar_index=avatar_index,
        )

        self.assertEqual(buckets["林风"]["totalViewCount"], 100)
        self.assertEqual(buckets["林风"]["works"][0]["mainCvs"], ["辰朔", "林风"])
    def write_json(self, tmp: str, name: str, payload: dict) -> Path:
        path = Path(tmp) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def min_remote_missevan_store(self, records: dict[str, dict]) -> dict:
        payload = dict(records)
        filler_idx = 0
        while len(payload) < 100:
            drama_id = str(900000 + filler_idx)
            payload.setdefault(drama_id, {"dramaId": int(drama_id), "title": f"猫耳占位{filler_idx}", "maincvs": []})
            filler_idx += 1
        return payload

    def min_remote_manbo_store(self, records: list[dict]) -> dict:
        out = {"version": 1, "records": list(records)}
        filler_idx = 0
        while len(out["records"]) < 50:
            drama_id = str(800000 + filler_idx)
            out["records"].append({"dramaId": drama_id, "name": f"漫播占位{filler_idx}", "mainCvIds": []})
            filler_idx += 1
        return out

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
            def upstash(command: list[object]) -> object:
                if command[0] == "GET":
                    return None
                if command[0] == "SET":
                    return "OK"
                raise AssertionError(command)

            upstash = Mock(side_effect=upstash)

            with (
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", return_value=remote_map),
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs"),
                patch.object(build_cv_ranks, "run_cleanup_best_effort") as cleanup,
            ):
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
            self.assertEqual(payload["version"], 3)
            self.assertEqual(payload["missevanDramaCount"], 2)
            self.assertEqual(payload["manboDramaCount"], 1)
            self.assertEqual(set(payload["rankings"]), {"missevan", "manbo"})
            self.assertEqual(set(payload["paidRankings"]), {"missevan", "manbo"})

            missevan_cv = payload["rankings"]["missevan"][0]
            self.assertEqual(missevan_cv["cvName"], "Canonical CV")
            self.assertEqual(missevan_cv["avatar"], "https://avatar.test/canonical.jpg")
            self.assertEqual(missevan_cv["totalViewCount"], 100)
            self.assertEqual(missevan_cv["workCount"], 1)
            self.assertEqual([work["dramaId"] for work in missevan_cv["works"]], ["100"])
            self.assertEqual(missevan_cv["works"][0]["cover"], "m-cover")
            self.assertIs(missevan_cv["works"][0]["isPaid"], False)
            self.assertEqual(payload["paidRankings"]["missevan"], [])

            manbo_cv = payload["rankings"]["manbo"][0]
            self.assertEqual(manbo_cv["cvName"], "Canonical CV")
            self.assertEqual(manbo_cv["avatar"], "https://avatar.test/canonical.jpg")
            self.assertEqual(manbo_cv["totalViewCount"], 60)
            self.assertEqual(manbo_cv["workCount"], 1)
            self.assertEqual([work["dramaId"] for work in manbo_cv["works"]], ["200"])
            self.assertEqual(manbo_cv["works"][0]["cover"], "mb-cover")
            self.assertIs(manbo_cv["works"][0]["isPaid"], True)
            self.assertEqual(payload["paidRankings"]["manbo"][0]["totalViewCount"], 60)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            written_keys = [
                call.args[0][1]
                for call in upstash.call_args_list
                if call.args[0][0] == "SET"
            ]
            self.assertEqual(
                written_keys,
                ["ranks:cv:latest", "ranks:trend:cv:missevan", "ranks:trend:cv:manbo"],
            )
            cleanup.assert_called_once()

    def test_builds_paid_rankings_from_platform_paid_flags(self) -> None:
        missevan_store = {
            "100": {
                "dramaId": 100,
                "title": "猫耳免费剧",
                "needpay": False,
                "is_member": False,
                "maincvs": [11],
                "cvnames": {"11": "同一CV"},
            },
            "101": {
                "dramaId": 101,
                "title": "猫耳会员剧",
                "needpay": False,
                "is_member": True,
                "maincvs": [11],
                "cvnames": {"11": "同一CV"},
            },
            "102": {
                "dramaId": 102,
                "title": "猫耳付费剧",
                "needpay": True,
                "is_member": False,
                "maincvs": [11],
                "cvnames": {"11": "同一CV"},
            },
        }
        manbo_store = {
            "records": [
                {
                    "dramaId": "200",
                    "name": "漫播免费剧",
                    "needpay": False,
                    "vipFree": 0,
                    "mainCvIds": [22],
                    "mainCvNicknames": ["漫播CV"],
                },
                {
                    "dramaId": "201",
                    "name": "漫播会员剧",
                    "needpay": False,
                    "vipFree": 1,
                    "mainCvIds": [22],
                    "mainCvNicknames": ["漫播CV"],
                },
                {
                    "dramaId": "202",
                    "name": "漫播付费剧",
                    "needpay": True,
                    "vipFree": 0,
                    "mainCvIds": [22],
                    "mainCvNicknames": ["漫播CV"],
                },
            ]
        }

        payload = build_cv_ranks.build_cv_ranks_payload(
            missevan_store=missevan_store,
            manbo_store=manbo_store,
            missevan_counts={
                "100": {"view_count": 100},
                "101": {"view_count": 40},
                "102": {"view_count": 60},
            },
            manbo_counts={
                "200": {"view_count": 30},
                "201": {"view_count": 70},
                "202": {"view_count": 50},
            },
            cvid_map={},
            generated_at="2026-06-10T12:00:00+00:00",
        )

        self.assertEqual(payload["rankings"]["missevan"][0]["totalViewCount"], 200)
        self.assertEqual(payload["paidRankings"]["missevan"][0]["totalViewCount"], 100)
        self.assertEqual(
            [work["dramaId"] for work in payload["paidRankings"]["missevan"][0]["works"]],
            ["102", "101"],
        )
        self.assertEqual(payload["rankings"]["manbo"][0]["totalViewCount"], 150)
        self.assertEqual(payload["paidRankings"]["manbo"][0]["totalViewCount"], 120)
        self.assertEqual(
            [work["dramaId"] for work in payload["paidRankings"]["manbo"][0]["works"]],
            ["201", "202"],
        )

    def test_build_cv_trend_payload_keeps_latest_works_and_prunes_to_50_dates(self) -> None:
        old_dates = [f"2026-04-{day:02d}" for day in range(1, 51)]
        current = {
            "version": 1,
            "kind": "cv",
            "platform": "missevan",
            "updated_at": "2026-05-20T00:00:00+00:00",
            "dates": old_dates,
            "cvs": {
                "同一CV": {
                    "cvName": "同一CV",
                    "avatar": "old-avatar",
                    "works": [{"dramaId": "old", "viewCount": 1}],
                    "samples": {
                        date: {
                            "generated_at": f"{date}T00:00:00+00:00",
                            "metrics": {"totalViewCount": idx, "paidViewCount": idx // 2},
                            "ranks": {"total": idx},
                            "works": [{"dramaId": "must-not-survive"}],
                        }
                        for idx, date in enumerate(old_dates, 1)
                    },
                },
                "过期CV": {
                    "cvName": "过期CV",
                    "samples": {
                        "2026-04-01": {
                            "metrics": {"totalViewCount": 1, "paidViewCount": 0},
                            "ranks": {"total": 2},
                        }
                    },
                },
            },
        }
        total_ranking = [
            {
                "cvName": "同一CV",
                "avatar": "new-avatar",
                "totalViewCount": 300,
                "rank": 1,
                "workCount": 2,
                "works": [
                    {"platform": "missevan", "dramaId": "101", "title": "新剧", "viewCount": 200, "isPaid": True},
                    {"platform": "missevan", "dramaId": "100", "title": "免费剧", "viewCount": 100, "isPaid": False},
                ],
            },
            {
                "cvName": "新CV",
                "avatar": "",
                "totalViewCount": 50,
                "rank": 2,
                "workCount": 1,
                "works": [
                    {"platform": "missevan", "dramaId": "102", "title": "新CV剧", "viewCount": 50, "isPaid": False}
                ],
            },
        ]
        paid_ranking = [
            {
                "cvName": "同一CV",
                "avatar": "new-avatar",
                "totalViewCount": 200,
                "rank": 1,
                "workCount": 1,
                "works": [
                    {"platform": "missevan", "dramaId": "101", "title": "新剧", "viewCount": 200, "isPaid": True}
                ],
            }
        ]

        payload = build_cv_ranks.build_cv_trend_payload(
            current,
            "missevan",
            "2026-06-10",
            total_ranking,
            paid_ranking,
            generated_at="2026-06-10T12:00:00+00:00",
        )

        self.assertEqual(len(payload["dates"]), 50)
        self.assertNotIn("2026-04-01", payload["dates"])
        self.assertIn("2026-06-10", payload["dates"])
        self.assertNotIn("过期CV", payload["cvs"])
        cv = payload["cvs"]["同一CV"]
        self.assertEqual(cv["avatar"], "new-avatar")
        self.assertEqual([work["dramaId"] for work in cv["works"]], ["101", "100"])
        self.assertEqual(cv["samples"]["2026-06-10"]["metrics"], {"totalViewCount": 300, "paidViewCount": 200})
        self.assertEqual(cv["samples"]["2026-06-10"]["ranks"], {"total": 1, "paid": 1})
        older_sample = cv["samples"]["2026-04-50"]
        self.assertEqual(older_sample["metrics"], {"totalViewCount": 50, "paidViewCount": 25})
        self.assertNotIn("works", older_sample)

    def test_build_and_publish_uploads_cv_trends_after_rank_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(
                tmp,
                "missevan.json",
                {
                    "100": {
                        "dramaId": 100,
                        "title": "猫耳会员剧",
                        "needpay": False,
                        "is_member": True,
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
                            "name": "漫播免费剧",
                            "needpay": False,
                            "vipFree": 0,
                            "mainCvIds": [22],
                            "mainCvNicknames": ["漫播名"],
                        }
                    ],
                },
            )
            missevan_counts = self.write_json(tmp, "missevan-counts.json", {"_meta": {}, "counts": {"100": {"view_count": 100}}})
            manbo_counts = self.write_json(tmp, "manbo-counts.json", {"_meta": {}, "counts": {"200": {"view_count": 60}}})
            cvid_map = self.write_json(tmp, "map.json", {})
            commands: list[list[object]] = []

            def fake_upstash(command: list[object]) -> object:
                commands.append(command)
                if command[:2] in (["GET", "ranks:trend:cv:missevan"], ["GET", "ranks:trend:cv:manbo"]):
                    return None
                if command[0] == "SET":
                    return "OK"
                raise AssertionError(command)

            with (
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", return_value={}),
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs"),
                patch.object(build_cv_ranks, "run_cleanup_best_effort") as cleanup,
                patch("builtins.print"),
            ):
                build_cv_ranks.build_and_publish_cv_ranks(
                    missevan_info_path=missevan_info,
                    manbo_info_path=manbo_info,
                    missevan_counts_path=missevan_counts,
                    manbo_counts_path=manbo_counts,
                    cvid_map_path=cvid_map,
                    output_path=Path(tmp) / "ranks-cv.json",
                    upstash=fake_upstash,
                    generated_at="2026-06-10T12:00:00+00:00",
                    upload=True,
                )

            self.assertEqual(commands[0][0:2], ["SET", "ranks:cv:latest"])
            self.assertEqual(commands[1][0:2], ["GET", "ranks:trend:cv:missevan"])
            self.assertEqual(commands[2][0:2], ["SET", "ranks:trend:cv:missevan"])
            self.assertEqual(commands[3][0:2], ["GET", "ranks:trend:cv:manbo"])
            self.assertEqual(commands[4][0:2], ["SET", "ranks:trend:cv:manbo"])
            missevan_trend = json.loads(commands[2][2])
            self.assertEqual(missevan_trend["cvs"]["猫耳名"]["samples"]["2026-06-10"]["metrics"]["paidViewCount"], 100)
            manbo_trend = json.loads(commands[4][2])
            self.assertEqual(manbo_trend["cvs"]["漫播名"]["samples"]["2026-06-10"]["metrics"]["paidViewCount"], 0)
            cleanup.assert_called_once()

    def test_upload_cv_trends_does_not_overwrite_when_current_read_fails(self) -> None:
        commands: list[list[object]] = []

        def fake_upstash(command: list[object]) -> object:
            commands.append(command)
            if command[:2] == ["GET", "ranks:trend:cv:missevan"]:
                raise RuntimeError("temporary read failure")
            if command[0] == "SET":
                raise AssertionError("trend should not be overwritten after read failure")
            raise AssertionError(command)

        with self.assertRaisesRegex(RuntimeError, "Failed to load ranks:trend:cv:missevan"):
            build_cv_ranks.upload_cv_trends(
                history_date="2026-06-10",
                generated_at="2026-06-10T12:00:00+00:00",
                full_rankings={"missevan": [], "manbo": []},
                full_paid_rankings={"missevan": [], "manbo": []},
                upstash=fake_upstash,
            )

        self.assertEqual(commands, [["GET", "ranks:trend:cv:missevan"]])

    def test_no_upload_still_syncs_remote_inputs(self) -> None:
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

            def sync_inputs(**kwargs):
                self.assertEqual(kwargs["missevan_info_path"], missevan_info)
                self.assertEqual(kwargs["manbo_info_path"], manbo_info)
                self.assertEqual(kwargs["cvid_map_path"], cvid_map)
                return remote_map

            with (
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", side_effect=sync_inputs) as sync_remote,
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs"),
            ):
                payload = build_cv_ranks.build_and_publish_cv_ranks(
                    missevan_info_path=missevan_info,
                    manbo_info_path=manbo_info,
                    missevan_counts_path=missevan_counts,
                    manbo_counts_path=manbo_counts,
                    cvid_map_path=cvid_map,
                    output_path=Path(tmp) / "ranks-cv.json",
                    upstash=Mock(),
                    generated_at="2026-06-10T12:00:00+00:00",
                    upload=False,
                )

            sync_remote.assert_called_once()
            self.assertEqual(payload["rankings"]["missevan"][0]["cvName"], "Remote CV")
            self.assertEqual(payload["rankings"]["missevan"][0]["avatar"], "remote-avatar")

    def test_downloads_remote_platform_libraries_before_building(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(tmp, "missevan.json", {})
            manbo_info = self.write_json(tmp, "manbo.json", {"version": 1, "records": []})
            missevan_counts = self.write_json(
                tmp,
                "missevan-counts.json",
                {"_meta": {}, "counts": {"100": {"view_count": 100}}},
            )
            manbo_counts = self.write_json(
                tmp,
                "manbo-counts.json",
                {"_meta": {}, "counts": {"200": {"view_count": 60}}},
            )
            cvid_map = self.write_json(tmp, "map.json", {})
            output = Path(tmp) / "ranks-cv.json"
            remote_manbo = self.min_remote_manbo_store(
                [
                    {
                        "dramaId": "200",
                        "name": "远端漫播剧",
                        "cover": "mb-cover",
                        "mainCvIds": [22],
                        "mainCvNicknames": ["漫播名"],
                    }
                ]
            )
            remote_missevan = self.min_remote_missevan_store(
                {
                    "100": {
                        "dramaId": 100,
                        "title": "远端猫耳剧",
                        "cover": "m-cover",
                        "maincvs": [11],
                        "cvnames": {"11": "猫耳名"},
                    }
                }
            )
            remote_map = {
                "Remote CV": {
                    "displayName": "Remote CV",
                    "missevanCvId": 11,
                    "manboCvId": 22,
                }
            }
            upstash = Mock(
                side_effect=[
                    None,
                    None,
                    json.dumps(remote_manbo, ensure_ascii=False),
                    json.dumps(remote_missevan, ensure_ascii=False),
                    json.dumps(remote_map, ensure_ascii=False),
                ]
            )

            payload = build_cv_ranks.build_and_publish_cv_ranks(
                missevan_info_path=missevan_info,
                manbo_info_path=manbo_info,
                missevan_counts_path=missevan_counts,
                manbo_counts_path=manbo_counts,
                cvid_map_path=cvid_map,
                output_path=output,
                upstash=upstash,
                generated_at="2026-06-10T12:00:00+00:00",
                upload=False,
            )

            self.assertEqual(upstash.call_args_list[0].args[0], ["GET", "missevan:watchcount:latest"])
            self.assertEqual(upstash.call_args_list[1].args[0], ["GET", "manbo:watchcount:latest"])
            self.assertEqual(upstash.call_args_list[2].args[0], ["GET", "manbo:info:v1"])
            self.assertEqual(upstash.call_args_list[3].args[0], ["GET", "missevan:info:v1"])
            self.assertEqual(upstash.call_args_list[4].args[0], ["GET", "cvid-map:v1"])
            self.assertEqual(json.loads(manbo_info.read_text(encoding="utf-8")), remote_manbo)
            self.assertEqual(json.loads(missevan_info.read_text(encoding="utf-8")), remote_missevan)
            self.assertEqual(json.loads(cvid_map.read_text(encoding="utf-8")), remote_map)
            self.assertEqual(payload["rankings"]["missevan"][0]["works"][0]["title"], "远端猫耳剧")
            self.assertEqual(payload["rankings"]["manbo"][0]["works"][0]["title"], "远端漫播剧")

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

            with (
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", return_value={}),
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs"),
            ):
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

    def test_syncs_remote_watchcounts_before_loading_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(
                tmp,
                "missevan.json",
                {"100": {"dramaId": 100, "title": "猫耳剧", "maincvs": [11], "cvnames": {"11": "猫耳名"}}},
            )
            manbo_info = self.write_json(tmp, "manbo.json", {"version": 1, "records": []})
            missevan_counts = self.write_json(
                tmp,
                "missevan-counts.json",
                {"_meta": {"updated_at": "2026-06-10T08:00:00+00:00"}, "counts": {"100": {"view_count": 100}}},
            )
            manbo_counts = self.write_json(tmp, "manbo-counts.json", {"_meta": {}, "counts": {}})
            cvid_map = self.write_json(tmp, "map.json", {})

            def sync_watchcounts(**kwargs):
                self.assertEqual(kwargs["missevan_counts_path"], missevan_counts)
                self.assertEqual(kwargs["manbo_counts_path"], manbo_counts)
                self.assertFalse(kwargs["force"])
                missevan_counts.write_text(
                    json.dumps(
                        {"_meta": {"updated_at": "2026-06-11T08:00:00+00:00"}, "counts": {"100": {"view_count": 250}}},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            with (
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs", side_effect=sync_watchcounts) as sync_remote,
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", return_value={}),
            ):
                payload = build_cv_ranks.build_and_publish_cv_ranks(
                    missevan_info_path=missevan_info,
                    manbo_info_path=manbo_info,
                    missevan_counts_path=missevan_counts,
                    manbo_counts_path=manbo_counts,
                    cvid_map_path=cvid_map,
                    output_path=Path(tmp) / "ranks-cv.json",
                    upload=False,
                )

            sync_remote.assert_called_once()
            self.assertEqual(payload["rankings"]["missevan"][0]["totalViewCount"], 250)
            self.assertEqual(payload["generated_at"], "2026-06-11T08:00:00+00:00")

    def test_force_is_passed_to_watchcount_sync_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missevan_info = self.write_json(tmp, "missevan.json", {})
            manbo_info = self.write_json(tmp, "manbo.json", {"version": 1, "records": []})
            missevan_counts = self.write_json(tmp, "missevan-counts.json", {"_meta": {}, "counts": {}})
            manbo_counts = self.write_json(tmp, "manbo-counts.json", {"_meta": {}, "counts": {}})
            cvid_map = self.write_json(tmp, "map.json", {})

            with (
                patch.object(build_cv_ranks, "sync_remote_watchcount_inputs") as sync_watchcounts,
                patch.object(build_cv_ranks, "sync_remote_rank_inputs", return_value={}) as sync_rank_inputs,
            ):
                build_cv_ranks.build_and_publish_cv_ranks(
                    missevan_info_path=missevan_info,
                    manbo_info_path=manbo_info,
                    missevan_counts_path=missevan_counts,
                    manbo_counts_path=manbo_counts,
                    cvid_map_path=cvid_map,
                    output_path=Path(tmp) / "ranks-cv.json",
                    upload=False,
                    force=True,
                )

        sync_watchcounts.assert_called_once()
        self.assertTrue(sync_watchcounts.call_args.kwargs["force"])
        self.assertNotIn("force", sync_rank_inputs.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
