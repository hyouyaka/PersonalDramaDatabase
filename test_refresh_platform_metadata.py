import unittest
from unittest.mock import Mock, patch

import refresh_platform_metadata
import platform_sync


class FirstEpisodeMonthTests(unittest.TestCase):
    def test_explicit_first_episode_beats_earlier_unnumbered_candidate(self) -> None:
        entries = [
            {"setTitle": "独立标题正片", "setNo": 1, "createTime": 1767225600000},
            {"setTitle": "EP01", "setNo": 2, "createTime": 1769904000000},
        ]

        result = platform_sync.pick_first_episode_month(
            entries,
            title_key="setTitle",
            time_key="createTime",
            milliseconds=True,
        )

        self.assertEqual(result, "2026.02")

    def test_unnumbered_first_content_is_inferred_after_promos(self) -> None:
        entries = [
            {"setTitle": "系列预热片", "setNo": 1, "createTime": 1696118400000},
            {"setTitle": "先导预告", "setNo": 2, "createTime": 1696204800000},
            {"setTitle": "《晓歌天下》之《空空》", "setNo": 3, "createTime": 1698796800000},
        ]

        result = platform_sync.pick_first_episode_month(
            entries,
            title_key="setTitle",
            time_key="createTime",
            milliseconds=True,
        )

        self.assertEqual(result, "2023.11")

    def test_only_non_main_material_remains_empty(self) -> None:
        entries = [
            {"name": "PV", "order": 1, "create_time": 1767225600},
            {"name": "主题曲", "order": 2, "create_time": 1767312000},
            {"name": "生日福利", "order": 3, "create_time": 1767398400},
        ]

        result = platform_sync.pick_first_episode_month(
            entries,
            title_key="name",
            time_key="create_time",
            milliseconds=False,
        )

        self.assertEqual(result, "")

    def test_numbered_extras_do_not_match_first_episode(self) -> None:
        for title in ("幕后企划①", "倒计时1天", "花絮01", "小剧场1", "雪饼日记1.0", "郑郑视角01"):
            with self.subTest(title=title):
                self.assertFalse(platform_sync.match_first_episode(title))

    def test_supported_first_episode_variants(self) -> None:
        for title in ("第一集", "第1期", "EP01", "E01", "Episode 1", "S02E01", "01 癖好", "上集"):
            with self.subTest(title=title):
                self.assertTrue(platform_sync.match_first_episode(title))


class ManboCvFallbackTests(unittest.TestCase):
    def test_upsert_manbo_rejects_non_numeric_id_before_loading_store(self) -> None:
        with patch.object(refresh_platform_metadata, "load_json") as load_store:
            with self.assertRaisesRegex(ValueError, "ASCII digits required"):
                refresh_platform_metadata.upsert_manbo_drama_ids(["drama-1"])

        load_store.assert_not_called()

    def test_build_manbo_record_falls_back_to_nickname_when_map_missing(self) -> None:
        payload = {
                "data": {
                    "title": "测试剧",
                    "coverPic": "https://cover.test/manbo.jpg",
                    "category": 1,
                    "categoryLabels": [{"name": "纯爱"}],
                "cvRespList": [
                    {
                        "dramaRoleType": 2,
                        "platUid": 1001,
                        "cvResp": {"id": 1001, "nickname": "映射昵称"},
                        "cvNickname": "接口昵称甲",
                        "role": "饰:甲",
                    },
                    {
                        "dramaRoleType": 2,
                        "platUid": 1002,
                        "cvResp": {"id": 1002, "nickname": "接口昵称乙"},
                        "cvNickname": "接口昵称乙",
                        "role": "饰:乙",
                    },
                ],
                "setRespList": [],
            }
        }

        record = refresh_platform_metadata.build_manbo_record(
            {"dramaId": "drama-1"},
            payload,
            {1001: "规范名甲"},
        )

        self.assertEqual(record["mainCvNames"], ["规范名甲", "接口昵称乙"])
        self.assertEqual(record["cover"], "https://cover.test/manbo.jpg")

    def test_build_manbo_record_preserves_existing_cover_when_api_cover_is_empty(self) -> None:
        record = refresh_platform_metadata.build_manbo_record(
            {"dramaId": "2235647356781461610", "cover": "https://cover.test/existing.jpg"},
            {"data": {"title": "测试剧", "cover": "", "category": 1, "categoryLabels": [], "setRespList": []}},
        )

        self.assertEqual(record["cover"], "https://cover.test/existing.jpg")

    def test_build_manbo_record_uses_first_available_manbo_cover_field(self) -> None:
        record = refresh_platform_metadata.build_manbo_record(
            {"dramaId": "2235627191910006844", "cover": ""},
            {
                "data": {
                    "title": "学不乖",
                    "coverPic": "",
                    "largePic": "https://cover.test/large.jpg",
                    "cover": "https://cover.test/cover.jpg",
                    "category": 1,
                    "categoryLabels": [],
                    "setRespList": [],
                }
            },
        )

        self.assertEqual(record["cover"], "https://cover.test/large.jpg")


class MissevanCoverTests(unittest.TestCase):
    def test_build_missevan_base_node_keeps_cover(self) -> None:
        node, _entries = refresh_platform_metadata.build_missevan_base_node(
            {
                "drama": {
                    "id": 93605,
                    "name": "猫耳测试剧",
                    "cover": "https://cover.test/missevan.jpg",
                    "catalog": 89,
                    "pay_type": 1,
                    "price": 1,
                },
                "cvs": [],
                "episodes": {"episode": []},
            },
            4,
        )

        self.assertEqual(node["cover"], "https://cover.test/missevan.jpg")


class MissevanCvMapEntryTests(unittest.TestCase):
    def test_upsert_missevan_cv_map_entry_initializes_avatar(self) -> None:
        combined_map = {}

        refresh_platform_metadata.upsert_missevan_cv_map_entry(combined_map, "CV A", 11)

        self.assertIn("avatar", combined_map["CV A"])
        self.assertEqual(combined_map["CV A"]["avatar"], "")

    def test_intro_search_miss_creates_name_only_cv_entry(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map") as save_map:
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"maincvs": []},
                "94602",
                [{"role_name": "季南溪", "display_name": "林风"}],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["fallbackCvNames"], ["林风"])
        self.assertEqual(updated["fallbackCvRoles"], {"林风": "季南溪"})
        self.assertIsNone(combined_map["林风"]["missevanCvId"])
        self.assertEqual(combined_map["林风"]["notes"], "猫耳搜索未命中，以显示名称识别")
        save_map.assert_called_once_with(combined_map)

    def test_intro_search_miss_uses_generated_numeric_id_when_allocator_is_available(self) -> None:
        combined_map = {}
        allocator = Mock(return_value=331234)
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"type": 4, "maincvs": []},
                "94602",
                [{"role_name": "季南溪", "display_name": "林风"}],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
                generated_cv_id_allocator=allocator,
            )

        self.assertEqual(updated["maincvs"], [331234])
        self.assertEqual(updated["cvnames"], {"331234": "林风"})
        self.assertEqual(updated["cvroles"], {"331234": "季南溪"})
        self.assertNotIn("fallbackCvNames", updated)
        self.assertEqual(combined_map["林风"]["missevanCvId"], 331234)

    def test_generated_id_upgrades_when_search_finds_real_id(self) -> None:
        combined_map = {
            "林风": {
                "cvId": 331234,
                "missevanCvId": 331234,
                "displayName": "林风",
                "aliases": [],
            }
        }
        replacements: dict[int, int] = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [331234],
                    "cvnames": {"331234": "林风"},
                    "cvroles": {"331234": "季南溪"},
                },
                "94602",
                [{"role_name": "季南溪", "display_name": "林风"}],
                combined_map=combined_map,
                search_cv=Mock(return_value={"cv_id": 1234, "display_name": "林风"}),
                generated_cv_id_allocator=Mock(),
                generated_id_replacements=replacements,
            )

        self.assertEqual(updated["maincvs"], [1234])
        self.assertEqual(combined_map["林风"]["missevanCvId"], 1234)
        self.assertEqual(replacements, {331234: 1234})

    def test_generated_id_upgrade_updates_original_key_when_search_name_changes(self) -> None:
        combined_map = {
            "旧账号名": {
                "cvId": 331234,
                "missevanCvId": 331234,
                "displayName": "旧账号名",
                "aliases": [],
            }
        }
        replacements: dict[int, int] = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [331234],
                    "cvnames": {"331234": "旧账号名"},
                    "cvroles": {"331234": "角色"},
                },
                "drama-1",
                [{"role_name": "角色", "display_name": "旧账号名", "account_name": "正式名"}],
                combined_map=combined_map,
                search_cv=Mock(
                    side_effect=lambda name: (
                        {"cv_id": 1234, "display_name": "正式名"} if name == "正式名" else None
                    )
                ),
                generated_id_replacements=replacements,
            )

        self.assertEqual(set(combined_map), {"旧账号名"})
        self.assertEqual(combined_map["旧账号名"]["missevanCvId"], 1234)
        self.assertEqual(combined_map["旧账号名"]["displayName"], "正式名")
        self.assertIn("旧账号名", combined_map["旧账号名"]["aliases"])
        self.assertEqual(updated["maincvs"], [1234])
        self.assertEqual(replacements, {331234: 1234})

    def test_generated_id_upgrade_merges_into_existing_real_id_record(self) -> None:
        combined_map = {
            "旧账号名": {
                "cvId": 331234,
                "missevanCvId": 331234,
                "displayName": "旧账号名",
                "aliases": ["旧别名"],
                "avatar": "old-avatar",
            },
            "正式名": {
                "cvId": 1234,
                "missevanCvId": 1234,
                "displayName": "正式名",
                "aliases": [],
                "avatar": "",
                "notes": "人工确认记录",
            },
        }
        replacements: dict[int, int] = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [331234],
                    "cvnames": {"331234": "旧账号名"},
                    "cvroles": {"331234": "角色"},
                },
                "drama-1",
                [{"role_name": "角色", "display_name": "旧账号名", "account_name": "正式名"}],
                combined_map=combined_map,
                search_cv=Mock(
                    side_effect=lambda name: (
                        {"cv_id": 1234, "display_name": "正式名"} if name == "正式名" else None
                    )
                ),
                generated_id_replacements=replacements,
            )

        self.assertEqual(set(combined_map), {"正式名"})
        self.assertEqual(combined_map["正式名"]["missevanCvId"], 1234)
        self.assertEqual(combined_map["正式名"]["avatar"], "old-avatar")
        self.assertIn("旧账号名", combined_map["正式名"]["aliases"])
        self.assertIn("旧别名", combined_map["正式名"]["aliases"])
        self.assertIn("人工确认记录", combined_map["正式名"]["notes"])
        self.assertIn("自动生成 CVID 331234", combined_map["正式名"]["notes"])
        self.assertEqual(updated["maincvs"], [1234])
        self.assertEqual(replacements, {331234: 1234})

    def test_account_name_merges_existing_name_only_record_into_real_record(self) -> None:
        combined_map = {
            "署名": {
                "cvId": None,
                "missevanCvId": None,
                "displayName": "署名",
                "aliases": [],
            },
            "账号": {
                "cvId": 123,
                "missevanCvId": 123,
                "displayName": "账号",
                "aliases": [],
                "notes": "人工确认记录",
            },
        }

        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
                "drama-1",
                [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
                combined_map=combined_map,
                search_cv=None,
            )

        self.assertEqual(set(combined_map), {"账号"})
        self.assertIn("署名", combined_map["账号"]["aliases"])
        self.assertEqual(combined_map["账号"]["notes"], "人工确认记录")
        self.assertEqual(updated["maincvs"], [123])

    def test_account_name_preserves_conflicting_manbo_mappings(self) -> None:
        combined_map = {
            "署名": {
                "cvId": None,
                "missevanCvId": None,
                "manboCvId": 111,
                "displayName": "署名",
                "aliases": [],
            },
            "账号": {
                "cvId": 123,
                "missevanCvId": 123,
                "manboCvId": 222,
                "displayName": "账号",
                "aliases": [],
            },
        }
        ambiguities: list[str] = []

        with patch.object(refresh_platform_metadata, "save_combined_map") as save_map:
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
                "drama-1",
                [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
                combined_map=combined_map,
                search_cv=None,
                cv_upgrade_ambiguities=ambiguities,
            )

        self.assertEqual(set(combined_map), {"署名", "账号"})
        self.assertEqual(combined_map["署名"]["manboCvId"], 111)
        self.assertEqual(combined_map["账号"]["manboCvId"], 222)
        self.assertEqual(updated["fallbackCvNames"], ["署名"])
        self.assertEqual(len(ambiguities), 1)
        save_map.assert_not_called()

    def test_account_name_conflict_is_preserved_without_map_updates(self) -> None:
        combined_map = {
            "署名": {
                "cvId": None,
                "missevanCvId": None,
                "manboCvId": 111,
                "displayName": "署名",
                "aliases": [],
            },
            "账号": {
                "cvId": 123,
                "missevanCvId": 123,
                "manboCvId": 222,
                "displayName": "账号",
                "aliases": [],
            },
        }
        ambiguities: list[str] = []

        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
            "drama-1",
            [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
            combined_map=combined_map,
            search_cv=None,
            update_combined_map=False,
            cv_upgrade_ambiguities=ambiguities,
        )

        self.assertEqual(set(combined_map), {"署名", "账号"})
        self.assertEqual(updated["fallbackCvNames"], ["署名"])
        self.assertEqual(len(ambiguities), 1)

    def test_account_resolution_removes_existing_fallback_alias(self) -> None:
        combined_map = {
            "署名": {
                "cvId": None,
                "missevanCvId": None,
                "displayName": "署名",
                "aliases": [],
            },
            "账号": {
                "cvId": 123,
                "missevanCvId": 123,
                "displayName": "账号",
                "aliases": [],
            },
        }
        node = {
            "type": 4,
            "maincvs": [],
            "cvnames": {},
            "cvroles": {},
            "fallbackCvNames": ["署名"],
            "fallbackCvRoles": {"署名": "角色"},
        }

        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                node,
                "drama-1",
                [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
                combined_map=combined_map,
                search_cv=None,
                existing_entries_first=True,
            )

        self.assertEqual(updated["maincvs"], [123])
        self.assertNotIn("fallbackCvNames", updated)
        self.assertNotIn("fallbackCvRoles", updated)
        self.assertEqual(len(refresh_platform_metadata.missevan_main_cv_entries(updated)), 1)

    def test_account_name_merges_generated_display_record_into_real_record(self) -> None:
        combined_map = {
            "心念": {
                "cvId": 337329,
                "missevanCvId": 337329,
                "manboCvId": None,
                "displayName": "心念",
                "aliases": [],
                "avatar": "",
            },
            "心念多个心": {
                "cvId": 3853,
                "missevanCvId": 3853,
                "manboCvId": 2948274737182,
                "displayName": "心念多个心",
                "aliases": [],
                "avatar": "real-avatar",
            },
        }
        replacements: dict[int, int] = {}
        search = Mock()

        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [337329, 3363],
                    "cvnames": {"337329": "心念", "3363": "刘一鸣"},
                    "cvroles": {"337329": "江也", "3363": "陆远之"},
                },
                "94915",
                [{"role_name": "江也", "display_name": "心念", "account_name": "心念多个心"}],
                combined_map=combined_map,
                search_cv=search,
                generated_id_replacements=replacements,
                existing_entries_first=True,
            )

        self.assertEqual(set(combined_map), {"心念多个心"})
        self.assertEqual(combined_map["心念多个心"]["missevanCvId"], 3853)
        self.assertEqual(combined_map["心念多个心"]["manboCvId"], 2948274737182)
        self.assertEqual(combined_map["心念多个心"]["avatar"], "real-avatar")
        self.assertIn("心念", combined_map["心念多个心"]["aliases"])
        self.assertEqual(updated["maincvs"], [3853, 3363])
        self.assertEqual(replacements, {337329: 3853})
        search.assert_not_called()

    def test_account_resolution_migrates_stale_generated_drama_after_map_was_merged(self) -> None:
        combined_map = {
            "心念多个心": {
                "cvId": 3853,
                "missevanCvId": 3853,
                "manboCvId": 2948274737182,
                "displayName": "心念多个心",
                "aliases": ["心念"],
                "avatar": "real-avatar",
            },
        }
        replacements: dict[int, int] = {}

        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [337329, 3363],
                    "cvnames": {"337329": "心念", "3363": "刘一鸣"},
                    "cvroles": {"337329": "江也", "3363": "陆远之"},
                },
                "94915",
                [{"role_name": "江也", "display_name": "心念", "account_name": "心念多个心"}],
                combined_map=combined_map,
                search_cv=Mock(),
                generated_id_replacements=replacements,
                existing_entries_first=True,
            )

        self.assertEqual(updated["maincvs"], [3853, 3363])
        self.assertEqual(replacements, {337329: 3853})

    def test_existing_generated_entry_does_not_suppress_ambiguous_display_map(self) -> None:
        combined_map = {
            "署名甲": {
                "cvId": 100,
                "missevanCvId": 100,
                "displayName": "署名甲",
                "aliases": ["署名"],
            },
            "署名乙": {
                "cvId": 200,
                "missevanCvId": 200,
                "displayName": "署名乙",
                "aliases": ["署名", "账号"],
            },
        }
        ambiguities: list[str] = []

        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {
                "type": 4,
                "maincvs": [337329],
                "cvnames": {"337329": "署名"},
                "cvroles": {"337329": "角色"},
            },
            "drama-1",
            [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
            combined_map=combined_map,
            search_cv=Mock(),
            update_combined_map=False,
            cv_upgrade_ambiguities=ambiguities,
            existing_entries_first=True,
        )

        self.assertEqual(updated["maincvs"], [337329])
        self.assertEqual(len(ambiguities), 1)
        self.assertIn("display name map has multiple targets", ambiguities[0])

    def test_existing_real_display_match_is_not_overridden_by_account(self) -> None:
        combined_map = {
            "刘一鸣": {
                "cvId": 3363,
                "missevanCvId": 3363,
                "displayName": "刘一鸣",
                "aliases": [],
            },
            "无关账号": {
                "cvId": 999,
                "missevanCvId": 999,
                "displayName": "无关账号",
                "aliases": [],
            },
        }
        search = Mock()

        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
            "drama-1",
            [{"role_name": "角色", "display_name": "刘一鸣", "account_name": "无关账号"}],
            combined_map=combined_map,
            search_cv=search,
            update_combined_map=False,
        )

        self.assertEqual(updated["maincvs"], [3363])
        search.assert_not_called()

    def test_conflicting_display_and_account_search_results_fail_closed(self) -> None:
        search = Mock(
            side_effect=lambda name: {
                "cv_id": 100 if name == "署名" else 200,
                "display_name": name,
            }
        )
        allocator = Mock()
        ambiguities: list[str] = []

        with patch.object(refresh_platform_metadata, "save_combined_map") as save_map:
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
                "drama-1",
                [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
                combined_map={},
                search_cv=search,
                generated_cv_id_allocator=allocator,
                cv_upgrade_ambiguities=ambiguities,
            )

        self.assertEqual(updated["fallbackCvNames"], ["署名"])
        self.assertEqual(len(ambiguities), 1)
        allocator.assert_not_called()
        save_map.assert_not_called()

    def test_ambiguous_display_map_is_not_bypassed_by_account_match(self) -> None:
        combined_map = {
            "署名甲": {
                "cvId": 100,
                "missevanCvId": 100,
                "displayName": "署名甲",
                "aliases": ["署名"],
            },
            "署名乙": {
                "cvId": 200,
                "missevanCvId": 200,
                "displayName": "署名乙",
                "aliases": ["署名", "账号"],
            },
        }
        ambiguities: list[str] = []

        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
            "drama-1",
            [{"role_name": "角色", "display_name": "署名", "account_name": "账号"}],
            combined_map=combined_map,
            search_cv=Mock(),
            update_combined_map=False,
            cv_upgrade_ambiguities=ambiguities,
        )

        self.assertEqual(updated["fallbackCvNames"], ["署名"])
        self.assertEqual(len(ambiguities), 1)
        self.assertIn("display name map has multiple targets", ambiguities[0])

    def test_non_exact_injected_search_result_is_rejected(self) -> None:
        allocator = Mock()

        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {"type": 4, "maincvs": [], "cvnames": {}, "cvroles": {}},
            "drama-1",
            [{"role_name": "角色", "display_name": "心念"}],
            combined_map={},
            search_cv=Mock(return_value={"cv_id": 3853, "display_name": "心念多个心"}),
            update_combined_map=False,
            generated_cv_id_allocator=allocator,
        )

        self.assertEqual(updated["fallbackCvNames"], ["心念"])
        allocator.assert_not_called()

    def test_generated_id_upgrade_with_multiple_real_targets_is_reported_and_preserved(self) -> None:
        combined_map = {
            "旧账号名": {"cvId": 331234, "missevanCvId": 331234, "displayName": "旧账号名", "aliases": []},
            "正式名甲": {"cvId": 1234, "missevanCvId": 1234, "displayName": "正式名甲", "aliases": []},
            "正式名乙": {"cvId": 1234, "missevanCvId": 1234, "displayName": "正式名乙", "aliases": []},
        }
        original_map = {key: dict(value) for key, value in combined_map.items()}
        replacements: dict[int, int] = {}
        ambiguities: list[str] = []
        with patch.object(refresh_platform_metadata, "save_combined_map") as save_map:
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [331234],
                    "cvnames": {"331234": "旧账号名"},
                    "cvroles": {"331234": "角色"},
                },
                "drama-1",
                [{"role_name": "角色", "display_name": "旧账号名", "account_name": "正式名"}],
                combined_map=combined_map,
                search_cv=Mock(
                    side_effect=lambda name: (
                        {"cv_id": 1234, "display_name": "正式名"} if name == "正式名" else None
                    )
                ),
                generated_id_replacements=replacements,
                cv_upgrade_ambiguities=ambiguities,
            )

        self.assertEqual(combined_map, original_map)
        self.assertEqual(updated["maincvs"], [331234])
        self.assertEqual(replacements, {})
        self.assertEqual(len(ambiguities), 1)
        self.assertIn("targets=正式名乙,正式名甲", ambiguities[0])
        save_map.assert_not_called()

    def test_existing_generated_id_is_reused_when_search_still_misses(self) -> None:
        combined_map = {
            "林风": {
                "cvId": 331234,
                "missevanCvId": 331234,
                "displayName": "林风",
                "aliases": [],
            }
        }
        allocator = Mock()
        updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
            {"type": 4, "maincvs": []},
            "94602",
            [{"role_name": "季南溪", "display_name": "林风"}],
            combined_map=combined_map,
            search_cv=Mock(return_value=None),
            update_combined_map=False,
            generated_cv_id_allocator=allocator,
        )

        self.assertEqual(updated["maincvs"], [331234])
        allocator.assert_not_called()

    def test_intro_fallback_merges_numeric_and_name_only_main_cv(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [11],
                    "cvnames": {"11": "已知CV"},
                    "cvroles": {"11": "角色甲"},
                },
                "drama-1",
                [
                    {"role_name": "角色甲", "display_name": "已知CV"},
                    {"role_name": "角色乙", "display_name": "未知CV"},
                ],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["maincvs"], [11])
        self.assertEqual(updated["cvroles"], {"11": "角色甲"})
        self.assertEqual(updated["fallbackCvNames"], ["未知CV"])
        self.assertEqual(updated["fallbackCvRoles"], {"未知CV": "角色乙"})
        self.assertNotIn("已知CV", combined_map)
        self.assertIn("未知CV", combined_map)

    def test_intro_fallback_keeps_numeric_cv_when_multiple_names_are_unresolved(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {
                    "type": 4,
                    "maincvs": [11],
                    "cvnames": {"11": "已知CV"},
                    "cvroles": {"11": "角色甲"},
                },
                "drama-1",
                [
                    {"role_name": "角色乙", "display_name": "未知CV乙"},
                    {"role_name": "角色丙", "display_name": "未知CV丙"},
                ],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
            )

        self.assertEqual(updated["maincvs"], [11])
        self.assertEqual(updated["fallbackCvNames"], ["未知CV乙"])
        self.assertNotIn("未知CV丙", updated["fallbackCvRoles"])


class MissevanIntroCvCandidateTests(unittest.TestCase):
    def test_extracts_social_account_and_truncates_following_text(self) -> None:
        self.assertEqual(
            refresh_platform_metadata.extract_missevan_intro_cv_identity("心念@心念多个心 其他内容"),
            ("心念", "心念多个心"),
        )

    def test_search_requires_one_exact_normalized_name(self) -> None:
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "Datas": [
                    {"id": 3853, "name": "心念多个心"},
                    {"id": 1417, "name": "彭尧"},
                    {"id": 2975, "name": "家康"},
                ]
            }
        }

        self.assertIsNone(refresh_platform_metadata.search_missevan_cv("心念", requester))

    def test_search_accepts_exact_name_among_other_results(self) -> None:
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "Datas": [
                    {"id": 3853, "name": "心念多个心"},
                    {"id": 1417, "name": "彭尧"},
                ]
            }
        }

        self.assertEqual(
            refresh_platform_metadata.search_missevan_cv("心念多个心", requester),
            {"cv_id": 3853, "display_name": "心念多个心"},
        )

    def test_filters_all_production_role_keywords_and_compounds(self) -> None:
        roles = [
            "配音团队", "配音导演", "声音导演", "导演", "录音", "录音棚", "录音师", "录制",
            "监制", "剧本", "后期", "策划", "企划", "编剧", "制作人", "统筹", "对轨", "画本",
            "配乐", "原创配乐", "作词", "作曲", "演唱", "编曲", "和声", "混音", "母带",
            "rap", "RAP", "Rap", "海报设计", "视觉设计", "美工", "题字", "商务", "商务宣传",
            "字幕", "宣传", "宣发", "出品", "发行", "后期监制", "剧本监修", "原创配乐/混音",
        ]
        intro = "<p>配音组</p>" + "".join(f"<p>{role}：制作人员</p>" for role in roles) + "<p>角色甲：导演小王</p>"

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro, limit=100)

        self.assertEqual(candidates, [{"role_name": "角色甲", "display_name": "导演小王"}])

    def test_94916_skips_voice_team_before_selecting_main_cvs(self) -> None:
        intro = """
        <p>🌳配音组：</p>
        <p>配音团队：729声工场@729声工场</p>
        <p>配音导演：刘校妤@牛怒牛笑魚</p>
        <p>旁白：家明@家明_HF</p>
        <p>温然/李述：孙路路@孙路路729</p>
        <p>顾昀迟：张福正@歪歪福正了</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "温然/李述", "display_name": "孙路路", "account_name": "孙路路729"},
                {"role_name": "顾昀迟", "display_name": "张福正", "account_name": "歪歪福正了"},
            ],
        )

    def test_extracts_candidates_from_plain_cv_section_titles(self) -> None:
        for title in ("配音组", "配音：", "CAST", "CV"):
            with self.subTest(title=title):
                intro = f"""
                <p>{title}</p>
                <p>角色甲：甲声优@alias</p>
                <p>角色乙：乙声优</p>
                """

                candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

                self.assertEqual(
                    candidates,
                    [
                        {"role_name": "角色甲", "display_name": "甲声优", "account_name": "alias"},
                        {"role_name": "角色乙", "display_name": "乙声优"},
                    ],
                )

    def test_extracts_candidates_from_decorated_cv_section_title(self) -> None:
        intro = """
        <p>ﾟ ˖◛⁺配音组☁ ﾟ</p>
        <p>张沉：孙睿扬@Sun睿扬</p>
        <p>程声：云惟一@-云惟一</p>
        <p>参与配音：阿步、姜贺</p>
        <p>ﾟ💦 制作组 ﾟ ˖◛⁺</p>
        <p>配音导演：张馨月</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "张沉", "display_name": "孙睿扬", "account_name": "Sun睿扬"},
                {"role_name": "程声", "display_name": "云惟一", "account_name": "-云惟一"},
            ],
        )

    def test_filters_staff_lines_inside_cv_section(self) -> None:
        intro = """
        <p>配音组</p>
        <p>谢岫：吴晛@吴晛Hsien</p>
        <p>配音导演：张馨月</p>
        <p>录音棚：九紫声优团</p>
        <p>录音师：旭阳</p>
        <p>霍无归：云惟一@-云惟一</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "谢岫", "display_name": "吴晛", "account_name": "吴晛Hsien"},
                {"role_name": "霍无归", "display_name": "云惟一", "account_name": "-云惟一"},
            ],
        )

    def test_94893_decorated_production_section_stops_before_promotion(self) -> None:
        intro = """
        <p>▷配音组◁</p>
        <p>萧华雍：柯暮卿@柯暮卿</p>
        <p>▷制作组◁</p>
        <p>配音导演：柯暮卿@柯暮卿 阑倾@阑倾-whisper</p>
        <p>宣发：鸡蛋饺子包邮</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro, limit=100)

        self.assertEqual(candidates, [{"role_name": "萧华雍", "display_name": "柯暮卿"}])

    def test_94802_intro_skips_director_and_recording_engineer(self) -> None:
        intro = """
        <p>=配音组=</p>
        <p>配音导演：路知行@路知知 马海燕</p>
        <p>录音师：旭阳</p>
        <p>刑鸣：路知行@路知知</p>
        <p>虞仲夜：郑希@96度希</p>
        <p>刑宏：张震@配音演员张震</p>
        <p>参与配音：孔喆@孔喆4396</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(
            candidates,
            [
                {"role_name": "刑鸣", "display_name": "路知行", "account_name": "路知知"},
                {"role_name": "虞仲夜", "display_name": "郑希", "account_name": "96度希"},
            ],
        )

    def test_does_not_extract_role_lines_without_cv_section_title(self) -> None:
        intro = """
        <p>普通简介</p>
        <p>张沉：孙睿扬@Sun睿扬</p>
        <p>配音导演：张馨月</p>
        """

        candidates = refresh_platform_metadata.extract_missevan_intro_cv_candidates(intro)

        self.assertEqual(candidates, [])


class MissevanBatchIntroTests(unittest.TestCase):
    def test_target_count_is_four_only_for_all_age(self) -> None:
        self.assertEqual(refresh_platform_metadata.missevan_target_main_cv_count(3), 4)
        self.assertEqual(refresh_platform_metadata.missevan_target_main_cv_count(4), 2)
        self.assertEqual(refresh_platform_metadata.missevan_target_main_cv_count(6), 2)

    def test_fetch_rows_sorts_numeric_ids_dedupes_and_uses_one_request(self) -> None:
        requester = Mock()
        requester.request_json.return_value = {
            "info": {
                "Datas": [
                    {"id": 20, "soundstr": "声展乙", "intro": "乙"},
                    {"id": "10", "soundstr": "声展甲", "intro": "甲"},
                    {"id": 10, "soundstr": "重复", "intro": "重复"},
                    {"id": "bad", "soundstr": "非法", "intro": "非法"},
                ]
            }
        }

        rows = refresh_platform_metadata.fetch_missevan_episode_intro_rows(requester, "94857")

        self.assertEqual([row["sound_id"] for row in rows], [10, 20])
        requester.request_json.assert_called_once_with(
            "https://www.missevan.com/dramaapi/getdramaepisodedetails?drama_id=94857&p=1&page_size=100"
        )

    def test_showcase_uses_first_two_sounds_and_allows_multiple_cvs_per_intro(self) -> None:
        rows = [
            {"sound_id": 10, "title": "声展甲", "intro": "<p>配音组</p><p>角色甲：CV甲</p><p>角色乙：CV乙</p>"},
            {"sound_id": 20, "title": "声展乙", "intro": "<p>配音组</p><p>角色丙：CV丙</p>"},
            {"sound_id": 30, "title": "声展丙", "intro": "<p>配音组</p><p>角色丁：CV丁</p>"},
        ]

        candidates = refresh_platform_metadata.collect_missevan_episode_intro_candidates(rows, method="showcase")

        self.assertEqual([item["display_name"] for item in candidates], ["CV甲", "CV乙"])

    def test_94893_showcases_collect_one_cv_from_each_sound(self) -> None:
        rows = [
            {
                "sound_id": 13080301,
                "title": "声展 · 萧华雍",
                "intro": "<p>▷配音组◁</p><p>萧华雍：柯暮卿@柯暮卿</p><p>▷制作组◁</p><p>宣发：鸡蛋饺子包邮</p>",
            },
            {
                "sound_id": 13085282,
                "title": "声展 · 沈羲和",
                "intro": "<p>▷配音组◁</p><p>沈羲和：醋醋@醋醋cucu</p><p>▷制作组◁</p><p>宣发：鸡蛋饺子包邮</p>",
            },
        ]

        candidates = refresh_platform_metadata.collect_missevan_episode_intro_candidates(rows, method="showcase")

        self.assertEqual(
            candidates,
            [
                {"role_name": "萧华雍", "display_name": "柯暮卿"},
                {"role_name": "沈羲和", "display_name": "醋醋", "account_name": "醋醋cucu"},
            ],
        )

    def test_preview_matches_keywords_and_excludes_existing_names(self) -> None:
        rows = [
            {"sound_id": 10, "title": "先导篇", "intro": "<p>配音组</p><p>角色甲：已有CV</p>"},
            {"sound_id": 20, "title": "主题曲", "intro": "<p>配音组</p><p>角色乙：CV乙</p>"},
            {"sound_id": 30, "title": "kv角色展", "intro": "<p>配音组</p><p>角色丙：CV丙</p>"},
            {"sound_id": 40, "title": "PV预告", "intro": "<p>配音组</p><p>角色丁：CV丁</p>"},
            {"sound_id": 50, "title": "普通正剧", "intro": "<p>配音组</p><p>角色戊：CV戊</p>"},
        ]

        candidates = refresh_platform_metadata.collect_missevan_episode_intro_candidates(
            rows,
            method="preview",
            excluded_names={refresh_platform_metadata.normalize_match("已有CV")},
        )

        self.assertEqual([item["display_name"] for item in candidates], ["CV乙", "CV丙"])

    def test_preview_title_matches_kv_case_insensitively(self) -> None:
        self.assertTrue(refresh_platform_metadata.is_missevan_preview_intro_title("角色KV"))
        self.assertTrue(refresh_platform_metadata.is_missevan_preview_intro_title("角色kv"))

    def test_all_age_keeps_direct_cvs_then_fills_showcase_and_preview_to_four(self) -> None:
        combined_map = {}
        node = {
            "type": 3,
            "maincvs": [10],
            "cvnames": {"10": "直接CV"},
            "cvroles": {"10": "直接角色"},
        }
        found_ids = {"声展甲": 11, "声展乙": 12, "预告丙": 13}

        def search(name):
            return {"cv_id": found_ids[name], "display_name": name}

        with patch.object(refresh_platform_metadata, "save_combined_map"):
            after_showcase = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                node,
                "100",
                [
                    {"role_name": "甲", "display_name": "声展甲"},
                    {"role_name": "乙", "display_name": "声展乙"},
                ],
                combined_map=combined_map,
                search_cv=search,
                existing_entries_first=True,
            )
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                after_showcase,
                "100",
                [{"role_name": "丙", "display_name": "预告丙"}],
                combined_map=combined_map,
                search_cv=search,
                existing_entries_first=True,
            )

        self.assertEqual(updated["maincvs"], [10, 11, 12, 13])

    def test_two_intro_stages_preserve_existing_name_only_candidates(self) -> None:
        combined_map = {}
        with patch.object(refresh_platform_metadata, "save_combined_map"):
            after_showcase = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                {"type": 3, "maincvs": [], "cvnames": {}, "cvroles": {}},
                "100",
                [{"role_name": "角色甲", "display_name": "声展甲"}],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
                existing_entries_first=True,
            )
            updated = refresh_platform_metadata.apply_missevan_intro_cv_fallback(
                after_showcase,
                "100",
                [{"role_name": "角色乙", "display_name": "预告乙"}],
                combined_map=combined_map,
                search_cv=Mock(return_value=None),
                existing_entries_first=True,
            )

        self.assertEqual(updated["fallbackCvNames"], ["声展甲", "预告乙"])
        self.assertEqual(updated["fallbackCvRoles"], {"声展甲": "角色甲", "预告乙": "角色乙"})


if __name__ == "__main__":
    unittest.main()
