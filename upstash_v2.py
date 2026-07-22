from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Callable

from upstash_editor import (
    RANK_META_KEY,
    build_rank_meta_update,
    decode_hgetall,
    hash_content_stats,
)

INFO_V2_KEYS = {
    "missevan:info:v1": "missevan:info:v2",
    "manbo:info:v1": "manbo:info:v2",
}
INFO_V2_META_KEYS = {
    "missevan:info:v1": "missevan:info:meta:v2",
    "manbo:info:v1": "manbo:info:meta:v2",
    "missevan:info:v2": "missevan:info:meta:v2",
    "manbo:info:v2": "manbo:info:meta:v2",
}
INFO_V1_KEYS = {v2_key: v1_key for v1_key, v2_key in INFO_V2_KEYS.items()}
NORMAL_TREND_V2_KEYS = {
    "missevan": "ranks:trend:missevan:v2",
    "manbo": "ranks:trend:manbo:v2",
}
PEAK_TREND_V2_KEY = "ranks:trend:peak:missevan:v2"
CV_TREND_V2_KEY = "ranks:trend:cv:v2"
NORMAL_TREND_V2_RETENTION_DATES = 45
CV_TREND_V2_RETENTION_DATES = 50
STAGING_TTL_SECONDS = 24 * 60 * 60
HASH_WRITE_CHUNK_SIZE = 100

INFO_META_COMPARE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""

INFO_SOURCE_COMPARE_AND_PUBLISH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '__missing__' then
  if current and current ~= false then
    return 0
  end
elseif not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
if redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('SET', KEYS[3], ARGV[2])
end
return 1
"""

HASH_ACTIVATE_SCRIPT = """
redis.call('RENAME', KEYS[1], KEYS[2])
redis.call('PERSIST', KEYS[2])
return 1
"""

HASH_ACTIVATE_WITH_META_SCRIPT = """
local current_meta = redis.call('HGET', KEYS[2], '__meta__')
if ARGV[1] == '__missing__' then
  if redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
  end
elseif not current_meta or current_meta ~= ARGV[1] then
  return 0
end
local rank_meta = redis.call('GET', KEYS[3])
if ARGV[2] == '__missing__' then
  if rank_meta and rank_meta ~= false then
    return 0
  end
elseif not rank_meta or redis.sha1hex(rank_meta) ~= ARGV[2] then
  return 0
end
redis.call('RENAME', KEYS[1], KEYS[2])
redis.call('PERSIST', KEYS[2])
redis.call('SET', KEYS[3], ARGV[3])
return 1
"""

RANK_STRING_PUBLISH_SCRIPT = """
local rank_meta = redis.call('GET', KEYS[2])
if ARGV[3] == '__missing__' then
  if rank_meta and rank_meta ~= false then
    return 0
  end
elseif not rank_meta or redis.sha1hex(rank_meta) ~= ARGV[3] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""


def compact_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def string_cas_token(raw: object) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() if isinstance(raw, str) else "__missing__"


def v2_publish_enabled() -> bool:
    return os.environ.get("UPSTASH_V2_PUBLISH_MODE", "best-effort").strip().lower() != "off"


def _verify_rank_resource(
    raw: object,
    *,
    scope: str,
    key: str,
    digest: str,
    byte_count: int,
    updated_at: str,
) -> None:
    if not isinstance(raw, (str, dict)):
        raise RuntimeError(f"Unable to verify {RANK_META_KEY} for {key}")
    meta = json.loads(raw) if isinstance(raw, str) else raw
    section = meta.get(scope) if isinstance(meta, dict) else None
    resources = section.get("resources") if isinstance(section, dict) else None
    resource = resources.get(key) if isinstance(resources, dict) else None
    if (
        not isinstance(resource, dict)
        or resource.get("contentSha1") != digest
        or int(resource.get("bytes") or -1) != byte_count
        or resource.get("updatedAt") != updated_at
    ):
        raise RuntimeError(f"Remote {RANK_META_KEY} verification failed for {key}")


def publish_rank_string(
    key: str,
    payload: object,
    *,
    scope: str,
    upstash: Callable[[list[object]], object],
) -> dict:
    encoded = json.dumps(payload, ensure_ascii=False)
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()
    byte_count = len(encoded.encode("utf-8"))
    updated_at = datetime.now(timezone.utc).isoformat()
    if isinstance(payload, dict):
        if key == "ranks:latest":
            updated_at = str((payload.get("_meta") or {}).get("updated_at") or updated_at)
        elif key == "ranks:cv:latest":
            updated_at = str(payload.get("generated_at") or updated_at)
    rank_meta: dict | None = None
    for _attempt in range(3):
        current_meta = upstash(["GET", RANK_META_KEY])
        rank_meta = build_rank_meta_update(
            current_meta,
            scope=scope,
            key=key,
            data_type="string",
            content_sha1=digest,
            byte_count=byte_count,
            updated_at=updated_at,
        )
        result = upstash(
            [
                "EVAL",
                RANK_STRING_PUBLISH_SCRIPT,
                2,
                key,
                RANK_META_KEY,
                encoded,
                compact_json(rank_meta),
                string_cas_token(current_meta),
            ]
        )
        if int(result or 0) == 1:
            break
    else:
        raise RuntimeError(f"Concurrent updates prevented publishing {key} and {RANK_META_KEY}")
    assert rank_meta is not None
    if upstash(["GET", key]) != encoded:
        raise RuntimeError(f"Remote payload verification failed for {key}")
    _verify_rank_resource(
        upstash(["GET", RANK_META_KEY]),
        scope=scope,
        key=key,
        digest=digest,
        byte_count=byte_count,
        updated_at=updated_at,
    )
    return rank_meta


def info_v2_key(key: str) -> str:
    if key in INFO_V1_KEYS:
        return key
    if key in INFO_V2_KEYS:
        return INFO_V2_KEYS[key]
    raise ValueError(f"Unsupported info key: {key}")


def info_v1_key(key: str) -> str:
    return INFO_V1_KEYS[info_v2_key(key)]


def _record_count(key: str, payload: object) -> int:
    v2_key = info_v2_key(key)
    if v2_key == "missevan:info:v2" and isinstance(payload, dict):
        return len(payload)
    if v2_key == "manbo:info:v2" and isinstance(payload, dict):
        records = payload.get("records")
        return len(records) if isinstance(records, list) else 0
    return 0


def build_info_v2_meta(key: str, encoded: str, payload: object) -> dict:
    v2_key = info_v2_key(key)
    return {
        "schemaVersion": 2,
        "dataKey": v2_key,
        "contentSha1": hashlib.sha1(encoded.encode("utf-8")).hexdigest(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "recordCount": _record_count(v2_key, payload),
        "bytes": len(encoded.encode("utf-8")),
    }


def publish_info_v2(
    key: str,
    payload: object,
    *,
    upstash: Callable[[list[object]], object],
    force: bool = False,
    source_encoded: str | None = None,
) -> dict | None:
    v2_key = info_v2_key(key)
    if key in INFO_V2_KEYS and not force and not v2_publish_enabled():
        return None
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    if v2_key == "manbo:info:v2":
        if not isinstance(normalized, dict):
            raise ValueError("manbo:info:v2 payload must be a JSON object")
        normalized["updatedAt"] = datetime.now(timezone.utc).isoformat()
    encoded = compact_json(normalized)
    meta_key = INFO_V2_META_KEYS[v2_key]
    legacy_key = info_v1_key(v2_key)
    if source_encoded is None:
        current = upstash(["GET", v2_key])
        expected = (
            hashlib.sha1(current.encode("utf-8")).hexdigest()
            if isinstance(current, str)
            else "__missing__"
        )
    else:
        expected = hashlib.sha1(source_encoded.encode("utf-8")).hexdigest()
    meta = build_info_v2_meta(v2_key, encoded, normalized)
    result = upstash(
        [
            "EVAL",
            INFO_SOURCE_COMPARE_AND_PUBLISH_SCRIPT,
            3,
            v2_key,
            meta_key,
            legacy_key,
            expected,
            encoded,
            compact_json(meta),
        ]
    )
    if int(result or 0) != 1:
        raise RuntimeError(f"Refusing to overwrite concurrently changed info v2: {v2_key}")
    if upstash(["GET", v2_key]) != encoded:
        raise RuntimeError(f"Remote payload verification failed for {v2_key}")
    verified_meta_raw = upstash(["GET", meta_key])
    verified_meta = json.loads(verified_meta_raw) if isinstance(verified_meta_raw, str) else verified_meta_raw
    if (
        not isinstance(verified_meta, dict)
        or verified_meta.get("contentSha1") != meta["contentSha1"]
        or int(verified_meta.get("bytes") or -1) != meta["bytes"]
        or verified_meta.get("updatedAt") != meta["updatedAt"]
    ):
        raise RuntimeError(f"Remote meta verification failed for {v2_key}")
    print(f"[ok] published authoritative {v2_key} and {meta_key} ({meta['bytes']} bytes)")
    return meta


def backfill_info_v2(v1_key: str, *, upstash: Callable[[list[object]], object]) -> dict:
    v2_key = info_v2_key(v1_key)
    if int(upstash(["EXISTS", v2_key]) or 0) == 1:
        raise RuntimeError(f"Refusing to backfill {v2_key}: authoritative v2 already exists")
    raw = upstash(["GET", v1_key])
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"Unable to backfill {v1_key}: remote value is empty")
    payload = json.loads(raw)
    meta = publish_info_v2(
        v1_key,
        payload,
        upstash=upstash,
        force=True,
    )
    assert meta is not None
    return meta


def _normalized_rank(rank: object) -> dict | None:
    if not isinstance(rank, dict):
        return None
    key = str(rank.get("key") or "").strip()
    if not key:
        return None
    return {
        "key": key,
        "name": str(rank.get("name") or key).strip(),
        "position": rank.get("position"),
    }


def build_normal_trend_v2(payload: dict, platform: str, *, retention_dates: int = NORMAL_TREND_V2_RETENTION_DATES) -> tuple[dict, dict[str, dict]]:
    dates = sorted({str(value) for value in payload.get("dates") or [] if value})
    kept_dates = dates[-retention_dates:]
    kept = set(kept_dates)
    third_metric = "subscription_num" if platform == "missevan" else "pay_count"
    fields: dict[str, dict] = {}
    for raw_id, raw_entry in (payload.get("dramas") or {}).items():
        if not isinstance(raw_entry, dict):
            continue
        all_samples = raw_entry.get("samples") if isinstance(raw_entry.get("samples"), dict) else {}
        last_rank = None
        samples: dict[str, dict] = {}
        for date, raw_sample in sorted(all_samples.items()):
            if not isinstance(raw_sample, dict):
                continue
            ranks = [rank for rank in (_normalized_rank(item) for item in raw_sample.get("ranks") or []) if rank]
            if ranks and str(date) not in kept:
                last_rank = {"date": str(date), "ranks": ranks}
            if str(date) not in kept:
                continue
            raw_metrics = raw_sample.get("metrics") if isinstance(raw_sample.get("metrics"), dict) else {}
            metrics = {
                key: raw_metrics.get(key)
                for key in ("view_count", "danmaku_uid_count", third_metric)
                if key in raw_metrics
            }
            samples[str(date)] = {
                "generated_at": str(raw_sample.get("generated_at") or ""),
                "metrics": metrics,
                "ranks": ranks,
            }
        if not samples:
            continue
        drama_id = str(raw_entry.get("id") or raw_id)
        record = {
            "version": 2,
            "id": drama_id,
            "name": str(raw_entry.get("name") or drama_id),
            "cover": raw_entry.get("cover", ""),
            "maincvs": raw_entry.get("maincvs") or [],
            "catalogName": raw_entry.get("catalogName", ""),
            "payStatus": raw_entry.get("payStatus", ""),
            "createTime": raw_entry.get("createTime", ""),
            "updated_at": raw_entry.get("updated_at", ""),
            "samples": samples,
        }
        if last_rank:
            record["lastRank"] = last_rank
        fields[drama_id] = record
    meta = {
        "version": 2,
        "platform": platform,
        "updated_at": str(payload.get("updated_at") or ""),
        "dates": kept_dates,
        "entityCount": len(fields),
        "retentionDates": retention_dates,
    }
    return meta, fields


def build_peak_trend_v2(payload: dict, *, retention_dates: int = NORMAL_TREND_V2_RETENTION_DATES) -> tuple[dict, dict[str, dict]]:
    dates = sorted({str(value) for value in payload.get("dates") or [] if value})
    kept_dates = dates[-retention_dates:]
    kept = set(kept_dates)
    fields: dict[str, dict] = {}
    for raw_name, raw_entry in (payload.get("series") or {}).items():
        if not isinstance(raw_entry, dict):
            continue
        samples = {}
        last_rank = None
        for date, sample in sorted((raw_entry.get("samples") or {}).items()):
            if not isinstance(sample, dict):
                continue
            if sample.get("position") is not None and str(date) not in kept:
                last_rank = {
                    "date": str(date),
                    "ranks": [{"key": "peak", "name": "巅峰榜", "position": sample.get("position")}],
                }
            if str(date) in kept:
                samples[str(date)] = sample
        if not samples:
            continue
        name = str(raw_entry.get("name") or raw_name)
        record = {**raw_entry, "version": 2, "name": name, "samples": samples}
        if last_rank:
            record["lastRank"] = last_rank
        fields[name] = record
    meta = {
        "version": 2,
        "platform": "missevan",
        "kind": "peak",
        "updated_at": str(payload.get("updated_at") or ""),
        "dates": kept_dates,
        "entityCount": len(fields),
        "retentionDates": retention_dates,
    }
    return meta, fields


def normalize_cv_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_cv_trend_v2(payloads: dict[str, dict]) -> tuple[dict, dict[str, dict]]:
    fields: dict[str, dict] = {}
    platforms_meta: dict[str, dict] = {}
    for platform in ("missevan", "manbo"):
        payload = payloads.get(platform) or {}
        dates = sorted({str(value) for value in payload.get("dates") or [] if value})[-CV_TREND_V2_RETENTION_DATES:]
        kept = set(dates)
        for raw_name, raw_entry in (payload.get("cvs") or {}).items():
            if not isinstance(raw_entry, dict):
                continue
            name = normalize_cv_name(raw_entry.get("cvName") or raw_name)
            if not name:
                continue
            samples = {
                str(date): sample
                for date, sample in (raw_entry.get("samples") or {}).items()
                if str(date) in kept
            }
            if not samples:
                continue
            field_key = f"{platform}:{name}"
            existing = fields.get(field_key) or {}
            fields[field_key] = {
                **existing,
                **raw_entry,
                "version": 2,
                "cvName": name,
                "samples": {**(existing.get("samples") or {}), **samples},
            }
        platforms_meta[platform] = {
            "updated_at": str(payload.get("updated_at") or ""),
            "dates": dates,
            "entityCount": sum(1 for field in fields if field.startswith(f"{platform}:")),
            "retentionDates": CV_TREND_V2_RETENTION_DATES,
        }
    meta = {
        "version": 2,
        "kind": "cv",
        "updated_at": max((item["updated_at"] for item in platforms_meta.values()), default=""),
        "platforms": platforms_meta,
        "entityCount": len(fields),
    }
    return meta, fields


def _load_hash_snapshot(
    key: str,
    *,
    upstash: Callable[[list[object]], object],
) -> tuple[dict, dict[str, dict], str | None]:
    raw = decode_hgetall(upstash(["HGETALL", key]))
    if not raw:
        return {}, {}, None
    raw_meta = raw.get("__meta__")
    if raw_meta is None:
        raise RuntimeError(f"Invalid authoritative v2 Hash: {key} is missing __meta__")
    try:
        meta = json.loads(raw_meta)
        fields = {
            field: json.loads(value)
            for field, value in raw.items()
            if field != "__meta__"
        }
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid authoritative v2 Hash: {key}") from exc
    if not isinstance(meta, dict) or not all(isinstance(value, dict) for value in fields.values()):
        raise RuntimeError(f"Invalid authoritative v2 Hash shape: {key}")
    return meta, fields, raw_meta


def _merge_latest_entity_samples(
    current_fields: dict[str, dict],
    candidate_fields: dict[str, dict],
    *,
    incoming_dates: set[str],
    kept_dates: set[str],
) -> dict[str, dict]:
    merged = json.loads(json.dumps(current_fields, ensure_ascii=False))
    for field, candidate in candidate_fields.items():
        existing = merged.get(field) if isinstance(merged.get(field), dict) else {}
        existing_samples = existing.get("samples") if isinstance(existing.get("samples"), dict) else {}
        candidate_samples = candidate.get("samples") if isinstance(candidate.get("samples"), dict) else {}
        samples = {
            str(date): sample
            for date, sample in existing_samples.items()
            if str(date) in kept_dates
        }
        for date, sample in candidate_samples.items():
            if str(date) in incoming_dates:
                samples[str(date)] = sample
        record = {**existing, **candidate, "samples": samples}
        if samples:
            merged[field] = record
    for field, record in list(merged.items()):
        samples = record.get("samples") if isinstance(record, dict) else None
        if not isinstance(samples, dict):
            merged.pop(field, None)
            continue
        record["samples"] = {
            str(date): sample for date, sample in samples.items() if str(date) in kept_dates
        }
        if not record["samples"]:
            merged.pop(field, None)
    return merged


def merge_normal_v2_authoritative(
    current_meta: dict,
    current_fields: dict[str, dict],
    candidate_meta: dict,
    candidate_fields: dict[str, dict],
) -> tuple[dict, dict[str, dict]]:
    retention = int(current_meta.get("retentionDates") or candidate_meta.get("retentionDates") or NORMAL_TREND_V2_RETENTION_DATES)
    candidate_dates = [str(value) for value in candidate_meta.get("dates") or [] if value]
    incoming_dates = {max(candidate_dates)} if candidate_dates else set()
    all_dates = sorted(
        {
            *(str(value) for value in current_meta.get("dates") or [] if value),
            *candidate_dates,
        }
    )
    kept_dates = set(all_dates[-retention:])
    fields = _merge_latest_entity_samples(
        current_fields,
        candidate_fields,
        incoming_dates=incoming_dates,
        kept_dates=kept_dates,
    )
    meta = {
        **current_meta,
        **candidate_meta,
        "dates": sorted(kept_dates),
        "entityCount": len(fields),
        "retentionDates": retention,
    }
    return meta, fields


def merge_cv_v2_authoritative(
    current_meta: dict,
    current_fields: dict[str, dict],
    candidate_meta: dict,
    candidate_fields: dict[str, dict],
) -> tuple[dict, dict[str, dict]]:
    fields = json.loads(json.dumps(current_fields, ensure_ascii=False))
    platforms_meta: dict[str, dict] = {}
    for platform in ("missevan", "manbo"):
        current_platform = ((current_meta.get("platforms") or {}).get(platform) or {})
        candidate_platform = ((candidate_meta.get("platforms") or {}).get(platform) or {})
        retention = int(
            current_platform.get("retentionDates")
            or candidate_platform.get("retentionDates")
            or CV_TREND_V2_RETENTION_DATES
        )
        candidate_dates = [str(value) for value in candidate_platform.get("dates") or [] if value]
        incoming_dates = {max(candidate_dates)} if candidate_dates else set()
        all_dates = sorted(
            {
                *(str(value) for value in current_platform.get("dates") or [] if value),
                *candidate_dates,
            }
        )
        kept_dates = set(all_dates[-retention:])
        prefix = f"{platform}:"
        platform_current = {field: item for field, item in fields.items() if field.startswith(prefix)}
        platform_candidate = {
            field: item for field, item in candidate_fields.items() if field.startswith(prefix)
        }
        platform_merged = _merge_latest_entity_samples(
            platform_current,
            platform_candidate,
            incoming_dates=incoming_dates,
            kept_dates=kept_dates,
        )
        for field in [field for field in fields if field.startswith(prefix)]:
            fields.pop(field)
        fields.update(platform_merged)
        platforms_meta[platform] = {
            **current_platform,
            **candidate_platform,
            "dates": sorted(kept_dates),
            "entityCount": len(platform_merged),
            "retentionDates": retention,
        }
    meta = {
        **current_meta,
        **candidate_meta,
        "platforms": platforms_meta,
        "entityCount": len(fields),
    }
    return meta, fields


def _rank_scope_for_hash(key: str) -> str:
    return "cv" if key == CV_TREND_V2_KEY else "normal"


def decorate_hash_meta(meta: dict, fields: dict[str, dict]) -> tuple[dict, dict[str, str]]:
    encoded_fields = {
        str(field): compact_json(value)
        for field, value in sorted(fields.items())
    }
    digest, byte_count = hash_content_stats(encoded_fields)
    decorated = {
        **meta,
        "version": 2,
        "updated_at": str(meta.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "revision": uuid.uuid4().hex,
        "contentSha1": digest,
        "entityCount": len(fields),
        "bytes": byte_count,
    }
    return decorated, encoded_fields


def publish_hash_snapshot_atomic(
    stable_key: str,
    meta: dict,
    fields: dict[str, dict],
    *,
    upstash: Callable[[list[object]], object],
    expected_meta_raw: str | None,
    chunk_size: int = HASH_WRITE_CHUNK_SIZE,
) -> None:
    if any(str(field) == "__meta__" for field in fields):
        raise ValueError(f"Reserved hash field __meta__ cannot be published to {stable_key}")
    staging_key = f"{stable_key}:staging:{uuid.uuid4().hex}"
    meta, raw_fields = decorate_hash_meta(meta, fields)
    encoded_fields = [("__meta__", compact_json(meta)), *sorted(raw_fields.items())]
    effective_chunk_size = max(1, min(chunk_size, HASH_WRITE_CHUNK_SIZE))
    try:
        for offset in range(0, len(encoded_fields), effective_chunk_size):
            chunk = encoded_fields[offset : offset + effective_chunk_size]
            args: list[object] = ["HSET", staging_key]
            for field, value in chunk:
                args.extend([field, value])
            result = upstash(args)
            if not isinstance(result, int) or isinstance(result, bool) or result != len(chunk):
                raise RuntimeError(
                    f"Unexpected HSET result for {staging_key}: {result!r} != {len(chunk)}"
                )
            if offset == 0:
                expire_result = upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS])
                if int(expire_result or 0) != 1:
                    raise RuntimeError(f"Failed to set staging TTL for {staging_key}: {expire_result!r}")
        actual = int(upstash(["HLEN", staging_key]) or 0)
        if actual != len(encoded_fields):
            raise RuntimeError(f"Hash verification failed for {staging_key}: {actual} != {len(encoded_fields)}")
        sample_fields = ["__meta__"] + [field for field, _value in encoded_fields[1:2]]
        sample_values = upstash(["HMGET", staging_key, *sample_fields])
        if not isinstance(sample_values, list) or len(sample_values) != len(sample_fields):
            raise RuntimeError(f"Unable to sample {staging_key}")
        for value in sample_values:
            json.loads(value)
        digest = str(meta["contentSha1"])
        byte_count = int(meta["bytes"])
        rank_meta: dict | None = None
        for _attempt in range(3):
            current_rank_meta = upstash(["GET", RANK_META_KEY])
            rank_meta = build_rank_meta_update(
                current_rank_meta,
                scope=_rank_scope_for_hash(stable_key),
                key=stable_key,
                data_type="hash",
                content_sha1=digest,
                byte_count=byte_count,
                updated_at=str(meta.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            )
            result = upstash(
                [
                    "EVAL",
                    HASH_ACTIVATE_WITH_META_SCRIPT,
                    3,
                    staging_key,
                    stable_key,
                    RANK_META_KEY,
                    expected_meta_raw if expected_meta_raw is not None else "__missing__",
                    string_cas_token(current_rank_meta),
                    compact_json(rank_meta),
                ]
            )
            if int(result or 0) == 1:
                break
        else:
            raise RuntimeError(f"Concurrent update detected while activating {stable_key}")
        assert rank_meta is not None
        verified = decode_hgetall(upstash(["HGETALL", stable_key]))
        verified_digest, verified_bytes = hash_content_stats(verified)
        verified_meta_raw = verified.get("__meta__")
        verified_meta = json.loads(verified_meta_raw) if isinstance(verified_meta_raw, str) else None
        if (
            verified_digest != digest
            or verified_bytes != byte_count
            or not isinstance(verified_meta, dict)
            or verified_meta.get("contentSha1") != digest
            or int(verified_meta.get("bytes") or -1) != byte_count
        ):
            raise RuntimeError(f"Remote Hash verification failed for {stable_key}")
        _verify_rank_resource(
            upstash(["GET", RANK_META_KEY]),
            scope=_rank_scope_for_hash(stable_key),
            key=stable_key,
            digest=digest,
            byte_count=byte_count,
            updated_at=str(meta.get("updated_at") or ""),
        )
    except Exception:
        try:
            upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS])
        except Exception:
            pass
        raise
    print(f"[ok] published {stable_key} ({len(fields)} entities)")


def publish_normal_trend_v2(platform: str, payload: dict, *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    key = NORMAL_TREND_V2_KEYS[platform]
    if force and int(upstash(["EXISTS", key]) or 0) == 1:
        raise RuntimeError(f"Refusing legacy backfill: authoritative v2 already exists: {key}")
    candidate_meta, candidate_fields = build_normal_trend_v2(payload, platform)
    current_meta, current_fields, current_meta_raw = _load_hash_snapshot(key, upstash=upstash)
    if current_fields:
        meta, fields = merge_normal_v2_authoritative(
            current_meta,
            current_fields,
            candidate_meta,
            candidate_fields,
        )
    else:
        meta, fields = candidate_meta, candidate_fields
    publish_hash_snapshot_atomic(
        NORMAL_TREND_V2_KEYS[platform],
        meta,
        fields,
        upstash=upstash,
        expected_meta_raw=current_meta_raw,
    )


def publish_peak_trend_v2(payload: dict, *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    if force and int(upstash(["EXISTS", PEAK_TREND_V2_KEY]) or 0) == 1:
        raise RuntimeError(f"Refusing legacy backfill: authoritative v2 already exists: {PEAK_TREND_V2_KEY}")
    candidate_meta, candidate_fields = build_peak_trend_v2(payload)
    current_meta, current_fields, current_meta_raw = _load_hash_snapshot(PEAK_TREND_V2_KEY, upstash=upstash)
    if current_fields:
        meta, fields = merge_normal_v2_authoritative(
            current_meta,
            current_fields,
            candidate_meta,
            candidate_fields,
        )
    else:
        meta, fields = candidate_meta, candidate_fields
    publish_hash_snapshot_atomic(
        PEAK_TREND_V2_KEY,
        meta,
        fields,
        upstash=upstash,
        expected_meta_raw=current_meta_raw,
    )


def publish_cv_trend_v2(payloads: dict[str, dict], *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    if force and int(upstash(["EXISTS", CV_TREND_V2_KEY]) or 0) == 1:
        raise RuntimeError(f"Refusing legacy backfill: authoritative v2 already exists: {CV_TREND_V2_KEY}")
    candidate_meta, candidate_fields = build_cv_trend_v2(payloads)
    current_meta, current_fields, current_meta_raw = _load_hash_snapshot(CV_TREND_V2_KEY, upstash=upstash)
    if current_fields:
        meta, fields = merge_cv_v2_authoritative(
            current_meta,
            current_fields,
            candidate_meta,
            candidate_fields,
        )
    else:
        meta, fields = candidate_meta, candidate_fields
    publish_hash_snapshot_atomic(
        CV_TREND_V2_KEY,
        meta,
        fields,
        upstash=upstash,
        expected_meta_raw=current_meta_raw,
    )


def publish_trend_v2_best_effort(label: str, publish: Callable[[], None]) -> None:
    try:
        publish()
    except Exception as exc:
        print(f"[error] failed to publish authoritative {label}: {exc}")
        raise
