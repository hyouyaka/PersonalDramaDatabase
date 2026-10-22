import json
import unittest
from unittest.mock import Mock, patch

import append_manbo_ids
import append_missevan_ids
import fetch_ongoing
import fetch_rank_data
import platform_sync
import refresh_platform_metadata as metadata
import sync_new_drama_ids


class DramaExclusionIngressTests(unittest.TestCase):
    def test_ongoing_output_filters_record_ids_and_alias_keys_per_platform(self):
        records = {
            "83940": {"dramaId": "83940", "updateType": "weekly"},
            "alias": {"dramaId": "96614", "updateType": "daily"},
            "100": {"dramaId": "100", "updateType": "weekly"},
        }
        self.assertEqual(set(fetch_ongoing.build_payload("missevan", records)["records"]), {"100"})
        self.assertEqual(set(fetch_ongoing.build_payload("manbo", records)["records"]), set(records))
        manbo = {"1896908912553099327": {"dramaId": "1896908912553099327"}}
        self.assertEqual(fetch_ongoing.build_payload("manbo", manbo)["records"], {})

    def test_stale_import_queue_cannot_reintroduce_excluded_records(self):
        payload = {"missevan": ["83940", "88641", "100"], "manbo": ["1896908912553099327", "200"]}
        with patch.object(sync_new_drama_ids, "upstash_request", return_value=json.dumps(payload)):
            self.assertEqual(sync_new_drama_ids.load_queue(), {"missevan": ["100"], "manbo": ["200"]})

    def test_rank_discovery_does_not_enqueue_removed_works(self):
        client = Mock(return_value='{"missevan":["100"],"manbo":["200"]}')
        with patch.object(fetch_rank_data, "upstash_request", client), patch("builtins.print"):
            fetch_rank_data.append_new_drama_ids_atomic(["83940", "100"], ["1896908912553099327", "200"])
        self.assertEqual(json.loads(client.call_args.args[0][-2]), ["100"])
        self.assertEqual(json.loads(client.call_args.args[0][-1]), ["200"])
        self.assertEqual(set(json.loads(client.call_args.args[0][-4])), platform_sync.excluded_drama_ids("missevan"))
        self.assertEqual(set(json.loads(client.call_args.args[0][-3])), platform_sync.excluded_drama_ids("manbo"))
        self.assertIn('merge("missevan", ARGV[3], ARGV[1])', client.call_args.args[0][1])

    def test_repair_scope_filters_excluded_ids_in_stale_ongoing_payload(self):
        for platform, blocked in (("missevan", "83940"), ("manbo", "1896908912553099327")):
            with self.subTest(platform=platform):
                rank_keys = fetch_rank_data.MISSEVAN_RANKS if platform == "missevan" else fetch_rank_data.MANBO_RANKS
                rank_key = next(key for key in rank_keys if key != "peak")
                latest = {platform: {"ranks": {rank_key: {"items": [{"drama_id": "100"}]}}, "dramas": {}}}
                ongoing = {"records": {blocked: {"dramaId": blocked}}}
                self.assertEqual(fetch_rank_data.collect_current_danmaku_scope_ids(platform, latest, ongoing), {"100"})

    def test_all_null_repair_does_not_restore_excluded_historical_drama(self):
        latest = {"missevan": {"ranks": {}, "dramas": {}}}
        trend = {"dramas": {
            drama_id: {"samples": {"2026-10-22": {"metrics": {"danmaku_uid_count": None}}}}
            for drama_id in ("83940", "100")
        }}
        with (
            patch.object(fetch_rank_data, "_load_upstash_json_strict", return_value=latest),
            patch.object(fetch_rank_data, "_load_normal_trend_v2_strict", return_value=trend),
            patch.object(fetch_rank_data, "load_archived_drama_ids", return_value={}),
        ):
            targets, sources, _ = fetch_rank_data.collect_null_danmaku_ids_from_layers(
                "missevan", "2026-10-22", all_null_danmaku=True,
            )
        self.assertEqual(targets, {"100"})
        self.assertEqual(sources, {"100": ["trend"]})

    def test_rank_output_filters_removed_works_without_removing_other_seasons(self):
        store = {
            "missevan": {
                "dramas": {"83940": {}, "45006": {}, "72383": {}},
                "ranks": {"views": {"items": [{"drama_id": "83940"}, {"drama_id": "72383"}]}},
            },
        }
        fetch_rank_data.filter_archived_dramas(store, archived_by_platform={})
        self.assertEqual(set(store["missevan"]["dramas"]), {"45006", "72383"})
        self.assertEqual(store["missevan"]["ranks"]["views"]["items"], [{"drama_id": "72383"}])

    def test_manual_append_and_upsert_reject_excluded_ids_before_io(self):
        for module, drama_id in ((append_missevan_ids, "88641"), (append_manbo_ids, "1896908912553099327")):
            with self.subTest(module=module.__name__), patch.object(module, "download_info_file") as download, patch("builtins.print"):
                self.assertEqual(module.main(["append.py", drama_id]), 0)
                download.assert_not_called()
        with patch.object(metadata, "load_json") as load:
            with self.assertRaisesRegex(ValueError, "Excluded"):
                metadata.upsert_missevan_drama_ids(["83940"])
            with self.assertRaisesRegex(ValueError, "Excluded"):
                metadata.upsert_manbo_drama_ids(["1896908912553099327"])
            load.assert_not_called()


class CvDashAndFirstEpisodeTests(unittest.TestCase):
    def test_all_age_first_episode_fills_four_main_cvs(self):
        store = {"100": {"dramaId": 100, "title": "全年龄测试", "type": 3, "createTime": "2026.08"}}
        requester = Mock(request_count=3, last_backoff_seconds=0)
        cv_map = {f"CV{i}": {"cvId": i, "missevanCvId": i, "displayName": f"CV{i}"} for i in range(1, 5)}
        intro = "<p>CAST</p>" + "".join(f"<p>角色{i}—CV{i}</p>" for i in range(1, 5))
        requester.request_json.side_effect = [
            {"info": {"drama": {"id": 100, "name": "全年龄测试", "catalog": 89}, "cvs": [], "episodes": {"episode": [{"sound_id": 1, "soundstr": "第一集"}]}}},
            {"info": {"cvs": []}},
            {"info": {"Datas": [{"id": 1, "soundstr": "第一集", "intro": intro}]}},
        ]
        with (
            patch.object(metadata, "load_json", side_effect=[store, {}]),
            patch.object(metadata, "load_combined_map", return_value=cv_map),
            patch.object(metadata, "save_combined_map"),
            patch.object(metadata, "MissevanRequester", return_value=requester),
            patch.object(metadata, "save_missevan_store"),
            patch.object(metadata, "save_json"),
            patch.object(metadata, "_get_missevan_cv_name_map", return_value={i: f"CV{i}" for i in range(1, 5)}),
            patch("builtins.print"),
        ):
            metadata.refresh_missevan(target_drama_ids={"100"}, update_counts=False)
        self.assertEqual(store["100"]["maincvs"], [1, 2, 3, 4])

    def test_refresh_uses_first_episode_intro_when_preview_has_no_cast_heading(self):
        store = {"95054": {"dramaId": 95054, "title": "属于他的冰上赛场", "type": 4, "createTime": "2026.08"}}
        requester = Mock()
        requester.request_count = 4
        requester.last_backoff_seconds = 0
        requester.request_json.side_effect = [
            {"info": {"drama": {"id": 95054, "name": "属于他的冰上赛场", "catalog": 89}, "cvs": [], "episodes": {"episode": [
                {"sound_id": 1, "soundstr": "预告"}, {"sound_id": 2, "soundstr": "第一集"},
            ]}}},
            {"info": {"cvs": []}},
            {"info": {"cvs": []}},
            {"info": {"Datas": [
                {"id": 1, "soundstr": "预告", "intro": "<p>俞游歌—颜辉</p>"},
                {"id": 2, "soundstr": "第一集", "intro": "<p>——CAST——</p><p>俞游歌—颜辉</p><p>程于非—大老师</p>"},
            ]}},
        ]
        cv_map = {"颜辉": {"cvId": 11, "missevanCvId": 11, "displayName": "颜辉"}, "大老师": {"cvId": 12, "missevanCvId": 12, "displayName": "大老师"}}
        with (
            patch.object(metadata, "load_json", side_effect=[store, {}]),
            patch.object(metadata, "load_combined_map", return_value=cv_map),
            patch.object(metadata, "save_combined_map"),
            patch.object(metadata, "MissevanRequester", return_value=requester),
            patch.object(metadata, "save_missevan_store"),
            patch.object(metadata, "save_json"),
            patch.object(metadata, "_get_missevan_cv_name_map", return_value={11: "颜辉", 12: "大老师"}),
            patch("builtins.print"),
        ):
            result = metadata.refresh_missevan(target_drama_ids={"95054"}, update_counts=False)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(store["95054"]["maincvs"], [11, 12])
        self.assertEqual(store["95054"]["cvroles"], {"11": "俞游歌", "12": "程于非"})

    def test_95054_first_episode_requires_cast_heading_and_accepts_em_dash(self):
        intro = (
            "<p>——STAFF——</p><p>导演—调儿</p>"
            "<p>——CAST——</p><p>俞游歌—颜辉@Cv颜辉</p>"
            "<p>程于非—大老师@我精神状态挺好的呀哈哈哈</p>"
        )
        candidates = metadata.collect_missevan_episode_intro_candidates([
            {"sound_id": 1, "title": "预告", "intro": intro.replace("<p>——CAST——</p>", "")},
            {"sound_id": 2, "title": "《属于他的冰上赛场》第一集", "intro": intro},
            {"sound_id": 3, "title": "《属于他的冰上赛场》第二集", "intro": "<p>CAST</p><p>错误角色—错误CV</p>"},
        ], method="first_episode")
        self.assertEqual(candidates, [
            {"role_name": "俞游歌", "display_name": "颜辉", "account_name": "Cv颜辉"},
            {"role_name": "程于非", "display_name": "大老师", "account_name": "我精神状态挺好的呀哈哈哈"},
        ])
        self.assertEqual(metadata.extract_missevan_intro_cv_candidates("<p>俞游歌—颜辉</p><p>程于非—大老师</p>"), [])

    def test_double_dash_cv_first_credits_are_not_treated_as_role_first_pairs(self):
        self.assertEqual(metadata.extract_missevan_intro_cv_candidates(
            "<p>CAST</p><p>续续点灯——傅柏云</p><p>杜骁——舒清扬</p>",
        ), [])

    def test_second_season_keeps_copied_leads_after_platform_refresh(self):
        node, _ = metadata.build_missevan_base_node({"drama": {"id": 72383}, "cvs": []}, 4)
        self.assertEqual([(x["cv_id"], x["display_name"], x["role_name"]) for x in platform_sync.missevan_main_cv_entries(node)], [
            (1040, "赵毅", "皮修"), (1312, "林予曦", "文熙"),
        ])


if __name__ == "__main__":
    unittest.main()
