from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    MissevanRequester,
    iter_missevan_nodes,
    load_json,
    missevan_main_cv_entries,
    normalize,
    normalize_match,
    request_manbo_json,
    save_json,
    utc_now,
)


GENERATED_MISSEVAN_CVID_MIN = 330000
GENERATED_MISSEVAN_CVID_MAX = 339999
GENERATED_MISSEVAN_CVID_RANGE = GENERATED_MISSEVAN_CVID_MAX - GENERATED_MISSEVAN_CVID_MIN + 1
LEGACY_GENERATED_MISSEVAN_CVID_REGISTRY_KEY = "cvid:generated:missevan"
GENERATED_MISSEVAN_CVID_REGISTRY_KEY = "cvid:generated:{missevan}"
GENERATED_MISSEVAN_CVID_RESERVE_SCRIPT = """
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then
  return tonumber(existing)
end
if redis.call('HEXISTS', KEYS[1], ARGV[2]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3], ARGV[2], ARGV[4])
return tonumber(ARGV[3])
"""
GENERATED_MISSEVAN_CVID_SEED_SCRIPT = """
local by_name = redis.call('HGET', KEYS[1], ARGV[1])
if by_name and tostring(by_name) ~= ARGV[3] then
  return -1
end
local by_id = redis.call('HGET', KEYS[1], ARGV[2])
if by_id and by_id ~= ARGV[4] and by_id ~= '__legacy__' then
  return -1
end
local added = 0
if not by_name then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
  added = added + 1
end
if not by_id or by_id == '__legacy__' then
  redis.call('HSET', KEYS[1], ARGV[2], ARGV[4])
  if not by_id then added = added + 1 end
end
return added
"""


def is_generated_missevan_cvid(value: object) -> bool:
    try:
        cv_id = int(value)
    except (TypeError, ValueError):
        return False
    return GENERATED_MISSEVAN_CVID_MIN <= cv_id <= GENERATED_MISSEVAN_CVID_MAX


def collect_generated_missevan_cvids(combined_map: dict, missevan_store: dict | None = None) -> set[int]:
    generated: set[int] = set()
    for payload in (combined_map or {}).values():
        for value in (payload.get("cvId"), payload.get("missevanCvId")):
            if is_generated_missevan_cvid(value):
                generated.add(int(value))
    for _title, _season, node in iter_missevan_nodes(missevan_store or {}):
        for value in node.get("maincvs") or []:
            if is_generated_missevan_cvid(value):
                generated.add(int(value))
    return generated


def generated_cvid_name_field(display_name: object) -> str:
    return f"name:{normalize_match(display_name)}"


def generated_cvid_id_field(cv_id: int) -> str:
    return f"id:{int(cv_id)}"


def seed_generated_missevan_cvid_registry(combined_map: dict, missevan_store: dict, *, upstash) -> int:
    id_to_name: dict[int, str] = {}
    for key, payload in (combined_map or {}).items():
        cv_id = payload.get("missevanCvId") or payload.get("cvId")
        if is_generated_missevan_cvid(cv_id):
            id_to_name[int(cv_id)] = normalize(payload.get("displayName") or key)
    for _title, _season, node in iter_missevan_nodes(missevan_store or {}):
        cvnames = node.get("cvnames") or {}
        for value in node.get("maincvs") or []:
            if is_generated_missevan_cvid(value):
                id_to_name.setdefault(int(value), normalize(cvnames.get(str(value))))

    legacy_ids = upstash(["SMEMBERS", LEGACY_GENERATED_MISSEVAN_CVID_REGISTRY_KEY]) or []
    for value in legacy_ids:
        if is_generated_missevan_cvid(value):
            id_to_name.setdefault(int(value), "")

    seeded = 0
    for cv_id, display_name in sorted(id_to_name.items()):
        id_field = generated_cvid_id_field(cv_id)
        if display_name:
            result = int(
                upstash(
                    [
                        "EVAL",
                        GENERATED_MISSEVAN_CVID_SEED_SCRIPT,
                        1,
                        GENERATED_MISSEVAN_CVID_REGISTRY_KEY,
                        generated_cvid_name_field(display_name),
                        id_field,
                        str(cv_id),
                        normalize_match(display_name),
                    ]
                ) or 0
            )
            if result < 0:
                raise RuntimeError(f"Generated Missevan CVID registry conflict for {display_name} ({cv_id})")
            seeded += result
        else:
            seeded += int(
                upstash(["HSETNX", GENERATED_MISSEVAN_CVID_REGISTRY_KEY, id_field, "__legacy__"]) or 0
            )
    return seeded


def load_generated_missevan_cvid_replacements(*, upstash) -> dict[int, int]:
    raw = upstash(["HGETALL", GENERATED_MISSEVAN_CVID_REGISTRY_KEY]) or []
    items = raw.items() if isinstance(raw, dict) else zip(raw[0::2], raw[1::2])
    replacements: dict[int, int] = {}
    for field, value in items:
        field = str(field)
        if not field.startswith("upgrade:"):
            continue
        replacements[int(field.split(":", 1)[1])] = int(value)
    return replacements


def persist_generated_missevan_cvid_replacements(replacements: dict[int, int], *, upstash) -> None:
    if not replacements:
        return
    command: list[object] = ["HSET", GENERATED_MISSEVAN_CVID_REGISTRY_KEY]
    for old_id, new_id in sorted(replacements.items()):
        command.extend([f"upgrade:{int(old_id)}", str(int(new_id))])
    upstash(command)


class UpstashGeneratedMissevanCvIdAllocator:
    def __init__(self, *, upstash, randbelow=secrets.randbelow) -> None:
        self.upstash = upstash
        self.randbelow = randbelow

    def __call__(self, combined_map: dict, display_name: object) -> int:
        used = collect_generated_missevan_cvids(combined_map)
        name_field = generated_cvid_name_field(display_name)
        normalized_name = normalize_match(display_name)
        start = self.randbelow(GENERATED_MISSEVAN_CVID_RANGE)
        coprime_steps = [
            step for step in range(1, GENERATED_MISSEVAN_CVID_RANGE) if math.gcd(step, GENERATED_MISSEVAN_CVID_RANGE) == 1
        ]
        step = coprime_steps[self.randbelow(len(coprime_steps))]
        for attempt in range(GENERATED_MISSEVAN_CVID_RANGE):
            candidate = GENERATED_MISSEVAN_CVID_MIN + (start + attempt * step) % GENERATED_MISSEVAN_CVID_RANGE
            if candidate in used:
                continue
            reserved = self.upstash(
                [
                    "EVAL",
                    GENERATED_MISSEVAN_CVID_RESERVE_SCRIPT,
                    1,
                    GENERATED_MISSEVAN_CVID_REGISTRY_KEY,
                    name_field,
                    generated_cvid_id_field(candidate),
                    str(candidate),
                    normalized_name,
                ]
            )
            reserved_id = int(reserved or 0)
            if reserved_id != 0:
                return reserved_id
        raise RuntimeError("Generated Missevan CVID range 330000-339999 is exhausted.")


def load_remote_combined_map(*, upstash=None) -> dict[str, dict]:
    from sync_new_drama_ids import CVID_MAP_KEY, load_remote_json_or_backup, upstash_request

    payload = load_remote_json_or_backup(
        CVID_MAP_KEY,
        COMBINED_CVID_MAP_PATH,
        None,
        upstash=upstash or upstash_request,
        upload_backup_if_missing=True,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unable to load {CVID_MAP_KEY} from Upstash or local backup.")
    return payload


def save_remote_combined_map(data: dict[str, dict], *, upstash=None) -> None:
    from sync_new_drama_ids import CVID_MAP_KEY, upload_json_payload, upstash_request

    save_combined_map(data)
    upload_json_payload(CVID_MAP_KEY, data, upstash=upstash or upstash_request)


def normalize_avatar_url(value: object) -> str:
    url = normalize(value)
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def fetch_missevan_cv_avatar(cv_id: int, requester: MissevanRequester) -> str:
    payload = requester.request_json(f"https://www.missevan.com/dramaapi/cvinfo?page=1&cv_id={cv_id}")
    return normalize_avatar_url(((payload.get("info") or {}).get("cv") or {}).get("icon"))


def fetch_manbo_cv_avatar(cv_id: int, *, manbo_request=request_manbo_json) -> str:
    payload = manbo_request(f"https://api.kilamanbo.com/api/v530/personal/homepage/getUserBaseInfo?uid={cv_id}")
    return normalize_avatar_url((((payload.get("b") or {}).get("userResp") or {}).get("headPortraitUrl")))


class CvAvatarLookup:
    def __init__(
        self,
        *,
        requester: MissevanRequester | None = None,
        manbo_request=request_manbo_json,
    ) -> None:
        self.requester = requester or MissevanRequester(base_delay=4.5, jitter=2.0)
        self.manbo_request = manbo_request
        self.fallback_to_manbo = 0

    def __call__(self, platform: str, cv_id: int) -> str:
        if platform == "猫耳":
            try:
                return fetch_missevan_cv_avatar(cv_id, self.requester)
            except Exception as exc:
                if _http_status(exc) in (403, 404):
                    return ""
                raise
        if platform == "漫播":
            return fetch_manbo_cv_avatar(cv_id, manbo_request=self.manbo_request)
        return ""


class BestEffortAvatarLookup:
    def __init__(self, lookup) -> None:
        self.lookup = lookup
        self.fallback_to_manbo = 0

    def __call__(self, platform: str, cv_id: int) -> str:
        try:
            return self.lookup(platform, cv_id)
        except Exception as exc:
            print(f"[warn] avatar lookup skipped platform={platform} cv_id={cv_id}: {exc}")
            return ""


def payload_avatar(payload: dict, *, avatar_lookup=None, force: bool = False) -> str:
    current = normalize_avatar_url(payload.get("avatar"))
    if (current and not force) or avatar_lookup is None:
        return current
    missevan_id = payload.get("missevanCvId", payload.get("cvId"))
    if missevan_id not in (None, "") and not is_generated_missevan_cvid(missevan_id):
        avatar = normalize_avatar_url(avatar_lookup("猫耳", int(missevan_id)))
        if avatar:
            return avatar
    manbo_id = payload.get("manboCvId")
    if manbo_id not in (None, ""):
        avatar = normalize_avatar_url(avatar_lookup("漫播", int(manbo_id)))
        if avatar and missevan_id not in (None, "") and hasattr(avatar_lookup, "fallback_to_manbo"):
            avatar_lookup.fallback_to_manbo += 1
        return avatar
    return ""


@dataclass
class ObservedCV:
    platform: str
    display_name: str
    platform_cv_id: int | None
    aliases: list[str]


def ensure_name_only_cv_entry(combined_map: dict, display_name: object) -> bool:
    key = normalize(display_name)
    if not key:
        return False
    norm = normalize_match(key)
    matches = {
        existing_key
        for existing_key, payload in (combined_map or {}).items()
        if norm
        in {
            normalize_match(candidate)
            for candidate in [existing_key, payload.get("displayName"), *(payload.get("aliases") or [])]
            if normalize_match(candidate)
        }
    }
    if matches:
        return False
    combined_map[key] = {
        "cvId": None,
        "missevanCvId": None,
        "manboCvId": None,
        "displayName": key,
        "aliases": [],
        "avatar": "",
        "source": "observed",
        "updatedAt": utc_now(),
        "notes": "猫耳搜索未命中，以显示名称识别",
    }
    return True


def ensure_generated_missevan_cv_entry(combined_map: dict, display_name: object, *, allocator) -> tuple[int | None, bool]:
    key = normalize(display_name)
    if not key:
        return None, False
    norm = normalize_match(key)
    matches = {
        existing_key
        for existing_key, payload in (combined_map or {}).items()
        if norm
        in {
            normalize_match(candidate)
            for candidate in [existing_key, payload.get("displayName"), *(payload.get("aliases") or [])]
            if normalize_match(candidate)
        }
    }
    if len(matches) > 1:
        return None, False
    target_key = next(iter(matches), key)
    payload = dict(combined_map.get(target_key) or {})
    existing = payload.get("missevanCvId") or payload.get("cvId")
    if existing not in (None, ""):
        return int(existing), False
    generated_id = int(allocator(combined_map, display_name))
    payload["cvId"] = generated_id
    payload["missevanCvId"] = generated_id
    payload.setdefault("manboCvId", None)
    payload["displayName"] = normalize(payload.get("displayName") or target_key)
    payload["aliases"] = [
        normalize(alias) for alias in (payload.get("aliases") or []) if normalize(alias) and normalize(alias) != target_key
    ]
    payload.setdefault("avatar", "")
    payload["source"] = "missevan_generated"
    payload["updatedAt"] = utc_now()
    payload["notes"] = f"猫耳搜索未命中，使用自动生成 CVID {generated_id}"
    combined_map[target_key] = payload
    return generated_id, True


def _nickname_variants(value: object) -> list[str]:
    text = normalize(value)
    if not text:
        return []
    variants = [text]
    stripped = text
    for token in ("🔅", "🌞", "⭐", "✨"):
        if token in stripped:
            head = normalize(stripped.split(token, 1)[0])
            if head and head not in variants:
                variants.append(head)
    stripped = normalize(stripped.replace("729声工场", ""))
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


def load_combined_map() -> dict[str, dict]:
    return load_json(COMBINED_CVID_MAP_PATH, {})


def save_combined_map(data: dict[str, dict]) -> None:
    save_json(COMBINED_CVID_MAP_PATH, data)


def collect_observed_cvs(
    missevan_store: dict,
    manbo_store: dict,
    *,
    missevan_drama_ids: set[str] | None = None,
    manbo_drama_ids: set[str] | None = None,
) -> list[ObservedCV]:
    observed: list[ObservedCV] = []
    for _series_title, _season_key, node in iter_missevan_nodes(missevan_store):
        drama_id = str(node.get("dramaId") or "").strip()
        if missevan_drama_ids is not None and drama_id not in missevan_drama_ids:
            continue
        for entry in missevan_main_cv_entries(node):
            cv_id = entry["cv_id"]
            raw_name = normalize(entry["display_name"])
            aliases = _nickname_variants(raw_name)
            observed.append(ObservedCV("猫耳", raw_name or f"猫耳CV_{cv_id}", cv_id, aliases))
    for record in (manbo_store.get("records") or []):
        drama_id = str(record.get("dramaId") or "").strip()
        if manbo_drama_ids is not None and drama_id not in manbo_drama_ids:
            continue
        ids = record.get("mainCvIds") or []
        names = record.get("mainCvNicknames") or []
        for idx, cv_id in enumerate(ids):
            raw_name = normalize(names[idx] if idx < len(names) else "")
            aliases = _nickname_variants(raw_name)
            observed.append(ObservedCV("漫播", raw_name or f"漫播CV_{cv_id}", int(cv_id), aliases))
    return observed


def update_combined_cvid_map(
    missevan_store: dict,
    manbo_store: dict,
    *,
    missevan_drama_ids: set[str] | None = None,
    manbo_drama_ids: set[str] | None = None,
    remote: bool = False,
    upstash=None,
    avatar_lookup=None,
    force_avatar: bool = False,
    persistent_generated_replacements: dict[int, int] | None = None,
) -> dict:
    current = load_remote_combined_map(upstash=upstash) if remote else load_combined_map()
    now = utc_now()
    ambiguous: list[str] = []
    created = 0
    updated = 0
    unchanged = 0
    generated_replacements: dict[int, int] = dict(persistent_generated_replacements or {})

    for key, existing_payload in list(current.items()):
        payload = dict(existing_payload)
        existing_id = payload.get("missevanCvId") or payload.get("cvId")
        if not is_generated_missevan_cvid(existing_id):
            continue
        real_id = generated_replacements.get(int(existing_id))
        if real_id is None:
            continue
        payload["cvId"] = int(real_id)
        payload["missevanCvId"] = int(real_id)
        payload["source"] = "observed"
        payload["notes"] = f"自动生成 CVID {int(existing_id)} 已升级为真实猫耳 CVID {int(real_id)}"
        payload["updatedAt"] = now
        current[key] = payload
        updated += 1

    name_index: dict[str, set[str]] = {}
    missevan_id_index: dict[int, set[str]] = {}
    manbo_id_index: dict[int, set[str]] = {}

    def register_indexes(key: str, payload: dict) -> None:
        for candidate in [key, payload.get("displayName"), *(payload.get("aliases") or [])]:
            norm = normalize_match(candidate)
            if norm:
                name_index.setdefault(norm, set()).add(key)
        missevan_id = payload.get("missevanCvId", payload.get("cvId"))
        manbo_id = payload.get("manboCvId")
        if missevan_id not in (None, ""):
            missevan_id_index.setdefault(int(missevan_id), set()).add(key)
        if manbo_id not in (None, ""):
            manbo_id_index.setdefault(int(manbo_id), set()).add(key)

    for key, payload in current.items():
        if normalize(key):
            register_indexes(key, payload)

    for item in collect_observed_cvs(
        missevan_store,
        manbo_store,
        missevan_drama_ids=missevan_drama_ids,
        manbo_drama_ids=manbo_drama_ids,
    ):
        candidate_keys: set[str] = set()
        if item.platform_cv_id is not None:
            id_index = missevan_id_index if item.platform == "猫耳" else manbo_id_index
            candidate_keys.update(id_index.get(int(item.platform_cv_id), set()))
        for candidate in [item.display_name, *item.aliases]:
            candidate_keys.update(name_index.get(normalize_match(candidate), set()))

        if len(candidate_keys) > 1:
            ambiguous.append(f"{item.platform}:{item.display_name}")
            continue

        if len(candidate_keys) == 1:
            key = next(iter(candidate_keys))
            payload = dict(current[key])
            generated_metadata_changed = False
            if item.platform == "猫耳":
                existing = payload.get("missevanCvId", payload.get("cvId"))
                observed_id = int(item.platform_cv_id) if item.platform_cv_id is not None else None
                replaced_generated_id: int | None = None
                if observed_id is not None and existing not in (None, "") and int(existing) != observed_id:
                    existing_id = int(existing)
                    if is_generated_missevan_cvid(existing_id) and not is_generated_missevan_cvid(observed_id):
                        generated_replacements[existing_id] = observed_id
                        replaced_generated_id = existing_id
                    elif not is_generated_missevan_cvid(existing_id) and is_generated_missevan_cvid(observed_id):
                        generated_replacements[observed_id] = existing_id
                        observed_id = existing_id
                    else:
                        ambiguous.append(f"{item.platform}:{item.display_name}")
                        continue
                next_cv_id = observed_id if observed_id is not None else payload.get("cvId")
                next_missevan_cv_id = observed_id if observed_id is not None else payload.get("missevanCvId")
                id_changed = payload.get("cvId") != next_cv_id or payload.get("missevanCvId") != next_missevan_cv_id
                payload["cvId"] = next_cv_id
                payload["missevanCvId"] = next_missevan_cv_id
                if replaced_generated_id is not None:
                    payload["source"] = "observed"
                    payload["notes"] = (
                        f"自动生成 CVID {replaced_generated_id} 已升级为真实猫耳 CVID {int(observed_id)}"
                    )
                elif observed_id is not None and is_generated_missevan_cvid(observed_id):
                    generated_note = f"猫耳搜索未命中，使用自动生成 CVID {observed_id}"
                    generated_metadata_changed = (
                        payload.get("source") != "missevan_generated" or payload.get("notes") != generated_note
                    )
                    payload["source"] = "missevan_generated"
                    payload["notes"] = generated_note
            else:
                existing = payload.get("manboCvId")
                if item.platform_cv_id is not None and existing not in (None, "") and int(existing) != int(item.platform_cv_id):
                    ambiguous.append(f"{item.platform}:{item.display_name}")
                    continue
                next_manbo_cv_id = int(item.platform_cv_id) if item.platform_cv_id is not None else payload.get("manboCvId")
                id_changed = payload.get("manboCvId") != next_manbo_cv_id
                payload["manboCvId"] = next_manbo_cv_id
            aliases = []
            for alias in payload.get("aliases") or []:
                alias = normalize(alias)
                if alias and alias != key and alias not in aliases:
                    aliases.append(alias)
            for alias in item.aliases:
                alias = normalize(alias)
                if alias and alias != key and alias not in aliases:
                    aliases.append(alias)
            aliases_changed = aliases != (payload.get("aliases") or [])
            display_name_changed = (payload.get("displayName") or key) != payload.get("displayName")
            payload["aliases"] = aliases
            payload["displayName"] = payload.get("displayName") or key
            payload.setdefault("notes", "")
            next_avatar = payload_avatar(payload, avatar_lookup=avatar_lookup, force=force_avatar)
            avatar_changed = payload.get("avatar") != next_avatar
            payload["avatar"] = next_avatar
            if not id_changed and not aliases_changed and not display_name_changed and not avatar_changed and not generated_metadata_changed:
                unchanged += 1
                continue
            payload["updatedAt"] = now
            current[key] = payload
            updated += 1
            continue

        key = normalize(item.display_name)
        if not key:
            key = f"{item.platform}CV_{item.platform_cv_id}" if item.platform_cv_id is not None else "未命名CV"
        if key in current:
            ambiguous.append(f"{item.platform}:{item.display_name}")
            continue
        payload = {
            "cvId": int(item.platform_cv_id) if item.platform == "猫耳" and item.platform_cv_id is not None else None,
            "missevanCvId": int(item.platform_cv_id) if item.platform == "猫耳" and item.platform_cv_id is not None else None,
            "manboCvId": int(item.platform_cv_id) if item.platform == "漫播" and item.platform_cv_id is not None else None,
            "displayName": key,
            "aliases": [alias for alias in item.aliases if normalize(alias) and normalize(alias) != key],
            "avatar": "",
            "source": "observed",
            "updatedAt": now,
            "notes": "猫耳搜索未命中，以显示名称识别" if item.platform == "猫耳" and item.platform_cv_id is None else "",
        }
        payload["avatar"] = payload_avatar(payload, avatar_lookup=avatar_lookup, force=force_avatar)
        current[key] = payload
        register_indexes(key, payload)
        created += 1

    if remote:
        if upstash is None:
            from sync_new_drama_ids import upstash_request

            upstash = upstash_request
        persist_generated_missevan_cvid_replacements(generated_replacements, upstash=upstash)
        save_remote_combined_map(current, upstash=upstash)
    else:
        save_combined_map(current)
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "ambiguous_count": len(ambiguous),
        "ambiguous_samples": ambiguous[:20],
        "total_entries": len(current),
        "missevan_generated_replacements": generated_replacements,
    }
