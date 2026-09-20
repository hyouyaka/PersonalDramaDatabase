import json
import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

import refresh_watch_counts


class RefreshWatchCountsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.download_info_patcher = patch.object(refresh_watch_counts, "download_info_file")
        self.download_info = self.download_info_patcher.start()
        self.addCleanup(self.download_info_patcher.stop)
        self.load_archives_patcher = patch.object(refresh_watch_counts, "load_local_archives")
        self.load_archives = self.load_archives_patcher.start()
        self.load_archives.return_value = (
            {"version": 1, "platform": "test", "updatedAt": None, "records": {}},
            {"version": 1, "platform": "test", "updatedAt": None, "records": {}},
        )
        self.addCleanup(self.load_archives_patcher.stop)
        self.ensure_archives_patcher = patch.object(refresh_watch_counts, "ensure_remote_archive_keys")
        self.ensure_archives_patcher.start()
        self.addCleanup(self.ensure_archives_patcher.stop)

    def test_default_all_mode_runs_missevan_and_manbo_concurrently(self) -> None:
        missevan_started = threading.Event()
        manbo_started = threading.Event()
        overlap = {"value": False}

        def fake_missevan(*, target_ids=None):
            missevan_started.set()
            overlap["value"] = manbo_started.wait(timeout=1)
            return {"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0}

        def fake_manbo(*, target_ids=None):
            manbo_started.set()
            missevan_started.wait(timeout=1)
            return {"processed": 1, "skipped": 0}

        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=fake_missevan),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", side_effect=fake_manbo),
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main([])

        self.assertEqual(result, 0)
        self.assertTrue(overlap["value"])

    def test_platform_missevan_only_does_not_refresh_manbo(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 0)
        missevan.assert_called_once_with(target_ids=None)
        manbo.assert_not_called()

    def test_explicit_missevan_ids_do_not_trigger_manbo_full_refresh(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--missevan", "100"])

        self.assertEqual(result, 0)
        missevan.assert_called_once_with(target_ids={"100"})
        manbo.assert_not_called()

    def test_missevan_418_still_returns_exit_2(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=RuntimeError("HTTP_418")),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 2)
        manbo.assert_not_called()

    def test_missevan_418_publishes_partial_results_before_exit(self) -> None:
        stats = {
            "processed": 1,
            "skipped": 0,
            "archived": 0,
            "request_count": 2,
            "last_backoff_seconds": 60,
            "info_observations": {"100": {"needpay": True, "soundIds": ["2001"]}},
        }
        interrupted = refresh_watch_counts.MissevanRefreshInterrupted("HTTP_418", stats)
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=interrupted),
            patch.object(refresh_watch_counts, "publish_info_observations", return_value={}) as publish_info,
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 2)
        publish_info.assert_called_once_with("missevan", stats["info_observations"])
        upload.assert_called_once_with(
            "missevan",
            refresh_watch_counts.MISSEVAN_COUNTS_PATH,
            excluded_drama_ids=set(),
        )

    def test_parallel_418_still_publishes_completed_manbo_result(self) -> None:
        missevan_stats = {
            "processed": 1,
            "skipped": 0,
            "archived": 0,
            "request_count": 2,
            "last_backoff_seconds": 60,
            "info_observations": {"100": {"soundIds": ["2001"]}},
        }
        manbo_stats = {
            "processed": 1,
            "skipped": 0,
            "info_observations": {"200": {"soundIds": ["3001"]}},
        }
        interrupted = refresh_watch_counts.MissevanRefreshInterrupted("HTTP_418", missevan_stats)
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=interrupted),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", return_value=manbo_stats),
            patch.object(refresh_watch_counts, "publish_info_observations", return_value={}) as publish_info,
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main([])

        self.assertEqual(result, 2)
        self.assertEqual(
            publish_info.call_args_list,
            [
                call("missevan", missevan_stats["info_observations"]),
                call("manbo", manbo_stats["info_observations"]),
            ],
        )
        self.assertEqual(
            upload.call_args_list,
            [
                call(
                    "missevan",
                    refresh_watch_counts.MISSEVAN_COUNTS_PATH,
                    excluded_drama_ids=set(),
                ),
                call(
                    "manbo",
                    refresh_watch_counts.MANBO_COUNTS_PATH,
                    excluded_drama_ids=set(),
                ),
            ],
        )

    def test_parallel_manbo_runtime_error_is_not_reported_as_missevan_418(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", side_effect=RuntimeError("manbo failed")),
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            with self.assertRaises(RuntimeError):
                refresh_watch_counts.main([])

    def test_default_all_mode_syncs_and_uploads_both_watchcount_platforms(self) -> None:
        with (
            patch.object(
                refresh_watch_counts,
                "sync_remote_watchcount_if_newer",
                return_value={},
            ) as sync_remote,
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", return_value={"processed": 1, "skipped": 0}),
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main([])

        self.assertEqual(result, 0)
        self.assertEqual(
            self.download_info.call_args_list,
            [
                call(refresh_watch_counts.MISSEVAN_INFO_KEY, refresh_watch_counts.MISSEVAN_INFO_PATH),
                call(refresh_watch_counts.MANBO_INFO_KEY, refresh_watch_counts.MANBO_INFO_PATH),
            ],
        )
        self.assertEqual(
            sync_remote.call_args_list,
            [
                call("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH, force=False),
                call("manbo", refresh_watch_counts.MANBO_COUNTS_PATH, force=False),
            ],
        )
        self.assertEqual(
            upload.call_args_list,
            [
                call(
                    "missevan",
                    refresh_watch_counts.MISSEVAN_COUNTS_PATH,
                    excluded_drama_ids=set(),
                ),
                call(
                    "manbo",
                    refresh_watch_counts.MANBO_COUNTS_PATH,
                    excluded_drama_ids=set(),
                ),
            ],
        )

    def test_force_only_changes_remote_watchcount_sync_policy(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer") as sync_remote,
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as refresh_missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as refresh_manbo,
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan", "--force"])

        self.assertEqual(result, 0)
        sync_remote.assert_called_once_with("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH, force=True)
        refresh_missevan.assert_called_once_with(target_ids=None)
        refresh_manbo.assert_not_called()

    def test_refresh_all_is_forwarded_without_changing_force_sync(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer") as sync_remote,
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as refresh_missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as refresh_manbo,
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan", "--refresh-all"])

        self.assertEqual(result, 0)
        sync_remote.assert_called_once_with("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH, force=False)
        refresh_missevan.assert_called_once_with(target_ids=None, refresh_all=True)
        refresh_manbo.assert_not_called()

    def test_explicit_manbo_ids_download_only_manbo_info(self) -> None:
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", return_value={"processed": 1, "skipped": 0}),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts") as refresh_missevan,
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--manbo", "200"])

        self.assertEqual(result, 0)
        self.download_info.assert_called_once_with(refresh_watch_counts.MANBO_INFO_KEY, refresh_watch_counts.MANBO_INFO_PATH)
        refresh_missevan.assert_not_called()

    def test_info_download_failure_stops_before_watchcount_sync_and_refresh(self) -> None:
        self.download_info.side_effect = RuntimeError("invalid remote info")
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer") as sync_remote,
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts") as refresh_missevan,
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid remote info"):
                refresh_watch_counts.main(["--platform", "missevan"])

        sync_remote.assert_not_called()
        refresh_missevan.assert_not_called()
        upload.assert_not_called()

    def test_no_upload_leaves_remote_info_and_watchcount_keys_untouched(self) -> None:
        with (
            patch.object(refresh_watch_counts, "load_env_file"),
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as refresh_manbo,
            patch.object(refresh_watch_counts, "publish_info_observations") as publish_info,
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan", "--no-upload"])

        self.assertEqual(result, 0)
        refresh_manbo.assert_not_called()
        publish_info.assert_not_called()
        upload.assert_not_called()

    def test_main_loads_env_before_remote_watchcount_sync(self) -> None:
        calls = []

        def fake_load_env(path):
            calls.append(("env", path))

        def fake_download(*args, **kwargs):
            calls.append(("info", args, kwargs))

        def fake_sync(*args, **kwargs):
            calls.append(("sync", args, kwargs))

        with (
            patch.object(refresh_watch_counts, "load_env_file", side_effect=fake_load_env),
            patch.object(refresh_watch_counts, "download_info_file", side_effect=fake_download),
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer", side_effect=fake_sync),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("env", refresh_watch_counts.ROOT / ".env"))
        self.assertEqual(calls[1][0], "info")
        self.assertEqual(calls[2][0], "sync")

    def test_info_publish_failure_prevents_watchcount_upload(self) -> None:
        stats = {
            "processed": 1,
            "skipped": 0,
            "archived": 0,
            "request_count": 1,
            "last_backoff_seconds": 0,
            "info_observations": {"100": {"needpay": True, "soundIds": ["2001"]}},
        }
        with (
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", return_value=stats),
            patch.object(refresh_watch_counts, "publish_info_observations", side_effect=RuntimeError("info failed")),
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "info failed"):
                refresh_watch_counts.main(["--platform", "missevan"])

        upload.assert_not_called()

    def test_publish_results_preserves_fresh_local_watchcount_during_archive(self) -> None:
        result = {
            "archive_candidates": {
                "100": {
                    "archivedAt": "2026-07-31T04:00:00+00:00",
                    "archivedReason": "HTTP_403",
                }
            },
            "info_observations": {},
        }
        with (
            patch.object(
                refresh_watch_counts,
                "publish_archive_candidates",
                return_value={"archived": 1},
            ) as publish_archive,
            patch.object(refresh_watch_counts, "publish_info_observations", return_value={}),
            patch.object(refresh_watch_counts, "upload_watchcount_file"),
            patch("builtins.print"),
        ):
            refresh_watch_counts.publish_refresh_results(
                ("missevan",),
                {"missevan": result},
            )

        publish_archive.assert_called_once_with(
            "missevan",
            result["archive_candidates"],
            sync_local_watchcount=False,
        )


class ArchiveRetryQueueTests(unittest.TestCase):
    @staticmethod
    def http_error(status: int) -> Exception:
        exc = RuntimeError(f"HTTP {status}")
        exc.response = Mock(status_code=status)
        return exc

    def test_failed_item_does_not_block_later_first_attempts(self) -> None:
        now = [0.0]
        calls = []
        attempts = {"100": 0, "101": 0}
        completed = []

        def request_one(drama_id, request_number):
            calls.append((drama_id, request_number, now[0]))
            attempts[drama_id] += 1
            if drama_id == "100" and attempts[drama_id] < 4:
                raise self.http_error(403)
            return {"id": drama_id}

        stats = refresh_watch_counts.run_archive_retry_queue(
            "missevan",
            ["100", "101"],
            drama_id_of=lambda drama_id: drama_id,
            request_one=request_one,
            on_success=lambda drama_id, _payload: completed.append(drama_id),
            on_archive=lambda _drama_id, _reason: self.fail("should not archive"),
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

        self.assertEqual(
            [(drama_id, request_number) for drama_id, request_number, _at in calls],
            [("100", 1), ("101", 1), ("100", 2), ("100", 3), ("100", 4)],
        )
        self.assertEqual([at for drama_id, _request, at in calls if drama_id == "100"], [0, 30, 90, 210])
        self.assertEqual(completed, ["101", "100"])
        self.assertEqual(stats["retry_requests"], 3)

    def test_four_archive_signals_archive_for_each_platform(self) -> None:
        for platform, expected_reason in (
            ("missevan", "HTTP_403"),
            ("manbo", "MANBO_CODE_400_作品已下架"),
        ):
            with self.subTest(platform=platform):
                now = [0.0]
                archived = []

                def request_one(_item, _request_number):
                    if platform == "missevan":
                        raise self.http_error(403)
                    return {"code": 400, "msg": "作品已下架", "data": None}

                refresh_watch_counts.run_archive_retry_queue(
                    platform,
                    ["1"],
                    drama_id_of=lambda drama_id: drama_id,
                    request_one=request_one,
                    on_success=lambda _item, _payload: self.fail("should not succeed"),
                    on_archive=lambda drama_id, reason: archived.append((drama_id, reason)),
                    monotonic=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                )

                self.assertEqual(archived, [("1", expected_reason)])
                self.assertEqual(now[0], 210)

    def test_four_missevan_403s_record_null_without_archiving(self) -> None:
        store = {"100": {"dramaId": 100, "title": "伪风控剧"}}
        cache = {
            "_meta": {},
            "counts": {
                "100": {
                    "name": "旧名称",
                    "view_count": 123,
                    "fetched_at": "2026-07-01T00:00:00+00:00",
                }
            },
        }
        requester = Mock()
        requester.request_json.side_effect = [self.http_error(403) for _ in range(4)]
        requester.request_count = 4
        requester.last_backoff_seconds = 0

        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value=cache),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "ARCHIVE_RETRY_DELAYS", (0, 0, 0)),
            patch.object(refresh_watch_counts, "apply_local_archive_candidates") as apply_archive,
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
            patch("builtins.print"),
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["archived"], 0)
        self.assertEqual(stats["archive_candidates"], {})
        self.assertIsNone(cache["counts"]["100"]["view_count"])
        self.assertEqual(cache["counts"]["100"]["name"], "旧名称")
        self.assertIn("100", store)
        apply_archive.assert_not_called()

    def test_manbo_archive_signal_requires_both_code_and_message(self) -> None:
        self.assertEqual(
            refresh_watch_counts.archive_reason(
                "manbo",
                payload={"code": 400, "msg": "作品已下架", "data": None},
            ),
            "MANBO_CODE_400_作品已下架",
        )
        self.assertIsNone(
            refresh_watch_counts.archive_reason(
                "manbo",
                payload={"code": 400, "msg": "其他错误", "data": None},
            )
        )
        self.assertIsNone(
            refresh_watch_counts.archive_reason(
                "manbo",
                payload={"code": 200, "msg": "作品已下架", "data": {}},
            )
        )

    def test_non_archive_status_still_fails_fast(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            refresh_watch_counts.run_archive_retry_queue(
                "missevan",
                ["1"],
                drama_id_of=lambda drama_id: drama_id,
                request_one=lambda _item, _request_number: (_ for _ in ()).throw(
                    self.http_error(404)
                ),
                on_success=lambda _item, _payload: None,
                on_archive=lambda _item, _reason: None,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None,
            )

    def test_manbo_network_error_does_not_trigger_archive_retries(self) -> None:
        requests = []
        archived = []

        def request_one(_item, request_number):
            requests.append(request_number)
            raise RuntimeError("connection timeout")

        with self.assertRaisesRegex(RuntimeError, "connection timeout"):
            refresh_watch_counts.run_archive_retry_queue(
                "manbo",
                ["1"],
                drama_id_of=lambda drama_id: drama_id,
                request_one=request_one,
                on_success=lambda _item, _payload: None,
                on_archive=lambda drama_id, reason: archived.append((drama_id, reason)),
                monotonic=lambda: 0,
                sleep=lambda _seconds: self.fail("network errors must not enter the retry queue"),
            )

        self.assertEqual(requests, [1])
        self.assertEqual(archived, [])


class InfoRefreshTests(unittest.TestCase):
    def test_refresh_all_backfills_missing_missevan_create_time_and_records_observation(self) -> None:
        store = {"100": {"dramaId": "100", "title": "猫耳测试", "createTime": ""}}
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {"name": "猫耳测试", "view_count": 10, "pay_type": 0, "price": 0},
                    "episodes": {"episode": [{"sound_id": "300", "name": "预告"}]},
                }
            },
            {
                "info": {
                    "episodes": {
                        "episode": [
                            {
                                "name": "第一集",
                                "create_time": datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp(),
                            }
                        ]
                    }
                }
            },
        ]
        requester.request_count = 2
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        self.assertEqual(requester.request_json.call_count, 2)
        self.assertEqual(store["100"]["createTime"], "2026.08")
        self.assertEqual(stats["info_observations"]["100"]["createTime"], "2026.08")
        self.assertEqual(stats["create_time_checked"], 1)
        self.assertEqual(stats["create_time_updated"], 1)
        self.assertEqual(stats["create_time_still_missing"], 0)

    def test_refresh_all_backfills_manbo_create_time_from_same_detail_response(self) -> None:
        record = {"dramaId": "200", "name": "漫播测试", "createTime": None}
        store = {"records": [record]}
        payload = {
            "data": {
                "title": "漫播测试",
                "watchCount": 20,
                "price": 0,
                "memberPrice": 0,
                "vipFree": 0,
                "setRespList": [
                    {
                        "setTitle": "第一集",
                        "createTime": int(
                            datetime(2026, 7, 3, tzinfo=timezone.utc).timestamp() * 1000
                        ),
                    }
                ],
            }
        }
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "request_manbo_json", return_value=payload) as request_json,
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_json"),
        ):
            stats = refresh_watch_counts.refresh_manbo_watch_counts(refresh_all=True)

        request_json.assert_called_once()
        self.assertEqual(record["createTime"], "2026.07")
        self.assertEqual(stats["info_observations"]["200"]["createTime"], "2026.07")
        self.assertEqual(stats["create_time_checked"], 1)
        self.assertEqual(stats["create_time_updated"], 1)
        self.assertEqual(stats["create_time_still_missing"], 0)

    def test_refresh_all_keeps_create_time_empty_when_no_main_episode_exists(self) -> None:
        store = {"100": {"dramaId": "100", "title": "仅预告", "createTime": ""}}
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {"name": "仅预告", "view_count": 10, "pay_type": 0, "price": 0},
                    "episodes": {"episode": [{"sound_id": "300", "name": "预告"}]},
                }
            },
            {
                "info": {
                    "episodes": {
                        "episode": [
                            {
                                "name": "预告",
                                "create_time": datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp(),
                            }
                        ]
                    }
                }
            },
        ]
        requester.request_count = 2
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        self.assertEqual(store["100"]["createTime"], "")
        self.assertNotIn("createTime", stats["info_observations"]["100"])
        self.assertEqual(stats["create_time_checked"], 1)
        self.assertEqual(stats["create_time_updated"], 0)
        self.assertEqual(stats["create_time_still_missing"], 1)

    def test_refresh_all_does_not_request_sound_detail_when_create_time_exists(self) -> None:
        store = {"100": {"dramaId": "100", "title": "已有日期", "createTime": "2025.01"}}
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "drama": {"name": "已有日期", "view_count": 10, "pay_type": 0, "price": 0},
                "episodes": {"episode": [{"sound_id": "300", "name": "预告"}]},
            }
        }
        requester.request_count = 1
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        requester.request_json.assert_called_once()
        self.assertEqual(store["100"]["createTime"], "2025.01")
        self.assertEqual(stats["create_time_checked"], 0)

    def test_optional_missevan_create_time_failure_does_not_abort_refresh(self) -> None:
        store = {"100": {"dramaId": "100", "title": "补全失败", "createTime": ""}}
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {"name": "补全失败", "view_count": 10, "pay_type": 0, "price": 0},
                    "episodes": {"episode": [{"sound_id": "300", "name": "预告"}]},
                }
            },
            RuntimeError("HTTP_404"),
        ]
        requester.request_count = 2
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
            patch("builtins.print") as print_mock,
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["create_time_checked"], 1)
        self.assertEqual(stats["create_time_updated"], 0)
        self.assertEqual(stats["create_time_still_missing"], 1)
        self.assertEqual(store["100"]["createTime"], "")
        self.assertTrue(any("createTime 补全失败" in str(call) for call in print_mock.call_args_list))

    def test_missevan_create_time_418_still_interrupts_refresh_all(self) -> None:
        store = {"100": {"dramaId": "100", "title": "触发限流", "createTime": ""}}
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {"name": "触发限流", "view_count": 10, "pay_type": 0, "price": 0},
                    "episodes": {"episode": [{"sound_id": "300", "name": "预告"}]},
                }
            },
            RuntimeError("HTTP_418"),
        ]
        requester.request_count = 2
        requester.last_backoff_seconds = 60
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            with self.assertRaises(refresh_watch_counts.MissevanRefreshInterrupted):
                refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

    def test_refresh_all_bypasses_recent_cache_for_both_platforms(self) -> None:
        missevan_requester = Mock()
        missevan_requester.request_json.return_value = {
            "info": {"drama": {"name": "猫耳", "view_count": 10, "pay_type": 0, "price": 0}}
        }
        missevan_requester.request_count = 1
        missevan_requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value={"100": {"dramaId": "100"}}),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {"100": {"fetched_at": "recent"}}}),
            patch.object(refresh_watch_counts, "should_skip_recent", return_value=True),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=missevan_requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            missevan_stats = refresh_watch_counts.refresh_missevan_watch_counts(refresh_all=True)

        manbo_payload = {"data": {"title": "漫播", "watchCount": 20, "price": 0}}
        with (
            patch.object(refresh_watch_counts, "load_json", return_value={"records": [{"dramaId": "200"}]}),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {"200": {"fetched_at": "recent"}}}),
            patch.object(refresh_watch_counts, "should_skip_recent", return_value=True),
            patch.object(refresh_watch_counts, "request_manbo_json", return_value=manbo_payload) as manbo_request,
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_json"),
        ):
            manbo_stats = refresh_watch_counts.refresh_manbo_watch_counts(refresh_all=True)

        missevan_requester.request_json.assert_called_once()
        manbo_request.assert_called_once()
        self.assertEqual(missevan_stats["skipped"], 0)
        self.assertEqual(manbo_stats["skipped"], 0)

    def test_missevan_418_carries_completed_info_observations(self) -> None:
        store = {
            "100": {"dramaId": 100, "title": "已完成", "soundIds": ["old"]},
            "101": {"dramaId": 101, "title": "触发限流", "soundIds": ["old"]},
        }
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {
                        "name": "已完成",
                        "cover": "https://cover.test/missevan-completed.jpg",
                        "view_count": 10,
                        "pay_type": 2,
                        "price": 199,
                    },
                    "episodes": {"episode": [{"sound_id": "2001"}]},
                }
            },
            RuntimeError("HTTP_418"),
        ]
        requester.request_count = 2
        requester.last_backoff_seconds = 60
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
            patch.object(refresh_watch_counts, "save_json"),
        ):
            with self.assertRaises(refresh_watch_counts.MissevanRefreshInterrupted) as caught:
                refresh_watch_counts.refresh_missevan_watch_counts()

        self.assertEqual(caught.exception.stats["processed"], 1)
        self.assertEqual(
            caught.exception.stats["info_observations"],
            {
                "100": {
                    "needpay": True,
                    "cover": "https://cover.test/missevan-completed.jpg",
                    "soundIds": ["2001"],
                }
            },
        )
        self.assertEqual(store["100"]["cover"], "https://cover.test/missevan-completed.jpg")
        self.assertEqual(store["100"]["soundIds"], ["2001"])

    def test_missevan_watchcount_request_also_collects_cover_pricing_and_sound_ids(self) -> None:
        store = {
            "100": {
                "dramaId": 100,
                "title": "测试",
                "needpay": False,
                "is_member": False,
                "soundIds": ["old"],
            }
        }
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "drama": {
                    "name": "测试",
                    "cover": "https://cover.test/missevan-new.jpg",
                    "view_count": 10,
                    "pay_type": 2,
                    "price": 199,
                    "vip": 1,
                },
                "episodes": {
                    "episode": [
                        {"sound_id": "2002"},
                        {"sound_id": ""},
                        {"sound_id": 2001},
                        {"sound_id": "2002"},
                    ]
                },
            }
        }
        requester.request_count = 1
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            stats = refresh_watch_counts.refresh_missevan_watch_counts()

        requester.request_json.assert_called_once()
        self.assertEqual(
            stats["info_observations"],
            {
                "100": {
                    "needpay": True,
                    "is_member": True,
                    "cover": "https://cover.test/missevan-new.jpg",
                    "soundIds": ["2002", "2001"],
                }
            },
        )
        self.assertEqual(store["100"]["cover"], "https://cover.test/missevan-new.jpg")
        self.assertEqual(store["100"]["soundIds"], ["2002", "2001"])
        self.assertEqual(stats["pricing_checked"], 1)

    def test_missing_or_empty_episodes_preserve_existing_sound_ids(self) -> None:
        for info_extra in ({}, {"episodes": {"episode": [{"sound_id": ""}, {}]}}):
            with self.subTest(info_extra=info_extra):
                store = {"100": {"dramaId": 100, "title": "测试", "soundIds": ["old"]}}
                requester = Mock()
                requester.request_json.return_value = {
                    "info": {
                        "drama": {"name": "测试", "view_count": 10, "pay_type": 0, "price": 0},
                        **info_extra,
                    }
                }
                requester.request_count = 1
                requester.last_backoff_seconds = 0
                with (
                    patch.object(refresh_watch_counts, "load_json", return_value=store),
                    patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
                    patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
                    patch.object(refresh_watch_counts, "save_cache"),
                    patch.object(refresh_watch_counts, "save_missevan_store"),
                ):
                    stats = refresh_watch_counts.refresh_missevan_watch_counts()

                requester.request_json.assert_called_once()
                self.assertEqual(store["100"]["soundIds"], ["old"])
                self.assertNotIn("soundIds", stats["info_observations"]["100"])

    def test_manbo_watchcount_request_also_collects_cover_pricing_and_sound_ids(self) -> None:
        record = {
            "dramaId": "200",
            "name": "漫播测试",
            "needpay": False,
            "vipFree": 0,
            "soundIds": ["old"],
        }
        store = {"records": [record]}
        payload = {
            "data": {
                "title": "漫播测试",
                "coverPic": "",
                "largePic": "https://cover.test/manbo-new.jpg",
                "cover": "https://cover.test/manbo-fallback.jpg",
                "watchCount": 20,
                "price": 1990,
                "memberPrice": 1592,
                "vipFree": 1,
                "setRespList": [
                    {"radioDramaSetIdStr": "3002", "id": "ignored"},
                    {"dramaSetId": 3001},
                    {"setId": "3002"},
                    {"id": ""},
                    "invalid",
                ],
            }
        }
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "request_manbo_json", return_value=payload) as request_json,
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_json"),
        ):
            stats = refresh_watch_counts.refresh_manbo_watch_counts()

        request_json.assert_called_once()
        self.assertEqual(
            stats["info_observations"],
            {
                "200": {
                    "needpay": True,
                    "vipFree": 1,
                    "cover": "https://cover.test/manbo-new.jpg",
                    "soundIds": ["3002", "3001"],
                }
            },
        )
        self.assertEqual(record["cover"], "https://cover.test/manbo-new.jpg")
        self.assertEqual(record["soundIds"], ["3002", "3001"])

    def test_missing_or_empty_cover_preserves_existing_cover_without_extra_requests(self) -> None:
        missevan_store = {
            "100": {
                "dramaId": 100,
                "title": "测试",
                "cover": "https://cover.test/old-missevan.jpg",
            }
        }
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "drama": {
                    "name": "测试",
                    "cover": "",
                    "view_count": 10,
                    "pay_type": 0,
                    "price": 0,
                }
            }
        }
        requester.request_count = 1
        requester.last_backoff_seconds = 0
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=missevan_store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "MissevanRequester", return_value=requester),
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_missevan_store"),
        ):
            missevan_stats = refresh_watch_counts.refresh_missevan_watch_counts()

        manbo_record = {
            "dramaId": "200",
            "name": "漫播测试",
            "cover": "https://cover.test/old-manbo.jpg",
        }
        manbo_store = {"records": [manbo_record]}
        manbo_payload = {
            "data": {
                "title": "漫播测试",
                "coverPic": "",
                "largePic": None,
                "cover": " ",
                "sharePicUrl": "",
                "watchCount": 20,
                "price": 0,
                "memberPrice": 0,
                "vipFree": 0,
                "setRespList": [{}],
            }
        }
        with (
            patch.object(refresh_watch_counts, "load_json", return_value=manbo_store),
            patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
            patch.object(refresh_watch_counts, "request_manbo_json", return_value=manbo_payload) as request_json,
            patch.object(refresh_watch_counts, "save_cache"),
            patch.object(refresh_watch_counts, "save_json"),
        ):
            manbo_stats = refresh_watch_counts.refresh_manbo_watch_counts()

        requester.request_json.assert_called_once()
        request_json.assert_called_once()
        self.assertEqual(missevan_store["100"]["cover"], "https://cover.test/old-missevan.jpg")
        self.assertNotIn("cover", missevan_stats["info_observations"]["100"])
        self.assertEqual(manbo_record["cover"], "https://cover.test/old-manbo.jpg")
        self.assertNotIn("cover", manbo_stats["info_observations"]["200"])

    def test_manbo_missing_or_empty_sets_preserve_existing_sound_ids(self) -> None:
        for sets in (None, [], [{"id": ""}, {}]):
            with self.subTest(sets=sets):
                record = {"dramaId": "200", "name": "漫播测试", "soundIds": ["old"]}
                store = {"records": [record]}
                data = {
                    "title": "漫播测试",
                    "watchCount": 20,
                    "price": 1990,
                    "memberPrice": 1592,
                    "vipFree": 0,
                }
                if sets is not None:
                    data["setRespList"] = sets
                with (
                    patch.object(refresh_watch_counts, "load_json", return_value=store),
                    patch.object(refresh_watch_counts, "load_cache", return_value={"_meta": {}, "counts": {}}),
                    patch.object(refresh_watch_counts, "request_manbo_json", return_value={"data": data}),
                    patch.object(refresh_watch_counts, "save_cache"),
                    patch.object(refresh_watch_counts, "save_json"),
                ):
                    stats = refresh_watch_counts.refresh_manbo_watch_counts()

                self.assertEqual(record["soundIds"], ["old"])
                self.assertNotIn("soundIds", stats["info_observations"]["200"])

    def test_missing_pricing_fields_preserve_needpay(self) -> None:
        fields, complete = refresh_watch_counts.missevan_pricing_observation({"view_count": 10, "vip": 0})

        self.assertFalse(complete)
        self.assertEqual(fields, {"is_member": False})

    def test_manbo_pricing_covers_free_redbean_paid_and_member(self) -> None:
        free_payload = {
            "data": {
                "price": 0,
                "memberPrice": 0,
                "vipFree": 0,
                "setRespList": [{"price": 0, "memberPrice": 0, "vipFree": 0}],
            }
        }
        redbean_payload = {
            "data": {
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
                "setRespList": [{"price": 0, "memberPrice": 0, "vipFree": 0}],
            }
        }
        paid_payload = {
            "data": {"price": 1990, "memberPrice": 1592, "vipFree": 1, "setRespList": [{}]}
        }

        self.assertEqual(refresh_watch_counts.manbo_pricing_observation("1", free_payload), ({"needpay": False, "vipFree": 0}, True))
        self.assertEqual(refresh_watch_counts.manbo_pricing_observation("1", redbean_payload), ({"needpay": False, "vipFree": 0}, True))
        self.assertEqual(refresh_watch_counts.manbo_pricing_observation("1", paid_payload), ({"needpay": True, "vipFree": 1}, True))

    def test_remote_info_patch_retries_and_preserves_concurrent_fields(self) -> None:
        first = json.dumps(
            {"100": {"dramaId": 100, "title": "旧标题", "needpay": False, "soundIds": ["old"]}},
            ensure_ascii=False,
        )
        second = json.dumps(
            {
                "100": {
                    "dramaId": 100,
                    "title": "并发新标题",
                    "needpay": False,
                    "soundIds": ["old"],
                    "createTime": "2026.09",
                }
            },
            ensure_ascii=False,
        )
        commands = []
        remote_values = {"missevan:info:v2": first}
        compare_attempts = 0

        def fake_upstash(command):
            nonlocal compare_attempts
            commands.append(command)
            if command[0] == "GET":
                return remote_values.get(command[1])
            if command[0] == "EVAL" and command[2] == 2:
                compare_attempts += 1
                if compare_attempts == 1:
                    remote_values["missevan:info:v2"] = second
                    return 0
                current = remote_values.get(command[3])
                if hashlib.sha1(current.encode("utf-8")).hexdigest() != command[5]:
                    return 0
                remote_values[command[3]] = command[6]
                remote_values[command[4]] = command[7]
                return 1
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            refresh_watch_counts, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan.json"
        ):
            stats = refresh_watch_counts.publish_info_observations(
                "missevan",
                {
                    "100": {
                        "needpay": True,
                        "cover": "https://cover.test/missevan-remote.jpg",
                        "soundIds": ["2002", "2001"],
                        "createTime": "2026.08",
                    }
                },
                upstash=fake_upstash,
            )
            saved = json.loads((Path(tmp) / "missevan.json").read_text(encoding="utf-8"))

        self.assertEqual(stats["free_to_paid"], 1)
        self.assertEqual(stats["sound_ids_changed"], 1)
        self.assertEqual(stats["cover_changed"], 1)
        self.assertEqual(stats["create_time_changed"], 0)
        self.assertEqual(saved["100"]["title"], "并发新标题")
        self.assertTrue(saved["100"]["needpay"])
        self.assertEqual(saved["100"]["cover"], "https://cover.test/missevan-remote.jpg")
        self.assertEqual(saved["100"]["soundIds"], ["2002", "2001"])
        self.assertEqual(saved["100"]["createTime"], "2026.09")
        self.assertEqual([command[0] for command in commands[:4]], ["GET", "EVAL", "GET", "EVAL"])
        self.assertEqual(
            commands[3][3:5],
            ["missevan:info:v2", "missevan:info:meta:v2"],
        )

    def test_remote_info_patch_updates_manbo_cover_and_sound_ids(self) -> None:
        remote = json.dumps(
            {"records": [{"dramaId": "200", "name": "并发标题", "needpay": False, "soundIds": ["old"]}]},
            ensure_ascii=False,
        )
        commands = []
        remote_values = {"manbo:info:v2": remote}

        def fake_upstash(command):
            commands.append(command)
            if command[0] == "GET":
                return remote_values.get(command[1])
            if command[0] == "EVAL" and command[2] == 2:
                remote_values[command[3]] = command[6]
                remote_values[command[4]] = command[7]
                return 1
            if command[0] == "EVAL":
                remote_values[command[3]] = command[5]
                return 1
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            refresh_watch_counts, "MANBO_INFO_PATH", Path(tmp) / "manbo.json"
        ):
            stats = refresh_watch_counts.publish_info_observations(
                "manbo",
                {
                    "200": {
                        "needpay": True,
                        "vipFree": 1,
                        "cover": "https://cover.test/manbo-remote.jpg",
                        "soundIds": ["3002", "3001"],
                        "createTime": "2026.07",
                    }
                },
                upstash=fake_upstash,
            )
            saved = json.loads((Path(tmp) / "manbo.json").read_text(encoding="utf-8"))

        self.assertEqual(stats["free_to_paid"], 1)
        self.assertEqual(stats["membership_changed"], 1)
        self.assertEqual(stats["sound_ids_changed"], 1)
        self.assertEqual(stats["cover_changed"], 1)
        self.assertEqual(stats["create_time_changed"], 1)
        self.assertEqual(saved["records"][0]["name"], "并发标题")
        self.assertEqual(saved["records"][0]["cover"], "https://cover.test/manbo-remote.jpg")
        self.assertEqual(saved["records"][0]["soundIds"], ["3002", "3001"])
        self.assertEqual(saved["records"][0]["createTime"], "2026.07")
        self.assertEqual([command[0] for command in commands[:2]], ["GET", "EVAL"])
        self.assertEqual(
            commands[1][3:5],
            ["manbo:info:v2", "manbo:info:meta:v2"],
        )


if __name__ == "__main__":
    unittest.main()
