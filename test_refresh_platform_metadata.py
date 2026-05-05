import unittest
from unittest.mock import patch

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

    def test_extract_missevan_intro_cv_candidates_skips_narrator_and_strips_suffixes(self) -> None:
        intro = (
            "<p>=配音组=</p>"
            "<p>旁白/报幕：孔喆 @孔喆4396</p>"
            "<p>路小凡：李翰林@是翰不是憨憨</p>"
            "<p>贝律清：轩Zone@轩ZONE【声之翼】</p>"
            "<p>贝沫沙：朝阳@de朝阳</p>"
            "<p>参与配音：阿步@阿步灬Albo</p>"
        )

        candidates = metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "路小凡", "display_name": "李翰林"},
                {"role_name": "贝律清", "display_name": "轩Zone"},
            ],
        )

    def test_extract_missevan_intro_cv_candidates_accepts_cast_and_cv_section_headers(self) -> None:
        cast_intro = (
            "<p>=CAST=</p>"
            "<p>路小凡：李翰林@是翰不是憨憨</p>"
            "<p>贝律清：轩Zone@轩ZONE【声之翼】</p>"
        )
        cv_intro = (
            "<p>CV</p>"
            "<p>江停：阿杰</p>"
            "<p>严峫：杨天翔</p>"
        )

        self.assertEqual(
            metadata.extract_missevan_intro_cv_candidates(cast_intro),
            [
                {"role_name": "路小凡", "display_name": "李翰林"},
                {"role_name": "贝律清", "display_name": "轩Zone"},
            ],
        )
        self.assertEqual(
            metadata.extract_missevan_intro_cv_candidates(cv_intro),
            [
                {"role_name": "江停", "display_name": "阿杰"},
                {"role_name": "严峫", "display_name": "杨天翔"},
            ],
        )

    def test_extract_missevan_intro_cv_candidates_stops_at_colon_section_headers(self) -> None:
        intro = (
            "<p>配音</p>"
            "<p>路小凡：李翰林@是翰不是憨憨</p>"
            "<p>制作组：</p>"
            "<p>策划：不应识别</p>"
        )

        self.assertEqual(
            metadata.extract_missevan_intro_cv_candidates(intro),
            [{"role_name": "路小凡", "display_name": "李翰林"}],
        )

    def test_extract_missevan_intro_cv_candidates_stops_at_bare_staff_header(self) -> None:
        intro = (
            "<p>配音</p>"
            "<p>路小凡：李翰林@是翰不是憨憨</p>"
            "<p>Staff:</p>"
            "<p>策划：不应识别</p>"
        )

        self.assertEqual(
            metadata.extract_missevan_intro_cv_candidates(intro),
            [{"role_name": "路小凡", "display_name": "李翰林"}],
        )

    def test_extract_missevan_intro_cv_candidates_ignores_inline_cast_and_cv_labels(self) -> None:
        intro = (
            "<p>CAST：李翰林</p>"
            "<p>CV：轩Zone</p>"
            "<p>路小凡：李翰林@是翰不是憨憨</p>"
        )

        self.assertEqual(metadata.extract_missevan_intro_cv_candidates(intro), [])

    def test_apply_missevan_intro_cv_fallback_uses_existing_map_id(self) -> None:
        node = {"maincvs": [], "cvnames": {}, "cvroles": {}}
        candidates = [{"role_name": "路小凡", "display_name": "李翰林"}]

        updated = metadata.apply_missevan_intro_cv_fallback(
            node,
            "101",
            candidates,
            combined_map={"李翰林": {"displayName": "李翰林", "missevanCvId": 4770}},
            search_cv=lambda name: self.fail(f"unexpected search for {name}"),
            update_combined_map=False,
        )

        self.assertEqual(updated["maincvs"], [4770])
        self.assertEqual(updated["cvnames"], {"4770": "李翰林"})
        self.assertEqual(updated["cvroles"], {"4770": "路小凡"})
        self.assertNotIn("fallbackCvNames", updated)

    def test_apply_missevan_intro_cv_fallback_uses_unique_search_hit_and_updates_map(self) -> None:
        node = {"maincvs": [], "cvnames": {}, "cvroles": {}}
        candidates = [{"role_name": "路小凡", "display_name": "李翰林"}]
        combined_map = {}

        with patch.object(metadata, "save_combined_map") as save_map:
            updated = metadata.apply_missevan_intro_cv_fallback(
                node,
                "101",
                candidates,
                combined_map=combined_map,
                search_cv=lambda name: {"cv_id": 4770, "display_name": name},
            )

        self.assertEqual(updated["maincvs"], [4770])
        self.assertEqual(combined_map["李翰林"]["missevanCvId"], 4770)
        save_map.assert_called_once_with(combined_map)

    def test_apply_missevan_intro_cv_fallback_keeps_unresolved_names_out_of_maincvs(self) -> None:
        node = {"maincvs": [], "cvnames": {}, "cvroles": {}}
        candidates = [{"role_name": "路小凡", "display_name": "未知CV"}]

        updated = metadata.apply_missevan_intro_cv_fallback(
            node,
            "101",
            candidates,
            combined_map={},
            search_cv=lambda name: None,
            update_combined_map=False,
        )

        self.assertEqual(updated["maincvs"], [])
        self.assertEqual(updated["cvnames"], {})
        self.assertEqual(updated["cvroles"], {})
        self.assertEqual(updated["fallbackCvNames"], ["未知CV"])
        self.assertEqual(updated["fallbackCvRoles"], {"未知CV": "路小凡"})


if __name__ == "__main__":
    unittest.main()
