from __future__ import annotations

import argparse
from pathlib import Path

from platform_sync import COMBINED_CVID_MAP_PATH
from sync_new_drama_ids import (
    CVID_MAP_KEY,
    MANBO_INFO_KEY,
    MANBO_INFO_PATH,
    MISSEVAN_INFO_KEY,
    MISSEVAN_INFO_PATH,
    ROOT,
    assert_info_download_is_safe,
    configure_stdio,
    decode_remote_info_payload,
    decode_remote_json_payload,
    load_env_file,
    upstash_request,
    write_json_work_copy,
)


def assert_cvid_map_download_is_safe(payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to download {CVID_MAP_KEY}: expected a JSON object.")
    if not payload:
        raise RuntimeError(f"Refusing to download {CVID_MAP_KEY}: payload is empty.")
    for item_key, item_value in payload.items():
        if not isinstance(item_key, str) or not isinstance(item_value, dict):
            raise RuntimeError(f"Refusing to download {CVID_MAP_KEY}: unexpected payload shape.")


def download_cvid_map_file(*, path: Path = COMBINED_CVID_MAP_PATH, upstash=upstash_request) -> None:
    write_payloads([fetch_cvid_map_payload(path=path, upstash=upstash)])


def fetch_info_payload(key: str, path: Path, *, upstash=upstash_request) -> tuple[Path, object]:
    payload = decode_remote_info_payload(key, upstash(["GET", key]))
    assert_info_download_is_safe(key, payload)
    return path, payload


def fetch_info_payloads(*, upstash=upstash_request) -> list[tuple[Path, object]]:
    return [
        fetch_info_payload(MANBO_INFO_KEY, MANBO_INFO_PATH, upstash=upstash),
        fetch_info_payload(MISSEVAN_INFO_KEY, MISSEVAN_INFO_PATH, upstash=upstash),
    ]


def fetch_cvid_map_payload(*, path: Path = COMBINED_CVID_MAP_PATH, upstash=upstash_request) -> tuple[Path, object]:
    payload = decode_remote_json_payload(CVID_MAP_KEY, upstash(["GET", CVID_MAP_KEY]))
    assert_cvid_map_download_is_safe(payload)
    return path, payload


def write_payloads(payloads: list[tuple[Path, object]]) -> None:
    for path, payload in payloads:
        backup_path = write_json_work_copy(path, payload)
        if backup_path is not None:
            print(f"[backup] {path.name} -> {backup_path}")
        print(f"[ok] downloaded remote data -> {path.name}")


def sync_remote_libraries(
    *,
    fetch_info_payloads_func=fetch_info_payloads,
    fetch_cvid_map_payload_func=fetch_cvid_map_payload,
    write_payloads_func=write_payloads,
) -> None:
    print("=== Downloading remote platform info stores ===")
    payloads = fetch_info_payloads_func()
    print("=== Downloading remote CV map ===")
    payloads.append(fetch_cvid_map_payload_func())
    write_payloads_func(payloads)
    print("[done] remote libraries downloaded")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download remote platform libraries")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    sync_remote_libraries()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
