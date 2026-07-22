from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from platform_sync import MANBO_INFO_PATH, normalize, request_manbo_json, save_json, utc_now
from sync_new_drama_ids import (
    MANBO_INFO_KEY,
    ROOT,
    assert_info_download_is_safe,
    configure_stdio,
    decode_remote_info_payload,
    load_env_file,
    upload_json_file,
    upstash_request,
    write_json_work_copy,
)
from upstash_v2 import publish_info_v2


SAVE_EVERY = 25
COVER_FIELDS = ("coverPic", "largePic", "cover", "sharePicUrl")


def _cover_missing(value: object) -> bool:
    return not normalize(value)


def http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def extract_manbo_cover(payload: dict) -> str:
    data = (payload or {}).get("data") or {}
    for field in COVER_FIELDS:
        cover = normalize(data.get(field))
        if cover:
            return cover
    return ""


def count_missing_covers(store: dict) -> int:
    return sum(
        1
        for record in (store or {}).get("records") or []
        if isinstance(record, dict) and _cover_missing(record.get("cover"))
    )


def download_manbo_info(path: Path, *, upstash=upstash_request) -> tuple[dict, str]:
    raw = upstash(["GET", MANBO_INFO_KEY])
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"{MANBO_INFO_KEY} is empty or unsupported")
    payload = decode_remote_info_payload(MANBO_INFO_KEY, raw)
    if path.resolve() == MANBO_INFO_PATH.resolve():
        assert_info_download_is_safe(MANBO_INFO_KEY, payload)
    backup_path = write_json_work_copy(path, payload)
    if backup_path is not None:
        print(f"[backup] {path.name} -> {backup_path}")
    print(f"[ok] downloaded {MANBO_INFO_KEY} -> {path.name}")
    return (
        payload if isinstance(payload, dict) else {"version": 1, "records": []},
        raw,
    )


def upload_manbo_info(path: Path, *, source_encoded: str, upstash=upstash_request) -> None:
    if path.resolve() == MANBO_INFO_PATH.resolve():
        upload_json_file(
            MANBO_INFO_KEY,
            path,
            upstash=upstash,
            source_encoded=source_encoded,
        )
        return
    value = path.read_text(encoding="utf-8")
    publish_info_v2(
        MANBO_INFO_KEY,
        json.loads(value),
        upstash=upstash,
        force=True,
        source_encoded=source_encoded,
    )
    print(f"[ok] uploaded authoritative {path.name} -> {MANBO_INFO_KEY}")


def backfill_manbo_covers(
    *,
    path: Path = MANBO_INFO_PATH,
    upstash=upstash_request,
    manbo_request=request_manbo_json,
    upload: bool = True,
) -> dict:
    store, source_encoded = download_manbo_info(path, upstash=upstash)
    records = [record for record in store.get("records") or [] if isinstance(record, dict)]
    targets: list[tuple[dict, str]] = []
    skipped = 0
    for record in records:
        drama_id = normalize(record.get("dramaId"))
        if not drama_id:
            continue
        if not _cover_missing(record.get("cover")):
            skipped += 1
            continue
        targets.append((record, drama_id))

    processed = 0
    failed = 0
    try:
        for idx, (record, drama_id) in enumerate(targets, start=1):
            print(f"[漫播封面] 正在补齐 ID={drama_id} ({idx}/{len(targets)})")
            try:
                payload = manbo_request(f"https://www.kilamanbo.world/web_manbo/dramaDetail?dramaId={drama_id}")
            except Exception as exc:
                if http_status(exc) in (403, 404):
                    record.setdefault("cover", "")
                    failed += 1
                    print(f"[漫播封面] 跳过 ID={drama_id} status={http_status(exc)}")
                    if (processed + failed) % SAVE_EVERY == 0 or idx == len(targets):
                        store["updatedAt"] = utc_now()
                        save_json(path, store)
                    continue
                raise
            cover = extract_manbo_cover(payload)
            if cover:
                record["cover"] = cover
            else:
                record.setdefault("cover", "")
            processed += 1
            if processed % SAVE_EVERY == 0 or idx == len(targets):
                store["updatedAt"] = utc_now()
                save_json(path, store)
    except Exception:
        store["updatedAt"] = utc_now()
        save_json(path, store)
        raise

    store["updatedAt"] = utc_now()
    save_json(path, store)
    missing_cover = count_missing_covers(store)
    uploaded = False
    if upload:
        upload_manbo_info(path, source_encoded=source_encoded, upstash=upstash)
        uploaded = True
    return {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "missing_cover": missing_cover,
        "uploaded": uploaded,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Manbo cover URLs from the remote info store")
    parser.add_argument("--no-upload", action="store_true", help=f"Do not upload the updated store to {MANBO_INFO_KEY}")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    path: Path = MANBO_INFO_PATH,
    upstash=upstash_request,
    manbo_request=request_manbo_json,
) -> int:
    args = parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    stats = backfill_manbo_covers(
        path=path,
        upstash=upstash,
        manbo_request=manbo_request,
        upload=not args.no_upload,
    )
    print("漫播 covers processed:", stats["processed"])
    print("漫播 covers skipped:", stats["skipped"])
    print("漫播 covers failed:", stats["failed"])
    print("漫播 covers missing:", stats["missing_cover"])
    print("漫播 covers uploaded:", stats["uploaded"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
