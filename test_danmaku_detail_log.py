import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import fetch_rank_data as ranks
from danmaku_detail_log import EpisodeDetailLog


class EpisodeDetailLogTests(unittest.TestCase):
    def test_daily_environment_enables_file_with_requested_date_and_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"DRAMA_DANMAKU_DETAIL_LOG": "1", "DRAMA_DANMAKU_LOG_DIR": tmp,
                                        "DRAMA_DANMAKU_LOG_DATE": "2026-11-17", "DRAMA_DANMAKU_RUN_ID": "daily-run"}),
                patch.object(ranks, "DANMAKU_DETAIL_LOG", None),
                patch("builtins.print"),
            ):
                ranks.configure_episode_detail_log()
                ranks.record_episode_detail("missevan", "93038", "13111277", "第十三集", 25000, True)
            row = json.loads((Path(tmp) / "danmaku-details-2026-11-17.jsonl").read_text(encoding="utf-8"))
            self.assertEqual((row["run_id"], row["fetched_count"]), ("daily-run", 25000))

    def test_parallel_rows_and_attempts_survive_reopening_for_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = EpisodeDetailLog(Path(tmp), "2026-11-17", "run-a")
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: journal.record("manbo", "600", "1", "标题\n下一行", 10, True), range(20)))
            reopened = EpisodeDetailLog(Path(tmp), "2026-11-17", "run-a")
            reopened.record("manbo", "600", "1", "标题", 8, False)
            rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["attempt"] for row in rows], list(range(1, 22)))
            self.assertEqual(len(rows[0]), 9)
            self.assertEqual(rows[-1]["complete"], False)
            self.assertEqual(rows[0]["episode_title"], "标题\n下一行")
            other = EpisodeDetailLog(Path(tmp), "2026-11-17", "run-b")
            other.record("manbo", "600", "1", "标题", 10, True)
            self.assertEqual(json.loads(journal.path.read_text(encoding="utf-8").splitlines()[-1])["attempt"], 1)

    def test_interrupted_line_does_not_corrupt_next_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = EpisodeDetailLog(Path(tmp), "2026-11-17", "run")
            journal.path.write_bytes(b'{"interrupted":')
            journal.record("missevan", "93038", "1", "分集", 0, True)
            lines = journal.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[-1])["fetched_count"], 0)

    def test_write_failure_warns_once_and_does_not_fail_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = EpisodeDetailLog(Path(tmp), "2026-11-17", "run")
            with patch.object(Path, "open", side_effect=OSError("disk full")), patch("builtins.print") as output:
                journal.record("manbo", "1", "2", "", 1, True)
                journal.record("manbo", "1", "3", "", 2, True)
            self.assertEqual(output.call_count, 1)

    def test_missevan_logs_raw_count_and_http_418_without_extra_requests(self):
        logger = Mock()
        response = requests.Response()
        response.status_code = 418
        response.url = "https://www.missevan.com/sound/getdm?soundid=2"
        episodes = [{"sound_id": "1", "need_pay": 1, "name": "第一集"},
                    {"sound_id": "2", "need_pay": 1, "name": "第二集"}]
        xml = '<i><d p="0,0,0,0,0,0,u">a</d><d p="0,0,0,0,0,0,u">b</d></i>'
        with (
            patch.object(ranks, "DANMAKU_DETAIL_LOG", logger),
            patch.object(ranks.requests, "get", side_effect=[Mock(text=xml), response]) as get,
            patch.object(ranks.time, "sleep"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
                ranks._fetch_missevan_danmaku(None, episodes, {}, drama_id="93038")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(logger.record.call_args_list[0].args, ("missevan", "93038", "1", "第一集", 2, True))
        self.assertEqual(logger.record.call_args_list[1].args, ("missevan", "93038", "2", "第二集", None, False))

    def test_manbo_retries_replace_short_page_counts_and_reuse_complete_pages(self):
        progress = {}
        logger = Mock()
        calls = []
        page_two_attempts = 0

        def request(url):
            nonlocal page_two_attempts
            calls.append(url)
            if "/dramaDetail?" in url:
                return {"data": {"setRespList": [{"setId": "1", "payType": 1, "setTitle": "第一集"}]}}
            if "pageNo=1" in url:
                return {"data": {"count": 4, "list": [{"eid": "a"}, {"eid": "b"}]}}
            self.assertIn("pageNo=2", url)
            page_two_attempts += 1
            return {"data": {"count": 4, "list": [{"eid": "b"}] if page_two_attempts == 1 else [{"eid": "b"}, {"eid": "c"}]}}

        with patch.object(ranks, "DANMAKU_DETAIL_LOG", logger):
            first = ranks.fetch_manbo_paid_danmaku_benchmark(
                "600", request_json=request, page_size=2, short_page_retries=1, progress=progress,
            )
            second = ranks.fetch_manbo_paid_danmaku_benchmark(
                "600", request_json=request, page_size=2, short_page_retries=1, progress=progress,
            )
        self.assertEqual(len(calls), 4)  # detail + first page + short page + replacement page
        self.assertEqual(first["unique_user_count"], 2)
        self.assertEqual(second["unique_user_count"], 3)
        self.assertEqual(logger.record.call_args_list[0].args, ("manbo", "600", "1", "第一集", 3, False))
        self.assertEqual(logger.record.call_args_list[1].args, ("manbo", "600", "1", "第一集", 4, True))

    def test_manbo_missing_page_is_incomplete_even_when_no_rows_return(self):
        logger = Mock()
        with (
            patch.object(ranks, "DANMAKU_DETAIL_LOG", logger),
            patch.object(ranks, "_request_manbo_danmaku_page", side_effect=RuntimeError("timeout")),
        ):
            result = ranks.fetch_manbo_paid_danmaku_benchmark(
                "600", paid_set_id_loader=lambda *_a, **_k: ["1"],
            )
        self.assertEqual(result["failed_page_count"], 1)
        logger.record.assert_called_once_with("manbo", "600", "1", "", None, False)


if __name__ == "__main__":
    unittest.main()
