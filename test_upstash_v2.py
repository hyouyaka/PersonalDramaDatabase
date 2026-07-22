from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import upstash_v2


class FakeUpstash:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[list[object]] = []

    def __call__(self, command: list[object]) -> object:
        self.commands.append(command)
        operation = str(command[0]).upper()
        if operation == "GET":
            return self.strings.get(str(command[1]))
        if operation == "SET":
            self.strings[str(command[1])] = str(command[2])
            return "OK"
        if operation == "EXISTS":
            key = str(command[1])
            return int(key in self.strings or key in self.hashes)
        if operation == "HGETALL":
            result: list[str] = []
            for field, value in self.hashes.get(str(command[1]), {}).items():
                result.extend([field, value])
            return result
        if operation == "EVAL":
            if command[1] == upstash_v2.HASH_ACTIVATE_WITH_META_SCRIPT:
                source, target = str(command[3]), str(command[4])
                expected_meta = str(command[6])
                expected_rank_meta = str(command[7])
                current_meta = self.hashes.get(target, {}).get("__meta__")
                if expected_meta == "__missing__":
                    if target in self.hashes:
                        return 0
                elif current_meta != expected_meta:
                    return 0
                current_rank_meta = self.strings.get(str(command[5]))
                if expected_rank_meta == "__missing__":
                    if current_rank_meta is not None:
                        return 0
                elif current_rank_meta is None or hashlib.sha1(current_rank_meta.encode("utf-8")).hexdigest() != expected_rank_meta:
                    return 0
                self.hashes[target] = self.hashes.pop(source)
                self.ttls.pop(source, None)
                self.ttls.pop(target, None)
                self.strings[str(command[5])] = str(command[8])
                return 1
            if command[1] == upstash_v2.INFO_SOURCE_COMPARE_AND_PUBLISH_SCRIPT:
                v2_key = str(command[3])
                meta_key = str(command[4])
                legacy_key = str(command[5])
                expected = str(command[6])
                current = self.strings.get(v2_key)
                if expected == "__missing__":
                    if current is not None:
                        return 0
                elif current is None or hashlib.sha1(current.encode("utf-8")).hexdigest() != expected:
                    return 0
                self.strings[v2_key] = str(command[7])
                self.strings[meta_key] = str(command[8])
                if legacy_key in self.strings:
                    self.strings[legacy_key] = str(command[7])
                return 1
            if command[1] == upstash_v2.RANK_STRING_PUBLISH_SCRIPT:
                current_meta = self.strings.get(str(command[4]))
                expected_meta = str(command[7])
                if expected_meta == "__missing__":
                    if current_meta is not None:
                        return 0
                elif current_meta is None or hashlib.sha1(current_meta.encode("utf-8")).hexdigest() != expected_meta:
                    return 0
                self.strings[str(command[3])] = str(command[5])
                self.strings[str(command[4])] = str(command[6])
                return 1
            v2_key = str(command[3])
            meta_key = str(command[4])
            expected = str(command[5])
            current = self.strings.get(v2_key, "")
            if hashlib.sha1(current.encode("utf-8")).hexdigest() != expected:
                return 0
            self.strings[meta_key] = str(command[6])
            return 1
        if operation == "HSET":
            target = self.hashes.setdefault(str(command[1]), {})
            created = 0
            for index in range(2, len(command), 2):
                field = str(command[index])
                created += int(field not in target)
                target[field] = str(command[index + 1])
            return created
        if operation == "EXPIRE":
            self.ttls[str(command[1])] = int(command[2])
            return 1
        if operation == "HLEN":
            return len(self.hashes.get(str(command[1]), {}))
        if operation == "HMGET":
            target = self.hashes.get(str(command[1]), {})
            return [target.get(str(field)) for field in command[2:]]
        raise AssertionError(command)


class UpstashV2Tests(unittest.TestCase):
    def test_rank_string_publish_retries_meta_race_without_losing_other_resource(self) -> None:
        class RacingMetaUpstash(FakeUpstash):
            def __init__(self) -> None:
                super().__init__()
                self.raced = False

            def __call__(self, command: list[object]) -> object:
                if command[:2] == ["EVAL", upstash_v2.RANK_STRING_PUBLISH_SCRIPT] and not self.raced:
                    self.raced = True
                    self.strings[upstash_v2.RANK_META_KEY] = upstash_v2.compact_json(
                        {
                            "normal": {
                                "resources": {
                                    "ranks:latest": {
                                        "dataType": "string",
                                        "contentSha1": "other",
                                        "bytes": 10,
                                        "updatedAt": "other",
                                    }
                                }
                            }
                        }
                    )
                return super().__call__(command)

        fake = RacingMetaUpstash()
        upstash_v2.publish_rank_string(
            "ranks:cv:latest",
            {"generated_at": "2026-07-22T00:00:00+00:00"},
            scope="cv",
            upstash=fake,
        )

        meta = json.loads(fake.strings[upstash_v2.RANK_META_KEY])
        self.assertIn("ranks:latest", meta["normal"]["resources"])
        self.assertIn("ranks:cv:latest", meta["cv"]["resources"])

    def test_publish_mode_off_skips_best_effort_v2_writes(self) -> None:
        fake = FakeUpstash()
        with patch.dict(os.environ, {"UPSTASH_V2_PUBLISH_MODE": "off"}):
            result = upstash_v2.publish_info_v2(
                "missevan:info:v1",
                {"100": {"title": "测试剧"}},
                upstash=fake,
            )

        self.assertIsNone(result)
        self.assertEqual(fake.commands, [])

    def test_every_info_writer_imports_the_shared_v2_publisher(self) -> None:
        root = Path(__file__).resolve().parent
        for filename in (
            "sync_new_drama_ids.py",
            "refresh_watch_counts.py",
            "backfill_missevan_covers.py",
            "backfill_manbo_covers.py",
        ):
            source = (root / filename).read_text(encoding="utf-8")
            self.assertIn("publish_info_v2(", source, filename)

    def test_info_v2_and_meta_are_published_from_the_same_payload(self) -> None:
        fake = FakeUpstash()
        payload = {"100": {"dramaId": 100, "title": "测试剧"}}
        meta = upstash_v2.publish_info_v2("missevan:info:v2", payload, upstash=fake, force=True)

        encoded = fake.strings["missevan:info:v2"]
        self.assertEqual(json.loads(encoded), payload)
        self.assertEqual(json.loads(fake.strings["missevan:info:meta:v2"]), meta)
        self.assertEqual(meta["contentSha1"], hashlib.sha1(encoded.encode("utf-8")).hexdigest())

    def test_info_publish_refuses_a_newer_remote_body_after_a_race(self) -> None:
        class RacingUpstash(FakeUpstash):
            def __init__(self) -> None:
                super().__init__()
                self.raced = False

            def __call__(self, command: list[object]) -> object:
                if str(command[0]).upper() == "EVAL" and not self.raced:
                    self.raced = True
                    self.strings["missevan:info:v2"] = upstash_v2.compact_json({"new": {"title": "更新内容"}})
                return super().__call__(command)

        fake = RacingUpstash()
        with self.assertRaisesRegex(RuntimeError, "concurrently changed"):
            upstash_v2.publish_info_v2(
                "missevan:info:v2",
                {"old": {"title": "旧内容"}},
                upstash=fake,
                force=True,
            )
        self.assertEqual(json.loads(fake.strings["missevan:info:v2"]), {"new": {"title": "更新内容"}})
        self.assertNotIn("missevan:info:meta:v2", fake.strings)

    def test_info_publish_updates_existing_v1_but_does_not_recreate_missing_v1(self) -> None:
        fake = FakeUpstash()
        old_source = json.dumps({"old": {"title": "旧内容"}}, ensure_ascii=False)
        fake.strings["missevan:info:v2"] = old_source
        fake.strings["missevan:info:v1"] = "legacy"
        upstash_v2.publish_info_v2(
            "missevan:info:v2",
            {"new": {"title": "更新内容"}},
            upstash=fake,
            force=True,
            source_encoded=old_source,
        )
        self.assertEqual(json.loads(fake.strings["missevan:info:v1"]), {"new": {"title": "更新内容"}})
        fake.strings.pop("missevan:info:v1")
        current = fake.strings["missevan:info:v2"]
        upstash_v2.publish_info_v2(
            "missevan:info:v2",
            {"final": {"title": "最终内容"}},
            upstash=fake,
            force=True,
            source_encoded=current,
        )
        self.assertNotIn("missevan:info:v1", fake.strings)

    def test_normal_v2_retains_hot_dates_and_last_rank_summary(self) -> None:
        payload = {
            "version": 1,
            "platform": "missevan",
            "updated_at": "now",
            "dates": ["2026-01-01", "2026-02-01", "2026-03-01"],
            "dramas": {
                "100": {
                    "id": "100",
                    "name": "测试剧",
                    "samples": {
                        "2026-01-01": {
                            "generated_at": "old",
                            "metrics": {"view_count": 1, "reward_num": 9},
                            "ranks": [{"key": "hot", "name": "热榜", "position": 3}],
                        },
                        "2026-03-01": {
                            "generated_at": "new",
                            "metrics": {"view_count": 2, "subscription_num": 4, "reward_num": 10},
                            "ranks": [],
                        },
                    },
                }
            },
        }

        meta, fields = upstash_v2.build_normal_trend_v2(payload, "missevan", retention_dates=1)

        self.assertEqual(meta["dates"], ["2026-03-01"])
        self.assertEqual(fields["100"]["lastRank"]["date"], "2026-01-01")
        metrics = fields["100"]["samples"]["2026-03-01"]["metrics"]
        self.assertEqual(metrics, {"view_count": 2, "subscription_num": 4})

    def test_normal_v2_preserves_danmaku_not_required_marker(self) -> None:
        payload = {
            "dates": ["2026-07-16"],
            "dramas": {
                "100": {
                    "id": "100",
                    "name": "第31名",
                    "samples": {
                        "2026-07-16": {
                            "metrics": {"view_count": 10, "danmaku_uid_count": "无需抓取"},
                            "ranks": [{"key": "popular_weekly", "name": "人气周榜", "position": 31}],
                        }
                    },
                }
            },
        }

        _meta, fields = upstash_v2.build_normal_trend_v2(payload, "missevan")

        self.assertEqual(
            fields["100"]["samples"]["2026-07-16"]["metrics"]["danmaku_uid_count"],
            "无需抓取",
        )

    def test_atomic_hash_publish_replaces_stable_key_only_after_verification(self) -> None:
        fake = FakeUpstash()
        old_meta = '{"version":2,"revision":"old"}'
        fake.hashes["ranks:trend:missevan:v2"] = {"__meta__": old_meta, "old": "value"}

        upstash_v2.publish_hash_snapshot_atomic(
            "ranks:trend:missevan:v2",
            {"version": 2, "entityCount": 1},
            {"100": {"version": 2, "id": "100", "samples": {}}},
            upstash=fake,
            expected_meta_raw=old_meta,
        )

        stable = fake.hashes["ranks:trend:missevan:v2"]
        self.assertNotIn("old", stable)
        self.assertEqual(json.loads(stable["100"])["id"], "100")
        self.assertNotIn("ranks:trend:missevan:v2", fake.ttls)
        self.assertTrue(
            any(command[:2] == ["EVAL", upstash_v2.HASH_ACTIVATE_WITH_META_SCRIPT] for command in fake.commands)
        )

    def test_hash_publish_retries_rank_meta_race_without_losing_other_resource(self) -> None:
        class RacingMetaUpstash(FakeUpstash):
            def __init__(self) -> None:
                super().__init__()
                self.raced = False

            def __call__(self, command: list[object]) -> object:
                if command[:2] == ["EVAL", upstash_v2.HASH_ACTIVATE_WITH_META_SCRIPT] and not self.raced:
                    self.raced = True
                    self.strings[upstash_v2.RANK_META_KEY] = upstash_v2.compact_json(
                        {
                            "cv": {
                                "resources": {
                                    "ranks:cv:latest": {
                                        "dataType": "string",
                                        "contentSha1": "other",
                                        "bytes": 10,
                                        "updatedAt": "other",
                                    }
                                }
                            }
                        }
                    )
                return super().__call__(command)

        fake = RacingMetaUpstash()
        old_meta = '{"version":2,"revision":"old"}'
        key = "ranks:trend:missevan:v2"
        fake.hashes[key] = {"__meta__": old_meta, "old": "value"}

        upstash_v2.publish_hash_snapshot_atomic(
            key,
            {"version": 2, "updated_at": "2026-07-22T00:00:00+00:00"},
            {"100": {"version": 2, "id": "100", "samples": {}}},
            upstash=fake,
            expected_meta_raw=old_meta,
        )

        meta = json.loads(fake.strings[upstash_v2.RANK_META_KEY])
        self.assertIn("ranks:cv:latest", meta["cv"]["resources"])
        self.assertIn(key, meta["normal"]["resources"])

    def test_failed_hash_verification_keeps_the_stable_snapshot(self) -> None:
        class InvalidLengthUpstash(FakeUpstash):
            def __call__(self, command: list[object]) -> object:
                if str(command[0]).upper() == "HLEN" and ":staging:" in str(command[1]):
                    super().__call__(command)
                    return 0
                return super().__call__(command)

        fake = InvalidLengthUpstash()
        fake.hashes["ranks:trend:missevan:v2"] = {"old": "value"}

        with self.assertRaisesRegex(RuntimeError, "Hash verification failed"):
            upstash_v2.publish_hash_snapshot_atomic(
                "ranks:trend:missevan:v2",
                {"version": 2, "entityCount": 1},
                {"100": {"version": 2, "id": "100", "samples": {}}},
                upstash=fake,
                expected_meta_raw=None,
            )

        self.assertEqual(fake.hashes["ranks:trend:missevan:v2"], {"old": "value"})
        self.assertFalse(
            any(command[:2] == ["EVAL", upstash_v2.HASH_ACTIVATE_WITH_META_SCRIPT] for command in fake.commands)
        )

    def test_hash_publish_rejects_the_reserved_meta_field(self) -> None:
        fake = FakeUpstash()

        with self.assertRaisesRegex(ValueError, "Reserved hash field"):
            upstash_v2.publish_hash_snapshot_atomic(
                "ranks:trend:missevan:v2",
                {"version": 2},
                {"__meta__": {"id": "invalid"}},
                upstash=fake,
                expected_meta_raw=None,
            )

        self.assertEqual(fake.commands, [])

    def test_cv_v2_combines_platforms_with_normalized_field_names(self) -> None:
        meta, fields = upstash_v2.build_cv_trend_v2({
            "missevan": {
                "updated_at": "2026-07-10",
                "dates": ["2026-07-10"],
                "cvs": {"CV  A": {"cvName": "CV  A", "samples": {"2026-07-10": {"metrics": {}}}}},
            },
            "manbo": {"updated_at": "2026-07-10", "dates": [], "cvs": {}},
        })

        self.assertIn("missevan:CV A", fields)
        self.assertEqual(meta["platforms"]["missevan"]["entityCount"], 1)

    def test_cv_v2_merges_names_that_normalize_to_the_same_hash_field(self) -> None:
        meta, fields = upstash_v2.build_cv_trend_v2({
            "missevan": {
                "dates": ["2026-07-03", "2026-07-10"],
                "cvs": {
                    "CV A": {"samples": {"2026-07-03": {"metrics": {"totalViewCount": 1}}}},
                    "CV  A": {"samples": {"2026-07-10": {"metrics": {"totalViewCount": 2}}}},
                },
            },
            "manbo": {"dates": [], "cvs": {}},
        })

        self.assertEqual(meta["platforms"]["missevan"]["entityCount"], 1)
        self.assertEqual(set(fields["missevan:CV A"]["samples"]), {"2026-07-03", "2026-07-10"})

    def test_normal_publish_merges_latest_sample_into_authoritative_v2(self) -> None:
        fake = FakeUpstash()
        key = "ranks:trend:missevan:v2"
        fake.hashes[key] = {
            "__meta__": upstash_v2.compact_json(
                {
                    "version": 2,
                    "platform": "missevan",
                    "updated_at": "old",
                    "dates": ["2026-07-10"],
                    "retentionDates": 45,
                }
            ),
            "100": upstash_v2.compact_json(
                {
                    "version": 2,
                    "id": "100",
                    "name": "人工标题",
                    "samples": {"2026-07-10": {"metrics": {"view_count": 10}}},
                }
            ),
        }
        legacy_payload = {
            "version": 1,
            "platform": "missevan",
            "updated_at": "2026-07-17",
            "dates": ["2026-07-10", "2026-07-17"],
            "dramas": {
                "100": {
                    "id": "100",
                    "name": "抓取标题",
                    "samples": {
                        "2026-07-10": {"metrics": {"view_count": 999}, "ranks": []},
                        "2026-07-17": {"metrics": {"view_count": 20}, "ranks": []},
                    },
                }
            },
        }
        upstash_v2.publish_normal_trend_v2("missevan", legacy_payload, upstash=fake)
        saved = json.loads(fake.hashes[key]["100"])
        self.assertEqual(saved["samples"]["2026-07-10"]["metrics"]["view_count"], 10)
        self.assertEqual(saved["samples"]["2026-07-17"]["metrics"]["view_count"], 20)


if __name__ == "__main__":
    unittest.main()
