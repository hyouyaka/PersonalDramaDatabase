import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_cv_ranks as cv


class SeriesCountMetadataTests(unittest.TestCase):
    WATCHCOUNT_TIME = "2026-11-06T05:17:38+00:00"
    BUILD_TIME = "2026-11-13T01:30:00+00:00"

    def inputs(self):
        return {
            "missevan_store": {"100": {"dramaId": "100", "title": "作品", "fallbackCvNames": ["演员"]}},
            "manbo_store": {"records": [{"dramaId": "200", "name": "作品", "mainCvNames": ["演员"]}]},
            "missevan_counts": {"100": {"view_count": 10}},
            "manbo_counts": {"200": {"view_count": 20}},
            "cvid_map": {"演员": {"aliases": ["别名"]}},
            "series_info": {"猫耳:系列": {
                "platform": "猫耳", "category": "广播剧", "series title": "系列", "dramaIds": ["100"],
            }},
            "generated_at": self.WATCHCOUNT_TIME,
        }

    def build(self, inputs=None, build_time=None):
        return cv.build_cv_ranks_payload(
            **(inputs or self.inputs()), series_count_generated_at=build_time or self.BUILD_TIME,
        )

    def test_count_metadata_uses_current_time_and_versions_of_only_its_actual_inputs(self):
        inputs = self.inputs()
        with patch.object(cv, "now_iso", return_value=self.BUILD_TIME) as now:
            payload = cv.build_cv_ranks_payload(**inputs)
        now.assert_called_once_with()
        self.assertEqual(payload["date"], "2026-11-06")
        self.assertEqual(payload["generated_at"], self.WATCHCOUNT_TIME)
        for platform, key in (("missevan", cv.MISSEVAN_INFO_KEY), ("manbo", cv.MANBO_INFO_KEY)):
            meta = payload["seriesCountMeta"][platform]
            self.assertEqual(meta["generated_at"], self.BUILD_TIME)
            self.assertEqual(set(meta["sourceVersions"]), {key, cv.CVID_MAP_KEY, cv.SERIES_INFO_KEY})
            for version in meta["sourceVersions"].values():
                self.assertRegex(version, r"^sha256:[0-9a-f]{64}$")
        canonical_map = '{"演员":{"aliases":["别名"]}}'
        expected = "sha256:" + hashlib.sha256(canonical_map.encode("utf-8")).hexdigest()
        self.assertEqual(payload["seriesCountMeta"]["missevan"]["sourceVersions"][cv.CVID_MAP_KEY], expected)
        self.assertEqual(set(payload["seriesCountRankings"]), {"missevan", "manbo"})

    def test_repeated_build_updates_count_time_but_preserves_source_versions_and_view_rankings(self):
        first = self.build()
        second = self.build(build_time="2026-11-14T01:30:00+00:00")
        for platform in cv.PLATFORMS:
            self.assertNotEqual(first["seriesCountMeta"][platform]["generated_at"], second["seriesCountMeta"][platform]["generated_at"])
            self.assertEqual(first["seriesCountMeta"][platform]["sourceVersions"], second["seriesCountMeta"][platform]["sourceVersions"])
        self.assertEqual(
            {k: v for k, v in first.items() if k != "seriesCountMeta"},
            {k: v for k, v in second.items() if k != "seriesCountMeta"},
        )

    def test_new_work_without_new_watchcounts_changes_only_count_result_and_source_version(self):
        inputs = self.inputs()
        first = self.build(inputs)
        inputs["missevan_store"]["101"] = {
            "dramaId": "101", "title": "新增作品", "fallbackCvNames": ["演员"],
        }
        second = self.build(inputs, build_time="2026-11-14T01:30:00+00:00")
        self.assertEqual(first["seriesCountRankings"]["missevan"][0]["seriesCount"], 1)
        self.assertEqual(second["seriesCountRankings"]["missevan"][0]["seriesCount"], 2)
        first_meta, second_meta = (payload["seriesCountMeta"]["missevan"] for payload in (first, second))
        self.assertNotEqual(first_meta["generated_at"], second_meta["generated_at"])
        self.assertNotEqual(first_meta["sourceVersions"][cv.MISSEVAN_INFO_KEY], second_meta["sourceVersions"][cv.MISSEVAN_INFO_KEY])
        self.assertEqual(first["seriesCountRankings"]["manbo"], second["seriesCountRankings"]["manbo"])
        for field in ("date", "generated_at", "rankings", "paidRankings"):
            self.assertEqual(first[field], second[field])

    def test_each_source_change_updates_the_corresponding_content_version(self):
        before = self.build()["seriesCountMeta"]
        cases = (
            ("missevan_store", cv.MISSEVAN_INFO_KEY, {"missevan"}),
            ("manbo_store", cv.MANBO_INFO_KEY, {"manbo"}),
            ("cvid_map", cv.CVID_MAP_KEY, set(cv.PLATFORMS)),
            ("series_info", cv.SERIES_INFO_KEY, set(cv.PLATFORMS)),
        )
        for input_key, source_key, affected in cases:
            with self.subTest(source=source_key):
                inputs = self.inputs()
                if input_key == "missevan_store":
                    inputs[input_key]["100"]["title"] = "修改标题"
                elif input_key == "manbo_store":
                    inputs[input_key]["records"][0]["name"] = "修改标题"
                elif input_key == "cvid_map":
                    inputs[input_key]["演员"]["aliases"].append("新别名")
                else:
                    inputs[input_key]["猫耳:系列"]["series title"] = "修改系列标题"
                after = self.build(inputs)["seriesCountMeta"]
                for platform in cv.PLATFORMS:
                    changed = {k for k, v in after[platform]["sourceVersions"].items()
                               if v != before[platform]["sourceVersions"][k]}
                    self.assertEqual(changed, {source_key} if platform in affected else set())

    def test_versions_ignore_json_object_key_order_and_watchcount_changes(self):
        inputs = self.inputs()
        before = self.build(inputs)["seriesCountMeta"]
        inputs["missevan_store"]["100"] = dict(reversed(list(inputs["missevan_store"]["100"].items())))
        inputs["series_info"]["猫耳:系列"] = dict(reversed(list(inputs["series_info"]["猫耳:系列"].items())))
        inputs["missevan_counts"]["100"]["view_count"] = 999
        inputs["manbo_counts"] = {}
        self.assertEqual(self.build(inputs)["seriesCountMeta"], before)

    def test_publisher_keeps_watchcount_trend_date_and_time_with_independent_count_metadata(self):
        inputs = self.inputs()
        cache = {"_meta": {"updated_at": self.WATCHCOUNT_TIME}, "counts": {}}
        for override in (None, "2026-10-30T00:00:00+00:00"):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "ranks-cv.json"
                def load_info(path, default):
                    return inputs["missevan_store"] if path == cv.MISSEVAN_INFO_PATH else inputs["manbo_store"]

                with (
                    patch.object(cv, "sync_remote_watchcount_inputs"),
                    patch.object(cv, "load_cache", return_value=cache),
                    patch.object(cv, "sync_remote_rank_inputs", return_value=inputs["cvid_map"]),
                    patch.object(cv, "fetch_remote_series_info", return_value=inputs["series_info"]),
                    patch.object(cv, "load_json", side_effect=load_info),
                    patch.object(cv, "now_iso", return_value=self.BUILD_TIME),
                    patch.object(cv, "upload_cv_ranks") as upload,
                    patch.object(cv, "upload_cv_trends") as trends,
                ):
                    payload = cv.build_and_publish_cv_ranks(
                        output_path=output, upload=True, upstash=Mock(), generated_at=override,
                    )
                expected_sample_time = override or self.WATCHCOUNT_TIME
                self.assertEqual(payload["generated_at"], expected_sample_time)
                self.assertEqual(payload["date"], expected_sample_time[:10])
                for platform in cv.PLATFORMS:
                    self.assertEqual(payload["seriesCountMeta"][platform]["generated_at"], self.BUILD_TIME)
                self.assertEqual(trends.call_args.kwargs["history_date"], expected_sample_time[:10])
                self.assertEqual(trends.call_args.kwargs["generated_at"], expected_sample_time)
                self.assertEqual(upload.call_args.args[0], payload)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)


class SeriesCountRankTests(unittest.TestCase):
    @staticmethod
    def series(platform, ids, title="系列"):
        return {"platform": platform, "category": "广播剧", "series title": title, "dramaIds": ids}

    @staticmethod
    def missevan(drama_id, name="演员", **extra):
        return {"dramaId": drama_id, "title": f"剧{drama_id}", "fallbackCvNames": [name], **extra}

    @staticmethod
    def manbo(drama_id, name="演员", **extra):
        return {"dramaId": drama_id, "name": f"剧{drama_id}", "mainCvNames": [name], **extra}

    def payload(self, *, missevan=None, manbo=None, series=None, cvid_map=None, counts=None, top_n=30):
        return cv.build_cv_ranks_payload(
            missevan_store=missevan or {}, manbo_store={"records": manbo or []},
            missevan_counts=counts or {}, manbo_counts={}, cvid_map=cvid_map or {},
            series_info=series or {}, generated_at="2026-11-12T00:00:00+00:00", top_n=top_n,
        )

    def test_series_aliases_duplicate_records_and_platforms_are_independent(self):
        records = [
            self.manbo("100", "别名", needpay=False),
            self.manbo("101", "演员", needpay=True),
            self.manbo("101", "别名", vipFree=1),
            self.manbo("102", "演员"),
        ]
        payload = self.payload(
            missevan={"100": self.missevan("100")}, manbo=records,
            series={"猫耳:系列": self.series("猫耳", [100, "999"]),
                    "漫播:系列": self.series("漫播", ["100", "101", "101"])},
            cvid_map={"演员": {"aliases": ["别名"], "avatar": "avatar"}},
        )
        self.assertEqual(payload["version"], 4)
        missevan, manbo = (payload["seriesCountRankings"][p][0] for p in cv.PLATFORMS)
        self.assertEqual(missevan["seriesCount"], 1)
        self.assertEqual(missevan["works"][0]["dramaIds"], ["100"])
        self.assertEqual(manbo["seriesCount"], 2)
        self.assertEqual(manbo["avatar"], "avatar")
        by_key = {work["seriesKey"]: work for work in manbo["works"]}
        self.assertEqual(by_key["漫播:系列"]["dramaIds"], ["100", "101"])
        self.assertEqual(by_key["manbo:drama:102"]["dramaIds"], ["102"])
        self.assertEqual(payload["rankings"], {"missevan": [], "manbo": []})
        self.assertEqual(payload["paidRankings"], {"missevan": [], "manbo": []})

    def test_archived_members_and_their_cvs_cannot_be_resurrected(self):
        # 100 and 200 remain in the series table after being removed from active info.
        payload = self.payload(
            missevan={"101": self.missevan("101", "仍在主役")},
            manbo=[self.manbo("201", "仍在主役")],
            series={"猫耳:部分归档": self.series("猫耳", ["100", "101"]),
                    "猫耳:全部归档": self.series("猫耳", ["102", "103"]),
                    "漫播:部分归档": self.series("漫播", ["200", "201"]),
                    "漫播:全部归档": self.series("漫播", ["202", "203"])},
            cvid_map={"仅出演归档季": {}, "仍在主役": {}},
            counts={"100": {"view_count": 999}, "102": {"view_count": 999}},
        )
        for platform, remaining in (("missevan", "101"), ("manbo", "201")):
            ranks = payload["seriesCountRankings"][platform]
            self.assertEqual(len(ranks), 1)
            self.assertEqual(ranks[0]["cvName"], "仍在主役")
            self.assertEqual(ranks[0]["seriesCount"], 1)
            self.assertEqual(ranks[0]["works"][0]["dramaIds"], [remaining])

    def test_season_leads_are_counted_only_for_their_own_seasons(self):
        payload = self.payload(
            missevan={"100": self.missevan("100", "甲"), "101": self.missevan("101", "乙")},
            series={"猫耳:系列": self.series("猫耳", ["100", "101"])},
        )
        by_name = {row["cvName"]: row for row in payload["seriesCountRankings"]["missevan"]}
        for name, drama_id in (("甲", "100"), ("乙", "101")):
            self.assertEqual(by_name[name]["seriesCount"], 1)
            self.assertEqual(by_name[name]["works"][0]["dramaIds"], [drama_id])

    def test_missing_null_and_invalid_counts_do_not_affect_series_totals(self):
        store = {str(i): self.missevan(str(i)) for i in range(100, 104)}
        before = copy.deepcopy(store)
        payload = self.payload(missevan=store, counts={
            "101": {"view_count": None}, "102": {"view_count": "bad"}, "103": {"view_count": 10},
        })
        self.assertEqual(payload["seriesCountRankings"]["missevan"][0]["seriesCount"], 4)
        self.assertEqual(payload["rankings"]["missevan"][0]["workCount"], 2)
        self.assertEqual(payload["rankings"]["missevan"][0]["totalViewCount"], 10)
        self.assertEqual(store, before)

    def test_same_title_does_not_merge_distinct_series_or_standalone_works(self):
        ranks = self.payload(
            manbo=[self.manbo(str(i), name="演员", seriesTitle="同名") for i in range(100, 104)],
            series={"漫播:同名甲": self.series("漫播", ["100"], "同名"),
                    "漫播:同名乙": self.series("漫播", ["101"], "同名")},
        )["seriesCountRankings"]["manbo"]
        self.assertEqual(ranks[0]["seriesCount"], 4)
        self.assertEqual(len({work["seriesKey"] for work in ranks[0]["works"]}), 4)

    def test_unknown_leads_and_excluded_records_do_not_count(self):
        with patch.object(cv, "is_excluded_drama", side_effect=lambda platform, drama_id: drama_id == "100"):
            payload = self.payload(
                missevan={"100": self.missevan("100"), "101": self.missevan("101", "主役未知")},
                manbo=[self.manbo("100"), self.manbo("101", "主役未知")],
            )
        self.assertEqual(payload["seriesCountRankings"], {"missevan": [], "manbo": []})

    def test_counts_then_name_order_top_30_and_numeric_detail_order(self):
        records = [self.manbo(str(1000 + i), f"CV{i:02d}") for i in reversed(range(35))]
        records += [self.manbo("10", "领先"), self.manbo("2", "领先"), self.manbo("20", "领先")]
        ranks = self.payload(manbo=records, series={
            "漫播:系列": self.series("漫播", ["10", "2"]),
        })["seriesCountRankings"]["manbo"]
        self.assertEqual(len(ranks), 36)
        self.assertEqual(ranks[0]["cvName"], "领先")
        self.assertEqual(ranks[0]["seriesCount"], 2)
        self.assertEqual([row["cvName"] for row in ranks[1:]], [f"CV{i:02d}" for i in range(35)])
        self.assertEqual([row["rank"] for row in ranks], [1] + [2] * 35)
        series = next(work for work in ranks[0]["works"] if work["seriesKey"] == "漫播:系列")
        self.assertEqual(series["dramaIds"], ["2", "10"])

    def test_empty_platforms_and_smaller_limit(self):
        self.assertEqual(self.payload()["seriesCountRankings"], {"missevan": [], "manbo": []})
        ranks = self.payload(manbo=[self.manbo("100", "A"), self.manbo("101", "B")], top_n=1)
        self.assertEqual([r["cvName"] for r in ranks["seriesCountRankings"]["manbo"]], ["A", "B"])
        self.assertEqual([r["rank"] for r in ranks["seriesCountRankings"]["manbo"]], [1, 1])

    def rankings_for_series_counts(self, counts, *, top_n=30):
        missevan = {}
        manbo = []
        for index, count in enumerate(counts):
            for work in range(count):
                drama_id = str(100000 + index * 1000 + work)
                name = f"CV{index:02d}"
                missevan[drama_id] = self.missevan(drama_id, name)
                manbo.append(self.manbo(drama_id, name))
        return self.payload(missevan=missevan, manbo=manbo, top_n=top_n)["seriesCountRankings"]

    def test_equal_series_counts_share_rank_and_next_rank_skips_tied_entries(self):
        for platform, ranks in self.rankings_for_series_counts([7, 6, 6, 5, 4, 4, 3]).items():
            with self.subTest(platform=platform):
                self.assertEqual([r["rank"] for r in ranks], [1, 2, 2, 4, 5, 5, 7])
                self.assertEqual([r["cvName"] for r in ranks], [f"CV{i:02d}" for i in range(7)])

    def test_all_entries_tied_at_rank_30_are_retained_and_rank_33_is_excluded(self):
        counts = list(range(31, 2, -1)) + [2, 2, 2, 1, 1]
        for platform, ranks in self.rankings_for_series_counts(counts).items():
            with self.subTest(platform=platform):
                self.assertEqual(len(ranks), 32)
                self.assertEqual([r["rank"] for r in ranks], list(range(1, 30)) + [30, 30, 30])
                self.assertEqual([r["cvName"] for r in ranks[-3:]], ["CV29", "CV30", "CV31"])

    def test_rank_cutoff_does_not_include_next_group_when_ties_skip_rank_30(self):
        counts = list(range(30, 2, -1)) + [2, 2, 2, 2, 1]
        for platform, ranks in self.rankings_for_series_counts(counts).items():
            with self.subTest(platform=platform):
                self.assertEqual(len(ranks), 32)
                self.assertEqual([r["rank"] for r in ranks], list(range(1, 29)) + [29] * 4)

    def test_all_tied_cvs_are_retained_even_if_more_than_30(self):
        for platform, ranks in self.rankings_for_series_counts([1] * 35).items():
            with self.subTest(platform=platform):
                self.assertEqual(len(ranks), 35)
                self.assertEqual([r["rank"] for r in ranks], [1] * 35)

    def test_distinct_counts_still_keep_only_ranks_up_to_30(self):
        for platform, ranks in self.rankings_for_series_counts(list(range(35, 0, -1))).items():
            with self.subTest(platform=platform):
                self.assertEqual(len(ranks), 30)
                self.assertEqual([r["rank"] for r in ranks], list(range(1, 31)))

    def test_zero_rank_limit_returns_empty_rankings(self):
        self.assertEqual(self.rankings_for_series_counts([2, 1, 1], top_n=0), {"missevan": [], "manbo": []})

    def test_series_validation_and_conflicting_membership(self):
        valid = self.series("猫耳", ["100"])
        invalid = [None, [], {"bad": []}, {"bad": {}},
                   {"bad": {**valid, "platform": "other"}},
                   {"bad": {**valid, "dramaIds": "100"}},
                   {"bad": {**valid, "dramaIds": [True]}},
                   {"bad": {**valid, "dramaIds": [""]}},
                   {"a": valid, "b": valid}]
        for series in invalid:
            with self.subTest(series=series), self.assertRaises(ValueError):
                cv.build_series_index(series)

    def test_bad_remote_series_never_saves_or_publishes_rankings(self):
        valid = self.series("猫耳", ["100"])
        for raw in (None, "{", "{}", "[]", json.dumps({"a": valid, "b": valid})):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "ranks-cv.json"
                output.write_text("previous result", encoding="utf-8")
                remote = Mock(return_value=raw)
                with (
                    patch.object(cv, "sync_remote_watchcount_inputs"),
                    patch.object(cv, "load_cache", return_value={}),
                    patch.object(cv, "sync_remote_rank_inputs", return_value={}),
                    patch.object(cv, "upload_cv_ranks") as upload,
                    patch.object(cv, "upload_cv_trends") as trends,
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    cv.build_and_publish_cv_ranks(output_path=output, upstash=remote)
                upload.assert_not_called()
                trends.assert_not_called()
                self.assertEqual(output.read_text(encoding="utf-8"), "previous result")
                remote.assert_called_once_with(["GET", cv.SERIES_INFO_KEY])

    def test_remote_read_failure_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "network"):
            cv.fetch_remote_series_info(upstash=Mock(side_effect=RuntimeError("network")))


if __name__ == "__main__":
    unittest.main()
