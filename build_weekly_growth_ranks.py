from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from platform_sync import (
    MANBO_CATALOG_NAME_ALIASES,
    MANBO_CATALOG_NAME_BY_ID,
    MISSEVAN_CATALOG_NAME_BY_ID,
    manbo_main_cv_display_names,
    missevan_main_cv_entries,
    normalize,
    save_json,
)
from sync_new_drama_ids import (
    MANBO_INFO_KEY,
    MISSEVAN_INFO_KEY,
    ROOT,
    assert_info_download_is_safe,
    configure_stdio,
    decode_remote_info_payload,
    decode_watchcount_history,
    load_env_file,
    upstash_request,
    watchcount_key,
)
from upstash_v2 import publish_rank_string


RANK_KEY = "ranks:weekly-growth:latest"
OUTPUT_PATH = Path(__file__).resolve().parent / "ranks-weekly-growth.json"
PLATFORMS = ("missevan", "manbo")
PERIOD_DAYS = {"weekly": 7, "fourWeek": 28}
TOP_N = 50
CREATE_MONTH_PATTERN = re.compile(r"^(\d{4})[./-](\d{1,2})(?:[./-](\d{1,2}))?$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date(value: str, *, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be YYYY-MM-DD: {value!r}") from exc


def info_indexes(payloads: dict[str, object]) -> dict[str, dict[str, dict]]:
    missevan_payload = payloads["missevan"]
    manbo_payload = payloads["manbo"]
    if not isinstance(missevan_payload, dict):
        raise RuntimeError(f"{MISSEVAN_INFO_KEY} must be a JSON object.")
    if not isinstance(manbo_payload, dict) or not isinstance(manbo_payload.get("records"), list):
        raise RuntimeError(f"{MANBO_INFO_KEY}.records must be a JSON array.")

    missevan: dict[str, dict] = {}
    for field, value in missevan_payload.items():
        if isinstance(value, dict):
            drama_id = normalize(value.get("dramaId") or field)
            if drama_id:
                missevan[drama_id] = value

    manbo: dict[str, dict] = {}
    for value in manbo_payload["records"]:
        if isinstance(value, dict):
            drama_id = normalize(value.get("dramaId"))
            if drama_id:
                manbo[drama_id] = value
    return {"missevan": missevan, "manbo": manbo}


def history_date_set(history: dict[str, dict]) -> set[str]:
    return {
        str(point[0])
        for entry in history.values()
        for point in entry.get("points") or []
        if isinstance(point, list) and len(point) == 2
    }


def latest_history_date(platform: str, history: dict[str, dict]) -> date:
    dates = history_date_set(history)
    if not dates:
        raise RuntimeError(f"{watchcount_key(platform, 'history')} contains no data points.")
    return max(parse_date(item, label=f"{platform} history date") for item in dates)


def select_baseline_date(end_date: date, days: int, available_dates: set[str]) -> date:
    target = end_date - timedelta(days=days)
    # Exact first; on an equal one-day deviation prefer the earlier snapshot so
    # the measured interval is never shorter than the requested period.
    for candidate in (target, target - timedelta(days=1), target + timedelta(days=1)):
        if candidate.isoformat() in available_dates:
            return candidate
    return target


def point_map(entry: dict) -> dict[str, int | float]:
    return {str(point[0]): point[1] for point in entry.get("points") or []}


def integer_watchcount(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be an integer watch count.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise RuntimeError(f"{label} must be an integer watch count: {value!r}")


def previous_month(value: date) -> tuple[int, int]:
    if value.month == 1:
        return value.year - 1, 12
    return value.year, value.month - 1


def missing_baseline_new_reason(create_time: object, end_date: date) -> str | None:
    text = normalize(create_time)
    if not text:
        return "missingCreateTime"
    match = CREATE_MONTH_PATTERN.match(text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    if match.group(3) is not None:
        try:
            date(year, month, int(match.group(3)))
        except ValueError:
            return None
    if (year, month) in {(end_date.year, end_date.month), previous_month(end_date)}:
        return "createdInEndMonth"
    return None


def truthy_member_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def pay_status(record: dict) -> str | None:
    needpay = record.get("needpay")
    if needpay is False:
        return "免费"
    if needpay is True:
        if truthy_member_value(record.get("is_member")) or truthy_member_value(record.get("vipFree")):
            return "会员"
        return "付费"
    return None


def catalog_name(platform: str, record: dict) -> str | None:
    if platform == "manbo":
        existing = normalize(record.get("catalogName"))
        if existing:
            return MANBO_CATALOG_NAME_ALIASES.get(existing, existing)
        mapping = MANBO_CATALOG_NAME_BY_ID
    else:
        mapping = MISSEVAN_CATALOG_NAME_BY_ID
    value = record.get("catalog")
    if value in (None, ""):
        return None
    try:
        return mapping.get(int(value))
    except (TypeError, ValueError):
        return None


def main_cvs(platform: str, record: dict) -> list[str]:
    if platform == "manbo":
        return manbo_main_cv_display_names(record)
    return [
        normalize(entry.get("display_name"))
        for entry in missevan_main_cv_entries(record)
        if normalize(entry.get("display_name"))
    ]


def build_period_ranking(
    platform: str,
    history: dict[str, dict],
    info: dict[str, dict],
    *,
    end_date: date,
    period_days: int,
) -> list[dict]:
    end_text = end_date.isoformat()
    candidates: list[dict] = []
    for drama_id, entry in history.items():
        metadata = info.get(drama_id)
        if metadata is None:
            continue
        points = point_map(entry)
        if end_text not in points:
            continue
        baseline_date = select_baseline_date(end_date, period_days, set(points))
        baseline_text = baseline_date.isoformat()
        current = integer_watchcount(points[end_text], label=f"{platform}:{drama_id}@{end_text}")
        is_new = False
        new_reason: str | None = None
        if baseline_text in points:
            baseline = integer_watchcount(
                points[baseline_text],
                label=f"{platform}:{drama_id}@{baseline_text}",
            )
        else:
            new_reason = missing_baseline_new_reason(metadata.get("createTime"), end_date)
            if new_reason is None:
                continue
            baseline = 0
            is_new = True
        increase = current - baseline
        if increase <= 0:
            continue
        create_time = normalize(metadata.get("createTime")) or None
        title = normalize(
            metadata.get("seriesTitle") or metadata.get("title") or metadata.get("name")
        ) or None
        candidates.append(
            {
                "rank": 0,
                "platform": platform,
                "dramaId": str(drama_id),
                "title": title,
                "viewCount": current,
                "viewCountIncrease": increase,
                "isNew": is_new,
                "newReason": new_reason,
                "mainCvs": main_cvs(platform, metadata),
                "catalogName": catalog_name(platform, metadata),
                "payStatus": pay_status(metadata),
                "createTime": create_time,
            }
        )

    candidates.sort(
        key=lambda item: (
            -item["viewCountIncrease"],
            -item["viewCount"],
            int(item["dramaId"]),
        )
    )
    result = candidates[:TOP_N]
    for rank, item in enumerate(result, 1):
        item["rank"] = rank
    return result


def load_remote_sources(
    upstash: Callable[[list[object]], object],
) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, dict]]]:
    histories = {
        platform: decode_watchcount_history(
            platform,
            upstash(["HGETALL", watchcount_key(platform, "history")]),
        )
        for platform in PLATFORMS
    }
    info_payloads = {
        "missevan": decode_remote_info_payload(MISSEVAN_INFO_KEY, upstash(["GET", MISSEVAN_INFO_KEY])),
        "manbo": decode_remote_info_payload(MANBO_INFO_KEY, upstash(["GET", MANBO_INFO_KEY])),
    }
    assert_info_download_is_safe(MISSEVAN_INFO_KEY, info_payloads["missevan"])
    assert_info_download_is_safe(MANBO_INFO_KEY, info_payloads["manbo"])
    return histories, info_indexes(info_payloads)


def build_payload(
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    expected_end_date: str | None = None,
    generated_at: str | None = None,
) -> dict:
    expected = (
        parse_date(expected_end_date, label="--expected-end-date")
        if expected_end_date is not None
        else None
    )
    histories, info = load_remote_sources(upstash)
    end_dates = {
        platform: latest_history_date(platform, histories[platform])
        for platform in PLATFORMS
    }
    if expected is not None:
        stale = [
            f"{platform}={end_dates[platform].isoformat()}"
            for platform in PLATFORMS
            if end_dates[platform] != expected
        ]
        if stale:
            raise RuntimeError(
                "Refusing to publish stale weekly growth ranks: expected "
                f"{expected.isoformat()}, got {', '.join(stale)}."
            )

    periods: dict[str, dict[str, dict[str, str]]] = {}
    rankings: dict[str, dict[str, list[dict]]] = {}
    for period, days in PERIOD_DAYS.items():
        periods[period] = {}
        rankings[period] = {}
        for platform in PLATFORMS:
            end_date = end_dates[platform]
            baseline_date = select_baseline_date(
                end_date,
                days,
                history_date_set(histories[platform]),
            )
            periods[period][platform] = {
                "startDate": baseline_date.isoformat(),
                "endDate": end_date.isoformat(),
            }
            rankings[period][platform] = build_period_ranking(
                platform,
                histories[platform],
                info[platform],
                end_date=end_date,
                period_days=days,
            )

    return {
        "version": 1,
        "kind": "weeklyViewGrowth",
        "date": max(end_dates.values()).isoformat(),
        "generated_at": generated_at or now_iso(),
        "statisticsPeriods": periods,
        "rankings": rankings,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从远端 watchcount history 生成 7 天/4 周增量榜")
    parser.add_argument("--no-upload", action="store_true", help="只写本地 JSON，不发布到 Upstash")
    parser.add_argument(
        "--expected-end-date",
        metavar="YYYY-MM-DD",
        help="要求双平台 history 最新日期与该 UTC 日期一致",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    upstash: Callable[[list[object]], object] = upstash_request,
    output_path: Path = OUTPUT_PATH,
) -> int:
    configure_stdio()
    args = parse_args(argv)
    load_env_file(ROOT / ".env")
    payload = build_payload(upstash=upstash, expected_end_date=args.expected_end_date)
    save_json(output_path, payload)
    print(f"[ok] wrote {output_path.name}")
    for period in PERIOD_DAYS:
        counts = ", ".join(
            f"{platform}={len(payload['rankings'][period][platform])}"
            for platform in PLATFORMS
        )
        print(f"[ok] {period}: {counts}")
    if not args.no_upload:
        publish_rank_string(RANK_KEY, payload, scope="cv", upstash=upstash)
        print(f"[ok] published {RANK_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
