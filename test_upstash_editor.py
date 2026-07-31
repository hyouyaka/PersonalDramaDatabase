from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import upstash_editor


class FakeUpstash:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[list[object]] = []

    def __call__(self, command: list[object]) -> object:
        self.commands.append(list(command))
        operation = str(command[0]).upper()
        if operation == "GET":
            return self.strings.get(str(command[1]))
        if operation == "SET":
            self.strings[str(command[1])] = str(command[2])
            return "OK"
        if operation == "HGETALL":
            result: list[str] = []
            for field, value in self.hashes.get(str(command[1]), {}).items():
                result.extend([field, value])
            return result
        if operation == "HGET":
            return self.hashes.get(str(command[1]), {}).get(str(command[2]))
        if operation == "HSET":
            target = self.hashes.setdefault(str(command[1]), {})
            created = 0
            for index in range(2, len(command), 2):
                field = str(command[index])
                created += int(field not in target)
                target[field] = str(command[index + 1])
            return created
        if operation == "HLEN":
            return len(self.hashes.get(str(command[1]), {}))
        if operation == "HMGET":
            target = self.hashes.get(str(command[1]), {})
            return [target.get(str(field)) for field in command[2:]]
        if operation == "EXPIRE":
            self.ttls[str(command[1])] = int(command[2])
            return 1
        if operation == "EVAL":
            script = str(command[1])
            if script in (upstash_editor.STRING_SAVE_SCRIPT, upstash_editor.INFO_STRING_SAVE_SCRIPT):
                source_key, meta_key = str(command[3]), str(command[4])
                argument_offset = 1 if script == upstash_editor.INFO_STRING_SAVE_SCRIPT else 0
                current = self.strings.get(source_key, "")
                if hashlib.sha1(current.encode("utf-8")).hexdigest() != str(command[5 + argument_offset]):
                    return 0
                current_meta = self.strings.get(meta_key)
                expected_meta = str(command[8 + argument_offset])
                if expected_meta == "__missing__":
                    if current_meta is not None:
                        return 0
                elif current_meta is None or hashlib.sha1(current_meta.encode("utf-8")).hexdigest() != expected_meta:
                    return 0
                self.strings[source_key] = str(command[6 + argument_offset])
                self.strings[meta_key] = str(command[7 + argument_offset])
                if script == upstash_editor.INFO_STRING_SAVE_SCRIPT:
                    legacy_key = str(command[5])
                    if legacy_key in self.strings:
                        self.strings[legacy_key] = str(command[7])
                return 1
            if script == upstash_editor.HASH_SAVE_SCRIPT:
                stable_key, staging_key, meta_key = map(str, command[3:6])
                current_meta = self.hashes.get(stable_key, {}).get("__meta__")
                if current_meta != str(command[6]):
                    return 0
                current_rank_meta = self.strings.get(meta_key)
                expected_rank_meta = str(command[8])
                if expected_rank_meta == "__missing__":
                    if current_rank_meta is not None:
                        return 0
                elif current_rank_meta is None or hashlib.sha1(current_rank_meta.encode("utf-8")).hexdigest() != expected_rank_meta:
                    return 0
                self.hashes[stable_key] = self.hashes.pop(staging_key)
                self.ttls.pop(staging_key, None)
                self.strings[meta_key] = str(command[7])
                return 1
        raise AssertionError(command)


def build_missevan_info(count: int = 100) -> dict:
    return {
        str(index): {"dramaId": index, "title": f"剧目 {index}"}
        for index in range(1, count + 1)
    }


class UpstashEditorTests(unittest.TestCase):
    def test_find_list_item_by_identity_survives_index_shift(self) -> None:
        original = [{"cvName": "甲"}, {"cvName": "乙"}]
        shifted = original[1:]

        restored = upstash_editor.find_list_item_by_identity(original, "cvName", shifted[0]["cvName"])

        self.assertEqual(restored, {"cvName": "乙"})

    def test_repeated_load_reuses_same_backup_until_content_changes(self) -> None:
        fake = FakeUpstash()
        fake.strings["missevan:info:v2"] = upstash_editor.compact_json(build_missevan_info())
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir)
            first = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=backup_root,
            )
            second = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=backup_root,
            )
            fake.strings["missevan:info:v2"] = upstash_editor.compact_json(build_missevan_info(101))
            changed = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=backup_root,
            )

            self.assertEqual(second.backup_path, first.backup_path)
            self.assertNotEqual(changed.backup_path, first.backup_path)
            self.assertEqual(len(list(backup_root.glob("*.json"))), 2)

    def test_backup_deduplication_is_scoped_to_resource_key(self) -> None:
        raw = upstash_editor.compact_json(build_missevan_info())
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir)
            first, _digest, _bytes = upstash_editor._write_backup(
                upstash_editor.RESOURCE_SPECS["missevan:info:v2"],
                raw_string=raw,
                raw_fields=None,
                backup_root=backup_root,
            )
            second, _digest, _bytes = upstash_editor._write_backup(
                upstash_editor.RESOURCE_SPECS["manbo:info:v2"],
                raw_string=raw,
                raw_fields=None,
                backup_root=backup_root,
            )

            self.assertNotEqual(first, second)
            self.assertEqual(len(list(backup_root.glob("*.json"))), 2)

    def test_string_load_writes_exact_backup_before_parsing(self) -> None:
        fake = FakeUpstash()
        raw = json.dumps(build_missevan_info(), ensure_ascii=False, indent=1)
        fake.strings["missevan:info:v2"] = raw
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=Path(temp_dir),
            )
            self.assertEqual(loaded.backup_path.read_text(encoding="utf-8"), raw)
            self.assertEqual(loaded.content_sha1, hashlib.sha1(raw.encode("utf-8")).hexdigest())

    def test_invalid_json_is_backed_up_before_load_fails(self) -> None:
        fake = FakeUpstash()
        fake.strings["missevan:info:v2"] = "{invalid"
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
                upstash_editor.load_resource(
                    "missevan:info:v2",
                    upstash=fake,
                    backup_root=backup_root,
                )
            backups = list(backup_root.glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{invalid")

    def test_backup_failure_aborts_before_payload_is_exposed(self) -> None:
        fake = FakeUpstash()
        fake.strings["missevan:info:v2"] = "{}"
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-directory"
            blocker.write_text("blocked", encoding="utf-8")
            with self.assertRaises(OSError):
                upstash_editor.load_resource(
                    "missevan:info:v2",
                    upstash=fake,
                    backup_root=blocker / "backups",
                )

        self.assertEqual(fake.commands, [["GET", "missevan:info:v2"]])

    def test_info_save_updates_existing_v1_v2_and_meta(self) -> None:
        fake = FakeUpstash()
        source = build_missevan_info()
        fake.strings["missevan:info:v2"] = upstash_editor.compact_json(source)
        fake.strings["missevan:info:v1"] = "legacy"
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=Path(temp_dir),
            )
        source["1"]["title"] = "人工修正"
        with patch.object(upstash_editor, "_write_local_mirrors"):
            result = upstash_editor.save_resource(
                loaded,
                source,
                upstash=fake,
                now=lambda: "2026-07-20T12:00:00+00:00",
            )
        self.assertEqual(json.loads(fake.strings["missevan:info:v2"])["1"]["title"], "人工修正")
        self.assertEqual(json.loads(fake.strings["missevan:info:v1"])["1"]["title"], "人工修正")
        meta = json.loads(fake.strings["missevan:info:meta:v2"])
        self.assertEqual(meta["contentSha1"], result.content_sha1)
        self.assertTrue(any("missevan:info:v1" in command for command in fake.commands))

    def test_info_save_does_not_recreate_retired_v1(self) -> None:
        fake = FakeUpstash()
        source = build_missevan_info()
        fake.strings["missevan:info:v2"] = upstash_editor.compact_json(source)
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=Path(temp_dir),
            )
        source["1"]["title"] = "人工修正"
        with patch.object(upstash_editor, "_write_local_mirrors"):
            upstash_editor.save_resource(loaded, source, upstash=fake)

        self.assertNotIn("missevan:info:v1", fake.strings)

    def test_cvid_map_save_updates_body_and_meta_atomically(self) -> None:
        fake = FakeUpstash()
        source = {
            f"CV {index}": {
                "cvId": index,
                "missevanCvId": index,
                "manboCvId": None,
                "displayName": f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }
        fake.strings[upstash_editor.CVID_MAP_KEY] = upstash_editor.compact_json(source)
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                upstash_editor.CVID_MAP_KEY,
                upstash=fake,
                backup_root=Path(temp_dir),
            )
        source["CV 1"]["aliases"] = ["一号"]

        with patch.object(upstash_editor, "_write_local_mirrors"):
            result = upstash_editor.save_resource(
                loaded,
                source,
                upstash=fake,
                now=lambda: "2026-07-30T12:00:00+00:00",
            )

        self.assertEqual(
            json.loads(fake.strings[upstash_editor.CVID_MAP_KEY])["CV 1"]["aliases"],
            ["一号"],
        )
        meta = json.loads(fake.strings[upstash_editor.CVID_MAP_META_KEY])
        self.assertEqual(meta["schemaVersion"], 1)
        self.assertEqual(meta["dataKey"], upstash_editor.CVID_MAP_KEY)
        self.assertEqual(meta["recordCount"], 50)
        self.assertEqual(meta["contentSha1"], result.content_sha1)

    def test_cvid_map_load_reports_record_count_mismatch(self) -> None:
        fake = FakeUpstash()
        source = {
            f"CV {index}": {"displayName": f"CV {index}", "aliases": []}
            for index in range(1, 51)
        }
        encoded = upstash_editor.compact_json(source)
        fake.strings[upstash_editor.CVID_MAP_KEY] = encoded
        fake.strings[upstash_editor.CVID_MAP_META_KEY] = upstash_editor.compact_json(
            {
                "schemaVersion": 1,
                "dataKey": upstash_editor.CVID_MAP_KEY,
                "contentSha1": upstash_editor.sha1_text(encoded),
                "updatedAt": "2026-07-30T12:00:00+00:00",
                "recordCount": 49,
                "bytes": len(encoded.encode("utf-8")),
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                upstash_editor.CVID_MAP_KEY,
                upstash=fake,
                backup_root=Path(temp_dir),
            )

        self.assertEqual(loaded.meta_status, "JSON Meta 摘要不匹配")

    def test_cvid_map_collection_uses_record_key_as_edit_identity(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {"旧键": {"displayName": "可编辑显示名", "aliases": []}}

        collection = upstash_editor.collection_refs(spec, payload)[0]

        self.assertIsNone(collection.identity_field)
        self.assertIs(collection.container, payload)

    def test_cvid_map_allows_display_name_to_differ_from_record_key(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {
            f"CV {index}": {
                "displayName": "正式名" if index == 1 else f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }

        upstash_editor.validate_payload(spec, payload)

    def test_cvid_map_rejects_empty_display_name(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {
            f"CV {index}": {
                "displayName": "" if index == 1 else f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }

        with self.assertRaisesRegex(ValueError, "displayName must not be empty"):
            upstash_editor.validate_payload(spec, payload)

    def test_cvid_map_rejects_mismatched_missevan_id_fields(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {
            f"CV {index}": {
                "cvId": index,
                "missevanCvId": 999 if index == 1 else index,
                "manboCvId": None,
                "displayName": f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }

        with self.assertRaisesRegex(ValueError, "cvId and missevanCvId must match"):
            upstash_editor.validate_payload(spec, payload)

    def test_cvid_map_rejects_duplicate_missevan_id(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {
            f"CV {index}": {
                "cvId": 1 if index == 2 else index,
                "missevanCvId": 1 if index == 2 else index,
                "manboCvId": None,
                "displayName": f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }

        with self.assertRaisesRegex(ValueError, "Duplicate missevan CVID 1"):
            upstash_editor.validate_payload(spec, payload)

    def test_cvid_map_rejects_duplicate_manbo_id(self) -> None:
        spec = upstash_editor.RESOURCE_SPECS[upstash_editor.CVID_MAP_KEY]
        payload = {
            f"CV {index}": {
                "cvId": index,
                "missevanCvId": index,
                "manboCvId": 101 if index in (1, 2) else None,
                "displayName": f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 51)
        }

        with self.assertRaisesRegex(ValueError, "Duplicate manbo CVID 101"):
            upstash_editor.validate_payload(spec, payload)

    def test_cvid_map_editor_rejects_generated_identity_changes(self) -> None:
        original = {
            f"CV {index}": {
                "cvId": 330001 if index == 1 else index,
                "missevanCvId": 330001 if index == 1 else index,
                "manboCvId": None,
                "displayName": f"CV {index}",
                "aliases": [],
            }
            for index in range(1, 52)
        }

        def assert_rejected(mutator) -> None:
            fake = FakeUpstash()
            source = json.loads(json.dumps(original))
            fake.strings[upstash_editor.CVID_MAP_KEY] = upstash_editor.compact_json(source)
            with tempfile.TemporaryDirectory() as temp_dir:
                loaded = upstash_editor.load_resource(
                    upstash_editor.CVID_MAP_KEY,
                    upstash=fake,
                    backup_root=Path(temp_dir),
                )
            mutator(source)
            with (
                patch.object(upstash_editor, "_write_local_mirrors"),
                self.assertRaisesRegex(ValueError, "registry-managed"),
            ):
                upstash_editor.save_resource(loaded, source, upstash=fake)

        with self.subTest("display name"):
            assert_rejected(lambda source: source["CV 1"].__setitem__("displayName", "新名字"))
        with self.subTest("generated ID"):
            assert_rejected(
                lambda source: source["CV 1"].update(
                    {"cvId": 50001, "missevanCvId": 50001}
                )
            )
        with self.subTest("delete"):
            assert_rejected(lambda source: source.pop("CV 1"))
        with self.subTest("add"):
            assert_rejected(
                lambda source: source.__setitem__(
                    "新增生成身份",
                    {
                        "cvId": 330002,
                        "missevanCvId": 330002,
                        "manboCvId": None,
                        "displayName": "新增生成身份",
                        "aliases": [],
                    },
                )
            )

    def test_string_conflict_does_not_write_payload_or_meta(self) -> None:
        fake = FakeUpstash()
        source = build_missevan_info()
        fake.strings["missevan:info:v2"] = upstash_editor.compact_json(source)
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                "missevan:info:v2",
                upstash=fake,
                backup_root=Path(temp_dir),
            )
        fake.strings["missevan:info:v2"] = upstash_editor.compact_json({**source, "999": {"dramaId": 999}})
        with self.assertRaisesRegex(RuntimeError, "Concurrent update"):
            upstash_editor.save_resource(loaded, source, upstash=fake)
        self.assertNotIn("missevan:info:meta:v2", fake.strings)

    def test_hash_save_builds_digest_meta_and_preserves_other_cv_fields(self) -> None:
        fake = FakeUpstash()
        key = "ranks:trend:cv:v2"
        fake.hashes[key] = {
            "__meta__": upstash_editor.compact_json(
                {
                    "version": 2,
                    "kind": "cv",
                    "updated_at": "old",
                    "platforms": {
                        "missevan": {"retentionDates": 50},
                        "manbo": {"retentionDates": 50},
                    },
                }
            ),
            "missevan:甲": upstash_editor.compact_json(
                {"version": 2, "cvName": "甲", "samples": {"2026-07-10": {"metrics": {}}}}
            ),
            "manbo:乙": upstash_editor.compact_json(
                {"version": 2, "cvName": "乙", "samples": {"2026-07-10": {"metrics": {}}}}
            ),
        }
        fake.strings["ranks:meta"] = upstash_editor.compact_json(
            {
                "normal": {"updatedAt": None, "publishedAt": None},
                "cv": {"updatedAt": None, "publishedAt": None},
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(key, upstash=fake, backup_root=Path(temp_dir))
        payload = dict(loaded.payload)
        payload["missevan:甲"]["samples"]["2026-07-17"] = {"metrics": {"totalViewCount": 2}}
        with patch.object(upstash_editor, "_write_local_mirrors"):
            result = upstash_editor.save_resource(
                loaded,
                payload,
                upstash=fake,
                now=lambda: "2026-07-20T12:00:00+00:00",
            )
        self.assertIn("manbo:乙", fake.hashes[key])
        meta = json.loads(fake.hashes[key]["__meta__"])
        self.assertEqual(meta["contentSha1"], result.content_sha1)
        self.assertEqual(meta["entityCount"], 2)
        rank_meta = json.loads(fake.strings["ranks:meta"])
        self.assertEqual(rank_meta["cv"]["resources"][key]["contentSha1"], result.content_sha1)

    def test_hash_conflict_keeps_stable_key_and_rank_meta(self) -> None:
        fake = FakeUpstash()
        key = "ranks:trend:missevan:v2"
        fake.hashes[key] = {
            "__meta__": upstash_editor.compact_json(
                {"version": 2, "platform": "missevan", "retentionDates": 45}
            ),
            "1": upstash_editor.compact_json(
                {"version": 2, "id": "1", "name": "测试", "samples": {"2026-07-10": {}}}
            ),
        }
        fake.strings["ranks:meta"] = "{}"
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(key, upstash=fake, backup_root=Path(temp_dir))
        fake.hashes[key]["__meta__"] = upstash_editor.compact_json(
            {"version": 2, "platform": "missevan", "retentionDates": 45, "updated_at": "raced"}
        )
        stable_before = dict(fake.hashes[key])
        with self.assertRaisesRegex(RuntimeError, "Concurrent update"):
            upstash_editor.save_resource(loaded, loaded.payload, upstash=fake)
        self.assertEqual(fake.hashes[key], stable_before)
        self.assertEqual(fake.strings["ranks:meta"], "{}")

    def test_cv_rank_save_rebuilds_sequential_ranks(self) -> None:
        fake = FakeUpstash()
        payload = {
            "version": 3,
            "date": "2026-07-17",
            "generated_at": "old",
            "rankings": {
                "missevan": [{"cvName": "甲", "rank": 8}, {"cvName": "乙", "rank": 12}],
                "manbo": [],
            },
            "paidRankings": {"missevan": [], "manbo": []},
        }
        fake.strings["ranks:cv:latest"] = upstash_editor.compact_json(payload)
        fake.strings["ranks:meta"] = "{}"
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = upstash_editor.load_resource(
                "ranks:cv:latest",
                upstash=fake,
                backup_root=Path(temp_dir),
            )
        with patch.object(upstash_editor, "_write_local_mirrors"):
            upstash_editor.save_resource(loaded, payload, upstash=fake)
        saved = json.loads(fake.strings["ranks:cv:latest"])
        self.assertEqual([item["rank"] for item in saved["rankings"]["missevan"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
