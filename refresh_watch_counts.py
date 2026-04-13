from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from platform_sync import (
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    MissevanRequester,
    iter_missevan_nodes,
    load_cache,
    load_json,
    normalize,
    remove_missevan_node as remove_missevan_store_node,
    request_manbo_json,
    save_cache,
    save_json,
    save_missevan_store,
    utc_now,
)


CACHE_WINDOW = timedelta(hours=1)
UTC = timezone.utc
MISSEVAN_BLOCKLIST = {"47639", "25812"}
MISSEVAN_ARCHIVED_INFO_PATH = MISSEVAN_INFO_PATH.with_name("missevan-archived-drama.json")


def parse_iso_datetime(value: object) -> datetime | None:
    text = normalize(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def should_skip_recent(cache_entry: dict, now: datetime) -> bool:
    fetched_at = parse_iso_datetime((cache_entry or {}).get("fetched_at"))
    if fetched_at is None:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return now - fetched_at < CACHE_WINDOW


def is_http_403(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 403


def archive_missevan_node(archive: dict, series_title: str, season_key: str, node: dict, watch_count: dict | None) -> None:
    archived_node = deepcopy(node)
    archived_node["archivedReason"] = "HTTP_403"
    archived_node["archivedAt"] = utc_now()
    archived_node["archivedWatchCount"] = deepcopy(watch_count)
    archive.setdefault(series_title, {})[season_key] = archived_node


def remove_missevan_node(store: dict, series_title: str, season_key: str) -> None:
    remove_missevan_store_node(store, series_title, season_key)


def archive_missevan_contexts(store: dict, archive: dict, cache: dict, contexts: list[tuple[str, str, dict]], drama_id: str) -> int:
    watch_count = (cache.get("counts") or {}).get(drama_id)
    archived = 0
    for series_title, season_key, node in contexts:
        archive_missevan_node(archive, series_title, season_key, node, watch_count)
        remove_missevan_node(store, series_title, season_key)
        archived += 1
        print(f"[猫耳] 403归档 ID={drama_id} {season_key} title={normalize(node.get('title') or series_title)}")
    cache.get("counts", {}).pop(drama_id, None)
    return archived


def refresh_missevan_watch_counts(*, target_ids: set[str] | None = None) -> dict:
    store = load_json(MISSEVAN_INFO_PATH, {})
    archive = load_json(MISSEVAN_ARCHIVED_INFO_PATH, {})
    cache = load_cache(MISSEVAN_COUNTS_PATH)
    requester = MissevanRequester()
    processed = 0
    skipped = 0
    archived = 0
    now = datetime.now(UTC)

    drama_ids: list[str] = []
    drama_contexts: dict[str, list[tuple[str, str, dict]]] = {}
    for series_title, season_key, node in iter_missevan_nodes(store):
        drama_id = str(node.get("dramaId") or "").strip()
        if not drama_id or drama_id in MISSEVAN_BLOCKLIST:
            continue
        if target_ids is not None and drama_id not in target_ids:
            continue
        drama_contexts.setdefault(drama_id, []).append((series_title, season_key, node))
        if drama_id not in drama_ids:
            drama_ids.append(drama_id)

    for idx, drama_id in enumerate(drama_ids, start=1):
        cached = (cache.get("counts") or {}).get(drama_id) or {}
        if should_skip_recent(cached, now):
            print(f"[猫耳] 跳过 ID={drama_id} ({idx}/{len(drama_ids)})")
            skipped += 1
            continue
        print(f"[猫耳] 正在刷新 ID={drama_id} ({idx}/{len(drama_ids)})")
        try:
            payload = requester.request_json(f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}")
        except RuntimeError:
            save_missevan_store(MISSEVAN_INFO_PATH, store)
            save_json(MISSEVAN_ARCHIVED_INFO_PATH, archive)
            save_cache(MISSEVAN_COUNTS_PATH, cache)
            raise
        except Exception as exc:
            if is_http_403(exc):
                archived += archive_missevan_contexts(store, archive, cache, drama_contexts.get(drama_id, []), drama_id)
                save_missevan_store(MISSEVAN_INFO_PATH, store)
                save_json(MISSEVAN_ARCHIVED_INFO_PATH, archive)
                save_cache(MISSEVAN_COUNTS_PATH, cache)
                continue
            save_missevan_store(MISSEVAN_INFO_PATH, store)
            save_json(MISSEVAN_ARCHIVED_INFO_PATH, archive)
            save_cache(MISSEVAN_COUNTS_PATH, cache)
            print(f"Failed while refreshing 猫耳 watch counts. Progress has been saved. dramaId={drama_id} error={type(exc).__name__}: {exc}")
            raise
        info = (payload or {}).get("info") or {}
        drama = info.get("drama") or {}
        cache["counts"][drama_id] = {
            "name": normalize(drama.get("name")),
            "view_count": None if drama.get("view_count") is None else int(drama["view_count"]),
            "fetched_at": utc_now(),
        }
        processed += 1
        if processed % 20 == 0 or idx == len(drama_ids):
            save_cache(MISSEVAN_COUNTS_PATH, cache)

    save_cache(MISSEVAN_COUNTS_PATH, cache)
    return {
        "processed": processed,
        "skipped": skipped,
        "archived": archived,
        "request_count": requester.request_count,
        "last_backoff_seconds": requester.last_backoff_seconds,
    }


def refresh_manbo_watch_counts(*, target_ids: set[str] | None = None) -> dict:
    store = load_json(MANBO_INFO_PATH, {"records": []})
    cache = load_cache(MANBO_COUNTS_PATH)
    processed = 0
    skipped = 0
    now = datetime.now(UTC)
    records = store.get("records", [])

    target_records = [record for record in records if str(record.get("dramaId") or "").strip() and (target_ids is None or str(record.get("dramaId") or "").strip() in target_ids)]
    for idx, record in enumerate(target_records, start=1):
        drama_id = str(record.get("dramaId") or "").strip()
        cached = (cache.get("counts") or {}).get(drama_id) or {}
        if should_skip_recent(cached, now):
            print(f"[漫播] 跳过 ID={drama_id} ({idx}/{len(target_records)})")
            skipped += 1
            continue
        print(f"[漫播] 正在刷新 ID={drama_id} ({idx}/{len(target_records)})")
        payload = request_manbo_json(f"https://www.kilamanbo.world/web_manbo/dramaDetail?dramaId={drama_id}")
        data = payload.get("data") or {}
        cache["counts"][drama_id] = {
            "name": normalize(data.get("title") or record.get("name")),
            "view_count": None if data.get("watchCount") is None else int(data["watchCount"]),
            "fetched_at": utc_now(),
        }
        processed += 1
        if processed % 50 == 0 or idx == len(target_records):
            save_cache(MANBO_COUNTS_PATH, cache)

    save_cache(MANBO_COUNTS_PATH, cache)
    return {"processed": processed, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("all", "missevan", "manbo"), default="all")
    parser.add_argument("--missevan", nargs="+", help="只刷新指定猫耳 dramaId，可传多个")
    parser.add_argument("--manbo", nargs="+", help="只刷新指定漫播 dramaId，可传多个")
    args = parser.parse_args()

    missevan_ids = {item.strip() for item in (args.missevan or []) if item.strip()}
    manbo_ids = {item.strip() for item in (args.manbo or []) if item.strip()}
    explicit_target_mode = bool(missevan_ids or manbo_ids)

    if missevan_ids or (not explicit_target_mode and args.platform in ("all", "missevan")):
        try:
            missevan_stats = refresh_missevan_watch_counts(target_ids=missevan_ids or None)
        except RuntimeError:
            print("Hit 418 while refreshing 猫耳 watch counts. Progress has been saved.")
            return 2
        print("猫耳 watch counts processed:", missevan_stats["processed"])
        print("猫耳 watch counts skipped:", missevan_stats["skipped"])
        print("猫耳 watch counts archived:", missevan_stats["archived"])
        print("猫耳 requests:", missevan_stats["request_count"])
        print("猫耳 recent backoff seconds:", missevan_stats["last_backoff_seconds"])

    if manbo_ids or (not explicit_target_mode and args.platform in ("all", "manbo")):
        manbo_stats = refresh_manbo_watch_counts(target_ids=manbo_ids or None)
        print("漫播 watch counts processed:", manbo_stats["processed"])
        print("漫播 watch counts skipped:", manbo_stats["skipped"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
