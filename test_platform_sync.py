import unittest
from unittest.mock import Mock, patch

import requests

import platform_sync


class ManboCvEntryTests(unittest.TestCase):
    def test_build_manbo_cv_entries_uses_top_level_cv_id_when_profile_id_missing(self) -> None:
        entries = platform_sync.build_manbo_cv_entries(
            {
                "cvRespList": [
                    {
                        "dramaRoleType": 2,
                        "cvId": 2178908802986803500,
                        "cvNickname": "兰斯洛特",
                        "role": "饰:黎清",
                    }
                ]
            }
        )

        self.assertEqual(
            entries,
            [
                {
                    "index": 0,
                    "cv_id": 2178908802986803500,
                    "display_name": "兰斯洛特",
                    "role_name": "黎清",
                    "raw_role_name": "饰:黎清",
                }
            ],
        )


class MissevanRequesterRetryTests(unittest.TestCase):
    def test_request_json_retries_transient_connection_errors(self) -> None:
        requester = platform_sync.MissevanRequester(base_delay=0, jitter=0, max_retries=2)
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True}

        with (
            patch.object(platform_sync.time, "sleep"),
            patch.object(platform_sync.requests, "get", side_effect=[requests.ConnectionError("dns"), response]) as get,
        ):
            payload = requester.request_json("https://example.test")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(requester.request_count, 2)


if __name__ == "__main__":
    unittest.main()
