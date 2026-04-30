import unittest

import sync_new_drama_ids as sync


class SyncNewDramaIdsReadinessTests(unittest.TestCase):
    def test_missevan_ready_requires_is_member_field(self) -> None:
        base = {
            "title": "猫耳剧",
            "type": 4,
            "catalog": 89,
            "createTime": "2026.04",
            "maincvs": [1, 2],
        }

        self.assertFalse(sync.is_missevan_ready(base))
        self.assertTrue(sync.is_missevan_ready({**base, "is_member": False}))

    def test_manbo_ready_requires_vip_free_field(self) -> None:
        base = {
            "name": "漫播剧",
            "catalog": 1,
            "createTime": "2026.04",
            "genre": "纯爱",
            "mainCvNicknames": ["CV A", "CV B"],
        }

        self.assertFalse(sync.is_manbo_ready(base))
        self.assertTrue(sync.is_manbo_ready({**base, "vipFree": 0}))


if __name__ == "__main__":
    unittest.main()
