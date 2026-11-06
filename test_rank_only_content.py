import json
import sys
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

import build_cv_ranks as cv
import fetch_ongoing as ongoing
import fetch_rank_data as ranks
import platform_sync as policy
import sync_new_drama_ids as sync
import upstash_v2
from test_upstash_v2 import FakeUpstash


SONGS = ("1896908912553099327", "2281413604869669104")


class RankOnlyContentTests(unittest.TestCase):
    def setUp(self):
        self.print_patch = patch("builtins.print")
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)

    def fetch_store(self, song_id=SONGS[1]):
        rows = [{"radioDramaIdStr": str(1000 + i), "title": f"作品{i}",
                 "diamondValue": 100 - i} for i in range(1, 21)]
        rows[13] = {"radioDramaIdStr": song_id, "title": "翻唱合集",
                    "coverPic": "https://example.test/cover.jpg", "diamondValue": 86.5}
        store = {"_meta": {}, "missevan": {"dramas": {}, "ranks": {}},
                 "manbo": {"dramas": {}, "ranks": {}}}
        with (
            patch.object(ranks, "MANBO_RANKS", {"diamond_monthly": (5, "钻石榜月榜", 20, "diamondValue")}),
            patch.object(ranks, "request_manbo_json", return_value={"b": {"radioDramaRespList": rows}}),
        ):
            discovered = ranks.fetch_manbo_ranks(store)
        self.assertIn(song_id, discovered)
        return store

    def test_library_and_rank_exclusions_are_independent_and_platform_scoped(self):
        for song_id in SONGS:
            self.assertTrue(policy.is_excluded_drama("manbo", song_id))
            self.assertFalse(policy.is_rank_excluded_drama("manbo", song_id))
            self.assertFalse(policy.is_excluded_drama("missevan", song_id))
        for platform in ("missevan", "manbo"):
            for drama_id in policy.excluded_drama_ids(platform):
                self.assertFalse(policy.is_rank_excluded_drama(platform, drama_id))
        with patch.dict(policy.RANK_EXCLUSIONS, {"manbo": frozenset({"200"})}):
            self.assertTrue(policy.is_rank_excluded_drama("manbo", "200"))
            self.assertFalse(policy.is_excluded_drama("manbo", "200"))

    def test_both_cover_albums_keep_rank_items_values_and_positions(self):
        for song_id in SONGS:
            with self.subTest(song_id=song_id):
                store = self.fetch_store(song_id)
                original = deepcopy(store["manbo"]["ranks"])
                ranks.filter_archived_dramas(store, archived_by_platform={})
                self.assertEqual(store["manbo"]["ranks"], original)
                snapshot = ranks.build_rank_snapshot_payloads(
                    store, platforms=("manbo",), history_date="2026-11-06",
                    generated_at="2026-11-06T00:00:00+00:00",
                )["manbo"]["list"]["ranks"]["diamond_monthly"]["items"]
                self.assertEqual(len(snapshot), 20)
                self.assertEqual((snapshot[13]["drama_id"], snapshot[13]["rank_value"],
                                  snapshot[13]["position"]), (song_id, 86.5, 14))
                self.assertEqual(snapshot[14]["position"], 15)
                self.assertEqual(store["manbo"]["dramas"][song_id]["name"], "翻唱合集")

    def test_original_positions_survive_other_rank_exclusions_and_archival(self):
        store = self.fetch_store()
        with patch.dict(policy.RANK_EXCLUSIONS, {"manbo": frozenset({"1002"})}):
            ranks.filter_archived_dramas(store, archived_by_platform={"manbo": {"1003"}})
        snapshot = ranks.build_rank_snapshot_payloads(
            store, platforms=("manbo",), history_date="2026-11-06",
            generated_at="2026-11-06T00:00:00+00:00",
        )["manbo"]["list"]["ranks"]["diamond_monthly"]["items"]
        self.assertEqual(len(snapshot), 18)
        by_id = {row["drama_id"]: row for row in snapshot}
        self.assertEqual(by_id[SONGS[1]]["position"], 14)
        self.assertEqual(by_id["1015"]["position"], 15)
        self.assertEqual(by_id["1020"]["position"], 20)
        self.assertNotIn("1002", by_id)
        self.assertNotIn("1003", by_id)

    def test_rank_publication_keeps_cover_album_and_trend_rank_position(self):
        store = self.fetch_store()
        with (
            patch.object(ranks, "upload_rank_trend_snapshot") as publish_trend,
            patch.object(ranks, "upload_full_ranks") as publish_latest,
        ):
            ranks.upload_rank_outputs(store, ("manbo",), archived_by_platform={})
        list_payload = publish_trend.call_args.args[3]
        row = list_payload["ranks"]["diamond_monthly"]["items"][13]
        self.assertEqual((row["drama_id"], row["position"], row["rank_value"]), (SONGS[1], 14, 86.5))
        badges = ranks._rank_badges_by_drama("manbo", list_payload)
        self.assertEqual(badges[SONGS[1]][0]["position"], 14)
        self.assertEqual(len(publish_latest.call_args.args[0]["manbo"]["ranks"]["diamond_monthly"]["items"]), 20)

    def test_same_album_keeps_separate_positions_and_values_on_multiple_ranks(self):
        store = self.fetch_store()
        responses = [
            {"b": {"radioDramaRespList": [
                {"radioDramaIdStr": "200", "hotValue": 99},
                {"radioDramaIdStr": SONGS[1], "hotValue": 88},
            ]}},
            {"b": {"radioDramaRespList": [
                {"radioDramaIdStr": SONGS[1], "diamondValue": 123.5},
            ]}},
        ]
        with (
            patch.object(ranks, "MANBO_RANKS", {
                "hot": (0, "热播榜", 20, "hotValue"),
                "diamond_monthly": (5, "钻石榜月榜", 20, "diamondValue"),
            }),
            patch.object(ranks, "request_manbo_json", side_effect=responses),
        ):
            ranks.fetch_manbo_ranks(store)
        ranks.filter_archived_dramas(store, archived_by_platform={})
        items = store["manbo"]["ranks"]
        self.assertEqual(items["hot"]["items"][1], {"dramaId": SONGS[1], "position": 2, "hotValue": 88})
        self.assertEqual(items["diamond_monthly"]["items"][0],
                         {"dramaId": SONGS[1], "position": 1, "diamondValue": 123.5})

    def test_cv_lookup_clears_stale_cv_names_without_enqueuing_cover_albums(self):
        store = {"missevan": {"dramas": {}}, "manbo": {"dramas": {
            SONGS[0]: {"name": "旧合集", "maincvs": ["错误CV"], "view_count": 123},
            SONGS[1]: {"name": "星未眠翻唱合集", "maincvs": ["错误CV"], "diamond_value": 456},
            "200": {"name": "待补剧"},
            "201": {"name": "正常剧"},
        }}}
        info = {"records": [
            {"dramaId": song_id, "mainCvNames": ["错误CV"], "mainCvIds": [11], "cover": "bad.jpg"}
            for song_id in SONGS
        ] + [{"dramaId": "201", "mainCvNames": ["正常CV"], "mainCvIds": [22], "cover": "ok.jpg"}]}
        with (
            patch.object(ranks, "_load_upstash_json", side_effect=lambda key: info if key == "manbo:info:v2" else {}),
            patch.object(ranks, "append_new_drama_ids_atomic") as enqueue,
        ):
            ranks.lookup_cvs(store)
        enqueue.assert_called_once_with([], ["200"])
        for song_id in SONGS:
            self.assertIsNone(store["manbo"]["dramas"][song_id]["maincvs"])
        self.assertEqual(store["manbo"]["dramas"][SONGS[0]]["view_count"], 123)
        self.assertEqual(store["manbo"]["dramas"][SONGS[1]]["diamond_value"], 456)
        self.assertEqual(store["manbo"]["dramas"]["201"]["maincvs"], ["正常CV"])

    def test_non_drama_category_can_supply_rank_metrics_without_admitting_to_library(self):
        payload = {"b": {"title": "翻唱合集", "category": 4, "watchCount": 123, "payCount": 0}}
        with patch.object(ranks, "request_manbo_json", return_value=payload):
            entry = {}
            ranks._fetch_one_manbo(SONGS[1], entry)
            self.assertEqual(entry["view_count"], 123)
            with self.assertRaises(ranks.RejectedDramaRecord):
                ranks._fetch_one_manbo("200", {})

    def test_rejected_detail_does_not_remove_rank_only_entry_or_its_source_value(self):
        store = self.fetch_store()
        original = deepcopy(store["manbo"]["ranks"])
        pending = {SONGS[1]}
        with (
            patch.object(ranks, "_fetch_one_manbo", side_effect=ranks.RejectedDramaRecord("empty title")),
            patch.object(ranks, "save_json"),
        ):
            ranks.fetch_manbo_drama_details({SONGS[1]}, store, skip_danmaku=True, pending_ids=pending)
        self.assertEqual(store["manbo"]["ranks"], original)
        self.assertEqual(store["manbo"]["dramas"][SONGS[1]]["name"], "翻唱合集")
        self.assertEqual(pending, set())

    def test_legacy_rank_items_still_receive_fallback_positions(self):
        self.assertEqual(ranks._coerce_rank_item({"dramaId": "200"}, 3)["position"], 3)
        self.assertEqual(ranks._coerce_rank_item("200", 4)["position"], 4)


class StaleRankCvCleanupTests(unittest.TestCase):
    def make_store(self):
        store = {"_meta": {}}
        for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
            store[platform] = {
                "dramas": {
                    blocked: {"name": "禁止入库内容", "maincvs": ["旧主役"],
                              "cover": "cover.jpg", "view_count": 123, "danmaku_uid_count": 2},
                    "200": {"name": "正常作品", "maincvs": ["正常主役"], "view_count": 456},
                },
                "ranks": {"hot": {"name": "榜单", "items": [
                    {"dramaId": blocked, "position": 14, "hotValue": 88.5},
                    {"dramaId": "200", "position": 15, "hotValue": 77},
                ]}},
            }
        return store

    def assert_clean_and_preserved(self, actual, original):
        expected = deepcopy(original)
        for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
            expected[platform]["dramas"][blocked]["maincvs"] = None
            self.assertEqual(actual[platform], expected[platform])

    def test_common_cleanup_clears_both_platforms_without_changing_other_data(self):
        store = self.make_store()
        original = deepcopy(store)
        ranks.filter_archived_dramas(store, archived_by_platform={})
        self.assert_clean_and_preserved(store, original)

    def test_publication_without_new_samples_cleans_authoritative_history(self):
        generated_at = "2026-11-06T12:00:00+00:00"
        dates = ["2026-11-04", "2026-11-05"]
        for keep_cached_entry in (True, False):
            with self.subTest(keep_cached_entry=keep_cached_entry):
                fake = FakeUpstash()
                store = self.make_store()
                original = deepcopy(store)
                histories = {}
                for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
                    records = {}
                    for drama_id, entry in store[platform]["dramas"].items():
                        entry["fetched_at"] = generated_at  # Fresh cache; no metric captured this run.
                        records[drama_id] = {
                            "version": 2, "id": drama_id, "name": entry["name"],
                            "maincvs": entry["maincvs"], "cover": "old-cover.jpg",
                            "lastRank": {"date": "2026-11-03", "ranks": [
                                {"key": "hot", "name": "榜单", "position": 19},
                            ]},
                            "samples": {date: {
                                "generated_at": date + "T10:00:00Z",
                                "metrics": {"view_count": 123, "danmaku_uid_count": 2},
                                "ranks": [{"key": "hot", "name": "榜单", "position": 14}],
                            } for date in dates},
                        }
                    histories[platform] = deepcopy(records)
                    key = upstash_v2.NORMAL_TREND_V2_KEYS[platform]
                    fake.hashes[key] = {
                        "__meta__": upstash_v2.compact_json({
                            "version": 2, "platform": platform, "dates": dates,
                            "updated_at": dates[-1], "retentionDates": 45,
                        }),
                        **{drama_id: upstash_v2.compact_json(record)
                           for drama_id, record in records.items()},
                    }
                    if not keep_cached_entry:
                        # A historical entity can also be entirely absent from latest.
                        store[platform]["dramas"].pop(blocked)
                    metrics = ranks._build_metric_payload(store, platform, "2026-11-06", generated_at)
                    candidate = ranks.build_rank_trend_payload(
                        None, platform, "2026-11-06", metrics, {"ranks": {}},
                        generated_at=generated_at, rank_sample_captured=False,
                    )
                    self.assertEqual(candidate["dramas"], {})

                with (
                    patch.object(ranks, "now_iso", return_value=generated_at),
                    patch.object(ranks, "upstash_request", side_effect=fake),
                    patch.object(ranks, "lookup_cvs", side_effect=AssertionError("Unexpected CV lookup")),
                    patch("builtins.print"),
                ):
                    ranks.upload_rank_outputs(
                        store, ("missevan", "manbo"), archived_by_platform={},
                        include_rank_samples=False, include_peak_trend=False,
                    )

                latest = json.loads(fake.strings["ranks:latest"])
                for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
                    key = upstash_v2.NORMAL_TREND_V2_KEYS[platform]
                    saved = {drama_id: json.loads(raw) for drama_id, raw in fake.hashes[key].items()
                             if drama_id != "__meta__"}
                    expected = deepcopy(histories[platform])
                    expected[blocked]["maincvs"] = None
                    # Entire entity equality covers all samples, metrics, ranks and lastRank.
                    self.assertEqual(saved, expected)
                    self.assertEqual(json.loads(fake.hashes[key]["__meta__"])["dates"], dates)
                    self.assertEqual(latest[platform]["ranks"], original[platform]["ranks"])
                    expected_latest = deepcopy(store[platform])
                    if keep_cached_entry:
                        self.assertIsNone(latest[platform]["dramas"][blocked]["maincvs"])
                    self.assertEqual(latest[platform], expected_latest)

    def test_normal_trend_publisher_cleans_candidates_with_and_without_existing_v2(self):
        for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
            for has_current in (False, True):
                with self.subTest(platform=platform, has_current=has_current):
                    fake = FakeUpstash()
                    payload = {"dates": ["2026-11-06"], "updated_at": "2026-11-06",
                               "dramas": {drama_id: {
                                   "id": drama_id, "name": drama_id, "maincvs": ["接口主役"],
                                   "samples": {"2026-11-06": {
                                       "metrics": {"view_count": 123},
                                       "ranks": [{"key": "hot", "name": "榜单", "position": 14}],
                                   }},
                               } for drama_id in (blocked, "200")}}
                    original = deepcopy(payload)
                    meta, expected = upstash_v2.build_normal_trend_v2(payload, platform)
                    key = upstash_v2.NORMAL_TREND_V2_KEYS[platform]
                    if has_current:
                        fake.hashes[key] = {
                            "__meta__": upstash_v2.compact_json(meta),
                            **{drama_id: upstash_v2.compact_json(record)
                               for drama_id, record in expected.items()},
                        }
                    expected[blocked]["maincvs"] = None

                    upstash_v2.publish_normal_trend_v2(platform, payload, upstash=fake)

                    saved = {drama_id: json.loads(raw) for drama_id, raw in fake.hashes[key].items()
                             if drama_id != "__meta__"}
                    self.assertEqual(saved, expected)
                    self.assertEqual(payload, original)

    def test_publication_without_cv_lookup_clears_latest_and_trend_metadata(self):
        store = self.make_store()
        original = deepcopy(store)
        with (
            patch.object(ranks, "lookup_cvs", side_effect=AssertionError("CV lookup must not be required")),
            patch.object(ranks, "upload_rank_trend_snapshot") as trend,
            patch.object(ranks, "upload_full_ranks") as latest,
        ):
            ranks.upload_rank_outputs(
                store, ("missevan", "manbo"), archived_by_platform={}, include_peak_trend=False,
            )
        self.assert_clean_and_preserved(latest.call_args.args[0], original)
        self.assertEqual(trend.call_count, 2)
        for call in trend.call_args_list:
            platform, _date, metrics, lists = call.args
            blocked = "94604" if platform == "missevan" else SONGS[1]
            self.assertIsNone(metrics["dramas"][blocked]["maincvs"])
            self.assertEqual(metrics["dramas"]["200"]["maincvs"], ["正常主役"])
            rows = lists["ranks"]["hot"]["items"]
            self.assertEqual([(r["drama_id"], r["position"], r["rank_value"]) for r in rows],
                             [(blocked, 14, 88.5), ("200", 15, 77)])

    def test_only_danmaku_cli_cleans_old_cvs_for_both_and_single_platform_runs(self):
        for options in ([], ["--missevan-only"], ["--manbo-only"]):
            with self.subTest(options=options):
                store = self.make_store()
                original = deepcopy(store)
                saved = []

                def refresh_danmaku(target, *, force, do_missevan, do_manbo):
                    # main() must clean old CVs before refreshing or saving.
                    self.assert_clean_and_preserved(target, original)
                    for platform, enabled in (("missevan", do_missevan), ("manbo", do_manbo)):
                        if enabled:
                            for entry in target[platform]["dramas"].values():
                                ranks.assign_fetched_metric(entry, "danmaku_uid_count", 7)

                with (
                    patch.object(sys, "argv", ["fetch_rank_data.py", "--only-danmaku", *options]),
                    patch.object(ranks, "load_env_file"),
                    patch.object(ranks, "load_initial_rank_store", return_value=store),
                    patch.object(ranks, "load_archived_drama_ids", return_value={"missevan": set(), "manbo": set()}),
                    patch.object(ranks, "only_danmaku_mode", side_effect=refresh_danmaku),
                    patch.object(ranks, "lookup_cvs", side_effect=AssertionError("Unexpected CV lookup")),
                    patch.object(ranks, "save_json", side_effect=lambda path, payload: saved.append(deepcopy(payload))),
                    patch.object(ranks, "upload_rank_trend_snapshot") as trend,
                    patch.object(ranks, "upload_full_ranks") as latest,
                    patch.object(ranks, "upload_missevan_peak_trend") as peak,
                    patch.object(ranks, "clear_418_checkpoint_after_publish"),
                    patch("builtins.print"),
                ):
                    ranks.main()
                self.assertTrue(saved)
                peak.assert_not_called()
                published = latest.call_args.args[0]
                for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
                    self.assertIsNone(saved[0][platform]["dramas"][blocked]["maincvs"])
                    self.assertIsNone(published[platform]["dramas"][blocked]["maincvs"])
                    self.assertEqual(published[platform]["dramas"]["200"]["maincvs"], ["正常主役"])
                    self.assertEqual(published[platform]["ranks"], original[platform]["ranks"])
                for call in trend.call_args_list:
                    platform, _date, metrics, lists = call.args
                    blocked = "94604" if platform == "missevan" else SONGS[1]
                    self.assertIsNone(metrics["dramas"][blocked]["maincvs"])
                    self.assertEqual(metrics["dramas"][blocked]["danmaku_uid_count"], 7)
                    self.assertFalse(lists["ranks"])

    def test_archive_load_failure_does_not_partially_mutate_cached_entries(self):
        store = self.make_store()
        original = deepcopy(store)
        with patch.object(ranks, "load_archived_drama_ids", side_effect=RuntimeError("archive unavailable")):
            with self.assertRaisesRegex(RuntimeError, "archive unavailable"):
                ranks.filter_archived_dramas(store)
        self.assertEqual(store, original)

    def test_low_level_latest_publication_cleans_copy_without_requiring_lookup(self):
        store = self.make_store()
        original = deepcopy(store)
        with patch.object(ranks, "publish_rank_string") as publish, patch("builtins.print"):
            ranks.upload_full_ranks(store)
        self.assert_clean_and_preserved(publish.call_args.args[1], original)
        self.assertEqual(store, original)

    def test_danmaku_repair_publication_clears_old_cvs_without_changing_rankings(self):
        date = "2026-11-06"
        for platform, blocked in (("missevan", "94604"), ("manbo", SONGS[1])):
            with self.subTest(platform=platform):
                store = self.make_store()
                original = deepcopy(store)
                trend = {"platform": platform, "dates": [date], "dramas": {
                    blocked: {"id": blocked, "name": "禁止入库内容", "maincvs": ["旧主役"],
                              "samples": {date: {"metrics": {"view_count": 123}, "ranks": []}}},
                    "200": {"id": "200", "name": "正常作品", "maincvs": ["正常主役"],
                            "samples": {date: {"metrics": {"danmaku_uid_count": None}, "ranks": []}}},
                }}
                loaded = {"latest": deepcopy(store), "trend": deepcopy(trend)}
                with (
                    patch.object(ranks, "_load_repair_publish_sources", return_value=("source-version", store, trend)),
                    patch.object(ranks, "load_archived_drama_ids", return_value={}),
                    patch.object(ranks, "publish_repaired_rank_snapshot") as publish,
                    patch("builtins.print"),
                ):
                    ranks.write_repaired_danmaku_layers(platform, date, {"200": 7}, loaded)
                published_latest, published_trend = publish.call_args.args[1:3]
                for p, blocked_id in (("missevan", "94604"), ("manbo", SONGS[1])):
                    self.assertIsNone(published_latest[p]["dramas"][blocked_id]["maincvs"])
                    self.assertEqual(published_latest[p]["ranks"], original[p]["ranks"])
                    self.assertEqual(published_latest[p]["dramas"]["200"]["maincvs"], ["正常主役"])
                self.assertIsNone(published_trend["dramas"][blocked]["maincvs"])
                self.assertEqual(published_trend["dramas"][blocked]["samples"],
                                 loaded["trend"]["dramas"][blocked]["samples"])
                self.assertEqual(published_latest[platform]["dramas"]["200"]["danmaku_uid_count"], 7)


class CvExclusionDefenseTests(unittest.TestCase):
    def test_stale_library_and_count_entries_cannot_contribute_to_cv_totals(self):
        missevan = {
            "83940": {"dramaId": 83940, "maincvs": [11], "cvnames": {"11": "演员"}, "needpay": True},
            "94604": {"dramaId": 94604, "maincvs": [11], "cvnames": {"11": "演员"}, "needpay": True},
            "100": {"dramaId": 100, "maincvs": [11], "cvnames": {"11": "演员"}, "needpay": True},
        }
        manbo = {"records": [
            {"dramaId": song_id, "mainCvIds": [22], "mainCvNames": ["演员"], "needpay": True}
            for song_id in SONGS
        ] + [{"dramaId": "200", "mainCvIds": [22], "mainCvNames": ["演员"], "needpay": True}]}
        payload, full, paid = cv.build_cv_rank_outputs(
            missevan_store=missevan, manbo_store=manbo,
            missevan_counts={"83940": {"view_count": 999}, "94604": {"view_count": 999}, "100": {"view_count": 10}},
            manbo_counts={**{song_id: {"view_count": 999} for song_id in SONGS}, "200": {"view_count": 20}},
            cvid_map={"演员": {"displayName": "演员", "missevanCvId": 11, "manboCvId": 22}},
            generated_at="2026-11-06T00:00:00+00:00",
        )
        self.assertEqual((payload["missevanDramaCount"], payload["manboDramaCount"]), (1, 1))
        for result in [full, paid]:
            self.assertEqual(result["missevan"][0]["totalViewCount"], 10)
            self.assertEqual(result["manbo"][0]["totalViewCount"], 20)
            self.assertEqual(result["missevan"][0]["workCount"], 1)
            self.assertEqual(result["manbo"][0]["workCount"], 1)


class BothPlatformRankAndOngoingTests(unittest.TestCase):
    def fetch_missevan_store(self):
        rows = [{"id": i + 1000, "name": f"作品{i}", "view_count": i * 10}
                for i in range(1, 21)]
        rows[0] = {"name": "无效行但不改变后续的原始名次"}
        rows[13] = {"id": 94604, "name": "重创星纪元", "cover": "cover.jpg", "view_count": 12345}

        class Requester:
            def request_json(self, url):
                if "/x/rank/peak-details" in url:
                    return {"data": {"data": []}}
                return {"info": {"data": rows}}

        store = {"_meta": {}, "missevan": {"ranks": {}, "dramas": {}}, "manbo": {"ranks": {}, "dramas": {}}}
        with (
            patch.object(ranks, "MISSEVAN_RANKS", {"popular_weekly": (2, 2, "人气周榜", 50, 30)}),
            patch.object(ranks, "load_series_info", return_value={}),
            patch("builtins.print"),
        ):
            found, _eligible, _deferred = ranks.fetch_missevan_ranks(Requester(), store)
        self.assertIn("94604", found)
        return store

    def test_missevan_keeps_original_rank_value_and_position_after_other_item_is_archived(self):
        store = self.fetch_missevan_store()
        ranks.filter_archived_dramas(store, archived_by_platform={"missevan": {"1002"}})
        lists = ranks._build_rank_list_payload(store, "missevan", "2026-11-06", "2026-11-06T00:00:00+00:00")
        by_id = {row["drama_id"]: row for row in lists["ranks"]["popular_weekly"]["items"]}
        self.assertEqual((by_id["94604"]["position"], by_id["94604"]["rank_value"]), (14, 12345))
        self.assertEqual(by_id["1015"]["position"], 15)
        self.assertNotIn("1002", by_id)
        self.assertEqual(store["missevan"]["dramas"]["94604"]["name"], "重创星纪元")

    def test_ongoing_export_and_read_filter_all_library_exclusions_on_both_platforms(self):
        for platform in ("missevan", "manbo"):
            with self.subTest(platform=platform):
                records = {f"alias-{drama_id}": {"dramaId": drama_id}
                           for drama_id in policy.excluded_drama_ids(platform)}
                records["200"] = {"dramaId": "200"}
                exported = ongoing.build_payload(platform, records)
                self.assertEqual(exported["records"], {"200": {"dramaId": "200"}})
                with patch.object(ranks, "_load_upstash_json", return_value={"records": records}):
                    self.assertEqual(ranks.load_ongoing_drama_ids(platform), {"200"})

    def test_queue_read_filters_both_platforms_without_dropping_ordinary_ids(self):
        payload = {platform: [*sorted(policy.excluded_drama_ids(platform)), "200"]
                   for platform in ("missevan", "manbo")}
        with patch.object(sync, "upstash_request", return_value=json.dumps(payload)):
            self.assertEqual(sync.load_queue(), {"missevan": ["200"], "manbo": ["200"]})

    def test_missevan_cv_lookup_preserves_display_data_but_never_queues_rank_only_content(self):
        store = self.fetch_missevan_store()
        store["missevan"]["dramas"]["94604"]["maincvs"] = ["旧CV"]
        source = {"94604": {"maincvs": [11], "cvnames": {"11": "旧CV"}, "cover": "cover.jpg"}}
        with (
            patch.object(ranks, "_load_upstash_json", side_effect=lambda key: source if key == "missevan:info:v2" else {"records": []}),
            patch.object(ranks, "append_new_drama_ids_atomic") as queue,
            patch("builtins.print"),
        ):
            ranks.lookup_cvs(store)
        queue.assert_not_called()
        self.assertIsNone(store["missevan"]["dramas"]["94604"]["maincvs"])
        self.assertEqual(store["missevan"]["dramas"]["94604"]["name"], "重创星纪元")

    def test_missevan_detail_rejection_does_not_remove_rank_only_entry(self):
        store = self.fetch_missevan_store()
        original = deepcopy(store["missevan"]["ranks"])
        pending = {"94604"}
        with (
            patch.object(ranks, "_fetch_one_missevan", side_effect=ranks.RejectedDramaRecord("empty title")),
            patch.object(ranks, "save_json"),
            patch("builtins.print"),
        ):
            ranks.fetch_missevan_drama_details(None, {"94604"}, store, skip_danmaku=True, pending_ids=pending)
        self.assertEqual(store["missevan"]["ranks"], original)
        self.assertEqual(store["missevan"]["dramas"]["94604"]["name"], "重创星纪元")
        self.assertEqual(pending, set())

    def test_missevan_rank_detail_can_read_non_library_category(self):
        requester = Mock()

        def respond(url):
            if "getdrama?" in url:
                return {"info": {"drama": {"id": 94604, "name": "重创星纪元", "catalog": 123, "view_count": 777}}}
            return {"info": {}}

        requester.request_json.side_effect = respond
        entry = {}
        ranks._fetch_one_missevan(requester, "94604", entry, skip_danmaku=True, clear_danmaku_on_skip=True)
        self.assertEqual(entry["view_count"], 777)
        with self.assertRaises(ranks.RejectedDramaRecord):
            ranks._fetch_one_missevan(requester, "200", {}, skip_danmaku=True, clear_danmaku_on_skip=True)


if __name__ == "__main__":
    unittest.main()
