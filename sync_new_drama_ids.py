from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from platform_sync import (
    COMBINED_CVID_MAP_PATH,
    MANBO_INFO_PATH,
    MISSEVAN_INFO_PATH,
    SERIES_INFO_PATH,
    iter_missevan_nodes,
    load_json,
    normalize,
)


ROOT = Path(__file__).resolve().parent
QUEUE_KEY = "new:dramaIDs"
MANBO_INFO_KEY = "manbo:info:v1"
MISSEVAN_INFO_KEY = "missevan:info:v1"
CVID_MAP_KEY = "cvid-map:v1"
SERIES_INFO_KEY = "drama:series-info:v1"
WATCHCOUNT_KEY_PREFIXES = {
    "missevan": "missevan:watchcount",
    "manbo": "manbo:watchcount",
}
INFO_UPLOAD_MIN_COUNTS = {
    MISSEVAN_INFO_KEY: 100,
    MANBO_INFO_KEY: 50,
}
ALLOW_SMALL_INFO_UPLOAD_ENV = "ALLOW_SMALL_INFO_UPLOAD"


class RemoteJsonMissing(RuntimeError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def upstash_request(command: list[object]) -> object:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        raise RuntimeError("Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN in environment.")
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=command,
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload.get("result")


def load_queue() -> dict[str, list[str]]:
    raw = upstash_request(["GET", QUEUE_KEY])
    if raw in (None, ""):
        return {"manbo": [], "missevan": []}
    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, dict):
        data = raw
    else:
        raise RuntimeError(f"Unsupported payload type for {QUEUE_KEY}: {type(raw).__name__}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{QUEUE_KEY} must be a JSON object.")
    return {
        "manbo": normalize_ids(data.get("manbo") or []),
        "missevan": normalize_ids(data.get("missevan") or []),
    }


def normalize_ids(values: list[object]) -> list[str]:
    out: list[str] = []
    for value in values:
        item = normalize(value)
        if item and item not in out:
            out.append(item)
    return out


def run_script(script_name: str, drama_ids: list[str]) -> None:
    if not drama_ids:
        print(f"[skip] {script_name}: no ids")
        return
    command = [sys.executable, "-X", "utf8", script_name, *drama_ids]
    print(f"$ {subprocess.list2cmdline(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"{script_name} failed with exit code {return_code}")


def upload_json_file(key: str, path: Path, *, upstash=upstash_request) -> None:
    value = path.read_text(encoding="utf-8")
    assert_info_upload_is_safe(key, value, path)
    if key == CVID_MAP_KEY:
        assert_cvid_map_upload_meets_remote_floor(json.loads(value), upstash=upstash)
    result = upstash(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload {path.name} to {key}: {result!r}")
    print(f"[ok] uploaded {path.name} -> {key}")


def upload_json_payload(key: str, payload: object, *, upstash=upstash_request) -> None:
    value = json.dumps(payload, ensure_ascii=False)
    assert_info_upload_is_safe(key, value, Path(key))
    if key == CVID_MAP_KEY:
        assert_cvid_map_upload_meets_remote_floor(payload, upstash=upstash)
    result = upstash(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload payload to {key}: {result!r}")
    print(f"[ok] uploaded payload -> {key}")


def write_info_payload(path: Path, payload: object) -> str:
    value = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(value, encoding="utf-8")
    return value


def count_info_payload(key: str, payload: object) -> int | None:
    if key == MISSEVAN_INFO_KEY and isinstance(payload, dict):
        return len(payload)
    if key == MANBO_INFO_KEY and isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return len(records)
    return None


def remote_json_count(key: str, *, upstash=upstash_request) -> int | None:
    raw = upstash(["GET", key])
    if raw in (None, ""):
        return None
    payload = decode_remote_json_payload(key, raw)
    if isinstance(payload, dict):
        return len(payload)
    return None


def assert_cvid_map_upload_meets_remote_floor(payload: object, *, upstash=upstash_request) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to upload {CVID_MAP_KEY}: expected a JSON object.")
    remote_count = remote_json_count(CVID_MAP_KEY, upstash=upstash)
    if remote_count in (None, 0):
        return
    minimum = (remote_count + 1) // 2
    if len(payload) < minimum:
        raise RuntimeError(
            f"Refusing to upload {CVID_MAP_KEY}: {len(payload)} entries found, "
            f"expected at least {minimum} (half of current remote count {remote_count})."
        )


def assert_info_upload_is_safe(key: str, value: str, path: Path) -> None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to upload invalid JSON from {path.name} to {key}: {exc}") from exc
    if key in (CVID_MAP_KEY, SERIES_INFO_KEY):
        if not isinstance(payload, dict):
            raise RuntimeError(f"Refusing to upload {path.name} to {key}: expected a JSON object.")
        if not payload:
            raise RuntimeError(f"Refusing to upload {path.name} to {key}: payload is empty.")
        for item_key, item_value in payload.items():
            if not isinstance(item_key, str) or not isinstance(item_value, dict):
                raise RuntimeError(f"Refusing to upload {path.name} to {key}: unexpected payload shape.")
        return
    minimum = INFO_UPLOAD_MIN_COUNTS.get(key)
    if minimum is None or os.environ.get(ALLOW_SMALL_INFO_UPLOAD_ENV) == "1":
        return
    count = count_info_payload(key, payload)
    if count is None:
        raise RuntimeError(f"Refusing to upload {path.name} to {key}: unexpected info store shape.")
    if count < minimum:
        raise RuntimeError(
            f"Refusing to upload {path.name} to {key}: only {count} records found, "
            f"expected at least {minimum}. Set {ALLOW_SMALL_INFO_UPLOAD_ENV}=1 to override intentionally."
        )


def decode_remote_info_payload(key: str, raw: object) -> object:
    if raw in (None, ""):
        raise RuntimeError(f"Refusing to download {key}: remote value is empty.")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Refusing to download {key}: remote value is invalid JSON: {exc}") from exc
    return raw


def assert_info_download_is_safe(key: str, payload: object) -> None:
    minimum = INFO_UPLOAD_MIN_COUNTS.get(key)
    count = count_info_payload(key, payload)
    if count is None:
        raise RuntimeError(f"Refusing to download {key}: unexpected info store shape.")
    if minimum is not None and count < minimum:
        raise RuntimeError(
            f"Refusing to download {key}: only {count} records found, expected at least {minimum}."
        )


def backup_local_info_file(path: Path) -> Path | None:
    return backup_local_json_file(path)


def backup_local_json_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = ROOT / "recovery_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{stamp}_{path.name}"
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path


def decode_remote_json_payload(key: str, raw: object) -> object:
    if raw in (None, ""):
        raise RemoteJsonMissing(f"{key} is empty or missing.")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{key} contains invalid JSON: {exc}") from exc
    return raw


def parse_remote_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def watchcount_key(platform: str, suffix: str) -> str:
    prefix = WATCHCOUNT_KEY_PREFIXES.get(platform)
    if prefix is None:
        raise RuntimeError(f"Unsupported watchcount platform: {platform}")
    return f"{prefix}:{suffix}"


def assert_watchcount_payload_is_safe(key: str, payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to use {key}: expected a JSON object.")
    if not isinstance(payload.get("_meta"), dict):
        raise RuntimeError(f"Refusing to use {key}: missing _meta object.")
    if not isinstance(payload.get("counts"), dict):
        raise RuntimeError(f"Refusing to use {key}: missing counts object.")


def watchcount_updated_at(payload: object) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    return parse_remote_iso_datetime(((payload.get("_meta") or {}).get("updated_at")))


def load_watchcount_payload(path: Path) -> dict:
    payload = load_json(path, {"_meta": {"updated_at": None}, "counts": {}})
    if not isinstance(payload, dict):
        return {"_meta": {"updated_at": None}, "counts": {}}
    payload.setdefault("_meta", {"updated_at": None})
    payload.setdefault("counts", {})
    return payload


def decode_remote_watchcount_payload(key: str, raw: object) -> dict:
    payload = decode_remote_json_payload(key, raw)
    assert_watchcount_payload_is_safe(key, payload)
    return payload


def sync_remote_watchcount_if_newer(
    platform: str,
    path: Path,
    *,
    upstash=upstash_request,
    force: bool = False,
) -> bool:
    key = watchcount_key(platform, "latest")
    local_payload = load_watchcount_payload(path)
    try:
        remote_payload = decode_remote_watchcount_payload(key, upstash(["GET", key]))
    except RemoteJsonMissing:
        print(f"[skip] {key}: remote value is empty or missing")
        return False

    local_updated = watchcount_updated_at(local_payload)
    remote_updated = watchcount_updated_at(remote_payload)
    should_download = force or (remote_updated is not None and (local_updated is None or remote_updated > local_updated))
    if not should_download:
        print(f"[skip] {key}: local watchcount is up to date")
        return False

    backup_path = write_json_work_copy(path, remote_payload)
    if backup_path is not None:
        print(f"[backup] {path.name} -> {backup_path}")
    print(f"[ok] downloaded {key} -> {path.name}")
    return True


def upload_watchcount_file(platform: str, path: Path, *, upstash=upstash_request) -> None:
    payload = load_watchcount_payload(path)
    latest_key = watchcount_key(platform, "latest")
    assert_watchcount_payload_is_safe(latest_key, payload)
    updated_at = watchcount_updated_at(payload) or datetime.now(timezone.utc)
    date_key = watchcount_key(platform, updated_at.astimezone(timezone.utc).date().isoformat())
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for key in (date_key, latest_key):
        result = upstash(["SET", key, encoded])
        if result != "OK":
            raise RuntimeError(f"Failed to upload {path.name} to {key}: {result!r}")
        print(f"[ok] uploaded {path.name} -> {key} ({len(encoded)} bytes)")


def write_json_work_copy(path: Path, payload: object) -> Path | None:
    backup_path = backup_local_json_file(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path


def load_remote_json_or_backup(
    key: str,
    path: Path,
    default: object,
    *,
    upstash=upstash_request,
    upload_backup_if_missing: bool = False,
    write_remote_to_local: bool = True,
) -> object:
    try:
        raw = upstash(["GET", key])
        payload = decode_remote_json_payload(key, raw)
        if write_remote_to_local:
            backup_path = write_json_work_copy(path, payload)
            if backup_path is not None:
                print(f"[backup] {path.name} -> {backup_path}")
            print(f"[ok] downloaded {key} -> {path.name}")
        return payload
    except Exception as exc:
        local_exists = path.exists()
        if local_exists:
            local_payload = load_json(path, default)
            print(f"[local backup] using {path.name} for {key}: {exc}")
            if upload_backup_if_missing and isinstance(exc, RemoteJsonMissing):
                upload_json_payload(key, local_payload, upstash=upstash)
            return local_payload
        print(f"[local backup] no {path.name} backup for {key}: {exc}")
        return default


def download_json_key_to_file(
    key: str,
    path: Path,
    default: object,
    *,
    upload_backup_if_missing: bool = False,
) -> object:
    return load_remote_json_or_backup(
        key,
        path,
        default,
        upload_backup_if_missing=upload_backup_if_missing,
    )


def download_info_file(key: str, path: Path) -> None:
    payload = decode_remote_info_payload(key, upstash_request(["GET", key]))
    assert_info_download_is_safe(key, payload)
    backup_path = write_json_work_copy(path, payload)
    if backup_path is not None:
        print(f"[backup] {path.name} -> {backup_path}")
    print(f"[ok] downloaded {key} -> {path.name}")


def download_info_files() -> None:
    download_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH)
    download_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH)


def download_support_files() -> None:
    download_json_key_to_file(CVID_MAP_KEY, COMBINED_CVID_MAP_PATH, {}, upload_backup_if_missing=True)


def build_missevan_index(store: dict) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for _series_title, _season_key, node in iter_missevan_nodes(store):
        drama_id = normalize(node.get("dramaId"))
        if drama_id and drama_id not in indexed:
            indexed[drama_id] = node
    return indexed


def build_manbo_index(store: dict) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for record in store.get("records") or []:
        drama_id = normalize(record.get("dramaId"))
        if drama_id and drama_id not in indexed:
            indexed[drama_id] = record
    return indexed


def merge_missevan_info_for_ids(remote_store: dict, local_store: dict, drama_ids: list[str]) -> dict:
    merged = dict(remote_store)
    local_index = build_missevan_index(local_store)
    for drama_id in normalize_ids(drama_ids):
        record = local_index.get(drama_id)
        if record is None:
            print(f"[warn] no local 猫耳 record to upload for dramaId={drama_id}")
            continue
        merged[drama_id] = record
    return merged


def merge_manbo_info_for_ids(remote_store: dict, local_store: dict, drama_ids: list[str]) -> dict:
    merged = dict(remote_store)
    records = list(remote_store.get("records") or [])
    local_index = build_manbo_index(local_store)
    position_by_id = {
        normalize(record.get("dramaId")): idx
        for idx, record in enumerate(records)
        if isinstance(record, dict) and normalize(record.get("dramaId"))
    }
    for drama_id in normalize_ids(drama_ids):
        record = local_index.get(drama_id)
        if record is None:
            print(f"[warn] no local 漫播 record to upload for dramaId={drama_id}")
            continue
        idx = position_by_id.get(drama_id)
        if idx is None:
            position_by_id[drama_id] = len(records)
            records.append(record)
        else:
            records[idx] = record
    merged["records"] = records
    return merged


def merge_info_payload_for_ids(key: str, remote_store: object, local_store: object, drama_ids: list[str]) -> object:
    if key == MISSEVAN_INFO_KEY:
        if not isinstance(remote_store, dict) or not isinstance(local_store, dict):
            raise RuntimeError(f"{key} must be a JSON object.")
        return merge_missevan_info_for_ids(remote_store, local_store, drama_ids)
    if key == MANBO_INFO_KEY:
        if not isinstance(remote_store, dict) or not isinstance(local_store, dict):
            raise RuntimeError(f"{key} must be a JSON object.")
        return merge_manbo_info_for_ids(remote_store, local_store, drama_ids)
    raise RuntimeError(f"Unsupported info key for merge upload: {key}")


def merge_and_upload_info_file(key: str, path: Path, drama_ids: list[str]) -> None:
    latest_remote = decode_remote_info_payload(key, upstash_request(["GET", key]))
    assert_info_download_is_safe(key, latest_remote)
    local_payload = load_json(path, {})
    merged = merge_info_payload_for_ids(key, latest_remote, local_payload, drama_ids)
    value = write_info_payload(path, merged)
    assert_info_upload_is_safe(key, value, path)
    result = upstash_request(["SET", key, value])
    if result != "OK":
        raise RuntimeError(f"Failed to upload merged {path.name} to {key}: {result!r}")
    print(f"[ok] merged and uploaded {path.name} -> {key}")


def is_missevan_ready(record: dict | None) -> bool:
    if not record:
        return False
    if not normalize(record.get("title")):
        return False
    if record.get("type") in (None, ""):
        return False
    if record.get("catalog") in (None, ""):
        return False
    has_create_time = bool(normalize(record.get("createTime")))
    has_author = bool(normalize(record.get("author")))
    if not has_create_time and not has_author:
        return False
    if not normalize(record.get("cover")):
        return False
    if "is_member" not in record:
        return False
    return len(record.get("maincvs") or []) >= 2


def is_manbo_ready(record: dict | None) -> bool:
    if not record:
        return False
    if not normalize(record.get("name")):
        return False
    if record.get("catalog") in (None, ""):
        return False
    if not normalize(record.get("createTime")):
        return False
    if not normalize(record.get("genre")):
        return False
    if "vipFree" not in record:
        return False
    return len(record.get("mainCvNicknames") or []) >= 2


def prune_queue(queue: dict[str, list[str]]) -> dict[str, list[str]]:
    missevan_store = load_json(MISSEVAN_INFO_PATH, {})
    manbo_store = load_json(MANBO_INFO_PATH, {"records": []})
    missevan_index = build_missevan_index(missevan_store)
    manbo_index = build_manbo_index(manbo_store)
    remaining_missevan = [drama_id for drama_id in queue.get("missevan", []) if not is_missevan_ready(missevan_index.get(drama_id))]
    remaining_manbo = [drama_id for drama_id in queue.get("manbo", []) if not is_manbo_ready(manbo_index.get(drama_id))]
    return {"manbo": remaining_manbo, "missevan": remaining_missevan}


def save_queue(queue: dict[str, list[str]]) -> None:
    payload = json.dumps(
        {
            "manbo": normalize_ids(queue.get("manbo") or []),
            "missevan": normalize_ids(queue.get("missevan") or []),
        },
        ensure_ascii=False,
    )
    result = upstash_request(["SET", QUEUE_KEY, payload])
    if result != "OK":
        raise RuntimeError(f"Failed to update {QUEUE_KEY}: {result!r}")
    print(
        "[ok] updated queue:",
        json.dumps(
            {
                "manbo": len(queue.get("manbo") or []),
                "missevan": len(queue.get("missevan") or []),
            },
            ensure_ascii=False,
        ),
    )


def rank_backfill_platforms(missevan_ids: list[str], manbo_ids: list[str]) -> tuple[str, ...]:
    platforms: list[str] = []
    if missevan_ids:
        platforms.append("missevan")
    if manbo_ids:
        platforms.append("manbo")
    return tuple(platforms)


def backfill_rank_metadata(platforms: tuple[str, ...]) -> None:
    if not platforms:
        print("[skip] rank backfill: no platforms")
        return

    import fetch_rank_data as ranks

    print(f"=== Backfilling rank metadata ({', '.join(platforms)}) ===")
    store = ranks.load_initial_rank_store()
    store.setdefault("_meta", {})
    store.setdefault("missevan", {"ranks": {}, "dramas": {}})
    store.setdefault("manbo", {"ranks": {}, "dramas": {}})
    store["missevan"].setdefault("ranks", {})
    store["missevan"].setdefault("dramas", {})
    store["manbo"].setdefault("ranks", {})
    store["manbo"].setdefault("dramas", {})
    ranks.sanitize_rank_store(store)
    ranks.lookup_cvs(store)
    store["_meta"]["updated_at"] = ranks.now_iso()
    ranks.save_json(ranks.RANKS_PATH, store)
    ranks.upload_rank_outputs(store, platforms)
    print("[ok] backfilled rank metadata")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync queued new drama IDs into platform info stores")
    parser.add_argument(
        "--backfill-ranks",
        action="store_true",
        help="After syncing info stores, backfill rank metadata from the latest info stores",
    )
    parser.add_argument("--upload-cv-map", action="store_true", help=f"Upload local CV map to {CVID_MAP_KEY}")
    parser.add_argument(
        "--upload-series-info",
        action="store_true",
        help=f"Upload local drama series info to {SERIES_INFO_KEY}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args([] if argv is None else argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    if args.upload_cv_map:
        upload_json_file(CVID_MAP_KEY, COMBINED_CVID_MAP_PATH)
    if args.upload_series_info:
        upload_json_file(SERIES_INFO_KEY, SERIES_INFO_PATH)
    if args.upload_cv_map or args.upload_series_info:
        return 0
    queue = load_queue()
    manbo_ids = queue.get("manbo") or []
    missevan_ids = queue.get("missevan") or []
    print(f"[queue] manbo={len(manbo_ids)} missevan={len(missevan_ids)}")
    if not manbo_ids and not missevan_ids:
        print("No pending drama IDs in new:dramaIDs.")
        return 0

    download_info_files()
    download_support_files()

    run_script("append_manbo_ids.py", manbo_ids)
    run_script("append_missevan_ids.py", missevan_ids)

    merge_and_upload_info_file(MANBO_INFO_KEY, MANBO_INFO_PATH, manbo_ids)
    merge_and_upload_info_file(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH, missevan_ids)

    remaining_queue = prune_queue(queue)
    save_queue(remaining_queue)
    if args.backfill_ranks:
        backfill_rank_metadata(rank_backfill_platforms(missevan_ids, manbo_ids))
    print(
        "[done]",
        json.dumps(
            {
                "removed_manbo": len(manbo_ids) - len(remaining_queue["manbo"]),
                "removed_missevan": len(missevan_ids) - len(remaining_queue["missevan"]),
                "remaining_manbo": len(remaining_queue["manbo"]),
                "remaining_missevan": len(remaining_queue["missevan"]),
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
