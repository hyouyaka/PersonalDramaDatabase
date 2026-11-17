import tempfile
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from pathlib import Path
from unittest.mock import patch

import log_status_server


class LogStatusServerTests(unittest.TestCase):
    def test_detail_filter_pagination_and_partial_last_line(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(log_status_server, "LOG_DIR", Path(tmp)):
            name = "danmaku-details-2026-11-17.jsonl"
            rows = [{"platform": "missevan", "drama_id": "93038", "episode_id": str(n), "complete": n != 2}
                    for n in range(4)]
            path = Path(tmp) / name
            path.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"partial":', encoding="utf-8")
            first = log_status_server.query_episode_details(name, {"drama_id": "93038"}, limit=2)
            second = log_status_server.query_episode_details(name, {"drama_id": "93038"}, first["next_offset"], limit=2)
            self.assertEqual(first["rows"] + second["rows"], rows)
            incomplete = log_status_server.query_episode_details(name, {"complete": "false"})
            self.assertEqual(incomplete["rows"], [rows[2]])
            self.assertIsNone(incomplete["next_offset"])
            path.write_bytes(b"")
            self.assertEqual(log_status_server.query_episode_details(name, {})["rows"], [])
            for bad in ("../" + name, "other.jsonl", "danmaku-details-not-a-date.jsonl"):
                with self.assertRaises(ValueError):
                    log_status_server.safe_detail_path(bad)
            with self.assertRaises(ValueError):
                log_status_server.query_episode_details(name, {}, limit=1000)

    def test_http_query_and_download_return_full_files_beyond_tail_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(log_status_server, "LOG_DIR", Path(tmp)):
            name = "danmaku-details-2026-11-17.jsonl"
            payload = (json.dumps({"platform": "manbo", "drama_id": "600", "episode_title": "测试", "padding": "x" * 210000}, ensure_ascii=False) + "\n").encode("utf-8")
            (Path(tmp) / name).write_bytes(payload)
            main_name = "daily-update-2026-11-17.log"
            (Path(tmp) / main_name).write_bytes(payload)
            server = ThreadingHTTPServer(("127.0.0.1", 0), log_status_server.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for url in (f"/download-details?name={name}", f"/download?name={main_name}"):
                    with urlopen(base + url) as response:
                        self.assertIn("attachment", response.headers["Content-Disposition"])
                        self.assertEqual(response.read(), payload)
                with urlopen(base + f"/api/episode-details?name={name}&drama_id=600") as response:
                    self.assertEqual(json.load(response)["rows"][0]["episode_title"], "测试")
                with urlopen(base + "/api/detail-logs") as response:
                    self.assertEqual(json.load(response)["logs"][0]["name"], name)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

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
