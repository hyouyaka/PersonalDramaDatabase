import unittest

import fetch_ongoing


class FakeRequester:
    def __init__(self, payload):
        self.payload = payload

    def request_json(self, _url):
        return self.payload


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
