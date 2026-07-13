import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import refresh_watch_counts


class RefreshWatchCountsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.download_info_patcher = patch.object(refresh_watch_counts, "download_info_file")
        self.download_info = self.download_info_patcher.start()
        self.addCleanup(self.download_info_patcher.stop)

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
        upload.assert_called_once_with("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH)

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
                call("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH),
                call("manbo", refresh_watch_counts.MANBO_COUNTS_PATH),
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
                call("missevan", refresh_watch_counts.MISSEVAN_COUNTS_PATH),
                call("manbo", refresh_watch_counts.MANBO_COUNTS_PATH),
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


class InfoRefreshTests(unittest.TestCase):
    def test_missevan_418_carries_completed_info_observations(self) -> None:
        store = {
            "100": {"dramaId": 100, "title": "已完成", "soundIds": ["old"]},
            "101": {"dramaId": 101, "title": "触发限流", "soundIds": ["old"]},
        }
        requester = Mock()
        requester.request_json.side_effect = [
            {
                "info": {
                    "drama": {"name": "已完成", "view_count": 10, "pay_type": 2, "price": 199},
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
            {"100": {"needpay": True, "soundIds": ["2001"]}},
        )
        self.assertEqual(store["100"]["soundIds"], ["2001"])

    def test_missevan_watchcount_request_also_collects_pricing_and_sound_ids(self) -> None:
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
                "drama": {"name": "测试", "view_count": 10, "pay_type": 2, "price": 199, "vip": 1},
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
            {"100": {"needpay": True, "is_member": True, "soundIds": ["2002", "2001"]}},
        )
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

    def test_manbo_watchcount_request_also_collects_pricing_and_sound_ids(self) -> None:
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
            {"200": {"needpay": True, "vipFree": 1, "soundIds": ["3002", "3001"]}},
        )
        self.assertEqual(record["soundIds"], ["3002", "3001"])

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
            {"100": {"dramaId": 100, "title": "并发新标题", "needpay": False, "soundIds": ["old"]}},
            ensure_ascii=False,
        )
        commands = []

        def fake_upstash(command):
            commands.append(command)
            if command[0] == "GET":
                return first if sum(1 for item in commands if item[0] == "GET") == 1 else second
            if command[0] == "EVAL":
                return 0 if sum(1 for item in commands if item[0] == "EVAL") == 1 else 1
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            refresh_watch_counts, "MISSEVAN_INFO_PATH", Path(tmp) / "missevan.json"
        ):
            stats = refresh_watch_counts.publish_info_observations(
                "missevan", {"100": {"needpay": True, "soundIds": ["2002", "2001"]}}, upstash=fake_upstash
            )
            saved = json.loads((Path(tmp) / "missevan.json").read_text(encoding="utf-8"))

        self.assertEqual(stats["free_to_paid"], 1)
        self.assertEqual(stats["sound_ids_changed"], 1)
        self.assertEqual(saved["100"]["title"], "并发新标题")
        self.assertTrue(saved["100"]["needpay"])
        self.assertEqual(saved["100"]["soundIds"], ["2002", "2001"])
        self.assertEqual([command[0] for command in commands], ["GET", "EVAL", "GET", "EVAL"])

    def test_remote_info_patch_updates_manbo_sound_ids(self) -> None:
        remote = json.dumps(
            {"records": [{"dramaId": "200", "name": "并发标题", "needpay": False, "soundIds": ["old"]}]},
            ensure_ascii=False,
        )
        commands = []

        def fake_upstash(command):
            commands.append(command)
            return remote if command[0] == "GET" else 1

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            refresh_watch_counts, "MANBO_INFO_PATH", Path(tmp) / "manbo.json"
        ):
            stats = refresh_watch_counts.publish_info_observations(
                "manbo",
                {"200": {"needpay": True, "vipFree": 1, "soundIds": ["3002", "3001"]}},
                upstash=fake_upstash,
            )
            saved = json.loads((Path(tmp) / "manbo.json").read_text(encoding="utf-8"))

        self.assertEqual(stats["free_to_paid"], 1)
        self.assertEqual(stats["membership_changed"], 1)
        self.assertEqual(stats["sound_ids_changed"], 1)
        self.assertEqual(saved["records"][0]["name"], "并发标题")
        self.assertEqual(saved["records"][0]["soundIds"], ["3002", "3001"])
        self.assertEqual([command[0] for command in commands], ["GET", "EVAL"])


if __name__ == "__main__":
    unittest.main()
