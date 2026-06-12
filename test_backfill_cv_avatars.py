import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

import backfill_cv_avatars


class BackfillCvAvatarsTests(unittest.TestCase):
    def write_map(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / "missevan&manbo-cvid-map.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missevan_avatar_wins_over_manbo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(
                tmp,
                {"CV A": {"displayName": "CV A", "missevanCvId": 11, "manboCvId": 22}},
            )
            requester = Mock()
            requester.request_count = 1
            requester.last_backoff_seconds = 0
            requester.request_json.return_value = {"info": {"cv": {"icon": "https://missevan.test/a.jpg"}}}
            manbo_request = Mock(return_value={"b": {"userResp": {"headPortraitUrl": "https://img.kilamanbo.com/a.png?t=0"}}})

            stats = backfill_cv_avatars.backfill_cv_avatars(
                path=path,
                requester=requester,
                manbo_request=manbo_request,
                upstash=Mock(return_value="OK"),
                upload=False,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["CV A"]["avatar"], "https://missevan.test/a.jpg")
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(stats["missing_avatar"], 0)
            manbo_request.assert_not_called()

    def test_manbo_fallback_strips_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(tmp, {"CV B": {"displayName": "CV B", "missevanCvId": None, "manboCvId": 22}})
            requester = Mock()
            requester.request_count = 0
            requester.last_backoff_seconds = 0
            manbo_request = Mock(return_value={"b": {"userResp": {"headPortraitUrl": "https://img.kilamanbo.com/a.png?t=0"}}})

            stats = backfill_cv_avatars.backfill_cv_avatars(
                path=path,
                requester=requester,
                manbo_request=manbo_request,
                upstash=Mock(return_value="OK"),
                upload=False,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["CV B"]["avatar"], "https://img.kilamanbo.com/a.png")
            self.assertEqual(stats["processed"], 1)

    def test_force_refetches_existing_avatar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(
                tmp,
                {"CV A": {"displayName": "CV A", "missevanCvId": 11, "avatar": "old-avatar"}},
            )
            requester = Mock()
            requester.request_count = 1
            requester.last_backoff_seconds = 0
            requester.request_json.return_value = {"info": {"cv": {"icon": "new-avatar"}}}

            stats = backfill_cv_avatars.backfill_cv_avatars(
                path=path,
                requester=requester,
                manbo_request=Mock(),
                upstash=Mock(return_value="OK"),
                upload=False,
                force=True,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["CV A"]["avatar"], "new-avatar")
            self.assertEqual(stats["processed"], 1)
            requester.request_json.assert_called_once()

    def test_missevan_404_falls_back_to_manbo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(tmp, {"CV C": {"displayName": "CV C", "missevanCvId": 11, "manboCvId": 22}})
            response = Mock()
            response.status_code = 404
            not_found = requests.HTTPError("404", response=response)
            requester = Mock()
            requester.request_count = 1
            requester.last_backoff_seconds = 0
            requester.request_json.side_effect = not_found
            manbo_request = Mock(return_value={"b": {"userResp": {"headPortraitUrl": "https://img.kilamanbo.com/c.png?t=0"}}})

            stats = backfill_cv_avatars.backfill_cv_avatars(
                path=path,
                requester=requester,
                manbo_request=manbo_request,
                upstash=Mock(return_value="OK"),
                upload=False,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["CV C"]["avatar"], "https://img.kilamanbo.com/c.png")
            self.assertEqual(stats["fallback_to_manbo"], 1)

    def test_418_saves_progress_and_returns_exit_2_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(
                tmp,
                {
                    "CV A": {"displayName": "CV A", "missevanCvId": 11},
                    "CV B": {"displayName": "CV B", "missevanCvId": 12},
                },
            )
            requester = Mock()
            requester.request_count = 2
            requester.last_backoff_seconds = 12
            requester.request_json.side_effect = [
                {"info": {"cv": {"icon": "https://missevan.test/a.jpg"}}},
                RuntimeError("HTTP_418"),
            ]
            upstash = Mock(return_value="OK")

            result = backfill_cv_avatars.main(
                ["--no-upload"],
                path=path,
                requester=requester,
                manbo_request=Mock(),
                upstash=upstash,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(saved["CV A"]["avatar"], "https://missevan.test/a.jpg")
            self.assertNotIn("avatar", saved["CV B"])
            upstash.assert_not_called()

    def test_default_uploads_cvid_map_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(tmp, {"CV A": {"displayName": "CV A", "manboCvId": 22}})
            requester = Mock()
            requester.request_count = 0
            requester.last_backoff_seconds = 0
            upstash = Mock(return_value="OK")

            backfill_cv_avatars.backfill_cv_avatars(
                path=path,
                requester=requester,
                manbo_request=Mock(return_value={"b": {"userResp": {"headPortraitUrl": "https://img.kilamanbo.com/a.png?t=0"}}}),
                upstash=upstash,
                upload=True,
            )

            self.assertEqual(upstash.call_args.args[0][:2], ["SET", "cvid-map:v1"])

    def test_download_remote_cvid_map_preserves_local_avatar_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_map(
                tmp,
                {
                    "CV A": {"displayName": "CV A", "missevanCvId": 11, "avatar": "local-avatar"},
                    "CV B": {"displayName": "CV B", "manboCvId": 22, "avatar": ""},
                },
            )
            remote = {
                "CV A": {"displayName": "CV A", "missevanCvId": 11, "avatar": ""},
                "CV B": {"displayName": "CV B", "manboCvId": 22, "avatar": "remote-avatar"},
            }
            upstash = Mock(return_value=json.dumps(remote, ensure_ascii=False))

            payload = backfill_cv_avatars.download_remote_cvid_map(path, upstash=upstash)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["CV A"]["avatar"], "local-avatar")
            self.assertEqual(saved["CV A"]["avatar"], "local-avatar")
            self.assertEqual(saved["CV B"]["avatar"], "remote-avatar")


if __name__ == "__main__":
    unittest.main()
