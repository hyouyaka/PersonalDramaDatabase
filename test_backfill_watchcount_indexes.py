import json
import unittest
from datetime import datetime, timedelta

import backfill_watchcount_indexes
import sync_new_drama_ids


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.calls: list[list[object]] = []
        self.mutations: list[list[object]] = []

    def __call__(self, command: list[object]) -> object:
        self.calls.append(command)
        operation = command[0]
        if operation == "SCAN":
            prefix = str(command[command.index("MATCH") + 1]).removesuffix("????-??-??")
            keys = sorted(key for key in self.values if key.startswith(prefix) and len(key) == len(prefix) + 10)
            return ["0", keys]
        if operation == "GET":
            return self.values.get(str(command[1]))
        if operation == "MGET":
            return [self.values.get(str(key)) for key in command[1:]]
        if operation == "HGETALL":
            values = self.hashes.get(str(command[1]), {})
            return [item for field in sorted(values) for item in (field, values[field])]
        if operation == "HSET":
            self.mutations.append(command)
            target = self.hashes.setdefault(str(command[1]), {})
            for index in range(2, len(command), 2):
                target[str(command[index])] = str(command[index + 1])
            return len(command[2::2])
        if operation == "SET":
            self.mutations.append(command)
            self.values[str(command[1])] = str(command[2])
            return "OK"
        if operation == "HDEL":
            self.mutations.append(command)
            target = self.hashes.setdefault(str(command[1]), {})
            removed = 0
            for field in command[2:]:
                removed += field in target
                target.pop(str(field), None)
            return removed
        if operation == "DEL":
            self.mutations.append(command)
            removed = 0
            for key in command[1:]:
                removed += str(key) in self.values
                self.values.pop(str(key), None)
            return removed
        raise AssertionError(f"unsupported command: {command!r}")


def snapshot(date_text: str, counts: dict[str, dict]) -> str:
    return json.dumps({
        "_meta": {"updated_at": f"{date_text}T04:06:41+00:00"},
        "counts": counts,
    }, ensure_ascii=False, separators=(",", ":"))


def prepared_redis(platform: str) -> FakeRedis:
    redis = FakeRedis()
    dates = ["2026-06-19", "2026-06-26", "2026-07-03", "2026-07-10"]
    for index, date_text in enumerate(dates, start=1):
        redis.values[sync_new_drama_ids.watchcount_key(platform, date_text)] = snapshot(
            date_text,
            {
                "100": {"name": "测试剧", "view_count": index},
                "invalid": {"name": "坏数据", "view_count": "NaN"},
            },
        )
    redis.values[sync_new_drama_ids.watchcount_key(platform, "latest")] = redis.values[
        sync_new_drama_ids.watchcount_key(platform, "2026-07-10")
    ]
    return redis


class WatchcountBackfillTests(unittest.TestCase):
    def test_dry_run_reads_both_platforms_without_mutation(self) -> None:
        for platform in ("missevan", "manbo"):
            with self.subTest(platform=platform):
                redis = prepared_redis(platform)
                plan = backfill_watchcount_indexes.build_backfill_plan(platform, upstash=redis)
                backfill_watchcount_indexes.print_backfill_plan(plan, mode="dry-run")

                self.assertEqual(plan["index_payload"]["dates"], ["2026-06-19", "2026-06-26", "2026-07-03", "2026-07-10"])
                self.assertEqual(len(plan["history"]), 1)
                self.assertEqual(plan["history"]["100"]["points"][0], ["2026-06-19", 1])
                self.assertEqual(plan["history"]["100"]["points"][-1], ["2026-07-10", 4])
                self.assertFalse(redis.mutations)

    def test_apply_is_idempotent_for_both_platforms(self) -> None:
        for platform in ("missevan", "manbo"):
            with self.subTest(platform=platform):
                redis = prepared_redis(platform)
                first_plan = backfill_watchcount_indexes.build_backfill_plan(platform, upstash=redis)
                backfill_watchcount_indexes.apply_backfill_plan(first_plan, upstash=redis)
                first_history = dict(redis.hashes[sync_new_drama_ids.watchcount_key(platform, "history")])
                first_index = redis.values[sync_new_drama_ids.watchcount_key(platform, "index")]

                second_plan = backfill_watchcount_indexes.build_backfill_plan(platform, upstash=redis)
                backfill_watchcount_indexes.apply_backfill_plan(second_plan, upstash=redis)

                self.assertEqual(redis.hashes[sync_new_drama_ids.watchcount_key(platform, "history")], first_history)
                self.assertEqual(redis.values[sync_new_drama_ids.watchcount_key(platform, "index")], first_index)
                self.assertEqual(second_plan["delete_dates"], [])

    def test_apply_history_failure_does_not_write_index_or_delete(self) -> None:
        redis = prepared_redis("missevan")
        plan = backfill_watchcount_indexes.build_backfill_plan("missevan", upstash=redis)
        calls: list[list[object]] = []

        def failing_upstash(command: list[object]) -> object:
            calls.append(command)
            if command[0] == "HSET":
                return "NO"
            raise AssertionError(f"unexpected command after history failure: {command!r}")

        with self.assertRaisesRegex(RuntimeError, "history"):
            backfill_watchcount_indexes.apply_backfill_plan(plan, upstash=failing_upstash)
        self.assertEqual([command[0] for command in calls], ["HSET"])

    def test_apply_index_failure_does_not_delete(self) -> None:
        redis = prepared_redis("manbo")
        plan = backfill_watchcount_indexes.build_backfill_plan("manbo", upstash=redis)
        calls: list[list[object]] = []

        def failing_upstash(command: list[object]) -> object:
            calls.append(command)
            if command[0] == "HSET":
                return 1
            if command[0] == "SET":
                return "NO"
            raise AssertionError(f"unexpected command after index failure: {command!r}")

        with self.assertRaisesRegex(RuntimeError, "index"):
            backfill_watchcount_indexes.apply_backfill_plan(plan, upstash=failing_upstash)
        self.assertEqual([command[0] for command in calls], ["HSET", "SET"])

    def test_apply_index_failure_preserves_points_from_existing_index(self) -> None:
        redis = FakeRedis()
        dates = [(datetime(2026, 6, 1) + timedelta(days=index)).date().isoformat() for index in range(33)]
        for index, date_text in enumerate(dates):
            redis.values[sync_new_drama_ids.watchcount_key("manbo", date_text)] = snapshot(
                date_text,
                {"100": {"name": "剧", "view_count": index}},
            )
        redis.values[sync_new_drama_ids.watchcount_key("manbo", "latest")] = redis.values[
            sync_new_drama_ids.watchcount_key("manbo", dates[-1])
        ]
        redis.values[sync_new_drama_ids.watchcount_key("manbo", "index")] = json.dumps({
            "version": 1,
            "platform": "manbo",
            "updated_at": f"{dates[-2]}T04:06:41Z",
            "dates": dates[:-1],
        })
        plan = backfill_watchcount_indexes.build_backfill_plan("manbo", upstash=redis)
        calls: list[list[object]] = []

        def failing_upstash(command: list[object]) -> object:
            calls.append(command)
            return 1 if command[0] == "HSET" else "NO"

        with self.assertRaisesRegex(RuntimeError, "index"):
            backfill_watchcount_indexes.apply_backfill_plan(plan, upstash=failing_upstash)

        staged_history = json.loads(calls[0][3])
        self.assertEqual(len(staged_history["points"]), 33)
        self.assertEqual(staged_history["points"][0][0], dates[0])
        self.assertEqual(staged_history["points"][-1][0], dates[-1])
        self.assertEqual([command[0] for command in calls], ["HSET", "SET"])

    def test_hash_value_keeps_zero_and_ignores_invalid_numbers(self) -> None:
        history = sync_new_drama_ids.build_watchcount_history("missevan", {
            "2026-07-10": json.loads(snapshot("2026-07-10", {
                "100": {"name": "零播放", "view_count": 0},
                "200": {"name": "坏数据", "view_count": "not-a-number"},
                "300": {"name": "坏数据", "view_count": float("nan")},
                "not-a-drama-id": {"name": "坏 ID", "view_count": 1},
            })),
        })
        self.assertEqual(history, {"100": {"name": "零播放", "points": [["2026-07-10", 0]]}})

    def test_history_rejects_non_drama_id_hash_fields(self) -> None:
        raw = ["not-a-drama-id", json.dumps({"name": "坏 ID", "points": [["2026-07-10", 1]]})]
        with self.assertRaisesRegex(RuntimeError, "not a dramaId"):
            sync_new_drama_ids.decode_watchcount_history("missevan", raw)

    def test_current_empty_name_falls_back_to_existing_name_after_old_points_expire(self) -> None:
        merged = sync_new_drama_ids.merge_watchcount_history(
            {"100": {"name": "旧名称", "points": [["2026-06-19", 1]]}},
            {"counts": {"100": {"name": "", "view_count": 2}}},
            "2026-07-10",
            ["2026-07-10"],
        )
        self.assertEqual(merged, {"100": {"name": "旧名称", "points": [["2026-07-10", 2]]}})

    def test_history_points_are_capped_at_32_dates(self) -> None:
        dates = [(datetime(2026, 6, 1) + timedelta(days=index)).date().isoformat() for index in range(33)]
        snapshots = {
            date_text: json.loads(snapshot(date_text, {"100": {"name": "剧", "view_count": index}}))
            for index, date_text in enumerate(dates)
        }
        history = sync_new_drama_ids.build_watchcount_history("manbo", snapshots)

        self.assertEqual(len(history["100"]["points"]), 32)
        self.assertEqual(history["100"]["points"][0][0], dates[1])
        self.assertEqual(history["100"]["points"][-1][0], dates[-1])

    def test_delete_failure_is_recoverable_on_next_apply(self) -> None:
        redis = FakeRedis()
        dates = [(datetime(2026, 6, 1) + timedelta(days=index)).date().isoformat() for index in range(33)]
        for index, date_text in enumerate(dates):
            redis.values[sync_new_drama_ids.watchcount_key("missevan", date_text)] = snapshot(
                date_text,
                {"100": {"name": "剧", "view_count": index}},
            )
        redis.values[sync_new_drama_ids.watchcount_key("missevan", "latest")] = redis.values[
            sync_new_drama_ids.watchcount_key("missevan", dates[-1])
        ]
        plan = backfill_watchcount_indexes.build_backfill_plan("missevan", upstash=redis)
        original_call = redis
        failed = False

        def fail_once(command: list[object]) -> object:
            nonlocal failed
            if command[0] == "DEL" and not failed:
                failed = True
                return None
            return original_call(command)

        with self.assertRaisesRegex(RuntimeError, "delete"):
            backfill_watchcount_indexes.apply_backfill_plan(plan, upstash=fail_once)
        retry_plan = backfill_watchcount_indexes.build_backfill_plan("missevan", upstash=redis)
        backfill_watchcount_indexes.apply_backfill_plan(retry_plan, upstash=redis)

        self.assertNotIn(sync_new_drama_ids.watchcount_key("missevan", dates[0]), redis.values)
        self.assertEqual(retry_plan["delete_dates"], [dates[0]])


if __name__ == "__main__":
    unittest.main()
