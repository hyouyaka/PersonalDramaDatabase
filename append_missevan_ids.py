from __future__ import annotations

import sys

from cvid_map_tools import update_combined_cvid_map
from platform_sync import load_json, MANBO_INFO_PATH, MISSEVAN_INFO_PATH
from refresh_platform_metadata import upsert_missevan_drama_ids


def main(argv: list[str]) -> int:
    drama_ids = [item.strip() for item in argv[1:] if item.strip()]
    if not drama_ids:
        print("Usage: python append_missevan_ids.py <drama_id> [<drama_id> ...]")
        return 1
    stats = upsert_missevan_drama_ids(drama_ids, force=True)
    map_stats = update_combined_cvid_map(
        load_json(MISSEVAN_INFO_PATH, {}),
        load_json(MANBO_INFO_PATH, {"records": []}),
        missevan_drama_ids=set(drama_ids),
        manbo_drama_ids=set(),
    )
    print("猫耳 metadata updated:", stats["processed"])
    print("猫耳 requests:", stats["request_count"])
    print("猫耳 backoff seconds:", stats["last_backoff_seconds"])
    print("猫耳 watch-count entries updated:", stats.get("count_entries_updated", stats["processed"]))
    print("cvid map updated:", map_stats["updated"])
    print("cvid map created:", map_stats["created"])
    print("cvid map unchanged:", map_stats["unchanged"])
    print("cvid map ambiguous:", map_stats["ambiguous_count"])
    if map_stats["ambiguous_samples"]:
        print("cvid map ambiguous samples:", " | ".join(map_stats["ambiguous_samples"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
