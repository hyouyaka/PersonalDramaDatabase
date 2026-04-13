from __future__ import annotations

import sys

from cvid_map_tools import update_combined_cvid_map
from platform_sync import load_json, MANBO_INFO_PATH, MISSEVAN_INFO_PATH
from refresh_platform_metadata import upsert_manbo_drama_ids


def main(argv: list[str]) -> int:
    drama_ids = [item.strip() for item in argv[1:] if item.strip()]
    if not drama_ids:
        print("Usage: python append_manbo_ids.py <drama_id> [<drama_id> ...]")
        return 1
    target_drama_ids = set(drama_ids)
    stats = upsert_manbo_drama_ids(drama_ids, force=True)
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    records = manbo_store.get("records") or []
    synced_main_cv_names = sum(
        1
        for record in records
        if str(record.get("dramaId") or "") in target_drama_ids and record.get("mainCvNames") is not None
    )
    map_stats = update_combined_cvid_map(
        load_json(MISSEVAN_INFO_PATH, {}),
        manbo_store,
        missevan_drama_ids=set(),
        manbo_drama_ids=target_drama_ids,
    )
    print("漫播 processed:", stats["processed"])
    print("漫播 watch counts updated:", stats["processed"])
    print("漫播 mainCvNames synced:", synced_main_cv_names)
    print("cvid map updated:", map_stats["updated"])
    print("cvid map created:", map_stats["created"])
    print("cvid map unchanged:", map_stats["unchanged"])
    print("cvid map ambiguous:", map_stats["ambiguous_count"])
    if map_stats["ambiguous_samples"]:
        print("cvid map ambiguous samples:", " | ".join(map_stats["ambiguous_samples"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
