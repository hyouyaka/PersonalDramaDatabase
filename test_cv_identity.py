import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_cv_ranks as ranks
import cvid_map_tools as cvs
import upstash_v2 as trends
from cv_identity import CvIdentityIndex, ensure_cv_identities
from platform_sync import replace_manbo_main_cv_ids, replace_missevan_main_cv_ids
from refresh_platform_metadata import apply_manbo_intro_cv_fallback, apply_missevan_intro_cv_fallback
from test_upstash_v2 import FakeUpstash


UUID = "161c78e0-e441-48dc-8db2-a0ec1266cd0c"
KEY = f"person:{UUID}"


class CvIdentityTests(unittest.TestCase):
    def mapping(self):
        return {"Old": {"displayName": "New", "aliases": ["Old"], "cvKey": KEY,
                        "missevanCvId": "001", "manboCvId": "90071992547409931234"}}

    def test_assignment_is_persistent_and_rename_cannot_replace_key(self):
        mapping = {"Old": {"missevanCvId": 1, "manboCvId": 2}}
        self.assertTrue(ensure_cv_identities(mapping))
        original = copy.deepcopy(mapping)
        self.assertFalse(ensure_cv_identities(mapping))
        renamed = {"New": {"missevanCvId": "1", "manboCvId": "2"}}
        ensure_cv_identities(renamed, original)
        self.assertEqual(renamed["New"]["cvKey"], original["Old"]["cvKey"])
        self.assertIn("Old", renamed["New"]["aliases"])
        renamed["New"]["cvKey"] = KEY
        with self.assertRaisesRegex(ValueError, "Cannot change persisted"):
            ensure_cv_identities(renamed, original)

    def test_ids_are_opaque_and_platform_scoped(self):
        index = CvIdentityIndex(self.mapping())
        self.assertEqual(index.resolve("missevan", "001", "Old"), (KEY, "New", True))
        self.assertEqual(index.resolve("manbo", "90071992547409931234", "Old")[0], KEY)
        self.assertEqual(index.resolve("missevan", "1", "Old")[0], "missevan:id:1")
        self.assertEqual(index.resolve("manbo", "001", "Old")[0], "manbo:id:001")
        self.assertEqual(index.resolve("missevan", None, "Old")[0], "missevan:name:Old")

    def test_new_virtual_cv_gets_one_uuid_for_both_platforms(self):
        for create in (cvs.ensure_generated_manbo_cv_entry, cvs.ensure_generated_missevan_cv_entry):
            with self.subTest(create=create.__name__):
                mapping = {}
                allocator = Mock(return_value=UUID)
                self.assertEqual(create(mapping, "CV", allocator=allocator), (UUID, True))
                self.assertEqual(create(mapping, "CV", allocator=allocator), (UUID, False))
                row = mapping["CV"]
                self.assertEqual((row["cvKey"], row["cvId"], row["missevanCvId"], row["manboCvId"]),
                                 (KEY, UUID, UUID, UUID))
                allocator.assert_called_once()

    def test_uuid_allocator_uses_shared_atomic_reservation(self):
        client = Mock(return_value=UUID)
        self.assertEqual(cvs.UpstashCvUuidAllocator(upstash=client)({}, "CV"), UUID)
        command = client.call_args.args[0]
        self.assertEqual(command[0], "EVAL")
        self.assertIn("cvid:generated:uuid", command)

    def test_uuid_flows_through_both_intro_fallbacks(self):
        for platform in ("missevan", "manbo"):
            with self.subTest(platform=platform):
                mapping = {}
                if platform == "missevan":
                    with (
                        patch("refresh_platform_metadata._get_missevan_cv_name_map", return_value={}),
                        patch("refresh_platform_metadata.save_combined_map"),
                    ):
                        row = apply_missevan_intro_cv_fallback(
                            {"type": 4}, "100", [{"display_name": "Actor", "role_name": "Role"}],
                            combined_map=mapping, search_cv=lambda _: None,
                            generated_cv_id_allocator=lambda *_: UUID,
                        )
                    self.assertEqual(row["maincvs"], [UUID])
                else:
                    with patch("refresh_platform_metadata.extract_manbo_intro_cv_candidates", return_value=[
                        {"display_name": "Actor", "role_name": "Role"},
                    ]):
                        row = apply_manbo_intro_cv_fallback(
                            {"dramaId": "100", "type": 4}, "intro", combined_map=mapping,
                            search_cv=lambda _: None, generated_cv_id_allocator=lambda *_: UUID,
                        )
                    self.assertEqual(row["mainCvIds"], [UUID])
                self.assertEqual(mapping["Actor"]["cvKey"], KEY)

    def test_uuid_upgrades_preserve_person_identity_and_references(self):
        for platform in ("missevan", "manbo"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                mapping = {"Actor": {"cvKey": KEY, "missevanCvId": UUID, "cvId": UUID,
                                     "manboCvId": UUID, "aliases": []}}
                path = Path(tmp) / "map.json"
                path.write_text(json.dumps(mapping), encoding="utf-8")
                cat = {"100": {"dramaId": "100", "maincvs": [UUID if platform == "manbo" else 12],
                               "cvnames": {UUID if platform == "manbo" else "12": "Actor"}}}
                manbo = {"records": [{"dramaId": "200", "mainCvIds": [34 if platform == "manbo" else UUID],
                                      "mainCvNames": ["Actor"]}]}
                with patch.object(cvs, "COMBINED_CVID_MAP_PATH", path):
                    stats = cvs.update_combined_cvid_map(cat, manbo)
                saved = json.loads(path.read_text(encoding="utf-8"))["Actor"]
                self.assertEqual(saved["cvKey"], KEY)
                self.assertEqual(saved[f"{platform}CvId"], "12" if platform == "missevan" else "34")
                old_cat = {"100": {"dramaId": "100", "maincvs": [UUID], "cvnames": {UUID: "Actor"}}}
                old_manbo = {"records": [{"dramaId": "200", "mainCvIds": [UUID], "mainCvNames": ["Actor"]}]}
                if platform == "missevan":
                    self.assertEqual(replace_missevan_main_cv_ids(old_cat, stats["missevan_generated_replacements"]), {"100"})
                else:
                    self.assertEqual(replace_manbo_main_cv_ids(old_manbo, stats["manbo_generated_replacements"]), {"200"})

    def test_all_rank_rows_and_partner_members_use_same_person_key(self):
        mapping = self.mapping()
        mapping["Old"]["avatar"] = "https://example.test/avatar.jpg"
        mapping["Other"] = {"missevanCvId": "002", "manboCvId": "3", "displayName": "Other"}
        cat = {"100": {"dramaId": "100", "title": "Work", "type": 4, "needpay": True,
                       "maincvs": ["001", "002"], "cvnames": {"001": "Old", "002": "Other"}}}
        manbo = {"records": [{"dramaId": "200", "name": "Work", "type": 4, "needpay": True,
                              "mainCvIds": ["90071992547409931234", "3"], "mainCvNames": ["New", "Other"]}]}
        payload = ranks.build_cv_ranks_payload(
            missevan_store=cat, manbo_store=manbo, missevan_counts={"100": {"view_count": 10}},
            manbo_counts={"200": {"view_count": 20}}, cvid_map=mapping, series_info={}, generated_at="2026-11-20T00:00:00Z",
        )
        self.assertEqual(payload["cvIdentityVersion"], 1)
        for group in ("rankings", "paidRankings", "seriesCountRankings"):
            for platform in ("missevan", "manbo", "combined"):
                rows = payload[group][platform]
                self.assertTrue(all("cvKey" in row for row in rows))
                self.assertEqual(next(row for row in rows if row["cvName"] == "New")["cvKey"], KEY)
        partners = payload["partnerRankings"]["viewCount"]
        self.assertTrue(partners)
        self.assertIn(KEY, [cv["cvKey"] for cv in partners[0]["cvs"]])
        member = next(cv for cv in partners[0]["cvs"] if cv["cvKey"] == KEY)
        self.assertEqual(member["missevanCvId"], "001")
        self.assertEqual(member["manboCvId"], "90071992547409931234")
        self.assertEqual(member["avatar"], "https://example.test/avatar.jpg")
        self.assertEqual(next(row for row in payload["rankings"]["combined"] if row["cvKey"] == KEY)["totalViewCount"], 30)

    def test_unmapped_same_names_do_not_merge_across_ids_or_platforms(self):
        payload = ranks.build_cv_ranks_payload(
            missevan_store={"1": {"dramaId": "1", "maincvs": ["001", "1"], "cvnames": {"001": "Same", "1": "Same"}}},
            manbo_store={"records": [{"dramaId": "2", "mainCvIds": ["1"], "mainCvNames": ["Same"]}]},
            missevan_counts={"1": {"view_count": 5}}, manbo_counts={"2": {"view_count": 8}},
            cvid_map={}, series_info={}, generated_at="2026-11-20T00:00:00Z",
        )
        self.assertEqual({row["cvKey"] for row in payload["rankings"]["combined"]},
                         {"missevan:id:001", "missevan:id:1", "manbo:id:1"})
        self.assertEqual(len(payload["rankings"]["missevan"]), 2)

    def test_v3_merges_total_and_paid_by_key_and_excludes_name_only(self):
        total = {"cvKey": KEY, "cvName": "New", "rank": 2, "totalViewCount": 50, "works": [{"dramaId": "1"}]}
        paid = {**total, "cvName": "Old", "rank": 3, "totalViewCount": 30}
        _, fields = trends.build_cv_trend_v3(
            {"missevan": [total, {"cvKey": "missevan:name:Unknown", "cvName": "Unknown"}]},
            {"missevan": [paid]}, history_date="2026-11-20", generated_at="now", publication_id="pub",
        )
        self.assertEqual(len(fields), 1)
        entry = fields[f"missevan:{KEY}"]
        self.assertEqual(entry["works"], [{"dramaId": "1"}])
        self.assertEqual(entry["samples"]["2026-11-20"], {
            "cvKey": KEY, "generated_at": "now", "publicationId": "pub",
            "metrics": {"totalViewCount": 50, "paidViewCount": 30}, "ranks": {"total": 2, "paid": 3},
        })


    def test_platform_history_moves_to_person_key_after_confirmation(self):
        fake = FakeUpstash()
        row = {"cvKey": "missevan:id:001", "cvName": "Old", "rank": 1, "totalViewCount": 10}
        trends.publish_cv_trend_v3({"missevan": [row]}, {}, cvid_map={}, history_date="2026-11-13",
                                   generated_at="old", publication_id="historical", upstash=fake)
        row = {**row, "cvKey": KEY, "cvName": "New", "totalViewCount": 20}
        trends.publish_cv_trend_v3({"missevan": [row]}, {}, cvid_map=self.mapping(), history_date="2026-11-20",
                                   generated_at="new", publication_id="current", upstash=fake)
        snapshot = fake.hashes[trends.CV_TREND_V3_KEY]
        self.assertNotIn("missevan:missevan:id:001", snapshot)
        entry = json.loads(snapshot[f"missevan:{KEY}"])
        self.assertEqual(set(entry["samples"]), {"2026-11-13", "2026-11-20"})
        self.assertTrue(all(sample["cvKey"] == KEY for sample in entry["samples"].values()))
        self.assertEqual(entry["samples"]["2026-11-13"]["publicationId"], "historical")

    def test_unranked_person_keeps_history_with_empty_current_works(self):
        fake = FakeUpstash()
        row = {"cvKey": KEY, "cvName": "New", "rank": 1, "totalViewCount": 10, "works": [{"dramaId": "1"}]}
        trends.publish_cv_trend_v3({"missevan": [row]}, {}, cvid_map=self.mapping(), history_date="2026-11-13",
                                   generated_at="old", publication_id="historical", upstash=fake)
        trends.publish_cv_trend_v3({}, {}, cvid_map=self.mapping(), history_date="2026-11-20",
                                   generated_at="new", publication_id="current", upstash=fake)
        entry = json.loads(fake.hashes[trends.CV_TREND_V3_KEY][f"missevan:{KEY}"])
        self.assertEqual(entry["works"], [])
        self.assertEqual(set(entry["samples"]), {"2026-11-13"})


    def test_alias_with_missing_publication_id_preserves_known_historical_id(self):
        sample = {"generated_at": "2026-11-20T05:17:49+00:00", "publicationId": "historical",
                  "metrics": {"totalViewCount": 10}, "ranks": {"total": 1}}
        for existing_id, incoming_id in (("historical", None), (None, "historical")):
            with self.subTest(existing_id=existing_id):
                result = trends._merge_cv_identity_history(
                    {"cvKey": KEY, "samples": {"2026-11-20": {**sample, "publicationId": existing_id}}},
                    {"cvKey": KEY, "samples": {"2026-11-20": {**sample, "publicationId": incoming_id}}},
                )
                self.assertEqual(result["samples"]["2026-11-20"]["publicationId"], "historical")

    def test_historical_alias_cannot_clear_current_works(self):
        for older_date in ("2026-07-10", "2026-11-20"):
            current = {"cvKey": KEY, "works": [{"dramaId": "1"}], "samples": {
                "2026-11-20": {"generated_at": "2026-11-20T05:17:49+00:00", "metrics": {"totalViewCount": 10}},
            }}
            historical = {"cvKey": KEY, "works": [], "samples": {
                older_date: {"generated_at": "2026-11-20T05:17:49+00:00", "metrics": {"totalViewCount": 10}},
            }}
            for first, second in ((current, historical), (historical, current)):
                with self.subTest(older_date=older_date, current_first=first is current):
                    result = trends._merge_cv_identity_history(first, second)
                    self.assertEqual(result["works"], current["works"])

    def test_editor_accepts_v3_and_rejects_mismatched_sample_identity(self):
        import upstash_editor as editor
        _, fields = trends.build_cv_trend_v3(
            {"missevan": [{"cvKey": KEY, "cvName": "New", "rank": 1, "totalViewCount": 1}]}, {},
            history_date="2026-11-20", generated_at="now", publication_id="pub",
        )
        spec = editor.RESOURCE_SPECS[trends.CV_TREND_V3_KEY]
        editor.validate_payload(spec, fields)
        _, meta, *_ = editor._normalize_hash_payload(spec, fields, {"migratedFrom": "ranks:trend:cv:v2"}, "now")
        self.assertEqual(meta["version"], 3)
        self.assertEqual(meta["migratedFrom"], "ranks:trend:cv:v2")
        fields[f"missevan:{KEY}"]["samples"]["2026-11-20"]["cvKey"] = "missevan:id:1"
        with self.assertRaisesRegex(ValueError, "sample identity"):
            editor.validate_payload(spec, fields)

    def test_publication_preserves_v2_then_never_recreates_it_after_deletion(self):
        legacy_key = "ranks:trend:cv:v2"
        fake = FakeUpstash()
        # Malformed legacy data proves it is not used even to initialize an empty v3.
        legacy = {"__meta__": "legacy metadata", "legacy record": "unreadable legacy data"}
        fake.hashes[legacy_key] = copy.deepcopy(legacy)
        row = {"cvKey": KEY, "cvName": "New", "rank": 1, "totalViewCount": 10,
               "works": [{"platform": "missevan", "dramaId": "1", "viewCount": 10}]}
        for date, publication_id in (("2026-11-13", "historical"), ("2026-11-20", "current"), ("2026-11-20", "replacement")):
            ranks.upload_cv_trends(
                history_date=date, generated_at=date + "T00:00:00Z", publication_id=publication_id,
                full_rankings={"missevan": [row]}, full_paid_rankings={"missevan": [row]},
                cvid_map=self.mapping(), upstash=fake,
            )
            if publication_id == "historical":
                self.assertEqual(fake.hashes.pop(legacy_key), legacy)
            else:
                self.assertNotIn(legacy_key, fake.hashes)
        self.assertFalse(any(str(arg) == legacy_key for command in fake.commands for arg in command[1:]))
        snapshot = fake.hashes[trends.CV_TREND_V3_KEY]
        self.assertEqual(json.loads(snapshot["__meta__"])["version"], 3)
        self.assertEqual(json.loads(snapshot["__meta__"])["publicationId"], "replacement")
        self.assertEqual(set(snapshot), {"__meta__", f"missevan:{KEY}"})
        entry = json.loads(snapshot[f"missevan:{KEY}"])
        self.assertEqual(entry["samples"]["2026-11-13"]["publicationId"], "historical")
        self.assertEqual(entry["samples"]["2026-11-20"]["publicationId"], "replacement")
        self.assertEqual(entry["samples"]["2026-11-20"]["metrics"], {"totalViewCount": 10, "paidViewCount": 10})

    def test_v3_history_merges_without_requiring_or_rewriting_migration_markers(self):
        for markers in ({}, {"migratedFrom": "ranks:trend:cv:v2", "migrationSkippedFields": ["missevan:暂无"]}):
            with self.subTest(markers=markers):
                fake = FakeUpstash()
                row = {"cvKey": KEY, "cvName": "New", "rank": 1, "totalViewCount": 10}
                meta, fields = trends.build_cv_trend_v3({"missevan": [row]}, {}, history_date="2026-11-13",
                                                       generated_at="old", publication_id="historical")
                meta.update(markers)
                fake.hashes[trends.CV_TREND_V3_KEY] = {
                    "__meta__": json.dumps(meta), **{field: json.dumps(entry) for field, entry in fields.items()},
                }
                ranks.upload_cv_trends(history_date="2026-11-20", generated_at="new", publication_id="current",
                                       full_rankings={"missevan": [row]}, full_paid_rankings={}, cvid_map=self.mapping(), upstash=fake)
                actual_meta = json.loads(fake.hashes[trends.CV_TREND_V3_KEY]["__meta__"])
                for key, value in markers.items():
                    self.assertEqual(actual_meta[key], value)
                if not markers:
                    self.assertNotIn("migratedFrom", actual_meta)
                entry = json.loads(fake.hashes[trends.CV_TREND_V3_KEY][f"missevan:{KEY}"])
                self.assertEqual(entry["samples"]["2026-11-13"], fields[f"missevan:{KEY}"]["samples"]["2026-11-13"])

    def test_v3_retains_latest_50_dates_and_historical_publication_ids(self):
        from datetime import date, timedelta
        fake = FakeUpstash()
        dates = [(date(2026, 1, 1) + timedelta(weeks=index)).isoformat() for index in range(51)]
        samples = {day: {"cvKey": KEY, "generated_at": day, "publicationId": "historical-" + day,
                         "metrics": {"totalViewCount": index}, "ranks": {"total": 1}}
                   for index, day in enumerate(dates[:-1])}
        fake.hashes[trends.CV_TREND_V3_KEY] = {
            "__meta__": json.dumps({"version": 3, "kind": "cv", "platforms": {
                "missevan": {"dates": dates[:-1], "retentionDates": 50},
            }}),
            f"missevan:{KEY}": json.dumps({"version": 3, "cvKey": KEY, "cvName": "New", "works": [], "samples": samples}),
        }
        row = {"cvKey": KEY, "cvName": "New", "rank": 1, "totalViewCount": 100}
        ranks.upload_cv_trends(history_date=dates[-1], generated_at=dates[-1], publication_id="current",
                               full_rankings={"missevan": [row]}, full_paid_rankings={}, cvid_map=self.mapping(), upstash=fake)
        actual = json.loads(fake.hashes[trends.CV_TREND_V3_KEY][f"missevan:{KEY}"])["samples"]
        self.assertEqual(set(actual), set(dates[1:]))
        self.assertEqual(actual[dates[1]], samples[dates[1]])

    def test_v2_is_not_registered_as_an_active_cv_resource(self):
        import sync_new_drama_ids as sync
        import upstash_editor as editor
        self.assertNotIn("ranks:trend:cv:v2", sync.CV_REMOTE_KEYS)
        self.assertNotIn("ranks:trend:cv:v2", editor.RESOURCE_SPECS)


if __name__ == "__main__":
    unittest.main()
