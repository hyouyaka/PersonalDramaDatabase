import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import log_status_server


class LogStatusServerTests(unittest.TestCase):
    def test_safe_log_path_allows_daily_and_weekly_logs_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            latest = log_dir / "daily-update-latest.log"
            with (
                patch.object(log_status_server, "LOG_DIR", log_dir),
                patch.object(log_status_server, "LATEST_LOG", latest),
                patch.object(log_status_server, "LOG_PREFIXES", ("daily-update", "weekly-cv-update")),
            ):
                self.assertEqual(log_status_server.safe_log_path("latest"), latest)
                self.assertEqual(
                    log_status_server.safe_log_path("daily-update-2026-06-10.log"),
                    (log_dir / "daily-update-2026-06-10.log").resolve(),
                )
                self.assertEqual(
                    log_status_server.safe_log_path("weekly-cv-update-2026-06-10.log"),
                    (log_dir / "weekly-cv-update-2026-06-10.log").resolve(),
                )
                with self.assertRaises(ValueError):
                    log_status_server.safe_log_path("other-update-2026-06-10.log")
                with self.assertRaises(ValueError):
                    log_status_server.safe_log_path("../daily-update-2026-06-10.txt")

    def test_list_logs_includes_daily_and_weekly_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "daily-update-latest.log").write_text("daily", encoding="utf-8")
            (log_dir / "weekly-cv-update-latest.log").write_text("weekly", encoding="utf-8")
            (log_dir / "other-update-latest.log").write_text("other", encoding="utf-8")
            with (
                patch.object(log_status_server, "LOG_DIR", log_dir),
                patch.object(log_status_server, "LATEST_LOG", log_dir / "daily-update-latest.log"),
                patch.object(log_status_server, "LOG_PREFIXES", ("daily-update", "weekly-cv-update")),
            ):
                names = {item["name"] for item in log_status_server.list_logs()}

        self.assertEqual(names, {"daily-update-latest.log", "weekly-cv-update-latest.log"})


if __name__ == "__main__":
    unittest.main()
