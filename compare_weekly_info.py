"""Compare old info backups with current Upstash using read-only MGET.

Run with Python 3.10+ and the project's requests dependency:
  python -B compare_weekly_info.py --missevan-before <backup.json> \
      --manbo-before <backup.json> --log <weekly.log>

Print Markdown to stdout; never publish data or write reports/snapshots.
--snapshot-dir reads a previously captured manifest and JSON files offline.
Date labels come from backup filenames and current info meta; they do not
filter changes. Archives are confirmed by removed IDs intersecting the archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from platform_sync import is_excluded_drama, iter_missevan_nodes


ROOT = Path(__file__).resolve().parent
PLATFORMS = {"missevan": "猫耳", "manbo": "漫播"}
IGNORED_FIELDS = frozenset({"cover", "coverPic", "largePic", "sharePicUrl", "soundIds"})
READ_KEYS = (
    "missevan:info:v2", "manbo:info:v2",
    "missevan:info:meta:v2", "manbo:info:meta:v2",
    "missevan:info:archive:v1", "manbo:info:archive:v1",
    "cvid-map:v1", "cvid-map:meta:v1",
)


def read_sources(snapshot_dir: Path | None) -> tuple[dict, dict]:
    if snapshot_dir is not None:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        raw = {key: (snapshot_dir / manifest["keys"][key]).read_text(encoding="utf-8")
               for key in READ_KEYS}
    else:
        from sync_new_drama_ids import load_env_file, upstash_request

        load_env_file(ROOT / ".env")
        # One Redis command reads all relevant sources at a consistent instant.
        values = upstash_request(["MGET", *READ_KEYS])
        if not isinstance(values, list) or len(values) != len(READ_KEYS):
            raise ValueError("Invalid MGET response")
        raw = dict(zip(READ_KEYS, values))
        manifest = {"captured_at": datetime.now(timezone.utc).isoformat()}
    if any(not isinstance(value, str) for value in raw.values()):
        raise ValueError("Required Upstash source missing or not a JSON string")
    sources = {key: json.loads(value) for key, value in raw.items()}
    if any(not isinstance(value, dict) for value in sources.values()):
        raise ValueError("Required Upstash source is not an object")
    for data_key, meta_key in (
        ("missevan:info:v2", "missevan:info:meta:v2"),
        ("manbo:info:v2", "manbo:info:meta:v2"),
        ("cvid-map:v1", "cvid-map:meta:v1"),
    ):
        encoded = raw[data_key].encode("utf-8")
        meta = sources[meta_key]
        if meta.get("dataKey") != data_key or meta.get("contentSha1") != hashlib.sha1(encoded).hexdigest():
            raise ValueError(f"Source/meta mismatch: {data_key}")
        if meta.get("bytes") != len(encoded):
            raise ValueError(f"Source byte count mismatch: {data_key}")
    if sources["cvid-map:meta:v1"].get("recordCount") != len(sources["cvid-map:v1"]):
        raise ValueError("CV mapping record count mismatch")
    return sources, manifest


def flatten(platform: str, data: dict) -> dict[str, dict]:
    if not isinstance(data, dict):
        raise ValueError(f"Invalid info object: {platform}")
    if platform == "missevan":
        rows = [row for _, _, row in iter_missevan_nodes(data)]
    else:
        rows = data.get("records")
        if not isinstance(rows, list):
            raise ValueError("Manbo info records must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("dramaId"):
            raise ValueError(f"Missing dramaId: {platform}")
        drama_id = str(row["dramaId"])
        if drama_id in result:
            raise ValueError(f"Duplicate dramaId: {platform}/{drama_id}")
        result[drama_id] = row
    return result


def changed_fields(before: dict, after: dict) -> dict[str, list]:
    # A missing field is different from an explicit null.
    return {key: [before.get(key), after.get(key)]
            for key in sorted(before.keys() | after.keys())
            if (key in before) != (key in after) or before.get(key) != after.get(key)}


def compare(before: dict[str, dict], after: dict[str, dict]) -> dict:
    common = before.keys() & after.keys()
    changes, cosmetic = {}, []
    for drama_id in sorted(common, key=int):
        fields = changed_fields(before[drama_id], after[drama_id])
        meaningful = {key: value for key, value in fields.items() if key not in IGNORED_FIELDS}
        if meaningful:
            changes[drama_id] = meaningful
        elif fields:
            cosmetic.append(drama_id)
    return {
        "added": sorted(after.keys() - before.keys(), key=int),
        "removed": sorted(before.keys() - after.keys(), key=int),
        "changed": changes, "cosmetic": cosmetic,
    }


def backup_time(path: Path) -> datetime:
    match = re.match(r"(\d{8}T\d{12}Z)_", path.name)
    if not match:
        raise ValueError(f"Backup filename must start with YYYYMMDDTHHMMSSffffffZ_: {path.name}")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)


def confirm_archived(removed: list[str], archive: dict) -> dict[str, dict]:
    """Confirm removed records by archive membership, regardless of timestamps."""
    confirmed = {}
    for drama_id in removed:
        if drama_id not in archive:
            continue
        entry = archive[drama_id]
        record = entry.get("record") if isinstance(entry, dict) else None
        if not isinstance(record, dict) or str(record.get("dramaId")) != drama_id:
            raise ValueError(f"Invalid archived record: {drama_id}")
        confirmed[drama_id] = record
    return confirmed


def cv_indexes(mapping: dict) -> dict[str, dict[str, str]]:
    indexes = {platform: {} for platform in PLATFORMS}
    for key, row in mapping.items():
        if not isinstance(row, dict):
            raise ValueError(f"Invalid CV mapping entry: {key}")
        name = str(row.get("displayName") or key)
        for platform, field in (("missevan", "missevanCvId"), ("manbo", "manboCvId")):
            cv_id = row.get(field)
            if cv_id is None and platform == "missevan":
                cv_id = row.get("cvId")
            if cv_id is None:
                continue
            existing = indexes[platform].get(str(cv_id))
            if existing and existing != name:
                raise ValueError(f"Conflicting CV identity: {platform}/{cv_id}")
            indexes[platform][str(cv_id)] = name
    return indexes


def format_drama(platform: str, row: dict, indexes: dict) -> str:
    names = []
    if platform == "missevan":
        for cv_id in row.get("maincvs") or []:
            names.append(indexes[platform].get(str(cv_id))
                         or (row.get("cvnames") or {}).get(str(cv_id)) or f"CV {cv_id}")
    else:
        ids = row.get("mainCvIds") or []
        stored = row.get("mainCvNames") or []
        nicknames = row.get("mainCvNicknames") or []
        for i in range(max(len(ids), len(stored), len(nicknames))):
            mapped = indexes[platform].get(str(ids[i])) if i < len(ids) else None
            names.append(mapped or (stored[i] if i < len(stored) else "")
                         or (nicknames[i] if i < len(nicknames) else "")
                         or (f"CV {ids[i]}" if i < len(ids) else "主役暂缺"))
    names = list(dict.fromkeys(names))
    title = str(row.get("title") or row.get("name") or row["dramaId"])
    return f"- {title}（{'、'.join(names) if names else '主役暂缺'}）"


def log_summary(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    summary = {}
    for platform, label in PLATFORMS.items():
        counts = re.findall(rf"^{platform} archive published: (\d+)\s*$", text, re.M)
        detected = re.findall(rf"^{label} watch counts archived: (\d+)\s*$", text, re.M)
        if not counts or not detected:
            raise ValueError(f"Weekly log has no archive summary: {platform}")
        summary[platform] = {"published": int(counts[-1]), "detected": int(detected[-1])}
    exits = re.findall(r"Done final_exit=(\d+)", text)
    summary["success"] = bool(exits and exits[-1] == "0")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missevan-before", type=Path, required=True)
    parser.add_argument("--manbo-before", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, help="Read a captured manifest offline instead of Upstash")
    parser.add_argument("--details", action="store_true", help="Print changed fields and source checks to stderr")
    args = parser.parse_args()
    sources, manifest = read_sources(args.snapshot_dir)
    indexes = cv_indexes(sources["cvid-map:v1"])
    log = log_summary(args.log)
    results, before, after, archived, starts, ends = {}, {}, {}, {}, {}, {}
    for platform in PLATFORMS:
        path = getattr(args, platform + "_before")
        starts[platform] = backup_time(path)
        meta = sources[f"{platform}:info:meta:v2"]
        ends[platform] = datetime.fromisoformat(meta["updatedAt"])
        before[platform] = flatten(platform, json.loads(path.read_text(encoding="utf-8-sig")))
        after[platform] = flatten(platform, sources[f"{platform}:info:v2"])
        if meta.get("recordCount") != len(after[platform]):
            raise ValueError(f"Info record count mismatch: {platform}")
        results[platform] = compare(before[platform], after[platform])
        archive = sources[f"{platform}:info:archive:v1"].get("records")
        if not isinstance(archive, dict):
            raise ValueError(f"Invalid archive records: {platform}")
        archived[platform] = confirm_archived(results[platform]["removed"], archive)
        if args.details:
            print(json.dumps({"platform": platform, "before": len(before[platform]),
                              "after": len(after[platform]), **results[platform],
                              "archived_removed": list(archived[platform]),
                              "log_archive_summary": log[platform]}, ensure_ascii=False), file=sys.stderr)
    start_dates = {value.strftime('%m/%d') for value in starts.values()}
    end_dates = {value.strftime('%m/%d') for value in ends.values()}
    if len(start_dates) != 1 or len(end_dates) != 1:
        raise ValueError("Platform snapshot dates differ; compare separately")
    print(f"### {next(iter(start_dates))} - {next(iter(end_dates))} 数据库更新\n")
    for category, label in (("added", "新入库"), ("changed", "旧信息修正补全"), ("archived", "归档下架")):
        for platform, platform_label in PLATFORMS.items():
            ids = sorted(archived[platform], key=int) if category == "archived" else results[platform][category]
            print(f"# {platform_label}{label}（{len(ids)}部）\n")
            for drama_id in ids:
                row = archived[platform][drama_id] if category == "archived" else after[platform][drama_id]
                print(format_drama(platform, row, indexes))
            if not ids:
                print("- 无")
            print()
    excluded = sum(len(result["cosmetic"]) for result in results.values())
    print(f"仅封面/soundIds 变化已排除：{excluded}部。")
    print("归档按旧库移出 ID 与当前归档库交集确认；标题日期仅作标签，不用于筛选。")
    for platform, label in PLATFORMS.items():
        for drama_id in results[platform]["removed"]:
            archive_records = sources[f"{platform}:info:archive:v1"]["records"]
            if drama_id in archived[platform]:
                continue
            reason = "禁止入库清理，不计下架归档" if is_excluded_drama(platform, drama_id) and drama_id not in archive_records else "移出原因待核对，不自动认定为本周下架"
            print(f"{label}移出：{format_drama(platform, before[platform][drama_id], indexes)[2:]}：{reason}。")
    print(f"周更日志完成状态：{'成功' if log['success'] else '未确认成功'}；本次发布归档："
          + "、".join(f"{label}{log[platform]['published']}部" for platform, label in PLATFORMS.items()) + "。")
    if args.details:
        print(json.dumps({"captured_at": manifest.get("captured_at"),
                          "meta_updated_at": {p: sources[f"{p}:info:meta:v2"]["updatedAt"] for p in PLATFORMS}},
                         ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
