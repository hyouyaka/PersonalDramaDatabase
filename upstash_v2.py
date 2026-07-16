from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Callable


INFO_V2_KEYS = {
    "missevan:info:v1": "missevan:info:v2",
    "manbo:info:v1": "manbo:info:v2",
}
INFO_V2_META_KEYS = {
    "missevan:info:v1": "missevan:info:meta:v2",
    "manbo:info:v1": "manbo:info:meta:v2",
}
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
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2])
redis.call('SET', KEYS[3], ARGV[3])
return 1
"""

HASH_ACTIVATE_SCRIPT = """
redis.call('RENAME', KEYS[1], KEYS[2])
redis.call('PERSIST', KEYS[2])
return 1
"""


def compact_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def v2_publish_enabled() -> bool:
    return os.environ.get("UPSTASH_V2_PUBLISH_MODE", "best-effort").strip().lower() != "off"


def _record_count(v1_key: str, payload: object) -> int:
    if v1_key == "missevan:info:v1" and isinstance(payload, dict):
        return len(payload)
    if v1_key == "manbo:info:v1" and isinstance(payload, dict):
        records = payload.get("records")
        return len(records) if isinstance(records, list) else 0
    return 0


def build_info_v2_meta(v1_key: str, encoded: str, payload: object) -> dict:
    return {
        "schemaVersion": 2,
        "dataKey": INFO_V2_KEYS[v1_key],
        "contentSha1": hashlib.sha1(encoded.encode("utf-8")).hexdigest(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "recordCount": _record_count(v1_key, payload),
        "bytes": len(encoded.encode("utf-8")),
    }


def publish_info_v2(
    v1_key: str,
    payload: object,
    *,
    upstash: Callable[[list[object]], object],
    force: bool = False,
    source_encoded: str | None = None,
) -> dict | None:
    if v1_key not in INFO_V2_KEYS:
        raise ValueError(f"Unsupported info key: {v1_key}")
    if not force and not v2_publish_enabled():
        return None
    v2_key = INFO_V2_KEYS[v1_key]
    meta_key = INFO_V2_META_KEYS[v1_key]
    if source_encoded is not None:
        return _publish_info_v2_from_source(
            v1_key,
            source_encoded,
            payload,
            upstash=upstash,
        )
    encoded = compact_json(payload)
    result = upstash(["SET", v2_key, encoded])
    if result != "OK":
        raise RuntimeError(f"Failed to publish {v2_key}: {result!r}")
    meta = build_info_v2_meta(v1_key, encoded, payload)
    result = upstash([
        "EVAL",
        INFO_META_COMPARE_SCRIPT,
        2,
        v2_key,
        meta_key,
        meta["contentSha1"],
        compact_json(meta),
    ])
    if int(result or 0) != 1:
        current_encoded = upstash(["GET", v2_key])
        if not isinstance(current_encoded, str) or not current_encoded:
            raise RuntimeError(f"Unable to rebuild current info meta: {meta_key}")
        current_payload = json.loads(current_encoded)
        meta = build_info_v2_meta(v1_key, current_encoded, current_payload)
        result = upstash([
            "EVAL",
            INFO_META_COMPARE_SCRIPT,
            2,
            v2_key,
            meta_key,
            meta["contentSha1"],
            compact_json(meta),
        ])
        if int(result or 0) != 1:
            raise RuntimeError(f"Refusing to publish stale info meta: {meta_key}")
    print(f"[ok] published {v2_key} and {meta_key} ({meta['bytes']} bytes)")
    return meta


def _publish_info_v2_from_source(
    v1_key: str,
    source_encoded: str,
    payload: object,
    *,
    upstash: Callable[[list[object]], object],
) -> dict:
    v2_key = INFO_V2_KEYS[v1_key]
    meta_key = INFO_V2_META_KEYS[v1_key]
    current_source = source_encoded
    current_payload = payload
    for _attempt in range(2):
        encoded = compact_json(current_payload)
        meta = build_info_v2_meta(v1_key, encoded, current_payload)
        result = upstash([
            "EVAL",
            INFO_SOURCE_COMPARE_AND_PUBLISH_SCRIPT,
            3,
            v1_key,
            v2_key,
            meta_key,
            hashlib.sha1(current_source.encode("utf-8")).hexdigest(),
            encoded,
            compact_json(meta),
        ])
        if int(result or 0) == 1:
            print(f"[ok] published {v2_key} and {meta_key} ({meta['bytes']} bytes)")
            return meta
        current_source = upstash(["GET", v1_key])
        if not isinstance(current_source, str) or not current_source:
            raise RuntimeError(f"Unable to rebuild info v2 from current source: {v1_key}")
        current_payload = json.loads(current_source)
    raise RuntimeError(f"Refusing to publish stale info v2: {v2_key}")


def publish_info_v2_best_effort(
    v1_key: str,
    payload: object,
    *,
    upstash: Callable[[list[object]], object],
    source_encoded: str | None = None,
) -> dict | None:
    try:
        return publish_info_v2(
            v1_key,
            payload,
            upstash=upstash,
            source_encoded=source_encoded,
        )
    except Exception as exc:
        print(f"[warn] failed to publish info v2 for {v1_key}: {exc}")
        return None


def backfill_info_v2(v1_key: str, *, upstash: Callable[[list[object]], object]) -> dict:
    raw = upstash(["GET", v1_key])
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"Unable to backfill {v1_key}: remote value is empty")
    payload = json.loads(raw)
    meta = publish_info_v2(
        v1_key,
        payload,
        upstash=upstash,
        force=True,
        source_encoded=raw,
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


def publish_hash_snapshot_atomic(
    stable_key: str,
    meta: dict,
    fields: dict[str, dict],
    *,
    upstash: Callable[[list[object]], object],
    chunk_size: int = HASH_WRITE_CHUNK_SIZE,
) -> None:
    if any(str(field) == "__meta__" for field in fields):
        raise ValueError(f"Reserved hash field __meta__ cannot be published to {stable_key}")
    staging_key = f"{stable_key}:staging:{uuid.uuid4().hex}"
    encoded_fields = [("__meta__", compact_json(meta))] + [
        (str(field), compact_json(value)) for field, value in sorted(fields.items())
    ]
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
        result = upstash(["EVAL", HASH_ACTIVATE_SCRIPT, 2, staging_key, stable_key])
        if int(result or 0) != 1:
            raise RuntimeError(f"Failed to activate {stable_key}: {result!r}")
    except Exception:
        try:
            upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS])
        except Exception:
            pass
        raise
    print(f"[ok] published {stable_key} ({len(fields)} entities)")


def publish_normal_trend_v2(platform: str, payload: dict, *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    if not force and not v2_publish_enabled():
        return
    meta, fields = build_normal_trend_v2(payload, platform)
    publish_hash_snapshot_atomic(NORMAL_TREND_V2_KEYS[platform], meta, fields, upstash=upstash)


def publish_peak_trend_v2(payload: dict, *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    if not force and not v2_publish_enabled():
        return
    meta, fields = build_peak_trend_v2(payload)
    publish_hash_snapshot_atomic(PEAK_TREND_V2_KEY, meta, fields, upstash=upstash)


def publish_cv_trend_v2(payloads: dict[str, dict], *, upstash: Callable[[list[object]], object], force: bool = False) -> None:
    if not force and not v2_publish_enabled():
        return
    meta, fields = build_cv_trend_v2(payloads)
    publish_hash_snapshot_atomic(CV_TREND_V2_KEY, meta, fields, upstash=upstash)


def publish_trend_v2_best_effort(label: str, publish: Callable[[], None]) -> None:
    try:
        publish()
    except Exception as exc:
        print(f"[warn] failed to publish {label}: {exc}")
