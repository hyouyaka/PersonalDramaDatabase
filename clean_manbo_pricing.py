from __future__ import annotations

import sys

from platform_sync import (
    MANBO_COUNTS_PATH,
    MANBO_INFO_PATH,
    load_cache,
    load_json,
    normalize,
    request_manbo_json,
    safe_int,
    save_cache,
    save_json,
)

MANBO_PRICING_EXCLUSIONS = {
    "1703186311042564211",  # 灰大叔与混血王子
}


def classify_manbo_pricing(payload: dict) -> str:
    data = payload.get("data") or {}
    sets = data.get("setRespList")
    if not isinstance(sets, list) or not sets:
        return "keep"

    if not all(
        safe_int(item.get("price")) == 0
        and safe_int(item.get("memberPrice")) == 0
        and safe_int(item.get("vipFree")) == 0
        for item in sets
    ):
        return "keep"

    top_price = safe_int(data.get("price"))
    top_member_price = safe_int(data.get("memberPrice"))
    if top_price == 0 and top_member_price == 0:
        return "free"
    if top_price == 100 and top_member_price == 100:
        return "100_redbean"
    return "keep"


def clean_manbo_pricing() -> dict:
    info = load_json(MANBO_INFO_PATH, {"version": 1, "updatedAt": None, "records": []})
    records = info.get("records", [])
    cache = load_cache(MANBO_COUNTS_PATH)

    kept_records: list[dict] = []
    removed_free: list[dict] = []
    removed_redbean: list[dict] = []
    skipped: list[dict] = []

    for idx, record in enumerate(records, start=1):
        drama_id = str(record.get("dramaId") or "").strip()
        if not drama_id:
            kept_records.append(record)
            continue
        if drama_id in MANBO_PRICING_EXCLUSIONS:
            kept_records.append(record)
            continue

        try:
            payload = request_manbo_json(f"https://www.kilamanbo.world/web_manbo/dramaDetail?dramaId={drama_id}")
        except Exception as exc:
            kept_records.append(record)
            skipped.append(
                {
                    "dramaId": drama_id,
                    "title": normalize(record.get("name")),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        title = normalize((payload.get("data") or {}).get("title") or record.get("name"))
        category = classify_manbo_pricing(payload)
        item = {"dramaId": drama_id, "title": title, "category": category}
        if category == "free":
            removed_free.append(item)
            cache.get("counts", {}).pop(drama_id, None)
            continue
        if category == "100_redbean":
            removed_redbean.append(item)
            cache.get("counts", {}).pop(drama_id, None)
            continue

        kept_records.append(record)
        if idx % 50 == 0:
            print(f"checked {idx}/{len(records)}")

    info["records"] = kept_records
    save_json(MANBO_INFO_PATH, info)
    save_cache(MANBO_COUNTS_PATH, cache)

    return {
        "scanned": len(records),
        "deleted": len(removed_free) + len(removed_redbean),
        "kept": len(kept_records),
        "free": removed_free,
        "100_redbean": removed_redbean,
        "skipped": skipped,
    }


def print_report(result: dict) -> None:
    print("漫播 scanned:", result["scanned"])
    print("漫播 deleted:", result["deleted"])
    print("漫播 kept:", result["kept"])
    print("免费剧 deleted:", len(result["free"]))
    for item in result["free"]:
        print(f"  {item['dramaId']} | {item['title']} | 免费剧")
    print("100红豆剧 deleted:", len(result["100_redbean"]))
    for item in result["100_redbean"]:
        print(f"  {item['dramaId']} | {item['title']} | 100红豆剧")
    print("跳过:", len(result["skipped"]))
    for item in result["skipped"]:
        print(f"  {item['dramaId']} | {item['title']} | {item['reason']}")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("Usage: python clean_manbo_pricing.py")
        return 1
    result = clean_manbo_pricing()
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
