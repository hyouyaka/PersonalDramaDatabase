"""Fetch rank data from Missevan and Manbo platforms, saving to ranks.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from platform_sync import (
    MANBO_HEADERS,
    MANBO_CATALOG_NAME_ALIASES,
    MANBO_CATALOG_NAME_BY_ID,
    MISSEVAN_HEADERS,
    MISSEVAN_CATALOG_NAME_BY_ID,
    MissevanRequester,
    load_json,
    normalize,
    request_manbo_json,
    save_json as _save_json,
)
from rank_key_cleanup import cleanup_legacy_normal_rank_keys, run_cleanup_best_effort

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
RANKS_PATH = HERE / "ranks.json"
SERIES_INFO_PATH = HERE / "drama-series-info.json"
CACHE_WINDOW = timedelta(hours=12)
RANK_TREND_RETENTION_DATES = 90
SAVE_LOCK = threading.Lock()
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc

load_env_file(HERE / ".env")

QUEUE_KEY = "new:dramaIDs"
PEAK_TREND_KEY = "ranks:trend:peak:missevan"
SERIES_INFO_KEY = "drama:series-info:v1"
PLATFORMS = ("missevan", "manbo")
TREND_KEYS = {
    "missevan": "ranks:trend:missevan",
    "manbo": "ranks:trend:manbo",
}
ONGOING_KEYS = {
    "missevan": "ongoing:missevan",
    "manbo": "ongoing:manbo",
}


def save_json(path: Path, data) -> None:
    with SAVE_LOCK:
        _save_json(path, data)


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default

# -- Missevan rank definitions (key -> (type, sub_type, display_name)) ------
MISSEVAN_RANKS = {
    "new_daily":          (1, 1, "新品日榜"),
    "new_weekly":         (1, 2, "新品周榜"),
    "popular_weekly":     (2, 2, "人气周榜"),
    "popular_monthly":    (2, 3, "人气月榜"),
    "bestseller_weekly":  (9, 2, "畅销周榜"),
    "bestseller_monthly": (9, 3, "畅销月榜"),
}

# -- Manbo rank definitions (key -> (rankId, display_name, limit, value_field))
MANBO_RANKS = {
    "hot":               (0, "热播榜",       20,   "hotValue"),
    "box_office_total":  (7, "票房榜总榜",    None, "hotValue"),
    "box_office_member": (8, "票房榜会员剧榜", None, "hotValue"),
    "box_office_paid":   (9, "票房榜付费剧榜", None, "hotValue"),
    "diamond_monthly":   (5, "钻石榜月榜",    20,   "diamondValue"),
    "peak":              (4, "巅峰榜",       50,   "hotValue"),
}

MANBO_DANMAKU_PAGE_SIZE = 200
MANBO_DANMAKU_PAGE_CONCURRENCY = env_int("MANBO_DANMAKU_PAGE_CONCURRENCY", 24)
MANBO_DANMAKU_DRAMA_CONCURRENCY = 1
MANBO_DANMAKU_REQUEST_RETRIES = 3
MANBO_DANMAKU_SHORT_PAGE_RETRIES = env_int("MANBO_DANMAKU_SHORT_PAGE_RETRIES", 4)
MANBO_DANMAKU_SHORT_PAGE_RETRY_DELAY = env_float("MANBO_DANMAKU_SHORT_PAGE_RETRY_DELAY", 1.2)
DANMAKU_DRAMA_RETRY_ATTEMPTS = 3
MANBO_DANMAKU_LOW_VALUE_RATIO = 0.02


class DanmakuRefreshError(RuntimeError):
    """Raised when a drama-level danmaku refresh should be retried."""

# ---------------------------------------------------------------------------
# Upstash helpers (adapted from sync_new_drama_ids.py)
# ---------------------------------------------------------------------------

def upstash_request(command: list[object]) -> object:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        raise RuntimeError("Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN in environment.")
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=command,
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload.get("result")


def load_queue() -> dict[str, list[str]]:
    raw = upstash_request(["GET", QUEUE_KEY])
    if raw in (None, ""):
        return {"manbo": [], "missevan": []}
    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, dict):
        data = raw
    else:
        raise RuntimeError(f"Unsupported payload type for {QUEUE_KEY}: {type(raw).__name__}")
    return {
        "manbo": list(dict.fromkeys(str(i) for i in (data.get("manbo") or []))),
        "missevan": list(dict.fromkeys(str(i) for i in (data.get("missevan") or []))),
    }


def save_queue(queue: dict[str, list[str]]) -> None:
    payload = json.dumps(queue, ensure_ascii=False)
    result = upstash_request(["SET", QUEUE_KEY, payload])
    if result != "OK":
        raise RuntimeError(f"Failed to update {QUEUE_KEY}: {result!r}")
    print(f"[ok] updated queue: manbo={len(queue.get('manbo', []))}, missevan={len(queue.get('missevan', []))}")


def append_new_drama_ids_atomic(missevan_ids: list[str], manbo_ids: list[str]) -> None:
    """Atomically merge new drama IDs into Upstash queue."""
    if not missevan_ids and not manbo_ids:
        return
    script = r'''
local raw = redis.call("GET", KEYS[1])
local queue = {missevan = {}, manbo = {}}
if raw and raw ~= false and raw ~= "" then
  queue = cjson.decode(raw)
  queue["missevan"] = queue["missevan"] or {}
  queue["manbo"] = queue["manbo"] or {}
end

local function merge(field, additions_json)
  local seen = {}
  local merged = {}
  for _, value in ipairs(queue[field] or {}) do
    local text = tostring(value)
    if not seen[text] then
      seen[text] = true
      table.insert(merged, text)
    end
  end
  for _, value in ipairs(cjson.decode(additions_json)) do
    local text = tostring(value)
    if not seen[text] then
      seen[text] = true
      table.insert(merged, text)
    end
  end
  queue[field] = merged
end

merge("missevan", ARGV[1])
merge("manbo", ARGV[2])
local payload = cjson.encode(queue)
redis.call("SET", KEYS[1], payload)
return payload
'''
    raw = upstash_request([
        "EVAL",
        script,
        1,
        QUEUE_KEY,
        json.dumps([str(value) for value in missevan_ids], ensure_ascii=False),
        json.dumps([str(value) for value in manbo_ids], ensure_ascii=False),
    ])
    updated = json.loads(raw) if isinstance(raw, str) else (raw or {})
    print(
        f"[ok] updated queue: manbo={len(updated.get('manbo', []))}, "
        f"missevan={len(updated.get('missevan', []))}"
    )


def upload_full_ranks(store: dict) -> None:
    """Upload complete merged ranks under the latest full-rank key."""
    payload = json.dumps(store, ensure_ascii=False)
    result = upstash_request(["SET", "ranks:latest", payload])
    if result != "OK":
        raise RuntimeError(f"Failed to upload ranks:latest: {result!r}")
    print(f"[ok] uploaded merged ranks to Upstash ({len(payload)} bytes)")


def decode_upstash_json(raw: object, default: object = None) -> object:
    if raw in (None, ""):
        return default
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def platform_store(value: object) -> dict:
    if isinstance(value, dict):
        value.setdefault("ranks", {})
        value.setdefault("dramas", {})
        return value
    return {"ranks": {}, "dramas": {}}


def load_remote_full_ranks() -> dict | None:
    payload = decode_upstash_json(upstash_request(["GET", "ranks:latest"]))
    return payload if isinstance(payload, dict) else None


def load_series_info() -> dict:
    payload = _load_upstash_json(SERIES_INFO_KEY)
    if isinstance(payload, dict):
        print(f"  [upstash] loaded {SERIES_INFO_KEY}")
        return payload
    print(f"  [local backup] loaded series info from {SERIES_INFO_PATH}")
    return load_json(SERIES_INFO_PATH, {})


def upload_rank_outputs(store: dict, platforms: tuple[str, ...] | list[str]) -> dict:
    generated_at = now_iso()
    history_date = generated_at[:10]
    payloads = build_rank_snapshot_payloads(
        store,
        platforms=platforms,
        history_date=history_date,
        generated_at=generated_at,
    )
    if "missevan" in platforms:
        upload_missevan_peak_trend(
            store,
            history_date=history_date,
            generated_at=generated_at,
        )
    for platform in platforms:
        snapshot = payloads[platform]
        upload_rank_trend_snapshot(
            platform,
            history_date,
            snapshot["metrics"],
            snapshot["list"],
            generated_at=generated_at,
        )
    upload_full_ranks(store)
    run_cleanup_best_effort(lambda: cleanup_legacy_normal_rank_keys(upstash_request))
    return store


def load_initial_rank_store() -> dict:
    try:
        remote = load_remote_full_ranks()
        if isinstance(remote, dict):
            print("  [upstash] loaded initial ranks store from ranks:latest")
            return sanitize_rank_store(remote)
    except Exception as exc:
        print(f"  [upstash] WARN: failed to load remote initial ranks store: {exc}")

    store = load_json(RANKS_PATH, None)
    if store is not None:
        print(f"  [local] loaded initial ranks store from {RANKS_PATH}")
        return sanitize_rank_store(store)
    print("  [local] no ranks.json found; initializing empty ranks store")
    return sanitize_rank_store(init_ranks_store())


def _coerce_rank_item(item: object, position: int) -> dict:
    row: dict[str, object] = {"position": position}
    if isinstance(item, dict):
        drama_id = item.get("dramaId") or item.get("drama_id")
        drama_ids = item.get("dramaIds") or item.get("drama_ids")
        title = item.get("name") or item.get("title")
        if drama_id is not None:
            row["drama_id"] = str(drama_id)
        if drama_ids:
            row["drama_ids"] = [str(value) for value in drama_ids]
        if title:
            row["title"] = str(title)
            row["series_key"] = str(title)
        for value_name in ("hotValue", "diamondValue", "view_count"):
            if value_name in item:
                row["rank_value"] = item.get(value_name)
                row["rank_value_name"] = value_name
                break
        row["raw"] = item
        return row
    row["drama_id"] = str(item)
    row["raw"] = item
    return row


def _build_rank_list_payload(store: dict, platform: str, history_date: str, generated_at: str) -> dict:
    rank_payload: dict[str, object] = {}
    for rank_key, rank in (store.get(platform, {}).get("ranks") or {}).items():
        if platform == "missevan" and rank_key == "peak":
            continue
        items = rank.get("items") or []
        rank_payload[rank_key] = {
            "name": rank.get("name", rank_key),
            "fetched_at": rank.get("fetched_at"),
            "rankId": rank.get("rankId"),
            "unitName": rank.get("unitName"),
            "items": [
                _coerce_rank_item(item, position)
                for position, item in enumerate(items, 1)
            ],
        }
    return {
        "version": 1,
        "date": history_date,
        "platform": platform,
        "generated_at": generated_at,
        "ranks": rank_payload,
    }


def _build_metric_payload(store: dict, platform: str, history_date: str, generated_at: str) -> dict:
    metric_fields = (
        "name",
        "view_count",
        "danmaku_uid_count",
        "favorite_count",
        "subscription_num",
        "reward_num",
        "reward_total",
        "pay_count",
        "diamond_value",
        "cover",
        "maincvs",
        "catalogName",
        "payStatus",
        "createTime",
        "updated_at",
        "fetched_at",
    )
    dramas: dict[str, dict] = {}
    for drama_id, entry in (store.get(platform, {}).get("dramas") or {}).items():
        if not isinstance(entry, dict):
            continue
        dramas[str(drama_id)] = {
            field: entry.get(field)
            for field in metric_fields
            if field in entry
        }
    return {
        "version": 1,
        "date": history_date,
        "platform": platform,
        "generated_at": generated_at,
        "dramas": dramas,
    }


TREND_METRIC_FIELDS = (
    "view_count",
    "danmaku_uid_count",
    "favorite_count",
    "subscription_num",
    "reward_num",
    "reward_total",
    "pay_count",
    "diamond_value",
)

TREND_DRAMA_FIELDS = (
    "cover",
    "maincvs",
    "catalogName",
    "payStatus",
    "createTime",
    "updated_at",
)

DANMAKU_REPAIR_COPY_FIELDS = (
    "name",
    "view_count",
    "favorite_count",
    "subscription_num",
    "reward_num",
    "reward_total",
    "pay_count",
    "diamond_value",
    "cover",
    "maincvs",
    "catalogName",
    "payStatus",
    "createTime",
    "updated_at",
    "fetched_at",
)


def _trend_metrics_from_entry(entry: dict) -> dict:
    return {
        field: entry.get(field)
        for field in TREND_METRIC_FIELDS
        if field in entry and entry.get(field) is not None
    }


def _copy_trend_drama_fields(target: dict, source: dict) -> None:
    for field in TREND_DRAMA_FIELDS:
        value = source.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, list) and not value:
            continue
        target[field] = value


def _rank_item_drama_ids(item: object) -> list[str]:
    if not isinstance(item, dict):
        return [str(item)] if item not in (None, "") else []
    ids = item.get("dramaIds") or item.get("drama_ids")
    if ids:
        return [str(value) for value in ids if value not in (None, "")]
    single = item.get("dramaId") or item.get("drama_id") or item.get("id")
    return [str(single)] if single not in (None, "") else []


def _rank_badges_by_drama(platform: str, list_payload: dict | None) -> dict[str, list[dict]]:
    if not isinstance(list_payload, dict):
        return {}
    ranks = list_payload.get("ranks")
    if not isinstance(ranks, dict):
        return {}
    badges: dict[str, list[dict]] = {}
    for rank_key, rank in ranks.items():
        rank_key_text = str(rank_key)
        if platform == "missevan" and rank_key_text == "peak":
            continue
        if not isinstance(rank, dict):
            continue
        rank_name = str(rank.get("name") or rank_key_text)
        for fallback_position, item in enumerate(rank.get("items") or [], 1):
            if not isinstance(item, dict):
                continue
            position = item.get("position") or fallback_position
            badge = {
                "key": rank_key_text,
                "name": rank_name,
                "position": safe_int(position, fallback_position),
            }
            for drama_id in _rank_item_drama_ids(item):
                badges.setdefault(drama_id, []).append(dict(badge))
    return badges


def build_rank_trend_payload(
    current: dict | None,
    platform: str,
    history_date: str,
    metrics_payload: dict | None,
    list_payload: dict | None,
    *,
    generated_at: str,
    pruned_dates: list[str] | tuple[str, ...] | set[str] = (),
) -> dict:
    if platform not in TREND_KEYS:
        raise ValueError(f"Unsupported platform: {platform}")

    payload = current if isinstance(current, dict) else {}
    pruned = {str(value) for value in pruned_dates if value not in (None, "")}
    dates = {
        str(value)
        for value in (payload.get("dates") or [])
        if value not in (None, "") and str(value) not in pruned
    }

    dramas: dict[str, dict] = {}
    for drama_id, entry in (payload.get("dramas") or {}).items():
        if not isinstance(entry, dict):
            continue
        samples = {
            str(date_key): sample
            for date_key, sample in (entry.get("samples") or {}).items()
            if str(date_key) not in pruned
        }
        if not samples:
            continue
        drama_id_text = str(entry.get("id") or drama_id)
        copied = {
            "id": drama_id_text,
            "name": str(entry.get("name") or drama_id_text),
            "samples": samples,
        }
        _copy_trend_drama_fields(copied, entry)
        dramas[drama_id_text] = copied

    metric_dramas = metrics_payload.get("dramas") if isinstance(metrics_payload, dict) else None
    if isinstance(metric_dramas, dict) and metric_dramas:
        rank_badges = _rank_badges_by_drama(platform, list_payload)
        sample_generated_at = str(
            (metrics_payload or {}).get("generated_at")
            or (list_payload or {}).get("generated_at")
            or generated_at
        )
        dates.add(history_date)
        for drama_id, metric_entry in metric_dramas.items():
            if not isinstance(metric_entry, dict):
                continue
            drama_id_text = str(drama_id)
            entry = dramas.get(
                drama_id_text,
                {
                    "id": drama_id_text,
                    "name": str(metric_entry.get("name") or drama_id_text),
                    "samples": {},
                },
            )
            entry["id"] = drama_id_text
            entry["name"] = str(metric_entry.get("name") or entry.get("name") or drama_id_text)
            _copy_trend_drama_fields(entry, metric_entry)
            entry.setdefault("samples", {})
            entry["samples"][history_date] = {
                "generated_at": sample_generated_at,
                "metrics": _trend_metrics_from_entry(metric_entry),
                "ranks": rank_badges.get(drama_id_text, []),
            }
            dramas[drama_id_text] = entry

    dates = {
        str(sample_date)
        for entry in dramas.values()
        for sample_date in (entry.get("samples") or {})
    }
    kept_dates = set(sorted(dates)[-RANK_TREND_RETENTION_DATES:])
    for drama_id, entry in list(dramas.items()):
        entry["samples"] = {
            sample_date: sample
            for sample_date, sample in entry["samples"].items()
            if sample_date in kept_dates
        }
        if not entry["samples"]:
            dramas.pop(drama_id)

    return {
        "version": 1,
        "platform": platform,
        "updated_at": generated_at,
        "dates": sorted(kept_dates),
        "dramas": dramas,
    }


def upload_rank_trend_snapshot(
    platform: str,
    history_date: str,
    metrics_payload: dict | None,
    list_payload: dict | None,
    *,
    generated_at: str,
    pruned_dates: list[str] | tuple[str, ...] | set[str] = (),
) -> dict:
    key = TREND_KEYS[platform]
    current = _load_upstash_json_strict(key)
    payload = build_rank_trend_payload(
        current if isinstance(current, dict) else None,
        platform,
        history_date,
        metrics_payload if isinstance(metrics_payload, dict) else None,
        list_payload if isinstance(list_payload, dict) else None,
        generated_at=generated_at,
        pruned_dates=pruned_dates,
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    result = upstash_request(["SET", key, encoded])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {key}: {result!r}")
    print(f"[ok] uploaded {key} ({len(encoded)} bytes, date={history_date})")
    return payload


def build_missevan_peak_trend_payload(
    current: dict | None,
    store: dict,
    history_date: str,
    generated_at: str,
    *,
    pruned_dates: list[str] | tuple[str, ...] | set[str],
) -> dict:
    """Merge one Missevan peak rank snapshot into the long-lived trend payload."""
    payload = current if isinstance(current, dict) else {}
    dates = {str(value) for value in (payload.get("dates") or []) if value not in (None, "")}
    pruned = {str(value) for value in pruned_dates if value not in (None, "")}
    dates.difference_update(pruned)
    dates.add(history_date)

    series_payload: dict[str, dict] = {}
    for name, entry in (payload.get("series") or {}).items():
        if not isinstance(entry, dict):
            continue
        samples = {
            str(date_key): sample
            for date_key, sample in (entry.get("samples") or {}).items()
            if str(date_key) not in pruned
        }
        if not samples:
            continue
        copied = dict(entry)
        copied["name"] = str(copied.get("name") or name)
        copied["samples"] = samples
        series_payload[str(name)] = copied

    peak_rank = (store.get("missevan", {}).get("ranks") or {}).get("peak") or {}
    fetched_at = peak_rank.get("fetched_at") or generated_at
    for position, item in enumerate(peak_rank.get("items") or [], 1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        entry = series_payload.get(name, {"name": name, "samples": {}})
        entry["name"] = name
        entry["dramaIds"] = [str(value) for value in (item.get("dramaIds") or item.get("drama_ids") or [])]
        entry["cvs"] = [str(value) for value in (item.get("cvs") or [])]
        entry["cover"] = item.get("cover", "")
        view_count = item.get("view_count")
        entry.setdefault("samples", {})
        entry["samples"][history_date] = {
            "view_count": None if view_count is None else safe_int(view_count),
            "position": position,
            "fetched_at": fetched_at,
        }
        series_payload[name] = entry

    kept_dates = set(sorted(dates)[-RANK_TREND_RETENTION_DATES:])
    for name, entry in list(series_payload.items()):
        entry["samples"] = {
            sample_date: sample
            for sample_date, sample in (entry.get("samples") or {}).items()
            if sample_date in kept_dates
        }
        if not entry["samples"]:
            series_payload.pop(name)

    return {
        "version": 1,
        "platform": "missevan",
        "rank": "peak",
        "metric": "view_count",
        "updated_at": generated_at,
        "dates": sorted(kept_dates),
        "series": series_payload,
    }


def upload_missevan_peak_trend(
    store: dict,
    *,
    history_date: str,
    generated_at: str,
    pruned_dates: list[str] | tuple[str, ...] | set[str] = (),
) -> dict:
    current = _load_upstash_json(PEAK_TREND_KEY)
    payload = build_missevan_peak_trend_payload(
        current if isinstance(current, dict) else None,
        store,
        history_date,
        generated_at,
        pruned_dates=pruned_dates,
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    result = upstash_request(["SET", PEAK_TREND_KEY, encoded])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {PEAK_TREND_KEY}: {result!r}")
    print(f"[ok] uploaded {PEAK_TREND_KEY} ({len(encoded)} bytes, date={history_date})")
    return payload


def _history_date_from_store_meta(store: dict) -> tuple[str, str]:
    updated_at = str((store.get("_meta") or {}).get("updated_at") or now_iso())
    try:
        parsed = datetime.fromisoformat(updated_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TIMEZONE).date().isoformat(), updated_at
    except ValueError:
        return updated_at[:10], updated_at


def backfill_missevan_peak_trend_from_latest() -> str:
    latest = _load_upstash_json("ranks:latest")
    if not isinstance(latest, dict):
        raise RuntimeError("Unable to load ranks:latest for peak trend backfill.")
    history_date, generated_at = _history_date_from_store_meta(latest)
    upload_missevan_peak_trend(
        latest,
        history_date=history_date,
        generated_at=generated_at,
        pruned_dates=(),
    )
    return history_date


def build_rank_snapshot_payloads(
    store: dict,
    *,
    platforms: tuple[str, ...] | list[str] = PLATFORMS,
    history_date: str | None = None,
    generated_at: str | None = None,
) -> dict[str, dict[str, dict]]:
    generated = generated_at or now_iso()
    if history_date is None:
        history_date = generated[:10]
    payloads: dict[str, dict[str, dict]] = {}
    for platform in platforms:
        payloads[platform] = {
            "list": _build_rank_list_payload(store, platform, history_date, generated),
            "metrics": _build_metric_payload(store, platform, history_date, generated),
        }
    return payloads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_stale(fetched_at: str | None, force: bool) -> bool:
    """Return True if the entry should be refreshed."""
    if force or not fetched_at:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
        return datetime.now(timezone.utc) - fetched > CACHE_WINDOW
    except (ValueError, TypeError):
        return True


def select_stale_ids(drama_ids: set[str], existing_dramas: dict, *, force: bool) -> tuple[set[str], int]:
    to_update: set[str] = set()
    skipped = 0
    for did in drama_ids:
        drama_id = str(did)
        existing = existing_dramas.get(drama_id, {}) if isinstance(existing_dramas, dict) else {}
        if is_stale(existing.get("fetched_at") if isinstance(existing, dict) else None, force):
            to_update.add(drama_id)
        else:
            skipped += 1
    return to_update, skipped


def should_refresh_only_danmaku_entry(entry: dict | None, *, force: bool) -> bool:
    """Return True when only-danmaku mode should refresh one existing metric entry."""
    if force:
        return True
    if not isinstance(entry, dict):
        return True

    count_value = entry.get("danmaku_uid_count")
    if count_value in (None, ""):
        return True
    try:
        if int(count_value) <= 0:
            return True
    except (TypeError, ValueError):
        return True

    return is_stale(entry.get("fetched_at"), False)


def sanitize_rank_store(store: dict) -> dict:
    manbo_dramas = (store.get("manbo") or {}).get("dramas")
    if isinstance(manbo_dramas, dict):
        for entry in manbo_dramas.values():
            if isinstance(entry, dict):
                entry.pop("danmaku_paid_episode_count", None)
    return store


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def retry_failed_danmaku_ids(
    platform: str,
    failed_ids: set[str] | list[str],
    refresh_one,
    *,
    mark_failed=None,
    max_attempts: int = DANMAKU_DRAMA_RETRY_ATTEMPTS,
) -> set[str]:
    """Retry failed drama-level danmaku refreshes and return IDs still failing."""
    remaining = {str(value) for value in failed_ids if value not in (None, "")}
    for attempt in range(1, max_attempts + 1):
        if not remaining:
            break
        print(f"  [{platform}] danmaku retry {attempt}/{max_attempts}: {len(remaining)} drama(s)")
        next_failed: set[str] = set()
        for idx, drama_id in enumerate(sorted(remaining), 1):
            try:
                refresh_one(drama_id)
            except RuntimeError as exc:
                if "HTTP_418" in str(exc):
                    raise
                print(f"  [{platform}] retry {attempt}/{max_attempts} ERROR on {drama_id}: {exc}")
                next_failed.add(drama_id)
            except Exception as exc:
                print(f"  [{platform}] retry {attempt}/{max_attempts} ERROR on {drama_id}: {exc}")
                next_failed.add(drama_id)
            else:
                print(f"  [{platform}] retry {attempt}/{max_attempts} ({idx}/{len(remaining)}) drama {drama_id}: ok")
        remaining = next_failed
    if remaining:
        print(f"  [{platform}] WARN: danmaku still failed after {max_attempts} retries: {', '.join(sorted(remaining))}")
        if mark_failed is not None:
            for drama_id in sorted(remaining):
                mark_failed(drama_id)
    return remaining


def manbo_danmaku_count_too_low(
    previous_count: object,
    next_count: object,
    *,
    ratio: float = MANBO_DANMAKU_LOW_VALUE_RATIO,
) -> bool:
    try:
        previous = int(previous_count)
        current = int(next_count)
    except (TypeError, ValueError):
        return False
    if previous <= 0 or current < 0:
        return False
    return current < previous * (1 - ratio)


def assign_manbo_danmaku_uid_count(entry: dict, drama_id: str, uid_count: int) -> None:
    previous = entry.get("danmaku_uid_count")
    if manbo_danmaku_count_too_low(previous, uid_count):
        raise DanmakuRefreshError(
            f"low Manbo danmaku count for {drama_id}: previous={previous}, current={uid_count}, "
            f"threshold={MANBO_DANMAKU_LOW_VALUE_RATIO:.0%}"
        )
    entry["danmaku_uid_count"] = uid_count


def init_ranks_store() -> dict:
    return {
        "_meta": {"updated_at": now_iso()},
        "missevan": {"ranks": {}, "dramas": {}},
        "manbo": {"ranks": {}, "dramas": {}},
    }


def _rank_item_drama_id(item: object) -> str | None:
    if isinstance(item, dict):
        value = item.get("dramaId") or item.get("drama_id") or item.get("id")
    else:
        value = item
    if value in (None, ""):
        return None
    return str(value)


def collect_missevan_danmaku_target_ids(store: dict) -> set[str]:
    """Collect Missevan rank drama IDs whose paid danmaku UID counts should refresh."""
    targets: set[str] = set()
    ranks = store.get("missevan", {}).get("ranks") or {}
    for rank_key, rank in ranks.items():
        if rank_key == "peak":
            continue
        for item in rank.get("items") or []:
            drama_id = _rank_item_drama_id(item)
            if drama_id:
                targets.add(drama_id)
    return targets


def collect_manbo_danmaku_target_ids(store: dict) -> set[str]:
    """Collect Manbo rank drama IDs whose paid danmaku UID counts should refresh."""
    targets: set[str] = set()
    ranks = store.get("manbo", {}).get("ranks") or {}
    for rank_key, rank in ranks.items():
        if rank_key == "peak":
            continue
        for item in rank.get("items") or []:
            drama_id = _rank_item_drama_id(item)
            if drama_id:
                targets.add(drama_id)
    return targets


def extract_ongoing_ids(payload: object) -> set[str]:
    """Extract drama IDs from an ongoing payload."""
    if not isinstance(payload, dict):
        return set()
    records = payload.get("records")
    if not isinstance(records, dict):
        return set()
    ids: set[str] = set()
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        value = record.get("dramaId")
        if value in (None, ""):
            value = key
        if value not in (None, ""):
            ids.add(str(value))
    return ids


def load_ongoing_drama_ids(platform: str) -> set[str]:
    """Load ongoing drama IDs for one platform from Upstash."""
    key = ONGOING_KEYS[platform]
    return extract_ongoing_ids(_load_upstash_json(key))


def merge_rank_and_ongoing_ids(rank_ids, ongoing_ids) -> set[str]:
    """Merge rank-selected and ongoing drama IDs, coercing values to strings."""
    merged: set[str] = set()
    for value in list(rank_ids or []) + list(ongoing_ids or []):
        if value not in (None, ""):
            merged.add(str(value))
    return merged


# ---------------------------------------------------------------------------
# Phase 2: Fetch rank lists
# ---------------------------------------------------------------------------

def fetch_missevan_ranks(requester: MissevanRequester, store: dict) -> tuple[set[str], set[str]]:
    """Fetch all Missevan rank lists, updating store. Return (all drama IDs, danmaku-eligible IDs)."""
    all_ids: set[str] = set()
    danmaku_ids: set[str] = set()
    ranks = store["missevan"].setdefault("ranks", {})

    # Standard ranks
    for key, (type_val, sub_type, name) in MISSEVAN_RANKS.items():
        url = f"https://www.missevan.com/rank/details?page_size=30&page=1&type={type_val}&sub_type={sub_type}"
        print(f"  [missevan] fetching rank: {name} ...")
        try:
            data = requester.request_json(url)
        except Exception as exc:
            print(f"  [missevan] WARN: failed to fetch {name}: {exc}")
            continue
        info = data.get("info") or {}
        items_raw = info.get("data") or []
        items = [item["id"] for item in items_raw if "id" in item]
        ranks[key] = {"name": name, "fetched_at": now_iso(), "items": items}
        all_ids.update(str(i) for i in items)
        danmaku_ids.update(str(i) for i in items)
        print(f"  [missevan] {name}: {len(items)} items")

    # Peak rank
    print("  [missevan] fetching rank: 巅峰榜 ...")
    try:
        data = requester.request_json("https://www.missevan.com/x/rank/peak-details")
        outer_data = data.get("data") or {}
        inner_list = outer_data.get("data") or []
        # Each inner_list item has "elements" array
        elements = []
        for group in inner_list:
            elements.extend(group.get("elements") or [])
        # Hardcoded overrides for peak dramaIds
        PEAK_DRAMAID_OVERRIDES: dict[str, list[str]] = {
            "二哈和他的白猫师尊广播剧（翼之声中文配音社团）": ["20741"],
            "娘娘腔（贾诩、苏莫离）": ["19255"],
            "错撩": ["52355"],
        }
        # Build series title -> dramaIds lookup for 猫耳 platform
        series_info = load_series_info()
        missevan_series: dict[str, list[str]] = {}
        for entry in series_info.values():
            if entry.get("platform") == "猫耳":
                missevan_series[entry.get("series title", "")] = entry.get("dramaIds", [])
        missevan_series.update(PEAK_DRAMAID_OVERRIDES)
        # Collect names not matched in drama-series-info
        unmatched_names: set[str] = set()
        for el in elements:
            name = el.get("name", "")
            if name and name not in missevan_series:
                unmatched_names.add(name)
        # Fallback: search upstash missevan:info:v1 for unmatched names
        upstash_series: dict[str, list[str]] = {}
        if unmatched_names:
            print(f"  [missevan] 巅峰榜: {len(unmatched_names)} names not in drama-series-info, searching upstash ...")
            missevan_info = _load_upstash_json("missevan:info:v1") or {}
            for drama_id, node in missevan_info.items():
                title = node.get("title", "")
                if title in unmatched_names:
                    upstash_series.setdefault(title, []).append(str(drama_id))
        peak_items = []
        for el in elements:
            name = el.get("name", "")
            cvs = [cv.get("name", "") for cv in (el.get("cvs") or []) if cv.get("name")]
            drama_ids = missevan_series.get(name) or upstash_series.get(name, [])
            peak_items.append({
                "name": name,
                "view_count": el.get("view_count", 0),
                "cover": el.get("cover", ""),
                "cvs": cvs,
                "dramaIds": drama_ids,
            })
        ranks["peak"] = {"name": "巅峰榜", "fetched_at": now_iso(), "items": peak_items}
        print(f"  [missevan] 巅峰榜: {len(peak_items)} items")
    except Exception as exc:
        print(f"  [missevan] WARN: failed to fetch 巅峰榜: {exc}")

    return all_ids, danmaku_ids


def fetch_manbo_ranks(store: dict) -> set[str]:
    """Fetch all Manbo rank lists, updating store. Return set of drama IDs."""
    all_ids: set[str] = set()
    ranks = store["manbo"].setdefault("ranks", {})

    for key, (rank_id, name, limit, value_field) in MANBO_RANKS.items():
        url = f"https://api.kilamanbo.com/api/v530/rank/drama/common/detail?rankId={rank_id}"
        print(f"  [manbo] fetching rank: {name} ...")
        try:
            data = request_manbo_json(url)
        except Exception as exc:
            print(f"  [manbo] WARN: failed to fetch {name}: {exc}")
            continue
        body = data.get("b") or data.get("data") or {}
        drama_list = body.get("radioDramaRespList") or []
        if limit is not None:
            drama_list = drama_list[:limit]
        unit_name = body.get("unitName", "")
        items = []
        for d in drama_list:
            drama_id = str(d.get("radioDramaIdStr") or d.get("radioDramaId", ""))
            val = d.get(value_field, 0)
            if drama_id:
                items.append({"dramaId": drama_id, value_field: val})
                all_ids.add(drama_id)
        ranks[key] = {"name": name, "rankId": rank_id, "unitName": unit_name, "fetched_at": now_iso(), "items": items}
        print(f"  [manbo] {name}: {len(items)} items")

    return all_ids


# ---------------------------------------------------------------------------
# Phase 4: Missevan drama detail collection
# ---------------------------------------------------------------------------

def fetch_missevan_drama_details(
    requester: MissevanRequester,
    drama_ids: set[str],
    store: dict,
    *,
    skip_danmaku: bool,
    danmaku_ids: set[str] | None = None,
) -> None:
    """Fetch detailed info for each Missevan drama ID."""
    dramas = store["missevan"].setdefault("dramas", {})
    total = len(drama_ids)
    failed_danmaku_ids: set[str] = set()
    for idx, drama_id in enumerate(sorted(drama_ids), 1):
        print(f"  [missevan] ({idx}/{total}) drama {drama_id} ...")
        entry: dict = dramas.get(str(drama_id), {})
        should_skip_dm = skip_danmaku or (danmaku_ids is not None and str(drama_id) not in danmaku_ids)
        try:
            _fetch_one_missevan(
                requester,
                str(drama_id),
                entry,
                skip_danmaku=should_skip_dm,
                clear_danmaku_on_skip=skip_danmaku,
            )
        except RuntimeError as exc:
            if "HTTP_418" in str(exc):
                print(f"  [missevan] FATAL: rate limited (418). Saving progress and stopping.")
                dramas[str(drama_id)] = entry
                save_json(RANKS_PATH, store)
                raise
            if isinstance(exc, DanmakuRefreshError):
                failed_danmaku_ids.add(str(drama_id))
            print(f"  [missevan] ERROR on {drama_id}: {exc}")
        except Exception as exc:
            print(f"  [missevan] ERROR on {drama_id}: {exc}")
        dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    def retry_one_danmaku(drama_id: str) -> None:
        entry = dramas.get(str(drama_id), {})
        try:
            _fetch_one_missevan(
                requester,
                str(drama_id),
                entry,
                skip_danmaku=False,
                clear_danmaku_on_skip=False,
            )
        finally:
            dramas[str(drama_id)] = entry
            save_json(RANKS_PATH, store)

    def mark_danmaku_failed(drama_id: str) -> None:
        entry = dramas.get(str(drama_id), {})
        entry["danmaku_uid_count"] = None
        dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    retry_failed_danmaku_ids(
        "missevan",
        failed_danmaku_ids,
        retry_one_danmaku,
        mark_failed=mark_danmaku_failed,
    )


def _fetch_one_missevan(
    requester: MissevanRequester,
    drama_id: str,
    entry: dict,
    *,
    skip_danmaku: bool,
    clear_danmaku_on_skip: bool,
) -> None:
    # Basic drama info
    url = f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}"
    data = requester.request_json(url)
    info = data.get("info") or {}
    drama = info.get("drama") or {}
    episodes_section = info.get("episodes") or {}
    episodes = episodes_section.get("episode") or []

    entry["name"] = drama.get("name", entry.get("name", ""))
    entry["cover"] = drama.get("cover", entry.get("cover", ""))
    entry["view_count"] = drama.get("view_count", 0)

    # Reward detail
    try:
        reward_url = f"https://www.missevan.com/reward/drama-reward-detail?drama_id={drama_id}"
        reward_data = requester.request_json(reward_url)
        reward_info = reward_data.get("info") or {}
        entry["reward_num"] = int(reward_info.get("reward_num") or reward_info.get("data", {}).get("reward_num") or 0)
    except Exception:
        entry.setdefault("reward_num", 0)

    # Reward total (coin sum)
    try:
        rank_url = f"https://www.missevan.com/reward/user-reward-rank?period=3&drama_id={drama_id}"
        rank_data = requester.request_json(rank_url)
        rank_info = rank_data.get("info") or {}
        rank_list = rank_info.get("list") or rank_info.get("data") or []
        if isinstance(rank_list, dict):
            rank_list = rank_list.get("list") or []
        entry["reward_total"] = sum(int(item.get("coin") or 0) for item in rank_list)
    except Exception:
        entry.setdefault("reward_total", 0)

    # updated_at + subscription_num via getdramabysound
    sound_ids = [str(ep.get("sound_id")) for ep in episodes if ep.get("sound_id")]
    if sound_ids:
        try:
            sound_url = f"https://www.missevan.com/dramaapi/getdramabysound?sound_id={sound_ids[0]}"
            sound_data = requester.request_json(sound_url)
            sound_drama = (sound_data.get("info") or {}).get("drama") or {}
            entry["subscription_num"] = sound_drama.get("subscription_num", 0)
            lastupdate = sound_drama.get("lastupdate_time")
            if lastupdate:
                if isinstance(lastupdate, (int, float)):
                    entry["updated_at"] = datetime.fromtimestamp(lastupdate, tz=timezone.utc).isoformat()
                else:
                    entry["updated_at"] = str(lastupdate)
            else:
                entry.setdefault("updated_at", None)
        except Exception:
            entry.setdefault("subscription_num", 0)
            entry.setdefault("updated_at", None)
    else:
        entry["subscription_num"] = 0
        entry["updated_at"] = None

    # Danmaku UID count
    if skip_danmaku:
        if clear_danmaku_on_skip:
            entry["danmaku_uid_count"] = None
        else:
            entry.setdefault("danmaku_uid_count", None)
    else:
        _fetch_missevan_danmaku(requester, episodes, entry)

    entry["fetched_at"] = now_iso()


def _fetch_missevan_danmaku(requester: MissevanRequester, episodes: list[dict], entry: dict) -> None:
    """Fetch danmaku for paid episodes and count unique UIDs."""
    paid_sounds = []
    for ep in episodes:
        if ep.get("need_pay") in (True, 1, "1") or int(ep.get("price") or 0) > 0:
            sid = ep.get("sound_id")
            if sid:
                paid_sounds.append(str(sid))

    if not paid_sounds:
        entry["danmaku_uid_count"] = 0
        print("    [danmaku] paid_sounds=0 success=0 failed=0 unique_users=0")
        return

    uid_set: set[str] = set()
    failed_sounds: list[str] = []
    success_sounds = 0
    for sound_id in paid_sounds:
        try:
            dm_url = f"https://www.missevan.com/sound/getdm?soundid={sound_id}"
            resp = requests.get(dm_url, headers=MISSEVAN_HEADERS, timeout=30)
            resp.raise_for_status()
            _parse_missevan_dm_xml(resp.text, uid_set)
            success_sounds += 1
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 418:
                raise RuntimeError(f"HTTP_418 while fetching Missevan danmaku sound {sound_id}") from exc
            print(f"    [danmaku] WARN: failed for sound {sound_id}: {exc}")
            failed_sounds.append(sound_id)
        except Exception as exc:
            print(f"    [danmaku] WARN: failed for sound {sound_id}: {exc}")
            failed_sounds.append(sound_id)
        time.sleep(0.35)  # small delay per episode
    print(
        f"    [danmaku] paid_sounds={len(paid_sounds)} success={success_sounds} "
        f"failed={len(failed_sounds)} unique_users={len(uid_set)}"
    )
    if failed_sounds:
        raise DanmakuRefreshError(f"failed Missevan danmaku sounds: {', '.join(failed_sounds)}")
    entry["danmaku_uid_count"] = len(uid_set)


def _parse_missevan_dm_xml(xml_text: str, uid_set: set[str]) -> None:
    """Parse Missevan danmaku XML, extracting unique UIDs (position 6 in p attr)."""
    for match in re.finditer(r'<d p="([^"]+)"', xml_text):
        parts = match.group(1).split(",")
        if len(parts) > 6 and parts[6]:
            uid_set.add(parts[6])


# ---------------------------------------------------------------------------
# Phase 5: Manbo drama detail collection
# ---------------------------------------------------------------------------

def fetch_manbo_drama_details(
    drama_ids: set[str],
    store: dict,
    *,
    skip_danmaku: bool,
    danmaku_ids: set[str] | None = None,
) -> None:
    """Fetch detailed info for each Manbo drama ID."""
    dramas = store["manbo"].setdefault("dramas", {})
    total = len(drama_ids)
    save_counter = 0
    failed_danmaku_ids: set[str] = set()
    for idx, drama_id in enumerate(sorted(drama_ids), 1):
        print(f"  [manbo] ({idx}/{total}) drama {drama_id} ...")
        entry: dict = dramas.get(drama_id, {})
        try:
            _fetch_one_manbo(drama_id, entry)
        except Exception as exc:
            print(f"  [manbo] ERROR on {drama_id}: {exc}")
            entry.pop("danmaku_paid_episode_count", None)
            dramas[drama_id] = entry
            save_counter += 1
            if save_counter >= 5:
                save_json(RANKS_PATH, store)
                save_counter = 0
            continue

        if skip_danmaku:
            entry["danmaku_uid_count"] = None
        elif danmaku_ids is None or drama_id in danmaku_ids:
            try:
                _, uid_count = fetch_one_manbo_danmaku_count(drama_id)
                assign_manbo_danmaku_uid_count(entry, drama_id, uid_count)
            except Exception as exc:
                print(f"  [manbo] DANMAKU ERROR on {drama_id}: {exc}")
                failed_danmaku_ids.add(str(drama_id))
        entry.pop("danmaku_paid_episode_count", None)
        dramas[drama_id] = entry
        save_counter += 1
        if save_counter >= 5:
            save_json(RANKS_PATH, store)
            save_counter = 0
    if save_counter > 0:
        save_json(RANKS_PATH, store)

    def retry_one_danmaku(drama_id: str) -> None:
        entry = dramas.get(str(drama_id), {})
        _, uid_count = fetch_one_manbo_danmaku_count(str(drama_id))
        assign_manbo_danmaku_uid_count(entry, str(drama_id), uid_count)
        entry["fetched_at"] = now_iso()
        entry.pop("danmaku_paid_episode_count", None)
        dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    def mark_danmaku_failed(drama_id: str) -> None:
        entry = dramas.get(str(drama_id), {})
        entry["danmaku_uid_count"] = None
        entry.pop("danmaku_paid_episode_count", None)
        dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    retry_failed_danmaku_ids(
        "manbo",
        failed_danmaku_ids,
        retry_one_danmaku,
        mark_failed=mark_danmaku_failed,
    )


def _fetch_one_manbo(drama_id: str, entry: dict) -> None:
    url = f"https://api.kilamanbo.com/api/v530/radio/drama/detail?radioDramaId={drama_id}"
    data = request_manbo_json(url)
    body = data.get("b") or data.get("data") or {}

    entry["name"] = body.get("title", entry.get("name", ""))
    entry["cover"] = body.get("coverPic") or body.get("largePic") or body.get("cover", entry.get("cover", ""))
    entry["view_count"] = body.get("watchCount", 0)
    entry["favorite_count"] = body.get("favoriteCount", 0)

    # isVIP: whether the drama is a VIP (member) drama
    vip_free = int(body.get("vipFree") or 0)
    entry["isVIP"] = vip_free == 1

    # pay_count: use memberListenCount for member dramas, payCount otherwise
    pay_count = body.get("payCount", 0)
    member_listen = body.get("memberListenCount", 0)
    if vip_free == 1:
        entry["pay_count"] = member_listen
    else:
        entry["pay_count"] = pay_count

    # diamond_value: from v530 radioDramaRankResp.totalDiamond
    rank_resp = body.get("radioDramaRankResp") or {}
    entry["diamond_value"] = rank_resp.get("totalDiamond", 0)

    # updated_at
    update_time = body.get("updateTime")
    if update_time:
        if isinstance(update_time, (int, float)):
            entry["updated_at"] = datetime.fromtimestamp(update_time / 1000, tz=timezone.utc).isoformat()
        else:
            entry["updated_at"] = str(update_time)
    else:
        entry["updated_at"] = None

    entry["fetched_at"] = now_iso()


def is_paid_manbo_episode(episode: dict) -> bool:
    """Return True when a Manbo episode/set requires payment or membership."""
    return (
        safe_int(episode.get("payType") or episode.get("setPayType")) == 1
        or safe_int(episode.get("vipFree")) == 1
        or safe_int(episode.get("price")) > 0
        or safe_int(episode.get("memberPrice") or episode.get("member_price")) > 0
    )


def _extract_manbo_set_id(episode: dict) -> str | None:
    for key in (
        "radioDramaSetIdStr",
        "radioDramaSetId",
        "dramaSetIdStr",
        "dramaSetId",
        "setId",
        "sound_id",
        "id",
    ):
        value = episode.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _extract_manbo_set_list(payload: dict) -> list[dict]:
    for key in ("setRespList", "radioDramaSetRespList", "dramaSetRespList", "sets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fetch_manbo_paid_set_ids(
    drama_id: str,
    *,
    request_json=request_manbo_json,
) -> list[str]:
    """Fetch Manbo drama detail and return paid episode/set IDs."""
    detail_urls = [
        f"https://www.kilamanbo.com/web_manbo/dramaDetail?dramaId={drama_id}",
        f"https://api.kilamanbo.com/api/v530/radio/drama/detail?radioDramaId={drama_id}",
    ]
    for url in detail_urls:
        data = request_json(url)
        payload = data.get("data") or data.get("b") or {}
        episodes = _extract_manbo_set_list(payload)
        paid_ids = []
        for episode in episodes:
            set_id = _extract_manbo_set_id(episode)
            if set_id and is_paid_manbo_episode(episode):
                paid_ids.append(set_id)
        if paid_ids or episodes:
            return list(dict.fromkeys(paid_ids))
    return []


def fetch_manbo_danmaku_users(
    set_id: str,
    *,
    request_json=request_manbo_json,
    page_size: int = MANBO_DANMAKU_PAGE_SIZE,
    page_concurrency: int = MANBO_DANMAKU_PAGE_CONCURRENCY,
    retry_delay: float = 0.5,
) -> set[str]:
    """Fetch Manbo danmaku pages for one episode/set and return unique user eids."""
    def fetch_page(page_no: int) -> tuple[int, set[str]]:
        url = (
            "https://www.kilamanbo.com/web_manbo/getDanmaKuPgList"
            f"?pageSize={page_size}&dramaSetId={set_id}&pageNo={page_no}"
        )
        last_error: Exception | None = None
        for attempt in range(1, MANBO_DANMAKU_REQUEST_RETRIES + 1):
            try:
                data = request_json(url)
                break
            except Exception as exc:
                last_error = exc
                if attempt >= MANBO_DANMAKU_REQUEST_RETRIES:
                    raise
                time.sleep(retry_delay * attempt)
        else:
            raise last_error or RuntimeError("failed to fetch Manbo danmaku page")
        payload = data.get("data") or {}
        entries = payload.get("list") if isinstance(payload, dict) else []
        users = {
            str(item.get("eid"))
            for item in (entries or [])
            if isinstance(item, dict) and item.get("eid") not in (None, "")
        }
        total = safe_int(payload.get("count") if isinstance(payload, dict) else 0, len(users))
        return total, users

    total_count, users = fetch_page(1)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if total_pages <= 1:
        return users

    workers = max(1, min(page_concurrency, total_pages - 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_page, page_no) for page_no in range(2, total_pages + 1)]
        for future in as_completed(futures):
            _, page_users = future.result()
            users.update(page_users)
    return users


def _request_manbo_danmaku_page(
    set_id: str,
    page_no: int,
    *,
    request_json=request_manbo_json,
    page_size: int = MANBO_DANMAKU_PAGE_SIZE,
    retry_delay: float = 0.5,
    short_page_retries: int = MANBO_DANMAKU_SHORT_PAGE_RETRIES,
    short_page_retry_delay: float = MANBO_DANMAKU_SHORT_PAGE_RETRY_DELAY,
    expected_total_count: int | None = None,
) -> dict:
    url = (
        "https://www.kilamanbo.com/web_manbo/getDanmaKuPgList"
        f"?pageSize={page_size}&dramaSetId={set_id}&pageNo={page_no}"
    )
    last_error: Exception | None = None
    max_short_attempts = max(1, short_page_retries)
    for short_attempt in range(1, max_short_attempts + 1):
        data = None
        for attempt in range(1, MANBO_DANMAKU_REQUEST_RETRIES + 1):
            try:
                data = request_json(url)
                break
            except Exception as exc:
                last_error = exc
                if attempt >= MANBO_DANMAKU_REQUEST_RETRIES:
                    raise
                time.sleep(retry_delay * attempt)
        if data is None:
            raise last_error or RuntimeError("failed to fetch Manbo danmaku page")

        total_count, _total_pages, _users, entry_count = _extract_manbo_danmaku_page(data, page_size=page_size)
        expected_count = total_count if expected_total_count is None else expected_total_count
        expected_entries = expected_manbo_danmaku_page_entries(expected_count, page_no, page_size)
        if entry_count >= expected_entries or short_attempt >= max_short_attempts:
            return data
        time.sleep(short_page_retry_delay * short_attempt)

    raise last_error or RuntimeError("failed to fetch Manbo danmaku page")


def expected_manbo_danmaku_page_entries(total_count: int, page_no: int, page_size: int) -> int:
    remaining = max(0, total_count - max(0, page_no - 1) * page_size)
    return min(page_size, remaining)


def _extract_manbo_danmaku_page(data: dict, *, page_size: int) -> tuple[int, int, set[str], int]:
    payload = data.get("data") or {}
    entries = payload.get("list") if isinstance(payload, dict) else []
    users = {
        str(item.get("eid"))
        for item in (entries or [])
        if isinstance(item, dict) and item.get("eid") not in (None, "")
    }
    total_count = safe_int(payload.get("count") if isinstance(payload, dict) else 0, len(users))
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    entry_count = len(entries or []) if isinstance(entries, list) else 0
    return total_count, total_pages, users, entry_count


def fetch_manbo_paid_danmaku_benchmark(
    drama_id: str,
    *,
    title: str = "",
    request_json=request_manbo_json,
    paid_set_id_loader=fetch_manbo_paid_set_ids,
    page_size: int = MANBO_DANMAKU_PAGE_SIZE,
    page_concurrency: int = MANBO_DANMAKU_PAGE_CONCURRENCY,
    retry_delay: float = 0.5,
    short_page_retries: int = MANBO_DANMAKU_SHORT_PAGE_RETRIES,
    short_page_retry_delay: float = MANBO_DANMAKU_SHORT_PAGE_RETRY_DELAY,
) -> dict:
    """Fetch all paid Manbo danmaku pages through one bounded queue and globally dedupe eids."""
    started = time.perf_counter()
    paid_set_ids = paid_set_id_loader(drama_id, request_json=request_json)
    users: set[str] = set()
    failed_pages: list[dict[str, object]] = []
    total_pages_by_set: dict[str, int] = {}
    fetched_pages_by_set: dict[str, int] = {}
    total_danmaku_by_set: dict[str, int] = {}
    fetched_danmaku_by_set: dict[str, int] = {}

    def fetch_and_extract(
        set_id: str,
        page_no: int,
        expected_total_count: int | None = None,
    ) -> tuple[str, int, int, int, set[str], int, int]:
        data = _request_manbo_danmaku_page(
            set_id,
            page_no,
            request_json=request_json,
            page_size=page_size,
            retry_delay=retry_delay,
            short_page_retries=short_page_retries,
            short_page_retry_delay=short_page_retry_delay,
            expected_total_count=expected_total_count,
        )
        total_count, total_pages, page_users, entry_count = _extract_manbo_danmaku_page(data, page_size=page_size)
        expected_count = total_count if expected_total_count is None else expected_total_count
        expected_entries = expected_manbo_danmaku_page_entries(expected_count, page_no, page_size)
        return set_id, page_no, total_count, total_pages, page_users, entry_count, expected_entries

    workers = max(1, min(page_concurrency, len(paid_set_ids) or 1))
    first_page_results: list[tuple[str, int, int, int, set[str], int, int]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_page = {
            executor.submit(fetch_and_extract, set_id, 1): (set_id, 1)
            for set_id in paid_set_ids
        }
        for future in as_completed(future_to_page):
            set_id, page_no = future_to_page[future]
            try:
                result = future.result()
            except Exception as exc:
                failed_pages.append({"set_id": set_id, "page_no": page_no, "error": str(exc)})
                continue
            first_page_results.append(result)
            _, _, total_count, total_pages, page_users, entry_count, expected_entries = result
            total_danmaku_by_set[set_id] = total_count
            total_pages_by_set[set_id] = total_pages
            fetched_pages_by_set[set_id] = fetched_pages_by_set.get(set_id, 0) + 1
            fetched_danmaku_by_set[set_id] = fetched_danmaku_by_set.get(set_id, 0) + entry_count
            users.update(page_users)
            if entry_count < expected_entries:
                failed_pages.append({
                    "set_id": set_id,
                    "page_no": page_no,
                    "expected_entries": expected_entries,
                    "actual_entries": entry_count,
                    "error": (
                        "incomplete Manbo danmaku page: "
                        f"page={page_no}, entries={entry_count}/{expected_entries}"
                    ),
                })

    remaining_pages = [
        (set_id, page_no)
        for set_id, total_pages in total_pages_by_set.items()
        for page_no in range(2, total_pages + 1)
    ]
    workers = max(1, min(page_concurrency, len(remaining_pages) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_page = {
            executor.submit(fetch_and_extract, set_id, page_no, total_danmaku_by_set.get(set_id)): (set_id, page_no)
            for set_id, page_no in remaining_pages
        }
        for future in as_completed(future_to_page):
            set_id, page_no = future_to_page[future]
            try:
                result = future.result()
            except Exception as exc:
                failed_pages.append({"set_id": set_id, "page_no": page_no, "error": str(exc)})
                continue
            _, _, _, _, page_users, entry_count, expected_entries = result
            fetched_pages_by_set[set_id] = fetched_pages_by_set.get(set_id, 0) + 1
            fetched_danmaku_by_set[set_id] = fetched_danmaku_by_set.get(set_id, 0) + entry_count
            users.update(page_users)
            if entry_count < expected_entries:
                failed_pages.append({
                    "set_id": set_id,
                    "page_no": page_no,
                    "expected_entries": expected_entries,
                    "actual_entries": entry_count,
                    "error": (
                        "incomplete Manbo danmaku page: "
                        f"page={page_no}, entries={entry_count}/{expected_entries}"
                    ),
                })

    for set_id, total_pages in total_pages_by_set.items():
        fetched_pages = fetched_pages_by_set.get(set_id, 0)
        expected_count = total_danmaku_by_set.get(set_id, 0)
        fetched_count = fetched_danmaku_by_set.get(set_id, 0)
        if fetched_pages < total_pages:
            failed_pages.append({
                "set_id": set_id,
                "page_no": None,
                "error": (
                    "incomplete Manbo danmaku pages: "
                    f"pages={fetched_pages}/{total_pages}, entries={fetched_count}/{expected_count}"
                ),
            })

    elapsed_seconds = time.perf_counter() - started
    return {
        "drama_id": str(drama_id),
        "title": title,
        "paid_set_count": len(paid_set_ids),
        "total_pages": sum(total_pages_by_set.values()),
        "fetched_pages": sum(fetched_pages_by_set.values()),
        "unique_user_count": len(users),
        "failed_page_count": len(failed_pages),
        "failed_pages": failed_pages,
        "page_size": page_size,
        "page_concurrency": page_concurrency,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "elapsed": str(timedelta(seconds=elapsed_seconds)),
        "total_danmaku": sum(total_danmaku_by_set.values()),
        "fetched_danmaku": sum(fetched_danmaku_by_set.values()),
    }


def fetch_one_manbo_danmaku_count(
    drama_id: str,
    *,
    request_json=request_manbo_json,
) -> tuple[str, int]:
    """Return (drama_id, paid_danmaku_uid_count) for one Manbo drama."""
    result = fetch_manbo_paid_danmaku_benchmark(
        drama_id,
        request_json=request_json,
        page_concurrency=MANBO_DANMAKU_PAGE_CONCURRENCY,
    )
    print(
        f"    [danmaku] manbo drama {drama_id}: paid_sets={result.get('paid_set_count', 0)} "
        f"total_pages={result.get('total_pages', 0)} fetched_pages={result.get('fetched_pages', 0)} "
        f"declared_danmaku={result.get('total_danmaku', 0)} fetched_danmaku={result.get('fetched_danmaku', 0)} "
        f"unique_users={result.get('unique_user_count', 0)} failed_pages={result.get('failed_page_count', 0)}"
    )
    failed_pages = result.get("failed_pages") or []
    if failed_pages:
        raise RuntimeError(f"failed Manbo danmaku pages for {drama_id}: {failed_pages!r}")
    return (
        drama_id,
        int(result.get("unique_user_count") or 0),
    )


def fetch_manbo_danmaku_details(
    drama_ids: set[str],
    store: dict,
    *,
    force: bool,
) -> None:
    """Refresh paid danmaku UID counts for selected Manbo dramas."""
    manbo_dramas = store["manbo"].setdefault("dramas", {})
    targets = []
    for drama_id in drama_ids:
        entry = manbo_dramas.get(drama_id, {})
        if should_refresh_only_danmaku_entry(entry, force=force):
            targets.append(drama_id)

    print(f"  [manbo] paid danmaku IDs: total={len(drama_ids)}, update={len(targets)}")
    if not targets:
        return

    save_counter = 0
    failed_danmaku_ids: set[str] = set()
    workers = max(1, min(MANBO_DANMAKU_DRAMA_CONCURRENCY, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_drama = {
            executor.submit(fetch_one_manbo_danmaku_count, drama_id): drama_id
            for drama_id in sorted(targets)
        }
        completed = 0
        for future in as_completed(future_to_drama):
            drama_id = future_to_drama[future]
            completed += 1
            entry = manbo_dramas.get(drama_id, {})
            try:
                _, uid_count = future.result()
                assign_manbo_danmaku_uid_count(entry, drama_id, uid_count)
                entry["fetched_at"] = now_iso()
                print(
                    f"  [manbo] ({completed}/{len(targets)}) drama {drama_id}: "
                    f"{uid_count} paid danmaku IDs"
                )
            except Exception as exc:
                print(f"  [manbo] ({completed}/{len(targets)}) ERROR on {drama_id}: {exc}")
                failed_danmaku_ids.add(str(drama_id))
            entry.pop("danmaku_paid_episode_count", None)
            manbo_dramas[drama_id] = entry
            save_counter += 1
            if save_counter >= 5:
                save_json(RANKS_PATH, store)
                save_counter = 0
    if save_counter > 0:
        save_json(RANKS_PATH, store)

    def retry_one_danmaku(drama_id: str) -> None:
        entry = manbo_dramas.get(str(drama_id), {})
        _, uid_count = fetch_one_manbo_danmaku_count(str(drama_id))
        assign_manbo_danmaku_uid_count(entry, str(drama_id), uid_count)
        entry["fetched_at"] = now_iso()
        entry.pop("danmaku_paid_episode_count", None)
        manbo_dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    def mark_danmaku_failed(drama_id: str) -> None:
        entry = manbo_dramas.get(str(drama_id), {})
        entry["danmaku_uid_count"] = None
        entry.pop("danmaku_paid_episode_count", None)
        manbo_dramas[str(drama_id)] = entry
        save_json(RANKS_PATH, store)

    retry_failed_danmaku_ids(
        "manbo",
        failed_danmaku_ids,
        retry_one_danmaku,
        mark_failed=mark_danmaku_failed,
    )


# ---------------------------------------------------------------------------
# Phase 6: Upstash CV lookup
# ---------------------------------------------------------------------------

def catalog_name_from_missevan(node: dict) -> str | None:
    value = node.get("catalog")
    if value in (None, ""):
        return None
    try:
        return MISSEVAN_CATALOG_NAME_BY_ID.get(int(value))
    except (TypeError, ValueError):
        return None


def catalog_name_from_manbo(record: dict) -> str | None:
    name = str(record.get("catalogName") or "").strip()
    if name:
        return MANBO_CATALOG_NAME_ALIASES.get(name, name)
    value = record.get("catalog")
    if value in (None, ""):
        return None
    try:
        return MANBO_CATALOG_NAME_BY_ID.get(int(value))
    except (TypeError, ValueError):
        return None


def pay_status_from_needpay(value: object) -> str | None:
    if value is True:
        return "付费"
    if value is False:
        return "免费"
    return None


def truthy_member_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def pay_status_from_metadata(source: dict) -> str | None:
    needpay = source.get("needpay")
    if needpay is False:
        return "免费"
    if needpay is True:
        if truthy_member_value(source.get("is_member")) or truthy_member_value(source.get("vipFree")):
            return "会员"
        return "付费"
    return None


def metadata_create_time(source: dict) -> str | None:
    value = str(source.get("createTime") or "").strip()
    return value or None


def update_metadata_fields(entry: dict, *, catalog_name: str | None, pay_status: str | None, create_time: str | None) -> None:
    if catalog_name is not None:
        entry["catalogName"] = catalog_name
    else:
        entry.setdefault("catalogName", None)
    if pay_status is not None:
        entry["payStatus"] = pay_status
    else:
        entry.setdefault("payStatus", None)
    if create_time is not None:
        entry["createTime"] = create_time
    else:
        entry.setdefault("createTime", None)


def manbo_main_cv_names(record: dict) -> list[str] | None:
    names = record.get("mainCvNames") or []
    nicknames = record.get("mainCvNicknames") or []
    count = max(len(names), len(nicknames))
    if count <= 0:
        return None

    resolved: list[str] = []
    for idx in range(count):
        name = normalize(names[idx]) if idx < len(names) else ""
        if not name and idx < len(nicknames):
            name = normalize(nicknames[idx])
        resolved.append(name)
    return resolved or None


def lookup_cvs(store: dict) -> None:
    """Look up main CVs and metadata from Upstash info stores, registering unknown IDs."""
    missevan_dramas = store["missevan"].get("dramas") or {}
    manbo_dramas = store["manbo"].get("dramas") or {}

    if not missevan_dramas and not manbo_dramas:
        print("  [upstash] no dramas to look up CVs for")
        return

    # Load info stores from Upstash
    print("  [upstash] loading missevan:info:v1 ...")
    missevan_info = _load_upstash_json("missevan:info:v1") or {}
    print("  [upstash] loading manbo:info:v1 ...")
    manbo_info_raw = _load_upstash_json("manbo:info:v1") or {}
    manbo_records = manbo_info_raw.get("records") or []
    manbo_by_id: dict[str, dict] = {str(r.get("dramaId", "")): r for r in manbo_records}

    new_missevan: list[str] = []
    new_manbo: list[str] = []

    # Missevan CV lookup
    for drama_id, entry in missevan_dramas.items():
        node = missevan_info.get(str(drama_id))
        if node:
            cvnames = node.get("cvnames") or {}
            maincvs_ids = node.get("maincvs") or []
            entry["maincvs"] = [cvnames.get(str(cid), str(cid)) for cid in maincvs_ids] or None
            update_metadata_fields(
                entry,
                catalog_name=catalog_name_from_missevan(node),
                pay_status=pay_status_from_metadata(node),
                create_time=metadata_create_time(node),
            )
        else:
            entry.setdefault("maincvs", None)
            update_metadata_fields(entry, catalog_name=None, pay_status=None, create_time=None)
            new_missevan.append(str(drama_id))

    # Manbo CV lookup
    for drama_id, entry in manbo_dramas.items():
        record = manbo_by_id.get(drama_id)
        if record:
            entry["maincvs"] = manbo_main_cv_names(record)
            update_metadata_fields(
                entry,
                catalog_name=catalog_name_from_manbo(record),
                pay_status=pay_status_from_metadata(record),
                create_time=metadata_create_time(record),
            )
        else:
            entry.setdefault("maincvs", None)
            update_metadata_fields(entry, catalog_name=None, pay_status=None, create_time=None)
            new_manbo.append(drama_id)

    # Register unknown IDs to queue
    if new_missevan or new_manbo:
        print(f"  [upstash] registering new IDs: missevan={len(new_missevan)}, manbo={len(new_manbo)}")
        try:
            append_new_drama_ids_atomic(new_missevan, new_manbo)
        except Exception as exc:
            print(f"  [upstash] WARN: failed to update queue: {exc}")
    else:
        print("  [upstash] all drama IDs found in info stores")


def _load_upstash_json(key: str) -> dict | list | None:
    try:
        return _load_upstash_json_strict(key)
    except Exception as exc:
        print(f"  [upstash] WARN: failed to load {key}: {exc}")
        return None


def _load_upstash_json_strict(key: str) -> dict | list | None:
    try:
        raw = upstash_request(["GET", key])
        if raw in (None, ""):
            return None
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    except Exception as exc:
        raise RuntimeError(f"Failed to load {key}: {exc}") from exc


# ---------------------------------------------------------------------------
# Null danmaku repair mode
# ---------------------------------------------------------------------------

def is_empty_danmaku_value(value: object) -> bool:
    """Return True for missing/blank danmaku values while preserving valid zero counts."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def resolve_repair_history_date(platform: str) -> str:
    if platform not in TREND_KEYS:
        raise ValueError(f"Unsupported platform: {platform}")
    trend = _load_upstash_json_strict(TREND_KEYS[platform])
    dates = [
        str(value)
        for value in ((trend or {}).get("dates") or [])
        if value not in (None, "")
    ] if isinstance(trend, dict) else []
    if dates:
        return max(dates)
    latest = _load_upstash_json("ranks:latest")
    if isinstance(latest, dict):
        return _history_date_from_store_meta(latest)[0]
    raise RuntimeError(f"No rank trend date or ranks:latest timestamp found for {platform}.")


def _add_repair_source(targets: set[str], sources: dict[str, list[str]], drama_id: str, source: str) -> None:
    targets.add(drama_id)
    source_list = sources.setdefault(drama_id, [])
    if source not in source_list:
        source_list.append(source)


def _entry_has_empty_danmaku(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    if "danmaku_uid_count" not in entry:
        return True
    return is_empty_danmaku_value(entry.get("danmaku_uid_count"))


def _collect_rank_memberships(ranks: object) -> dict[str, set[str]]:
    memberships: dict[str, set[str]] = {}
    if not isinstance(ranks, dict):
        return memberships
    for rank_key, rank in ranks.items():
        if not isinstance(rank, dict):
            continue
        for item in rank.get("items") or []:
            for drama_id in _rank_item_drama_ids(item):
                memberships.setdefault(drama_id, set()).add(str(rank_key))
    return memberships


def _merge_rank_memberships(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for drama_id, rank_keys in source.items():
        target.setdefault(drama_id, set()).update(rank_keys)


def _manbo_rank_memberships_from_payloads(payloads: dict[str, object], history_date: str) -> dict[str, set[str]]:
    memberships: dict[str, set[str]] = {}

    latest = payloads.get("latest")
    latest_manbo = latest.get("manbo") if isinstance(latest, dict) else None
    if isinstance(latest_manbo, dict):
        _merge_rank_memberships(memberships, _collect_rank_memberships(latest_manbo.get("ranks")))

    trend = payloads.get("trend")
    trend_dramas = trend.get("dramas") if isinstance(trend, dict) else None
    if isinstance(trend_dramas, dict):
        for drama_id, entry in trend_dramas.items():
            if not isinstance(entry, dict):
                continue
            samples = entry.get("samples")
            sample = samples.get(history_date) if isinstance(samples, dict) else None
            ranks = sample.get("ranks") if isinstance(sample, dict) else None
            if not isinstance(ranks, list):
                continue
            for rank in ranks:
                if not isinstance(rank, dict):
                    continue
                rank_key = rank.get("key")
                if rank_key not in (None, ""):
                    memberships.setdefault(str(drama_id), set()).add(str(rank_key))

    return memberships


def _drop_manbo_peak_only_targets(
    targets: set[str],
    sources: dict[str, list[str]],
    payloads: dict[str, object],
    history_date: str,
) -> None:
    memberships = _manbo_rank_memberships_from_payloads(payloads, history_date)
    ongoing_ids = extract_ongoing_ids(payloads.get("ongoing"))
    for drama_id, rank_keys in list(memberships.items()):
        if drama_id in targets and drama_id not in ongoing_ids and rank_keys and rank_keys <= {"peak"}:
            targets.discard(drama_id)
            sources.pop(drama_id, None)


def collect_null_danmaku_ids_from_layers(
    platform: str,
    history_date: str,
) -> tuple[set[str], dict[str, list[str]], dict[str, object]]:
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")

    payloads: dict[str, object] = {
        "latest": _load_upstash_json_strict("ranks:latest"),
        "trend": _load_upstash_json_strict(TREND_KEYS[platform]),
    }
    if platform == "manbo":
        payloads["ongoing"] = _load_upstash_json(ONGOING_KEYS[platform])
    targets: set[str] = set()
    sources: dict[str, list[str]] = {}

    latest = payloads.get("latest")
    latest_platform = latest.get(platform) if isinstance(latest, dict) else None
    latest_dramas = latest_platform.get("dramas") if isinstance(latest_platform, dict) else None
    if isinstance(latest_dramas, dict):
        for drama_id, entry in latest_dramas.items():
            if _entry_has_empty_danmaku(entry):
                _add_repair_source(targets, sources, str(drama_id), "latest")

    trend = payloads.get("trend")
    trend_dramas = trend.get("dramas") if isinstance(trend, dict) else None
    if isinstance(trend_dramas, dict):
        for drama_id, entry in trend_dramas.items():
            if not isinstance(entry, dict):
                continue
            samples = entry.get("samples")
            if not isinstance(samples, dict) or history_date not in samples:
                continue
            sample = samples.get(history_date)
            metrics_sample = sample.get("metrics") if isinstance(sample, dict) else None
            if not isinstance(metrics_sample, dict) or "danmaku_uid_count" not in metrics_sample:
                _add_repair_source(targets, sources, str(drama_id), "trend")
            elif is_empty_danmaku_value(metrics_sample.get("danmaku_uid_count")):
                _add_repair_source(targets, sources, str(drama_id), "trend")

    if platform == "manbo":
        _drop_manbo_peak_only_targets(targets, sources, payloads, history_date)

    return targets, sources, payloads


def _payload_drama_entry(payloads: dict[str, object], layer: str, platform: str, drama_id: str) -> dict | None:
    payload = payloads.get(layer)
    if layer == "latest" and isinstance(payload, dict):
        platform_payload = payload.get(platform)
        dramas = platform_payload.get("dramas") if isinstance(platform_payload, dict) else None
        entry = dramas.get(drama_id) if isinstance(dramas, dict) else None
        return entry if isinstance(entry, dict) else None
    if layer == "trend" and isinstance(payload, dict):
        dramas = payload.get("dramas")
        entry = dramas.get(drama_id) if isinstance(dramas, dict) else None
        return entry if isinstance(entry, dict) else None
    return None


def _source_metric_entry(payloads: dict[str, object], platform: str, history_date: str, drama_id: str) -> dict:
    merged: dict[str, object] = {}
    for layer in ("latest", "trend"):
        entry = _payload_drama_entry(payloads, layer, platform, drama_id)
        if not isinstance(entry, dict):
            continue
        if layer == "trend":
            for field in ("name", "cover", "maincvs", "catalogName", "payStatus", "createTime", "updated_at"):
                value = entry.get(field)
                if value not in (None, "") and field not in merged:
                    merged[field] = value
            samples = entry.get("samples")
            sample = samples.get(history_date) if isinstance(samples, dict) else None
            sample_metrics = sample.get("metrics") if isinstance(sample, dict) else None
            if isinstance(sample_metrics, dict):
                for field in DANMAKU_REPAIR_COPY_FIELDS:
                    value = sample_metrics.get(field)
                    if value not in (None, "") and field not in merged:
                        merged[field] = value
            continue
        for field in DANMAKU_REPAIR_COPY_FIELDS:
            value = entry.get(field)
            if value not in (None, "") and field not in merged:
                merged[field] = value
    merged.setdefault("name", drama_id)
    return merged


def _ensure_repair_latest_payload(payload: object, generated_at: str) -> dict:
    result = payload if isinstance(payload, dict) else {}
    result.setdefault("_meta", {})
    if isinstance(result["_meta"], dict):
        result["_meta"]["updated_at"] = generated_at
    for platform in PLATFORMS:
        platform_payload = result.get(platform)
        if not isinstance(platform_payload, dict):
            platform_payload = {}
            result[platform] = platform_payload
        platform_payload.setdefault("ranks", {})
        platform_payload.setdefault("dramas", {})
        if not isinstance(platform_payload["dramas"], dict):
            platform_payload["dramas"] = {}
    return result


def _ensure_repair_trend_payload(payload: object, platform: str, history_date: str, generated_at: str) -> dict:
    result = payload if isinstance(payload, dict) else {}
    result["version"] = result.get("version") or 1
    result["platform"] = result.get("platform") or platform
    result["updated_at"] = generated_at
    dates = [str(value) for value in (result.get("dates") or []) if value not in (None, "")]
    dates.append(history_date)
    result["dates"] = sorted(set(dates))
    result.setdefault("dramas", {})
    if not isinstance(result["dramas"], dict):
        result["dramas"] = {}
    return result


def write_repaired_danmaku_layers(
    platform: str,
    history_date: str,
    repaired_counts: dict[str, int],
    loaded_payloads: dict[str, object],
) -> None:
    if not repaired_counts:
        return

    generated_at = now_iso()
    latest_payload = _ensure_repair_latest_payload(loaded_payloads.get("latest"), generated_at)
    trend_payload = _ensure_repair_trend_payload(
        loaded_payloads.get("trend"),
        platform,
        history_date,
        generated_at,
    )

    latest_dramas = latest_payload[platform]["dramas"]
    trend_dramas = trend_payload["dramas"]

    for drama_id, count in sorted(repaired_counts.items()):
        source_entry = _source_metric_entry(loaded_payloads, platform, history_date, drama_id)

        latest_entry = latest_dramas.setdefault(drama_id, dict(source_entry))
        if isinstance(latest_entry, dict):
            for field, value in source_entry.items():
                latest_entry.setdefault(field, value)
            latest_entry["danmaku_uid_count"] = count
            latest_entry["fetched_at"] = generated_at

        trend_entry = trend_dramas.setdefault(drama_id, {"id": drama_id, "name": source_entry.get("name") or drama_id})
        if isinstance(trend_entry, dict):
            trend_entry["id"] = str(trend_entry.get("id") or drama_id)
            trend_entry["name"] = str(source_entry.get("name") or trend_entry.get("name") or drama_id)
            _copy_trend_drama_fields(trend_entry, source_entry)
            samples = trend_entry.setdefault("samples", {})
            if not isinstance(samples, dict):
                samples = {}
                trend_entry["samples"] = samples
            sample = samples.setdefault(history_date, {"metrics": {}, "ranks": []})
            if not isinstance(sample, dict):
                sample = {"metrics": {}, "ranks": []}
                samples[history_date] = sample
            sample.setdefault("ranks", [])
            sample["generated_at"] = generated_at
            sample_metrics = sample.setdefault("metrics", {})
            if not isinstance(sample_metrics, dict):
                sample_metrics = {}
                sample["metrics"] = sample_metrics
            for field in TREND_METRIC_FIELDS:
                if field in source_entry and field not in sample_metrics:
                    sample_metrics[field] = source_entry[field]
            sample_metrics["danmaku_uid_count"] = count

    writes = (
        ("ranks:latest", latest_payload),
        (TREND_KEYS[platform], trend_payload),
    )
    for key, payload in writes:
        encoded = json.dumps(payload, ensure_ascii=False)
        result = upstash_request(["SET", key, encoded])
        if result != "OK":
            raise RuntimeError(f"Failed to upload {key}: {result!r}")
        print(f"[ok] repaired {key} ({len(encoded)} bytes)")


def fetch_one_missevan_danmaku_count(drama_id: str, requester: MissevanRequester | None = None) -> tuple[str, int]:
    active_requester = requester or MissevanRequester()
    url = f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}"
    data = active_requester.request_json(url)
    info = data.get("info") or {}
    episodes = (info.get("episodes") or {}).get("episode") or []
    entry: dict = {}
    _fetch_missevan_danmaku(active_requester, episodes, entry)
    return str(drama_id), safe_int(entry.get("danmaku_uid_count"))


def _repair_one_danmaku(platform: str, drama_id: str, requester: MissevanRequester | None = None) -> tuple[str, int]:
    if platform == "missevan":
        return fetch_one_missevan_danmaku_count(drama_id, requester)
    if platform == "manbo":
        return fetch_one_manbo_danmaku_count(drama_id)
    raise ValueError(f"Unsupported platform: {platform}")


def repair_null_danmaku_for_platform(
    platform: str,
    history_date: str,
    *,
    attempts: int = DANMAKU_DRAMA_RETRY_ATTEMPTS,
    dry_run: bool = False,
) -> dict[str, object]:
    targets, sources, payloads = collect_null_danmaku_ids_from_layers(platform, history_date)
    sorted_targets = sorted(targets)
    print(f"[repair-null-danmaku] {platform} date={history_date} targets={len(sorted_targets)}")
    for drama_id in sorted_targets:
        print(f"  [{platform}] target {drama_id}: sources={','.join(sources.get(drama_id, []))}")

    if dry_run or not sorted_targets:
        if not sorted_targets:
            print(f"  [{platform}] no empty danmaku entries")
        return {"platform": platform, "date": history_date, "targets": sorted_targets, "repaired": {}, "failed": []}

    requester = MissevanRequester() if platform == "missevan" else None
    repaired: dict[str, int] = {}
    failed: set[str] = set()
    for idx, drama_id in enumerate(sorted_targets, 1):
        try:
            _, count = _repair_one_danmaku(platform, drama_id, requester)
            repaired[drama_id] = count
            print(f"  [{platform}] ({idx}/{len(sorted_targets)}) drama {drama_id}: {count} danmaku IDs")
        except RuntimeError as exc:
            if "HTTP_418" in str(exc):
                if repaired:
                    write_repaired_danmaku_layers(platform, history_date, repaired, payloads)
                raise
            print(f"  [{platform}] ({idx}/{len(sorted_targets)}) ERROR on {drama_id}: {exc}")
            failed.add(drama_id)
        except Exception as exc:
            print(f"  [{platform}] ({idx}/{len(sorted_targets)}) ERROR on {drama_id}: {exc}")
            failed.add(drama_id)

    def retry_one_danmaku(drama_id: str) -> None:
        _, count = _repair_one_danmaku(platform, drama_id, requester)
        repaired[str(drama_id)] = count

    try:
        still_failed = retry_failed_danmaku_ids(
            platform,
            failed,
            retry_one_danmaku,
            max_attempts=attempts,
        )
    except RuntimeError as exc:
        if "HTTP_418" in str(exc) and repaired:
            write_repaired_danmaku_layers(platform, history_date, repaired, payloads)
        raise
    for drama_id in still_failed:
        repaired.pop(str(drama_id), None)

    if repaired:
        write_repaired_danmaku_layers(platform, history_date, repaired, payloads)
    else:
        print(f"  [{platform}] no danmaku entries repaired")

    return {
        "platform": platform,
        "date": history_date,
        "targets": sorted_targets,
        "repaired": repaired,
        "failed": sorted(still_failed),
    }


def repair_null_danmaku_mode(
    *,
    platforms: tuple[str, ...] | list[str],
    attempts: int,
    dry_run: bool,
) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for platform in platforms:
        history_date = resolve_repair_history_date(platform)
        results[platform] = repair_null_danmaku_for_platform(
            platform,
            history_date,
            attempts=attempts,
            dry_run=dry_run,
        )
    return results


# ---------------------------------------------------------------------------
# --only-danmaku mode
# ---------------------------------------------------------------------------

def only_danmaku_mode(store: dict, *, force: bool, do_missevan: bool, do_manbo: bool) -> None:
    """Only update paid danmaku UID counts for existing metric entries in ranks.json."""
    requester = MissevanRequester()

    if do_missevan:
        missevan_dramas = store["missevan"].get("dramas") or {}
        targets = []
        for drama_id, entry in missevan_dramas.items():
            if should_refresh_only_danmaku_entry(entry, force=force):
                targets.append(drama_id)
        print(
            f"[only-danmaku] missevan: {len(targets)} dramas to update "
            f"(existing={len(missevan_dramas)})"
        )
        failed_danmaku_ids: set[str] = set()
        for idx, drama_id in enumerate(sorted(targets), 1):
            print(f"  [missevan] ({idx}/{len(targets)}) danmaku for drama {drama_id} ...")
            # Need episodes list from getdrama
            try:
                url = f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}"
                data = requester.request_json(url)
                info = data.get("info") or {}
                episodes = (info.get("episodes") or {}).get("episode") or []
                _fetch_missevan_danmaku(requester, episodes, missevan_dramas[drama_id])
                missevan_dramas[drama_id]["fetched_at"] = now_iso()
            except RuntimeError as exc:
                if "HTTP_418" in str(exc):
                    print(f"  [missevan] FATAL: rate limited. Saving progress and stopping.")
                    save_json(RANKS_PATH, store)
                    raise
                if isinstance(exc, DanmakuRefreshError):
                    failed_danmaku_ids.add(str(drama_id))
                print(f"  [missevan] ERROR: {exc}")
            except Exception as exc:
                print(f"  [missevan] ERROR: {exc}")
            save_json(RANKS_PATH, store)

        def retry_one_missevan_danmaku(drama_id: str) -> None:
            url = f"https://www.missevan.com/dramaapi/getdrama?drama_id={drama_id}"
            data = requester.request_json(url)
            info = data.get("info") or {}
            episodes = (info.get("episodes") or {}).get("episode") or []
            _fetch_missevan_danmaku(requester, episodes, missevan_dramas[str(drama_id)])
            missevan_dramas[str(drama_id)]["fetched_at"] = now_iso()
            save_json(RANKS_PATH, store)

        def mark_missevan_danmaku_failed(drama_id: str) -> None:
            entry = missevan_dramas.get(str(drama_id), {})
            entry["danmaku_uid_count"] = None
            missevan_dramas[str(drama_id)] = entry
            save_json(RANKS_PATH, store)

        retry_failed_danmaku_ids(
            "missevan",
            failed_danmaku_ids,
            retry_one_missevan_danmaku,
            mark_failed=mark_missevan_danmaku_failed,
        )

    if do_manbo:
        manbo_dramas = store["manbo"].get("dramas") or {}
        existing_targets = set(manbo_dramas)
        print(
            f"[only-danmaku] manbo: {len(existing_targets)} dramas selected "
            f"(existing={len(manbo_dramas)})"
        )
        fetch_manbo_danmaku_details(existing_targets, store, force=force)




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch rank data from Missevan and Manbo")
    danmaku_group = parser.add_mutually_exclusive_group()
    danmaku_group.add_argument("--skip-danmaku", action="store_true", help="Skip danmaku UID counting")
    danmaku_group.add_argument("--only-danmaku", action="store_true", help="Only update danmaku UID counts for existing dramas")
    parser.add_argument("--force", action="store_true", help="Ignore 12h cache window, force refresh all")
    parser.add_argument("--benchmark-manbo-danmaku", metavar="DRAMA_ID", help="Benchmark one Manbo drama with the unified paid danmaku page queue")
    parser.add_argument("--benchmark-title", default="", help="Title to include in the Manbo benchmark output")
    parser.add_argument("--benchmark-output", default="manbo_huixin_danmaku_benchmark.json", help="Path for Manbo benchmark JSON output")
    parser.add_argument("--benchmark-page-concurrency", type=int, default=MANBO_DANMAKU_PAGE_CONCURRENCY, help="Global page concurrency for Manbo benchmark")
    parser.add_argument("--benchmark-page-size", type=int, default=MANBO_DANMAKU_PAGE_SIZE, help="Page size for Manbo benchmark")
    parser.add_argument("--backfill-missevan-peak-trend-from-latest", action="store_true", help="Backfill Missevan peak view-count trend from ranks:latest")
    parser.add_argument("--repair-null-danmaku", action="store_true", help="Repair empty danmaku UID counts across Upstash rank layers")
    parser.add_argument("--repair-attempts", type=int, default=DANMAKU_DRAMA_RETRY_ATTEMPTS, help="Retry rounds for failed danmaku repairs")
    parser.add_argument("--dry-run", action="store_true", help="List repair targets without fetching or writing")
    platform_group = parser.add_mutually_exclusive_group()
    platform_group.add_argument("--missevan-only", action="store_true", help="Only process Missevan")
    platform_group.add_argument("--manbo-only", action="store_true", help="Only process Manbo")
    args = parser.parse_args()

    do_missevan = not args.manbo_only
    do_manbo = not args.missevan_only
    active_platforms = tuple(
        platform
        for platform, enabled in (("missevan", do_missevan), ("manbo", do_manbo))
        if enabled
    )

    if args.repair_null_danmaku:
        print("=== Repairing null danmaku UID counts ===")
        results = repair_null_danmaku_mode(
            platforms=active_platforms,
            attempts=args.repair_attempts,
            dry_run=args.dry_run,
        )
        for platform, result in results.items():
            print(
                f"[ok] repair summary {platform}: "
                f"targets={len(result.get('targets') or [])}, "
                f"repaired={len(result.get('repaired') or {})}, "
                f"failed={len(result.get('failed') or [])}"
            )
        print("=== Done (repair-null-danmaku) ===")
        return

    if args.backfill_missevan_peak_trend_from_latest:
        print("=== Backfilling Missevan peak trend from ranks:latest ===")
        history_date = backfill_missevan_peak_trend_from_latest()
        print(f"[ok] backfilled {PEAK_TREND_KEY} for {history_date}")
        return

    if args.benchmark_manbo_danmaku:
        print("=== Manbo paid danmaku benchmark ===")
        result = fetch_manbo_paid_danmaku_benchmark(
            str(args.benchmark_manbo_danmaku),
            title=args.benchmark_title,
            page_size=args.benchmark_page_size,
            page_concurrency=args.benchmark_page_concurrency,
        )
        output_path = Path(args.benchmark_output)
        if not output_path.is_absolute():
            output_path = HERE / output_path
        save_json(output_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"[ok] benchmark result saved to {output_path}")
        return

    # Load or init store
    store = load_initial_rank_store()
    store.setdefault("_meta", {})
    store.setdefault("missevan", {"ranks": {}, "dramas": {}})
    store.setdefault("manbo", {"ranks": {}, "dramas": {}})
    store["missevan"].setdefault("ranks", {})
    store["missevan"].setdefault("dramas", {})
    store["manbo"].setdefault("ranks", {})
    store["manbo"].setdefault("dramas", {})
    sanitize_rank_store(store)

    # --only-danmaku mode: skip everything else
    if args.only_danmaku:
        print("=== Only-danmaku mode ===")
        only_danmaku_mode(store, force=args.force, do_missevan=do_missevan, do_manbo=do_manbo)
        store["_meta"]["updated_at"] = now_iso()
        save_json(RANKS_PATH, store)
        upload_rank_outputs(store, active_platforms)
        print("=== Done (only-danmaku) ===")
        return

    # Phase 2: Fetch rank lists
    print("=== Phase 2: Fetching rank lists ===")
    requester = MissevanRequester()

    missevan_ids: set[str] = set()
    manbo_ids: set[str] = set()

    missevan_danmaku_ids: set[str] = set()

    if do_missevan and do_manbo:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(fetch_missevan_ranks, requester, store): "missevan",
                executor.submit(fetch_manbo_ranks, store): "manbo",
            }
            for future in as_completed(futures):
                platform = futures[future]
                if platform == "missevan":
                    missevan_ids, missevan_danmaku_ids = future.result()
                else:
                    manbo_ids = future.result()
    else:
        if do_missevan:
            missevan_ids, missevan_danmaku_ids = fetch_missevan_ranks(requester, store)
        if do_manbo:
            manbo_ids = fetch_manbo_ranks(store)

    ongoing_missevan_ids: set[str] = set()
    ongoing_manbo_ids: set[str] = set()
    manbo_danmaku_ids: set[str] = set()
    if do_missevan:
        ongoing_missevan_ids = load_ongoing_drama_ids("missevan")
        rank_count = len(missevan_ids)
        missevan_ids = merge_rank_and_ongoing_ids(missevan_ids, ongoing_missevan_ids)
        missevan_danmaku_ids = merge_rank_and_ongoing_ids(missevan_danmaku_ids, ongoing_missevan_ids)
        print(
            f"  [missevan] drama IDs: rank={rank_count}, "
            f"ongoing={len(ongoing_missevan_ids)}, combined={len(missevan_ids)}"
        )
    if do_manbo:
        ongoing_manbo_ids = load_ongoing_drama_ids("manbo")
        rank_count = len(manbo_ids)
        manbo_ids = merge_rank_and_ongoing_ids(manbo_ids, ongoing_manbo_ids)
        manbo_danmaku_ids = merge_rank_and_ongoing_ids(collect_manbo_danmaku_target_ids(store), ongoing_manbo_ids)
        print(
            f"  [manbo] drama IDs: rank={rank_count}, "
            f"ongoing={len(ongoing_manbo_ids)}, combined={len(manbo_ids)}"
        )

    save_json(RANKS_PATH, store)

    # Phase 3: Dedup & cache filter
    print("=== Phase 3: Dedup & cache filtering ===")
    missevan_dramas_existing = store["missevan"].get("dramas") or {}
    manbo_dramas_existing = store["manbo"].get("dramas") or {}

    missevan_to_update, missevan_skipped = select_stale_ids(
        missevan_ids,
        missevan_dramas_existing,
        force=args.force,
    )

    manbo_to_update, manbo_skipped = select_stale_ids(
        manbo_ids,
        manbo_dramas_existing,
        force=args.force,
    )

    print(f"  missevan: total={len(missevan_ids)}, skip={missevan_skipped}, update={len(missevan_to_update)}")
    print(f"  manbo:    total={len(manbo_ids)}, skip={manbo_skipped}, update={len(manbo_to_update)}")

    def update_missevan_details() -> None:
        if not (do_missevan and missevan_to_update):
            return
        print(f"=== Phase 4: Missevan drama details ({len(missevan_to_update)}) ===")
        fetch_missevan_drama_details(
            requester, missevan_to_update, store,
            skip_danmaku=args.skip_danmaku,
            danmaku_ids=missevan_danmaku_ids,
        )

    def update_manbo_details() -> None:
        if do_manbo and manbo_to_update:
            print(f"=== Phase 5: Manbo drama details ({len(manbo_to_update)}) ===")
            fetch_manbo_drama_details(
                manbo_to_update, store,
                skip_danmaku=args.skip_danmaku,
                danmaku_ids=manbo_danmaku_ids,
            )

    if do_missevan and do_manbo:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(update_missevan_details),
                executor.submit(update_manbo_details),
            ]
            for future in as_completed(futures):
                future.result()
    else:
        update_missevan_details()
        update_manbo_details()

    # Phase 6: Upstash CV lookup
    print("=== Phase 6: Upstash CV lookup ===")
    try:
        lookup_cvs(store)
    except Exception as exc:
        print(f"  [upstash] WARN: CV lookup failed: {exc}")

    # Final save
    store["_meta"]["updated_at"] = now_iso()
    save_json(RANKS_PATH, store)

    # Upload to Upstash
    print("=== Uploading ranks to Upstash ===")
    upload_rank_outputs(store, active_platforms)

    print("=== Done ===")


if __name__ == "__main__":
    main()
