from __future__ import annotations

from dataclasses import dataclass

from platform_sync import COMBINED_CVID_MAP_PATH, iter_missevan_nodes, load_json, normalize, normalize_match, save_json, utc_now


@dataclass
class ObservedCV:
    platform: str
    display_name: str
    platform_cv_id: int | None
    aliases: list[str]


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
        cvnames = node.get("cvnames") or {}
        for cv_id in node.get("maincvs") or []:
            raw_name = normalize(cvnames.get(str(cv_id)))
            aliases = _nickname_variants(raw_name)
            observed.append(ObservedCV("猫耳", raw_name or f"猫耳CV_{cv_id}", int(cv_id), aliases))
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
) -> dict:
    current = load_combined_map()
    now = utc_now()
    ambiguous: list[str] = []
    created = 0
    updated = 0
    unchanged = 0

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
            if item.platform == "猫耳":
                existing = payload.get("missevanCvId", payload.get("cvId"))
                if existing not in (None, "") and int(existing) != int(item.platform_cv_id):
                    ambiguous.append(f"{item.platform}:{item.display_name}")
                    continue
                next_cv_id = int(item.platform_cv_id) if item.platform_cv_id is not None else payload.get("cvId")
                next_missevan_cv_id = int(item.platform_cv_id) if item.platform_cv_id is not None else payload.get("missevanCvId")
                id_changed = payload.get("cvId") != next_cv_id or payload.get("missevanCvId") != next_missevan_cv_id
                payload["cvId"] = next_cv_id
                payload["missevanCvId"] = next_missevan_cv_id
            else:
                existing = payload.get("manboCvId")
                if existing not in (None, "") and int(existing) != int(item.platform_cv_id):
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
            if not id_changed and not aliases_changed and not display_name_changed:
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
            "source": "observed",
            "updatedAt": now,
            "notes": "",
        }
        current[key] = payload
        register_indexes(key, payload)
        created += 1

    save_combined_map(current)
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "ambiguous_count": len(ambiguous),
        "ambiguous_samples": ambiguous[:20],
        "total_entries": len(current),
    }
