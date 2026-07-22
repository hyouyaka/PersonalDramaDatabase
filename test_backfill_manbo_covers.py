import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import backfill_manbo_covers


class BackfillManboCoversTests(unittest.TestCase):
    def write_store(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / "manbo-drama-info.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_downloads_remote_store_fills_missing_covers_and_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"version": 1, "records": []})
            remote_store = {
                "version": 1,
                "records": [
                    {"dramaId": "100", "name": "缺封面"},
                    {"dramaId": "200", "name": "已有封面", "cover": "old-cover"},
                ],
            }
            upstash = Mock(return_value=json.dumps(remote_store, ensure_ascii=False))
            manbo_request = Mock(return_value={"data": {"coverPic": "https://cover.test/100.jpg"}})

            with patch.object(backfill_manbo_covers, "publish_info_v2", return_value={}) as publish:
                stats = backfill_manbo_covers.backfill_manbo_covers(
                    path=path,
                    upstash=upstash,
                    manbo_request=manbo_request,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["records"][0]["cover"], "https://cover.test/100.jpg")
            self.assertEqual(saved["records"][1]["cover"], "old-cover")
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(stats["missing_cover"], 0)
            self.assertTrue(stats["uploaded"])
            self.assertEqual(upstash.call_args_list[0].args[0], ["GET", "manbo:info:v2"])
            publish.assert_called_once()
            self.assertEqual(
                publish.call_args.kwargs["source_encoded"],
                json.dumps(remote_store, ensure_ascii=False),
            )
            manbo_request.assert_called_once_with("https://www.kilamanbo.world/web_manbo/dramaDetail?dramaId=100")

    def test_uses_first_available_cover_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"version": 1, "records": []})
            remote_store = {"version": 1, "records": [{"dramaId": "100", "name": "缺封面"}]}
            upstash = Mock(return_value=json.dumps(remote_store, ensure_ascii=False))
            manbo_request = Mock(return_value={"data": {"coverPic": "", "largePic": "large", "cover": "cover"}})

            with patch.object(backfill_manbo_covers, "publish_info_v2", return_value={}):
                backfill_manbo_covers.backfill_manbo_covers(
                    path=path,
                    upstash=upstash,
                    manbo_request=manbo_request,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["records"][0]["cover"], "large")

    def test_http_404_records_empty_cover_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"version": 1, "records": []})
            remote_store = {
                "version": 1,
                "records": [
                    {"dramaId": "100", "name": "不可访问"},
                    {"dramaId": "200", "name": "可访问"},
                ],
            }
            response = Mock()
            response.status_code = 404
            not_found = requests.HTTPError("404 Client Error", response=response)
            upstash = Mock(return_value=json.dumps(remote_store, ensure_ascii=False))
            manbo_request = Mock(side_effect=[not_found, {"data": {"coverPic": "second"}}])

            with patch.object(backfill_manbo_covers, "publish_info_v2", return_value={}):
                stats = backfill_manbo_covers.backfill_manbo_covers(
                    path=path,
                    upstash=upstash,
                    manbo_request=manbo_request,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["records"][0]["cover"], "")
            self.assertEqual(saved["records"][1]["cover"], "second")
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(stats["missing_cover"], 1)

    def test_no_upload_leaves_remote_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_store(tmp, {"version": 1, "records": []})
            remote_store = {"version": 1, "records": [{"dramaId": "100", "name": "缺封面"}]}
            upstash = Mock(return_value=json.dumps(remote_store, ensure_ascii=False))
            manbo_request = Mock(return_value={"data": {"coverPic": "cover"}})

            stats = backfill_manbo_covers.backfill_manbo_covers(
                path=path,
                upstash=upstash,
                manbo_request=manbo_request,
                upload=False,
            )

            self.assertFalse(stats["uploaded"])
            self.assertEqual(upstash.call_count, 1)


if __name__ == "__main__":
    unittest.main()
