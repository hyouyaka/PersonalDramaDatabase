import unittest
from datetime import datetime, timezone

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


class ManboOngoingPayTypeTests(unittest.TestCase):
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
