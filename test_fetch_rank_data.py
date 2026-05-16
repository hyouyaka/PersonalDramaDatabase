import json
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
