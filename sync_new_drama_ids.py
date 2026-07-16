from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_INFO_PATH,
    SERIES_INFO_PATH,
    iter_missevan_nodes,
    is_numeric_drama_id,
    load_json,
    missevan_main_cv_entries,
    normalize,
)


ROOT = Path(__file__).resolve().parent
QUEUE_KEY = "new:dramaIDs"
MANBO_INFO_KEY = "manbo:info:v1"
MISSEVAN_INFO_KEY = "missevan:info:v1"
CVID_MAP_KEY = "cvid-map:v1"
SERIES_INFO_KEY = "drama:series-info:v1"
WATCHCOUNT_KEY_PREFIXES = {
    "missevan": "missevan:watchcount",
    "manbo": "manbo:watchcount",
}
WATCHCOUNT_INDEX_VERSION = 1
WATCHCOUNT_MAX_DATES = 32
WATCHCOUNT_HISTORY_MAX_POINTS = WATCHCOUNT_MAX_DATES
WATCHCOUNT_SCAN_CACHE_TTL_SECONDS = 300
WATCHCOUNT_SCAN_COUNT = 1000
WATCHCOUNT_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WATCHCOUNT_SCAN_CACHE: dict[str, tuple[float, list[str]]] = {}
INFO_UPLOAD_MIN_COUNTS = {
    MISSEVAN_INFO_KEY: 100,
    MANBO_INFO_KEY: 50,
}
ALLOW_SMALL_INFO_UPLOAD_ENV = "ALLOW_SMALL_INFO_UPLOAD"
INVALID_MANBO_ID_CLEANUP_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""


class RemoteJsonMissing(RuntimeError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


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
    if not isinstance(data, dict):
        raise RuntimeError(f"{QUEUE_KEY} must be a JSON object.")
    raw_manbo = normalize_ids(data.get("manbo") or [], numeric_only=False)
    raw_missevan = normalize_ids(data.get("missevan") or [], numeric_only=False)
    invalid_manbo = [value for value in raw_manbo if not is_numeric_drama_id(value)]
    invalid_missevan = [value for value in raw_missevan if not is_numeric_drama_id(value)]
    if invalid_manbo or invalid_missevan:
        print(f"[warn] ignored invalid queue IDs: manbo={invalid_manbo} missevan={invalid_missevan}")
    return {
        "manbo": [value for value in raw_manbo if is_numeric_drama_id(value)],
        "missevan": [value for value in raw_missevan if is_numeric_drama_id(value)],
    }


def normalize_ids(values: list[object], *, numeric_only: bool = True) -> list[str]:
    out: list[str] = []
    for value in values:
        item = normalize(value)
        if item and (not numeric_only or is_numeric_drama_id(item)) and item not in out:
            out.append(item)
    return out


def run_script(script_name: str, drama_ids: list[str]) -> None:
    if not drama_ids:
        print(f"[skip] {script_name}: no ids")
        return
    command = [sys.executable, "-X", "utf8", script_name, *drama_ids]
    print(f"$ {subprocess.list2cmdline(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"{script_name} failed with exit code {return_code}")


def upload_json_file(key: str, path: Path, *, upstash=upstash_request) -> None:
    value = path.read_text(encoding="utf-8")
    assert_info_upload_is_safe(key, value, path)
    if key == CVID_MAP_KEY:
        assert_cvid_map_upload_meets_remote_floor(json.loads(value), upstash=upstash)
    result = upstash(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {path.name} to {key}: {result!r}")
    print(f"[ok] uploaded {path.name} -> {key}")


def upload_json_payload(key: str, payload: object, *, upstash=upstash_request) -> None:
    value = json.dumps(payload, ensure_ascii=False)
    assert_info_upload_is_safe(key, value, Path(key))
    if key == CVID_MAP_KEY:
        assert_cvid_map_upload_meets_remote_floor(payload, upstash=upstash)
    result = upstash(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload payload to {key}: {result!r}")
    print(f"[ok] uploaded payload -> {key}")


def write_info_payload(path: Path, payload: object) -> str:
    value = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(value, encoding="utf-8")
    return value


def count_info_payload(key: str, payload: object) -> int | None:
    if key == MISSEVAN_INFO_KEY and isinstance(payload, dict):
        return len(payload)
    if key == MANBO_INFO_KEY and isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return len(records)
    return None


def remote_json_count(key: str, *, upstash=upstash_request) -> int | None:
    raw = upstash(["GET", key])
    if raw in (None, ""):
        return None
    payload = decode_remote_json_payload(key, raw)
    if isinstance(payload, dict):
        return len(payload)
    return None


def assert_cvid_map_upload_meets_remote_floor(payload: object, *, upstash=upstash_request) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to upload {CVID_MAP_KEY}: expected a JSON object.")
    remote_count = remote_json_count(CVID_MAP_KEY, upstash=upstash)
    if remote_count in (None, 0):
        return
    minimum = (remote_count + 1) // 2
    if len(payload) < minimum:
        raise RuntimeError(
            f"Refusing to upload {CVID_MAP_KEY}: {len(payload)} entries found, "
            f"expected at least {minimum} (half of current remote count {remote_count})."
        )


def assert_info_upload_is_safe(key: str, value: str, path: Path) -> None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to upload invalid JSON from {path.name} to {key}: {exc}") from exc
    if key in (CVID_MAP_KEY, SERIES_INFO_KEY):
        if not isinstance(payload, dict):
            raise RuntimeError(f"Refusing to upload {path.name} to {key}: expected a JSON object.")
        if not payload:
            raise RuntimeError(f"Refusing to upload {path.name} to {key}: payload is empty.")
        for item_key, item_value in payload.items():
            if not isinstance(item_key, str) or not isinstance(item_value, dict):
                raise RuntimeError(f"Refusing to upload {path.name} to {key}: unexpected payload shape.")
        return
    minimum = INFO_UPLOAD_MIN_COUNTS.get(key)
    if minimum is None or os.environ.get(ALLOW_SMALL_INFO_UPLOAD_ENV) == "1":
        return
    count = count_info_payload(key, payload)
    if count is None:
        raise RuntimeError(f"Refusing to upload {path.name} to {key}: unexpected info store shape.")
    if count < minimum:
        raise RuntimeError(
            f"Refusing to upload {path.name} to {key}: only {count} records found, "
            f"expected at least {minimum}. Set {ALLOW_SMALL_INFO_UPLOAD_ENV}=1 to override intentionally."
        )


def decode_remote_info_payload(key: str, raw: object) -> object:
    if raw in (None, ""):
        raise RuntimeError(f"Refusing to download {key}: remote value is empty.")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Refusing to download {key}: remote value is invalid JSON: {exc}") from exc
    return raw


def assert_info_download_is_safe(key: str, payload: object) -> None:
    minimum = INFO_UPLOAD_MIN_COUNTS.get(key)
    count = count_info_payload(key, payload)
    if count is None:
        raise RuntimeError(f"Refusing to download {key}: unexpected info store shape.")
    if minimum is not None and count < minimum:
        raise RuntimeError(
            f"Refusing to download {key}: only {count} records found, expected at least {minimum}."
        )


def backup_local_info_file(path: Path) -> Path | None:
    return backup_local_json_file(path)


def backup_local_json_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = ROOT / "recovery_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{stamp}_{path.name}"
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path


def decode_remote_json_payload(key: str, raw: object) -> object:
    if raw in (None, ""):
        raise RemoteJsonMissing(f"{key} is empty or missing.")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{key} contains invalid JSON: {exc}") from exc
    return raw


def parse_remote_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def watchcount_key(platform: str, suffix: str) -> str:
    prefix = WATCHCOUNT_KEY_PREFIXES.get(platform)
    if prefix is None:
        raise RuntimeError(f"Unsupported watchcount platform: {platform}")
    return f"{prefix}:{suffix}"


def assert_watchcount_payload_is_safe(key: str, payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to use {key}: expected a JSON object.")
    if not isinstance(payload.get("_meta"), dict):
        raise RuntimeError(f"Refusing to use {key}: missing _meta object.")
    if not isinstance(payload.get("counts"), dict):
        raise RuntimeError(f"Refusing to use {key}: missing counts object.")


def watchcount_updated_at(payload: object) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    return parse_remote_iso_datetime(((payload.get("_meta") or {}).get("updated_at")))


def normalize_watchcount_snapshot_date(value: object, *, key: str = "watchcount") -> str:
    date_text = str(value).strip() if value is not None else ""
    if not WATCHCOUNT_DATE_PATTERN.fullmatch(date_text):
        raise RuntimeError(f"Refusing to use {key}: invalid snapshot date {value!r}.")
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError(f"Refusing to use {key}: invalid snapshot date {value!r}.") from exc
    return date_text


def normalize_watchcount_snapshot_dates(values: object, *, key: str = "watchcount") -> list[str]:
    if not isinstance(values, list):
        raise RuntimeError(f"Refusing to use {key}: dates must be a JSON array.")
    return sorted({normalize_watchcount_snapshot_date(value, key=key) for value in values})


def assert_watchcount_index_is_safe(key: str, payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to use {key}: expected a JSON object.")
    if payload.get("version") != WATCHCOUNT_INDEX_VERSION:
        raise RuntimeError(f"Refusing to use {key}: unsupported version {payload.get('version')!r}.")
    expected_platform = key.split(":", 1)[0]
    if payload.get("platform") != expected_platform:
        raise RuntimeError(f"Refusing to use {key}: platform does not match index key.")
    if parse_remote_iso_datetime(payload.get("updated_at")) is None:
        raise RuntimeError(f"Refusing to use {key}: updated_at must be a valid ISO timestamp.")
    dates = payload.get("dates")
    normalized_dates = normalize_watchcount_snapshot_dates(dates, key=key)
    if dates != normalized_dates:
        raise RuntimeError(f"Refusing to use {key}: dates must be sorted and deduplicated.")
    if len(normalized_dates) > WATCHCOUNT_MAX_DATES:
        raise RuntimeError(
            f"Refusing to use {key}: at most {WATCHCOUNT_MAX_DATES} snapshot dates are allowed."
        )


def decode_remote_watchcount_index(platform: str, raw: object) -> dict:
    key = watchcount_key(platform, "index")
    payload = decode_remote_json_payload(key, raw)
    assert_watchcount_index_is_safe(key, payload)
    return payload


def read_watchcount_index(platform: str, *, upstash=upstash_request) -> dict | None:
    key = watchcount_key(platform, "index")
    raw = upstash(["GET", key])
    if raw in (None, ""):
        return None
    return decode_remote_watchcount_index(platform, raw)


def _watchcount_number(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value) if any(marker in value.lower() for marker in (".", "e")) else int(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _watchcount_history_pairs(raw: object, *, key: str) -> list[tuple[str, object]]:
    if raw in (None, ""):
        return []
    if isinstance(raw, dict):
        return [(str(field), value) for field, value in raw.items()]
    if not isinstance(raw, (list, tuple)) or len(raw) % 2:
        raise RuntimeError(f"Refusing to use {key}: HGETALL returned an invalid response.")
    return [(str(raw[index]), raw[index + 1]) for index in range(0, len(raw), 2)]


def _watchcount_history_fields(raw: object, *, key: str) -> set[str]:
    if isinstance(raw, dict):
        return {str(field) for field in raw if str(field)}
    if isinstance(raw, (list, tuple)):
        return {str(raw[index]) for index in range(0, len(raw) - 1, 2) if str(raw[index])}
    return set()


def _decode_watchcount_history_entry(field: str, raw: object, *, key: str) -> dict:
    payload = decode_remote_json_payload(f"{key}[{field}]", raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to use {key}[{field}]: expected a JSON object.")
    name = payload.get("name", "")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise RuntimeError(f"Refusing to use {key}[{field}]: name must be a string.")
    points = payload.get("points")
    if not isinstance(points, list):
        raise RuntimeError(f"Refusing to use {key}[{field}]: points must be a JSON array.")
    normalized_points: dict[str, int | float] = {}
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise RuntimeError(f"Refusing to use {key}[{field}]: invalid point {point!r}.")
        date_text = normalize_watchcount_snapshot_date(point[0], key=f"{key}[{field}]")
        number = _watchcount_number(point[1])
        if number is None:
            raise RuntimeError(f"Refusing to use {key}[{field}]: invalid point value {point[1]!r}.")
        normalized_points[date_text] = number
    ordered_dates = sorted(normalized_points)
    return {
        "name": name,
        "points": [[date_text, normalized_points[date_text]] for date_text in ordered_dates],
    }


def decode_watchcount_history(platform: str, raw: object) -> dict[str, dict]:
    key = watchcount_key(platform, "history")
    history: dict[str, dict] = {}
    for field, value in _watchcount_history_pairs(raw, key=key):
        if not is_numeric_drama_id(field):
            raise RuntimeError(f"Refusing to use {key}: history field {field!r} is not a dramaId.")
        history[field] = _decode_watchcount_history_entry(field, value, key=key)
    return history


def read_watchcount_history(platform: str, *, upstash=upstash_request) -> tuple[object, dict[str, dict]]:
    key = watchcount_key(platform, "history")
    raw = upstash(["HGETALL", key])
    return raw, decode_watchcount_history(platform, raw)


def _history_entry_from_points(
    name: str,
    points: dict[str, int | float],
    *,
    max_points: int | None = WATCHCOUNT_HISTORY_MAX_POINTS,
) -> dict:
    dates = sorted(points)
    if max_points is not None:
        dates = dates[-max_points:]
    return {
        "name": name,
        "points": [[date_text, points[date_text]] for date_text in dates],
    }


def build_watchcount_history(
    platform: str,
    snapshots: dict[str, dict],
    *,
    max_points: int | None = WATCHCOUNT_HISTORY_MAX_POINTS,
) -> dict[str, dict]:
    entries: dict[str, dict[str, object]] = {}
    for date_text in sorted(snapshots):
        payload = snapshots[date_text]
        assert_watchcount_payload_is_safe(watchcount_key(platform, date_text), payload)
        for drama_id, item in payload["counts"].items():
            if not isinstance(item, dict):
                continue
            number = _watchcount_number(item.get("view_count"))
            if number is None:
                continue
            field = str(drama_id).strip()
            if not is_numeric_drama_id(field):
                continue
            entry = entries.setdefault(field, {"name": "", "points": {}})
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                entry["name"] = name
            entry["points"][date_text] = number
    return {
        field: _history_entry_from_points(
            str(entry["name"]),
            entry["points"],
            max_points=max_points,
        )
        for field, entry in sorted(entries.items())
        if entry["points"]
    }


def merge_watchcount_history(
    existing: dict[str, dict],
    payload: dict,
    current_date: str,
    allowed_dates: list[str],
    *,
    max_points: int | None = WATCHCOUNT_HISTORY_MAX_POINTS,
) -> dict[str, dict]:
    allowed = set(allowed_dates)
    merged: dict[str, dict] = {}
    existing_names = {field: entry["name"] for field, entry in existing.items()}
    for field, entry in existing.items():
        points = {
            point[0]: point[1]
            for point in entry["points"]
            if point[0] in allowed
        }
        if points:
            merged[field] = _history_entry_from_points(
                entry["name"],
                points,
                max_points=max_points,
            )
    for drama_id, item in payload["counts"].items():
        if not isinstance(item, dict):
            continue
        number = _watchcount_number(item.get("view_count"))
        if number is None:
            continue
        field = str(drama_id).strip()
        if not is_numeric_drama_id(field):
            continue
        entry = merged.get(field, {"name": existing_names.get(field, ""), "points": []})
        points = {point[0]: point[1] for point in entry["points"]}
        points[current_date] = number
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name
        merged[field] = _history_entry_from_points(
            entry["name"],
            points,
            max_points=max_points,
        )
    return dict(sorted(merged.items()))


def filter_watchcount_history(
    history: dict[str, dict],
    retained_dates: list[str],
) -> dict[str, dict]:
    retained = set(retained_dates)
    filtered: dict[str, dict] = {}
    for field, entry in history.items():
        points = {
            point[0]: point[1]
            for point in entry["points"]
            if point[0] in retained
        }
        if points:
            filtered[field] = _history_entry_from_points(entry["name"], points)
    return dict(sorted(filtered.items()))


def encode_watchcount_history(history: dict[str, dict]) -> list[object]:
    fields: list[object] = []
    for field in sorted(history):
        fields.extend(
            [
                field,
                json.dumps(history[field], ensure_ascii=False, separators=(",", ":")),
            ]
        )
    return fields


def _assert_hash_write_succeeded(operation: str, key: str, result: object) -> None:
    if result == "OK":
        return
    if isinstance(result, int) and not isinstance(result, bool) and result >= 0:
        return
    if isinstance(result, str) and result.isdigit():
        return
    raise RuntimeError(f"Failed to {operation} {key}: {result!r}")


def _assert_delete_succeeded(operation: str, keys: list[str], result: object) -> None:
    if isinstance(result, int) and not isinstance(result, bool) and result >= 0:
        return
    if isinstance(result, str) and result.isdigit():
        return
    raise RuntimeError(f"Failed to {operation}: {keys!r}; result={result!r}")


def _load_watchcount_snapshots_by_dates(
    platform: str,
    dates: list[str],
    *,
    upstash=upstash_request,
) -> dict[str, dict]:
    if not dates:
        return {}
    keys = [watchcount_key(platform, date_text) for date_text in dates]
    raw = upstash(["MGET", *keys])
    if not isinstance(raw, (list, tuple)) or len(raw) != len(keys):
        raise RuntimeError(f"Unsupported MGET response for {platform} watchcount snapshots: {raw!r}")
    snapshots: dict[str, dict] = {}
    for date_text, key, value in zip(dates, keys, raw):
        if value in (None, ""):
            raise RuntimeError(f"Refusing to rebuild {platform} history: {key} is missing.")
        snapshots[date_text] = decode_remote_watchcount_payload(key, value)
    return snapshots


def _watchcount_snapshot_date_from_key(platform: str, key: object) -> str | None:
    if not isinstance(key, str):
        return None
    prefix = watchcount_key(platform, "")
    if not key.startswith(prefix):
        return None
    suffix = key[len(prefix) :]
    if suffix == "latest" or not WATCHCOUNT_DATE_PATTERN.fullmatch(suffix):
        return None
    try:
        return normalize_watchcount_snapshot_date(suffix, key=key)
    except RuntimeError:
        return None


def _scan_watchcount_snapshot_dates(platform: str, *, upstash=upstash_request) -> list[str]:
    pattern = f"{watchcount_key(platform, '')}????-??-??"
    cursor = "0"
    seen_cursors: set[str] = set()
    dates: set[str] = set()
    while True:
        if cursor in seen_cursors:
            raise RuntimeError(f"SCAN for {pattern} returned a repeated cursor {cursor!r}.")
        seen_cursors.add(cursor)
        raw = upstash(["SCAN", cursor, "MATCH", pattern, "COUNT", str(WATCHCOUNT_SCAN_COUNT)])
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise RuntimeError(f"Unsupported SCAN response for {pattern}: {raw!r}")
        next_cursor, keys = raw
        if not isinstance(keys, list):
            raise RuntimeError(f"Unsupported SCAN keys response for {pattern}: {keys!r}")
        for remote_key in keys:
            date_text = _watchcount_snapshot_date_from_key(platform, remote_key)
            if date_text is not None:
                dates.add(date_text)
        cursor = str(next_cursor)
        if cursor == "0":
            return sorted(dates)


def clear_watchcount_scan_cache() -> None:
    _WATCHCOUNT_SCAN_CACHE.clear()


def scan_watchcount_snapshot_dates(
    platform: str,
    *,
    upstash=upstash_request,
    cache_ttl_seconds: float = WATCHCOUNT_SCAN_CACHE_TTL_SECONDS,
    use_cache: bool = True,
) -> list[str]:
    now = time.monotonic()
    cached = _WATCHCOUNT_SCAN_CACHE.get(platform)
    if use_cache and cached is not None and now - cached[0] < cache_ttl_seconds:
        return list(cached[1])
    dates = _scan_watchcount_snapshot_dates(platform, upstash=upstash)
    _WATCHCOUNT_SCAN_CACHE[platform] = (now, dates)
    return list(dates)


def load_watchcount_snapshot_dates(
    platform: str,
    *,
    upstash=upstash_request,
    cache_ttl_seconds: float = WATCHCOUNT_SCAN_CACHE_TTL_SECONDS,
) -> list[str]:
    """Read the snapshot date list, preferring the index during rollout."""
    index_key = watchcount_key(platform, "index")
    try:
        index = read_watchcount_index(platform, upstash=upstash)
        if index is not None:
            return list(index["dates"])
    except Exception as exc:
        print(f"[warn] {index_key}: index unavailable, using cached SCAN fallback: {exc}")
    return scan_watchcount_snapshot_dates(
        platform,
        upstash=upstash,
        cache_ttl_seconds=cache_ttl_seconds,
        use_cache=True,
    )


def load_watchcount_snapshots(
    platform: str,
    *,
    upstash=upstash_request,
    cache_ttl_seconds: float = WATCHCOUNT_SCAN_CACHE_TTL_SECONDS,
) -> dict[str, dict]:
    """Load dated snapshots using the index-first date discovery path."""
    snapshots: dict[str, dict] = {}
    for date_text in load_watchcount_snapshot_dates(
        platform,
        upstash=upstash,
        cache_ttl_seconds=cache_ttl_seconds,
    ):
        key = watchcount_key(platform, date_text)
        try:
            snapshots[date_text] = decode_remote_watchcount_payload(key, upstash(["GET", key]))
        except RemoteJsonMissing:
            print(f"[warn] {key}: indexed snapshot is missing")
    return snapshots


def load_watchcount_payload(path: Path) -> dict:
    payload = load_json(path, {"_meta": {"updated_at": None}, "counts": {}})
    if not isinstance(payload, dict):
        return {"_meta": {"updated_at": None}, "counts": {}}
    payload.setdefault("_meta", {"updated_at": None})
    payload.setdefault("counts", {})
    return payload


def decode_remote_watchcount_payload(key: str, raw: object) -> dict:
    payload = decode_remote_json_payload(key, raw)
    assert_watchcount_payload_is_safe(key, payload)
    return payload


def sync_remote_watchcount_if_newer(
    platform: str,
    path: Path,
    *,
    upstash=upstash_request,
    force: bool = False,
    require_remote: bool = False,
) -> bool:
    key = watchcount_key(platform, "latest")
    local_payload = load_watchcount_payload(path)
    try:
        remote_payload = decode_remote_watchcount_payload(key, upstash(["GET", key]))
    except RemoteJsonMissing:
        if require_remote:
            raise RuntimeError(f"Refusing to continue: {key} is empty or missing")
        print(f"[skip] {key}: remote value is empty or missing")
        return False

    local_updated = watchcount_updated_at(local_payload)
    remote_updated = watchcount_updated_at(remote_payload)
    should_download = force or (remote_updated is not None and (local_updated is None or remote_updated > local_updated))
    if not should_download:
        print(f"[skip] {key}: local watchcount is up to date")
        return False

    backup_path = write_json_work_copy(path, remote_payload)
    if backup_path is not None:
        print(f"[backup] {path.name} -> {backup_path}")
    print(f"[ok] downloaded {key} -> {path.name}")
    return True


def upload_watchcount_file(platform: str, path: Path, *, upstash=upstash_request) -> None:
    payload = load_watchcount_payload(path)
    latest_key = watchcount_key(platform, "latest")
    assert_watchcount_payload_is_safe(latest_key, payload)
    updated_at = watchcount_updated_at(payload) or datetime.now(timezone.utc)
    current_date = updated_at.astimezone(timezone.utc).date().isoformat()
    date_key = watchcount_key(platform, current_date)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for key in (date_key, latest_key):
        result = upstash(["SET", key, encoded])
        if result != "OK":
            raise RuntimeError(f"Failed to upload {path.name} to {key}: {result!r}")
        print(f"[ok] uploaded {path.name} -> {key} ({len(encoded)} bytes)")

    history_key = watchcount_key(platform, "history")
    raw_history = upstash(["HGETALL", history_key])
    history_fields = _watchcount_history_fields(raw_history, key=history_key)
    history_invalid = False
    try:
        existing_history = decode_watchcount_history(platform, raw_history)
    except RuntimeError as exc:
        print(f"[warn] {history_key}: history unavailable, rebuilding from dated snapshots: {exc}")
        existing_history = {}
        history_invalid = True

    existing_index = read_watchcount_index(platform, upstash=upstash)
    if existing_index is None:
        existing_dates = scan_watchcount_snapshot_dates(platform, upstash=upstash, use_cache=False)
    else:
        existing_dates = list(existing_index["dates"])
    retained_dates = sorted(set(existing_dates) | {current_date})[-WATCHCOUNT_MAX_DATES:]
    staging_dates = (
        sorted(set(existing_dates) | {current_date})
        if existing_index is not None
        else retained_dates
    )

    needs_rebuild = existing_index is None or history_invalid or not existing_history
    if needs_rebuild:
        snapshots = _load_watchcount_snapshots_by_dates(platform, staging_dates, upstash=upstash)
        staged_history = build_watchcount_history(platform, snapshots, max_points=None)
    else:
        staged_history = merge_watchcount_history(
            existing_history,
            payload,
            current_date,
            staging_dates,
            max_points=None,
        )
    desired_history = filter_watchcount_history(staged_history, retained_dates)

    staged_history_args = encode_watchcount_history(staged_history)
    if staged_history_args:
        result = upstash(["HSET", history_key, *staged_history_args])
        _assert_hash_write_succeeded("write history hash", history_key, result)
        print(f"[ok] staged watchcount history -> {history_key} ({len(staged_history)} dramas)")

    index_payload = {
        "version": WATCHCOUNT_INDEX_VERSION,
        "platform": platform,
        "updated_at": (
            updated_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "dates": retained_dates,
    }
    index_key = watchcount_key(platform, "index")
    index_encoded = json.dumps(index_payload, ensure_ascii=False, separators=(",", ":"))
    result = upstash(["SET", index_key, index_encoded])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {path.name} index to {index_key}: {result!r}")
    print(f"[ok] uploaded watchcount index -> {index_key} ({len(index_encoded)} bytes)")

    if staged_history != desired_history:
        desired_history_args = encode_watchcount_history(desired_history)
        if desired_history_args:
            result = upstash(["HSET", history_key, *desired_history_args])
            _assert_hash_write_succeeded("trim history hash", history_key, result)
            print(f"[ok] trimmed watchcount history -> {history_key} ({len(desired_history)} dramas)")

    stale_history_fields = sorted((history_fields | set(staged_history)) - set(desired_history))
    if stale_history_fields:
        result = upstash(["HDEL", history_key, *stale_history_fields])
        _assert_hash_write_succeeded("clean history hash", history_key, result)
        print(f"[ok] deleted stale history fields from {history_key}: {len(stale_history_fields)}")

    evicted_dates = set(existing_dates) - set(retained_dates)
    if len(retained_dates) == WATCHCOUNT_MAX_DATES and existing_index is not None:
        # The previous run may have committed the new index but failed during DEL.
        # Re-scan only the mature 32-period set so a later retry can discover those orphans.
        evicted_dates.update(
            set(scan_watchcount_snapshot_dates(platform, upstash=upstash, use_cache=False))
            - set(retained_dates)
        )
    evicted_dates = sorted(evicted_dates)
    if evicted_dates:
        evicted_keys = [watchcount_key(platform, date_text) for date_text in evicted_dates]
        result = upstash(["DEL", *evicted_keys])
        _assert_delete_succeeded("delete evicted watchcount snapshots", evicted_keys, result)
        print(f"[ok] deleted evicted watchcount snapshots: {', '.join(evicted_keys)}")


def write_json_work_copy(path: Path, payload: object) -> Path | None:
    backup_path = backup_local_json_file(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path


def load_remote_json_or_backup(
    key: str,
    path: Path,
    default: object,
    *,
    upstash=upstash_request,
    upload_backup_if_missing: bool = False,
    write_remote_to_local: bool = True,
) -> object:
    try:
        raw = upstash(["GET", key])
        payload = decode_remote_json_payload(key, raw)
        if write_remote_to_local:
            backup_path = write_json_work_copy(path, payload)
            if backup_path is not None:
                print(f"[backup] {path.name} -> {backup_path}")
            print(f"[ok] downloaded {key} -> {path.name}")
        return payload
    except Exception as exc:
        local_exists = path.exists()
        if local_exists:
            local_payload = load_json(path, default)
            print(f"[local backup] using {path.name} for {key}: {exc}")
            if upload_backup_if_missing and isinstance(exc, RemoteJsonMissing):
                upload_json_payload(key, local_payload, upstash=upstash)
            return local_payload
        print(f"[local backup] no {path.name} backup for {key}: {exc}")
        return default


def download_json_key_to_file(
    key: str,
    path: Path,
    default: object,
    *,
    upload_backup_if_missing: bool = False,
) -> object:
    return load_remote_json_or_backup(
        key,
        path,
        default,
        upload_backup_if_missing=upload_backup_if_missing,
    )


def download_info_file(key: str, path: Path) -> None:
    payload = decode_remote_info_payload(key, upstash_request(["GET", key]))
    assert_info_download_is_safe(key, payload)
    backup_path = write_json_work_copy(path, payload)
    if backup_path is not None:
        print(f"[backup] {path.name} -> {backup_path}")
    print(f"[ok] downloaded {key} -> {path.name}")


def download_info_files() -> None:
    download_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH)
    download_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH)


def download_support_files() -> None:
    download_json_key_to_file(CVID_MAP_KEY, COMBINED_CVID_MAP_PATH, {}, upload_backup_if_missing=True)


def build_missevan_index(store: dict) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for _series_title, _season_key, node in iter_missevan_nodes(store):
        drama_id = normalize(node.get("dramaId"))
        if drama_id and drama_id not in indexed:
            indexed[drama_id] = node
    return indexed


def build_manbo_index(store: dict) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for record in store.get("records") or []:
        drama_id = normalize(record.get("dramaId"))
        if drama_id and drama_id not in indexed:
            indexed[drama_id] = record
    return indexed


def merge_missevan_info_for_ids(remote_store: dict, local_store: dict, drama_ids: list[str]) -> dict:
    merged = dict(remote_store)
    local_index = build_missevan_index(local_store)
    for drama_id in normalize_ids(drama_ids):
        record = local_index.get(drama_id)
        if record is None:
            print(f"[warn] no local 猫耳 record to upload for dramaId={drama_id}")
            continue
        merged[drama_id] = record
    return merged


def merge_manbo_info_for_ids(remote_store: dict, local_store: dict, drama_ids: list[str]) -> dict:
    merged = dict(remote_store)
    records = list(remote_store.get("records") or [])
    local_index = build_manbo_index(local_store)
    position_by_id = {
        normalize(record.get("dramaId")): idx
        for idx, record in enumerate(records)
        if isinstance(record, dict) and normalize(record.get("dramaId"))
    }
    for drama_id in normalize_ids(drama_ids):
        record = local_index.get(drama_id)
        if record is None:
            print(f"[warn] no local 漫播 record to upload for dramaId={drama_id}")
            continue
        idx = position_by_id.get(drama_id)
        if idx is None:
            position_by_id[drama_id] = len(records)
            records.append(record)
        else:
            records[idx] = record
    merged["records"] = records
    return merged


def merge_info_payload_for_ids(key: str, remote_store: object, local_store: object, drama_ids: list[str]) -> object:
    if key == MISSEVAN_INFO_KEY:
        if not isinstance(remote_store, dict) or not isinstance(local_store, dict):
            raise RuntimeError(f"{key} must be a JSON object.")
        return merge_missevan_info_for_ids(remote_store, local_store, drama_ids)
    if key == MANBO_INFO_KEY:
        if not isinstance(remote_store, dict) or not isinstance(local_store, dict):
            raise RuntimeError(f"{key} must be a JSON object.")
        return merge_manbo_info_for_ids(remote_store, local_store, drama_ids)
    raise RuntimeError(f"Unsupported info key for merge upload: {key}")


def merge_and_upload_info_file(key: str, path: Path, drama_ids: list[str]) -> None:
    latest_remote = decode_remote_info_payload(key, upstash_request(["GET", key]))
    assert_info_download_is_safe(key, latest_remote)
    local_payload = load_json(path, {})
    merged = merge_info_payload_for_ids(key, latest_remote, local_payload, drama_ids)
    value = write_info_payload(path, merged)
    assert_info_upload_is_safe(key, value, path)
    result = upstash_request(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload merged {path.name} to {key}: {result!r}")
    print(f"[ok] merged and uploaded {path.name} -> {key}")


def is_missevan_ready(record: dict | None) -> bool:
    if not record:
        return False
    if not normalize(record.get("title")):
        return False
    if record.get("type") in (None, ""):
        return False
    if record.get("catalog") in (None, ""):
        return False
    has_create_time = bool(normalize(record.get("createTime")))
    has_author = bool(normalize(record.get("author")))
    if not has_create_time and not has_author:
        return False
    if not normalize(record.get("cover")):
        return False
    if "is_member" not in record:
        return False
    return len(missevan_main_cv_entries(record)) >= 2


def is_manbo_ready(record: dict | None) -> bool:
    if not record:
        return False
    if not normalize(record.get("name")):
        return False
    if record.get("catalog") in (None, ""):
        return False
    if not normalize(record.get("createTime")):
        return False
    if not normalize(record.get("genre")):
        return False
    if not normalize(record.get("cover")):
        return False
    if "vipFree" not in record:
        return False
    return len(record.get("mainCvNicknames") or []) >= 2


def prune_queue(queue: dict[str, list[str]]) -> dict[str, list[str]]:
    missevan_store = load_json(MISSEVAN_INFO_PATH, {})
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    missevan_index = build_missevan_index(missevan_store)
    manbo_index = build_manbo_index(manbo_store)
    remaining_missevan = [
        drama_id
        for drama_id in queue.get("missevan", [])
        if is_numeric_drama_id(drama_id) and not is_missevan_ready(missevan_index.get(drama_id))
    ]
    remaining_manbo = [
        drama_id
        for drama_id in queue.get("manbo", [])
        if is_numeric_drama_id(drama_id) and not is_manbo_ready(manbo_index.get(drama_id))
    ]
    return {"manbo": remaining_manbo, "missevan": remaining_missevan}


def save_queue(queue: dict[str, list[str]]) -> None:
    payload = json.dumps(
        {
            "manbo": normalize_ids(queue.get("manbo") or []),
            "missevan": normalize_ids(queue.get("missevan") or []),
        },
        ensure_ascii=False,
    )
    result = upstash_request(["SET", QUEUE_KEY, payload])
    if result != "OK":
        raise RuntimeError(f"Failed to update {QUEUE_KEY}: {result!r}")
    print(
        "[ok] updated queue:",
        json.dumps(
            {
                "manbo": len(queue.get("manbo") or []),
                "missevan": len(queue.get("missevan") or []),
            },
            ensure_ascii=False,
        ),
    )


def rank_backfill_platforms(missevan_ids: list[str], manbo_ids: list[str]) -> tuple[str, ...]:
    platforms: list[str] = []
    if missevan_ids:
        platforms.append("missevan")
    if manbo_ids:
        platforms.append("manbo")
    return tuple(platforms)


def backfill_rank_metadata(platforms: tuple[str, ...]) -> None:
    if not platforms:
        print("[skip] rank backfill: no platforms")
        return

    import fetch_rank_data as ranks

    print(f"=== Backfilling rank metadata ({', '.join(platforms)}) ===")
    store = ranks.load_initial_rank_store()
    store.setdefault("_meta", {})
    store.setdefault("missevan", {"ranks": {}, "dramas": {}})
    store.setdefault("manbo", {"ranks": {}, "dramas": {}})
    store["missevan"].setdefault("ranks", {})
    store["missevan"].setdefault("dramas", {})
    store["manbo"].setdefault("ranks", {})
    store["manbo"].setdefault("dramas", {})
    ranks.sanitize_rank_store(store)
    ranks.lookup_cvs(store)
    store["_meta"]["updated_at"] = ranks.now_iso()
    ranks.save_json(ranks.RANKS_PATH, store)
    ranks.upload_rank_outputs(store, platforms)
    print("[ok] backfilled rank metadata")


def cleanup_invalid_manbo_ids(
    *,
    upstash=upstash_request,
    info_path: Path = MANBO_INFO_PATH,
    counts_path: Path = MANBO_COUNTS_PATH,
    backup_dir: Path | None = None,
) -> dict[str, int]:
    target_dir = backup_dir or (ROOT / "recovery_backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    specs = (
        (QUEUE_KEY, "queue"),
        (MANBO_INFO_KEY, "info"),
        (watchcount_key("manbo", "latest"), "watchcount"),
    )
    cleaned_payloads: dict[str, dict] = {}
    removed: dict[str, int] = {}
    for key, kind in specs:
        raw = upstash(["GET", key])
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Refusing to clean {key}: remote payload is empty or unsupported")
        payload = decode_remote_json_payload(key, raw)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Refusing to clean {key}: expected a JSON object")
        backup_path = target_dir / f"{stamp}_{key.replace(':', '-')}.json"
        backup_path.write_text(raw, encoding="utf-8")

        cleaned = dict(payload)
        if kind == "queue":
            values = list(payload.get("manbo") or [])
            valid = [str(value) for value in values if is_numeric_drama_id(value)]
            cleaned["manbo"] = list(dict.fromkeys(valid))
            removed[kind] = len(values) - len(cleaned["manbo"])
        elif kind == "info":
            records = list(payload.get("records") or [])
            cleaned["records"] = [
                record
                for record in records
                if isinstance(record, dict) and is_numeric_drama_id(record.get("dramaId"))
            ]
            removed[kind] = len(records) - len(cleaned["records"])
        else:
            counts = payload.get("counts")
            if not isinstance(counts, dict):
                raise RuntimeError(f"Refusing to clean {key}: missing counts object")
            cleaned["counts"] = {
                str(drama_id): entry
                for drama_id, entry in counts.items()
                if is_numeric_drama_id(drama_id)
            }
            removed[kind] = len(counts) - len(cleaned["counts"])

        encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        result = upstash(
            [
                "EVAL",
                INVALID_MANBO_ID_CLEANUP_SCRIPT,
                1,
                key,
                hashlib.sha1(raw.encode("utf-8")).hexdigest(),
                encoded,
            ]
        )
        if int(result or 0) != 1:
            raise RuntimeError(f"Refusing to clean {key}: remote payload changed concurrently")
        verified_raw = upstash(["GET", key])
        if not isinstance(verified_raw, str):
            raise RuntimeError(f"Failed to verify cleaned {key}")
        verified = decode_remote_json_payload(key, verified_raw)
        if kind == "queue":
            invalid = [value for value in (verified.get("manbo") or []) if not is_numeric_drama_id(value)]
        elif kind == "info":
            invalid = [
                record.get("dramaId")
                for record in (verified.get("records") or [])
                if not isinstance(record, dict) or not is_numeric_drama_id(record.get("dramaId"))
            ]
        else:
            invalid = [value for value in (verified.get("counts") or {}) if not is_numeric_drama_id(value)]
        if invalid:
            raise RuntimeError(f"Failed to clean {key}: invalid drama IDs remain: {invalid}")
        cleaned_payloads[kind] = verified

    write_json_work_copy(info_path, cleaned_payloads["info"])
    write_json_work_copy(counts_path, cleaned_payloads["watchcount"])
    return removed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync queued new drama IDs into platform info stores")
    parser.add_argument(
        "--backfill-ranks",
        action="store_true",
        help="After syncing info stores, backfill rank metadata from the latest info stores",
    )
    parser.add_argument("--upload-cv-map", action="store_true", help=f"Upload local CV map to {CVID_MAP_KEY}")
    parser.add_argument(
        "--upload-series-info",
        action="store_true",
        help=f"Upload local drama series info to {SERIES_INFO_KEY}",
    )
    parser.add_argument(
        "--cleanup-invalid-manbo-ids",
        action="store_true",
        help="Remove non-numeric 漫播 dramaIds from the active queue, info store, and latest watchcount",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args([] if argv is None else argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    if args.cleanup_invalid_manbo_ids:
        stats = cleanup_invalid_manbo_ids()
        print("[ok] cleaned invalid 漫播 dramaIds:", json.dumps(stats, ensure_ascii=False))
        return 0
    if args.upload_cv_map:
        upload_json_file(CVID_MAP_KEY, COMBINED_CVID_MAP_PATH)
    if args.upload_series_info:
        upload_json_file(SERIES_INFO_KEY, SERIES_INFO_PATH)
    if args.upload_cv_map or args.upload_series_info:
        return 0
    queue = load_queue()
    manbo_ids = queue.get("manbo") or []
    missevan_ids = queue.get("missevan") or []
    print(f"[queue] manbo={len(manbo_ids)} missevan={len(missevan_ids)}")
    if not manbo_ids and not missevan_ids:
        print("No pending drama IDs in new:dramaIDs.")
        return 0

    download_info_files()
    download_support_files()

    run_script("append_manbo_ids.py", manbo_ids)
    run_script("append_missevan_ids.py", missevan_ids)

    merge_and_upload_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH, manbo_ids)
    merge_and_upload_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH, missevan_ids)

    remaining_queue = prune_queue(queue)
    save_queue(remaining_queue)
    if args.backfill_ranks:
        backfill_rank_metadata(rank_backfill_platforms(missevan_ids, manbo_ids))
    print(
        "[done]",
        json.dumps(
            {
                "removed_manbo": len(manbo_ids) - len(remaining_queue["manbo"]),
                "removed_missevan": len(missevan_ids) - len(remaining_queue["missevan"]),
                "remaining_manbo": len(remaining_queue["manbo"]),
                "remaining_missevan": len(remaining_queue["missevan"]),
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
