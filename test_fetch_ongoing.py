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


if __name__ == "__main__":
    unittest.main()
