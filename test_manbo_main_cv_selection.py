import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import refresh_platform_metadata as metadata
import refresh_watch_counts
import sync_new_drama_ids as sync
import upstash_v2
from manbo_cv_review import MANBO_CV_REVIEW_KEY, manbo_cv_review_decisions
from test_upstash_v2 import FakeUpstash


def actor(cv_id, name=None, role=None, role_type=2):
    return {"dramaRoleType": role_type, "cvResp": {"id": cv_id, "nickname": name or f"CV{cv_id}"},
            "role": role or f"饰:角色{cv_id}"}


class ManboMainCvSelectionTests(unittest.TestCase):
    def build(self, data, record=None):
        return metadata.build_manbo_record(record or {"dramaId": "123"}, {"data": {
            "category": 1, "title": "测试剧", "categoryLabels": [{"name": "纯爱"}], **data,
        }}, {i: f"规范{i}" for i in range(1, 7)})

    def test_primary_list_filters_non_actors_and_deduplicates_without_truncation(self):
        main = [actor(99, role_type=4), actor(88, role_type=3), actor(2), actor("2"),
                actor(1), actor(3), actor(4), actor(5)]
        with patch.object(metadata, "extract_manbo_intro_cv_candidates") as intro:
            record = self.build({"cvRespList": [actor(90), actor(91)],
                                 "cvPerformMainRespList": main,
                                 "desc": "主役：干扰甲（甲）、干扰乙（乙）"})
        intro.assert_not_called()
        self.assertEqual(record["mainCvIds"], [2, 1, 3, 4, 5])
        self.assertEqual(record["mainCvNames"], ["规范2", "规范1", "规范3", "规范4", "规范5"])
        self.assertEqual(record["mainCvRoleNames"], ["角色2", "角色1", "角色3", "角色4", "角色5"])
        self.assertFalse(record["mainCvReviewRequired"])
        self.assertEqual(record["mainCvSource"], "api_main")

    def test_one_valid_primary_actor_is_not_padded_from_full_list_or_intro(self):
        with patch.object(metadata, "extract_manbo_intro_cv_candidates") as intro:
            record = self.build({"cvPerformMainRespList": [actor(2)],
                                 "cvRespList": [actor(1), actor(3)], "desc": "主役：甲（乙）"})
        self.assertEqual(record["mainCvIds"], [2])
        self.assertFalse(record["mainCvReviewRequired"])
        intro.assert_not_called()

    def test_manual_override_wins_even_over_primary_and_unknown_marker(self):
        for main in [[], [actor(9), actor(8)]]:
            with self.subTest(main=main):
                record = self.build({"cvPerformMainRespList": main}, {
                    "dramaId": "1875551878436094023", "mainCvNames": ["主役未知"],
                })
                self.assertEqual(record["mainCvIds"], [1825104379909, 1867638054916])
                self.assertEqual(record["mainCvRoleNames"], ["封亦", "席恙"])
                self.assertFalse(record["mainCvReviewRequired"])

    def test_manual_override_can_find_actors_only_in_primary_list(self):
        record = self.build({"cvPerformMainRespList": [actor(3053261148187), actor(1842233442307)]},
                            {"dramaId": "2069403049792634966"})
        self.assertEqual(record["mainCvIds"], [1842233442307, 3053261148187])
        self.assertFalse(record["mainCvReviewRequired"])

    def test_field_override_unknown_is_manual_on_first_refresh(self):
        drama_id = "2241266599933771973"
        originals = [
            {"dramaId": drama_id, "name": ""},
            {"dramaId": drama_id, "mainCvIds": [1, 2], "mainCvNames": ["旧主役甲", "旧主役乙"]},
            {"dramaId": drama_id, "mainCvReviewRequired": True, "mainCvSource": "legacy"},
        ]
        for original in originals:
            for extra in [{}, {"cvPerformMainRespList": []},
                          {"cvPerformMainRespList": [actor(1), actor(2)]}]:
                with self.subTest(original=original, extra=extra):
                    before = deepcopy(original)
                    with patch.object(metadata, "extract_manbo_intro_cv_candidates") as intro:
                        record = self.build({"cvRespList": [actor(1), actor(2)], **extra}, original)
                    self.assertEqual(record["mainCvIds"], [])
                    self.assertEqual(record["mainCvNames"], ["主役未知"])
                    self.assertEqual(record["mainCvNicknames"], ["主役未知"])
                    self.assertEqual(record["mainCvRoleNames"], [])
                    self.assertEqual(record["mainCvSource"], "manual")
                    self.assertFalse(record["mainCvReviewRequired"])
                    self.assertEqual(manbo_cv_review_decisions({"records": [record]}), {drama_id: False})
                    self.assertEqual(original, before)
                    intro.assert_not_called()

    def test_unavailable_primary_preserves_all_existing_cv_fields_and_marks_review(self):
        original = {"dramaId": "123", "mainCvIds": [None, "1867638054916"],
                    "mainCvNames": ["旧姓名", "顾辰"], "mainCvNicknames": ["旧昵称", "顾辰昵称"],
                    "mainCvRoleNames": ["角色甲", "席恙"], "mainCvMissevanIds": [123, 1920]}
        fields = {k: v for k, v in original.items() if k.startswith("mainCv")}
        for extra in [{}, {"cvPerformMainRespList": None}, {"cvPerformMainRespList": []},
                      {"cvPerformMainRespList": [actor(99, role_type=4)]},
                      {"cvPerformMainRespList": [None, {}, actor(0), actor("invalid")]}]:
            with self.subTest(extra=extra), patch.object(metadata, "extract_manbo_intro_cv_candidates") as intro:
                record = self.build({"cvRespList": [actor(1), actor(2)], **extra}, deepcopy(original))
                for key, value in fields.items():
                    self.assertEqual(record[key], value)
                self.assertTrue(record["mainCvReviewRequired"])
                self.assertEqual(record["mainCvSource"], "preserved")
                intro.assert_not_called()
        self.assertEqual(original, {"dramaId": "123", **fields})

    def test_name_only_existing_leads_are_preserved(self):
        record = self.build({"cvRespList": [actor(1), actor(2)]},
                            {"dramaId": "123", "mainCvNames": ["人工姓名"]})
        self.assertEqual(record["mainCvNames"], ["人工姓名"])
        self.assertNotIn("mainCvIds", record)
        self.assertTrue(record["mainCvReviewRequired"])

    def test_new_drama_uses_two_legacy_actors_even_for_all_age(self):
        for extra in [{}, {"cvPerformMainRespList": []}]:
            with self.subTest(extra=extra):
                record = self.build({"categoryLabels": [{"name": "全年龄"}],
                                     "cvRespList": [actor(90, role="旁白"), actor(1), actor(2), actor(3)], **extra})
                self.assertEqual(record["mainCvIds"], [1, 2])
                self.assertTrue(record["mainCvReviewRequired"])
                self.assertEqual(record["mainCvSource"], "legacy")

    def test_valid_primary_replaces_old_cast_and_clears_review(self):
        old = self.build({"cvRespList": [actor(1), actor(2)]})
        record = self.build({"cvPerformMainRespList": [actor(3), actor(4)]}, old)
        self.assertEqual(record["mainCvIds"], [3, 4])
        self.assertFalse(record["mainCvReviewRequired"])


class ManboCvReviewPublicationTests(unittest.TestCase):
    def publish(self, remote, records, **kwargs):
        with patch("builtins.print"):
            return upstash_v2.publish_info_v2("manbo:info:v2", {"records": records}, upstash=remote, **kwargs)

    def test_publication_creates_queue_and_resolves_only_explicit_decisions(self):
        remote = FakeUpstash()
        self.publish(remote, [{"dramaId": "123", "mainCvReviewRequired": True}])
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"123"})
        remote.sets[MANBO_CV_REVIEW_KEY].update({"456", "789"})
        self.publish(remote, [{"dramaId": "123", "mainCvReviewRequired": False},
                              {"dramaId": "456", "mainCvReviewRequired": True}, {"dramaId": "789"}])
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"456", "789"})
        self.assertEqual(json.loads(remote.strings["manbo:info:v2"])["records"][0]["dramaId"], "123")
        self.assertEqual(json.loads(remote.strings["manbo:info:meta:v2"])["recordCount"], 3)

    def test_first_manual_unknown_refresh_removes_stale_pending_id(self):
        drama_id = "2241266599933771973"
        remote = FakeUpstash()
        remote.sets[MANBO_CV_REVIEW_KEY] = {drama_id, "999"}
        record = metadata.build_manbo_record(
            {"dramaId": drama_id}, {"data": {"title": "三国演义有声剧", "category": 1}},
        )
        self.publish(remote, [record])
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"999"})
        published = json.loads(remote.strings["manbo:info:v2"])["records"][0]
        self.assertEqual(published["mainCvSource"], "manual")
        self.assertFalse(published["mainCvReviewRequired"])

    def test_concurrent_info_change_updates_neither_queue_nor_info(self):
        remote = FakeUpstash()
        remote.strings["manbo:info:v2"] = '{"records":[],"newer":true}'
        remote.sets[MANBO_CV_REVIEW_KEY] = {"456"}
        before = deepcopy(remote.strings)
        with self.assertRaisesRegex(RuntimeError, "concurrently changed"):
            self.publish(remote, [{"dramaId": "123", "mainCvReviewRequired": True}], source_encoded='{"records":[]}')
        self.assertEqual(remote.strings, before)
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"456"})

    def test_wrong_queue_type_fails_before_info_or_meta_write(self):
        remote = FakeUpstash()
        remote.strings[MANBO_CV_REVIEW_KEY] = "wrong type"
        before = deepcopy(remote.strings)
        with self.assertRaisesRegex(RuntimeError, "Redis Set"):
            self.publish(remote, [{"dramaId": "123", "mainCvReviewRequired": True}])
        self.assertEqual(remote.strings, before)

    def test_invalid_ids_and_legacy_records_cannot_change_queue(self):
        self.assertEqual(manbo_cv_review_decisions({"records": [None, {},
            {"dramaId": "not-id", "mainCvReviewRequired": True},
            {"dramaId": "123"}, {"dramaId": "456", "mainCvReviewRequired": "true"},
            {"dramaId": "2281413604869669104", "mainCvReviewRequired": True},
        ]}), {"2281413604869669104": False})

    def test_watchcount_and_old_manbo_publications_without_decisions_do_not_touch_queue(self):
        remote = FakeUpstash()
        remote.sets[MANBO_CV_REVIEW_KEY] = {"123"}
        self.publish(remote, [{"dramaId": "123", "mainCvNames": ["旧主役"]}])
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"123"})
        self.assertTrue(all(MANBO_CV_REVIEW_KEY not in cmd for cmd in remote.commands))

    def test_real_refresh_and_merge_publication_preserve_then_replace_leads_and_resolve_queue(self):
        records = [{"dramaId": str(i), "name": f"剧集{i}"} for i in range(100, 152)]
        target = records[23]
        target.update({"mainCvIds": [1, 2], "mainCvNames": ["已确认甲", "已确认乙"],
                       "mainCvNicknames": ["昵称甲", "昵称乙"], "mainCvRoleNames": ["角色甲", "角色乙"]})
        remote = FakeUpstash()
        remote.strings[sync.MANBO_INFO_KEY] = json.dumps({"version": 1, "records": records})
        remote.sets[MANBO_CV_REVIEW_KEY] = {"999"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manbo-info.json"
            path.write_text(remote.strings[sync.MANBO_INFO_KEY], encoding="utf8")
            with (
                patch.object(metadata, "MANBO_INFO_PATH", path),
                patch.object(metadata, "load_cache", return_value={"counts": {}}),
                patch.object(metadata, "save_cache"),
                patch.object(metadata, "load_combined_map", return_value={}),
                patch.object(metadata, "build_manbo_cv_name_map", return_value={3: "主役丙", 4: "主役丁"}),
                patch.object(metadata, "search_manbo_cv") as search,
                patch.object(metadata, "update_combined_cvid_map") as map_update,
                patch.object(sync, "upstash_request", remote),
                patch("builtins.print"),
            ):
                for main in [[], [actor(3), actor(4)]]:
                    payload = {"data": {"radioDramaId": "123", "title": "测试剧", "category": 1,
                        "watchCount": 100, "cvRespList": [actor(90), actor(91)],
                        "cvPerformMainRespList": main}}
                    with patch.object(metadata, "request_manbo_json", return_value=payload):
                        stats = metadata.refresh_manbo(target_drama_ids={"123"})
                    self.assertEqual(stats["processed"], 1)
                    # A local refresh alone must not change the remote queue.
                    self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"123", "999"} if main else {"999"})
                    sync.merge_and_upload_info_file(sync.MANBO_INFO_KEY, path, ["123"])
                    published = json.loads(remote.strings[sync.MANBO_INFO_KEY])
                    current = next(r for r in published["records"] if r["dramaId"] == "123")
                    self.assertEqual(current["mainCvIds"], [3, 4] if main else [1, 2])
                    self.assertEqual(current["mainCvNames"], ["主役丙", "主役丁"] if main else ["已确认甲", "已确认乙"])
                    self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"999"} if main else {"123", "999"})
                    self.assertEqual([r for r in published["records"] if r["dramaId"] != "123"],
                                     [r for r in records if r["dramaId"] != "123"])
                search.assert_not_called()
                map_update.assert_not_called()

    def test_weekly_info_patch_preserves_cast_and_does_not_call_cv_selection(self):
        remote = FakeUpstash()
        record = {"dramaId": "123", "name": "测试剧", "cover": "old",
                  "mainCvIds": [1, 2], "mainCvNames": ["甲", "乙"],
                  "mainCvReviewRequired": True, "mainCvSource": "preserved"}
        remote.strings[sync.MANBO_INFO_KEY] = json.dumps({"records": [record]})
        remote.sets[MANBO_CV_REVIEW_KEY] = {"123", "999"}
        with (
            patch.object(refresh_watch_counts, "save_json"),
            patch.object(metadata, "build_manbo_record") as build,
            patch.object(metadata, "request_manbo_json") as request,
            patch("builtins.print"),
        ):
            refresh_watch_counts.publish_info_observations("manbo", {"123": {"cover": "new"}}, upstash=remote)
        published = json.loads(remote.strings[sync.MANBO_INFO_KEY])["records"][0]
        self.assertEqual(published, {**record, "cover": "new"})
        self.assertEqual(remote.sets[MANBO_CV_REVIEW_KEY], {"123", "999"})
        build.assert_not_called()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
