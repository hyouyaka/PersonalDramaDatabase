from __future__ import annotations

import sys

from cvid_map_tools import (
    BestEffortAvatarLookup, CvAvatarLookup, update_combined_cvid_map,
    UpstashGeneratedManboCvIdAllocator, seed_generated_manbo_cvid_registry,
    load_generated_manbo_cvid_replacements,
)
from platform_sync import is_numeric_drama_id, load_json, MANBO_INFO_PATH, MISSEVAN_INFO_PATH
from platform_sync import is_excluded_drama
from platform_sync import COMBINED_CVID_MAP_PATH, manbo_main_cv_entries, replace_manbo_main_cv_ids, save_json
from refresh_platform_metadata import upsert_manbo_drama_ids
from sync_new_drama_ids import (
    MANBO_INFO_KEY,
    ROOT,
    download_info_file,
    download_support_files,
    load_env_file,
    merge_and_upload_info_file,
    upstash_request,
)


def _referenced_manbo_cv_ids(store: dict) -> set[int]:
    return {entry["cv_id"] for record in store.get("records") or []
            for entry in manbo_main_cv_entries(record) if entry["cv_id"] is not None}


def main(argv: list[str]) -> int:
    drama_ids = [item.strip() for item in argv[1:] if item.strip()]
    if not drama_ids:
        print("Usage: python append_manbo_ids.py <drama_id> [<drama_id> ...]")
        return 1
    invalid_ids = [drama_id for drama_id in drama_ids if not is_numeric_drama_id(drama_id)]
    if invalid_ids:
        print("Invalid 漫播 dramaId (ASCII digits required):", ", ".join(invalid_ids))
        return 1
    excluded = [value for value in drama_ids if is_excluded_drama("manbo", value)]
    if excluded:
        print("Skipped excluded 漫播 dramaIds:", ", ".join(excluded))
        drama_ids = [value for value in drama_ids if not is_excluded_drama("manbo", value)]
        if not drama_ids:
            return 0
    load_env_file(ROOT / ".env")
    download_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH)
    download_support_files()
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    combined_map = load_json(COMBINED_CVID_MAP_PATH, {})
    seed_generated_manbo_cvid_registry(combined_map, manbo_store, upstash=upstash_request)
    replacements = load_generated_manbo_cvid_replacements(upstash=upstash_request)
    referenced_ids = _referenced_manbo_cv_ids(manbo_store)
    target_drama_ids = set(drama_ids)
    stats = upsert_manbo_drama_ids(drama_ids, force=True,
                                  generated_cv_id_allocator=UpstashGeneratedManboCvIdAllocator(upstash=upstash_request))
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    # A refresh may remove the last old reference or introduce one through reused
    # metadata. Confirm both sets, plus every upgrade discovered in this run.
    referenced_ids.update(_referenced_manbo_cv_ids(manbo_store))
    replacements = {old: new for old, new in replacements.items() if old in referenced_ids}
    replacements.update(stats.get("manbo_generated_replacements") or {})
    records = manbo_store.get("records") or []
    synced_main_cv_names = sum(
        1
        for record in records
        if str(record.get("dramaId") or "") in target_drama_ids and record.get("mainCvNames") is not None
    )
    synced_covers = sum(
        1
        for record in records
        if str(record.get("dramaId") or "") in target_drama_ids and str(record.get("cover") or "").strip()
    )
    map_stats = update_combined_cvid_map(
        load_json(MISSEVAN_INFO_PATH, {}),
        manbo_store,
        missevan_drama_ids=set(),
        manbo_drama_ids=target_drama_ids,
        remote=True,
        upstash=upstash_request,
        avatar_lookup=BestEffortAvatarLookup(CvAvatarLookup()),
        persistent_manbo_generated_replacements=replacements,
    )
    confirmed = dict(map_stats.get("manbo_generated_replacements") or {})
    unconfirmed = {old: new for old, new in replacements.items() if confirmed.get(old) != new}
    if unconfirmed:
        raise RuntimeError(
            "Refusing to publish Manbo info with unconfirmed CVID replacements: "
            + ", ".join(f"{old}->{new}" for old, new in sorted(unconfirmed.items()))
        )
    replacements = confirmed
    combined_map = load_json(COMBINED_CVID_MAP_PATH, {})
    names = {int(row["manboCvId"]): row.get("displayName") or key
             for key, row in combined_map.items() if row.get("manboCvId") not in (None, "")}
    migrated = replace_manbo_main_cv_ids(manbo_store, replacements, cv_name_map=names)
    if migrated:
        save_json(MANBO_INFO_PATH, manbo_store)
    merge_and_upload_info_file(
        MANBO_INFO_KEY, MANBO_INFO_PATH, list(dict.fromkeys([*drama_ids, *sorted(migrated)])),
        removed_drama_ids=stats.get("blocked_producer_ids") or [],
    )
    print("migrated 漫播 dramas:", len(migrated))
    print("漫播 processed:", stats["processed"])
    print("漫播 watch counts updated:", stats["processed"])
    print("漫播 mainCvNames synced:", synced_main_cv_names)
    print("漫播 covers synced:", synced_covers)
    print("cvid map updated:", map_stats["updated"])
    print("cvid map created:", map_stats["created"])
    print("cvid map unchanged:", map_stats["unchanged"])
    print("cvid map ambiguous:", map_stats["ambiguous_count"])
    if map_stats["ambiguous_samples"]:
        print("cvid map ambiguous samples:", " | ".join(map_stats["ambiguous_samples"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
