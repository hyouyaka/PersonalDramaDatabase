import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import fetch_ongoing


class FakeRequester:
    def __init__(self, payload):
        self.payload = payload

    def request_json(self, _url):
        return self.payload


class FakeSequenceRequester:
    def __init__(self, payloads_by_sound_id):
        self.payloads_by_sound_id = payloads_by_sound_id
        self.urls = []

    def request_json(self, url):
        self.urls.append(url)
        sound_id = url.rsplit("=", 1)[-1]
        return self.payloads_by_sound_id[sound_id]


class FakeUrlRequester:
    def __init__(self, payloads_by_url):
        self.payloads_by_url = payloads_by_url
        self.urls = []

    def request_json(self, url):
        self.urls.append(url)
        payload = self.payloads_by_url.get(url)
        if isinstance(payload, Exception):
            raise payload
        return payload


def make_sound_page(start: int, end: int) -> str:
    return "".join(
        (
            f'<a href="/sound/{sound_id}">sound</a>'
            f'<div class="vw-frontsound-viewcount floatleft">100</div>'
            f'<div class="vw-frontsound-commentcount floatleft">20</div>'
        )
        for sound_id in range(start, end + 1)
    )


def sound_payload(create_time):
    return {"success": True, "info": {"sound": {"create_time": create_time}}}


class MissevanDailySoundWindowTests(unittest.TestCase):
    def test_timestamp_boundary_uses_beijing_date_only(self):
        inside = fetch_ongoing.missevan_timestamp_to_beijing_date(1779984000)
        outside = fetch_ongoing.missevan_timestamp_to_beijing_date(1779983999)

        self.assertEqual(inside.isoformat(), "2026-05-29")
        self.assertEqual(outside.isoformat(), "2026-05-28")

    def test_stops_after_initial_batch_when_last_sound_is_older_than_seven_beijing_dates(self):
        requester = FakeSequenceRequester({"20": sound_payload(1779983999)})

        sound_ids = fetch_ongoing.collect_missevan_daily_sound_ids(
            lambda _page: make_sound_page(1, 40),
            requester=requester,
            now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(sound_ids, [str(i) for i in range(1, 21)])
        self.assertEqual(len(requester.urls), 1)
        self.assertIn("soundid=20", requester.urls[0])

    def test_extends_by_ten_until_batch_last_sound_is_older_than_seven_beijing_dates(self):
        requester = FakeSequenceRequester(
            {
                "20": sound_payload(1779984000),
                "30": sound_payload(1779983999),
            }
        )

        sound_ids = fetch_ongoing.collect_missevan_daily_sound_ids(
            lambda _page: make_sound_page(1, 40),
            requester=requester,
            now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(sound_ids, [str(i) for i in range(1, 31)])
        self.assertEqual(len(requester.urls), 2)
        self.assertIn("soundid=20", requester.urls[0])
        self.assertIn("soundid=30", requester.urls[1])

    def test_caps_daily_sound_collection_at_max_sound_ids(self):
        payloads = {str(sound_id): sound_payload(1779984000) for sound_id in range(20, 121, 10)}
        requester = FakeSequenceRequester(payloads)

        sound_ids = fetch_ongoing.collect_missevan_daily_sound_ids(
            lambda _page: make_sound_page(1, 140),
            requester=requester,
            now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(sound_ids, [str(i) for i in range(1, 121)])
        self.assertEqual(len(requester.urls), 11)
        self.assertIn("soundid=120", requester.urls[-1])

    def test_missing_create_time_stops_expansion_after_current_batch(self):
        requester = FakeSequenceRequester({"20": {"success": True, "info": {"sound": {}}}})

        sound_ids = fetch_ongoing.collect_missevan_daily_sound_ids(
            lambda _page: make_sound_page(1, 40),
            requester=requester,
            now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(sound_ids, [str(i) for i in range(1, 21)])


class MissevanOngoingPayTypeTests(unittest.TestCase):
    def test_timeline_keeps_pay_type_1_and_2_only_skips_0(self):
        payload = {
            "info": [
                {
                    "dramas": [
                        {"id": 100, "pay_type": 0},
                        {"id": 101, "pay_type": 1},
                        {"id": 102, "pay_type": 2},
                    ]
                }
            ]
        }

        records = fetch_ongoing.parse_missevan_timeline_weekly_records(payload)

        self.assertEqual(
            records,
            [
                {"dramaId": "101", "updateType": "weekly"},
                {"dramaId": "102", "updateType": "weekly"},
            ],
        )

    def test_summerdrama_fallback_keeps_pay_type_1_and_2_only_skips_0(self):
        requester = FakeRequester(
            {
                "info": [
                    [
                        {"id": 200, "pay_type": 0},
                        {"id": 201, "pay_type": 1},
                        {"id": 202, "pay_type": 2},
                    ]
                ]
            }
        )

        records = fetch_ongoing.fetch_missevan_weekly_records(requester, fetch_timeline=lambda: None)

        self.assertEqual(
            records,
            [
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "202", "updateType": "weekly"},
            ],
        )


class MissevanWeekdayCacheTests(unittest.TestCase):
    def test_today_empty_timeline_bucket_uses_same_weekday_cache(self):
        payload = {
            "info": [
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        cache = {
            "version": 1,
            "platform": "missevan",
            "updatedAt": "2026-06-10T04:00:00+00:00",
            "buckets": {
                "3": {
                    "weekday": 3,
                    "dateWeek": "三",
                    "observedAt": "2026-06-10T04:00:00+00:00",
                    "records": {
                        "93038": {"dramaId": "93038", "updateType": "weekly"},
                    },
                }
            },
        }

        records = fetch_ongoing.parse_missevan_timeline_weekly_records(
            payload,
            weekday_cache=cache,
            now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(records, [{"dramaId": "93038", "updateType": "weekly"}])

    def test_today_non_empty_timeline_bucket_does_not_inject_cache(self):
        payload = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [{"id": 101, "pay_type": 2}],
                },
            ]
        }
        cache = {
            "buckets": {
                "3": {
                    "observedAt": "2026-06-10T04:00:00+00:00",
                    "records": {
                        "93038": {"dramaId": "93038", "updateType": "weekly"},
                    },
                }
            }
        }

        records = fetch_ongoing.parse_missevan_timeline_weekly_records(
            payload,
            weekday_cache=cache,
            now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(records, [{"dramaId": "101", "updateType": "weekly"}])

    def test_non_today_empty_timeline_bucket_does_not_use_cache(self):
        payload = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": []},
            ]
        }
        cache = {
            "buckets": {
                "2": {
                    "observedAt": "2026-06-09T04:00:00+00:00",
                    "records": {
                        "222": {"dramaId": "222", "updateType": "weekly"},
                    },
                }
            }
        }

        records = fetch_ongoing.parse_missevan_timeline_weekly_records(
            payload,
            weekday_cache=cache,
            now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(records, [])

    def test_non_empty_timeline_bucket_updates_weekday_cache(self):
        payload = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [
                        {"id": 100, "pay_type": 0},
                        {"id": 101, "pay_type": 2},
                    ],
                },
            ]
        }

        cache = fetch_ongoing.build_missevan_weekday_cache_from_timeline(
            payload,
            existing_cache={},
            now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(
            cache["buckets"]["3"]["records"],
            {"101": {"dramaId": "101", "updateType": "weekly"}},
        )
        self.assertEqual(cache["buckets"]["3"]["dateWeek"], "三")

    def test_cache_bucket_older_than_fourteen_days_is_ignored(self):
        payload = {
            "info": [
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        cache = {
            "buckets": {
                "3": {
                    "observedAt": "2026-06-02T00:59:59+00:00",
                    "records": {
                        "93038": {"dramaId": "93038", "updateType": "weekly"},
                    },
                }
            }
        }

        records = fetch_ongoing.parse_missevan_timeline_weekly_records(
            payload,
            weekday_cache=cache,
            now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(records, [])

    def test_seed_missevan_weekday_cache_writes_seed_bucket(self):
        commands = []

        def fake_upstash(command):
            commands.append(command)
            if command[:2] == ["GET", fetch_ongoing.MISSEVAN_WEEKDAY_CACHE_KEY]:
                return None
            return "OK"

        with patch("builtins.print"):
            fetch_ongoing.seed_missevan_weekday_cache(
                ["3:93038"],
                upstash=fake_upstash,
                now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
            )

        written = commands[-1]
        self.assertEqual(written[:2], ["SET", fetch_ongoing.MISSEVAN_WEEKDAY_CACHE_KEY])
        payload = fetch_ongoing.json.loads(written[2])
        self.assertEqual(
            payload["buckets"]["3"]["records"],
            {"93038": {"dramaId": "93038", "updateType": "weekly"}},
        )

    def test_seed_missevan_weekday_cache_merges_existing_weekday_records(self):
        commands = []
        existing = {
            "version": 1,
            "platform": "missevan",
            "updatedAt": "2026-06-10T04:00:00+00:00",
            "buckets": {
                "3": {
                    "weekday": 3,
                    "dateWeek": "三",
                    "observedAt": "2026-06-10T04:00:00+00:00",
                    "records": {
                        "111": {"dramaId": "111", "updateType": "weekly"},
                    },
                }
            },
        }

        def fake_upstash(command):
            commands.append(command)
            if command[:2] == ["GET", fetch_ongoing.MISSEVAN_WEEKDAY_CACHE_KEY]:
                return fetch_ongoing.json.dumps(existing, ensure_ascii=False)
            return "OK"

        with patch("builtins.print"):
            fetch_ongoing.seed_missevan_weekday_cache(
                ["3:93038"],
                upstash=fake_upstash,
                now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
            )

        payload = fetch_ongoing.json.loads(commands[-1][2])
        self.assertEqual(
            payload["buckets"]["3"]["records"],
            {
                "111": {"dramaId": "111", "updateType": "weekly"},
                "93038": {"dramaId": "93038", "updateType": "weekly"},
            },
        )

    def test_summerdrama_today_bucket_uses_beijing_weekday_offset(self):
        payload = {
            "info": [
                [{"id": 201, "pay_type": 2}],
                [{"id": 93038, "pay_type": 2}],
                [{"id": 203, "pay_type": 2}],
            ]
        }

        records = fetch_ongoing.parse_missevan_summerdrama_weekday_records(
            payload,
            3,
            current_weekday=3,
        )

        self.assertEqual(records, [{"dramaId": "93038", "updateType": "weekly"}])

    def test_timeline_records_merge_with_all_summerdrama_records(self):
        timeline = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [{"id": 101, "pay_type": 2}],
                },
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 202, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "101", "updateType": "weekly"},
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "202", "updateType": "weekly"},
            ],
        )
        self.assertEqual(requester.urls, [fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL])

    def test_timeline_records_take_precedence_when_summerdrama_has_duplicate_drama_id(self):
        timeline = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [{"id": 101, "pay_type": 2}],
                },
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [
                            {"id": 101, "pay_type": 2},
                            {"id": 202, "pay_type": 2},
                        ],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "101", "updateType": "weekly"},
                {"dramaId": "202", "updateType": "weekly"},
            ],
        )

    def test_timeline_success_continues_when_summerdrama_request_fails_and_today_is_not_empty(self):
        timeline = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [{"id": 101, "pay_type": 2}],
                },
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: RuntimeError("summerdrama down"),
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(records, [{"dramaId": "101", "updateType": "weekly"}])

    def test_today_empty_timeline_uses_summerdrama_when_cache_load_fails(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": [{"id": 301, "pay_type": 2}]},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 93038, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", side_effect=RuntimeError("cache down")):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "301", "updateType": "weekly"},
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "93038", "updateType": "weekly"},
            ],
        )
        self.assertEqual(requester.urls, [fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL])

    def test_today_empty_timeline_merges_cache_and_all_summerdrama_records(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": [{"id": 301, "pay_type": 2}]},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        cache = {
            "buckets": {
                "3": {
                    "observedAt": "2026-06-10T04:00:00+00:00",
                    "records": {
                        "93038": {"dramaId": "93038", "updateType": "weekly"},
                    },
                }
            }
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 93038, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=cache):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "301", "updateType": "weekly"},
                {"dramaId": "93038", "updateType": "weekly"},
                {"dramaId": "201", "updateType": "weekly"},
            ],
        )

    def test_today_empty_timeline_uses_summerdrama_when_cache_is_stale(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": [{"id": 301, "pay_type": 2}]},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        stale_cache = {
            "buckets": {
                "3": {
                    "observedAt": "2026-06-02T00:59:59+00:00",
                    "records": {
                        "111": {"dramaId": "111", "updateType": "weekly"},
                    },
                }
            }
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 93038, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=stale_cache):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "301", "updateType": "weekly"},
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "93038", "updateType": "weekly"},
            ],
        )

    def test_today_non_empty_timeline_fetches_and_merges_summerdrama(self):
        timeline = {
            "info": [
                {
                    "date_week": "三",
                    "date_day": 17,
                    "is_today": 1,
                    "dramas": [{"id": 101, "pay_type": 2}],
                },
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "101", "updateType": "weekly"},
                {"dramaId": "201", "updateType": "weekly"},
            ],
        )
        self.assertEqual(requester.urls, [fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL])

    def test_today_empty_timeline_raises_when_summerdrama_has_no_today_records(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": [{"id": 301, "pay_type": 2}]},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [],
                    ]
                },
            }
        )

        with (
            patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None),
            self.assertRaisesRegex(RuntimeError, "today timeline bucket is empty"),
        ):
            fetch_ongoing.fetch_missevan_weekly_records(
                requester,
                fetch_timeline=lambda: timeline,
                sync_weekday_cache=False,
                now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
            )

    def test_today_empty_timeline_raises_when_summerdrama_request_fails(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": [{"id": 301, "pay_type": 2}]},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: RuntimeError("summerdrama down"),
            }
        )

        with (
            patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None),
            self.assertRaisesRegex(RuntimeError, "summerdrama fallback failed"),
        ):
            fetch_ongoing.fetch_missevan_weekly_records(
                requester,
                fetch_timeline=lambda: timeline,
                sync_weekday_cache=False,
                now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
            )

    def test_all_empty_timeline_falls_back_to_all_summerdrama_records(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": []},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 93038, "pay_type": 2}],
                    ]
                },
            }
        )

        with patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None):
            with patch("builtins.print"):
                records = fetch_ongoing.fetch_missevan_weekly_records(
                    requester,
                    fetch_timeline=lambda: timeline,
                    sync_weekday_cache=False,
                    now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(
            records,
            [
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "93038", "updateType": "weekly"},
            ],
        )

    def test_all_empty_timeline_does_not_save_empty_weekday_cache(self):
        timeline = {
            "info": [
                {"date_week": "二", "date_day": 16, "is_today": 0, "dramas": []},
                {"date_week": "三", "date_day": 17, "is_today": 1, "dramas": []},
            ]
        }
        requester = FakeUrlRequester(
            {
                fetch_ongoing.MISSEVAN_SUMMERDRAMA_URL: {
                    "info": [
                        [{"id": 201, "pay_type": 2}],
                        [{"id": 93038, "pay_type": 2}],
                    ]
                },
            }
        )

        with (
            patch.object(fetch_ongoing, "load_missevan_weekday_cache", return_value=None),
            patch.object(fetch_ongoing, "save_missevan_weekday_cache") as save_cache,
            patch("builtins.print"),
        ):
            records = fetch_ongoing.fetch_missevan_weekly_records(
                requester,
                fetch_timeline=lambda: timeline,
                sync_weekday_cache=True,
                now=datetime(2026, 6, 17, 1, tzinfo=timezone.utc),
            )

        self.assertEqual(
            records,
            [
                {"dramaId": "201", "updateType": "weekly"},
                {"dramaId": "93038", "updateType": "weekly"},
            ],
        )
        save_cache.assert_not_called()


class ManboOngoingPayTypeTests(unittest.TestCase):
    def test_does_not_treat_timeline_item_id_as_drama_id(self):
        item = {
            "id": 201,
            "updateSetTitle": "01",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "title": "缺少剧集ID的更新时间表条目",
                "category": 1,
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(records, {})

    def test_uses_numeric_radio_drama_id_when_string_field_is_missing(self):
        item = {
            "id": 201,
            "updateSetTitle": "01",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaId": 2238948515889807453,
                "title": "普通付费剧",
                "category": 1,
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(
            records,
            {
                "2238948515889807453": {
                    "dramaId": "2238948515889807453",
                    "updateType": "weekly",
                }
            },
        )

    def test_keeps_price_positive_item_even_when_member_price_is_zero(self):
        item = {
            "updateSetTitle": "🎂·陈挽day·04",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaIdStr": "333",
                "title": "普通付费剧",
                "category": 1,
                "price": 2340,
                "memberPrice": 0,
                "vipFree": 0,
                "categoryLabels": [{"name": "纯爱"}],
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(
            records,
            {
                "333": {
                    "dramaId": "333",
                    "updateType": "weekly",
                },
            },
        )


class ManboOngoingCatalogTests(unittest.TestCase):
    def test_uses_title_override_when_time_page_category_is_stale(self):
        item = {
            "updateSetTitle": "🎂·陈挽day·04",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaIdStr": "1955825512983036032",
                "title": "奇洛李维斯回信",
                "category": 1,
                "price": 2340,
                "memberPrice": 0,
                "vipFree": 0,
                "categoryLabels": [{"name": "纯爱"}],
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(
            records,
            {
                "1955825512983036032": {
                    "dramaId": "1955825512983036032",
                    "updateType": "daily",
                },
            },
        )

    def test_uses_time_page_category_for_regular_weekly_item(self):
        item = {
            "updateSetTitle": "01",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaIdStr": "111",
                "title": "普通广播剧",
                "category": 1,
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(records, {"111": {"dramaId": "111", "updateType": "weekly"}})

    def test_uses_time_page_category_for_regular_daily_item(self):
        item = {
            "updateSetTitle": "01",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaIdStr": "222",
                "title": "普通有声剧",
                "category": 5,
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(records, {"222": {"dramaId": "222", "updateType": "daily"}})

    def test_skips_unrecognized_time_page_category(self):
        item = {
            "updateSetTitle": "01",
            "workUpdateTimeFormat": "18:00",
            "radioDramaResp": {
                "radioDramaIdStr": "333",
                "title": "普通未知分类",
                "category": 9,
                "price": 100,
                "memberPrice": 100,
                "vipFree": 0,
            },
        }

        records = fetch_ongoing.collect_manbo_records_from_items([item])

        self.assertEqual(records, {})


if __name__ == "__main__":
    unittest.main()
