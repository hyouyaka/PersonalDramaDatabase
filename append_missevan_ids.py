from __future__ import annotations

import sys

from cvid_map_tools import (
    BestEffortAvatarLookup,
    CvAvatarLookup,
    load_generated_missevan_cvid_replacements,
    UpstashGeneratedMissevanCvIdAllocator,
    seed_generated_missevan_cvid_registry,
    update_combined_cvid_map,
)
from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    load_json,
    MANBO_INFO_PATH,
    MISSEVAN_INFO_PATH,
    replace_missevan_main_cv_ids,
    save_missevan_store,
)
from refresh_platform_metadata import upsert_missevan_drama_ids
from sync_new_drama_ids import (
    MISSEVAN_INFO_KEY,
    ROOT,
    download_info_file,
    download_support_files,
    load_env_file,
    merge_and_upload_info_file,
    upstash_request,
)


def main(argv: list[str]) -> int:
    drama_ids = [item.strip() for item in argv[1:] if item.strip()]
    if not drama_ids:
        print("Usage: python append_missevan_ids.py <drama_id> [<drama_id> ...]")
        return 1
    load_env_file(ROOT / ".env")
    download_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH)
    download_support_files()
    missevan_store = load_json(MISSEVAN_INFO_PATH, {})
    combined_map = load_json(COMBINED_CVID_MAP_PATH, {})
    seeded_generated_ids = seed_generated_missevan_cvid_registry(combined_map, missevan_store, upstash=upstash_request)
    generated_id_replacements = load_generated_missevan_cvid_replacements(upstash=upstash_request)
    allocator = UpstashGeneratedMissevanCvIdAllocator(upstash=upstash_request)
    cv_upgrade_ambiguities: list[str] = []
    stats = upsert_missevan_drama_ids(
        drama_ids,
        force=True,
        generated_cv_id_allocator=allocator,
        generated_id_replacements=generated_id_replacements,
        cv_upgrade_ambiguities=cv_upgrade_ambiguities,
    )
    map_stats = update_combined_cvid_map(
        load_json(MISSEVAN_INFO_PATH, {}),
        load_json(MANBO_INFO_PATH, {"records": []}),
        missevan_drama_ids=set(drama_ids),
        manbo_drama_ids=set(),
        remote=True,
        upstash=upstash_request,
        avatar_lookup=BestEffortAvatarLookup(CvAvatarLookup()),
        persistent_generated_replacements=generated_id_replacements,
    )
    generated_id_replacements.update(map_stats["missevan_generated_replacements"])
    missevan_store = load_json(MISSEVAN_INFO_PATH, {})
    migrated_drama_ids = replace_missevan_main_cv_ids(missevan_store, generated_id_replacements)
    if migrated_drama_ids:
        save_missevan_store(MISSEVAN_INFO_PATH, missevan_store)
    upload_drama_ids = list(dict.fromkeys([*drama_ids, *sorted(migrated_drama_ids)]))
    merge_and_upload_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH, upload_drama_ids)
    print("猫耳 metadata updated:", stats["processed"])
    print("猫耳 requests:", stats["request_count"])
    print("猫耳 backoff seconds:", stats["last_backoff_seconds"])
    print("猫耳 watch-count entries updated:", stats.get("count_entries_updated", stats["processed"]))
    print("generated cvid registry seeded:", seeded_generated_ids)
    print("generated cvid replacements:", len(generated_id_replacements))
    print("migrated 猫耳 dramas:", len(migrated_drama_ids))
    print("cvid map updated:", map_stats["updated"])
    print("cvid map created:", map_stats["created"])
    print("cvid map unchanged:", map_stats["unchanged"])
    ambiguity_samples = list(dict.fromkeys([*cv_upgrade_ambiguities, *map_stats["ambiguous_samples"]]))
    print("cvid map ambiguous:", map_stats["ambiguous_count"] + len(cv_upgrade_ambiguities))
    if ambiguity_samples:
        print("cvid map ambiguous samples:", " | ".join(ambiguity_samples[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
