import json
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import fetch_rank_data


class RankFullStoreKeyTests(unittest.TestCase):
    def test_upload_full_ranks_writes_latest_key_only(self) -> None:
        store = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}

        with (
            patch.object(fetch_rank_data, "upstash_request", return_value="OK") as request,
            patch("builtins.print"),
        ):
            fetch_rank_data.upload_full_ranks(store)

        request.assert_called_once()
        command = request.call_args.args[0]
        self.assertEqual(command[:2], ["SET", "ranks:latest"])
        self.assertEqual(json.loads(command[2]), store)

    def test_load_remote_full_ranks_reads_latest_key_only(self) -> None:
        payload = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}

        with patch.object(fetch_rank_data, "upstash_request", return_value=json.dumps(payload)) as request:
            self.assertEqual(fetch_rank_data.load_remote_full_ranks(), payload)

        request.assert_called_once_with(["GET", "ranks:latest"])


class SeriesInfoStoreTests(unittest.TestCase):
    def test_load_series_info_reads_remote_key_first(self) -> None:
        payload = {"series-1": {"platform": "猫耳", "dramaIds": ["100"]}}

        with patch.object(fetch_rank_data, "upstash_request", return_value=json.dumps(payload)) as request:
            self.assertEqual(fetch_rank_data.load_series_info(), payload)

        request.assert_called_once_with(["GET", "drama:series-info:v1"])

    def test_load_series_info_falls_back_to_local_backup(self) -> None:
        fallback = {"series-2": {"platform": "猫耳", "dramaIds": ["200"]}}

        with (
            patch.object(fetch_rank_data, "upstash_request", side_effect=RuntimeError("upstash down")),
            patch.object(fetch_rank_data, "load_json", return_value=fallback) as load_json,
            patch("builtins.print"),
        ):
            self.assertEqual(fetch_rank_data.load_series_info(), fallback)

        load_json.assert_called_once_with(fetch_rank_data.SERIES_INFO_PATH, {})


class RankTrendPayloadTests(unittest.TestCase):
    def _metrics_payload(self, date: str, *, generated_at: str = "2026-05-16T00:00:00+00:00") -> dict:
        return {
            "version": 1,
            "date": date,
            "platform": "missevan",
            "generated_at": generated_at,
            "dramas": {
                "93038": {
                    "name": "一屋暗灯 全一季",
                    "view_count": 123,
                    "danmaku_uid_count": 45,
                    "subscription_num": 67,
                    "cover": "cover-a",
                    "maincvs": ["甲", "乙"],
                    "catalogName": "广播剧",
                    "payStatus": "付费",
                    "createTime": "2026-01-01",
                    "updated_at": "2026-05-16T12:00:00+00:00",
                },
                "10000": {
                    "name": "只在指标里",
                    "view_count": 10,
                },
            },
        }

    def _list_payload(self, date: str, *, generated_at: str = "2026-05-16T00:00:00+00:00") -> dict:
        return {
            "version": 1,
            "date": date,
            "platform": "missevan",
            "generated_at": generated_at,
            "ranks": {
                "new_daily": {
                    "name": "新品日榜",
                    "items": [
                        {"drama_id": "93038", "position": 3},
                        {"drama_id": "missing-metric", "position": 4},
                    ],
                },
                "popular_weekly": {
                    "name": "人气周榜",
                    "items": [
                        {"dramaId": "93038", "position": 8},
                    ],
                },
                "peak": {
                    "name": "巅峰榜",
                    "items": [
                        {"drama_id": "93038", "position": 1},
                    ],
                },
            },
        }

    def test_build_rank_trend_payload_merges_metrics_and_rank_badges(self) -> None:
        payload = fetch_rank_data.build_rank_trend_payload(
            None,
            "missevan",
            "2026-05-16",
            self._metrics_payload("2026-05-16"),
            self._list_payload("2026-05-16"),
            generated_at="2026-05-16T00:00:00+00:00",
            pruned_dates=(),
        )

        self.assertEqual(payload["platform"], "missevan")
        self.assertEqual(payload["dates"], ["2026-05-16"])
        drama = payload["dramas"]["93038"]
        self.assertEqual(drama["id"], "93038")
        self.assertEqual(drama["name"], "一屋暗灯 全一季")
        self.assertEqual(drama["cover"], "cover-a")
        self.assertEqual(drama["maincvs"], ["甲", "乙"])
        self.assertEqual(drama["catalogName"], "广播剧")
        self.assertEqual(drama["payStatus"], "付费")
        self.assertEqual(drama["createTime"], "2026-01-01")
        self.assertEqual(drama["updated_at"], "2026-05-16T12:00:00+00:00")
        sample = drama["samples"]["2026-05-16"]
        self.assertEqual(sample["metrics"]["view_count"], 123)
        self.assertEqual(sample["metrics"]["danmaku_uid_count"], 45)
        self.assertEqual(
            sample["ranks"],
            [
                {"key": "new_daily", "name": "新品日榜", "position": 3},
                {"key": "popular_weekly", "name": "人气周榜", "position": 8},
            ],
        )
        self.assertNotIn("missing-metric", payload["dramas"])

    def test_build_rank_trend_payload_keeps_metrics_when_list_missing(self) -> None:
        payload = fetch_rank_data.build_rank_trend_payload(
            None,
            "missevan",
            "2026-05-16",
            self._metrics_payload("2026-05-16"),
            None,
            generated_at="2026-05-16T00:00:00+00:00",
            pruned_dates=(),
        )

        sample = payload["dramas"]["93038"]["samples"]["2026-05-16"]
        self.assertEqual(sample["ranks"], [])
        self.assertEqual(sample["metrics"]["subscription_num"], 67)

    def test_build_rank_trend_payload_prunes_old_dates_and_empty_dramas(self) -> None:
        current = {
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-05-15T00:00:00+00:00",
            "dates": ["2026-05-14", "2026-05-15"],
            "dramas": {
                "93038": {
                    "id": "93038",
                    "name": "一屋暗灯 全一季",
                    "samples": {"2026-05-14": {"metrics": {"view_count": 1}, "ranks": []}},
                },
                "old-only": {
                    "id": "old-only",
                    "name": "旧剧",
                    "samples": {"2026-05-14": {"metrics": {"view_count": 2}, "ranks": []}},
                },
            },
        }

        payload = fetch_rank_data.build_rank_trend_payload(
            current,
            "missevan",
            "2026-05-16",
            self._metrics_payload("2026-05-16"),
            self._list_payload("2026-05-16"),
            generated_at="2026-05-16T00:00:00+00:00",
            pruned_dates=("2026-05-14",),
        )

        self.assertEqual(payload["dates"], ["2026-05-16"])
        self.assertNotIn("old-only", payload["dramas"])
        self.assertNotIn("2026-05-14", payload["dramas"]["93038"]["samples"])

    def test_build_rank_trend_payload_updates_top_level_metadata_from_new_sample(self) -> None:
        current = {
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-05-15T00:00:00+00:00",
            "dates": ["2026-05-15"],
            "dramas": {
                "93038": {
                    "id": "93038",
                    "name": "旧名",
                    "cover": "old-cover",
                    "maincvs": ["旧CV"],
                    "catalogName": "旧分类",
                    "payStatus": "旧付费状态",
                    "createTime": "2025-01-01",
                    "updated_at": "2026-05-15T08:00:00+00:00",
                    "samples": {"2026-05-15": {"metrics": {"view_count": 100}, "ranks": []}},
                }
            },
        }

        payload = fetch_rank_data.build_rank_trend_payload(
            current,
            "missevan",
            "2026-05-16",
            self._metrics_payload("2026-05-16"),
            self._list_payload("2026-05-16"),
            generated_at="2026-05-16T00:00:00+00:00",
            pruned_dates=(),
        )

        drama = payload["dramas"]["93038"]
        self.assertEqual(drama["name"], "一屋暗灯 全一季")
        self.assertEqual(drama["cover"], "cover-a")
        self.assertEqual(drama["maincvs"], ["甲", "乙"])
        self.assertEqual(drama["catalogName"], "广播剧")
        self.assertEqual(drama["payStatus"], "付费")
        self.assertEqual(drama["createTime"], "2026-01-01")
        self.assertEqual(drama["updated_at"], "2026-05-16T12:00:00+00:00")
        self.assertIn("2026-05-15", drama["samples"])
        self.assertIn("2026-05-16", drama["samples"])


class RankTrendBackfillTests(unittest.TestCase):
    def test_backfill_reads_history_shards_and_writes_selected_trend_key(self) -> None:
        responses = {
            "ranks:index": {"version": 1, "dates": ["2026-05-15", "2026-05-16"]},
            "ranks:metrics:2026-05-15:missevan": {
                "version": 1,
                "date": "2026-05-15",
                "platform": "missevan",
                "generated_at": "2026-05-15T00:00:00+00:00",
                "dramas": {"93038": {"name": "一屋暗灯 全一季", "view_count": 100}},
            },
            "ranks:list:2026-05-15:missevan": {
                "version": 1,
                "date": "2026-05-15",
                "platform": "missevan",
                "generated_at": "2026-05-15T00:00:00+00:00",
                "ranks": {"new_daily": {"name": "新品日榜", "items": [{"drama_id": "93038", "position": 2}]}},
            },
            "ranks:metrics:2026-05-16:missevan": {
                "version": 1,
                "date": "2026-05-16",
                "platform": "missevan",
                "generated_at": "2026-05-16T00:00:00+00:00",
                "dramas": {"93038": {"name": "一屋暗灯 全一季", "view_count": 123}},
            },
            "ranks:list:2026-05-16:missevan": None,
        }
        commands: list[list[object]] = []

        def fake_request(command: list[object]) -> object:
            commands.append(command)
            if command[0] == "GET":
                value = responses[command[1]]
                return json.dumps(value, ensure_ascii=False) if value is not None else None
            if command[:2] == ["SET", "ranks:trend:missevan"]:
                return "OK"
            raise AssertionError(command)

        with patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request), patch("builtins.print"):
            payloads = fetch_rank_data.backfill_rank_trends_from_history(("missevan",))

        self.assertEqual(
            [command[:2] for command in commands[:5]],
            [
                ["GET", "ranks:index"],
                ["GET", "ranks:metrics:2026-05-15:missevan"],
                ["GET", "ranks:list:2026-05-15:missevan"],
                ["GET", "ranks:metrics:2026-05-16:missevan"],
                ["GET", "ranks:list:2026-05-16:missevan"],
            ],
        )
        self.assertEqual(commands[-1][:2], ["SET", "ranks:trend:missevan"])
        written = json.loads(commands[-1][2])
        self.assertEqual(written["dates"], ["2026-05-15", "2026-05-16"])
        self.assertEqual(set(written["dramas"]["93038"]["samples"]), {"2026-05-15", "2026-05-16"})
        self.assertEqual(payloads["missevan"], written)

    def test_upload_rank_history_updates_daily_trend_key(self) -> None:
        store = {
            "missevan": {
                "ranks": {"new_daily": {"name": "新品日榜", "items": [{"dramaId": "93038"}]}},
                "dramas": {
                    "93038": {
                        "name": "一屋暗灯 全一季",
                        "view_count": 123,
                        "cover": "daily-cover",
                        "maincvs": ["日常CV"],
                        "catalogName": "日常分类",
                        "payStatus": "日常付费状态",
                        "createTime": "2026-02-01",
                        "updated_at": "2026-05-16T18:00:00+00:00",
                    }
                },
            },
            "manbo": {"ranks": {}, "dramas": {}},
        }
        commands: list[list[object]] = []

        def fake_request(command: list[object]) -> object:
            commands.append(command)
            if command[0] == "GET":
                if command[1] == "ranks:trend:missevan":
                    return json.dumps(
                        {
                            "version": 1,
                            "platform": "missevan",
                            "updated_at": "2026-05-15T00:00:00+00:00",
                            "dates": ["2026-05-15"],
                            "dramas": {
                                "93038": {
                                    "id": "93038",
                                    "name": "一屋暗灯 全一季",
                                    "samples": {"2026-05-15": {"metrics": {"view_count": 100}, "ranks": []}},
                                }
                            },
                        },
                        ensure_ascii=False,
                    )
                return None
            if command[0] == "SET":
                return "OK"
            if command[0] == "DEL":
                return 1
            raise AssertionError(command)

        with (
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-16T00:00:00+00:00"),
            patch.object(fetch_rank_data, "update_rank_history_index_atomic", return_value=["2026-05-14"]),
            patch.object(fetch_rank_data, "upload_missevan_peak_trend"),
            patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request),
            patch("builtins.print"),
        ):
            fetch_rank_data.upload_rank_history(store, platforms=("missevan",))

        trend_sets = [command for command in commands if command[:2] == ["SET", "ranks:trend:missevan"]]
        self.assertEqual(len(trend_sets), 1)
        trend = json.loads(trend_sets[0][2])
        self.assertEqual(trend["dates"], ["2026-05-15", "2026-05-16"])
        drama = trend["dramas"]["93038"]
        self.assertEqual(drama["cover"], "daily-cover")
        self.assertEqual(drama["maincvs"], ["日常CV"])
        self.assertEqual(drama["catalogName"], "日常分类")
        self.assertEqual(drama["payStatus"], "日常付费状态")
        self.assertEqual(drama["createTime"], "2026-02-01")
        self.assertEqual(drama["updated_at"], "2026-05-16T18:00:00+00:00")
        self.assertEqual(drama["samples"]["2026-05-16"]["metrics"]["view_count"], 123)

    def test_upload_rank_trend_snapshot_does_not_overwrite_when_current_read_fails(self) -> None:
        commands: list[list[object]] = []

        def fake_request(command: list[object]) -> object:
            commands.append(command)
            if command[:2] == ["GET", "ranks:trend:missevan"]:
                raise RuntimeError("temporary read failure")
            if command[0] == "SET":
                raise AssertionError("trend should not be overwritten after read failure")
            raise AssertionError(command)

        with patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request):
            with self.assertRaisesRegex(RuntimeError, "Failed to load ranks:trend:missevan"):
                fetch_rank_data.upload_rank_trend_snapshot(
                    "missevan",
                    "2026-05-16",
                    {
                        "version": 1,
                        "date": "2026-05-16",
                        "platform": "missevan",
                        "generated_at": "2026-05-16T00:00:00+00:00",
                        "dramas": {"93038": {"name": "一屋暗灯 全一季", "view_count": 123}},
                    },
                    {
                        "version": 1,
                        "date": "2026-05-16",
                        "platform": "missevan",
                        "generated_at": "2026-05-16T00:00:00+00:00",
                        "ranks": {"new_daily": {"name": "新品日榜", "items": [{"drama_id": "93038"}]}},
                    },
                    generated_at="2026-05-16T00:00:00+00:00",
                )

        self.assertEqual(commands, [["GET", "ranks:trend:missevan"]])

    def test_upload_rank_outputs_still_updates_latest_when_trend_read_fails(self) -> None:
        store = {
            "_meta": {"updated_at": "2026-05-16T00:00:00+00:00"},
            "missevan": {"ranks": {}, "dramas": {}},
            "manbo": {
                "ranks": {"popular_daily": {"name": "人气日榜", "items": [{"dramaId": "93038"}]}},
                "dramas": {"93038": {"name": "一屋暗灯 全一季", "view_count": 123}},
            },
        }
        commands: list[list[object]] = []
        written: dict[str, str] = {}

        def fake_request(command: list[object]) -> object:
            commands.append(command)
            if command[0] == "EVAL":
                return "[]"
            if command[:2] == ["GET", "ranks:trend:manbo"]:
                raise RuntimeError("temporary trend read failure")
            if command[0] == "GET":
                return written.get(str(command[1]))
            if command[0] == "SET":
                written[str(command[1])] = str(command[2])
                return "OK"
            if command[0] == "DEL":
                return 1
            raise AssertionError(command)

        with (
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-16T00:00:00+00:00"),
            patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request),
            patch("builtins.print"),
        ):
            merged = fetch_rank_data.upload_rank_outputs(store, ("manbo",))

        self.assertIn("ranks:latest", written)
        self.assertEqual(json.loads(written["ranks:latest"])["manbo"]["dramas"]["93038"]["view_count"], 123)
        self.assertEqual(merged["manbo"]["dramas"]["93038"]["view_count"], 123)


class RankTrendCliTests(unittest.TestCase):
    def test_backfill_cli_runs_and_exits_before_refresh_flow(self) -> None:
        with (
            patch.object(sys, "argv", ["fetch_rank_data.py", "--backfill-rank-trends-from-history", "--missevan-only"]),
            patch.object(fetch_rank_data, "backfill_rank_trends_from_history", return_value={"missevan": {}}) as backfill,
            patch.object(fetch_rank_data, "load_initial_rank_store", side_effect=AssertionError("refresh should not run")),
            patch("builtins.print"),
        ):
            fetch_rank_data.main()

        backfill.assert_called_once_with(("missevan",))


class ManboCvLookupTests(unittest.TestCase):
    def test_lookup_cvs_falls_back_to_nicknames_when_main_cv_names_are_blank(self) -> None:
        store = {
            "missevan": {"dramas": {}},
            "manbo": {
                "dramas": {
                    "drama-1": {"name": "测试剧"},
                }
            },
        }

        def load_remote(key: str):
            if key == "missevan:info:v1":
                return {}
            if key == "manbo:info:v1":
                return {
                    "records": [
                        {
                            "dramaId": "drama-1",
                            "mainCvNames": ["规范名甲", ""],
                            "mainCvNicknames": ["接口昵称甲", "接口昵称乙"],
                            "catalog": 1,
                            "needpay": True,
                            "createTime": "2026.05",
                        }
                    ]
                }
            raise AssertionError(key)

        with patch.object(fetch_rank_data, "_load_upstash_json", side_effect=load_remote), patch("builtins.print"):
            fetch_rank_data.lookup_cvs(store)

        self.assertEqual(store["manbo"]["dramas"]["drama-1"]["maincvs"], ["规范名甲", "接口昵称乙"])


class ManboDanmakuStabilityTests(unittest.TestCase):
    def _manbo_page_requester(self, pages: dict[tuple[str, int], dict]):
        def request_json(url: str) -> dict:
            query = parse_qs(urlparse(url).query)
            set_id = query["dramaSetId"][0]
            page_no = int(query["pageNo"][0])
            return pages[(set_id, page_no)]

        return request_json

    def test_manbo_global_dedupe_matches_set_level_dedupe(self) -> None:
        pages = {
            ("set-a", 1): {"data": {"count": 3, "list": [{"eid": "1"}, {"eid": "2"}]}},
            ("set-a", 2): {"data": {"count": 3, "list": [{"eid": "3"}]}},
            ("set-b", 1): {"data": {"count": 2, "list": [{"eid": "2"}, {"eid": "4"}]}},
        }

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=self._manbo_page_requester(pages),
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a", "set-b"],
            page_size=2,
            page_concurrency=4,
        )

        self.assertEqual(result["failed_page_count"], 0)
        self.assertEqual(result["unique_user_count"], 4)
        self.assertEqual(result["total_danmaku"], 5)

    def test_manbo_missing_page_is_retryable_failure(self) -> None:
        pages = {
            ("set-a", 1): {"data": {"count": 5, "list": [{"eid": "1"}, {"eid": "2"}]}},
            ("set-a", 2): {"data": {"count": 5, "list": [{"eid": "3"}, {"eid": "4"}]}},
            ("set-a", 3): {"data": {"count": 5, "list": []}},
        }

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=self._manbo_page_requester(pages),
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a"],
            page_size=2,
            page_concurrency=4,
        )

        self.assertEqual(result["unique_user_count"], 4)
        self.assertEqual(result["failed_page_count"], 1)
        self.assertIn("incomplete", result["failed_pages"][0]["error"])

    def test_manbo_short_successful_pages_are_retryable_failure(self) -> None:
        pages = {
            ("set-a", 1): {"data": {"count": 4, "list": [{"eid": "1"}, {"eid": "2"}]}},
            ("set-a", 2): {"data": {"count": 4, "list": [{"eid": "3"}]}},
        }

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=self._manbo_page_requester(pages),
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a"],
            page_size=2,
            page_concurrency=4,
        )

        self.assertEqual(result["failed_page_count"], 1)
        self.assertIn("incomplete", result["failed_pages"][0]["error"])

    def test_manbo_low_value_over_two_percent_uses_existing_retry_path(self) -> None:
        store = {
            "manbo": {
                "dramas": {
                    "600": {"danmaku_uid_count": 600, "fetched_at": "old"},
                }
            }
        }

        with (
            patch.object(fetch_rank_data, "_fetch_one_manbo"),
            patch.object(fetch_rank_data, "fetch_one_manbo_danmaku_count", side_effect=[("600", 587), ("600", 601)]) as fetch_count,
            patch.object(fetch_rank_data, "save_json"),
            patch("builtins.print"),
        ):
            fetch_rank_data.fetch_manbo_drama_details({"600"}, store, skip_danmaku=False, danmaku_ids={"600"})

        self.assertEqual(fetch_count.call_count, 2)
        self.assertEqual(store["manbo"]["dramas"]["600"]["danmaku_uid_count"], 601)

    def test_manbo_low_value_within_two_percent_is_allowed(self) -> None:
        store = {
            "manbo": {
                "dramas": {
                    "600": {"danmaku_uid_count": 600, "fetched_at": "old"},
                }
            }
        }

        with (
            patch.object(fetch_rank_data, "_fetch_one_manbo"),
            patch.object(fetch_rank_data, "fetch_one_manbo_danmaku_count", return_value=("600", 590)) as fetch_count,
            patch.object(fetch_rank_data, "save_json"),
            patch("builtins.print"),
        ):
            fetch_rank_data.fetch_manbo_drama_details({"600"}, store, skip_danmaku=False, danmaku_ids={"600"})

        self.assertEqual(fetch_count.call_count, 1)
        self.assertEqual(store["manbo"]["dramas"]["600"]["danmaku_uid_count"], 590)


class MissevanDanmakuLoggingTests(unittest.TestCase):
    def test_missevan_danmaku_logs_paid_sound_summary_on_failure(self) -> None:
        episodes = [
            {"need_pay": True, "sound_id": "100"},
            {"price": 1, "sound_id": "200"},
        ]
        entry = {}

        class Response:
            def __init__(self, text: str, fail: bool = False) -> None:
                self.text = text
                self.fail = fail

            def raise_for_status(self) -> None:
                if self.fail:
                    raise RuntimeError("boom")

        def fake_get(url: str, **_kwargs) -> Response:
            if "soundid=200" in url:
                return Response("", fail=True)
            return Response('<d p="0,0,0,0,0,0,u1"></d>')

        with (
            patch.object(fetch_rank_data.requests, "get", side_effect=fake_get),
            patch.object(fetch_rank_data.time, "sleep"),
            patch("builtins.print") as print_mock,
        ):
            with self.assertRaises(fetch_rank_data.DanmakuRefreshError):
                fetch_rank_data._fetch_missevan_danmaku(None, episodes, entry)

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("paid_sounds=2", printed)
        self.assertIn("success=1", printed)
        self.assertIn("failed=1", printed)
        self.assertIn("unique_users=1", printed)


if __name__ == "__main__":
    unittest.main()
