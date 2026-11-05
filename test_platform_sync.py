import unittest
from unittest.mock import Mock, patch

import requests

import platform_sync


class MissevanLogicalMainCvTests(unittest.TestCase):
    def test_combines_numeric_and_name_only_main_cvs(self) -> None:
        entries = platform_sync.missevan_main_cv_entries(
            {
                "maincvs": [3946],
                "cvnames": {"3946": "辰朔"},
                "cvroles": {"3946": "秦越"},
                "fallbackCvNames": ["林风", "辰朔"],
                "fallbackCvRoles": {"林风": "季南溪"},
            }
        )

        self.assertEqual(
            entries,
            [
                {
                    "cv_id": 3946,
                    "display_name": "辰朔",
                    "role_name": "秦越",
                    "name_only": False,
                    "unknown": False,
                },
                {
                    "cv_id": None,
                    "display_name": "林风",
                    "role_name": "季南溪",
                    "name_only": True,
                    "unknown": False,
                },
            ],
        )

    def test_unknown_marker_overrides_existing_numeric_main_cvs(self) -> None:
        entries = platform_sync.missevan_main_cv_entries(
            {
                "maincvs": [1, 2],
                "cvnames": {"1": "旧主役甲", "2": "旧主役乙"},
                "cvroles": {"1": "角色甲", "2": "角色乙"},
                "fallbackCvNames": ["主役未知"],
            }
        )

        self.assertEqual(
            entries,
            [
                {
                    "cv_id": None,
                    "display_name": "暂无",
                    "role_name": "",
                    "name_only": True,
                    "unknown": True,
                }
            ],
        )

    def test_replaces_generated_ids_across_missevan_nodes(self) -> None:
        store = {
            "100": {
                "dramaId": 100,
                "maincvs": [331111, 22],
                "cvnames": {"331111": "林风", "22": "其他"},
                "cvroles": {"331111": "角色甲", "22": "角色乙"},
            },
            "101": {"dramaId": 101, "maincvs": [331111], "cvnames": {"331111": "林风"}},
        }

        affected = platform_sync.replace_missevan_main_cv_ids(store, {331111: 1234})

        self.assertEqual(affected, {"100", "101"})
        self.assertEqual(store["100"]["maincvs"], [1234, 22])
        self.assertEqual(store["100"]["cvnames"]["1234"], "林风")
        self.assertEqual(store["100"]["cvroles"]["1234"], "角色甲")

class MissevanPreviewSoundTests(unittest.TestCase):
    def test_first_main_episode_excludes_numbered_pre_and_post_interviews(self) -> None:
        for title in ("前采①", "后采1", "剧组前采第1期", "后采·第一集"):
            with self.subTest(title=title):
                info = {"episodes": {"episode": [
                    {"sound_id": 1, "soundstr": title},
                    {"sound_id": 2, "soundstr": "第一集"},
                ]}}
                self.assertFalse(platform_sync.match_main_episode(title))
                self.assertFalse(platform_sync.match_first_episode(title))
                self.assertEqual(platform_sync.first_main_episode_sound_id(info), "2")

    def test_preview_titles_select_sound_for_structured_main_cvs(self) -> None:
        for title in ("概念PV", "【pv】", "角色KV", "角色kv", "先导篇", "预告", "Preview"):
            with self.subTest(title=title):
                info = {"episodes": {"episode": [
                    {"sound_id": 1, "soundstr": "第一集"},
                    {"sound_id": 2, "soundstr": title},
                ]}}

                self.assertEqual(platform_sync.preview_sound_ids(info), ["2"])
                self.assertEqual(platform_sync.preferred_sound_id(info), ("2", True))

    def test_main_episode_remains_non_preview(self) -> None:
        info = {"episodes": {"episode": [{"sound_id": 1, "soundstr": "第一集"}]}}

        self.assertEqual(platform_sync.preview_sound_ids(info), [])
        self.assertEqual(platform_sync.preferred_sound_id(info), ("1", False))

    def test_theme_song_is_not_selected_as_preview(self) -> None:
        info = {"episodes": {"episode": [
            {"sound_id": 1, "soundstr": "第一集"},
            {"sound_id": 2, "soundstr": "《作品》主题曲"},
            {"sound_id": 3, "soundstr": "概念PV"},
        ]}}

        self.assertFalse(platform_sync.is_missevan_preview_title("《作品》主题曲"))
        self.assertEqual(platform_sync.preview_sound_ids(info), ["3"])
        self.assertEqual(platform_sync.preferred_sound_id(info), ("3", True))


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
    @staticmethod
    def response(status: int) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response.url = "https://www.missevan.com/dramaapi/getdrama?drama_id=39622"
        response._content = b'{"ok": true}'
        return response

    def test_temporary_http_errors_retry_then_return_payload(self) -> None:
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                requester = platform_sync.MissevanRequester(base_delay=0, jitter=0)
                with (
                    patch.object(platform_sync.time, "sleep") as sleep,
                    patch.object(platform_sync.random, "random", return_value=0),
                    patch.object(platform_sync.requests, "get", side_effect=[self.response(status), self.response(200)]) as get,
                    patch("builtins.print") as log,
                ):
                    self.assertEqual(requester.request_json("https://example.test"), {"ok": True})

                self.assertEqual(get.call_count, 2)
                self.assertEqual(requester.request_count, 2)
                self.assertEqual(requester.last_backoff_seconds, 0)
                self.assertEqual([args.args[0] for args in sleep.call_args_list], [0, 12, 0])
                self.assertIn(str(status), log.call_args.args[0])

    def test_persistent_504_stops_after_three_retries_and_preserves_http_error(self) -> None:
        requester = platform_sync.MissevanRequester(base_delay=0, jitter=0)
        response = self.response(504)
        with (
            patch.object(platform_sync.time, "sleep") as sleep,
            patch.object(platform_sync.random, "random", return_value=0),
            patch.object(platform_sync.requests, "get", return_value=response) as get,
            patch("builtins.print"),
        ):
            with self.assertRaises(requests.HTTPError) as raised:
                requester.request_json(response.url)

        self.assertIs(raised.exception.response, response)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(requester.request_count, 4)
        self.assertEqual([args.args[0] for args in sleep.call_args_list], [0, 12, 0, 24, 0, 48, 0])

    def test_permanent_http_errors_do_not_retry_in_requester(self) -> None:
        for status in (400, 401, 403, 404, 501):
            with self.subTest(status=status):
                requester = platform_sync.MissevanRequester(base_delay=0, jitter=0)
                with (
                    patch.object(platform_sync.time, "sleep") as sleep,
                    patch.object(platform_sync.requests, "get", return_value=self.response(status)) as get,
                ):
                    with self.assertRaises(requests.HTTPError):
                        requester.request_json("https://example.test")

                self.assertEqual(get.call_count, 1)
                sleep.assert_called_once_with(0)

    def test_mixed_connection_http_and_418_failures_share_retry_budget(self) -> None:
        requester = platform_sync.MissevanRequester(base_delay=0, jitter=0)
        with (
            patch.object(platform_sync.time, "sleep"),
            patch.object(platform_sync.requests, "get", side_effect=[
                requests.ConnectionError("dns"), self.response(504), self.response(418), self.response(418),
            ]) as get,
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "^HTTP_418$"):
                requester.request_json("https://example.test")

        self.assertEqual(get.call_count, 4)

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
