from __future__ import annotations

import argparse
import json
from datetime import timezone

from sync_new_drama_ids import (
    ROOT,
    WATCHCOUNT_INDEX_VERSION,
    WATCHCOUNT_MAX_DATES,
    _assert_delete_succeeded,
    _assert_hash_write_succeeded,
    _watchcount_history_fields,
    _load_watchcount_snapshots_by_dates,
    build_watchcount_history,
    decode_remote_watchcount_payload,
    encode_watchcount_history,
    filter_watchcount_history,
    load_env_file,
    read_watchcount_index,
    scan_watchcount_snapshot_dates,
    upstash_request,
    watchcount_key,
    watchcount_updated_at,
)


def build_backfill_plan(platform: str, *, upstash=upstash_request) -> dict:
    all_dates = scan_watchcount_snapshot_dates(platform, upstash=upstash, use_cache=False)
    if not all_dates:
        raise RuntimeError(f"No dated watchcount snapshots found for {platform}.")

    latest_key = watchcount_key(platform, "latest")
    latest_raw = upstash(["GET", latest_key])
    latest = decode_remote_watchcount_payload(latest_key, latest_raw)
    updated_at = watchcount_updated_at(latest)
    if updated_at is None:
        raise RuntimeError(f"Refusing to backfill {platform}: {latest_key} has no valid _meta.updated_at.")

    retained_dates = all_dates[-WATCHCOUNT_MAX_DATES:]
    existing_index = read_watchcount_index(platform, upstash=upstash)
    staging_dates = (
        sorted(set(existing_index["dates"]) | set(retained_dates))
        if existing_index is not None
        else retained_dates
    )
    snapshots = _load_watchcount_snapshots_by_dates(platform, staging_dates, upstash=upstash)
    staged_history = build_watchcount_history(platform, snapshots, max_points=None)
    history = filter_watchcount_history(staged_history, retained_dates)
    history_key = watchcount_key(platform, "history")
    raw_history = upstash(["HGETALL", history_key])
    existing_history_fields = _watchcount_history_fields(raw_history, key=history_key)
    stale_history_fields = sorted((existing_history_fields | set(staged_history)) - set(history))
    index_payload = {
        "version": WATCHCOUNT_INDEX_VERSION,
        "platform": platform,
        "updated_at": updated_at.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "dates": retained_dates,
    }
    history_args = encode_watchcount_history(history)
    return {
        "platform": platform,
        "history_key": history_key,
        "staged_history": staged_history,
        "staged_history_args": encode_watchcount_history(staged_history),
        "history": history,
        "history_args": history_args,
        "history_payload_bytes": sum(len(str(value).encode("utf-8")) for value in history_args[1::2]),
        "index_key": watchcount_key(platform, "index"),
        "index_payload": index_payload,
        "index_encoded": json.dumps(index_payload, ensure_ascii=False, separators=(",", ":")),
        "stale_history_fields": stale_history_fields,
        "delete_dates": sorted(set(all_dates) - set(retained_dates)),
    }


def apply_backfill_plan(plan: dict, *, upstash=upstash_request) -> None:
    history_key = plan["history_key"]
    staged_history_args = plan["staged_history_args"]
    if staged_history_args:
        result = upstash(["HSET", history_key, *staged_history_args])
        _assert_hash_write_succeeded("write history hash", history_key, result)
    result = upstash(["SET", plan["index_key"], plan["index_encoded"]])
    if result != "OK":
        raise RuntimeError(f"Failed to write {plan['index_key']}: {result!r}")

    history_args = plan["history_args"]
    if plan["staged_history"] != plan["history"] and history_args:
        result = upstash(["HSET", history_key, *history_args])
        _assert_hash_write_succeeded("trim history hash", history_key, result)

    stale_fields = plan["stale_history_fields"]
    if stale_fields:
        result = upstash(["HDEL", history_key, *stale_fields])
        _assert_hash_write_succeeded("clean history hash", history_key, result)

    delete_dates = plan["delete_dates"]
    if delete_dates:
        delete_keys = [watchcount_key(plan["platform"], date_text) for date_text in delete_dates]
        result = upstash(["DEL", *delete_keys])
        _assert_delete_succeeded("delete old watchcount snapshots", delete_keys, result)


def print_backfill_plan(plan: dict, *, mode: str) -> None:
    platform = plan["platform"]
    dates = plan["index_payload"]["dates"]
    delete_dates = plan["delete_dates"]
    print(f"[{mode}] {platform}: dates={','.join(dates)}")
    print(f"[{mode}] {platform}: dramas={len(plan['history'])}")
    print(f"[{mode}] {platform}: history_payload_bytes={plan['history_payload_bytes']}")
    print(f"[{mode}] {platform}: delete_keys={len(delete_dates)}")
    for date_text in delete_dates:
        print(f"[{mode}] delete {watchcount_key(platform, date_text)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill watchcount index and Redis Hash history from existing snapshots.")
    parser.add_argument("--platform", choices=("all", "missevan", "manbo"), default="all")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只读取并输出回填计划")
    mode.add_argument("--apply", action="store_true", help="写入 history/index 并删除淘汰快照")
    args = parser.parse_args(argv)
    load_env_file(ROOT / ".env")

    platforms = ("missevan", "manbo") if args.platform == "all" else (args.platform,)
    for platform in platforms:
        plan = build_backfill_plan(platform)
        if args.dry_run:
            print_backfill_plan(plan, mode="dry-run")
            continue
        apply_backfill_plan(plan)
        print_backfill_plan(plan, mode="apply")
        print(f"[ok] {platform}: backfill complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
