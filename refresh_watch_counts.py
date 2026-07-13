from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from clean_manbo_pricing import MANBO_PRICING_EXCLUSIONS, classify_manbo_pricing
from platform_sync import (
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    MissevanRequester,
    all_sound_ids,
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
from sync_new_drama_ids import (
    MANBO_INFO_KEY,
    MISSEVAN_INFO_KEY,
    ROOT,
    download_info_file,
    load_env_file,
    sync_remote_watchcount_if_newer,
    upload_watchcount_file,
    upstash_request,
)


CACHE_WINDOW = timedelta(hours=1)
UTC = timezone.utc
MISSEVAN_BLOCKLIST = {"47639", "25812"}
MISSEVAN_ARCHIVED_INFO_PATH = MISSEVAN_INFO_PATH.with_name("missevan-archived-drama.json")
INFO_PATCH_MAX_ATTEMPTS = 3
INFO_COMPARE_AND_SET_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""


class MissevanRefreshInterrupted(RuntimeError):
    def __init__(self, message: str, stats: dict):
        super().__init__(message)
        self.stats = stats


def missevan_pricing_observation(drama: dict) -> tuple[dict[str, object], bool]:
    fields: dict[str, object] = {}
    complete = drama.get("pay_type") not in (None, "") and drama.get("price") not in (None, "")
    if complete:
        fields["needpay"] = int(drama.get("pay_type") or 0) != 0 and int(drama.get("price") or 0) > 0
    if drama.get("vip") not in (None, ""):
        fields["is_member"] = int(drama.get("vip") or 0) == 1
    return fields, complete


def manbo_pricing_observation(drama_id: str, payload: dict) -> tuple[dict[str, object], bool]:
    data = (payload or {}).get("data") or {}
    complete = (
        isinstance(data.get("setRespList"), list)
        and bool(data.get("setRespList"))
        and data.get("price") not in (None, "")
        and data.get("memberPrice") not in (None, "")
    )
    fields: dict[str, object] = {}
    if complete:
        category = classify_manbo_pricing(payload)
        fields["needpay"] = str(drama_id) in MANBO_PRICING_EXCLUSIONS or category not in {"free", "100_redbean"}
    if data.get("vipFree") not in (None, ""):
        fields["vipFree"] = int(data.get("vipFree") or 0)
    return fields, complete


def manbo_sound_ids(payload: dict) -> list[str]:
    sets = ((payload or {}).get("data") or {}).get("setRespList")
    if not isinstance(sets, list):
        return []
    out: list[str] = []
    for item in sets:
        if not isinstance(item, dict):
            continue
        for field in (
            "radioDramaSetIdStr",
            "radioDramaSetId",
            "dramaSetIdStr",
            "dramaSetId",
            "setId",
            "sound_id",
            "id",
        ):
            value = normalize(item.get(field))
            if value:
                if value not in out:
                    out.append(value)
                break
    return out


def _apply_info_observations(platform: str, store: dict, observations: dict[str, dict[str, object]]) -> dict[str, int]:
    stats = {
        "changed": 0,
        "free_to_paid": 0,
        "paid_to_free": 0,
        "membership_changed": 0,
        "sound_ids_changed": 0,
    }
    if platform == "missevan":
        records = {
            str(node.get("dramaId") or ""): node
            for _title, _season, node in iter_missevan_nodes(store or {})
            if str(node.get("dramaId") or "")
        }
    elif platform == "manbo":
        records = {
            str(record.get("dramaId") or ""): record
            for record in (store.get("records") or [])
            if isinstance(record, dict) and str(record.get("dramaId") or "")
        }
    else:
        raise ValueError(f"Unsupported platform: {platform}")
    for drama_id, fields in observations.items():
        record = records.get(str(drama_id))
        if record is None:
            continue
        record_changed = False
        for field, value in fields.items():
            previous = record.get(field)
            if previous == value:
                continue
            if field == "needpay":
                if previous is False and value is True:
                    stats["free_to_paid"] += 1
                elif previous is True and value is False:
                    stats["paid_to_free"] += 1
            elif field in {"is_member", "vipFree"}:
                stats["membership_changed"] += 1
            elif field == "soundIds":
                stats["sound_ids_changed"] += 1
            record[field] = value
            record_changed = True
        if record_changed:
            stats["changed"] += 1
    return stats


def publish_info_observations(
    platform: str,
    observations: dict[str, dict[str, object]],
    *,
    upstash=upstash_request,
    max_attempts: int = INFO_PATCH_MAX_ATTEMPTS,
) -> dict[str, int]:
    if not observations:
        return {
            "changed": 0,
            "free_to_paid": 0,
            "paid_to_free": 0,
            "membership_changed": 0,
            "sound_ids_changed": 0,
        }
    key = MISSEVAN_INFO_KEY if platform == "missevan" else MANBO_INFO_KEY
    path = MISSEVAN_INFO_PATH if platform == "missevan" else MANBO_INFO_PATH
    for _attempt in range(max_attempts):
        raw = upstash(["GET", key])
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Refusing to update info: {key} is empty or unsupported")
        store = json.loads(raw)
        stats = _apply_info_observations(platform, store, observations)
        encoded = json.dumps(store, ensure_ascii=False, separators=(",", ":"))
        result = upstash(
            ["EVAL", INFO_COMPARE_AND_SET_SCRIPT, 1, key, hashlib.sha1(raw.encode("utf-8")).hexdigest(), encoded]
        )
        if int(result or 0) == 1:
            save_json(path, store)
            return stats
    raise RuntimeError(f"Refusing to update info: {key} changed concurrently {max_attempts} times")


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
    pricing_skipped = 0
    info_observations: dict[str, dict[str, object]] = {}
    now = datetime.now(UTC)

    def current_stats() -> dict:
        return {
            "processed": processed,
            "skipped": skipped,
            "archived": archived,
            "request_count": requester.request_count,
            "last_backoff_seconds": requester.last_backoff_seconds,
            "pricing_checked": processed - pricing_skipped,
            "pricing_skipped": pricing_skipped,
            "info_observations": info_observations,
        }

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
        except RuntimeError as exc:
            save_missevan_store(MISSEVAN_INFO_PATH, store)
            save_json(MISSEVAN_ARCHIVED_INFO_PATH, archive)
            save_cache(MISSEVAN_COUNTS_PATH, cache)
            if "HTTP_418" in str(exc):
                raise MissevanRefreshInterrupted(str(exc), current_stats()) from exc
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
        pricing_fields, pricing_complete = missevan_pricing_observation(drama)
        info_fields = dict(pricing_fields)
        sound_ids = all_sound_ids(info)
        if sound_ids:
            info_fields["soundIds"] = sound_ids
        if info_fields:
            info_observations[drama_id] = info_fields
            for _series_title, _season_key, context_node in drama_contexts.get(drama_id, []):
                for field, value in info_fields.items():
                    context_node[field] = value
        if not pricing_complete:
            pricing_skipped += 1
        cache["counts"][drama_id] = {
            "name": normalize(drama.get("name")),
            "view_count": None if drama.get("view_count") is None else int(drama["view_count"]),
            "fetched_at": utc_now(),
        }
        processed += 1
        if processed % 20 == 0 or idx == len(drama_ids):
            save_cache(MISSEVAN_COUNTS_PATH, cache)

    save_cache(MISSEVAN_COUNTS_PATH, cache)
    save_missevan_store(MISSEVAN_INFO_PATH, store)
    return current_stats()


def refresh_manbo_watch_counts(*, target_ids: set[str] | None = None) -> dict:
    store = load_json(MANBO_INFO_PATH, {"records": []})
    cache = load_cache(MANBO_COUNTS_PATH)
    processed = 0
    skipped = 0
    pricing_skipped = 0
    info_observations: dict[str, dict[str, object]] = {}
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
        pricing_fields, pricing_complete = manbo_pricing_observation(drama_id, payload)
        info_fields = dict(pricing_fields)
        sound_ids = manbo_sound_ids(payload)
        if sound_ids:
            info_fields["soundIds"] = sound_ids
        if info_fields:
            info_observations[drama_id] = info_fields
            for field, value in info_fields.items():
                record[field] = value
        if not pricing_complete:
            pricing_skipped += 1
        cache["counts"][drama_id] = {
            "name": normalize(data.get("title") or record.get("name")),
            "view_count": None if data.get("watchCount") is None else int(data["watchCount"]),
            "fetched_at": utc_now(),
        }
        processed += 1
        if processed % 50 == 0 or idx == len(target_records):
            save_cache(MANBO_COUNTS_PATH, cache)

    save_cache(MANBO_COUNTS_PATH, cache)
    save_json(MANBO_INFO_PATH, store)
    return {
        "processed": processed,
        "skipped": skipped,
        "pricing_checked": processed - pricing_skipped,
        "pricing_skipped": pricing_skipped,
        "info_observations": info_observations,
    }


def print_missevan_stats(stats: dict) -> None:
    print("猫耳 watch counts processed:", stats["processed"])
    print("猫耳 watch counts skipped:", stats["skipped"])
    print("猫耳 watch counts archived:", stats["archived"])
    print("猫耳 requests:", stats["request_count"])
    print("猫耳 recent backoff seconds:", stats["last_backoff_seconds"])
    print("猫耳 pricing checked:", stats.get("pricing_checked", 0))
    print("猫耳 pricing skipped:", stats.get("pricing_skipped", 0))


def print_manbo_stats(stats: dict) -> None:
    print("漫播 watch counts processed:", stats["processed"])
    print("漫播 watch counts skipped:", stats["skipped"])
    print("漫播 pricing checked:", stats.get("pricing_checked", 0))
    print("漫播 pricing skipped:", stats.get("pricing_skipped", 0))


def print_info_publish_stats(platform: str, stats: dict) -> None:
    print(f"{platform} info changed:", stats.get("changed", 0))
    print(f"{platform} pricing free->paid:", stats.get("free_to_paid", 0))
    print(f"{platform} pricing paid->free:", stats.get("paid_to_free", 0))
    print(f"{platform} membership changed:", stats.get("membership_changed", 0))
    print(f"{platform} soundIds changed:", stats.get("sound_ids_changed", 0))


def publish_refresh_results(platforms: list[str] | tuple[str, ...], refresh_results: dict[str, dict]) -> None:
    for platform in platforms:
        result = refresh_results.get(platform)
        if result is None:
            continue
        info_stats = publish_info_observations(platform, result.get("info_observations") or {})
        print_info_publish_stats(platform, info_stats)
        path = MISSEVAN_COUNTS_PATH if platform == "missevan" else MANBO_COUNTS_PATH
        upload_watchcount_file(platform, path)


def run_missevan_refresh(target_ids: set[str] | None) -> dict:
    try:
        stats = refresh_missevan_watch_counts(target_ids=target_ids)
    except RuntimeError as exc:
        if "HTTP_418" not in str(exc):
            raise
        print("Hit 418 while refreshing 猫耳 watch counts. Progress has been saved.")
        raise
    print_missevan_stats(stats)
    return stats


def run_manbo_refresh(target_ids: set[str] | None) -> dict:
    stats = refresh_manbo_watch_counts(target_ids=target_ids)
    print_manbo_stats(stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("all", "missevan", "manbo"), default="all")
    parser.add_argument("--missevan", nargs="+", help="只刷新指定猫耳 dramaId，可传多个")
    parser.add_argument("--manbo", nargs="+", help="只刷新指定漫播 dramaId，可传多个")
    parser.add_argument("--force", action="store_true", help="刷新前无条件拉取远端 watchcount latest")
    parser.add_argument("--no-upload", action="store_true", help="刷新后不上传 watchcount 到 Upstash")
    args = parser.parse_args(argv)
    load_env_file(ROOT / ".env")

    missevan_ids = {item.strip() for item in (args.missevan or []) if item.strip()}
    manbo_ids = {item.strip() for item in (args.manbo or []) if item.strip()}
    explicit_target_mode = bool(missevan_ids or manbo_ids)
    do_missevan = bool(missevan_ids or (not explicit_target_mode and args.platform in ("all", "missevan")))
    do_manbo = bool(manbo_ids or (not explicit_target_mode and args.platform in ("all", "manbo")))
    refreshed_platforms: list[str] = []
    refresh_results: dict[str, dict] = {}

    if do_missevan:
        download_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH)
    if do_manbo:
        download_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH)

    if do_missevan:
        sync_remote_watchcount_if_newer("missevan", MISSEVAN_COUNTS_PATH, force=args.force)
    if do_manbo:
        sync_remote_watchcount_if_newer("manbo", MANBO_COUNTS_PATH, force=args.force)

    if do_missevan and do_manbo and not explicit_target_mode and args.platform == "all":
        missevan_interrupted = False
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(run_missevan_refresh, None): "missevan",
                executor.submit(run_manbo_refresh, None): "manbo",
            }
            for future in as_completed(futures):
                try:
                    refresh_results[futures[future]] = future.result()
                except MissevanRefreshInterrupted as exc:
                    refresh_results["missevan"] = exc.stats
                    missevan_interrupted = True
                except RuntimeError as exc:
                    if futures[future] == "missevan" and "HTTP_418" in str(exc):
                        missevan_interrupted = True
                        continue
                    raise
        if not args.no_upload:
            publish_refresh_results(("missevan", "manbo"), refresh_results)
        return 2 if missevan_interrupted else 0

    if do_missevan:
        try:
            refresh_results["missevan"] = run_missevan_refresh(missevan_ids or None)
        except MissevanRefreshInterrupted as exc:
            refresh_results["missevan"] = exc.stats
            if not args.no_upload:
                publish_refresh_results(("missevan",), refresh_results)
            return 2
        except RuntimeError as exc:
            if "HTTP_418" not in str(exc):
                raise
            return 2
        refreshed_platforms.append("missevan")

    if do_manbo:
        refresh_results["manbo"] = run_manbo_refresh(manbo_ids or None)
        refreshed_platforms.append("manbo")

    if not args.no_upload:
        publish_refresh_results(refreshed_platforms, refresh_results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
