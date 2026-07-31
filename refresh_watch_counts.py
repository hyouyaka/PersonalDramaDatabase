from __future__ import annotations

import argparse
import heapq
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import time

from archive_manager import (
    ARCHIVE_RETRY_DELAYS,
    ARCHIVE_SIGNALS,
    apply_local_archive_candidates,
    ensure_remote_archive_keys,
    load_local_archives,
    publish_archive_candidates,
)

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
from upstash_v2 import publish_info_v2


CACHE_WINDOW = timedelta(hours=1)
UTC = timezone.utc
MISSEVAN_BLOCKLIST = {"47639", "25812"}
INFO_PATCH_MAX_ATTEMPTS = 3


class MissevanRefreshInterrupted(RuntimeError):
    def __init__(self, message: str, stats: dict):
        super().__init__(message)
        self.stats = stats


def deepcopy_json(payload):
    return json.loads(json.dumps(payload, ensure_ascii=False))


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
        try:
            publish_info_v2(
                key,
                store,
                upstash=upstash,
                force=True,
                source_encoded=raw,
            )
        except RuntimeError as exc:
            if "concurrently changed" in str(exc):
                continue
            raise
        verified_raw = upstash(["GET", key])
        if not isinstance(verified_raw, str) or not verified_raw:
            raise RuntimeError(f"Unable to read back published info: {key}")
        save_json(path, json.loads(verified_raw))
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


def http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def archive_reason(
    platform: str,
    *,
    payload: dict | None = None,
    exc: Exception | None = None,
) -> str | None:
    signal = ARCHIVE_SIGNALS[platform]
    if exc is not None:
        if "httpStatus" not in signal:
            return None
        return signal["reason"] if http_status(exc) == signal["httpStatus"] else None
    if not isinstance(payload, dict) or "payloadCode" not in signal:
        return None
    try:
        response_code = int(payload.get("code"))
    except (TypeError, ValueError):
        return None
    if (
        response_code == signal["payloadCode"]
        and normalize(payload.get("msg")) == signal["payloadMessage"]
    ):
        return signal["reason"]
    return None


def run_archive_retry_queue(
    platform: str,
    items: list[object],
    *,
    drama_id_of,
    request_one,
    on_success,
    on_archive,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> dict[str, int]:
    retry_heap: list[tuple[float, int, int, object]] = []
    sequence = 0
    retry_requests = 0

    def enqueue(item: object, requests_done: int, reason: str) -> None:
        nonlocal sequence
        delay = ARCHIVE_RETRY_DELAYS[requests_done - 1]
        sequence += 1
        heapq.heappush(retry_heap, (monotonic() + delay, sequence, requests_done, item))
        print(
            f"[{platform}] ID={drama_id_of(item)} {reason}; "
            f"{int(delay)}秒后进行第{requests_done + 1}次请求"
        )

    for item in items:
        try:
            payload = request_one(item, 1)
        except Exception as exc:
            reason = archive_reason(platform, exc=exc)
            if reason is None:
                raise
            enqueue(item, 1, reason)
            continue
        reason = archive_reason(platform, payload=payload)
        if reason is not None:
            enqueue(item, 1, reason)
            continue
        on_success(item, payload)

    while retry_heap:
        due_at, _sequence, requests_done, item = heapq.heappop(retry_heap)
        remaining = due_at - monotonic()
        if remaining > 0:
            sleep(remaining)
        retry_requests += 1
        next_request_number = requests_done + 1
        try:
            payload = request_one(item, next_request_number)
        except Exception as exc:
            reason = archive_reason(platform, exc=exc)
            if reason is None:
                raise
            if next_request_number >= 4:
                on_archive(item, reason)
            else:
                enqueue(item, next_request_number, reason)
            continue
        reason = archive_reason(platform, payload=payload)
        if reason is not None:
            if next_request_number >= 4:
                on_archive(item, reason)
            else:
                enqueue(item, next_request_number, reason)
            continue
        on_success(item, payload)

    return {"retry_requests": retry_requests}


def refresh_missevan_watch_counts(*, target_ids: set[str] | None = None) -> dict:
    store = load_json(MISSEVAN_INFO_PATH, {})
    cache = load_cache(MISSEVAN_COUNTS_PATH)
    requester = MissevanRequester()
    processed = 0
    skipped = 0
    archived = 0
    retry_requests = 0
    pricing_skipped = 0
    info_observations: dict[str, dict[str, object]] = {}
    archive_candidates: dict[str, dict[str, str]] = {}
    now = datetime.now(UTC)

    def current_stats() -> dict:
        return {
            "processed": processed,
            "skipped": skipped,
            "archived": archived,
            "request_count": requester.request_count,
            "last_backoff_seconds": requester.last_backoff_seconds,
            "archive_retry_requests": retry_requests,
            "pricing_checked": processed - pricing_skipped,
            "pricing_skipped": pricing_skipped,
            "info_observations": info_observations,
            "archive_candidates": deepcopy_json(archive_candidates),
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

    target_drama_ids: list[str] = []
    for idx, drama_id in enumerate(drama_ids, start=1):
        cached = (cache.get("counts") or {}).get(drama_id) or {}
        if should_skip_recent(cached, now):
            print(f"[猫耳] 跳过 ID={drama_id} ({idx}/{len(drama_ids)})")
            skipped += 1
            continue
        target_drama_ids.append(drama_id)

    target_positions = {
        drama_id: idx for idx, drama_id in enumerate(target_drama_ids, start=1)
    }

    def request_one(drama_id: str, request_number: int) -> dict:
        print(
            f"[猫耳] 正在刷新 ID={drama_id} "
            f"(作品 {target_positions[drama_id]}/{len(target_drama_ids)}, 请求 {request_number}/4)"
        )
        return requester.request_json(f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}")

    def on_success(drama_id: str, payload: dict) -> None:
        nonlocal processed, pricing_skipped
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
        if processed % 20 == 0:
            save_cache(MISSEVAN_COUNTS_PATH, cache)

    def on_archive(drama_id: str, reason: str) -> None:
        nonlocal archived
        archive_candidates[drama_id] = {
            "archivedAt": utc_now(),
            "archivedReason": reason,
        }
        archived += 1
        contexts = drama_contexts.get(drama_id, [])
        title = normalize(contexts[0][2].get("title")) if contexts else ""
        print(f"[猫耳] 4次HTTP 403后归档 ID={drama_id} title={title}")

    def save_progress() -> None:
        if archive_candidates:
            apply_local_archive_candidates("missevan", store, cache, archive_candidates)
        save_cache(MISSEVAN_COUNTS_PATH, cache)
        save_missevan_store(MISSEVAN_INFO_PATH, store)

    try:
        retry_stats = run_archive_retry_queue(
            "missevan",
            target_drama_ids,
            drama_id_of=lambda drama_id: drama_id,
            request_one=request_one,
            on_success=on_success,
            on_archive=on_archive,
        )
        retry_requests = retry_stats["retry_requests"]
    except Exception as exc:
        save_progress()
        if isinstance(exc, RuntimeError) and "HTTP_418" in str(exc):
            raise MissevanRefreshInterrupted(str(exc), current_stats()) from exc
        print(
            "Failed while refreshing 猫耳 watch counts. Progress has been saved. "
            f"error={type(exc).__name__}: {exc}"
        )
        raise
    save_progress()
    return current_stats()


def refresh_manbo_watch_counts(*, target_ids: set[str] | None = None) -> dict:
    store = load_json(MANBO_INFO_PATH, {"records": []})
    cache = load_cache(MANBO_COUNTS_PATH)
    processed = 0
    skipped = 0
    archived = 0
    retry_requests = 0
    pricing_skipped = 0
    info_observations: dict[str, dict[str, object]] = {}
    archive_candidates: dict[str, dict[str, str]] = {}
    now = datetime.now(UTC)
    records = store.get("records", [])

    target_records = [record for record in records if str(record.get("dramaId") or "").strip() and (target_ids is None or str(record.get("dramaId") or "").strip() in target_ids)]
    queued_records: list[dict] = []
    for idx, record in enumerate(target_records, start=1):
        drama_id = str(record.get("dramaId") or "").strip()
        cached = (cache.get("counts") or {}).get(drama_id) or {}
        if should_skip_recent(cached, now):
            print(f"[漫播] 跳过 ID={drama_id} ({idx}/{len(target_records)})")
            skipped += 1
            continue
        queued_records.append(record)

    queued_positions = {
        str(record.get("dramaId") or "").strip(): idx
        for idx, record in enumerate(queued_records, start=1)
    }

    def request_one(record: dict, request_number: int) -> dict:
        drama_id = str(record.get("dramaId") or "").strip()
        print(
            f"[漫播] 正在刷新 ID={drama_id} "
            f"(作品 {queued_positions[drama_id]}/{len(queued_records)}, 请求 {request_number}/4)"
        )
        return request_manbo_json(
            f"https://www.kilamanbo.world/web_manbo/dramaDetail?dramaId={drama_id}"
        )

    def on_success(record: dict, payload: dict) -> None:
        nonlocal processed, pricing_skipped
        drama_id = str(record.get("dramaId") or "").strip()
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
        if processed % 50 == 0:
            save_cache(MANBO_COUNTS_PATH, cache)

    def on_archive(record: dict, reason: str) -> None:
        nonlocal archived
        drama_id = str(record.get("dramaId") or "").strip()
        archive_candidates[drama_id] = {
            "archivedAt": utc_now(),
            "archivedReason": reason,
        }
        archived += 1
        print(
            f"[漫播] 4次返回code=400、msg=作品已下架后归档 "
            f"ID={drama_id} title={normalize(record.get('name'))}"
        )

    def save_progress() -> None:
        if archive_candidates:
            apply_local_archive_candidates("manbo", store, cache, archive_candidates)
        save_cache(MANBO_COUNTS_PATH, cache)
        save_json(MANBO_INFO_PATH, store)

    try:
        retry_stats = run_archive_retry_queue(
            "manbo",
            queued_records,
            drama_id_of=lambda record: str(record.get("dramaId") or "").strip(),
            request_one=request_one,
            on_success=on_success,
            on_archive=on_archive,
        )
        retry_requests = retry_stats["retry_requests"]
    except Exception:
        save_progress()
        raise
    save_progress()
    return {
        "processed": processed,
        "skipped": skipped,
        "archived": archived,
        "archive_retry_requests": retry_requests,
        "pricing_checked": processed - pricing_skipped,
        "pricing_skipped": pricing_skipped,
        "info_observations": info_observations,
        "archive_candidates": deepcopy_json(archive_candidates),
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
    print("漫播 watch counts archived:", stats.get("archived", 0))
    print("漫播 archive retry requests:", stats.get("archive_retry_requests", 0))
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
        # The local cache already contains this run's fresh API results. The
        # archive transaction reads the older remote latest, so it must not
        # write that verified payload back over the local cache.
        archive_stats = publish_archive_candidates(
            platform,
            result.get("archive_candidates") or {},
            sync_local_watchcount=False,
        )
        print(f"{platform} archive published:", archive_stats.get("archived", 0))
        info_stats = publish_info_observations(platform, result.get("info_observations") or {})
        print_info_publish_stats(platform, info_stats)
        path = MISSEVAN_COUNTS_PATH if platform == "missevan" else MANBO_COUNTS_PATH
        _info_archive, watch_archive = load_local_archives(platform)
        upload_watchcount_file(
            platform,
            path,
            excluded_drama_ids=set(watch_archive.get("records") or {}),
        )


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

    selected_platforms = [
        platform
        for platform, enabled in (("missevan", do_missevan), ("manbo", do_manbo))
        if enabled
    ]
    for platform in selected_platforms:
        load_local_archives(platform)
        if not args.no_upload:
            ensure_remote_archive_keys(platform)

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
