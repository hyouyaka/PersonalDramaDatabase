from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    GENRE_BY_TYPE,
    MANBO_CATALOG_NAME_BY_ID,
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_CATALOG_NAME_BY_ID,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    iter_missevan_nodes,
    is_excluded_drama,
    load_cache,
    load_json,
    manbo_main_cv_display_names,
    manbo_main_cv_entries,
    missevan_main_cv_entries,
    normalize,
    normalize_match,
    save_json,
)
from sync_new_drama_ids import ROOT, configure_stdio, load_env_file, sync_remote_watchcount_if_newer, upstash_request
from sync_new_drama_ids import CVID_MAP_KEY, MANBO_INFO_KEY, MISSEVAN_INFO_KEY, SERIES_INFO_KEY, decode_remote_json_payload
from sync_remote_libraries import fetch_cvid_map_payload, fetch_info_payload, write_payloads
from upstash_editor import decode_hgetall
from upstash_v2 import (
    CV_TREND_V2_KEY,
    publish_cv_trend_v2,
    publish_hash_snapshot_atomic,
    publish_rank_string,
    publish_trend_v2_best_effort,
)
from update_series_info import id_sort, normalize_title, parse_title, without_cv_metadata


HERE = Path(__file__).resolve().parent
CV_RANKS_PATH = HERE / "ranks-cv.json"
PARTNER_OVERRIDES_KEY = "drama:partner-overrides:v1"
MIN_PARTNER_WORK_COUNT = 4
RECORDING_EDITION = re.compile(
    r"[([【]\s*(原版|重[录錄]版)\s*[)\]】]|(?<!\S)(原版|重[录錄]版)(?!\S)"
)
PLATFORMS = ("missevan", "manbo")
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
    return sum(
        1 for _series_title, _season_key, node in iter_missevan_nodes(store or {})
        if normalize(node.get("dramaId")) and not is_excluded_drama("missevan", node["dramaId"])
    )


def count_manbo_dramas(store: dict) -> int:
    return sum(
        1 for record in (store or {}).get("records") or []
        if isinstance(record, dict) and normalize(record.get("dramaId"))
        and not is_excluded_drama("manbo", record["dramaId"])
    )


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
    return [
        normalize(entry["display_name"])
        for entry in missevan_main_cv_entries(node)
        if normalize(entry["display_name"])
    ]


def manbo_main_cv_names(record: dict) -> list[str]:
    return manbo_main_cv_display_names(record)


def add_work(
    buckets: dict, cv_name: str, work: dict, *, avatar: str = "", bucket_key: str | None = None,
) -> None:
    bucket = buckets.setdefault(
        bucket_key if bucket_key is not None else cv_name,
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
        if not drama_id or is_excluded_drama("missevan", drama_id):
            continue
        count_entry = counts.get(drama_id)
        if not isinstance(count_entry, dict) or "view_count" not in count_entry:
            continue
        raw_view_count = count_entry.get("view_count")
        if raw_view_count is None:
            view_count = 0
        else:
            view_count = safe_int_or_none(raw_view_count)
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
        for cv_entry in missevan_main_cv_entries(node):
            if cv_entry.get("unknown"):
                continue
            cv_id = cv_entry["cv_id"]
            raw_name = cv_entry["display_name"]
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
        if not drama_id or is_excluded_drama("manbo", drama_id):
            continue
        count_entry = counts.get(drama_id)
        if not isinstance(count_entry, dict) or "view_count" not in count_entry:
            continue
        raw_view_count = count_entry.get("view_count")
        if raw_view_count is None:
            view_count = 0
        else:
            view_count = safe_int_or_none(raw_view_count)
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
        for entry in manbo_main_cv_entries(record):
            cv_id = entry["cv_id"]
            raw_name = entry["display_name"]
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


def build_series_index(series_info: dict) -> dict[tuple[str, str], tuple[str, str]]:
    if not isinstance(series_info, dict):
        raise ValueError("series-info must be a JSON object")
    platforms = {"猫耳": "missevan", "漫播": "manbo"}
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for key, entry in series_info.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
            raise ValueError("Invalid series-info entry")
        platform_name = entry.get("platform")
        platform = platforms.get(platform_name) if isinstance(platform_name, str) else None
        title = entry.get("series title")
        category = entry.get("category")
        ids = entry.get("dramaIds")
        if (
            platform is None or not isinstance(title, str) or not title.strip()
            or not isinstance(category, str) or not category.strip() or not isinstance(ids, list)
        ):
            raise ValueError(f"Invalid series-info fields: {key}")
        for raw_id in ids:
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not normalize(raw_id):
                raise ValueError(f"Invalid series-info dramaId: {key}")
            identity = (platform, normalize(raw_id))
            if identity in index and index[identity][0] != key:
                raise ValueError(f"Multiple series own {platform}:{identity[1]}")
            index[identity] = (key, normalize(title))
    return index


def fetch_remote_series_info(*, upstash=upstash_request) -> dict:
    payload = decode_remote_json_payload(SERIES_INFO_KEY, upstash(["GET", SERIES_INFO_KEY]))
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"Refusing to use empty or invalid {SERIES_INFO_KEY}")
    build_series_index(payload)
    return payload


def fetch_partner_overrides(*, upstash=upstash_request) -> dict:
    raw = upstash(["GET", PARTNER_OVERRIDES_KEY])
    if raw is None:
        return {}
    payload = decode_remote_json_payload(PARTNER_OVERRIDES_KEY, raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{PARTNER_OVERRIDES_KEY} must be a JSON object")
    return payload


def build_series_count_rankings(
    *, missevan_store: dict, manbo_store: dict, series_info: dict, cvid_map: dict, top_n: int = 30,
    edition_aliases: dict[str, tuple[str, str]] | None = None,
) -> dict[str, list[dict]]:
    series_index = build_series_index(series_info)
    missevan_ids, manbo_ids, name_index, avatars = build_map_indexes(cvid_map)
    result: dict[str, list[dict]] = {}
    for platform, records, entries, title_key in (
        ("missevan", (node for _, _, node in iter_missevan_nodes(missevan_store)), missevan_main_cv_entries, "title"),
        ("manbo", manbo_store.get("records") or [], manbo_main_cv_entries, "name"),
    ):
        buckets: dict[str, dict[str, dict]] = {}
        # Traverse active info only: retained series members cannot resurrect archived works.
        for record in records:
            if not isinstance(record, dict):
                continue
            drama_id = normalize(record.get("dramaId"))
            if not drama_id or is_excluded_drama(platform, drama_id):
                continue
            series_key, title = series_index.get(
                (platform, drama_id),
                (f"{platform}:drama:{drama_id}", normalize(record.get("seriesTitle") or record.get(title_key))),
            )
            if edition_aliases is not None:
                series_key, title = edition_aliases.get(series_key, (series_key, title))
            for entry in entries(record):
                if entry.get("unknown"):
                    continue
                cv_name = resolve_cv_name(
                    entry["display_name"], entry["cv_id"], platform=platform,
                    missevan_ids=missevan_ids, manbo_ids=manbo_ids, name_index=name_index,
                )
                work = buckets.setdefault(cv_name, {}).setdefault(
                    series_key, {"seriesKey": series_key, "title": title, "dramaIds": set()},
                )
                work["dramaIds"].add(drama_id)
        rankings = []
        previous_count = None
        rank = 0
        for position, (cv_name, works) in enumerate(
            sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])), start=1,
        ):
            if len(works) != previous_count:
                rank = position
            if rank > top_n:
                break
            previous_count = len(works)
            rankings.append({
                "rank": rank, "cvName": cv_name, "avatar": avatars.get(cv_name, ""),
                "seriesCount": len(works),
                "works": [
                    {
                        **work,
                        "dramaIds": sorted(
                            work["dramaIds"],
                            key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
                        ),
                    }
                    for work in sorted(works.values(), key=lambda work: (work["title"], work["seriesKey"]))
                ],
            })
        result[platform] = rankings
    return result


def build_series_count_meta(
    *, missevan_store: dict, manbo_store: dict, cvid_map: dict, series_info: dict, generated_at: str,
) -> dict[str, dict]:
    def content_version(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    shared_versions = {
        CVID_MAP_KEY: content_version(cvid_map),
        SERIES_INFO_KEY: content_version(series_info),
    }
    platform_meta = {
        platform: {
            "generated_at": generated_at,
            "sourceVersions": {info_key: content_version(store), **shared_versions},
        }
        for platform, info_key, store in (
            ("missevan", MISSEVAN_INFO_KEY, missevan_store),
            ("manbo", MANBO_INFO_KEY, manbo_store),
        )
    }
    platform_meta["combined"] = {
        "generated_at": generated_at,
        "sourceVersions": {
            **platform_meta["missevan"]["sourceVersions"],
            **platform_meta["manbo"]["sourceVersions"],
        },
    }
    return platform_meta


@dataclass(frozen=True)
class RankedCv:
    key: str
    name: str
    platform_id: str
    canonical: bool


@dataclass
class RankedDrama:
    platform: str
    drama_id: str
    title: str
    series_key: str
    series_title: str
    genre: str
    medium: str
    cast: dict[str, RankedCv]
    complete_mapped_cast: bool

    @property
    def identity(self) -> tuple[str, str]:
        return self.platform, self.drama_id


def collect_ranked_dramas(
    missevan_store: dict, manbo_store: dict, series_info: dict, cvid_map: dict,
) -> list[RankedDrama]:
    series_index = build_series_index(series_info)
    missevan_ids, manbo_ids, _, _ = build_map_indexes(cvid_map)
    result = {}
    for platform, records, entries, title_key, catalog_names, platform_ids in (
        ("missevan", (row for _, _, row in iter_missevan_nodes(missevan_store)),
         missevan_main_cv_entries, "title", MISSEVAN_CATALOG_NAME_BY_ID, missevan_ids),
        ("manbo", manbo_store.get("records") or [],
         manbo_main_cv_entries, "name", MANBO_CATALOG_NAME_BY_ID, manbo_ids),
    ):
        for record in records:
            if not isinstance(record, dict):
                continue
            drama_id = normalize(record.get("dramaId"))
            if not drama_id or is_excluded_drama(platform, drama_id) or (platform, drama_id) in result:
                continue
            title = normalize(record.get(title_key) or record.get("seriesTitle"))
            series_key, series_title = series_index.get(
                (platform, drama_id), (f"{platform}:drama:{drama_id}", title),
            )
            cast = {}
            mapped = True
            for entry in entries(record):
                cv_id, raw_name = entry["cv_id"], entry["display_name"]
                if entry.get("unknown") or not (cv_id or raw_name):
                    mapped = False
                    continue
                candidates = platform_ids.get(cv_id, set())
                canonical = len(candidates) == 1
                if canonical:
                    name = next(iter(candidates))
                    key = f"canonical:{name}"
                else:
                    prefix = "猫耳CV" if platform == "missevan" else "漫播CV"
                    name = raw_name or f"{prefix}_{cv_id}"
                    key = f"{platform}:id:{cv_id}" if cv_id else f"{platform}:name:{name}"
                mapped = mapped and canonical
                cast.setdefault(key, RankedCv(key, name, normalize(cv_id), canonical))
            drama = RankedDrama(
                platform, drama_id, title, series_key, series_title,
                GENRE_BY_TYPE.get(safe_int_or_none(record.get("type")), ""),
                catalog_names.get(safe_int_or_none(record.get("catalog")), ""),
                cast, bool(cast) and mapped,
            )
            result[drama.identity] = drama
    return list(result.values())


def split_recording_edition(title: str) -> tuple[str, set[str]]:
    text = unicodedata.normalize("NFKC", title)
    editions = {
        (match[1] or match[2]).replace("錄", "录")
        for match in RECORDING_EDITION.finditer(without_cv_metadata(text))
    }
    return RECORDING_EDITION.sub(" ", text).strip() if editions else title, editions


@dataclass
class SeriesMatch:
    platform: str
    title: str
    editions: set[str]
    cast: set[str]
    complete_mapped_cast: bool


def series_match_candidates(
    members: dict[str, list[RankedDrama]],
) -> tuple[dict[tuple[str, str, str], list[str]], dict[str, SeriesMatch]]:
    candidates = defaultdict(list)
    matches = {}
    for series_key, releases in members.items():
        titles_and_types = set()
        cast = set()
        editions = set()
        for drama in releases:
            base_title, series_editions = split_recording_edition(drama.series_title)
            _, release_editions = split_recording_edition(drama.title)
            title = normalize_title(parse_title(base_title)[0])
            medium = re.escape(drama.medium)
            if medium:
                stripped = re.sub(rf"(?:\({medium}\)|【{medium}】|{medium})$", "", title)
                title = normalize_title(stripped) or title
            titles_and_types.add((title, drama.medium, drama.genre))
            editions.update(series_editions | release_editions)
            cast.update(drama.cast)
        if len(titles_and_types) != 1:
            continue
        signature, = titles_and_types
        if not all(signature):
            continue
        matches[series_key] = SeriesMatch(
            releases[0].platform, split_recording_edition(releases[0].series_title)[0],
            editions, cast, all(row.complete_mapped_cast for row in releases),
        )
        candidates[signature].append(series_key)
    return candidates, matches


def build_work_indexes(
    dramas: list[RankedDrama],
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    members = defaultdict(list)
    for drama in dramas:
        members[drama.series_key].append(drama)

    candidates, matches = series_match_candidates(members)
    local_editions = {}
    for keys in candidates.values():
        for platform in PLATFORMS:
            platform_keys = [key for key in keys if matches[key].platform == platform]
            if len(platform_keys) != 2:
                continue
            first, second = sorted(platform_keys, key=lambda key: ("重录版" in matches[key].editions, key))
            original, rerecording = matches[first], matches[second]
            if (
                original.editions == {"原版"} and rerecording.editions == {"重录版"}
                and original.cast and original.cast == rerecording.cast
            ):
                local_editions[first] = local_editions[second] = (first, original.title)

    grouped_members = defaultdict(list)
    for key, releases in members.items():
        group_key, _ = local_editions.get(key, (key, ""))
        grouped_members[group_key].extend(releases)

    candidates, matches = series_match_candidates(grouped_members)
    cross_platform = {}
    for keys in candidates.values():
        by_cast = defaultdict(list)
        for key in keys:
            match = matches[key]
            if match.complete_mapped_cast:
                by_cast[frozenset(match.cast)].append(key)
        for cast_keys in by_cast.values():
            if len(cast_keys) != 2:
                continue
            first, second = sorted(cast_keys, key=lambda key: matches[key].platform != "missevan")
            if matches[first].platform != matches[second].platform:
                cross_platform[first] = cross_platform[second] = (first, matches[first].title)

    combined = {}
    for key, releases in members.items():
        local = local_editions.get(key, (key, releases[0].series_title))
        combined[key] = cross_platform.get(local[0], local)
    return local_editions, combined


def works_by_release(full_rankings: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    return {
        (work["platform"], work["dramaId"]): work
        for platform_rankings in full_rankings.values()
        for row in platform_rankings for work in row["works"]
    }


def build_combined_view_rankings(
    dramas: list[RankedDrama], release_works: dict[tuple[str, str], dict],
    avatars: dict[str, str], work_index: dict[str, tuple[str, str]], top_n: int,
) -> list[dict]:
    buckets = {}
    counted_works = defaultdict(set)
    for drama in dramas:
        work = release_works.get(drama.identity)
        if work is None:
            continue
        series_key, _ = work_index[drama.series_key]
        for cv in drama.cast.values():
            avatar = avatars.get(cv.name, "") if cv.canonical else ""
            add_work(buckets, cv.name, work, avatar=avatar, bucket_key=cv.key)
            counted_works[cv.key].add(series_key)
    for key, row in buckets.items():
        row["cvKey"] = key
    rankings = build_ranking_from_buckets(buckets, top_n=top_n)
    for row in rankings:
        row["workCount"] = len(counted_works[row["cvKey"]])
    return rankings


def rank_by_series_count(rows: list[dict], *, field: str, top_n: int) -> list[dict]:
    result = []
    previous = None
    rank = 0
    for position, row in enumerate(sorted(rows, key=lambda row: -row[field]), start=1):
        if row[field] != previous:
            rank = position
        if rank > top_n:
            break
        result.append({"rank": rank, **row})
        previous = row[field]
    return result


def configured_pairs(drama: RankedDrama, override: object) -> set[tuple[str, str]] | None:
    if not isinstance(override, dict) or not isinstance(override.get("pairs"), list):
        return None
    cast_by_id = {cv.platform_id: cv.key for cv in drama.cast.values() if cv.platform_id}
    cast_by_name = defaultdict(list)
    for cv in drama.cast.values():
        cast_by_name[normalize_match(cv.name)].append(cv)
    pairs = set()
    for pair in override["pairs"]:
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        keys = []
        for cv in pair:
            if not isinstance(cv, dict) or not isinstance(cv.get("cvName"), str) or not cv["cvName"].strip():
                return None
            cv_id = normalize(cv.get("cvId"))
            if cv_id:
                key = cast_by_id.get(cv_id)
                if key is None:
                    return None
            else:
                candidates = cast_by_name.get(normalize_match(cv["cvName"]), [])
                if len(candidates) != 1 or candidates[0].platform_id:
                    return None
                key = candidates[0].key
            keys.append(key)
        first, second = sorted(keys)
        if first == second:
            return None
        pairs.add((first, second))
    return pairs


def build_combined_series_and_partner_ranks(
    dramas: list[RankedDrama], release_works: dict[tuple[str, str], dict], cvid_map: dict,
    partner_overrides: dict, work_index: dict[str, tuple[str, str]], *, top_n: int,
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    cv_profiles = {normalize(name): cv for name, cv in cvid_map.items() if isinstance(cv, dict)}
    cv_by_key = {}
    for drama in dramas:
        for cv in drama.cast.values():
            cv_by_key.setdefault(cv.key, cv)

    def cv_order(key: str) -> tuple[str, str]:
        return cv_by_key[key].name, key
    cv_works = defaultdict(dict)
    pair_works = defaultdict(dict)
    pending = []
    for drama in sorted(dramas, key=lambda drama: (drama.platform, id_sort(drama.drama_id))):
        series_key, title = work_index[drama.series_key]
        for key in drama.cast:
            work = cv_works[key].setdefault(series_key, {"seriesKey": series_key, "title": title, "dramaIds": {}})
            work["dramaIds"].setdefault(drama.platform, []).append(drama.drama_id)
        if drama.genre not in ("纯爱", "言情") or len(drama.cast) < 2:
            continue
        identity = ":".join(drama.identity)
        if len(drama.cast) == 2:
            pairs = {tuple(sorted(drama.cast))}
        else:
            pairs = configured_pairs(drama, partner_overrides.get(identity))
        if pairs is None:
            pending.append({
                "platform": drama.platform, "dramaId": drama.drama_id, "title": drama.title,
                "seriesKey": series_key, "genre": drama.genre,
                "mainCvs": [
                    {"cvName": cv.name, "cvId": cv.platform_id or None}
                    for cv in sorted(drama.cast.values(), key=lambda cv: (cv.name, cv.key))
                ],
                "reason": "invalidOverride" if identity in partner_overrides else "missingOverride",
            })
            continue
        for pair in pairs:
            work = pair_works[pair].setdefault(
                series_key, {"seriesKey": series_key, "title": title, "releases": []},
            )
            work["releases"].append({
                "platform": drama.platform, "dramaId": drama.drama_id, "title": drama.title,
                "viewCount": release_works.get(drama.identity, {}).get("viewCount"),
            })

    def sorted_works(works: dict) -> list[dict]:
        return sorted(works.values(), key=lambda work: (work["title"], work["seriesKey"]))

    count_rows = [
        {
            "cvKey": key,
            "cvName": cv_by_key[key].name,
            "avatar": normalize(cv_profiles.get(cv_by_key[key].name, {}).get("avatar"))
            if cv_by_key[key].canonical else "",
            "seriesCount": len(cv_works[key]),
            "works": sorted_works(cv_works[key]),
        }
        for key in sorted(cv_works, key=cv_order)
    ]
    count_rank_rows = []
    measured_rows = []
    ordered_pairs = [
        (tuple(sorted(pair, key=cv_order)), works) for pair, works in pair_works.items()
    ]
    for pair, works in sorted(ordered_pairs, key=lambda item: tuple(map(cv_order, item[0]))):
        cvs = []
        for key in pair:
            cv = cv_by_key[key]
            profile = cv_profiles.get(cv.name, {}) if cv.canonical else {}
            missevan_id = profile.get("missevanCvId", profile.get("cvId"))
            manbo_id = profile.get("manboCvId")
            if key.startswith("missevan:id:"):
                missevan_id = cv.platform_id
            elif key.startswith("manbo:id:"):
                manbo_id = cv.platform_id
            cvs.append({
                "cvKey": key, "cvName": cv.name, "avatar": normalize(profile.get("avatar")),
                "missevanCvId": normalize(missevan_id) or None,
                "manboCvId": normalize(manbo_id) or None,
            })
        samples = [
            entry["viewCount"] for work in works.values() for entry in work["releases"]
            if entry["viewCount"] is not None
        ]
        row = {"cvs": cvs, "workCount": len(works), "totalViewCount": sum(samples), "works": sorted_works(works)}
        if row["workCount"] >= MIN_PARTNER_WORK_COUNT:
            count_rank_rows.append(row)
        if samples:
            measured_rows.append(row)
    view_rows = sorted(measured_rows, key=lambda row: -row["totalViewCount"])[:top_n]
    return rank_by_series_count(count_rows, field="seriesCount", top_n=top_n), {
        "workCount": rank_by_series_count(count_rank_rows, field="workCount", top_n=top_n),
        "viewCount": [{"rank": rank, **row} for rank, row in enumerate(view_rows, start=1)],
    }, pending


def build_cv_rank_outputs(
    *,
    missevan_store: dict,
    manbo_store: dict,
    missevan_counts: dict,
    manbo_counts: dict,
    cvid_map: dict,
    series_info: dict,
    generated_at: str,
    top_n: int = 30,
    series_count_generated_at: str | None = None,
    partner_overrides: dict | None = None,
) -> tuple[dict, dict[str, list[dict]], dict[str, list[dict]]]:
    dramas = collect_ranked_dramas(missevan_store, manbo_store, series_info, cvid_map)
    edition_aliases, work_index = build_work_indexes(dramas)
    series_count_rankings = build_series_count_rankings(
        missevan_store=missevan_store, manbo_store=manbo_store,
        series_info=series_info, cvid_map=cvid_map, top_n=top_n, edition_aliases=edition_aliases,
    )
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
    release_works = works_by_release(full_rankings)
    combined_counts, partner_rankings, pending = build_combined_series_and_partner_ranks(
        dramas, release_works, cvid_map, partner_overrides or {}, work_index, top_n=top_n,
    )

    payload = {
        "version": 4,
        "date": generated_at[:10],
        "generated_at": generated_at,
        "source": {"scope": "all", "platforms": list(PLATFORMS)},
        "missevanDramaCount": count_missevan_dramas(missevan_store),
        "manboDramaCount": count_manbo_dramas(manbo_store),
        "rankings": {
            "missevan": full_rankings["missevan"][:top_n],
            "manbo": full_rankings["manbo"][:top_n],
            "combined": build_combined_view_rankings(dramas, release_works, avatar_index, work_index, top_n),
        },
        "paidRankings": {
            "missevan": full_paid_rankings["missevan"][:top_n],
            "manbo": full_paid_rankings["manbo"][:top_n],
            "combined": build_combined_view_rankings(
                dramas, works_by_release(full_paid_rankings), avatar_index, work_index, top_n,
            ),
        },
        "seriesCountRankings": {**series_count_rankings, "combined": combined_counts},
        "seriesCountMeta": build_series_count_meta(
            missevan_store=missevan_store, manbo_store=manbo_store,
            cvid_map=cvid_map, series_info=series_info,
            generated_at=series_count_generated_at or now_iso(),
        ),
        "partnerRankings": partner_rankings,
        "partnerReview": {"pending": pending},
    }
    return payload, full_rankings, full_paid_rankings


def build_cv_ranks_payload(
    *,
    missevan_store: dict,
    manbo_store: dict,
    missevan_counts: dict,
    manbo_counts: dict,
    cvid_map: dict,
    series_info: dict,
    generated_at: str,
    top_n: int = 30,
    series_count_generated_at: str | None = None,
    partner_overrides: dict | None = None,
) -> dict:
    payload, _full_rankings, _full_paid_rankings = build_cv_rank_outputs(
        missevan_store=missevan_store,
        manbo_store=manbo_store,
        missevan_counts=missevan_counts,
        manbo_counts=manbo_counts,
        cvid_map=cvid_map,
        series_info=series_info,
        generated_at=generated_at,
        top_n=top_n,
        series_count_generated_at=series_count_generated_at,
        partner_overrides=partner_overrides,
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
    if sample.get("publicationId") not in (None, ""):
        copied["publicationId"] = sample["publicationId"]
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
    publication_id: str | None = None,
    retention_dates: int = CV_TREND_RETENTION_DATES,
) -> dict:
    if platform not in PLATFORMS:
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
        if publication_id is not None:
            samples[history_date]["publicationId"] = publication_id
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
    key = "ranks:cv:latest"
    # Resource freshness covers all sub-rankings; the top-level date remains the watchcount sample date.
    timestamps = [payload.get("generated_at")]
    timestamps.extend(meta.get("generated_at") for meta in (payload.get("seriesCountMeta") or {}).values())
    candidates = [(parsed, value) for value in timestamps if (parsed := parse_iso_datetime(value)) is not None]
    updated_at = max(candidates, key=lambda item: item[0])[1] if candidates else None
    publish_rank_string(key, payload, scope="cv", upstash=upstash, updated_at=updated_at)
    print(f"[ok] uploaded {key} ({len(encoded)} bytes)")


def upload_cv_trends(
    *,
    history_date: str,
    generated_at: str,
    publication_id: str,
    full_rankings: dict[str, list[dict]],
    full_paid_rankings: dict[str, list[dict]],
    upstash=upstash_request,
) -> dict[str, dict]:
    v2_payloads = {
        platform: build_cv_trend_payload(
            None,
            platform,
            history_date,
            full_rankings.get(platform) or [],
            full_paid_rankings.get(platform) or [],
            generated_at=generated_at,
            publication_id=publication_id,
        )
        for platform in PLATFORMS
    }
    publish_trend_v2_best_effort(
        CV_TREND_V2_KEY,
        lambda: publish_cv_trend_v2(v2_payloads, publication_id=publication_id, upstash=upstash),
    )
    return v2_payloads


def remove_cv_trend_samples_by_generated_at(payload: dict, generated_at: str) -> tuple[dict, dict[str, int]]:
    cleaned = json.loads(json.dumps(payload, ensure_ascii=False))
    cvs = cleaned.get("cvs")
    if not isinstance(cvs, dict):
        cvs = {}
    removed_samples = 0
    removed_cvs = 0
    remaining_generated_at: list[str] = []
    remaining_dates: set[str] = set()
    kept_cvs: dict[str, object] = {}
    for cv_name, raw_entry in cvs.items():
        if not isinstance(raw_entry, dict):
            kept_cvs[str(cv_name)] = raw_entry
            continue
        entry = dict(raw_entry)
        samples = entry.get("samples")
        if not isinstance(samples, dict):
            kept_cvs[str(cv_name)] = entry
            continue
        kept_samples: dict[str, dict] = {}
        removed_from_entry = 0
        for date_key, sample in samples.items():
            if isinstance(sample, dict) and sample.get("generated_at") == generated_at:
                removed_samples += 1
                removed_from_entry += 1
                continue
            kept_samples[str(date_key)] = sample
            remaining_dates.add(str(date_key))
            if isinstance(sample, dict) and normalize(sample.get("generated_at")):
                remaining_generated_at.append(normalize(sample.get("generated_at")))
        if removed_from_entry and not kept_samples:
            removed_cvs += 1
            continue
        entry["samples"] = kept_samples
        kept_cvs[str(cv_name)] = entry
    cleaned["cvs"] = kept_cvs
    cleaned["dates"] = sorted(remaining_dates)
    cleaned["updated_at"] = max(remaining_generated_at) if remaining_generated_at else None
    return cleaned, {"removed_samples": removed_samples, "removed_cvs": removed_cvs}


def cleanup_remote_cv_trends_by_generated_at(
    generated_at: str,
    *,
    upstash=upstash_request,
    backup_dir: Path | None = None,
) -> dict[str, dict[str, object]]:
    target_dir = backup_dir or (ROOT / "recovery_backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_snapshot = upstash(["HGETALL", CV_TREND_V2_KEY])
    decoded = decode_hgetall(raw_snapshot)
    meta_raw = decoded.pop("__meta__", None)
    if not isinstance(meta_raw, str):
        raise RuntimeError(f"Refusing to clean {CV_TREND_V2_KEY}: __meta__ is missing")
    try:
        meta = json.loads(meta_raw)
        fields = {field: json.loads(value) for field, value in decoded.items()}
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Refusing to clean {CV_TREND_V2_KEY}: invalid JSON") from exc
    if not isinstance(meta, dict) or int(meta.get("version") or 0) != 2 or meta.get("kind") != "cv":
        raise RuntimeError(f"Refusing to clean {CV_TREND_V2_KEY}: invalid metadata")
    backup_path = target_dir / f"{stamp}_{CV_TREND_V2_KEY.replace(':', '-')}.json"
    backup_path.write_text(json.dumps(raw_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    results: dict[str, dict[str, object]] = {}
    for platform in PLATFORMS:
        prefix = f"{platform}:"
        payload = {
            "cvs": {
                field[len(prefix):]: record
                for field, record in fields.items()
                if field.startswith(prefix) and isinstance(record, dict)
            }
        }
        cleaned, stats = remove_cv_trend_samples_by_generated_at(payload, generated_at)
        for field in [field for field in fields if field.startswith(prefix)]:
            fields.pop(field, None)
        for cv_name, record in (cleaned.get("cvs") or {}).items():
            fields[f"{platform}:{cv_name}"] = record
        platform_meta = (meta.setdefault("platforms", {}).setdefault(platform, {}))
        platform_meta["dates"] = cleaned.get("dates") or []
        platform_meta["updated_at"] = cleaned.get("updated_at")
        platform_meta["entityCount"] = sum(
            1 for field in fields if field.startswith(prefix)
        )
        results[platform] = {
            **stats,
            "backup": str(backup_path),
            "updated_at": cleaned.get("updated_at"),
            "dates": cleaned.get("dates") or [],
        }
    remaining_platform_updates = [
        normalize((meta.get("platforms") or {}).get(platform, {}).get("updated_at"))
        for platform in PLATFORMS
    ]
    meta["updated_at"] = max(
        (value for value in remaining_platform_updates if value),
        default="",
    )
    publish_hash_snapshot_atomic(
        CV_TREND_V2_KEY,
        meta,
        fields,
        upstash=upstash,
        expected_meta_raw=meta_raw,
    )
    verified = decode_hgetall(upstash(["HGETALL", CV_TREND_V2_KEY]))
    verified_meta_raw = verified.get("__meta__")
    if not isinstance(verified_meta_raw, str):
        raise RuntimeError(f"Failed to verify {CV_TREND_V2_KEY}: __meta__ is missing")
    try:
        verified_meta = json.loads(verified_meta_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Failed to verify {CV_TREND_V2_KEY}: invalid __meta__") from exc
    if not isinstance(verified_meta, dict):
        raise RuntimeError(f"Failed to verify {CV_TREND_V2_KEY}: invalid __meta__ shape")
    residual = 0
    for field, raw in verified.items():
        if field == "__meta__":
            continue
        try:
            samples = (json.loads(raw).get("samples") or {})
        except (TypeError, ValueError, AttributeError):
            continue
        residual += sum(
            1 for sample in samples.values()
            if isinstance(sample, dict) and sample.get("generated_at") == generated_at
        )
    if residual:
        raise RuntimeError(f"Failed to clean {CV_TREND_V2_KEY}: {residual} matching samples remain")
    for platform in PLATFORMS:
        prefix = f"{platform}:"
        platform_meta = ((verified_meta.get("platforms") or {}).get(platform) or {})
        expected = results[platform]
        actual_count = sum(
            1 for field in verified if field != "__meta__" and field.startswith(prefix)
        )
        if (
            int(platform_meta.get("entityCount") or 0) != actual_count
            or platform_meta.get("dates") != expected["dates"]
            or platform_meta.get("updated_at") != expected["updated_at"]
        ):
            raise RuntimeError(
                f"Failed to verify {CV_TREND_V2_KEY}: inconsistent {platform} metadata"
            )
    expected_top_updated = max(
        (normalize(results[platform]["updated_at"]) for platform in PLATFORMS if normalize(results[platform]["updated_at"])),
        default="",
    )
    if expected_top_updated and verified_meta.get("updated_at") != expected_top_updated:
        raise RuntimeError(f"Failed to verify {CV_TREND_V2_KEY}: inconsistent updated_at")
    return results


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
    require_remote: bool = False,
) -> None:
    sync_remote_watchcount_if_newer(
        "missevan", missevan_counts_path, upstash=upstash, force=force, require_remote=require_remote
    )
    sync_remote_watchcount_if_newer(
        "manbo", manbo_counts_path, upstash=upstash, force=force, require_remote=require_remote
    )


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
        force=force or upload,
        require_remote=upload,
    )
    missevan_cache = load_cache(missevan_counts_path)
    manbo_cache = load_cache(manbo_counts_path)
    generated = generated_at or latest_watch_count_updated_at(missevan_cache, manbo_cache) or now_iso()
    print(
        "[inputs] watchcount updated_at:",
        f"missevan={((missevan_cache.get('_meta') or {}).get('updated_at'))}",
        f"manbo={((manbo_cache.get('_meta') or {}).get('updated_at'))}",
        f"rank_date={generated[:10]}",
    )
    cvid_map = sync_remote_rank_inputs(
        missevan_info_path=missevan_info_path,
        manbo_info_path=manbo_info_path,
        cvid_map_path=cvid_map_path,
        upstash=upstash,
    )
    series_info = fetch_remote_series_info(upstash=upstash)
    partner_overrides = fetch_partner_overrides(upstash=upstash)
    payload, full_rankings, full_paid_rankings = build_cv_rank_outputs(
        missevan_store=load_json(missevan_info_path, {}),
        manbo_store=load_json(manbo_info_path, {"records": []}),
        missevan_counts=missevan_cache.get("counts", {}),
        manbo_counts=manbo_cache.get("counts", {}),
        cvid_map=cvid_map,
        series_info=series_info,
        partner_overrides=partner_overrides,
        generated_at=generated,
    )
    payload["publicationId"] = str(uuid.uuid4())
    save_json(output_path, payload)
    if upload:
        upload_cv_ranks(payload, upstash=upstash)
        upload_cv_trends(
            history_date=payload["date"],
            generated_at=payload["generated_at"],
            publication_id=payload["publicationId"],
            full_rankings=full_rankings,
            full_paid_rankings=full_paid_rankings,
            upstash=upstash,
        )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CV view-count and series-count rankings from platform libraries")
    parser.add_argument("--no-upload", action="store_true", help="Only write ranks-cv.json locally")
    parser.add_argument("--date", help="Override the output and aggregate sample date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="构建前无条件拉取远端 watchcount latest")
    parser.add_argument(
        "--cleanup-trend-generated-at",
        help="Only remove CV trend samples whose generated_at exactly matches this value",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    if args.cleanup_trend_generated_at:
        results = cleanup_remote_cv_trends_by_generated_at(args.cleanup_trend_generated_at)
        print("[ok] cleaned CV trends:", json.dumps(results, ensure_ascii=False))
        return 0
    generated_at = None
    if args.date:
        generated_at = f"{args.date}T00:00:00+00:00"
    payload = build_and_publish_cv_ranks(generated_at=generated_at, upload=not args.no_upload, force=args.force)
    rankings = payload.get("rankings") or {}
    missevan_count = len(rankings.get("missevan") or [])
    manbo_count = len(rankings.get("manbo") or [])
    print(
        f"[ok] wrote {CV_RANKS_PATH.name}: "
        f"missevan_rankings={missevan_count}, manbo_rankings={manbo_count}, "
        f"combined_rankings={len(rankings.get('combined') or [])}, "
        f"pending_partner_works={len(payload['partnerReview']['pending'])}, date={payload.get('date')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
