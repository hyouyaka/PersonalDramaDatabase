import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import backfill_missevan_covers


class BackfillMissevanCoversTests(unittest.TestCase):
    def write_store(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / "missevan-drama-info.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_cover_is_fetched_and_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(
                tmp,
                {
                    "100": {
                        "dramaId": 100,
                        "title": "测试剧",
                        "maincvs": [1],
                        "cvnames": {"1": "CV A"},
                    }
                },
            )
            requester = Mock()
            requester.request_count = 1
            requester.last_backoff_seconds = 0
            requester.request_json.return_value = {"info": {"drama": {"cover": "https://cover.test/a.jpg"}}}
            source_encoded = path.read_text(encoding="utf-8")
            upstash = Mock(return_value=source_encoded)

            with patch.object(backfill_missevan_covers, "publish_info_v2", return_value={}) as publish:
                stats = backfill_missevan_covers.backfill_missevan_covers(
                    path=path,
                    requester=requester,
                    upstash=upstash,
                    upload=True,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["100"]["cover"], "https://cover.test/a.jpg")
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(stats["skipped"], 0)
            self.assertEqual(stats["missing_cover"], 0)
            self.assertTrue(stats["uploaded"])
            requester.request_json.assert_called_once_with(
                "https://www.missevan.com/dramaapi/getdrama?drama_id=100"
            )
            publish.assert_called_once()
            self.assertEqual(
                publish.call_args.kwargs["source_encoded"],
                source_encoded,
            )

    def test_existing_cover_is_skipped_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"100": {"dramaId": 100, "title": "测试剧", "cover": "old"}})
            requester = Mock()
            requester.request_count = 0
            requester.last_backoff_seconds = 0

            stats = backfill_missevan_covers.backfill_missevan_covers(
                path=path,
                requester=requester,
                upstash=Mock(return_value="OK"),
                upload=False,
            )

            requester.request_json.assert_not_called()
            self.assertEqual(stats["processed"], 0)
            self.assertEqual(stats["skipped"], 1)
            self.assertFalse(stats["uploaded"])

    def test_force_refetches_existing_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"100": {"dramaId": 100, "title": "测试剧", "cover": "old"}})
            requester = Mock()
            requester.request_count = 1
            requester.last_backoff_seconds = 0
            requester.request_json.return_value = {"info": {"drama": {"cover": "new"}}}

            stats = backfill_missevan_covers.backfill_missevan_covers(
                path=path,
                requester=requester,
                upstash=Mock(return_value="OK"),
                upload=False,
                force=True,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["100"]["cover"], "new")
            self.assertEqual(stats["processed"], 1)

    def test_418_saves_progress_and_returns_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(
                tmp,
                {
                    "100": {"dramaId": 100, "title": "第一部"},
                    "200": {"dramaId": 200, "title": "第二部"},
                },
            )
            requester = Mock()
            requester.request_count = 2
            requester.last_backoff_seconds = 12
            requester.request_json.side_effect = [
                {"info": {"drama": {"cover": "first"}}},
                RuntimeError("HTTP_418"),
            ]

            result = backfill_missevan_covers.main(
                ["--no-upload"],
                path=path,
                requester=requester,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(saved["100"]["cover"], "first")
            self.assertNotIn("cover", saved["200"])

    def test_403_is_recorded_and_does_not_stop_remaining_covers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(
                tmp,
                {
                    "100": {"dramaId": 100, "title": "不可访问"},
                    "200": {"dramaId": 200, "title": "可访问"},
                },
            )
            response = Mock()
            response.status_code = 403
            forbidden = requests.HTTPError("403 Client Error", response=response)
            requester = Mock()
            requester.request_count = 2
            requester.last_backoff_seconds = 0
            requester.request_json.side_effect = [
                forbidden,
                {"info": {"drama": {"cover": "second"}}},
            ]

            stats = backfill_missevan_covers.backfill_missevan_covers(
                path=path,
                requester=requester,
                upstash=Mock(return_value="OK"),
                upload=False,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(saved["100"]["cover"], "")
            self.assertEqual(saved["200"]["cover"], "second")

    def test_unexpected_error_saves_completed_progress_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(
                tmp,
                {
                    "100": {"dramaId": 100, "title": "第一部"},
                    "200": {"dramaId": 200, "title": "第二部"},
                },
            )
            requester = Mock()
            requester.request_count = 2
            requester.last_backoff_seconds = 0
            requester.request_json.side_effect = [
                {"info": {"drama": {"cover": "first"}}},
                requests.ConnectionError("temporary dns failure"),
            ]

            with self.assertRaises(requests.ConnectionError):
                backfill_missevan_covers.backfill_missevan_covers(
                    path=path,
                    requester=requester,
                    upstash=Mock(return_value="OK"),
                    upload=False,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["100"]["cover"], "first")
            self.assertNotIn("cover", saved["200"])


if __name__ == "__main__":
    unittest.main()
