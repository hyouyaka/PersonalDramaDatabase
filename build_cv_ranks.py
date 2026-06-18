from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    iter_missevan_nodes,
    load_cache,
    load_json,
    normalize,
    normalize_match,
    save_json,
)
from sync_new_drama_ids import ROOT, configure_stdio, load_env_file, sync_remote_watchcount_if_newer, upstash_request
from sync_new_drama_ids import MANBO_INFO_KEY, MISSEVAN_INFO_KEY
from sync_remote_libraries import fetch_cvid_map_payload, fetch_info_payload, write_payloads


HERE = Path(__file__).resolve().parent
CV_RANKS_PATH = HERE / "ranks-cv.json"
PLATFORMS = ("missevan", "manbo")
CV_TREND_KEYS = {
    "missevan": "ranks:trend:cv:missevan",
    "manbo": "ranks:trend:cv:manbo",
}
CV_TREND_RETENTION_DATES = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def truthy_paid_marker(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def is_paid_missevan_work(node: dict) -> bool:
    return node.get("needpay") is True or truthy_paid_marker(node.get("is_member"))


def is_paid_manbo_work(record: dict) -> bool:
    return record.get("needpay") is True or truthy_paid_marker(record.get("vipFree"))


def parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def latest_watch_count_updated_at(*caches: dict) -> str | None:
    candidates: list[tuple[datetime, str]] = []
    for cache in caches:
        value = ((cache or {}).get("_meta") or {}).get("updated_at")
        parsed = parse_iso_datetime(value)
        if parsed is not None:
            candidates.append((parsed, str(value)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def count_missevan_dramas(store: dict) -> int:
    return sum(1 for _series_title, _season_key, node in iter_missevan_nodes(store or {}) if normalize(node.get("dramaId")))


def count_manbo_dramas(store: dict) -> int:
    return sum(1 for record in (store or {}).get("records") or [] if isinstance(record, dict) and normalize(record.get("dramaId")))


def build_map_indexes(cvid_map: dict) -> tuple[dict[int, set[str]], dict[int, set[str]], dict[str, set[str]], dict[str, str]]:
    missevan_ids: dict[int, set[str]] = {}
    manbo_ids: dict[int, set[str]] = {}
    names: dict[str, set[str]] = {}
    avatars: dict[str, str] = {}
    for key, payload in (cvid_map or {}).items():
        if not isinstance(payload, dict):
            continue
        canonical = normalize(key)
        if not canonical:
            continue
        avatars[canonical] = normalize(payload.get("avatar"))
        for candidate in [canonical, payload.get("displayName"), *(payload.get("aliases") or [])]:
            norm = normalize_match(candidate)
            if norm:
                names.setdefault(norm, set()).add(canonical)
        missevan_id = payload.get("missevanCvId", payload.get("cvId"))
        manbo_id = payload.get("manboCvId")
        if missevan_id not in (None, ""):
            missevan_ids.setdefault(int(missevan_id), set()).add(canonical)
        if manbo_id not in (None, ""):
            manbo_ids.setdefault(int(manbo_id), set()).add(canonical)
    return missevan_ids, manbo_ids, names, avatars


def resolve_cv_name(
    raw_name: object,
    cv_id: int | None,
    *,
    platform: str,
    missevan_ids: dict[int, set[str]],
    manbo_ids: dict[int, set[str]],
    name_index: dict[str, set[str]],
) -> str:
    candidates: set[str] = set()
    if cv_id is not None:
        by_id = missevan_ids if platform == "missevan" else manbo_ids
        candidates.update(by_id.get(int(cv_id), set()))
    if len(candidates) == 1:
        return next(iter(candidates))
    norm = normalize_match(raw_name)
    if norm:
        candidates.update(name_index.get(norm, set()))
    if len(candidates) == 1:
        return next(iter(candidates))
    fallback = normalize(raw_name)
    if fallback:
        return fallback
    prefix = "猫耳CV" if platform == "missevan" else "漫播CV"
    return f"{prefix}_{cv_id}" if cv_id is not None else "未命名CV"


def missevan_main_cv_names(node: dict) -> list[str]:
    cvnames = node.get("cvnames") or {}
    names: list[str] = []
    for cv_id in node.get("maincvs") or []:
        name = normalize(cvnames.get(str(cv_id)))
        if name:
            names.append(name)
    return names


def manbo_main_cv_names(record: dict) -> list[str]:
    return [normalize(name) for name in (record.get("mainCvNames") or record.get("mainCvNicknames") or []) if normalize(name)]


def add_work(buckets: dict[str, dict], cv_name: str, work: dict, *, avatar: str = "") -> None:
    bucket = buckets.setdefault(
        cv_name,
        {
            "cvName": cv_name,
            "avatar": normalize(avatar),
            "totalViewCount": 0,
            "works": [],
        },
    )
    if avatar and not bucket.get("avatar"):
        bucket["avatar"] = normalize(avatar)
    bucket["totalViewCount"] += int(work["viewCount"])
    bucket["works"].append(work)


def collect_missevan_works(
    buckets: dict[str, dict],
    *,
    paid_buckets: dict[str, dict] | None = None,
    store: dict,
    counts: dict,
    missevan_ids: dict[int, set[str]],
    manbo_ids: dict[int, set[str]],
    name_index: dict[str, set[str]],
    avatar_index: dict[str, str],
) -> None:
    for _series_title, _season_key, node in iter_missevan_nodes(store):
        drama_id = normalize(node.get("dramaId"))
        if not drama_id:
            continue
        view_count = safe_int_or_none((counts.get(drama_id) or {}).get("view_count"))
        if view_count is None:
            continue
        main_cvs = missevan_main_cv_names(node)
        title = normalize(node.get("seriesTitle") or node.get("title"))
        is_paid = is_paid_missevan_work(node)
        base_work = {
            "platform": "missevan",
            "dramaId": drama_id,
            "title": title,
            "cover": normalize(node.get("cover")),
            "mainCvs": main_cvs,
            "viewCount": view_count,
            "isPaid": is_paid,
        }
        cvnames = node.get("cvnames") or {}
        for raw_cv_id in node.get("maincvs") or []:
            cv_id = safe_int_or_none(raw_cv_id)
            raw_name = cvnames.get(str(raw_cv_id))
            cv_name = resolve_cv_name(
                raw_name,
                cv_id,
                platform="missevan",
                missevan_ids=missevan_ids,
                manbo_ids=manbo_ids,
                name_index=name_index,
            )
            add_work(buckets, cv_name, dict(base_work), avatar=avatar_index.get(cv_name, ""))
            if is_paid and paid_buckets is not None:
                add_work(paid_buckets, cv_name, dict(base_work), avatar=avatar_index.get(cv_name, ""))


def collect_manbo_works(
    buckets: dict[str, dict],
    *,
    paid_buckets: dict[str, dict] | None = None,
    store: dict,
    counts: dict,
    missevan_ids: dict[int, set[str]],
    manbo_ids: dict[int, set[str]],
    name_index: dict[str, set[str]],
    avatar_index: dict[str, str],
) -> None:
    for record in store.get("records") or []:
        if not isinstance(record, dict):
            continue
        drama_id = normalize(record.get("dramaId"))
        if not drama_id:
            continue
        view_count = safe_int_or_none((counts.get(drama_id) or {}).get("view_count"))
        if view_count is None:
            continue
        main_cvs = manbo_main_cv_names(record)
        is_paid = is_paid_manbo_work(record)
        base_work = {
            "platform": "manbo",
            "dramaId": drama_id,
            "title": normalize(record.get("seriesTitle") or record.get("name")),
            "cover": normalize(record.get("cover")),
            "mainCvs": main_cvs,
            "viewCount": view_count,
            "isPaid": is_paid,
        }
        ids = record.get("mainCvIds") or []
        names = record.get("mainCvNames") or record.get("mainCvNicknames") or []
        for idx, raw_cv_id in enumerate(ids):
            cv_id = safe_int_or_none(raw_cv_id)
            raw_name = names[idx] if idx < len(names) else ""
            cv_name = resolve_cv_name(
                raw_name,
                cv_id,
                platform="manbo",
                missevan_ids=missevan_ids,
                manbo_ids=manbo_ids,
                name_index=name_index,
            )
            add_work(buckets, cv_name, dict(base_work), avatar=avatar_index.get(cv_name, ""))
            if is_paid and paid_buckets is not None:
                add_work(paid_buckets, cv_name, dict(base_work), avatar=avatar_index.get(cv_name, ""))


def sort_work_key(work: dict) -> tuple[int, str, str, str]:
    return (-int(work.get("viewCount") or 0), str(work.get("platform") or ""), str(work.get("title") or ""), str(work.get("dramaId") or ""))


def build_ranking_from_buckets(buckets: dict[str, dict], *, top_n: int) -> list[dict]:
    rankings = sorted(buckets.values(), key=lambda item: (-int(item["totalViewCount"]), item["cvName"]))[:top_n]
    for idx, item in enumerate(rankings, start=1):
        item["rank"] = idx
        item["works"] = sorted(item["works"], key=sort_work_key)
        item["workCount"] = len(item["works"])
    return rankings


def build_cv_rank_outputs(
    *,
    missevan_store: dict,
    manbo_store: dict,
    missevan_counts: dict,
    manbo_counts: dict,
    cvid_map: dict,
    generated_at: str,
    top_n: int = 30,
) -> tuple[dict, dict[str, list[dict]], dict[str, list[dict]]]:
    missevan_ids, manbo_ids, name_index, avatar_index = build_map_indexes(cvid_map)
    missevan_buckets: dict[str, dict] = {}
    manbo_buckets: dict[str, dict] = {}
    paid_missevan_buckets: dict[str, dict] = {}
    paid_manbo_buckets: dict[str, dict] = {}
    collect_missevan_works(
        missevan_buckets,
        paid_buckets=paid_missevan_buckets,
        store=missevan_store,
        counts=missevan_counts,
        missevan_ids=missevan_ids,
        manbo_ids=manbo_ids,
        name_index=name_index,
        avatar_index=avatar_index,
    )
    collect_manbo_works(
        manbo_buckets,
        paid_buckets=paid_manbo_buckets,
        store=manbo_store,
        counts=manbo_counts,
        missevan_ids=missevan_ids,
        manbo_ids=manbo_ids,
        name_index=name_index,
        avatar_index=avatar_index,
    )

    full_rankings = {
        "missevan": build_ranking_from_buckets(missevan_buckets, top_n=len(missevan_buckets)),
        "manbo": build_ranking_from_buckets(manbo_buckets, top_n=len(manbo_buckets)),
    }
    full_paid_rankings = {
        "missevan": build_ranking_from_buckets(paid_missevan_buckets, top_n=len(paid_missevan_buckets)),
        "manbo": build_ranking_from_buckets(paid_manbo_buckets, top_n=len(paid_manbo_buckets)),
    }

    payload = {
        "version": 3,
        "date": generated_at[:10],
        "generated_at": generated_at,
        "source": {"scope": "all", "platforms": list(PLATFORMS)},
        "missevanDramaCount": count_missevan_dramas(missevan_store),
        "manboDramaCount": count_manbo_dramas(manbo_store),
        "rankings": {
            "missevan": full_rankings["missevan"][:top_n],
            "manbo": full_rankings["manbo"][:top_n],
        },
        "paidRankings": {
            "missevan": full_paid_rankings["missevan"][:top_n],
            "manbo": full_paid_rankings["manbo"][:top_n],
        },
    }
    return payload, full_rankings, full_paid_rankings


def build_cv_ranks_payload(
    *,
    missevan_store: dict,
    manbo_store: dict,
    missevan_counts: dict,
    manbo_counts: dict,
    cvid_map: dict,
    generated_at: str,
    top_n: int = 30,
) -> dict:
    payload, _full_rankings, _full_paid_rankings = build_cv_rank_outputs(
        missevan_store=missevan_store,
        manbo_store=manbo_store,
        missevan_counts=missevan_counts,
        manbo_counts=manbo_counts,
        cvid_map=cvid_map,
        generated_at=generated_at,
        top_n=top_n,
    )
    return payload


def _cv_rank_by_name(rankings: list[dict]) -> dict[str, dict]:
    return {str(item.get("cvName") or ""): item for item in rankings if normalize(item.get("cvName"))}


def _copy_cv_trend_sample(sample: object) -> dict | None:
    if not isinstance(sample, dict):
        return None
    metrics = sample.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    copied_metrics = {
        field: safe_int_or_none(metrics.get(field))
        for field in ("totalViewCount", "paidViewCount")
        if metrics.get(field) is not None
    }
    if not copied_metrics:
        return None
    copied: dict[str, object] = {"metrics": copied_metrics}
    if sample.get("generated_at") not in (None, ""):
        copied["generated_at"] = sample.get("generated_at")
    ranks = sample.get("ranks")
    if isinstance(ranks, dict):
        copied["ranks"] = {
            str(key): safe_int_or_none(value)
            for key, value in ranks.items()
            if safe_int_or_none(value) is not None
        }
    return copied


def build_cv_trend_payload(
    current: dict | None,
    platform: str,
    history_date: str,
    total_rankings: list[dict],
    paid_rankings: list[dict],
    *,
    generated_at: str,
    retention_dates: int = CV_TREND_RETENTION_DATES,
) -> dict:
    if platform not in CV_TREND_KEYS:
        raise ValueError(f"Unsupported platform: {platform}")

    payload = current if isinstance(current, dict) else {}
    dates = {str(value) for value in (payload.get("dates") or []) if value not in (None, "")}
    dates.add(history_date)
    kept_dates = sorted(dates)[-retention_dates:]
    kept_date_set = set(kept_dates)

    cvs: dict[str, dict] = {}
    for cv_name, entry in (payload.get("cvs") or {}).items():
        if not isinstance(entry, dict):
            continue
        samples = {}
        for date_key, sample in (entry.get("samples") or {}).items():
            date_text = str(date_key)
            if date_text not in kept_date_set:
                continue
            copied_sample = _copy_cv_trend_sample(sample)
            if copied_sample is not None:
                samples[date_text] = copied_sample
        if not samples:
            continue
        cv_name_text = str(entry.get("cvName") or cv_name)
        copied = {
            "cvName": cv_name_text,
            "samples": samples,
        }
        avatar = normalize(entry.get("avatar"))
        if avatar:
            copied["avatar"] = avatar
        works = entry.get("works")
        if isinstance(works, list):
            copied["works"] = works
        cvs[cv_name_text] = copied

    total_by_name = _cv_rank_by_name(total_rankings)
    paid_by_name = _cv_rank_by_name(paid_rankings)
    for cv_name, total_item in total_by_name.items():
        if not cv_name:
            continue
        paid_item = paid_by_name.get(cv_name)
        entry = cvs.get(cv_name, {"cvName": cv_name, "samples": {}})
        entry["cvName"] = cv_name
        avatar = normalize(total_item.get("avatar"))
        if avatar:
            entry["avatar"] = avatar
        else:
            entry.setdefault("avatar", normalize(entry.get("avatar")))
        entry["works"] = list(total_item.get("works") or [])
        samples = entry.setdefault("samples", {})
        if not isinstance(samples, dict):
            samples = {}
            entry["samples"] = samples
        ranks = {"total": safe_int_or_none(total_item.get("rank"))}
        if paid_item is not None and safe_int_or_none(paid_item.get("rank")) is not None:
            ranks["paid"] = safe_int_or_none(paid_item.get("rank"))
        samples[history_date] = {
            "generated_at": generated_at,
            "metrics": {
                "totalViewCount": int(total_item.get("totalViewCount") or 0),
                "paidViewCount": int((paid_item or {}).get("totalViewCount") or 0),
            },
            "ranks": {key: value for key, value in ranks.items() if value is not None},
        }
        cvs[cv_name] = entry

    dates = sorted(
        {
            str(date_key)
            for entry in cvs.values()
            for date_key in (entry.get("samples") or {})
            if str(date_key) in kept_date_set
        }
    )
    return {
        "version": 1,
        "kind": "cv",
        "platform": platform,
        "updated_at": generated_at,
        "dates": dates,
        "cvs": cvs,
    }


def upload_cv_ranks(payload: dict, *, upstash=upstash_request) -> None:
    encoded = json.dumps(payload, ensure_ascii=False)
    for key in (f"ranks:cv:{payload['date']}", "ranks:cv:latest"):
        result = upstash(["SET", key, encoded])
        if result != "OK":
            raise RuntimeError(f"Failed to upload {key}: {result!r}")
        print(f"[ok] uploaded {key} ({len(encoded)} bytes)")


def load_cv_trend_payload(platform: str, *, upstash=upstash_request) -> dict | None:
    key = CV_TREND_KEYS[platform]
    try:
        raw = upstash(["GET", key])
    except Exception as exc:
        raise RuntimeError(f"Failed to load {key}: {exc}") from exc
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    raise RuntimeError(f"Unsupported payload type for {key}: {type(raw).__name__}")


def upload_cv_trends(
    *,
    history_date: str,
    generated_at: str,
    full_rankings: dict[str, list[dict]],
    full_paid_rankings: dict[str, list[dict]],
    upstash=upstash_request,
) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for platform in PLATFORMS:
        key = CV_TREND_KEYS[platform]
        current = load_cv_trend_payload(platform, upstash=upstash)
        payload = build_cv_trend_payload(
            current,
            platform,
            history_date,
            full_rankings.get(platform) or [],
            full_paid_rankings.get(platform) or [],
            generated_at=generated_at,
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        result = upstash(["SET", key, encoded])
        if result != "OK":
            raise RuntimeError(f"Failed to upload {key}: {result!r}")
        print(f"[ok] uploaded {key} ({len(encoded)} bytes, date={history_date})")
        payloads[platform] = payload
    return payloads


def sync_remote_cvid_map(*, cvid_map_path: Path = COMBINED_CVID_MAP_PATH, upstash=upstash_request) -> dict:
    _path, payload = fetch_cvid_map_payload(path=cvid_map_path, upstash=upstash)
    save_json(cvid_map_path, payload)
    print(f"[ok] downloaded remote data -> {cvid_map_path.name}")
    return payload


def sync_remote_rank_inputs(
    *,
    missevan_info_path: Path = MISSEVAN_INFO_PATH,
    manbo_info_path: Path = MANBO_INFO_PATH,
    cvid_map_path: Path = COMBINED_CVID_MAP_PATH,
    upstash=upstash_request,
) -> dict:
    payloads = [
        fetch_info_payload(MANBO_INFO_KEY, manbo_info_path, upstash=upstash),
        fetch_info_payload(MISSEVAN_INFO_KEY, missevan_info_path, upstash=upstash),
    ]
    cvid_payload = fetch_cvid_map_payload(path=cvid_map_path, upstash=upstash)
    payloads.append(cvid_payload)
    write_payloads(payloads)
    return cvid_payload[1]


def sync_remote_watchcount_inputs(
    *,
    missevan_counts_path: Path = MISSEVAN_COUNTS_PATH,
    manbo_counts_path: Path = MANBO_COUNTS_PATH,
    upstash=upstash_request,
    force: bool = False,
) -> None:
    sync_remote_watchcount_if_newer("missevan", missevan_counts_path, upstash=upstash, force=force)
    sync_remote_watchcount_if_newer("manbo", manbo_counts_path, upstash=upstash, force=force)


def build_and_publish_cv_ranks(
    *,
    missevan_info_path: Path = MISSEVAN_INFO_PATH,
    manbo_info_path: Path = MANBO_INFO_PATH,
    missevan_counts_path: Path = MISSEVAN_COUNTS_PATH,
    manbo_counts_path: Path = MANBO_COUNTS_PATH,
    cvid_map_path: Path = COMBINED_CVID_MAP_PATH,
    output_path: Path = CV_RANKS_PATH,
    upstash=upstash_request,
    generated_at: str | None = None,
    upload: bool = True,
    force: bool = False,
) -> dict:
    sync_remote_watchcount_inputs(
        missevan_counts_path=missevan_counts_path,
        manbo_counts_path=manbo_counts_path,
        upstash=upstash,
        force=force,
    )
    missevan_cache = load_cache(missevan_counts_path)
    manbo_cache = load_cache(manbo_counts_path)
    generated = generated_at or latest_watch_count_updated_at(missevan_cache, manbo_cache) or now_iso()
    cvid_map = sync_remote_rank_inputs(
        missevan_info_path=missevan_info_path,
        manbo_info_path=manbo_info_path,
        cvid_map_path=cvid_map_path,
        upstash=upstash,
    )
    payload, full_rankings, full_paid_rankings = build_cv_rank_outputs(
        missevan_store=load_json(missevan_info_path, {}),
        manbo_store=load_json(manbo_info_path, {"records": []}),
        missevan_counts=missevan_cache.get("counts", {}),
        manbo_counts=manbo_cache.get("counts", {}),
        cvid_map=cvid_map,
        generated_at=generated,
    )
    save_json(output_path, payload)
    if upload:
        upload_cv_ranks(payload, upstash=upstash)
        upload_cv_trends(
            history_date=payload["date"],
            generated_at=payload["generated_at"],
            full_rankings=full_rankings,
            full_paid_rankings=full_paid_rankings,
            upstash=upstash,
        )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CV view-count rankings from local platform libraries")
    parser.add_argument("--no-upload", action="store_true", help="Only write ranks-cv.json locally")
    parser.add_argument("--date", help="Override the output date and Upstash date key (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="构建前无条件拉取远端 watchcount latest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    generated_at = None
    if args.date:
        generated_at = f"{args.date}T00:00:00+00:00"
    payload = build_and_publish_cv_ranks(generated_at=generated_at, upload=not args.no_upload, force=args.force)
    rankings = payload.get("rankings") or {}
    missevan_count = len(rankings.get("missevan") or [])
    manbo_count = len(rankings.get("manbo") or [])
    print(
        f"[ok] wrote {CV_RANKS_PATH.name}: "
        f"missevan_rankings={missevan_count}, manbo_rankings={manbo_count}, date={payload.get('date')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
