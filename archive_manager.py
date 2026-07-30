from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from platform_sync import (
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    iter_missevan_nodes,
    load_json,
    normalize,
    remove_missevan_node,
    save_json,
    utc_now,
)
from sync_new_drama_ids import (
    MANBO_INFO_KEY,
    MISSEVAN_INFO_KEY,
    backup_local_json_file,
    decode_remote_info_payload,
    decode_remote_watchcount_payload,
    upstash_request,
    watchcount_key,
)
from upstash_v2 import build_info_v2_meta, compact_json, string_cas_token


ARCHIVE_VERSION = 1
ARCHIVE_SIGNALS = {
    "missevan": {
        "reason": "HTTP_403",
        "httpStatus": 403,
    },
    "manbo": {
        "reason": "MANBO_CODE_400_作品已下架",
        "payloadCode": 400,
        "payloadMessage": "作品已下架",
    },
}
ARCHIVE_RETRY_DELAYS = (30.0, 60.0, 120.0)
ARCHIVE_PUBLISH_MAX_ATTEMPTS = 3

ARCHIVE_INFO_KEYS = {
    "missevan": "missevan:info:archive:v1",
    "manbo": "manbo:info:archive:v1",
}
ARCHIVE_WATCHCOUNT_KEYS = {
    "missevan": "missevan:watchcount:archive:v1",
    "manbo": "manbo:watchcount:archive:v1",
}
ARCHIVE_INFO_PATHS = {
    "missevan": MISSEVAN_INFO_PATH.with_name("missevan-archived-drama.json"),
    "manbo": MANBO_INFO_PATH.with_name("manbo-archived-drama.json"),
}
ARCHIVE_WATCHCOUNT_PATHS = {
    "missevan": MISSEVAN_COUNTS_PATH.with_name("missevan-archived-watch-counts.json"),
    "manbo": MANBO_COUNTS_PATH.with_name("manbo-archived-watch-counts.json"),
}
ACTIVE_INFO_KEYS = {"missevan": MISSEVAN_INFO_KEY, "manbo": MANBO_INFO_KEY}
ACTIVE_INFO_PATHS = {"missevan": MISSEVAN_INFO_PATH, "manbo": MANBO_INFO_PATH}
ACTIVE_WATCHCOUNT_PATHS = {
    "missevan": MISSEVAN_COUNTS_PATH,
    "manbo": MANBO_COUNTS_PATH,
}

ARCHIVE_MERGE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '__missing__' then
  if current and current ~= false then return 0 end
elseif not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""

ARCHIVE_MOVE_SCRIPT = """
for index = 1, 4 do
  local current = redis.call('GET', KEYS[index])
  local expected = ARGV[index]
  if expected == '__missing__' then
    if current and current ~= false then return 0 end
  elseif not current or redis.sha1hex(current) ~= expected then
    return 0
  end
end
local id_count = tonumber(ARGV[9])
for index = 1, id_count do
  local current = redis.call('HGET', KEYS[7], ARGV[9 + index])
  local expected = ARGV[9 + id_count + index]
  if expected == '__missing__' then
    if current and current ~= false then return 0 end
  elseif not current or current ~= expected then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[5])
redis.call('SET', KEYS[5], ARGV[6])
if redis.call('EXISTS', KEYS[6]) == 1 then
  redis.call('SET', KEYS[6], ARGV[5])
end
redis.call('SET', KEYS[2], ARGV[7])
redis.call('SET', KEYS[3], ARGV[8])
redis.call('SET', KEYS[4], ARGV[9 + id_count * 2 + 1])
for index = 1, id_count do
  redis.call('HDEL', KEYS[7], ARGV[9 + index])
end
return 1
"""


def _empty_archive(platform: str) -> dict:
    return {
        "version": ARCHIVE_VERSION,
        "platform": platform,
        "updatedAt": utc_now(),
        "records": {},
    }


def _validate_platform(platform: str) -> None:
    if platform not in ARCHIVE_SIGNALS:
        raise ValueError(f"Unsupported platform: {platform}")


def _normalize_archive(payload: object, platform: str, *, key: str) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to use {key}: expected a JSON object")
    if payload.get("version") != ARCHIVE_VERSION:
        raise RuntimeError(f"Refusing to use {key}: unsupported version {payload.get('version')!r}")
    if payload.get("platform") != platform:
        raise RuntimeError(f"Refusing to use {key}: platform mismatch")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise RuntimeError(f"Refusing to use {key}: records must be an object")
    normalized = deepcopy(payload)
    normalized["records"] = {str(drama_id): deepcopy(record) for drama_id, record in records.items()}
    normalized.setdefault("updatedAt", None)
    return normalized


def decode_archive_payload(raw: object, platform: str, *, key: str) -> dict:
    if raw in (None, ""):
        return _empty_archive(platform)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to use {key}: invalid JSON: {exc}") from exc
    return _normalize_archive(payload, platform, key=key)


def _legacy_missevan_entries(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    entries: list[dict] = []
    for value in payload.values():
        if isinstance(value, dict) and value.get("dramaId") not in (None, ""):
            entries.append(value)
            continue
        if not isinstance(value, dict):
            continue
        entries.extend(
            node
            for node in value.values()
            if isinstance(node, dict) and node.get("dramaId") not in (None, "")
        )
    return entries


def load_local_archives(platform: str) -> tuple[dict, dict]:
    _validate_platform(platform)
    info_path = ARCHIVE_INFO_PATHS[platform]
    watch_path = ARCHIVE_WATCHCOUNT_PATHS[platform]
    raw_info = load_json(info_path, _empty_archive(platform))
    raw_watch = load_json(watch_path, _empty_archive(platform))

    if (
        isinstance(raw_info, dict)
        and raw_info.get("version") == ARCHIVE_VERSION
        and raw_info.get("platform") == platform
    ):
        info_archive = _normalize_archive(raw_info, platform, key=str(info_path))
    elif platform == "missevan":
        backup_local_json_file(info_path)
        info_archive = _empty_archive(platform)
        for node in _legacy_missevan_entries(raw_info):
            drama_id = normalize(node.get("dramaId"))
            if not drama_id:
                continue
            archived_at = normalize(node.get("archivedAt")) or utc_now()
            reason = normalize(node.get("archivedReason")) or "HTTP_403"
            record = deepcopy(node)
            latest = record.pop("archivedWatchCount", None)
            record.pop("archivedAt", None)
            record.pop("archivedReason", None)
            info_archive["records"][drama_id] = {
                "archivedAt": archived_at,
                "archivedReason": reason,
                "record": record,
            }
            if latest is not None:
                raw_watch = (
                    raw_watch
                    if isinstance(raw_watch, dict)
                    and raw_watch.get("version") == ARCHIVE_VERSION
                    else _empty_archive(platform)
                )
                raw_watch.setdefault("records", {})[drama_id] = {
                    "archivedAt": archived_at,
                    "archivedReason": reason,
                    "latest": deepcopy(latest),
                    "history": None,
                }
        info_archive["updatedAt"] = utc_now()
    elif raw_info in ({}, None):
        info_archive = _empty_archive(platform)
    else:
        raise RuntimeError(f"Refusing to migrate unsupported archive format: {info_path}")

    if (
        isinstance(raw_watch, dict)
        and raw_watch.get("version") == ARCHIVE_VERSION
        and raw_watch.get("platform") == platform
    ):
        watch_archive = _normalize_archive(raw_watch, platform, key=str(watch_path))
    elif raw_watch in ({}, None):
        watch_archive = _empty_archive(platform)
    else:
        raise RuntimeError(f"Refusing to migrate unsupported archive format: {watch_path}")

    save_local_archives(platform, info_archive, watch_archive)
    return info_archive, watch_archive


def save_local_archives(platform: str, info_archive: dict, watch_archive: dict) -> None:
    save_json(ARCHIVE_INFO_PATHS[platform], info_archive)
    save_json(ARCHIVE_WATCHCOUNT_PATHS[platform], watch_archive)


def _find_missevan_records(store: dict, drama_id: str) -> list[tuple[str, str, dict]]:
    return [
        (outer_key, season_key, node)
        for outer_key, season_key, node in iter_missevan_nodes(store or {})
        if normalize(node.get("dramaId")) == drama_id
    ]


def _pop_active_info_record(platform: str, store: dict, drama_id: str) -> dict | None:
    if platform == "missevan":
        contexts = _find_missevan_records(store, drama_id)
        if not contexts:
            return None
        record = deepcopy(contexts[0][2])
        for outer_key, season_key, _node in contexts:
            remove_missevan_node(store, outer_key, season_key)
        return record

    records = store.get("records")
    if not isinstance(records, list):
        return None
    found = None
    retained = []
    for record in records:
        if isinstance(record, dict) and normalize(record.get("dramaId")) == drama_id:
            if found is None:
                found = deepcopy(record)
            continue
        retained.append(record)
    store["records"] = retained
    return found


def apply_local_archive_candidates(
    platform: str,
    store: dict,
    cache: dict,
    candidates: dict[str, dict[str, str]],
) -> tuple[dict, dict]:
    info_archive, watch_archive = load_local_archives(platform)
    for drama_id, metadata in candidates.items():
        archived_at = metadata["archivedAt"]
        reason = metadata["archivedReason"]
        record = _pop_active_info_record(platform, store, drama_id)
        if record is not None and drama_id not in info_archive["records"]:
            info_archive["records"][drama_id] = {
                "archivedAt": archived_at,
                "archivedReason": reason,
                "record": record,
            }
        latest = (cache.get("counts") or {}).pop(drama_id, None)
        existing_watch = watch_archive["records"].get(drama_id)
        if latest is not None or existing_watch is None:
            watch_archive["records"][drama_id] = {
                "archivedAt": archived_at,
                "archivedReason": reason,
                "latest": deepcopy(latest),
                "history": None if existing_watch is None else existing_watch.get("history"),
            }
    if candidates:
        stamp = utc_now()
        info_archive["updatedAt"] = stamp
        watch_archive["updatedAt"] = stamp
    save_local_archives(platform, info_archive, watch_archive)
    return info_archive, watch_archive


def ensure_remote_archive_keys(
    platform: str,
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    max_attempts: int = ARCHIVE_PUBLISH_MAX_ATTEMPTS,
) -> tuple[dict, dict]:
    local_info, local_watch = load_local_archives(platform)
    results = []
    for key, path, local in (
        (ARCHIVE_INFO_KEYS[platform], ARCHIVE_INFO_PATHS[platform], local_info),
        (ARCHIVE_WATCHCOUNT_KEYS[platform], ARCHIVE_WATCHCOUNT_PATHS[platform], local_watch),
    ):
        for _attempt in range(max_attempts):
            raw = upstash(["GET", key])
            remote = decode_archive_payload(raw, platform, key=key)
            if raw not in (None, ""):
                verified = remote
                break
            encoded = compact_json(local)
            result = upstash(
                [
                    "EVAL",
                    ARCHIVE_MERGE_SCRIPT,
                    1,
                    key,
                    string_cas_token(raw),
                    encoded,
                ]
            )
            if int(result or 0) == 1:
                verified = decode_archive_payload(upstash(["GET", key]), platform, key=key)
                break
        else:
            raise RuntimeError(f"Concurrent updates prevented initializing {key}")
        save_json(path, verified)
        results.append(verified)
    return results[0], results[1]


def _history_raw_values(
    platform: str,
    drama_ids: list[str],
    *,
    upstash: Callable[[list[object]], object],
) -> list[object]:
    if not drama_ids:
        return []
    raw = upstash(["HMGET", watchcount_key(platform, "history"), *drama_ids])
    if not isinstance(raw, list) or len(raw) != len(drama_ids):
        raise RuntimeError(f"Invalid HMGET response for {platform} watchcount history")
    return raw


def _decoded_history(raw: object, *, platform: str, drama_id: str) -> dict | None:
    if raw in (None, ""):
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid history JSON for {platform}:{drama_id}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("points"), list):
        raise RuntimeError(f"Invalid history payload for {platform}:{drama_id}")
    return payload


def publish_archive_candidates(
    platform: str,
    candidates: dict[str, dict[str, str]],
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    max_attempts: int = ARCHIVE_PUBLISH_MAX_ATTEMPTS,
) -> dict[str, int]:
    if not candidates:
        return {"archived": 0}
    _validate_platform(platform)
    drama_ids = sorted(candidates)
    active_key = ACTIVE_INFO_KEYS[platform]
    info_archive_key = ARCHIVE_INFO_KEYS[platform]
    latest_key = watchcount_key(platform, "latest")
    watch_archive_key = ARCHIVE_WATCHCOUNT_KEYS[platform]
    meta_key = f"{platform}:info:meta:v2"
    legacy_key = f"{platform}:info:v1"
    history_key = watchcount_key(platform, "history")

    for _attempt in range(max_attempts):
        raw_active = upstash(["GET", active_key])
        raw_info_archive = upstash(["GET", info_archive_key])
        raw_latest = upstash(["GET", latest_key])
        raw_watch_archive = upstash(["GET", watch_archive_key])
        raw_histories = _history_raw_values(platform, drama_ids, upstash=upstash)

        active = decode_remote_info_payload(active_key, raw_active)
        info_archive = decode_archive_payload(raw_info_archive, platform, key=info_archive_key)
        latest = decode_remote_watchcount_payload(latest_key, raw_latest)
        watch_archive = decode_archive_payload(raw_watch_archive, platform, key=watch_archive_key)

        changed = 0
        for drama_id, raw_history in zip(drama_ids, raw_histories):
            metadata = candidates[drama_id]
            record = _pop_active_info_record(platform, active, drama_id)
            if record is not None:
                changed += 1
                info_archive["records"].setdefault(
                    drama_id,
                    {
                        "archivedAt": metadata["archivedAt"],
                        "archivedReason": metadata["archivedReason"],
                        "record": record,
                    },
                )
            latest_entry = (latest.get("counts") or {}).pop(drama_id, None)
            history_entry = _decoded_history(raw_history, platform=platform, drama_id=drama_id)
            existing_watch = watch_archive["records"].get(drama_id)
            if latest_entry is not None or history_entry is not None or existing_watch is None:
                watch_archive["records"][drama_id] = {
                    "archivedAt": metadata["archivedAt"],
                    "archivedReason": metadata["archivedReason"],
                    "latest": deepcopy(latest_entry)
                    if latest_entry is not None
                    else (existing_watch or {}).get("latest"),
                    "history": deepcopy(history_entry)
                    if history_entry is not None
                    else (existing_watch or {}).get("history"),
                }

        stamp = utc_now()
        if platform == "manbo":
            active["updatedAt"] = stamp
        latest.setdefault("_meta", {})["updated_at"] = stamp
        info_archive["updatedAt"] = stamp
        watch_archive["updatedAt"] = stamp
        encoded_active = compact_json(active)
        encoded_info_archive = compact_json(info_archive)
        encoded_latest = compact_json(latest)
        encoded_watch_archive = compact_json(watch_archive)
        encoded_meta = compact_json(build_info_v2_meta(active_key, encoded_active, active))
        history_expected = [raw if isinstance(raw, str) else "__missing__" for raw in raw_histories]
        args = [
            string_cas_token(raw_active),
            string_cas_token(raw_info_archive),
            string_cas_token(raw_latest),
            string_cas_token(raw_watch_archive),
            encoded_active,
            encoded_meta,
            encoded_info_archive,
            encoded_latest,
            len(drama_ids),
            *drama_ids,
            *history_expected,
            encoded_watch_archive,
        ]
        result = upstash(
            [
                "EVAL",
                ARCHIVE_MOVE_SCRIPT,
                7,
                active_key,
                info_archive_key,
                latest_key,
                watch_archive_key,
                meta_key,
                legacy_key,
                history_key,
                *args,
            ]
        )
        if int(result or 0) != 1:
            continue

        verified_active = decode_remote_info_payload(active_key, upstash(["GET", active_key]))
        verified_info_archive = decode_archive_payload(
            upstash(["GET", info_archive_key]), platform, key=info_archive_key
        )
        verified_latest = decode_remote_watchcount_payload(latest_key, upstash(["GET", latest_key]))
        verified_watch_archive = decode_archive_payload(
            upstash(["GET", watch_archive_key]), platform, key=watch_archive_key
        )
        save_json(ACTIVE_INFO_PATHS[platform], verified_active)
        save_json(ACTIVE_WATCHCOUNT_PATHS[platform], verified_latest)
        save_local_archives(platform, verified_info_archive, verified_watch_archive)
        return {"archived": changed}

    raise RuntimeError(f"Concurrent updates prevented archiving {platform} dramas: {', '.join(drama_ids)}")
