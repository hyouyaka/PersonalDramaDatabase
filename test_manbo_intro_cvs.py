import unittest
from copy import deepcopy
from unittest.mock import Mock, patch, call

import build_cv_ranks
import cvid_map_tools
import platform_sync
import rebuild_sqlite_from_libraries as rebuild
import refresh_platform_metadata as metadata


DRAMA_ID = "2256798215288389733"
# Captured from the actual Manbo description; tests never depend on a live response.
DESCRIPTION = (
    "在楚淮予身上，我永远输得起！\n\n"
    "磐歌原著，善读&扶光工作室&一色联合出品，梵叶（旁白）、一礼（楚淮予）、"
    "漠然烟若（崇墨）等精彩演绎，欢迎收听~\n\n"
    "《拒绝关系变质》前14集为免费收听集，付费集每集20红豆。"
)
PAYLOAD = {"data": {
    "title": "拒绝关系变质", "desc": DESCRIPTION, "category": 5,
    "categoryLabels": [{"name": "纯爱"}], "payType": 1, "watchCount": 1549,
    "cvRespList": [{"dramaRoleType": 3, "cvNickname": "扶光工作室",
                    "platUid": 3769593835552, "role": "出品"}],
}}


class ManboIntroCvTests(unittest.TestCase):
    def build(self, **kwargs):
        return metadata.build_manbo_record({"dramaId": DRAMA_ID}, deepcopy(PAYLOAD), **kwargs)

    def test_real_description_extracts_two_leads_without_narrator_or_producer(self):
        self.assertEqual(metadata.extract_manbo_intro_cv_candidates(DESCRIPTION), [
            {"display_name": "一礼", "role_name": "楚淮予"},
            {"display_name": "漠然烟若", "role_name": "崇墨"},
        ])
        self.assertEqual(metadata.extract_manbo_intro_cv_candidates("每集20（红豆），欢迎收听。"), [])
        self.assertEqual(metadata.extract_manbo_intro_cv_candidates(
            "配音组\n旁白：梵叶\n楚淮予：一礼\n崇墨：漠然烟若\n制作组\n导演：甲"
        ), [{"display_name": "一礼", "role_name": "楚淮予"},
            {"display_name": "漠然烟若", "role_name": "崇墨"}])

    def test_inline_credit_labels_are_not_part_of_cv_names(self):
        expected = [{"display_name": "一礼", "role_name": "楚淮予"},
                    {"display_name": "漠然烟若", "role_name": "崇墨"}]
        for label in ["配音：", "配音演员: ", "主役CV：", "CV:", "cast：", "领衔主演："]:
            with self.subTest(label=label):
                desc = f"{label}一礼（楚淮予）、漠然烟若（崇墨）等精彩演绎。"
                self.assertEqual(metadata.extract_manbo_intro_cv_candidates(desc), expected)
                payload = deepcopy(PAYLOAD)
                payload["data"]["desc"] = desc
                search, allocate = Mock(), Mock()
                record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, payload,
                    combined_map={"一礼": {"manboCvId": 123}, "漠然烟若": {"manboCvId": 456}},
                    search_cv=search, generated_cv_id_allocator=allocate)
                self.assertEqual(record["mainCvNames"], ["一礼", "漠然烟若"])
                self.assertEqual(record["mainCvIds"], [123, 456])
                search.assert_not_called()
                allocate.assert_not_called()

    def test_credit_label_cleaning_preserves_cv_nicknames_and_narrator_exclusion(self):
        self.assertEqual(metadata.extract_manbo_intro_cv_candidates(
            "配音：梵叶（旁白）、CV一礼（楚淮予）、漠然烟若（崇墨）等精彩演绎。"
        ), [{"display_name": "CV一礼", "role_name": "楚淮予"},
            {"display_name": "漠然烟若", "role_name": "崇墨"}])

    def test_map_aliases_and_cat_only_identity_are_used_before_search(self):
        search = Mock(return_value=None)
        record = self.build(combined_map={
            "规范一礼": {"displayName": "规范一礼", "aliases": ["一礼"], "manboCvId": 123},
            "漠然烟若": {"displayName": "漠然烟若", "missevanCvId": 456},
        }, search_cv=search)
        search.assert_called_once_with("漠然烟若")
        self.assertEqual(record["mainCvIds"], [123, None])
        self.assertEqual(record["mainCvMissevanIds"], [None, 456])
        self.assertEqual(record["mainCvNames"], ["规范一礼", "漠然烟若"])
        self.assertEqual(record["mainCvRoleNames"], ["楚淮予", "崇墨"])

    def test_search_results_keep_platform_ids_separate_and_missing_ids_null(self):
        record = self.build(search_cv=Mock(side_effect=[{"missevanCvId": 789}, {"manboCvId": 1234567890123456789}]))
        self.assertEqual(record["mainCvIds"], [None, 1234567890123456789])
        self.assertEqual(record["mainCvMissevanIds"], [789, None])
        self.assertEqual(platform_sync.manbo_main_cv_display_names(record), ["一礼", "漠然烟若"])

    def test_unmatched_names_survive_repeated_refresh_without_fake_ids(self):
        search = Mock(return_value=None)
        record = self.build(search_cv=search)
        record = metadata.build_manbo_record(record, PAYLOAD, search_cv=search)
        self.assertEqual(record["mainCvIds"], [None, None])
        self.assertEqual(record["mainCvNames"], ["一礼", "漠然烟若"])
        self.assertEqual(record["mainCvRoleNames"], ["楚淮予", "崇墨"])

    def test_complete_platform_cast_and_manual_unknown_marker_skip_fallback(self):
        search = Mock()
        payload = deepcopy(PAYLOAD)
        payload["data"]["cvRespList"] = [
            {"dramaRoleType": 2, "platUid": cv_id, "cvNickname": name, "role": role}
            for cv_id, name, role in [(1, "平台甲", "甲"), (2, "平台乙", "乙")]
        ]
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, payload, search_cv=search)
        self.assertEqual(record["mainCvNames"], ["平台甲", "平台乙"])
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID, "mainCvNames": ["主役未知"]}, PAYLOAD, search_cv=search)
        self.assertEqual(record["mainCvNames"], ["主役未知"])
        search.assert_not_called()

    def test_partial_platform_cast_keeps_id_name_role_alignment(self):
        payload = deepcopy(PAYLOAD)
        payload["data"]["cvRespList"] = [{"dramaRoleType": 2, "platUid": 1, "cvNickname": "一礼", "role": "楚淮予"}]
        search = Mock(return_value=None)
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, payload, search_cv=search)
        self.assertEqual(record["mainCvIds"], [1, None])
        self.assertEqual(record["mainCvNames"], ["一礼", "漠然烟若"])
        search.assert_called_once_with("漠然烟若")

    def test_narrator_in_partial_platform_cast_does_not_block_second_intro_lead(self):
        payload = deepcopy(PAYLOAD)
        payload["data"]["cvRespList"] = [
            {"dramaRoleType": 2, "platUid": 1, "cvNickname": "梵叶", "role": "旁白"},
            {"dramaRoleType": 2, "platUid": 2, "cvNickname": "一礼", "role": "楚淮予"},
        ]
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, payload, search_cv=Mock(return_value=None))
        self.assertEqual(record["mainCvNames"], ["一礼", "漠然烟若"])
        self.assertEqual(record["mainCvIds"], [2, None])

    def test_ambiguous_map_names_are_not_searched_or_merged(self):
        search = Mock(return_value=None)
        record = self.build(combined_map={
            "甲": {"aliases": ["一礼"], "manboCvId": 1},
            "乙": {"aliases": ["一礼"], "manboCvId": 2},
        }, search_cv=search)
        self.assertEqual(record["mainCvNames"], ["漠然烟若"])
        search.assert_called_once_with("漠然烟若")

    def test_manbo_search_requires_one_exact_account_match(self):
        exact = {"userResp": {"id": 1234567890123456789, "nickname": "一礼"}}
        body = {"h": {"code": 200}, "b": {"searchStructureNewRespList": [
            {"timelineItemResp": [exact, exact, {"userResp": {"id": 2, "nickname": "一礼粉丝"}}]},
        ]}}
        with patch.object(metadata, "request_manbo_json", return_value=body) as request:
            self.assertEqual(metadata.search_manbo_cv("一礼"), {"manboCvId": 1234567890123456789, "display_name": "一礼"})
        self.assertIn("type=3", request.call_args.args[0])
        body["b"]["searchStructureNewRespList"][0]["timelineItemResp"].append({"userResp": {"id": 3, "nickname": "一礼"}})
        with patch.object(metadata, "request_manbo_json", return_value=body):
            self.assertIsNone(metadata.search_manbo_cv("一礼"))
        with patch.object(metadata, "request_manbo_json", return_value={"h": {"code": 6001}}):
            with self.assertRaises(RuntimeError):
                metadata.search_manbo_cv("一礼")

    def test_refresh_searches_manbo_then_missevan_and_publishes_only_target_names(self):
        info = {"records": [{"dramaId": DRAMA_ID}]}
        with (
            patch.object(metadata, "load_json", return_value=info),
            patch.object(metadata, "load_cache", return_value={"counts": {}}),
            patch.object(metadata, "build_manbo_cv_name_map", return_value={}),
            patch.object(metadata, "load_combined_map", return_value={}),
            patch.object(metadata, "request_manbo_json", return_value=PAYLOAD),
            patch.object(metadata, "search_manbo_cv", side_effect=[{"manboCvId": 123}, None]) as manbo_search,
            patch.object(metadata, "search_missevan_cv", return_value=None) as cat_search,
            patch.object(metadata, "MissevanRequester") as requester,
            patch.object(metadata, "save_json"), patch.object(metadata, "save_cache"),
            patch.object(metadata, "update_combined_cvid_map") as publish,
        ):
            stats = metadata.refresh_manbo(target_drama_ids={DRAMA_ID})
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(manbo_search.call_args_list, [call("一礼"), call("漠然烟若")])
        cat_search.assert_called_once_with("漠然烟若", requester.return_value)
        self.assertEqual(info["records"][0]["mainCvIds"], [123, None])
        publish.assert_called_once_with({}, info, missevan_drama_ids=set(), manbo_drama_ids={DRAMA_ID})

    def test_name_only_and_cat_only_cvs_reach_remote_map_and_both_aggregations(self):
        record = self.build(search_cv=Mock(side_effect=[{"missevanCvId": 789}, None]))
        record["needpay"] = True
        info = {"records": [record]}
        with (
            patch.object(cvid_map_tools, "load_remote_combined_map", return_value={}),
            patch.object(cvid_map_tools, "save_remote_combined_map") as save_map,
            patch.object(cvid_map_tools, "persist_generated_missevan_cvid_replacements"),
        ):
            stats = cvid_map_tools.update_combined_cvid_map({}, info, remote=True, upstash=Mock())
        mapping = save_map.call_args.args[0]
        self.assertEqual(set(mapping), {"一礼", "漠然烟若"})
        self.assertEqual(mapping["一礼"]["missevanCvId"], 789)
        self.assertIsNone(mapping["一礼"]["manboCvId"])
        self.assertIsNone(mapping["漠然烟若"]["cvId"])
        self.assertEqual(stats["ambiguous_count"], 0)
        ids, manbo_ids, names, avatars = build_cv_ranks.build_map_indexes(mapping)
        buckets = {}
        build_cv_ranks.collect_manbo_works(buckets, store=info, counts={DRAMA_ID: {"view_count": 1549}},
            missevan_ids=ids, manbo_ids=manbo_ids, name_index=names, avatar_index=avatars)
        self.assertEqual(set(buckets), {"一礼", "漠然烟若"})
        self.assertEqual(buckets["漠然烟若"]["totalViewCount"], 1549)
        with (
            patch.object(rebuild, "load_json", side_effect=[{}, info, mapping]),
            patch.object(rebuild, "load_cache", side_effect=[{"counts": {}}, {"counts": {DRAMA_ID: {"view_count": 1549}}}]),
        ):
            rows = rebuild.build_rows()
        self.assertEqual({row["cv_name"] for row in rows}, {"一礼", "漠然烟若"})
        self.assertEqual({row["role_names"] for row in rows}, {"楚淮予", "崇墨"})


if __name__ == "__main__":
    unittest.main()
