from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platform_sync import (
    MANBO_CATALOG_NAME_BY_ID, MISSEVAN_CATALOG_NAME_BY_ID, is_unknown_main_cv,
    manbo_has_unknown_main_cv, missevan_has_unknown_main_cv,
)
from sync_new_drama_ids import (
    MANBO_INFO_KEY, MISSEVAN_INFO_KEY, ROOT, SERIES_INFO_KEY, SERIES_INFO_PATH,
    assert_info_download_is_safe, backup_local_json_file, configure_stdio, load_env_file,
    upstash_request,
)
from upstash_v2 import string_cas_token

KEYS = (SERIES_INFO_KEY, MANBO_INFO_KEY, MISSEVAN_INFO_KEY)
MIGRATIONS = {
    "猫耳:全职高手": "猫耳:全职高手广播剧",
    "猫耳:君有疾否": "猫耳:君有疾否广播剧",
}
CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""
NUMERAL = r"[零〇一二三四五六七八九十百千两兩壹贰叁肆伍陆柒捌玖拾0-9]+"
MARKER = re.compile(
    rf"第\s*{NUMERAL}\s*[季册冊卷]|[上中下]\s*季|完结季|完結季|最终季|最終季|"
    r"全一季|独家番外(?:篇)?|獨家番外(?:篇)?|番外(?:篇)?|"
    r"(?<=[\s·•.\-—（(【\[])\s*[上中下](?:[篇部册冊])?\s*[）)】\]]?\s*$"
)
TRAILING_SEPARATOR = re.compile(r"[\s·•.。\-—_:：|丨（(\[【「]+$")
VERSION_MARKER = re.compile(
    r"(?:日语|日語|日文|英语|英語|英文|韩语|韓語|韩文|韓文|泰语|泰語|"
    r"粤语|粵語|国语|國語|普通话|普通話|中文|台语|臺語)版|"
    r"全新版|重制版|重製版|修订版|修訂版|新版|旧版|舊版"
)
CV_LABEL = re.compile(r"(?<![A-Za-z])CV(?=\s|:|$)\s*:?[\s]*", re.IGNORECASE)
BRACKET_PAIRS = {'(': ')', '[': ']', '【': '】', '「': '」', '《': '》'}


def without_cv_metadata(text: str) -> str:
    """Exclude CV descriptions, including nested actor annotations, from version matching."""
    result, brackets = [], []
    index = 0
    while index < len(text):
        label = CV_LABEL.match(text, index)
        if label:
            depth = len(brackets)
            nested = list(brackets)
            index = label.end()
            while index < len(text):
                char = text[index]
                if nested and char == nested[-1]:
                    nested.pop()
                    if depth and len(nested) < depth:
                        break
                elif char in BRACKET_PAIRS:
                    nested.append(BRACKET_PAIRS[char])
                elif not depth and not nested and char in ';；\n|丨':
                    break
                index += 1
            continue
        char = text[index]
        if char in BRACKET_PAIRS:
            brackets.append(BRACKET_PAIRS[char])
        elif brackets and char == brackets[-1]:
            brackets.pop()
        result.append(char)
        index += 1
    return ''.join(result)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", title)).rstrip("·•.。-—_:：|丨")


def parse_title(title: str) -> tuple[str, str, bool]:
    """Return display base, matching base and whether a series marker is present."""
    text = unicodedata.normalize("NFKC", title)
    match = MARKER.search(text)
    if match is None:
        return title.strip(), normalize_title(title), False
    # NFKC can change character counts: locate the corresponding boundary in the original.
    boundary = 0
    while boundary < len(title) and len(unicodedata.normalize("NFKC", title[:boundary])) < match.start():
        boundary += 1
    base = TRAILING_SEPARATOR.sub("", title[:boundary]).strip()
    if not base:
        return title.strip(), normalize_title(title), False
    suffix = without_cv_metadata(text[match.end():])
    for version in VERSION_MARKER.finditer(suffix):
        qualifier = version.group()
        if qualifier not in normalize_title(base):
            base += qualifier
    marker = re.sub(r"\s+", "", match.group())
    return base, normalize_title(base), marker != "全一季"


def id_sort(value: str) -> tuple[int, object]:
    return (0, int(value)) if value.isdigit() else (1, value)


@dataclass
class Drama:
    platform: str
    id: str
    title: str
    base: str
    match_base: str
    category: str
    cvs: frozenset[str]
    cv_names: dict[str, str]
    marked: bool


@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)
    appended: dict[str, list[str]] = field(default_factory=dict)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_series(payload: object) -> None:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("series-info 必须是非空 JSON 对象")
    for key, entry in payload.items():
        if not isinstance(entry, dict) or not isinstance(key, str):
            raise ValueError("series-info 条目结构错误")
        if not all(isinstance(entry.get(k), str) and entry[k] for k in ("platform", "category", "series title")):
            raise ValueError(f"{key}: 缺少平台、类型或系列标题")
        ids = entry.get("dramaIds")
        if not isinstance(ids, list) or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in ids):
            raise ValueError(f"{key}: dramaIds 必须为 ID 数组")


def read_drama_records(manbo: dict, missevan: dict) -> list[Drama]:
    result = []
    seen = set()
    for platform, rows, catalogs in (
        ("漫播", manbo["records"], MANBO_CATALOG_NAME_BY_ID),
        ("猫耳", list(missevan.values()), MISSEVAN_CATALOG_NAME_BY_ID),
    ):
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{platform}: 非对象剧集条目")
            title = row.get("name" if platform == "漫播" else "title")
            drama_id = row.get("dramaId")
            if not isinstance(title, str) or not title.strip() or drama_id in (None, ""):
                raise ValueError(f"{platform}: 缺少剧集标题或 ID")
            identity = (platform, str(drama_id))
            if identity in seen:
                raise ValueError(f"{platform}: 重复 dramaId={drama_id}")
            seen.add(identity)
            try:
                category = catalogs.get(int(row.get("catalog")), "")
            except (TypeError, ValueError):
                category = ""
            raw_ids = row.get("mainCvIds" if platform == "漫播" else "maincvs") or []
            if not isinstance(raw_ids, list):
                raise ValueError(f"{platform}:{drama_id}: 主役 CV 字段必须为数组")
            if (manbo_has_unknown_main_cv(row) if platform == '漫播' else missevan_has_unknown_main_cv(row)):
                raw_ids = []
            names = {}
            cv_ids = set()
            for index, cv_id in enumerate(raw_ids):
                cv_id = str(cv_id)
                if not cv_id.isdigit() or int(cv_id) <= 0:
                    continue
                if platform == "漫播":
                    raw_names = row.get("mainCvNames") or []
                    nicknames = row.get("mainCvNicknames") or []
                    name = raw_names[index] if index < len(raw_names) else ""
                    if not isinstance(name, str) or not name.strip():
                        name = nicknames[index] if index < len(nicknames) else ""
                else:
                    name = (row.get("cvnames") or {}).get(cv_id, "")
                if is_unknown_main_cv(name) or name == "暂无":
                    continue
                cv_id = str(int(cv_id))
                cv_ids.add(cv_id)
                if isinstance(name, str) and name.strip():
                    names[cv_id] = name.strip()
            base, match_base, marked = parse_title(title)
            result.append(Drama(platform, str(drama_id), title, base, match_base, category,
                                frozenset(cv_ids), names, marked))
    return result


def components(records: list[Drama]) -> list[list[Drama]]:
    buckets = defaultdict(list)
    for row in records:
        if row.category and row.cvs:
            buckets[(row.platform, row.match_base, row.category)].append(row)
    result = []
    for bucket in buckets.values():
        parent = list(range(len(bucket)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        cv_owner = {}
        for index, row in enumerate(bucket):
            for cv in row.cvs:
                if cv in cv_owner:
                    parent[find(index)] = find(cv_owner[cv])
                else:
                    cv_owner[cv] = index
        parts = defaultdict(list)
        for index, row in enumerate(bucket):
            parts[find(index)].append(row)
        result.extend(sorted(part, key=lambda r: id_sort(r.id)) for part in parts.values())
    return sorted(result, key=lambda rs: (rs[0].platform, rs[0].match_base, rs[0].category, id_sort(rs[0].id)))


def build_update(existing: dict, records: list[Drama]) -> tuple[dict, ChangeSet]:
    validate_series(existing)
    updated = copy.deepcopy(existing)
    changes = ChangeSet()
    for source, target in MIGRATIONS.items():
        if source not in updated:
            continue
        old = updated[source]
        if target not in updated:
            updated[target] = copy.deepcopy(old)
            updated[target]["series title"] = target.split(":", 1)[1]
        else:
            entry = updated[target]
            ids = {str(v) for v in entry["dramaIds"]}
            for value in old["dramaIds"]:
                if str(value) not in ids:
                    entry["dramaIds"].append(value)
                    ids.add(str(value))
            for key, value in old.items():
                if key not in entry:
                    entry[key] = copy.deepcopy(value)
        del updated[source]
        changes.deleted.append(source)

    by_id = {(r.platform, r.id): r for r in records}
    owners = defaultdict(set)
    for key, entry in updated.items():
        for value in entry["dramaIds"]:
            owners[(entry["platform"], str(value))].add(key)
            row = by_id.get((entry["platform"], str(value)))
            if row is None:
                changes.missing.append((key, str(value)))
            elif row.category != entry["category"]:
                changes.warnings.append(f"{key}: 成员 {value} 的 info 类型为 {row.category or '未知'}，保留旧 category={entry['category']}")

    title_buckets = defaultdict(list)
    for row in records:
        title_buckets[(row.platform, row.match_base, row.category)].append(row)
    for rows in title_buckets.values():
        if len(rows) > 1 and any(r.marked for r in rows):
            for row in rows:
                if not row.cvs:
                    changes.warnings.append(f"{row.platform}:{row.title} ({row.id}): 缺少可匹配的主役 CV ID，跳过自动归类")

    proposals = []
    for rows in components(records):
        targets = set().union(*(owners[(r.platform, r.id)] for r in rows))
        if len(targets) > 1:
            changes.warnings.append(f"{rows[0].platform}:{rows[0].base}: 涉及多个旧系列 {sorted(targets)}，跳过候选")
            continue
        target = next(iter(targets), None)
        if target is None and (len(rows) < 2 or not any(r.marked for r in rows)):
            continue
        # Singleton components can anchor existing series, but never create one.
        proposals.append({"rows": rows, "target": target, "title": updated[target]["series title"] if target else rows[0].base})

    # Name a preserved manual series using all of its components in this title/category,
    # rather than choosing a separate CV suffix for each disconnected component.
    grouped = {}
    for proposal in proposals:
        rows, target = proposal['rows'], proposal['target']
        identity = (target, rows[0].match_base, rows[0].category) if target else (None, id(proposal))
        if identity in grouped:
            grouped[identity]['rows'].extend(rows)
        else:
            grouped[identity] = {**proposal, 'rows': list(rows)}
    proposals = list(grouped.values())

    naming = defaultdict(list)
    for proposal in proposals:
        rows = proposal["rows"]
        count = len(set(r.id for r in rows) | ({str(v) for v in updated[proposal['target']]['dramaIds']} if proposal['target'] else set()))
        if count >= 2:
            naming[(rows[0].platform, rows[0].match_base)].append(proposal)
    for group in naming.values():
        # Multiple components already belonging to one manual series remain together.
        distinct = {p["target"] or id(p) for p in group}
        if len(distinct) < 2:
            continue
        categories = {p["rows"][0].category for p in group}
        for proposal in group:
            rows = proposal["rows"]
            title = proposal["title"]
            if proposal["target"] and normalize_title(title) != rows[0].match_base:
                continue  # Preserve explicit/manual distinguishing names.
            if len(categories) > 1:
                title = f"{title} {rows[0].category}"
            peers = [p for p in group if p is not proposal and p["rows"][0].category == rows[0].category
                     and (not proposal['target'] or p['target'] != proposal['target'])]
            if peers:
                cvs = set().union(*(r.cvs for r in rows))
                other = set().union(*(r.cvs for p in peers for r in p["rows"]))
                names = {cv: name for r in rows for cv, name in r.cv_names.items()}
                unique = sorted(cvs - other, key=id_sort)
                available = [cv for cv in unique if cv in names]
                if not available:
                    proposal["blocked"] = True
                    changes.warnings.append(f"{rows[0].platform}:{title}: 无可靠 CV 姓名用于区分，跳过候选")
                    continue
                cv = available[0]
                label = names[cv]
                peer_names = {name for p in peers for r in p['rows'] for name in r.cv_names.values()}
                if label in peer_names:
                    label = '、'.join(names[v] for v in sorted(cvs, key=id_sort) if v in names)
                    peer_labels = {'、'.join({v: name for r in p['rows'] for v, name in r.cv_names.items()}[v]
                                            for v in sorted({v for r in p['rows'] for v in r.cv_names}, key=id_sort)) for p in peers}
                    if label in peer_labels:
                        label += f"；CV ID:{cv}"
                title = f"{title}（{label}）"
            proposal["title"] = title

    # Decide one destination per old target before applying any rename or append.
    # Manual series spanning different base titles must also move as a single entry.
    by_target = defaultdict(list)
    consolidated = []
    for proposal in proposals:
        if proposal.get('blocked'):
            continue
        if proposal['target']:
            by_target[proposal['target']].append(proposal)
        else:
            consolidated.append(proposal)
    for target, candidates in by_target.items():
        original_title = updated[target]['series title']
        titles = {p['title'] for p in candidates if p['title'] != original_title}
        if len(titles) > 1:
            changes.warnings.append(f"{target}: 多个候选要求不同改名 {sorted(titles)}，保留原名并统一追加成员")
        title = next(iter(titles)) if len(titles) == 1 else original_title
        rows = {r.id: r for p in candidates for r in p['rows']}
        consolidated.append({'target': target, 'title': title,
                             'rows': sorted(rows.values(), key=lambda r: id_sort(r.id))})
    proposals = consolidated

    # Reserve every destination before applying mutations; collisions never overwrite old data.
    destinations = defaultdict(list)
    for p in proposals:
        destinations[f"{p['rows'][0].platform}:{p['title']}"].append(p)
    for key, group in destinations.items():
        targets = {p['target'] for p in group}
        collision = len(group) > 1 and (None in targets or len(targets) > 1)
        collision |= key in updated and any(p['target'] != key for p in group)
        if collision:
            for p in group:
                p['blocked'] = True
            changes.warnings.append(f"{key}: 目标名称冲突，保留旧项并跳过候选")
    for proposal in proposals:
        if proposal.get('blocked'):
            continue
        rows, target = proposal['rows'], proposal['target']
        key = f"{rows[0].platform}:{proposal['title']}"
        if target:
            entry = updated[target]
            if key != target:
                del updated[target]
                updated[key] = entry
                entry['series title'] = proposal['title']
                changes.renamed.append((target, key))
            known = {str(v) for v in entry['dramaIds']}
            added = [r.id for r in rows if r.id not in known]
            entry['dramaIds'].extend(added)
            if added:
                changes.appended.setdefault(key, []).extend(added)
        else:
            updated[key] = {'series title': proposal['title'], 'platform': rows[0].platform,
                            'category': rows[0].category, 'dramaIds': [r.id for r in rows]}
            changes.added.append(key)
    for key, added in changes.appended.items():
        added[:] = sorted(set(added), key=id_sort)
        # Preserve all original members, globally sort only this run's additions.
        entry = updated[key]
        additions = set(added)
        entry['dramaIds'] = [v for v in entry['dramaIds'] if str(v) not in additions] + added
    return updated, changes


def fetch_inputs(upstash) -> tuple[dict[str, str], dict[str, dict]]:
    raw, payloads = {}, {}
    for key in KEYS:
        value = upstash(['GET', key])
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key}: 远端值为空或不是 JSON 字符串")
        payload = json.loads(value)
        if key == SERIES_INFO_KEY:
            validate_series(payload)
        else:
            assert_info_download_is_safe(key, payload)
        raw[key], payloads[key] = value, payload
    return raw, payloads


def print_changes(changes: ChangeSet, updated: dict) -> None:
    print(f"[summary] 新建 {len(changes.added)}，追加 {sum(map(len, changes.appended.values()))} 个成员，"
          f"改名 {len(changes.renamed)}，删除 {len(changes.deleted)}，保留缺失 ID {len(changes.missing)}，提示 {len(changes.warnings)}")
    for key in changes.added:
        print(f"[new] {key}: {', '.join(map(str, updated[key]['dramaIds']))}")
    for key, ids in changes.appended.items():
        print(f"[append] {key}: {', '.join(ids)}")
    for old, new in changes.renamed:
        print(f"[rename] {old} -> {new}")
    for key in changes.deleted:
        print(f"[delete] {key}")
    for key, drama_id in changes.missing:
        print(f"[keep-missing] {key}: {drama_id}")
    for warning in changes.warnings:
        print(f"[warn] {warning}")


def atomic_write(path: Path, encoded: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def update_series_info(*, dry_run=False, upstash=upstash_request, path=SERIES_INFO_PATH,
                       backup_root=None, backup_local=backup_local_json_file, write_local=atomic_write) -> dict:
    snapshot_dir = None
    for attempt in range(3):
        raw, payloads = fetch_inputs(upstash)
        records = read_drama_records(payloads[MANBO_INFO_KEY], payloads[MISSEVAN_INFO_KEY])
        updated, changes = build_update(payloads[SERIES_INFO_KEY], records)
        print_changes(changes, updated)
        if dry_run:
            print('[dry-run] 未写入远端或本地文件')
            return updated
        if snapshot_dir is None:
            root = backup_root or ROOT / 'recovery_backups' / 'series-info'
            root.mkdir(parents=True, exist_ok=True)
            snapshot_dir = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=root))
            backup = backup_local(path)
            print(f"[backup] 本地备份: {backup or '本地文件不存在'}；远端快照: {snapshot_dir}")
        for key, value in raw.items():
            (snapshot_dir / f"attempt-{attempt + 1}-{key.replace(':', '-')}.json").write_text(value, encoding='utf-8')
        encoded = json.dumps(updated, ensure_ascii=False, indent=2)
        changed = updated != payloads[SERIES_INFO_KEY]
        if changed:
            published = upstash(['EVAL', CAS_SCRIPT, 1, SERIES_INFO_KEY, string_cas_token(raw[SERIES_INFO_KEY]), encoded])
            if int(published or 0) != 1:
                print(f'[retry] 第 {attempt + 1} 次 CAS 冲突，重新下载三个 key')
                continue
            if upstash(['GET', SERIES_INFO_KEY]) != encoded:
                raise RuntimeError(f'远端上传后校验失败；未同步本地；快照: {snapshot_dir}')
        else:
            # Verify that the unchanged source still represents the current remote state.
            if upstash(['GET', SERIES_INFO_KEY]) != raw[SERIES_INFO_KEY]:
                print('[retry] 无变化结果校验时远端已变更，重新下载三个 key')
                continue
            print('[unchanged] 无变化，不重复上传')
        try:
            write_local(path, encoded)
        except OSError as exc:
            status = '远端已更新' if changed else '远端无变化'
            raise RuntimeError(f'{status}、本地未同步；备份/快照: {snapshot_dir}; {exc}') from exc
        print(f'[done] 远端与本地系列已同步: {path}')
        return updated
    raise RuntimeError(f'连续三次并发冲突，未同步本地；快照: {snapshot_dir}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='识别系列并更新远端和本地 series-info')
    parser.add_argument('--dry-run', action='store_true', help='只读取并预览，不写入远端或本地源文件')
    args = parser.parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / '.env')
    try:
        update_series_info(dry_run=args.dry_run)
    except Exception as exc:
        print(f'[error] {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
