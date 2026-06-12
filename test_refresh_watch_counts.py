import threading
import unittest
from unittest.mock import Mock, call, patch

import refresh_watch_counts


class RefreshWatchCountsCliTests(unittest.TestCase):
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

    def test_no_upload_leaves_remote_watchcount_keys_untouched(self) -> None:
        with (
            patch.object(refresh_watch_counts, "load_env_file"),
            patch.object(refresh_watch_counts, "sync_remote_watchcount_if_newer"),
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as refresh_manbo,
            patch.object(refresh_watch_counts, "upload_watchcount_file") as upload,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan", "--no-upload"])

        self.assertEqual(result, 0)
        refresh_manbo.assert_not_called()
        upload.assert_not_called()

    def test_main_loads_env_before_remote_watchcount_sync(self) -> None:
        calls = []

        def fake_load_env(path):
            calls.append(("env", path))

        def fake_sync(*args, **kwargs):
            calls.append(("sync", args, kwargs))

        with (
            patch.object(refresh_watch_counts, "load_env_file", side_effect=fake_load_env),
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
        self.assertEqual(calls[1][0], "sync")


if __name__ == "__main__":
    unittest.main()
