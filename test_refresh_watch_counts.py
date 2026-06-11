import threading
import unittest
from unittest.mock import Mock, patch

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
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=fake_missevan),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", side_effect=fake_manbo),
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main([])

        self.assertEqual(result, 0)
        self.assertTrue(overlap["value"])

    def test_platform_missevan_only_does_not_refresh_manbo(self) -> None:
        with (
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 0)
        missevan.assert_called_once_with(target_ids=None)
        manbo.assert_not_called()

    def test_explicit_missevan_ids_do_not_trigger_manbo_full_refresh(self) -> None:
        with (
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ) as missevan,
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--missevan", "100"])

        self.assertEqual(result, 0)
        missevan.assert_called_once_with(target_ids={"100"})
        manbo.assert_not_called()

    def test_missevan_418_still_returns_exit_2(self) -> None:
        with (
            patch.object(refresh_watch_counts, "refresh_missevan_watch_counts", side_effect=RuntimeError("HTTP_418")),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts") as manbo,
            patch("builtins.print"),
        ):
            result = refresh_watch_counts.main(["--platform", "missevan"])

        self.assertEqual(result, 2)
        manbo.assert_not_called()

    def test_parallel_manbo_runtime_error_is_not_reported_as_missevan_418(self) -> None:
        with (
            patch.object(
                refresh_watch_counts,
                "refresh_missevan_watch_counts",
                return_value={"processed": 1, "skipped": 0, "archived": 0, "request_count": 1, "last_backoff_seconds": 0},
            ),
            patch.object(refresh_watch_counts, "refresh_manbo_watch_counts", side_effect=RuntimeError("manbo failed")),
            patch("builtins.print"),
        ):
            with self.assertRaises(RuntimeError):
                refresh_watch_counts.main([])


if __name__ == "__main__":
    unittest.main()
