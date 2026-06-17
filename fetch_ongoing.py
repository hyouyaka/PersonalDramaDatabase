"""Fetch ongoing drama records from Missevan and Manbo and upload to Upstash."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from platform_sync import MANBO_CATALOG_OVERRIDES, MISSEVAN_HEADERS, MissevanRequester, normalize, request_manbo_json

HERE = Path(__file__).resolve().parent
BEIJING_TZ = timezone(timedelta(hours=8))

ONGOING_KEYS = {
    "missevan": "ongoing:missevan",
    "manbo": "ongoing:manbo",
}
MISSEVAN_WEEKDAY_CACHE_KEY = "ongoing:missevan:weekday-cache:v1"
MISSEVAN_WEEKDAY_CACHE_TTL_DAYS = 14
MISSEVAN_SUMMERDRAMA_URL = "https://www.missevan.com/dramaapi/summerdrama"
MISSEVAN_TIMELINE_URL = "https://app.missevan.com/drama/timeline"
MISSEVAN_SOUND_PAGE_URL = "https://www.missevan.com/sound/m?order=0&id=17&p={page}"
MISSEVAN_SOUND_INFO_URL = "https://www.missevan.com/sound/getsound?soundid={sound_id}"
MISSEVAN_SOUND_DRAMA_URL = "https://www.missevan.com/dramaapi/getdramabysound?sound_id={sound_id}"
MANBO_TIME_DETAIL_URL = (
    "https://api.kilamanbo.com/api/v530/radio/drama/new/time/detail"
    "?date={timestamp}&pageNo=1&pageSize=100&type=105"
)
BLOCKED_UPDATE_TITLE_WORDS = ("福利", "小剧场", "生日")
MISSEVAN_DAILY_VIEW_THRESHOLD = 100
MISSEVAN_DAILY_COMMENT_THRESHOLD = 20
MISSEVAN_WEEKDAY_BY_LABEL = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}
MISSEVAN_LABEL_BY_WEEKDAY = {
    value: key for key, value in MISSEVAN_WEEKDAY_BY_LABEL.items() if key != "天"
}


class MissevanTodayFallbackError(RuntimeError):
    """Raised when today's empty timeline bucket cannot be safely backfilled."""


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(HERE / ".env")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_for_now(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def missevan_timestamp_to_beijing_date(value: object) -> date | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(BEIJING_TZ).date()
    except (OSError, OverflowError, ValueError):
        return None


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


def load_upstash_json(key: str, *, upstash: Callable[[list[object]], object] = upstash_request) -> object:
    result = upstash(["GET", key])
    if result in (None, ""):
        return None
    if isinstance(result, str):
        return json.loads(result)
    return result


def set_upstash_json(
    key: str,
    payload: dict,
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
) -> None:
    result = upstash(["SET", key, json.dumps(payload, ensure_ascii=False)])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {key}: {result!r}")


def make_record(drama_id: object, update_type: str) -> dict[str, object]:
    return {
        "dramaId": str(drama_id),
        "updateType": update_type,
    }


def records_to_map(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    mapped: dict[str, dict[str, object]] = {}
    for record in records:
        drama_id = str(record.get("dramaId") or "").strip()
        if drama_id:
            mapped[drama_id] = dict(record)
    return mapped


def records_from_map(records: object) -> list[dict[str, object]]:
    if not isinstance(records, dict):
        return []
    result: list[dict[str, object]] = []
    for key, record in records.items():
        if isinstance(record, dict):
            drama_id = record.get("dramaId") or key
            if drama_id not in (None, ""):
                result.append(make_record(drama_id, str(record.get("updateType") or "weekly")))
        elif key not in (None, ""):
            result.append(make_record(key, "weekly"))
    return result


def dedupe_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records:
        drama_id = str(record.get("dramaId") or "").strip()
        if drama_id and drama_id not in seen:
            seen.add(drama_id)
            result.append(dict(record))
    return result


def merge_records(
    *,
    weekly: list[dict[str, object]] | None = None,
    daily: list[dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for record in weekly or []:
        drama_id = str(record.get("dramaId") or "").strip()
        if drama_id:
            records[drama_id] = dict(record)
    for record in daily or []:
        drama_id = str(record.get("dramaId") or "").strip()
        if drama_id and drama_id not in records:
            records[drama_id] = dict(record)
    return records


def build_payload(platform: str, records: dict[str, dict[str, object]], *, generated_at: str | None = None) -> dict:
    return {
        "version": 1,
        "updatedAt": generated_at or now_iso(),
        "platform": platform,
        "records": records,
    }


def upload_payload(
    platform: str,
    payload: dict,
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    dry_run: bool = False,
    dry_run_dir: Path = HERE,
) -> Path | None:
    serialized = json.dumps(payload, ensure_ascii=False)
    key = ONGOING_KEYS[platform]
    if dry_run:
        dry_run_dir.mkdir(parents=True, exist_ok=True)
        output_path = dry_run_dir / f"ongoing-{platform}.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[dry-run] wrote {output_path}: "
            f"{len(payload.get('records') or {})} records for {key}"
        )
        return output_path
    result = upstash(["SET", key, serialized])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {key}: {result!r}")
    print(f"[ok] uploaded {key}: {len(payload.get('records') or {})} records")
    return None


def build_missevan_timeline_headers() -> dict[str, str] | None:
    raw_headers = os.environ.get("MISSEVAN_TIMELINE_HEADERS_JSON", "").strip()
    if raw_headers:
        data = json.loads(raw_headers)
        if not isinstance(data, dict):
            raise RuntimeError("MISSEVAN_TIMELINE_HEADERS_JSON must be a JSON object.")
        return {str(key): str(value) for key, value in data.items()}

    authorization = os.environ.get("MISSEVAN_TIMELINE_AUTHORIZATION", "").strip()
    cookie = os.environ.get("MISSEVAN_TIMELINE_COOKIE", "").strip()
    x_m_date = os.environ.get("MISSEVAN_TIMELINE_DATE", "").strip()
    x_m_nonce = os.environ.get("MISSEVAN_TIMELINE_NONCE", "").strip()
    if not all((authorization, cookie, x_m_date, x_m_nonce)):
        return None
    return {
        "user-agent": os.environ.get(
            "MISSEVAN_TIMELINE_USER_AGENT",
            "MissEvanApp/6.6.0 (Android;12;Samsung SM-S9210 e1q)",
        ),
        "channel": "missevan",
        "accept": "application/json",
        "cookie": cookie,
        "authorization": authorization,
        "x-m-date": x_m_date,
        "x-m-nonce": x_m_nonce,
    }


def request_missevan_timeline_json() -> dict | None:
    headers = build_missevan_timeline_headers()
    if not headers:
        return None
    response = requests.get(MISSEVAN_TIMELINE_URL, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("success") is False:
        raise RuntimeError(f"Missevan timeline failed: code={data.get('code')!r}")
    return data


def parse_missevan_weekday(value: object) -> int | None:
    if value not in (None, ""):
        numeric = safe_int(value, -1)
        if 1 <= numeric <= 7:
            return numeric
    text = str(value or "").strip()
    for prefix in ("星期", "周"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    if text in MISSEVAN_WEEKDAY_BY_LABEL:
        return MISSEVAN_WEEKDAY_BY_LABEL[text]
    return None


def parse_missevan_group_weekday(group: dict) -> int | None:
    if "weekday" in group:
        weekday = parse_missevan_weekday(group.get("weekday"))
        if weekday is not None:
            return weekday
    return parse_missevan_weekday(group.get("date_week"))


def parse_missevan_timeline_group_records(group: dict) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for drama in group.get("dramas") or []:
        if not isinstance(drama, dict):
            continue
        if safe_int(drama.get("pay_type"), -1) == 0:
            continue
        drama_id = drama.get("id")
        if drama_id not in (None, ""):
            records.append(make_record(drama_id, "weekly"))
    return records


def parse_missevan_timeline_weekly_buckets(payload: dict) -> list[dict[str, object]]:
    buckets: list[dict[str, object]] = []
    info = payload.get("info") or []
    if not isinstance(info, list):
        return buckets
    for group in info:
        if not isinstance(group, dict):
            continue
        weekday = parse_missevan_group_weekday(group)
        buckets.append(
            {
                "weekday": weekday,
                "dateWeek": str(group.get("date_week") or "").strip(),
                "isToday": safe_int(group.get("is_today")) == 1,
                "records": parse_missevan_timeline_group_records(group),
            }
        )
    return buckets


def parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fresh_missevan_cache_records(
    weekday_cache: object,
    weekday: int | None,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    if weekday is None or not isinstance(weekday_cache, dict):
        return []
    buckets = weekday_cache.get("buckets")
    if not isinstance(buckets, dict):
        return []
    bucket = buckets.get(str(weekday))
    if not isinstance(bucket, dict):
        return []
    observed_at = parse_iso_datetime(bucket.get("observedAt"))
    if observed_at is None:
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - observed_at
    if age > timedelta(days=MISSEVAN_WEEKDAY_CACHE_TTL_DAYS):
        return []
    return records_from_map(bucket.get("records"))


def parse_missevan_timeline_weekly_records(
    payload: dict,
    *,
    weekday_cache: object | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    weekly: list[dict[str, object]] = []
    for bucket in parse_missevan_timeline_weekly_buckets(payload):
        records = bucket.get("records")
        if isinstance(records, list) and records:
            weekly.extend(records)
            continue
        if bucket.get("isToday"):
            weekly.extend(
                fresh_missevan_cache_records(
                    weekday_cache,
                    bucket.get("weekday") if isinstance(bucket.get("weekday"), int) else None,
                    now=now,
                )
            )
    return dedupe_records(weekly)


def build_missevan_weekday_cache_from_timeline(
    payload: dict,
    *,
    existing_cache: object | None = None,
    now: datetime | None = None,
) -> dict:
    current_iso = iso_for_now(now)
    cache: dict[str, object] = {
        "version": 1,
        "platform": "missevan",
        "updatedAt": current_iso,
        "buckets": {},
    }
    if isinstance(existing_cache, dict) and isinstance(existing_cache.get("buckets"), dict):
        cache["buckets"] = {
            str(key): dict(value)
            for key, value in existing_cache.get("buckets", {}).items()
            if isinstance(value, dict)
        }
    buckets = cache["buckets"]
    assert isinstance(buckets, dict)
    for bucket in parse_missevan_timeline_weekly_buckets(payload):
        weekday = bucket.get("weekday")
        records = bucket.get("records")
        if not isinstance(weekday, int) or not isinstance(records, list) or not records:
            continue
        date_week = str(bucket.get("dateWeek") or "").strip() or MISSEVAN_LABEL_BY_WEEKDAY.get(weekday, "")
        buckets[str(weekday)] = {
            "weekday": weekday,
            "dateWeek": date_week,
            "observedAt": current_iso,
            "records": records_to_map(records),
        }
    return cache


def current_beijing_weekday(*, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BEIJING_TZ).isoweekday()


def parse_missevan_summerdrama_group_records(group: object) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not isinstance(group, list):
        return records
    for item in group:
        if not isinstance(item, dict):
            continue
        if safe_int(item.get("pay_type"), -1) == 0:
            continue
        drama_id = item.get("id")
        if drama_id not in (None, ""):
            records.append(make_record(drama_id, "weekly"))
    return records


def parse_missevan_summerdrama_records(payload: dict) -> list[dict[str, object]]:
    weekly: list[dict[str, object]] = []
    info = payload.get("info") or []
    if not isinstance(info, list):
        return weekly
    for group in info:
        weekly.extend(parse_missevan_summerdrama_group_records(group))
    return dedupe_records(weekly)


def parse_missevan_summerdrama_weekly_buckets(
    payload: dict,
    *,
    current_weekday: int,
) -> dict[int, list[dict[str, object]]]:
    info = payload.get("info") or []
    if not isinstance(info, list):
        return {}
    buckets: dict[int, list[dict[str, object]]] = {}
    for index, group in enumerate(info):
        weekday = ((current_weekday - 2 + index) % 7) + 1
        buckets[weekday] = parse_missevan_summerdrama_group_records(group)
    return buckets


def parse_missevan_summerdrama_weekday_records(
    payload: dict,
    weekday: int,
    *,
    current_weekday: int | None = None,
) -> list[dict[str, object]]:
    active_weekday = current_weekday or current_beijing_weekday()
    buckets = parse_missevan_summerdrama_weekly_buckets(payload, current_weekday=active_weekday)
    return buckets.get(weekday, [])


def timeline_today_empty_weekday(payload: dict) -> int | None:
    for bucket in parse_missevan_timeline_weekly_buckets(payload):
        records = bucket.get("records")
        if bucket.get("isToday") and (not isinstance(records, list) or not records):
            weekday = bucket.get("weekday")
            return weekday if isinstance(weekday, int) else None
    return None


def load_missevan_weekday_cache(
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
) -> object:
    return load_upstash_json(MISSEVAN_WEEKDAY_CACHE_KEY, upstash=upstash)


def save_missevan_weekday_cache(
    cache: dict,
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
) -> None:
    set_upstash_json(MISSEVAN_WEEKDAY_CACHE_KEY, cache, upstash=upstash)


def parse_missevan_seed_spec(spec: str) -> tuple[int, list[str]]:
    weekday_text, separator, ids_text = spec.partition(":")
    if not separator:
        raise ValueError(f"Invalid seed spec {spec!r}; expected WEEKDAY:DRAMA_ID[,DRAMA_ID...]")
    weekday = parse_missevan_weekday(weekday_text)
    if weekday is None:
        raise ValueError(f"Invalid weekday in seed spec {spec!r}; expected 1-7 or 一/二/三/四/五/六/日")
    drama_ids = [item.strip() for item in ids_text.split(",") if item.strip()]
    if not drama_ids:
        raise ValueError(f"Invalid seed spec {spec!r}; expected at least one drama ID")
    return weekday, drama_ids


def seed_missevan_weekday_cache(
    specs: list[str],
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    now: datetime | None = None,
) -> dict:
    current_iso = iso_for_now(now)
    existing = load_missevan_weekday_cache(upstash=upstash)
    cache: dict[str, object] = {
        "version": 1,
        "platform": "missevan",
        "updatedAt": current_iso,
        "buckets": {},
    }
    if isinstance(existing, dict) and isinstance(existing.get("buckets"), dict):
        cache["buckets"] = {
            str(key): dict(value)
            for key, value in existing.get("buckets", {}).items()
            if isinstance(value, dict)
        }
    buckets = cache["buckets"]
    assert isinstance(buckets, dict)
    for spec in specs:
        weekday, drama_ids = parse_missevan_seed_spec(spec)
        existing_bucket = buckets.get(str(weekday))
        existing_records = (
            records_to_map(records_from_map(existing_bucket.get("records")))
            if isinstance(existing_bucket, dict)
            else {}
        )
        records = {
            **existing_records,
            **records_to_map([make_record(drama_id, "weekly") for drama_id in drama_ids]),
        }
        buckets[str(weekday)] = {
            "weekday": weekday,
            "dateWeek": (
                str(existing_bucket.get("dateWeek") or "").strip()
                if isinstance(existing_bucket, dict)
                else ""
            )
            or MISSEVAN_LABEL_BY_WEEKDAY.get(weekday, ""),
            "observedAt": current_iso,
            "records": records,
        }
    save_missevan_weekday_cache(cache, upstash=upstash)
    print(f"[ok] seeded {MISSEVAN_WEEKDAY_CACHE_KEY}: {len(specs)} bucket(s)")
    return cache


def fetch_missevan_weekly_records(
    requester: MissevanRequester,
    *,
    fetch_timeline: Callable[[], dict | None] = request_missevan_timeline_json,
    sync_weekday_cache: bool = True,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    try:
        timeline = fetch_timeline()
        if timeline:
            weekday_cache = None
            cache_load_failed = False
            try:
                weekday_cache = load_missevan_weekday_cache()
            except Exception as exc:
                cache_load_failed = True
                print(f"  [missevan] WARN: weekday cache load failed: {exc}")
            timeline_records = parse_missevan_timeline_weekly_records(timeline, weekday_cache=None, now=now)
            if not timeline_records:
                records = []
            else:
                records = timeline_records
            today_empty_weekday = timeline_today_empty_weekday(timeline)
            if records and today_empty_weekday is not None:
                cache_records = fresh_missevan_cache_records(weekday_cache, today_empty_weekday, now=now)
                if cache_records:
                    records = dedupe_records(timeline_records + cache_records)
                else:
                    try:
                        summerdrama = requester.request_json(MISSEVAN_SUMMERDRAMA_URL)
                    except Exception as exc:
                        raise MissevanTodayFallbackError(
                            "Missevan today timeline bucket is empty and summerdrama fallback failed."
                        ) from exc
                    summer_records = parse_missevan_summerdrama_weekday_records(
                        summerdrama,
                        today_empty_weekday,
                        current_weekday=current_beijing_weekday(now=now),
                    )
                    if not summer_records:
                        raise MissevanTodayFallbackError(
                            "Missevan today timeline bucket is empty and summerdrama has no records for today."
                        )
                    records = dedupe_records(timeline_records + summer_records)
            if sync_weekday_cache and not cache_load_failed and timeline_records:
                try:
                    next_cache = build_missevan_weekday_cache_from_timeline(
                        timeline,
                        existing_cache=weekday_cache,
                        now=now,
                    )
                    save_missevan_weekday_cache(next_cache)
                except Exception as exc:
                    print(f"  [missevan] WARN: weekday cache save failed: {exc}")
            if records:
                print(f"  [missevan] timeline weekly records={len(records)}")
                return records
            print("  [missevan] WARN: timeline returned no weekly records; falling back to summerdrama")
    except MissevanTodayFallbackError:
        raise
    except Exception as exc:
        print(f"  [missevan] WARN: timeline fetch failed; falling back to summerdrama: {exc}")

    data = requester.request_json(MISSEVAN_SUMMERDRAMA_URL)
    return parse_missevan_summerdrama_records(data)


def parse_missevan_sound_entries(html: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    pattern = re.compile(
        r'href="/sound/(?P<sound_id>\d+)"'
        r'(?P<body>.*?vw-frontsound-commentcount\s+floatleft">\s*(?P<comments>[\d,]+)\s*</div>)',
        re.DOTALL,
    )
    for match in pattern.finditer(html or ""):
        body = match.group("body")
        view_match = re.search(r'vw-frontsound-viewcount\s+floatleft">\s*([\d,]+)\s*</div>', body)
        if not view_match:
            continue
        entries.append(
            {
                "soundId": match.group("sound_id"),
                "viewCount": safe_int(view_match.group(1).replace(",", "")),
                "commentCount": safe_int(match.group("comments").replace(",", "")),
            }
        )
    return entries


def fetch_missevan_sound_page(page: int) -> str:
    response = requests.get(MISSEVAN_SOUND_PAGE_URL.format(page=page), headers=MISSEVAN_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def fetch_missevan_sound_create_date(requester: MissevanRequester, sound_id: str) -> date | None:
    data = requester.request_json(MISSEVAN_SOUND_INFO_URL.format(sound_id=sound_id))
    info = data.get("info") or {}
    sound = info.get("sound") if isinstance(info, dict) else {}
    return missevan_timestamp_to_beijing_date((sound or {}).get("create_time"))


def collect_missevan_daily_sound_ids(
    fetch_html: Callable[[int], str] = fetch_missevan_sound_page,
    *,
    requester: MissevanRequester | None = None,
    initial_limit: int = 20,
    batch_size: int = 10,
    max_sound_ids: int = 120,
    now: datetime | None = None,
    limit: int | None = None,
    max_pages: int = 50,
) -> list[str]:
    active_requester = requester or MissevanRequester()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff_date = current.astimezone(BEIJING_TZ).date() - timedelta(days=7)
    effective_max = max(1, safe_int(limit if limit is not None else max_sound_ids, max_sound_ids))
    next_check_count = min(max(1, safe_int(initial_limit, 20)), effective_max)
    step = max(1, safe_int(batch_size, 10))
    sound_ids: list[str] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        html = fetch_html(page)
        entries = parse_missevan_sound_entries(html)
        if not entries:
            break
        for entry in entries:
            sound_id = str(entry.get("soundId") or "")
            if not sound_id or sound_id in seen:
                continue
            if (
                safe_int(entry.get("viewCount")) >= MISSEVAN_DAILY_VIEW_THRESHOLD
                or safe_int(entry.get("commentCount")) >= MISSEVAN_DAILY_COMMENT_THRESHOLD
            ):
                seen.add(sound_id)
                sound_ids.append(sound_id)
                if len(sound_ids) >= next_check_count:
                    create_date = fetch_missevan_sound_create_date(active_requester, sound_id)
                    if create_date is None or create_date < cutoff_date:
                        return sound_ids
                    if len(sound_ids) >= effective_max:
                        return sound_ids
                    next_check_count = min(next_check_count + step, effective_max)
                if len(sound_ids) >= effective_max:
                    return sound_ids
    return sound_ids


def fetch_missevan_daily_drama_ids(
    requester: MissevanRequester,
    sound_ids: list[str],
) -> list[str]:
    drama_ids: list[str] = []
    seen: set[str] = set()
    for sound_id in sound_ids:
        data = requester.request_json(MISSEVAN_SOUND_DRAMA_URL.format(sound_id=sound_id))
        info = data.get("info") or {}
        drama = info.get("drama") if isinstance(info, dict) else {}
        drama_id = (drama or {}).get("id")
        if drama_id in (None, ""):
            continue
        text = str(drama_id)
        if text not in seen:
            seen.add(text)
            drama_ids.append(text)
    return drama_ids


def fetch_missevan_records(
    requester: MissevanRequester | None = None,
    *,
    sync_weekday_cache: bool = True,
) -> dict[str, dict[str, object]]:
    active_requester = requester or MissevanRequester()
    weekly = fetch_missevan_weekly_records(active_requester, sync_weekday_cache=sync_weekday_cache)
    sound_ids = collect_missevan_daily_sound_ids(requester=active_requester)
    daily_ids = fetch_missevan_daily_drama_ids(active_requester, sound_ids)
    daily = [make_record(drama_id, "daily") for drama_id in daily_ids]
    return merge_records(weekly=weekly, daily=daily)


def previous_7_beijing_midnight_timestamps(*, now: datetime | None = None) -> list[int]:
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(BEIJING_TZ).date()
    timestamps: list[int] = []
    for days_back in range(7, 0, -1):
        midnight = datetime.combine(today - timedelta(days=days_back), datetime.min.time(), tzinfo=BEIJING_TZ)
        timestamps.append(int(midnight.timestamp() * 1000))
    return timestamps


def manbo_labels(item: dict) -> list[str]:
    drama = item.get("radioDramaResp") or {}
    return [
        str(label.get("name") or "").strip()
        for label in drama.get("categoryLabels") or []
        if isinstance(label, dict) and str(label.get("name") or "").strip()
    ]


def is_paid_manbo_ongoing_item(item: dict) -> bool:
    drama = item.get("radioDramaResp") or {}
    price = safe_int(drama.get("price"))
    member_price = safe_int(drama.get("memberPrice"))
    vip_free = safe_int(drama.get("vipFree"))
    return vip_free == 1 or price > 0 or member_price > 0


def manbo_update_time_allowed(value: object) -> bool:
    match = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return False
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    return 9 * 60 + 59 <= minutes <= 20 * 60 + 1


def manbo_item_allowed(item: dict) -> bool:
    title = str(item.get("updateSetTitle") or "")
    if any(word in title for word in BLOCKED_UPDATE_TITLE_WORDS):
        return False
    if "全一期" in manbo_labels(item):
        return False
    return is_paid_manbo_ongoing_item(item) and manbo_update_time_allowed(item.get("workUpdateTimeFormat"))


def manbo_ongoing_category(item: dict) -> int:
    drama = item.get("radioDramaResp") or {}
    title = normalize(drama.get("title"))
    override = MANBO_CATALOG_OVERRIDES.get(title) if title else None
    if override is not None:
        return safe_int(override.get("catalog"))
    return safe_int(drama.get("category"))


def collect_manbo_records_from_items(items: list[dict]) -> dict[str, dict[str, object]]:
    weekly: list[dict[str, object]] = []
    daily: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or not manbo_item_allowed(item):
            continue
        drama = item.get("radioDramaResp") or {}
        drama_id = drama.get("radioDramaIdStr") or item.get("id")
        if drama_id in (None, ""):
            continue
        drama_id = str(drama_id)
        category = manbo_ongoing_category(item)
        if category == 1:
            weekly.append(make_record(drama_id, "weekly"))
        elif category == 5:
            daily.append(make_record(drama_id, "daily"))
    return merge_records(weekly=weekly, daily=daily)


def extract_manbo_items(payload: dict) -> list[dict]:
    body = payload.get("b") or payload.get("data") or {}
    items = body.get("itemTimeRespList") if isinstance(body, dict) else []
    return [item for item in (items or []) if isinstance(item, dict)]


def fetch_manbo_records() -> dict[str, dict[str, object]]:
    items: list[dict] = []
    for timestamp in previous_7_beijing_midnight_timestamps():
        data = request_manbo_json(MANBO_TIME_DETAIL_URL.format(timestamp=timestamp))
        items.extend(extract_manbo_items(data))
    return collect_manbo_records_from_items(items)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch ongoing drama records from Missevan and Manbo")
    platform_group = parser.add_mutually_exclusive_group()
    platform_group.add_argument("--missevan-only", action="store_true", help="Only process Missevan")
    platform_group.add_argument("--manbo-only", action="store_true", help="Only process Manbo")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without uploading to Upstash")
    parser.add_argument(
        "--seed-missevan-weekday-cache",
        action="append",
        default=[],
        metavar="WEEKDAY:DRAMA_ID[,DRAMA_ID...]",
        help="Seed one Missevan weekday cache bucket, e.g. 3:93038 for Wednesday.",
    )
    args = parser.parse_args()

    do_missevan = not args.manbo_only
    do_manbo = not args.missevan_only

    if args.seed_missevan_weekday_cache:
        seed_missevan_weekday_cache(args.seed_missevan_weekday_cache)

    if do_missevan:
        print("=== Fetching Missevan ongoing dramas ===")
        records = fetch_missevan_records(sync_weekday_cache=not args.dry_run)
        upload_payload("missevan", build_payload("missevan", records), dry_run=args.dry_run)

    if do_manbo:
        print("=== Fetching Manbo ongoing dramas ===")
        records = fetch_manbo_records()
        upload_payload("manbo", build_payload("manbo", records), dry_run=args.dry_run)

    print("=== Done ===")


if __name__ == "__main__":
    main()
