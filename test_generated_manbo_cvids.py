import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import append_manbo_ids
import cvid_map_tools as cvs
import platform_sync
import refresh_platform_metadata as metadata
import upstash_editor
from test_manbo_intro_cvs import DRAMA_ID, PAYLOAD
from test_upstash_v2 import FakeUpstash


def identity(name, manbo_id=None, cat_id=None, **extras):
    return {"displayName": name, "aliases": [], "cvId": cat_id, "missevanCvId": cat_id,
            "manboCvId": manbo_id, "avatar": "", "notes": "", **extras}


def drama(cv_id, name="一礼", drama_id=DRAMA_ID):
    return {"dramaId": drama_id, "mainCvIds": [cv_id], "mainCvNames": [name],
            "mainCvNicknames": [name], "mainCvRoleNames": ["楚淮予"]}


class GeneratedManboRegistryTests(unittest.TestCase):
    def test_atomic_reservation_uses_separate_range_and_skips_occupied_ids(self):
        remote = Mock(side_effect=[0, 340002])
        allocator = cvs.UpstashGeneratedManboCvIdAllocator(upstash=remote, randbelow=Mock(side_effect=[0, 0]))
        result = allocator({"已有": identity("已有", 340000)}, "一礼")
        self.assertEqual(result, 340002)
        self.assertEqual([c[3] for (c,), _kwargs in remote.call_args_list], [cvs.GENERATED_MANBO_CVID_REGISTRY_KEY] * 2)
        self.assertEqual(remote.call_args_list[0].args[0][4:], ["name:一礼", "id:340001", "340001", "一礼"])

    def test_range_boundaries_and_exhaustion(self):
        self.assertTrue(cvs.is_generated_manbo_cvid("340000"))
        self.assertTrue(cvs.is_generated_manbo_cvid(349999))
        for value in [339999, 350000, None, "invalid", 1234567890123]:
            self.assertFalse(cvs.is_generated_manbo_cvid(value))
        remote = Mock()
        allocator = cvs.UpstashGeneratedManboCvIdAllocator(upstash=remote, randbelow=lambda _: 0)
        with patch.object(allocator, "collect_used", return_value=set(range(340000, 350000))):
            with self.assertRaisesRegex(RuntimeError, "exhausted"):
                allocator({}, "一礼")
        remote.assert_not_called()

    def test_rejected_registry_id_does_not_leak_another_platform_namespace(self):
        allocator = cvs.UpstashGeneratedManboCvIdAllocator(upstash=Mock(return_value=330001), randbelow=lambda _: 0)
        with self.assertRaisesRegex(RuntimeError, "invalid ID"):
            allocator({}, "一礼")

    def test_seed_uses_canonical_map_owner_and_source_library_without_cat_ids(self):
        remote = Mock(return_value=1)
        mapping = {"规范一礼": identity("规范一礼", 340001, 330001), "真实": identity("真实", 1234567890123)}
        store = {"records": [drama(340001, "别名"), drama(340002, "漠然烟若", "200")]}
        self.assertEqual(cvs.seed_generated_manbo_cvid_registry(mapping, store, upstash=remote), 2)
        commands = [call.args[0] for call in remote.call_args_list]
        self.assertEqual(commands[0][3:], [cvs.GENERATED_MANBO_CVID_REGISTRY_KEY, "name:规范一礼", "id:340001", "340001", "规范一礼"])
        self.assertEqual(commands[1][5], "id:340002")
        remote.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "registry conflict"):
            cvs.seed_generated_manbo_cvid_registry(mapping, store, upstash=remote)

    def test_name_and_alias_reuse_keep_cat_identity_untouched(self):
        mapping = {"规范一礼": identity("规范一礼", cat_id=777, aliases=["一礼"])}
        allocator = Mock(return_value=340123)
        self.assertEqual(cvs.ensure_generated_manbo_cv_entry(mapping, "一礼", allocator=allocator), (340123, True))
        self.assertEqual(cvs.ensure_generated_manbo_cv_entry(mapping, "规范一礼", allocator=allocator), (340123, False))
        allocator.assert_called_once_with(mapping, "规范一礼")
        self.assertEqual(mapping["规范一礼"]["missevanCvId"], 777)
        self.assertEqual(mapping["规范一礼"]["manboIdSource"], "generated")
        self.assertNotIn("missevan_generated", mapping["规范一礼"]["source"])

    def test_replacement_registry_conflict_fails_instead_of_overwriting(self):
        remote = Mock(return_value=0)
        with self.assertRaisesRegex(RuntimeError, "Conflicting"):
            cvs.persist_generated_manbo_cvid_replacements({340001: 1234567890123}, upstash=remote)
        self.assertEqual(remote.call_args.args[0][3], cvs.GENERATED_MANBO_CVID_REGISTRY_KEY)
        with self.assertRaisesRegex(RuntimeError, "Invalid"):
            cvs.load_generated_manbo_cvid_replacements(upstash=Mock(return_value=["upgrade:330001", "999"]))


class GeneratedManboFallbackTests(unittest.TestCase):
    def test_real_drama_gets_stable_manbo_virtual_ids_without_creating_cat_ids(self):
        mapping = {}
        allocate = Mock(side_effect=[340111, 340222])
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, PAYLOAD,
            combined_map=mapping, search_cv=Mock(return_value=None), generated_cv_id_allocator=allocate)
        repeated = metadata.build_manbo_record(record, PAYLOAD, combined_map=mapping,
            search_cv=Mock(return_value=None), generated_cv_id_allocator=allocate)
        self.assertEqual(repeated["mainCvIds"], [340111, 340222])
        self.assertEqual(allocate.call_count, 2)
        self.assertEqual(repeated["mainCvMissevanIds"], [None, None])
        self.assertEqual(set(mapping), {"一礼", "漠然烟若"})

    def test_cat_match_still_receives_missing_manbo_id_and_existing_virtual_is_searched_for_upgrade(self):
        mapping = {"一礼": identity("一礼", cat_id=777), "漠然烟若": identity("漠然烟若", 340222)}
        search = Mock(side_effect=[None, {"manboCvId": 1234567890123}])
        allocate = Mock(return_value=340111)
        record = metadata.build_manbo_record({"dramaId": DRAMA_ID}, PAYLOAD,
            combined_map=mapping, search_cv=search, generated_cv_id_allocator=allocate)
        self.assertEqual(record["mainCvIds"], [340111, 1234567890123])
        self.assertEqual(record["mainCvMissevanIds"], [777, None])
        allocate.assert_called_once()

    def test_virtual_ids_skip_all_avatar_requests(self):
        request = Mock()
        self.assertEqual(cvs.fetch_manbo_cv_avatar(340001, manbo_request=request), "")
        request.assert_not_called()
        self.assertEqual(cvs.payload_avatar(identity("一礼", 340001), avatar_lookup=request, force=True), "")
        request.assert_not_called()
        lookup = cvs.CvAvatarLookup(manbo_request=request)
        self.assertEqual(lookup("漫播", 340001), "")
        request.assert_not_called()


class GeneratedManboUpgradeTests(unittest.TestCase):
    def update(self, mapping, store, **kwargs):
        with patch.object(cvs, "load_combined_map", return_value=deepcopy(mapping)), patch.object(cvs, "save_combined_map") as save:
            result = cvs.update_combined_cvid_map({}, store, **kwargs)
        return result, save.call_args.args[0]

    def test_conflicting_real_candidates_cancel_upgrade_regardless_of_order(self):
        original = {"一礼": identity("一礼", 340001)}
        for ids in [(1111111111111, 2222222222222, 1111111111111),
                    (2222222222222, 1111111111111, 2222222222222)]:
            with self.subTest(ids=ids):
                rows = [drama(cv_id, drama_id=str(index + 200)) for index, cv_id in enumerate(ids)]
                result, saved = self.update(original, {"records": rows})
                self.assertEqual(result["manbo_generated_replacements"], {})
                self.assertEqual(saved, original)
                self.assertTrue(any("conflicting-upgrade" in issue for issue in result["ambiguous_samples"]))

    def test_conflicting_observation_cannot_apply_a_persisted_upgrade(self):
        original = {"一礼": identity("一礼", 340001)}
        result, saved = self.update(original, {"records": [drama(2222222222222)]},
            persistent_manbo_generated_replacements={340001: 1111111111111})
        self.assertEqual(result["manbo_generated_replacements"], {})
        self.assertEqual(saved, original)

    def test_upgrade_and_retry_migrate_all_library_references_and_keep_alignment(self):
        mapping = {"一礼": identity("一礼", 340001, 777, manboIdSource="generated")}
        result, saved = self.update(mapping, {"records": [drama(1234567890123)]})
        replacements = result["manbo_generated_replacements"]
        self.assertEqual(replacements, {340001: 1234567890123})
        self.assertEqual(saved["一礼"]["manboIdSource"], "platform")
        self.assertEqual(saved["一礼"]["missevanCvId"], 777)
        retried, _saved = self.update(saved, {"records": []}, persistent_manbo_generated_replacements=replacements)
        self.assertEqual(retried["manbo_generated_replacements"], replacements)
        store = {"records": [drama(340001), drama(340001, drama_id="200")]}
        store["records"][0]["mainCvIds"].append(None)
        store["records"][0]["mainCvNames"].append("漠然烟若")
        self.assertEqual(platform_sync.replace_manbo_main_cv_ids(store, replacements), {DRAMA_ID, "200"})
        self.assertEqual(store["records"][0]["mainCvIds"], [1234567890123, None])
        self.assertEqual(store["records"][0]["mainCvRoleNames"], ["楚淮予", ""])
        self.assertEqual(platform_sync.replace_manbo_main_cv_ids(store, replacements), set())

    def test_upgrade_merges_existing_real_owner_without_losing_aliases_or_manual_notes(self):
        mapping = {"一礼": identity("一礼", 340001, 777, notes="旧备注"),
                   "规范一礼": identity("规范一礼", 1234567890123, 777, notes="人工确认", avatar="avatar")}
        result, saved = self.update(mapping, {"records": [drama(1234567890123)]})
        self.assertEqual(set(saved), {"规范一礼"})
        self.assertIn("一礼", saved["规范一礼"]["aliases"])
        self.assertIn("人工确认", saved["规范一礼"]["notes"])
        self.assertIn("旧备注", saved["规范一礼"]["notes"])
        self.assertEqual(saved["规范一礼"]["avatar"], "avatar")
        self.assertEqual(result["ambiguous_count"], 0)

    def test_conflicting_cat_identity_prevents_upgrade_merge(self):
        mapping = {"一礼": identity("一礼", 340001, 777),
                   "规范一礼": identity("规范一礼", 1234567890123, 888)}
        result, saved = self.update(mapping, {"records": [drama(1234567890123)]})
        self.assertEqual(saved, mapping)
        self.assertEqual(result["manbo_generated_replacements"], {})
        self.assertGreater(result["ambiguous_count"], 0)

    def test_stale_generated_observation_never_rolls_real_map_back(self):
        result, saved = self.update({"一礼": identity("一礼", 1234567890123)}, {"records": [drama(340001)]})
        self.assertEqual(saved["一礼"]["manboCvId"], 1234567890123)
        self.assertEqual(result["manbo_generated_replacements"], {340001: 1234567890123})

    def test_editor_cannot_bypass_registry_by_changing_manbo_id_or_owner(self):
        original = {f"CV {i}": identity(f"CV {i}", 340001 if i == 1 else None, i) for i in range(1, 52)}
        for field, value in [("displayName", "新名字"), ("manboCvId", 1234567890123)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                remote = FakeUpstash()
                remote.strings[upstash_editor.CVID_MAP_KEY] = json.dumps(original)
                loaded = upstash_editor.load_resource(upstash_editor.CVID_MAP_KEY, upstash=remote, backup_root=Path(tmp))
                candidate = deepcopy(original)
                candidate["CV 1"][field] = value
                with self.assertRaisesRegex(ValueError, "Generated Manbo.*registry-managed"):
                    upstash_editor.save_resource(loaded, candidate, upstash=remote)

    def test_append_retries_persisted_migration_before_publishing_info(self):
        store = {"records": [drama(340001, drama_id="201")]}
        mapping = {"一礼": identity("一礼", 1234567890123)}
        with (
            patch.object(append_manbo_ids, "load_env_file"),
            patch.object(append_manbo_ids, "download_info_file"),
            patch.object(append_manbo_ids, "download_support_files"),
            patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
            patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements", return_value={340001: 1234567890123}),
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={"processed": 1}),
            patch.object(append_manbo_ids, "load_json", side_effect=lambda path, default: store if path == append_manbo_ids.MANBO_INFO_PATH else mapping),
            patch.object(append_manbo_ids, "update_combined_cvid_map", return_value={
                "updated": 0, "created": 0, "unchanged": 0, "ambiguous_count": 0, "ambiguous_samples": [],
                "manbo_generated_replacements": {340001: 1234567890123},
            }),
            patch.object(append_manbo_ids, "save_json") as save,
            patch.object(append_manbo_ids, "merge_and_upload_info_file") as publish,
            patch("builtins.print"),
        ):
            self.assertEqual(append_manbo_ids.main(["append_manbo_ids.py", "200"]), 0)
        save.assert_called_once()
        publish.assert_called_once_with(append_manbo_ids.MANBO_INFO_KEY, append_manbo_ids.MANBO_INFO_PATH, ["200", "201"], removed_drama_ids=[])
        self.assertEqual(store["records"][0]["mainCvIds"], [1234567890123])

    def test_append_rejected_upgrade_stops_migration_and_info_publication(self):
        candidate = {340001: 1234567890123}
        mapping = {"一礼": identity("一礼", 340001, 777),
                   "另一位演员": identity("另一位演员", 1234567890123, 888)}
        for source in ["local", "registry"]:
            with self.subTest(source=source):
                store = {"records": [drama(340001, drama_id="201")]}
                original = deepcopy(store)
                with (
                    patch.object(append_manbo_ids, "load_env_file"),
                    patch.object(append_manbo_ids, "download_info_file"),
                    patch.object(append_manbo_ids, "download_support_files"),
                    patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
                    patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements", return_value=candidate if source == "registry" else {}),
                    patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={
                        "processed": 1, "manbo_generated_replacements": candidate if source == "local" else {},
                    }),
                    patch.object(append_manbo_ids, "load_json", side_effect=lambda path, default:
                                 store if path == append_manbo_ids.MANBO_INFO_PATH else mapping),
                    patch.object(append_manbo_ids, "update_combined_cvid_map", return_value={
                        "updated": 0, "created": 0, "unchanged": 0, "ambiguous_count": 1,
                        "ambiguous_samples": ["漫播:一礼:missevan-conflict=另一位演员"],
                        "manbo_generated_replacements": {},
                    }),
                    patch.object(append_manbo_ids, "replace_manbo_main_cv_ids") as migrate,
                    patch.object(append_manbo_ids, "save_json") as save,
                    patch.object(append_manbo_ids, "merge_and_upload_info_file") as publish,
                    self.assertRaisesRegex(RuntimeError, "unconfirmed.*340001"),
                ):
                    append_manbo_ids.main(["append_manbo_ids.py", "200"])
                migrate.assert_not_called()
                save.assert_not_called()
                publish.assert_not_called()
                self.assertEqual(store, original)

    def test_append_ignores_completed_history_when_its_real_target_was_deleted(self):
        mapping = {"当前主役": identity("当前主役", 9999999999999)}
        store = {"records": [drama(9999999999999, name="当前主役", drama_id="200")]}
        original = deepcopy(store)
        remote_calls = []

        def update_remote(*args, **kwargs):
            remote_calls.append(kwargs["persistent_manbo_generated_replacements"])
            with patch.object(cvs, "load_combined_map", return_value=deepcopy(mapping)), patch.object(cvs, "save_combined_map"):
                return cvs.update_combined_cvid_map({}, store,
                    persistent_manbo_generated_replacements=kwargs["persistent_manbo_generated_replacements"])

        with (
            patch.object(append_manbo_ids, "load_env_file"),
            patch.object(append_manbo_ids, "download_info_file"),
            patch.object(append_manbo_ids, "download_support_files"),
            patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
            patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements", return_value={340001: 1234567890123}),
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={"processed": 1}),
            patch.object(append_manbo_ids, "load_json", side_effect=lambda path, default:
                         store if path == append_manbo_ids.MANBO_INFO_PATH else mapping),
            patch.object(append_manbo_ids, "update_combined_cvid_map", side_effect=update_remote),
            patch.object(append_manbo_ids, "save_json") as save,
            patch.object(append_manbo_ids, "merge_and_upload_info_file") as publish,
            patch("builtins.print"),
        ):
            self.assertEqual(append_manbo_ids.main(["append_manbo_ids.py", "200"]), 0)
        self.assertEqual(remote_calls, [{}])
        self.assertEqual(store, original)
        save.assert_not_called()
        publish.assert_called_once_with(append_manbo_ids.MANBO_INFO_KEY, append_manbo_ids.MANBO_INFO_PATH, ["200"], removed_drama_ids=[])

    def test_append_keeps_unconfirmed_current_upgrades_blocked(self):
        old_id, real_id = 340001, 1234567890123
        mapping = {"一礼": identity("一礼", real_id)}
        # Refresh can remove or introduce an old reference; a new proposal also needs
        # confirmation even when no old reference remains in the refreshed library.
        for source in ["before", "after", "local"]:
            with self.subTest(source=source):
                store = {"records": [drama(old_id if source == "before" else real_id, drama_id="200")]}

                def refresh(*args, **kwargs):
                    store["records"][0]["mainCvIds"] = [old_id if source == "after" else real_id]
                    return {"processed": 1, "manbo_generated_replacements": {old_id: real_id} if source == "local" else {}}

                with (
                    patch.object(append_manbo_ids, "load_env_file"),
                    patch.object(append_manbo_ids, "download_info_file"),
                    patch.object(append_manbo_ids, "download_support_files"),
                    patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
                    patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements",
                                 return_value={} if source == "local" else {old_id: real_id}),
                    patch.object(append_manbo_ids, "upsert_manbo_drama_ids", side_effect=refresh),
                    patch.object(append_manbo_ids, "load_json", side_effect=lambda path, default:
                                 store if path == append_manbo_ids.MANBO_INFO_PATH else mapping),
                    patch.object(append_manbo_ids, "update_combined_cvid_map", return_value={
                        "manbo_generated_replacements": {}, "ambiguous_count": 1,
                    }) as update,
                    patch.object(append_manbo_ids, "replace_manbo_main_cv_ids") as migrate,
                    patch.object(append_manbo_ids, "save_json") as save,
                    patch.object(append_manbo_ids, "merge_and_upload_info_file") as publish,
                    self.assertRaisesRegex(RuntimeError, "unconfirmed.*340001"),
                ):
                    append_manbo_ids.main(["append_manbo_ids.py", "200"])
                self.assertEqual(update.call_args.kwargs["persistent_manbo_generated_replacements"], {old_id: real_id})
                migrate.assert_not_called()
                save.assert_not_called()
                publish.assert_not_called()

    def test_append_filters_retired_history_but_migrates_referenced_history(self):
        store = {"records": [drama(9999999999999, name="当前主役", drama_id="200"),
                             drama("340002", name="仍需迁移", drama_id="201")]}
        mapping = {"当前主役": identity("当前主役", 9999999999999),
                   "仍需迁移": identity("仍需迁移", 2222222222222)}
        active = {340002: 2222222222222}
        with (
            patch.object(append_manbo_ids, "load_env_file"),
            patch.object(append_manbo_ids, "download_info_file"),
            patch.object(append_manbo_ids, "download_support_files"),
            patch.object(append_manbo_ids, "seed_generated_manbo_cvid_registry"),
            patch.object(append_manbo_ids, "load_generated_manbo_cvid_replacements",
                         return_value={340001: 1234567890123, **active}),
            patch.object(append_manbo_ids, "upsert_manbo_drama_ids", return_value={"processed": 1}),
            patch.object(append_manbo_ids, "load_json", side_effect=lambda path, default:
                         store if path == append_manbo_ids.MANBO_INFO_PATH else mapping),
            patch.object(append_manbo_ids, "update_combined_cvid_map", return_value={
                "manbo_generated_replacements": active, "updated": 0, "created": 0,
                "unchanged": 1, "ambiguous_count": 0, "ambiguous_samples": [],
            }) as update,
            patch.object(append_manbo_ids, "save_json") as save,
            patch.object(append_manbo_ids, "merge_and_upload_info_file") as publish,
            patch("builtins.print"),
        ):
            self.assertEqual(append_manbo_ids.main(["append_manbo_ids.py", "200"]), 0)
        self.assertEqual(update.call_args.kwargs["persistent_manbo_generated_replacements"], active)
        self.assertEqual(store["records"][1]["mainCvIds"], [2222222222222])
        save.assert_called_once()
        publish.assert_called_once_with(append_manbo_ids.MANBO_INFO_KEY, append_manbo_ids.MANBO_INFO_PATH, ["200", "201"], removed_drama_ids=[])


if __name__ == "__main__":
    unittest.main()
