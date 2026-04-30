import unittest

import refresh_platform_metadata as metadata


class RefreshPlatformMetadataVipTests(unittest.TestCase):
    def test_missevan_base_node_persists_member_flag_from_getdrama(self) -> None:
        node, _ = metadata.build_missevan_base_node(
            {
                "drama": {
                    "id": 101,
                    "name": "猫耳会员剧",
                    "catalog": 89,
                    "author": "作者",
                    "pay_type": 2,
                    "price": 199,
                    "vip": 1,
                },
                "cvs": [
                    {"cv_info": {"id": 1, "name": "CV A"}, "character": "角色A"},
                    {"cv_info": {"id": 2, "name": "CV B"}, "character": "角色B"},
                ],
                "episodes": {"episode": [{"sound_id": 1001}]},
            },
            4,
        )

        self.assertIs(node["is_member"], True)

    def test_missevan_member_flag_falls_back_to_getdramabysound(self) -> None:
        self.assertIs(
            metadata.missevan_is_member_from_infos(
                {"drama": {"vip": None}},
                {"drama": {"vip": 1}},
            ),
            True,
        )

    def test_manbo_record_persists_vip_free_from_detail(self) -> None:
        record = metadata.build_manbo_record(
            {"dramaId": "201", "name": "旧标题"},
            {
                "data": {
                    "title": "漫播会员剧",
                    "catelog": 1,
                    "radioDramaCategoryResp": {"name": "广播剧"},
                    "categoryLabels": [{"name": "纯爱"}],
                    "vipFree": 1,
                    "price": 0,
                    "memberPrice": 0,
                    "setRespList": [{"setTitle": "第一集", "createTime": 1760000000000}],
                    "cvRespList": [
                        {
                            "dramaRoleType": 2,
                            "cvResp": {"id": 10, "nickname": "CV A"},
                            "cvNickname": "CV A",
                            "role": "角色A",
                        },
                        {
                            "dramaRoleType": 2,
                            "cvResp": {"id": 11, "nickname": "CV B"},
                            "cvNickname": "CV B",
                            "role": "角色B",
                        },
                    ],
                }
            },
            {},
        )

        self.assertEqual(record["vipFree"], 1)


if __name__ == "__main__":
    unittest.main()
