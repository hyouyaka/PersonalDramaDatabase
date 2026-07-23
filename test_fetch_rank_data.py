import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

import fetch_rank_data


class RankFetch418CheckpointTests(unittest.TestCase):
    def _progress(self, *, missevan_pending=None, manbo_pending=None):
        return {
            "missevan": {
                "target_ids": ["1", "2"],
                "pending_ids": list(missevan_pending or []),
                "danmaku_ids": ["1", "2"],
                "deferred_danmaku_ids": [],
            },
            "manbo": {
                "target_ids": ["m1"],
                "pending_ids": list(manbo_pending or []),
                "danmaku_ids": ["m1"],
            },
        }

    def _store(self):
        return {
            "_meta": {},
            "missevan": {"ranks": {}, "dramas": {}},
            "manbo": {"ranks": {}, "dramas": {}},
        }

    def test_checkpoint_expiry_is_anchored_to_first_418(self) -> None:
        first = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        initial = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
            now=first,
        )
        repeated = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
            first_rate_limited_at=initial["first_rate_limited_at"],
            now=first + timedelta(hours=2),
        )

        self.assertEqual(repeated["first_rate_limited_at"], initial["first_rate_limited_at"])
        self.assertEqual(repeated["expires_at"], initial["expires_at"])

    def test_expired_checkpoint_is_deleted(self) -> None:
        first = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
            now=first,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(path, payload)
            loaded = fetch_rank_data.load_418_checkpoint(
                path,
                resume_hours=3,
                expected_platforms=("missevan",),
                skip_danmaku=False,
                now=first + timedelta(hours=3, seconds=1),
            )

            self.assertIsNone(loaded)
            self.assertFalse(path.exists())

    def test_incompatible_or_malformed_checkpoint_is_deleted(self) -> None:
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(path, payload)
            self.assertIsNone(fetch_rank_data.load_418_checkpoint(
                path,
                resume_hours=3,
                expected_platforms=("missevan",),
                skip_danmaku=True,
            ))
            self.assertFalse(path.exists())

            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(fetch_rank_data.load_418_checkpoint(
                path,
                resume_hours=3,
                expected_platforms=("missevan",),
                skip_danmaku=False,
            ))
            self.assertFalse(path.exists())

    def test_418_saves_only_pending_ids_after_parallel_manbo_finishes(self) -> None:
        store = self._store()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"

            def fake_missevan(_requester, _ids, _store, **kwargs):
                kwargs["pending_ids"].discard("1")
                raise RuntimeError("HTTP_418")

            def fake_manbo(_ids, _store, **kwargs):
                kwargs["pending_ids"].clear()

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--resume-418-hours", "3"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
                patch.object(fetch_rank_data, "fetch_missevan_ranks", return_value=({"1", "2"}, {"1", "2"}, set())),
                patch.object(fetch_rank_data, "fetch_manbo_ranks", return_value={"m1"}),
                patch.object(fetch_rank_data, "load_ongoing_drama_ids", return_value=set()),
                patch.object(fetch_rank_data, "collect_manbo_danmaku_target_ids", return_value={"m1"}),
                patch.object(fetch_rank_data, "fetch_missevan_drama_details", side_effect=fake_missevan),
                patch.object(fetch_rank_data, "fetch_manbo_drama_details", side_effect=fake_manbo),
                patch.object(fetch_rank_data, "save_json"),
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
                    fetch_rank_data.main()

            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["progress"]["missevan"]["pending_ids"], ["2"])
            self.assertEqual(payload["progress"]["manbo"]["pending_ids"], [])

    def test_rejected_drama_is_removed_from_rank_before_later_418(self) -> None:
        store = self._store()
        store["missevan"]["ranks"] = {
            "new_daily": {"items": ["1", "2"]},
        }
        pending = {"1", "2"}
        with (
            patch.object(
                fetch_rank_data,
                "_fetch_one_missevan",
                side_effect=[fetch_rank_data.RejectedDramaRecord("non-target"), RuntimeError("HTTP_418")],
            ),
            patch.object(fetch_rank_data, "save_json"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
                fetch_rank_data.fetch_missevan_drama_details(
                    Mock(),
                    {"1", "2"},
                    store,
                    skip_danmaku=False,
                    danmaku_ids={"1", "2"},
                    pending_ids=pending,
                )

        self.assertEqual(store["missevan"]["ranks"]["new_daily"]["items"], ["2"])
        self.assertEqual(pending, {"2"})

    def test_resume_with_pending_failures_keeps_checkpoint_and_skips_publish(self) -> None:
        store = self._store()
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            store,
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--missevan-only", "--resume-418-hours", "3"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "fetch_missevan_drama_details"),
                patch.object(fetch_rank_data, "save_json"),
                patch.object(fetch_rank_data, "upload_rank_outputs") as upload,
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery remains incomplete"):
                    fetch_rank_data.main()

            upload.assert_not_called()
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["progress"]["missevan"]["pending_ids"], ["2"])

    def test_resume_skips_remote_and_rank_fetch_then_deletes_checkpoint_after_publish(self) -> None:
        store = self._store()
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            store,
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)

            def fake_details(_requester, ids, _store, **kwargs):
                self.assertEqual(ids, {"2"})
                kwargs["pending_ids"].clear()

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--missevan-only", "--resume-418-hours", "3"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "load_initial_rank_store", side_effect=AssertionError("remote must not load")),
                patch.object(fetch_rank_data, "fetch_missevan_ranks", side_effect=AssertionError("ranks must not refetch")),
                patch.object(fetch_rank_data, "fetch_missevan_drama_details", side_effect=fake_details),
                patch.object(fetch_rank_data, "lookup_cvs"),
                patch.object(fetch_rank_data, "save_json"),
                patch.object(fetch_rank_data, "upload_rank_outputs") as upload,
                patch("builtins.print"),
            ):
                fetch_rank_data.main()

            upload.assert_called_once()
            self.assertEqual(upload.call_args.args[1], ("missevan",))
            self.assertIn("updated_at", upload.call_args.args[0]["_meta"])
            self.assertFalse(checkpoint_path.exists())

    def test_successful_non_resume_run_discards_incompatible_checkpoint(self) -> None:
        store = self._store()
        invocation = fetch_rank_data.checkpoint_invocation(("missevan", "manbo"), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            store,
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--missevan-only"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
                patch.object(fetch_rank_data, "fetch_missevan_ranks", return_value=(set(), set(), set())),
                patch.object(fetch_rank_data, "load_ongoing_drama_ids", return_value=set()),
                patch.object(fetch_rank_data, "lookup_cvs"),
                patch.object(fetch_rank_data, "save_json"),
                patch.object(fetch_rank_data, "upload_rank_outputs") as upload,
                patch("builtins.print"),
            ):
                fetch_rank_data.main()

            upload.assert_called_once()
            self.assertFalse(checkpoint_path.exists())

    def test_successful_only_danmaku_run_discards_checkpoint(self) -> None:
        store = self._store()
        invocation = fetch_rank_data.checkpoint_invocation(("missevan", "manbo"), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            store,
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--only-danmaku", "--force"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
                patch.object(fetch_rank_data, "only_danmaku_mode"),
                patch.object(fetch_rank_data, "save_json"),
                patch.object(fetch_rank_data, "upload_rank_outputs") as upload,
                patch("builtins.print"),
            ):
                fetch_rank_data.main()

            upload.assert_called_once()
            self.assertFalse(checkpoint_path.exists())

    def test_noop_null_danmaku_repair_keeps_checkpoint(self) -> None:
        invocation = fetch_rank_data.checkpoint_invocation(("missevan", "manbo"), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        results = {
            "missevan": {"targets": [], "repaired": {}, "failed": []},
            "manbo": {"targets": ["m1"], "repaired": {}, "failed": ["m1"]},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)
            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--repair-null-danmaku"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "repair_null_danmaku_mode", return_value=results),
                patch("builtins.print"),
            ):
                fetch_rank_data.main()

            self.assertTrue(checkpoint_path.exists())

    def test_successful_null_danmaku_repair_discards_checkpoint(self) -> None:
        invocation = fetch_rank_data.checkpoint_invocation(("missevan", "manbo"), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            self._store(),
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        results = {
            "missevan": {"targets": ["2"], "repaired": {"2": 7}, "failed": []},
            "manbo": {"targets": [], "repaired": {}, "failed": []},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)
            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--repair-null-danmaku"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "repair_null_danmaku_mode", return_value=results),
                patch("builtins.print"),
            ):
                fetch_rank_data.main()

            self.assertFalse(checkpoint_path.exists())

    def test_publish_failure_keeps_checkpoint_with_empty_pending(self) -> None:
        store = self._store()
        invocation = fetch_rank_data.checkpoint_invocation(("missevan",), skip_danmaku=False, force=True)
        payload = fetch_rank_data.build_418_checkpoint(
            store,
            self._progress(missevan_pending=["2"]),
            invocation,
            resume_hours=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoint.json"
            fetch_rank_data.save_418_checkpoint_atomic(checkpoint_path, payload)

            def fake_details(_requester, _ids, _store, **kwargs):
                kwargs["pending_ids"].clear()

            with (
                patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--missevan-only", "--resume-418-hours", "3"]),
                patch.object(fetch_rank_data, "RANK_FETCH_418_CHECKPOINT_PATH", checkpoint_path),
                patch.object(fetch_rank_data, "fetch_missevan_drama_details", side_effect=fake_details),
                patch.object(fetch_rank_data, "lookup_cvs"),
                patch.object(fetch_rank_data, "save_json"),
                patch.object(fetch_rank_data, "upload_rank_outputs", side_effect=RuntimeError("publish failed")),
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(RuntimeError, "publish failed"):
                    fetch_rank_data.main()

            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["progress"]["missevan"]["pending_ids"], [])


class RankFullStoreKeyTests(unittest.TestCase):
    def test_upload_full_ranks_writes_latest_key_only(self) -> None:
        store = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}
        remote: dict[str, str] = {"ranks:meta": "{}"}

        def fake_request(command):
            if command[0] == "GET":
                return remote.get(command[1])
            if command[:2] == ["EVAL", fetch_rank_data.publish_rank_string.__globals__["RANK_STRING_PUBLISH_SCRIPT"]]:
                remote[command[3]] = command[5]
                remote[command[4]] = command[6]
                return 1
            raise AssertionError(command)

        with (
            patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request) as request,
            patch("builtins.print"),
        ):
            fetch_rank_data.upload_full_ranks(store)

        command = next(
            call.args[0]
            for call in request.call_args_list
            if call.args[0][0] == "EVAL"
        )
        self.assertEqual(command[0], "EVAL")
        self.assertEqual(command[3:5], ["ranks:latest", "ranks:meta"])
        self.assertEqual(json.loads(command[5]), store)

    def test_load_remote_full_ranks_reads_latest_key_only(self) -> None:
        payload = {"_meta": {"updated_at": "2026-05-08T00:33:19+00:00"}}

        with patch.object(fetch_rank_data, "upstash_request", return_value=json.dumps(payload)) as request:
            self.assertEqual(fetch_rank_data.load_remote_full_ranks(), payload)

        request.assert_called_once_with(["GET", "ranks:latest"])

    def test_load_initial_rank_store_prefers_complete_latest_snapshot(self) -> None:
        remote = {
            "_meta": {"updated_at": "2026-05-08T00:33:19+00:00"},
            "missevan": {"ranks": {"new_daily": {}}, "dramas": {"1": {"name": "猫耳"}}},
            "manbo": {"ranks": {"hot": {}}, "dramas": {"2": {"name": "漫播"}}},
        }

        with (
            patch.object(fetch_rank_data, "load_remote_full_ranks", return_value=remote) as load_remote,
            patch.object(fetch_rank_data, "load_json", side_effect=AssertionError("local fallback should not load")),
            patch("builtins.print"),
        ):
            loaded = fetch_rank_data.load_initial_rank_store()

        load_remote.assert_called_once_with()
        self.assertEqual(loaded["missevan"]["dramas"]["1"]["name"], "猫耳")
        self.assertEqual(loaded["manbo"]["dramas"]["2"]["name"], "漫播")

    def test_upload_rank_outputs_writes_only_aggregates_and_latest(self) -> None:
        store = {
            "_meta": {"updated_at": "2026-05-16T00:00:00+00:00"},
            "missevan": {
                "ranks": {"new_daily": {"name": "新品日榜", "items": [{"dramaId": "1"}]}},
                "dramas": {"1": {"name": "猫耳", "view_count": 10}},
            },
            "manbo": {"ranks": {}, "dramas": {}},
        }
        with (
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-16T00:00:00+00:00"),
            patch.object(fetch_rank_data, "upload_missevan_peak_trend") as peak,
            patch.object(fetch_rank_data, "upload_rank_trend_snapshot") as trend,
            patch.object(fetch_rank_data, "upload_full_ranks") as latest,
            patch.object(fetch_rank_data, "run_cleanup_best_effort") as cleanup,
            patch("builtins.print"),
        ):
            result = fetch_rank_data.upload_rank_outputs(store, ("missevan",))

        peak.assert_called_once()
        trend.assert_called_once()
        latest.assert_called_once_with(store)
        cleanup.assert_called_once()
        self.assertEqual(result, store)


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

    def test_build_rank_trend_payload_retains_latest_ninety_dates(self) -> None:
        old_dates = [f"2026-{month:02d}-{day:02d}" for month in range(1, 4) for day in range(1, 32)][:90]
        current = {
            "version": 1,
            "platform": "missevan",
            "dates": old_dates,
            "dramas": {
                "93038": {
                    "id": "93038",
                    "name": "一屋暗灯 全一季",
                    "samples": {
                        date: {"metrics": {"view_count": index}, "ranks": []}
                        for index, date in enumerate(old_dates)
                    },
                }
            },
        }

        payload = fetch_rank_data.build_rank_trend_payload(
            current,
            "missevan",
            "2026-04-01",
            self._metrics_payload("2026-04-01"),
            self._list_payload("2026-04-01"),
            generated_at="2026-04-01T00:00:00+00:00",
        )

        self.assertEqual(len(payload["dates"]), 90)
        self.assertNotIn(old_dates[0], payload["dates"])
        self.assertIn("2026-04-01", payload["dates"])
        self.assertNotIn(old_dates[0], payload["dramas"]["93038"]["samples"])

    def test_build_peak_trend_payload_retains_latest_ninety_dates(self) -> None:
        old_dates = [f"2026-{month:02d}-{day:02d}" for month in range(1, 4) for day in range(1, 32)][:90]
        current = {
            "version": 1,
            "platform": "missevan",
            "rank": "peak",
            "dates": old_dates,
            "series": {
                "系列剧": {
                    "name": "系列剧",
                    "samples": {
                        date: {"view_count": index, "position": 1}
                        for index, date in enumerate(old_dates)
                    },
                }
            },
        }
        store = {
            "missevan": {
                "ranks": {
                    "peak": {
                        "items": [{"name": "系列剧", "dramaIds": ["1"], "view_count": 999}],
                    }
                }
            }
        }

        payload = fetch_rank_data.build_missevan_peak_trend_payload(
            current,
            store,
            "2026-04-01",
            "2026-04-01T00:00:00+00:00",
            pruned_dates=(),
        )

        self.assertEqual(len(payload["dates"]), 90)
        self.assertNotIn(old_dates[0], payload["dates"])
        self.assertIn("2026-04-01", payload["dates"])
        self.assertNotIn(old_dates[0], payload["series"]["系列剧"]["samples"])


class RankTrendBackfillTests(unittest.TestCase):
    def test_upload_rank_trend_snapshot_does_not_overwrite_when_current_read_fails(self) -> None:
        commands: list[list[object]] = []

        def fake_request(command: list[object]) -> object:
            commands.append(command)
            if command[:2] == ["EXISTS", "ranks:trend:missevan"]:
                return 1
            if command[:2] == ["GET", "ranks:trend:missevan"]:
                raise RuntimeError("temporary read failure")
            if command[0] == "SET":
                raise AssertionError("trend should not be overwritten after read failure")
            raise AssertionError(command)

        with (
            patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request),
            patch.object(fetch_rank_data, "publish_trend_v2_best_effort"),
        ):
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

        self.assertEqual(
            commands,
            [["EXISTS", "ranks:trend:missevan"], ["GET", "ranks:trend:missevan"]],
        )

    def test_upload_rank_outputs_fails_when_trend_read_fails(self) -> None:
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
            if command[:2] == ["EXISTS", "ranks:trend:manbo"]:
                return 1
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
            patch.object(fetch_rank_data, "publish_trend_v2_best_effort"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary trend read failure"):
                fetch_rank_data.upload_rank_outputs(store, ("manbo",))

        self.assertNotIn("ranks:latest", written)


class MissevanRankLimitTests(unittest.TestCase):
    def test_popular_and_bestseller_fetch_50_but_only_top_30_need_danmaku(self) -> None:
        requested_urls: list[str] = []

        class FakeRequester:
            def request_json(self, url: str) -> dict:
                requested_urls.append(url)
                if "/x/rank/peak-details" in url:
                    return {"data": {"data": []}}
                query = parse_qs(urlparse(url).query)
                prefix = f"{query['type'][0]}-{query['sub_type'][0]}"
                return {"info": {"data": [{"id": f"{prefix}-{idx}"} for idx in range(1, 61)]}}

        store = {"missevan": {"ranks": {}, "dramas": {}}}
        with patch.object(fetch_rank_data, "load_series_info", return_value={}), patch("builtins.print"):
            all_ids, danmaku_ids, deferred_ids = fetch_rank_data.fetch_missevan_ranks(FakeRequester(), store)

        self.assertEqual(len(store["missevan"]["ranks"]["new_daily"]["items"]), 30)
        self.assertEqual(len(store["missevan"]["ranks"]["new_weekly"]["items"]), 30)
        for key in ("popular_weekly", "popular_monthly", "bestseller_weekly", "bestseller_monthly"):
            self.assertEqual(len(store["missevan"]["ranks"][key]["items"]), 50)
        standard_urls = [url for url in requested_urls if "/rank/details" in url]
        self.assertEqual([parse_qs(urlparse(url).query)["page_size"][0] for url in standard_urls], ["30", "30", "50", "50", "50", "50"])
        self.assertEqual(len(all_ids), 260)
        self.assertEqual(len(danmaku_ids), 180)
        self.assertEqual(len(deferred_ids), 80)
        self.assertIn("2-2-30", danmaku_ids)
        self.assertNotIn("2-2-31", danmaku_ids)
        self.assertIn("2-2-31", deferred_ids)

    def test_danmaku_cutoff_uses_original_api_position(self) -> None:
        class FakeRequester:
            def request_json(self, url: str) -> dict:
                if "/x/rank/peak-details" in url:
                    return {"data": {"data": []}}
                rows = [{"name": "missing id"}]
                rows.extend({"id": str(idx)} for idx in range(2, 51))
                return {"info": {"data": rows}}

        store = {"missevan": {"ranks": {}, "dramas": {}}}
        only_popular_weekly = {"popular_weekly": (2, 2, "人气周榜", 50, 30)}
        with (
            patch.object(fetch_rank_data, "MISSEVAN_RANKS", only_popular_weekly),
            patch.object(fetch_rank_data, "load_series_info", return_value={}),
            patch("builtins.print"),
        ):
            _all_ids, danmaku_ids, deferred_ids = fetch_rank_data.fetch_missevan_ranks(FakeRequester(), store)

        self.assertIn("30", danmaku_ids)
        self.assertNotIn("31", danmaku_ids)
        self.assertIn("31", deferred_ids)

    def test_top_30_and_ongoing_membership_override_deferred_membership(self) -> None:
        eligible, deferred = fetch_rank_data.classify_missevan_danmaku_ids(
            {"top", "overlap"},
            {"overlap", "ongoing", "bottom"},
            {"ongoing"},
        )

        self.assertEqual(eligible, {"top", "overlap", "ongoing"})
        self.assertEqual(deferred, {"bottom"})

    def test_marker_is_terminal_but_promotion_forces_detail_refresh(self) -> None:
        store = {
            "missevan": {
                "dramas": {
                    "bottom": {"danmaku_uid_count": 99, "fetched_at": "fresh"},
                    "promoted": {
                        "danmaku_uid_count": fetch_rank_data.MISSEVAN_DANMAKU_NOT_REQUIRED,
                        "fetched_at": "fresh",
                    },
                    "cached": {"danmaku_uid_count": 20, "fetched_at": "fresh"},
                }
            }
        }
        fetch_rank_data.apply_missevan_danmaku_not_required(store, {"bottom"})

        bottom = store["missevan"]["dramas"]["bottom"]
        self.assertEqual(bottom["danmaku_uid_count"], fetch_rank_data.MISSEVAN_DANMAKU_NOT_REQUIRED)
        self.assertFalse(fetch_rank_data.should_refresh_only_danmaku_entry(bottom, force=True))
        self.assertFalse(fetch_rank_data._entry_has_empty_danmaku(bottom))

        with patch.object(fetch_rank_data, "is_stale", return_value=False):
            selected, skipped = fetch_rank_data.select_missevan_detail_ids(
                {"promoted", "cached"},
                store["missevan"]["dramas"],
                {"promoted", "cached"},
                force=False,
            )

        self.assertEqual(selected, {"promoted"})
        self.assertEqual(skipped, 1)

    def test_marker_flows_into_metrics_and_trend(self) -> None:
        marker = fetch_rank_data.MISSEVAN_DANMAKU_NOT_REQUIRED
        store = {
            "missevan": {
                "ranks": {"popular_weekly": {"name": "人气周榜", "items": ["bottom"]}},
                "dramas": {"bottom": {"name": "第31名", "danmaku_uid_count": marker}},
            }
        }
        generated_at = "2026-07-16T00:00:00+00:00"
        metrics = fetch_rank_data._build_metric_payload(store, "missevan", "2026-07-16", generated_at)
        lists = fetch_rank_data._build_rank_list_payload(store, "missevan", "2026-07-16", generated_at)
        trend = fetch_rank_data.build_rank_trend_payload(
            None,
            "missevan",
            "2026-07-16",
            metrics,
            lists,
            generated_at=generated_at,
        )

        self.assertEqual(metrics["dramas"]["bottom"]["danmaku_uid_count"], marker)
        self.assertEqual(trend["dramas"]["bottom"]["samples"]["2026-07-16"]["metrics"]["danmaku_uid_count"], marker)

    def test_skip_danmaku_run_reapplies_marker_before_publish(self) -> None:
        marker = fetch_rank_data.MISSEVAN_DANMAKU_NOT_REQUIRED
        store = {
            "_meta": {},
            "missevan": {
                "ranks": {},
                "dramas": {"bottom": {"name": "第31名", "danmaku_uid_count": 99}},
            },
            "manbo": {"ranks": {}, "dramas": {}},
        }

        def fake_details(_requester, _ids, target_store, **_kwargs) -> None:
            target_store["missevan"]["dramas"]["bottom"]["danmaku_uid_count"] = None

        with (
            patch.object(sys, "argv", ["fetch_rank_data.py", "--missevan-only", "--skip-danmaku", "--force"]),
            patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
            patch.object(fetch_rank_data, "MissevanRequester"),
            patch.object(fetch_rank_data, "fetch_missevan_ranks", return_value=({"bottom"}, set(), {"bottom"})),
            patch.object(fetch_rank_data, "load_ongoing_drama_ids", return_value=set()),
            patch.object(fetch_rank_data, "fetch_missevan_drama_details", side_effect=fake_details),
            patch.object(fetch_rank_data, "lookup_cvs"),
            patch.object(fetch_rank_data, "save_json"),
            patch.object(fetch_rank_data, "upload_rank_outputs") as upload,
            patch("builtins.print"),
        ):
            fetch_rank_data.main()

        self.assertEqual(store["missevan"]["dramas"]["bottom"]["danmaku_uid_count"], marker)
        upload.assert_called_once_with(store, ("missevan",))

    def test_null_repair_ignores_marker_in_latest_and_trend(self) -> None:
        marker = fetch_rank_data.MISSEVAN_DANMAKU_NOT_REQUIRED
        responses = {
            "ranks:latest": {
                "missevan": {"ranks": {}, "dramas": {"bottom": {"danmaku_uid_count": marker}}},
                "manbo": {"ranks": {}, "dramas": {}},
            },
            "ranks:trend:missevan": {
                "dates": ["2026-07-16"],
                "dramas": {
                    "bottom": {
                        "samples": {
                            "2026-07-16": {"metrics": {"danmaku_uid_count": marker}, "ranks": []}
                        }
                    }
                },
            },
        }

        with (
            patch.object(
                fetch_rank_data,
                "_load_upstash_json_strict",
                side_effect=lambda key: responses[key],
            ),
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:missevan"],
            ),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers(
                "missevan",
                "2026-07-16",
            )

        self.assertEqual(targets, set())
        self.assertEqual(sources, {})


class RankTrendCliTests(unittest.TestCase):
    def test_history_backfill_cli_is_removed(self) -> None:
        with patch.object(sys, "argv", ["fetch_rank_data.py", "--backfill-rank-trends-from-history"]):
            with self.assertRaises(SystemExit):
                fetch_rank_data.main()

    def test_repair_date_cli_is_removed(self) -> None:
        with patch.object(sys, "argv", ["fetch_rank_data.py", "--repair-null-danmaku", "--date", "2026-05-28"]):
            with self.assertRaises(SystemExit):
                fetch_rank_data.main()

    def test_refresh_cli_fails_when_rank_upload_fails(self) -> None:
        store = {
            "_meta": {},
            "missevan": {"ranks": {}, "dramas": {}},
            "manbo": {"ranks": {}, "dramas": {}},
        }

        with (
            patch.object(sys, "argv", ["fetch_rank_data.py", "--force", "--missevan-only"]),
            patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
            patch.object(fetch_rank_data, "MissevanRequester"),
            patch.object(fetch_rank_data, "fetch_missevan_ranks", return_value=(set(), set(), set())),
            patch.object(fetch_rank_data, "load_ongoing_drama_ids", return_value=set()),
            patch.object(fetch_rank_data, "lookup_cvs"),
            patch.object(fetch_rank_data, "save_json"),
            patch.object(fetch_rank_data, "upload_rank_outputs", side_effect=RuntimeError("publish failed")),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "publish failed"):
                fetch_rank_data.main()

    def test_only_danmaku_cli_fails_when_rank_upload_fails(self) -> None:
        store = {
            "_meta": {},
            "missevan": {"ranks": {}, "dramas": {}},
            "manbo": {"ranks": {}, "dramas": {}},
        }

        with (
            patch.object(sys, "argv", ["fetch_rank_data.py", "--only-danmaku", "--missevan-only"]),
            patch.object(fetch_rank_data, "load_initial_rank_store", return_value=store),
            patch.object(fetch_rank_data, "only_danmaku_mode"),
            patch.object(fetch_rank_data, "save_json"),
            patch.object(fetch_rank_data, "upload_rank_outputs", side_effect=RuntimeError("publish failed")),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "publish failed"):
                fetch_rank_data.main()


class NullDanmakuRepairTests(unittest.TestCase):
    def test_resolve_repair_history_date_prefers_latest_trend_date(self) -> None:
        with (
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value={"dates": ["2026-05-27", "2026-05-28"]},
            ) as load,
            patch.object(fetch_rank_data, "_load_upstash_json", side_effect=AssertionError("latest fallback not needed")),
        ):
            result = fetch_rank_data.resolve_repair_history_date("missevan")

        self.assertEqual(result, "2026-05-28")
        load.assert_called_once_with("missevan")

    def test_resolve_repair_history_date_falls_back_to_latest_timestamp(self) -> None:
        latest = {"_meta": {"updated_at": "2026-05-28T23:30:00+00:00"}}
        with (
            patch.object(fetch_rank_data, "_load_normal_trend_v2_strict", return_value={"dates": []}),
            patch.object(fetch_rank_data, "_load_upstash_json", return_value=latest) as load_latest,
        ):
            result = fetch_rank_data.resolve_repair_history_date("manbo")

        self.assertEqual(result, fetch_rank_data._history_date_from_store_meta(latest)[0])
        load_latest.assert_called_once_with("ranks:latest")

    def test_collect_repair_ids_reads_latest_and_trend_only(self) -> None:
        responses = {
            "ranks:latest": {
                "_meta": {"updated_at": "2026-05-28T00:00:00+00:00"},
                "missevan": {
                    "ranks": {},
                    "dramas": {"latest-null": {"name": "latest", "danmaku_uid_count": None}},
                },
                "manbo": {"ranks": {}, "dramas": {}},
            },
            "ranks:trend:missevan": {
                "dates": ["2026-05-28"],
                "dramas": {
                    "trend-null": {
                        "name": "trend",
                        "samples": {"2026-05-28": {"metrics": {"danmaku_uid_count": None}, "ranks": []}},
                    }
                },
            },
        }

        with (
            patch.object(
                fetch_rank_data,
                "_load_upstash_json_strict",
                side_effect=lambda key: responses[key],
            ) as load,
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:missevan"],
            ),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers(
                "missevan",
                "2026-05-28",
            )

        self.assertEqual(targets, {"latest-null", "trend-null"})
        self.assertEqual(sources, {"latest-null": ["latest"], "trend-null": ["trend"]})
        self.assertEqual(
            [call.args[0] for call in load.call_args_list],
            ["ranks:latest"],
        )

    def test_write_repaired_layers_updates_latest_and_trend_only(self) -> None:
        payloads = {
            "latest": {
                "_meta": {"updated_at": "old"},
                "missevan": {"ranks": {}, "dramas": {"1": {"name": "剧", "danmaku_uid_count": None}}},
                "manbo": {"ranks": {}, "dramas": {}},
            },
            "trend": {
                "version": 1,
                "platform": "missevan",
                "dates": ["2026-05-28"],
                "dramas": {
                    "1": {
                        "id": "1",
                        "name": "剧",
                        "samples": {"2026-05-28": {"metrics": {"danmaku_uid_count": None}, "ranks": []}},
                    }
                },
            },
        }

        with (
            patch.object(fetch_rank_data, "upstash_request", return_value=0),
            patch.object(fetch_rank_data, "publish_rank_string") as publish_latest,
            patch.object(fetch_rank_data, "publish_normal_trend_v2") as publish_trend,
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-28T12:00:00+00:00"),
            patch("builtins.print"),
        ):
            fetch_rank_data.write_repaired_danmaku_layers(
                "missevan",
                "2026-05-28",
                {"1": 42},
                payloads,
            )

        latest_written = publish_latest.call_args.args[1]
        trend_written = publish_trend.call_args.args[1]
        self.assertEqual(latest_written["missevan"]["dramas"]["1"]["danmaku_uid_count"], 42)
        self.assertEqual(
            trend_written["dramas"]["1"]["samples"]["2026-05-28"]["metrics"][
                "danmaku_uid_count"
            ],
            42,
        )

    def test_is_empty_danmaku_value_preserves_zero(self) -> None:
        self.assertTrue(fetch_rank_data.is_empty_danmaku_value(None))
        self.assertTrue(fetch_rank_data.is_empty_danmaku_value(""))
        self.assertTrue(fetch_rank_data.is_empty_danmaku_value("   "))
        self.assertFalse(fetch_rank_data.is_empty_danmaku_value(0))
        self.assertFalse(fetch_rank_data.is_empty_danmaku_value("0"))
        self.assertFalse(fetch_rank_data.is_empty_danmaku_value(12))

    def test_collect_repair_ids_from_metrics_partial_latest_and_trend(self) -> None:
        responses = {
            "ranks:metrics:2026-05-28:missevan": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "missevan",
                "generated_at": "2026-05-28T00:00:00+00:00",
                "dramas": {
                    "metrics-null": {"name": "metrics", "danmaku_uid_count": None},
                    "ok": {"name": "ok", "danmaku_uid_count": 0},
                },
            },
            "ranks:partial:missevan": {
                "version": 1,
                "platform": "missevan",
                "data": {"ranks": {}, "dramas": {"partial-empty": {"name": "partial", "danmaku_uid_count": ""}}},
            },
            "ranks:latest": {
                "version": 1,
                "missevan": {"ranks": {}, "dramas": {"latest-missing": {"name": "latest"}}},
                "manbo": {"ranks": {}, "dramas": {}},
            },
            "ranks:trend:missevan": {
                "version": 1,
                "platform": "missevan",
                "dates": ["2026-05-28"],
                "dramas": {
                    "trend-null": {
                        "id": "trend-null",
                        "name": "trend",
                        "samples": {"2026-05-28": {"metrics": {"danmaku_uid_count": None}, "ranks": []}},
                    }
                },
            },
        }

        with (
            patch.object(fetch_rank_data, "_load_upstash_json_strict", side_effect=lambda key: responses.get(key)),
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:missevan"],
            ),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers("missevan", "2026-05-28")

        self.assertEqual(targets, {"latest-missing", "trend-null"})
        self.assertEqual(sources["latest-missing"], ["latest"])
        self.assertEqual(sources["trend-null"], ["trend"])
        self.assertNotIn("ok", targets)

    def test_collect_repair_ids_includes_trend_null_when_latest_is_zero(self) -> None:
        responses = {
            "ranks:metrics:2026-05-28:missevan": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "missevan",
                "generated_at": "2026-05-28T00:00:00+00:00",
                "dramas": {"93038": {"name": "剧目", "danmaku_uid_count": 0}},
            },
            "ranks:partial:missevan": {
                "version": 1,
                "platform": "missevan",
                "data": {"ranks": {}, "dramas": {"93038": {"name": "剧目", "danmaku_uid_count": 0}}},
            },
            "ranks:latest": {
                "version": 1,
                "missevan": {"ranks": {}, "dramas": {"93038": {"name": "剧目", "danmaku_uid_count": 0}}},
                "manbo": {"ranks": {}, "dramas": {}},
            },
            "ranks:trend:missevan": {
                "version": 1,
                "platform": "missevan",
                "dates": ["2026-05-28"],
                "dramas": {
                    "93038": {
                        "id": "93038",
                        "name": "剧目",
                        "samples": {"2026-05-28": {"metrics": {"danmaku_uid_count": None}, "ranks": []}},
                    }
                },
            },
        }

        with (
            patch.object(fetch_rank_data, "_load_upstash_json_strict", side_effect=lambda key: responses.get(key)),
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:missevan"],
            ),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers("missevan", "2026-05-28")

        self.assertEqual(targets, {"93038"})
        self.assertEqual(sources["93038"], ["trend"])

    def test_collect_repair_ids_skips_manbo_peak_only_entries(self) -> None:
        responses = {
            "ranks:metrics:2026-05-28:manbo": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "manbo",
                "generated_at": "2026-05-28T00:00:00+00:00",
                "dramas": {
                    "peak-only": {"name": "只在巅峰榜", "danmaku_uid_count": None},
                    "hot-only": {"name": "热播榜", "danmaku_uid_count": None},
                    "peak-and-hot": {"name": "两个榜都有", "danmaku_uid_count": None},
                },
            },
            "ranks:partial:manbo": {
                "version": 1,
                "platform": "manbo",
                "data": {
                    "ranks": {
                        "peak": {"name": "巅峰榜", "items": [{"dramaId": "peak-only"}, {"dramaId": "peak-and-hot"}]},
                        "hot": {"name": "热播榜", "items": [{"dramaId": "hot-only"}, {"dramaId": "peak-and-hot"}]},
                    },
                    "dramas": {
                        "peak-only": {"name": "只在巅峰榜", "danmaku_uid_count": None},
                        "hot-only": {"name": "热播榜", "danmaku_uid_count": None},
                        "peak-and-hot": {"name": "两个榜都有", "danmaku_uid_count": None},
                    },
                },
            },
            "ranks:latest": {
                "version": 1,
                "missevan": {"ranks": {}, "dramas": {}},
                "manbo": {
                    "ranks": {
                        "peak": {"name": "巅峰榜", "items": [{"dramaId": "peak-only"}, {"dramaId": "peak-and-hot"}]},
                        "hot": {"name": "热播榜", "items": [{"dramaId": "hot-only"}, {"dramaId": "peak-and-hot"}]},
                    },
                    "dramas": {},
                },
            },
            "ranks:trend:manbo": {
                "version": 1,
                "platform": "manbo",
                "dates": ["2026-05-28"],
                "dramas": {
                    "peak-only": {
                        "id": "peak-only",
                        "name": "只在巅峰榜",
                        "samples": {
                            "2026-05-28": {
                                "metrics": {"danmaku_uid_count": None},
                                "ranks": [{"key": "peak", "name": "巅峰榜", "position": 1}],
                            }
                        },
                    },
                    "hot-only": {
                        "id": "hot-only",
                        "name": "热播榜",
                        "samples": {
                            "2026-05-28": {
                                "metrics": {"danmaku_uid_count": None},
                                "ranks": [{"key": "hot", "name": "热播榜", "position": 1}],
                            }
                        },
                    },
                    "peak-and-hot": {
                        "id": "peak-and-hot",
                        "name": "两个榜都有",
                        "samples": {
                            "2026-05-28": {
                                "metrics": {"danmaku_uid_count": None},
                                "ranks": [
                                    {"key": "peak", "name": "巅峰榜", "position": 2},
                                    {"key": "hot", "name": "热播榜", "position": 2},
                                ],
                            }
                        },
                    },
                },
            },
        }

        with (
            patch.object(fetch_rank_data, "_load_upstash_json_strict", side_effect=lambda key: responses.get(key)),
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:manbo"],
            ),
            patch.object(fetch_rank_data, "_load_upstash_json", side_effect=lambda key: responses.get(key)),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers("manbo", "2026-05-28")

        self.assertEqual(targets, {"hot-only", "peak-and-hot"})
        self.assertNotIn("peak-only", sources)

    def test_collect_repair_ids_keeps_manbo_peak_only_when_ongoing(self) -> None:
        responses = {
            "ranks:metrics:2026-05-28:manbo": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "manbo",
                "dramas": {"peak-ongoing": {"name": "巅峰榜更新剧", "danmaku_uid_count": None}},
            },
            "ranks:partial:manbo": {
                "version": 1,
                "platform": "manbo",
                "data": {
                    "ranks": {"peak": {"name": "巅峰榜", "items": [{"dramaId": "peak-ongoing"}]}},
                    "dramas": {"peak-ongoing": {"name": "巅峰榜更新剧", "danmaku_uid_count": None}},
                },
            },
            "ranks:latest": {
                "version": 1,
                "missevan": {"ranks": {}, "dramas": {}},
                "manbo": {
                    "ranks": {"peak": {"name": "巅峰榜", "items": [{"dramaId": "peak-ongoing"}]}},
                    "dramas": {"peak-ongoing": {"name": "巅峰榜更新剧", "danmaku_uid_count": None}},
                },
            },
            "ranks:trend:manbo": {
                "version": 1,
                "platform": "manbo",
                "dates": ["2026-05-28"],
                "dramas": {
                    "peak-ongoing": {
                        "id": "peak-ongoing",
                        "name": "巅峰榜更新剧",
                        "samples": {
                            "2026-05-28": {
                                "metrics": {"danmaku_uid_count": None},
                                "ranks": [{"key": "peak", "name": "巅峰榜", "position": 1}],
                            }
                        },
                    }
                },
            },
            "ongoing:manbo": {
                "version": 1,
                "platform": "manbo",
                "records": {"peak-ongoing": {"dramaId": "peak-ongoing", "updateType": "weekly"}},
            },
        }

        with (
            patch.object(fetch_rank_data, "_load_upstash_json_strict", side_effect=lambda key: responses.get(key)),
            patch.object(
                fetch_rank_data,
                "_load_normal_trend_v2_strict",
                return_value=responses["ranks:trend:manbo"],
            ),
            patch.object(fetch_rank_data, "_load_upstash_json", side_effect=lambda key: responses.get(key)),
        ):
            targets, sources, _payloads = fetch_rank_data.collect_null_danmaku_ids_from_layers("manbo", "2026-05-28")

        self.assertEqual(targets, {"peak-ongoing"})
        self.assertEqual(sources["peak-ongoing"], ["latest", "trend"])

    def test_collect_repair_ids_aborts_when_rewritten_layer_read_fails(self) -> None:
        def fake_request(command: list[object]) -> object:
            if command[:2] == ["GET", "ranks:latest"]:
                raise RuntimeError("temporary latest read failure")
            if command[0] == "GET":
                return json.dumps({"version": 1, "dramas": {}}, ensure_ascii=False)
            if command[0] == "SET":
                raise AssertionError("repair should not write after a read failure")
            raise AssertionError(command)

        with patch.object(fetch_rank_data, "upstash_request", side_effect=fake_request):
            with self.assertRaisesRegex(RuntimeError, "Failed to load ranks:latest"):
                fetch_rank_data.collect_null_danmaku_ids_from_layers("missevan", "2026-05-28")

    def test_repair_dry_run_does_not_fetch_or_write(self) -> None:
        payloads = {
            "metrics": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "manbo",
                "dramas": {"600": {"name": "漫播剧", "danmaku_uid_count": None}},
            },
            "partial": {"version": 1, "platform": "manbo", "data": {"ranks": {}, "dramas": {}}},
            "latest": {"version": 1, "missevan": {"ranks": {}, "dramas": {}}, "manbo": {"ranks": {}, "dramas": {}}},
            "trend": {"version": 1, "platform": "manbo", "dates": [], "dramas": {}},
        }

        with (
            patch.object(fetch_rank_data, "collect_null_danmaku_ids_from_layers", return_value=({"600"}, {"600": ["metrics"]}, payloads)),
            patch.object(fetch_rank_data, "fetch_one_manbo_danmaku_count", side_effect=AssertionError("should not fetch")),
            patch.object(fetch_rank_data, "upstash_request", side_effect=AssertionError("should not write")),
            patch("builtins.print"),
        ):
            result = fetch_rank_data.repair_null_danmaku_for_platform("manbo", "2026-05-28", dry_run=True)

        self.assertEqual(result["targets"], ["600"])
        self.assertEqual(result["repaired"], {})

    def test_repair_writes_same_count_to_all_layers(self) -> None:
        payloads = {
            "metrics": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "manbo",
                "generated_at": "old",
                "dramas": {"600": {"name": "漫播剧", "danmaku_uid_count": None}},
            },
            "partial": {
                "version": 1,
                "platform": "manbo",
                "data": {"ranks": {}, "dramas": {"600": {"name": "漫播剧", "danmaku_uid_count": None}}},
            },
            "latest": {
                "version": 1,
                "missevan": {"ranks": {}, "dramas": {}},
                "manbo": {"ranks": {}, "dramas": {"600": {"name": "漫播剧", "danmaku_uid_count": None}}},
            },
            "trend": {
                "version": 1,
                "platform": "manbo",
                "dates": ["2026-05-28"],
                "dramas": {
                    "600": {
                        "id": "600",
                        "name": "漫播剧",
                        "samples": {"2026-05-28": {"metrics": {"danmaku_uid_count": None}, "ranks": []}},
                    }
                },
            },
        }
        with (
            patch.object(fetch_rank_data, "collect_null_danmaku_ids_from_layers", return_value=({"600"}, {"600": ["metrics", "partial", "latest", "trend"]}, payloads)),
            patch.object(fetch_rank_data, "fetch_one_manbo_danmaku_count", return_value=("600", 42)),
            patch.object(fetch_rank_data, "upstash_request", return_value=0),
            patch.object(fetch_rank_data, "publish_rank_string") as publish_latest,
            patch.object(fetch_rank_data, "publish_normal_trend_v2") as publish_trend,
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-28T12:00:00+00:00"),
            patch("builtins.print"),
        ):
            result = fetch_rank_data.repair_null_danmaku_for_platform("manbo", "2026-05-28")

        self.assertEqual(result["repaired"], {"600": 42})
        latest_written = publish_latest.call_args.args[1]
        trend_written = publish_trend.call_args.args[1]
        self.assertEqual(latest_written["manbo"]["dramas"]["600"]["danmaku_uid_count"], 42)
        trend_metrics = trend_written["dramas"]["600"]["samples"]["2026-05-28"]["metrics"]
        self.assertEqual(trend_metrics["danmaku_uid_count"], 42)

    def test_repair_creates_metric_entry_when_target_only_in_trend(self) -> None:
        payloads = {
            "metrics": {"version": 1, "date": "2026-05-28", "platform": "missevan", "dramas": {}},
            "partial": {"version": 1, "platform": "missevan", "data": {"ranks": {}, "dramas": {}}},
            "latest": {"version": 1, "missevan": {"ranks": {}, "dramas": {}}, "manbo": {"ranks": {}, "dramas": {}}},
            "trend": {
                "version": 1,
                "platform": "missevan",
                "dates": ["2026-05-28"],
                "dramas": {
                    "93038": {
                        "id": "93038",
                        "name": "猫耳剧",
                        "cover": "cover-a",
                        "maincvs": ["甲"],
                        "samples": {
                            "2026-05-28": {
                                "metrics": {"view_count": 123, "danmaku_uid_count": None},
                                "ranks": [],
                            }
                        },
                    }
                },
            },
        }
        with (
            patch.object(fetch_rank_data, "collect_null_danmaku_ids_from_layers", return_value=({"93038"}, {"93038": ["trend"]}, payloads)),
            patch.object(fetch_rank_data, "fetch_one_missevan_danmaku_count", return_value=("93038", 7)),
            patch.object(fetch_rank_data, "upstash_request", return_value=0),
            patch.object(fetch_rank_data, "publish_rank_string") as publish_latest,
            patch.object(fetch_rank_data, "publish_normal_trend_v2"),
            patch.object(fetch_rank_data, "now_iso", return_value="2026-05-28T12:00:00+00:00"),
            patch("builtins.print"),
        ):
            fetch_rank_data.repair_null_danmaku_for_platform("missevan", "2026-05-28")

        latest_entry = publish_latest.call_args.args[1]["missevan"]["dramas"]["93038"]
        self.assertEqual(latest_entry["name"], "猫耳剧")
        self.assertEqual(latest_entry["cover"], "cover-a")
        self.assertEqual(latest_entry["maincvs"], ["甲"])
        self.assertEqual(latest_entry["view_count"], 123)
        self.assertEqual(latest_entry["danmaku_uid_count"], 7)

    def test_repair_writes_completed_counts_when_retry_hits_418(self) -> None:
        payloads = {
            "metrics": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "missevan",
                "dramas": {
                    "ok": {"name": "已成功", "danmaku_uid_count": None},
                    "rate-limited": {"name": "限频", "danmaku_uid_count": None},
                },
            },
            "partial": {"version": 1, "platform": "missevan", "data": {"ranks": {}, "dramas": {}}},
            "latest": {"version": 1, "missevan": {"ranks": {}, "dramas": {}}, "manbo": {"ranks": {}, "dramas": {}}},
            "trend": {"version": 1, "platform": "missevan", "dates": [], "dramas": {}},
        }

        with (
            patch.object(
                fetch_rank_data,
                "collect_null_danmaku_ids_from_layers",
                return_value=({"ok", "rate-limited"}, {"ok": ["metrics"], "rate-limited": ["metrics"]}, payloads),
            ),
            patch.object(
                fetch_rank_data,
                "_repair_one_danmaku",
                side_effect=[("ok", 1), RuntimeError("temporary"), RuntimeError("HTTP_418")],
            ),
            patch.object(fetch_rank_data, "write_repaired_danmaku_layers") as write_layers,
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
                fetch_rank_data.repair_null_danmaku_for_platform("missevan", "2026-05-28")

        write_layers.assert_called_once_with("missevan", "2026-05-28", {"ok": 1}, payloads)

    def test_repair_stops_when_missevan_getdm_returns_418(self) -> None:
        payloads = {
            "metrics": {
                "version": 1,
                "date": "2026-05-28",
                "platform": "missevan",
                "dramas": {
                    "ok": {"name": "已成功", "danmaku_uid_count": None},
                    "rate-limited": {"name": "限频", "danmaku_uid_count": None},
                },
            },
            "partial": {"version": 1, "platform": "missevan", "data": {"ranks": {}, "dramas": {}}},
            "latest": {"version": 1, "missevan": {"ranks": {}, "dramas": {}}, "manbo": {"ranks": {}, "dramas": {}}},
            "trend": {"version": 1, "platform": "missevan", "dates": [], "dramas": {}},
        }

        class FakeRequester:
            def request_json(self, url: str) -> dict:
                if "ok" in url:
                    return {"info": {"episodes": {"episode": []}}}
                return {"info": {"episodes": {"episode": [{"need_pay": 1, "sound_id": "sound-418"}]}}}

        response = requests.Response()
        response.status_code = 418
        response.url = "https://www.missevan.com/sound/getdm?soundid=sound-418"

        with (
            patch.object(
                fetch_rank_data,
                "collect_null_danmaku_ids_from_layers",
                return_value=({"ok", "rate-limited"}, {"ok": ["metrics"], "rate-limited": ["metrics"]}, payloads),
            ),
            patch.object(fetch_rank_data, "MissevanRequester", return_value=FakeRequester()),
            patch.object(fetch_rank_data.requests, "get", return_value=response),
            patch.object(fetch_rank_data, "write_repaired_danmaku_layers") as write_layers,
            patch.object(fetch_rank_data.time, "sleep"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP_418"):
                fetch_rank_data.repair_null_danmaku_for_platform("missevan", "2026-05-28")

        write_layers.assert_called_once_with("missevan", "2026-05-28", {"ok": 0}, payloads)


class RankRejectionFilteringTests(unittest.TestCase):
    def test_rejected_id_is_filtered_from_multi_drama_item_without_dropping_series(self) -> None:
        store = {
            "missevan": {
                "ranks": {
                    "peak": {
                        "items": [
                            {"name": "系列剧", "dramaIds": ["100", "200"]},
                            {"dramaId": "300"},
                        ]
                    }
                }
            }
        }

        fetch_rank_data.remove_drama_ids_from_rank_items(store, "missevan", {"100", "300"})

        self.assertEqual(
            store["missevan"]["ranks"]["peak"]["items"],
            [{"name": "系列剧", "dramaIds": ["200"]}],
        )


class ManboCvLookupTests(unittest.TestCase):
    def test_lookup_cvs_falls_back_to_nicknames_when_main_cv_names_are_blank(self) -> None:
        store = {
            "missevan": {"dramas": {}},
            "manbo": {
                "dramas": {
                    "201": {"name": "测试剧"},
                }
            },
        }

        def load_remote(key: str):
            if key == "missevan:info:v2":
                return {}
            if key == "manbo:info:v2":
                return {
                    "records": [
                        {
                            "dramaId": "201",
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

        self.assertEqual(store["manbo"]["dramas"]["201"]["maincvs"], ["规范名甲", "接口昵称乙"])


class MissevanCvLookupTests(unittest.TestCase):
    def test_lookup_cvs_includes_name_only_main_cv(self) -> None:
        store = {
            "missevan": {"dramas": {"94602": {"name": "测试剧"}}},
            "manbo": {"dramas": {}},
        }

        def load_remote(key: str):
            if key == "missevan:info:v2":
                return {
                    "94602": {
                        "maincvs": [3946],
                        "cvnames": {"3946": "辰朔"},
                        "fallbackCvNames": ["林风"],
                    }
                }
            if key == "manbo:info:v2":
                return {"records": []}
            raise AssertionError(key)

        with patch.object(fetch_rank_data, "_load_upstash_json", side_effect=load_remote), patch("builtins.print"):
            fetch_rank_data.lookup_cvs(store)

        self.assertEqual(store["missevan"]["dramas"]["94602"]["maincvs"], ["辰朔", "林风"])

    def test_lookup_cvs_queues_existing_records_with_missing_cover(self) -> None:
        store = {
            "missevan": {"dramas": {"100": {"name": "猫耳剧"}}},
            "manbo": {"dramas": {"200": {"name": "漫播剧"}}},
        }

        def load_remote(key: str):
            if key == "missevan:info:v2":
                return {"100": {"cover": "", "maincvs": [1, 2], "cvnames": {"1": "甲", "2": "乙"}}}
            if key == "manbo:info:v2":
                return {"records": [{"dramaId": "200", "cover": "", "mainCvNames": ["丙", "丁"]}]}
            raise AssertionError(key)

        with (
            patch.object(fetch_rank_data, "_load_upstash_json", side_effect=load_remote),
            patch.object(fetch_rank_data, "append_new_drama_ids_atomic") as append_queue,
            patch("builtins.print"),
        ):
            fetch_rank_data.lookup_cvs(store)

        append_queue.assert_called_once_with(["100"], ["200"])

    def test_lookup_cvs_does_not_queue_non_numeric_drama_ids(self) -> None:
        store = {
            "missevan": {"dramas": {}},
            "manbo": {"dramas": {"drama-1": {"name": "测试占位"}, "200": {"name": "有效剧"}}},
        }
        with (
            patch.object(fetch_rank_data, "_load_upstash_json", side_effect=[{}, {"records": []}]),
            patch.object(fetch_rank_data, "append_new_drama_ids_atomic") as append_queue,
            patch("builtins.print"),
        ):
            fetch_rank_data.lookup_cvs(store)

        append_queue.assert_called_once_with([], ["200"])


class QueueDramaIdValidationTests(unittest.TestCase):
    def test_atomic_append_filters_invalid_inputs_and_cleans_existing_values_in_lua(self) -> None:
        upstash = Mock(return_value='{"missevan":["100"],"manbo":["200"]}')
        with patch.object(fetch_rank_data, "upstash_request", upstash), patch("builtins.print"):
            fetch_rank_data.append_new_drama_ids_atomic(["100", "bad"], ["200", "drama-1"])

        command = upstash.call_args.args[0]
        self.assertEqual(json.loads(command[-2]), ["100"])
        self.assertEqual(json.loads(command[-1]), ["200"])
        self.assertIn('string.match(text, "^%d+$")', command[1])


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
            short_page_retry_delay=0,
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
            short_page_retry_delay=0,
        )

        self.assertEqual(result["failed_page_count"], 1)
        self.assertIn("incomplete", result["failed_pages"][0]["error"])

    def test_manbo_short_page_is_retried_before_marking_failure(self) -> None:
        calls: dict[tuple[str, int], int] = {}

        def request_json(url: str) -> dict:
            query = parse_qs(urlparse(url).query)
            set_id = query["dramaSetId"][0]
            page_no = int(query["pageNo"][0])
            key = (set_id, page_no)
            calls[key] = calls.get(key, 0) + 1
            if key == ("set-a", 2) and calls[key] == 1:
                return {"data": {"count": 4, "list": [{"eid": "3"}]}}
            pages = {
                ("set-a", 1): {"data": {"count": 4, "list": [{"eid": "1"}, {"eid": "2"}]}},
                ("set-a", 2): {"data": {"count": 4, "list": [{"eid": "3"}, {"eid": "4"}]}},
            }
            return pages[key]

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=request_json,
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a"],
            page_size=2,
            page_concurrency=4,
            retry_delay=0,
            short_page_retry_delay=0,
        )

        self.assertEqual(result["failed_page_count"], 0)
        self.assertEqual(result["unique_user_count"], 4)
        self.assertEqual(result["fetched_danmaku"], 4)
        self.assertEqual(calls[("set-a", 2)], 2)

    def test_manbo_short_page_retry_only_refetches_short_page(self) -> None:
        calls: dict[tuple[str, int], int] = {}

        def request_json(url: str) -> dict:
            query = parse_qs(urlparse(url).query)
            set_id = query["dramaSetId"][0]
            page_no = int(query["pageNo"][0])
            key = (set_id, page_no)
            calls[key] = calls.get(key, 0) + 1
            if key == ("set-a", 2) and calls[key] == 1:
                return {"data": {"count": 6, "list": [{"eid": "3"}]}}
            pages = {
                ("set-a", 1): {"data": {"count": 6, "list": [{"eid": "1"}, {"eid": "2"}]}},
                ("set-a", 2): {"data": {"count": 6, "list": [{"eid": "3"}, {"eid": "4"}]}},
                ("set-a", 3): {"data": {"count": 6, "list": [{"eid": "5"}, {"eid": "6"}]}},
            }
            return pages[key]

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=request_json,
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a"],
            page_size=2,
            page_concurrency=4,
            retry_delay=0,
            short_page_retry_delay=0,
        )

        self.assertEqual(result["failed_page_count"], 0)
        self.assertEqual(result["unique_user_count"], 6)
        self.assertEqual(calls[("set-a", 1)], 1)
        self.assertEqual(calls[("set-a", 2)], 2)
        self.assertEqual(calls[("set-a", 3)], 1)

    def test_manbo_persistent_short_page_reports_page_and_counts(self) -> None:
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
            retry_delay=0,
            short_page_retry_delay=0,
        )

        self.assertEqual(result["failed_page_count"], 1)
        failure = result["failed_pages"][0]
        self.assertEqual(failure["set_id"], "set-a")
        self.assertEqual(failure["page_no"], 2)
        self.assertEqual(failure["expected_entries"], 2)
        self.assertEqual(failure["actual_entries"], 1)

    def test_manbo_short_page_uses_first_page_total_when_later_count_drops(self) -> None:
        pages = {
            ("set-a", 1): {"data": {"count": 4, "list": [{"eid": "1"}, {"eid": "2"}]}},
            ("set-a", 2): {"data": {"count": 2, "list": []}},
        }

        result = fetch_rank_data.fetch_manbo_paid_danmaku_benchmark(
            "drama-1",
            request_json=self._manbo_page_requester(pages),
            paid_set_id_loader=lambda *_args, **_kwargs: ["set-a"],
            page_size=2,
            page_concurrency=4,
            retry_delay=0,
            short_page_retry_delay=0,
        )

        self.assertEqual(result["failed_page_count"], 1)
        failure = result["failed_pages"][0]
        self.assertEqual(failure["set_id"], "set-a")
        self.assertEqual(failure["page_no"], 2)
        self.assertEqual(failure["expected_entries"], 2)
        self.assertEqual(failure["actual_entries"], 0)

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


class TargetCatalogAdmissionTests(unittest.TestCase):
    def test_manbo_detail_rejects_empty_title(self) -> None:
        with patch.object(fetch_rank_data, "request_manbo_json", return_value={"data": {"category": 1, "title": ""}}):
            with self.assertRaisesRegex(fetch_rank_data.RejectedDramaRecord, "empty title"):
                fetch_rank_data._fetch_one_manbo("200", {})

    def test_manbo_detail_rejects_podcast(self) -> None:
        payload = {"data": {"category": 4, "title": "播客"}}
        with patch.object(fetch_rank_data, "request_manbo_json", return_value=payload):
            with self.assertRaisesRegex(fetch_rank_data.RejectedDramaRecord, "non-target catalog=4"):
                fetch_rank_data._fetch_one_manbo("200", {})

    def test_manbo_detail_accepts_supported_catalog(self) -> None:
        entry = {}
        payload = {"data": {"category": 1, "title": "广播剧", "watchCount": 1}}
        with (
            patch.object(fetch_rank_data, "request_manbo_json", return_value=payload),
            patch.object(fetch_rank_data, "now_iso", return_value="2026-07-22T00:00:00+00:00"),
        ):
            fetch_rank_data._fetch_one_manbo("200", entry)

        self.assertEqual(entry["name"], "广播剧")


if __name__ == "__main__":
    unittest.main()
