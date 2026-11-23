"""Stable person identities; account IDs are opaque, platform-scoped strings."""
from __future__ import annotations

import uuid
from collections import defaultdict


ID_FIELDS = ("cvId", "missevanCvId", "manboCvId")


def account_id(value: object) -> str:
    return str(value).strip() if value is not None else ""


def is_uuid(value: object) -> bool:
    try:
        return str(uuid.UUID(str(value))) == str(value)
    except (ValueError, TypeError, AttributeError):
        return False


def legacy_id(value: object) -> int | str | None:
    """Keep legacy numeric consumers working while allowing UUID virtual IDs."""
    text = account_id(value)
    if text.isascii() and text.isdigit() and str(int(text)) == text:
        return int(text)
    return text or None


def confirmed_key(value: object) -> bool:
    return isinstance(value, str) and value.startswith("person:") and is_uuid(value[7:])


def trend_key(value: object) -> bool:
    if confirmed_key(value):
        return True
    return isinstance(value, str) and any(
        value.startswith(prefix) and bool(value[len(prefix):])
        for prefix in ("missevan:id:", "manbo:id:")
    )


def ensure_cv_identities(mapping: dict, previous: dict | None = None) -> bool:
    """Backfill once; recover the old key by account ID after a rename.

    A name-only observation is not a confirmed person. Do not manufacture a
    person identity for it. Existing platform IDs are never replaced.
    """
    owners = defaultdict(set)
    previous_profiles = defaultdict(list)
    for old_name, row in (previous or {}).items():
        if not isinstance(row, dict) or not confirmed_key(row.get("cvKey")):
            continue
        previous_profiles[row["cvKey"]].append((old_name, row))
        for platform, field in (("missevan", "missevanCvId"), ("manbo", "manboCvId")):
            value = row.get(field) or (row.get("cvId") if platform == "missevan" else None)
            if account_id(value):
                owners[(platform, account_id(value))].add(row["cvKey"])
    changed = False
    for name, row in mapping.items():
        if not isinstance(row, dict):
            raise ValueError(f"Invalid CV profile: {name}")
        before = dict(row)
        for field in ID_FIELDS:
            if row.get(field) not in (None, ""):
                row[field] = account_id(row[field])
        if row.get("cvId") and row.get("missevanCvId") and row["cvId"] != row["missevanCvId"]:
            raise ValueError(f"Conflicting Missevan CVIDs: {name}")
        matches = set()
        prior = (previous or {}).get(name) or {}
        if confirmed_key(prior.get("cvKey")):
            matches.add(prior["cvKey"])
        for platform, field in (("missevan", "missevanCvId"), ("manbo", "manboCvId")):
            value = row.get(field) or (row.get("cvId") if platform == "missevan" else None)
            matches.update(owners.get((platform, account_id(value)), set()))
        if len(matches) > 1:
            raise ValueError(f"Conflicting stable CV identities: {name}")
        key = row.get("cvKey")
        if key and not confirmed_key(key):
            raise ValueError(f"Invalid cvKey: {name}: {key}")
        if matches and key and key not in matches:
            raise ValueError(f"Cannot change persisted cvKey: {name}")
        if not key and (matches or any(row.get(field) for field in ID_FIELDS)):
            virtual = next((row[field] for field in ID_FIELDS if is_uuid(row.get(field))), None)
            row["cvKey"] = next(iter(matches)) if matches else f"person:{virtual or uuid.uuid4()}"
        for old_name, old_row in previous_profiles.get(row.get("cvKey"), []):
            current_name = account_id(row.get("displayName") or name)
            old_display = account_id(old_row.get("displayName") or old_name)
            if old_name != name or old_display != current_name:
                row["aliases"] = list(dict.fromkeys([
                    *(row.get("aliases") or []),
                    *(alias for alias in (old_name, old_display) if alias and alias != current_name),
                ]))
        changed = changed or before != row
    return changed


class CvIdentityIndex:
    def __init__(self, mapping: dict):
        self.by_id = defaultdict(set)
        self.profiles = {}
        self.by_name = defaultdict(set)
        self.by_platform_name = defaultdict(set)
        for name, row in mapping.items():
            key = row.get("cvKey")
            if not confirmed_key(key):
                continue
            self.profiles[key] = (str(row.get("displayName") or name), row)
            for alias in [name, row.get("displayName"), *(row.get("aliases") or [])]:
                if account_id(alias):
                    self.by_name[" ".join(str(alias).split()).casefold()].add(key)
            for platform, field in (("missevan", "missevanCvId"), ("manbo", "manboCvId")):
                value = row.get(field) or (row.get("cvId") if platform == "missevan" else None)
                if account_id(value):
                    self.by_id[(platform, account_id(value))].add(key)
                    for alias in [name, row.get("displayName"), *(row.get("aliases") or [])]:
                        if account_id(alias):
                            self.by_platform_name[(platform, " ".join(str(alias).split()).casefold())].add(key)

    def resolve(self, platform: str, cv_id: object, name: str) -> tuple[str, str, bool]:
        value = account_id(cv_id)
        keys = self.by_id.get((platform, value), set()) if value else set()
        if len(keys) == 1:
            key = next(iter(keys))
            return key, self.profiles[key][0], True
        return (f"{platform}:id:{value}" if value else f"{platform}:name:{name}"), name, False
