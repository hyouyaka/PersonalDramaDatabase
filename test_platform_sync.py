import unittest

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


if __name__ == "__main__":
    unittest.main()
