from __future__ import annotations

import argparse
import re
import sqlite3

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    GENRE_BY_TYPE,
    MANBO_CATALOG_NAME_BY_ID,
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_CATALOG_NAME_BY_ID,
    MISSEVAN_COUNTS_PATH,
    MISSEVAN_INFO_PATH,
    SQLITE_PATH,
    iter_missevan_nodes,
    load_cache,
    load_json,
    normalize,
    normalize_match,
)


HAN_RE = r"\u4e00-\u9fff"
SEASON_PATTERNS = [
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(][^()（）]*全新版[^()（）]*[)）]$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十0-9]+季\s*[（(]\s*[上下]\s*[)）]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(]\s*[上下]\s*[)）]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[上下中]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(]\s*[上下中]\s*[)）]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(][^()（）]*完结季[^()（）]*[)）]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(][^()（）]*终章[^()（）]*[)）]$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季\s*[（(][^()（）]*完[^()（）]*[)）]$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十0-9]+季\s*[·・]\s*[「『《【][^「『《【」』》】]+[」』》】]$"),
    re.compile(r"\s*[（(]\s*[上下]\s*[)）]$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十0-9]+季$"),
    re.compile(r"\s*第[一二三四五六七八九十0-9]+季$"),
    re.compile(r"\s*season\s*[0-9]+$", re.I),
    re.compile(r"\s+s[0-9]+$", re.I),
    re.compile(r"\s*全一季$"),
    re.compile(r"\s*全一期$"),
    re.compile(r"\s*全\d+季$"),
    re.compile(r"\s*(?:最终季|终季|完结季)$"),
    re.compile(r"\s*[（(]\s*[上下中]\s*[)）]$"),
    re.compile(r"\s+[上下中]$"),
    re.compile(r"\s*上季$"),
    re.compile(r"\s*下季$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+册$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+卷$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+部$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+篇$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+章$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+话$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+回$"),
    re.compile(r"\s*[·・]?\s*第[一二三四五六七八九十百千万0-9]+弹$"),
]
PROMO_TRAILER_PATTERNS = [
    re.compile(r"\s*[（(]\s*CV\s*[:：][^()（）]*[)）]$", re.I),
]


def normalize_role_token(value: object) -> str:
    text = normalize(value)
    if not text:
        return ""
    text = re.sub(rf"(?<=[{HAN_RE}])\s+(?=[{HAN_RE}])", "", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def merge_role_names(values: list[str]) -> str:
    out: list[str] = []
    for value in values:
        for part in str(value or "").split("/"):
            token = normalize_role_token(part)
            if token and token not in out:
                out.append(token)
    return "/".join(out)


def base_series_title(value: object) -> str:
    text = normalize(value)
    if not text:
        return ""
    changed = True
    while changed:
        changed = False
        for pattern in PROMO_TRAILER_PATTERNS:
            updated = pattern.sub("", text).strip()
            if updated != text:
                text = updated
                changed = True
    for pattern in SEASON_PATTERNS:
        updated = pattern.sub("", text).strip()
        if updated != text:
            text = updated
    return normalize(text)


def earliest_month(values: list[str]) -> str:
    filtered = [normalize(item) for item in values if normalize(item)]
    if not filtered:
        return ""
    return min(filtered)


def normalize_manbo_catalog_name(value: object, catalog: int | None) -> str:
    name = normalize(value or MANBO_CATALOG_NAME_BY_ID.get(catalog, ""))
    if name == "有声书":
        return "有声剧"
    return name


def total_for_ids(count_map: dict, drama_ids: list[str]) -> int | None:
    if not drama_ids:
        return None
    total = 0
    for drama_id in drama_ids:
        item = count_map.get(drama_id)
        if not item or item.get("view_count") is None:
            return None
        total += int(item["view_count"])
    return total


def build_map_indexes(cvid_map: dict) -> tuple[dict[int, set[str]], dict[int, set[str]], dict[str, set[str]]]:
    missevan_ids: dict[int, set[str]] = {}
    manbo_ids: dict[int, set[str]] = {}
    names: dict[str, set[str]] = {}
    for key, payload in cvid_map.items():
        canonical = normalize(key)
        if not canonical:
            continue
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
    return missevan_ids, manbo_ids, names


def resolve_cv_name(raw_name: str, cv_id: int | None, *, platform: str, missevan_ids: dict[int, set[str]], manbo_ids: dict[int, set[str]], name_index: dict[str, set[str]]) -> str:
    candidates: set[str] = set()
    if cv_id is not None:
        by_id = missevan_ids if platform == "猫耳" else manbo_ids
        candidates.update(by_id.get(int(cv_id), set()))
    if len(candidates) == 1:
        return next(iter(candidates))
    for candidate in [raw_name]:
        norm = normalize_match(candidate)
        if norm:
            candidates.update(name_index.get(norm, set()))
    if len(candidates) == 1:
        return next(iter(candidates))
    fallback = normalize(raw_name)
    if fallback:
        return fallback
    prefix = "猫耳CV" if platform == "猫耳" else "漫播CV"
    return f"{prefix}_{cv_id}" if cv_id is not None else "未命名CV"


def build_rows() -> list[dict]:
    missevan_store = load_json(MISSEVAN_INFO_PATH, {})
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    missevan_counts = load_cache(MISSEVAN_COUNTS_PATH).get("counts", {})
    manbo_counts = load_cache(MANBO_COUNTS_PATH).get("counts", {})
    cvid_map = load_json(COMBINED_CVID_MAP_PATH, {})
    missevan_ids, manbo_ids, name_index = build_map_indexes(cvid_map)

    buckets: dict[tuple[str, str, int | None, str], dict] = {}

    for title, _season_key, node in iter_missevan_nodes(missevan_store):
        if node.get("needpay") is not True:
            continue
        catalog = None if node.get("catalog") in (None, "") else int(node["catalog"])
        title_value = normalize(node.get("seriesTitle") or node.get("title") or title)
        base_title = base_series_title(title_value)
        genre = GENRE_BY_TYPE.get(int(node.get("type") or 0), "")
        catalog_name = MISSEVAN_CATALOG_NAME_BY_ID.get(catalog, "") if catalog is not None else ""
        for cv_id in [int(item) for item in (node.get("maincvs") or [])]:
            raw_name = normalize((node.get("cvnames") or {}).get(str(cv_id)))
            cv_name = resolve_cv_name(raw_name, cv_id, platform="猫耳", missevan_ids=missevan_ids, manbo_ids=manbo_ids, name_index=name_index)
            key = (cv_name, "猫耳", catalog, base_title)
            bucket = buckets.setdefault(
                key,
                {
                    "cv_name": cv_name,
                    "title": base_title,
                    "genre": genre,
                    "platform": "猫耳",
                    "catalog": catalog,
                    "catalog_name": catalog_name,
                    "drama_ids": [],
                    "role_names": [],
                    "create_months": [],
                },
            )
            drama_id = str(node.get("dramaId") or "").strip()
            if drama_id and drama_id not in bucket["drama_ids"]:
                bucket["drama_ids"].append(drama_id)
            role_name = normalize((node.get("cvroles") or {}).get(str(cv_id), ""))
            if role_name:
                bucket["role_names"].append(role_name)
            create_month = normalize(node.get("createTime"))
            if create_month:
                bucket["create_months"].append(create_month)

    for record in (manbo_store.get("records") or []):
        if record.get("needpay") is not True:
            continue
        catalog = None if record.get("catalog") in (None, "") else int(record["catalog"])
        title_value = normalize(record.get("seriesTitle") or record.get("name"))
        base_title = base_series_title(title_value)
        genre = normalize(record.get("genre") or GENRE_BY_TYPE.get(int(record.get("type") or 0), ""))
        catalog_name = normalize_manbo_catalog_name(record.get("catalogName"), catalog)
        ids = [int(item) for item in (record.get("mainCvIds") or [])]
        names = record.get("mainCvNicknames") or []
        roles = record.get("mainCvRoleNames") or []
        for idx, cv_id in enumerate(ids):
            raw_name = normalize(names[idx] if idx < len(names) else "")
            cv_name = resolve_cv_name(raw_name, cv_id, platform="漫播", missevan_ids=missevan_ids, manbo_ids=manbo_ids, name_index=name_index)
            key = (cv_name, "漫播", catalog, base_title)
            bucket = buckets.setdefault(
                key,
                {
                    "cv_name": cv_name,
                    "title": base_title,
                    "genre": genre,
                    "platform": "漫播",
                    "catalog": catalog,
                    "catalog_name": catalog_name,
                    "drama_ids": [],
                    "role_names": [],
                    "create_months": [],
                },
            )
            drama_id = str(record.get("dramaId") or "").strip()
            if drama_id and drama_id not in bucket["drama_ids"]:
                bucket["drama_ids"].append(drama_id)
            role_name = normalize(roles[idx] if idx < len(roles) else "")
            if role_name:
                bucket["role_names"].append(role_name)
            create_month = normalize(record.get("createTime"))
            if create_month:
                bucket["create_months"].append(create_month)

    rows: list[dict] = []
    for item in buckets.values():
        count_map = missevan_counts if item["platform"] == "猫耳" else manbo_counts
        drama_ids = sorted(item["drama_ids"], key=lambda value: (len(value), value))
        rows.append(
            {
                "cv_name": item["cv_name"],
                "title": item["title"],
                "genre": item["genre"],
                "dramaids_text": ",".join(drama_ids),
                "role_names": merge_role_names(item["role_names"]),
                "total_play_count": total_for_ids(count_map, drama_ids),
                "platform": item["platform"],
                "catalog": item["catalog"],
                "catalog_name": item["catalog_name"],
                "create_month": earliest_month(item["create_months"]),
            }
        )
    rows.sort(key=lambda row: (normalize_match(row["cv_name"]), row["platform"], normalize_match(row["title"])))
    return rows


def rebuild_sqlite(*, export_workbook: bool = False) -> int:
    rows = build_rows()
    conn = sqlite3.connect(SQLITE_PATH)
    conn.execute("DROP TABLE IF EXISTS work_drama_ids")
    conn.execute("DROP TABLE IF EXISTS cv_works")
    conn.execute(
        """
        CREATE TABLE cv_works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cv_name TEXT NOT NULL,
            title TEXT NOT NULL,
            genre TEXT,
            dramaids_text TEXT,
            role_names TEXT,
            total_play_count INTEGER,
            platform TEXT,
            catalog INTEGER,
            catalog_name TEXT,
            create_month TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE work_drama_ids (
            work_id INTEGER NOT NULL,
            drama_id TEXT NOT NULL,
            PRIMARY KEY (work_id, drama_id),
            FOREIGN KEY (work_id) REFERENCES cv_works(id) ON DELETE CASCADE
        )
        """
    )
    for row in rows:
        cur = conn.execute(
            """
            INSERT INTO cv_works(
                cv_name, title, genre, dramaids_text, role_names, total_play_count,
                platform, catalog, catalog_name, create_month
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["cv_name"],
                row["title"],
                row["genre"],
                row["dramaids_text"],
                row["role_names"],
                row["total_play_count"],
                row["platform"],
                row["catalog"],
                row["catalog_name"],
                row["create_month"],
            ),
        )
        work_id = cur.lastrowid
        for drama_id in row["dramaids_text"].split(",") if row["dramaids_text"] else []:
            conn.execute("INSERT INTO work_drama_ids(work_id, drama_id) VALUES (?, ?)", (work_id, drama_id))
    conn.commit()
    conn.close()

    if export_workbook:
        from export_sqlite_to_workbook import build_workbook

        build_workbook()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-workbook", action="store_true")
    args = parser.parse_args()
    row_count = rebuild_sqlite(export_workbook=args.export_workbook)
    print("SQLite rows rebuilt:", row_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
