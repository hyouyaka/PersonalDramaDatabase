"""Archive seven retired Redis strings; generate a standalone local restore tool."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

KEYS = (
    "missevan:info:v1", "manbo:info:v1",
    "ranks:trend:missevan", "ranks:trend:manbo",
    "ranks:trend:cv:missevan", "ranks:trend:cv:manbo",
    "ranks:trend:peak:missevan",
)
RESTORE_NAME = "restore_retired_upstash.py"
READ_COMMANDS = {"GET", "TYPE", "TTL"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def endpoint_fingerprint(url: str) -> str:
    parts = urlsplit(url.strip().rstrip("/"))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("Expected an HTTPS Upstash REST endpoint without credentials or path")
    port = "" if parts.port in (None, 443) else f":{parts.port}"
    return digest(f"https://{parts.hostname.lower()}{port}".encode())


class Redis:
    def __init__(self, env_file: Path | None = None):
        config = {}
        if env_file is not None:
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip().strip("\"'")
        self.url = os.environ.get("UPSTASH_REDIS_REST_URL") or config.get("UPSTASH_REDIS_REST_URL", "")
        self.token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or config.get("UPSTASH_REDIS_REST_TOKEN", "")
        if not self.url or not self.token:
            raise ValueError("Missing Upstash REST configuration")
        self.fingerprint = endpoint_fingerprint(self.url)

    def command(self, args: list):
        if args[0] not in READ_COMMANDS | {"SET"} or args[1] not in KEYS:
            raise ValueError("Command outside retired-key allowlist")
        if args[0] == "SET" and ("NX" not in args[3:] or any(x not in ("NX", "EX") for x in args[3:] if isinstance(x, str))):
            raise ValueError("Restore requires SET NX")
        request = Request(self.url.rstrip("/"), data=json.dumps(args).encode(), headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError("Upstash command failed: " + str(payload["error"]))
        return payload["result"]


def snapshot(redis, key: str) -> tuple[bytes, int]:
    if redis.command(["TYPE", key]) != "string":
        raise ValueError(f"{key}: missing or not a String")
    raw = redis.command(["GET", key])
    ttl = redis.command(["TTL", key])
    if not isinstance(raw, str) or type(ttl) is not int or (ttl != -1 and ttl <= 0):
        raise ValueError(f"{key}: invalid value or TTL")
    json.loads(raw)
    return raw.encode("utf-8"), ttl


def ttl_matches(actual: int, saved: int) -> bool:
    return actual == -1 if saved == -1 else type(actual) is int and 0 < actual <= saved


def validate_archive(folder: Path) -> tuple[dict, list[tuple[dict, bytes]]]:
    folder = folder.resolve()
    if (folder / "manifest.json").is_symlink():
        raise ValueError("Manifest must be a local file")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1 or manifest.get("complete") is not True:
        raise ValueError("Archive is incomplete or unsupported")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("endpointFingerprint", "")):
        raise ValueError("Invalid endpoint fingerprint")
    entries = manifest.get("keys")
    if not isinstance(entries, list) or len(entries) != len(KEYS) or [entry.get("key") for entry in entries] != list(KEYS):
        raise ValueError("Manifest must contain exactly the seven retired keys in order")
    verified = []
    for index, entry in enumerate(entries):
        filename = f"{index + 1:02d}_{entry['key'].replace(':', '_')}.json"
        if entry.get("file") != filename:
            raise ValueError("Unexpected archive filename")
        path = folder / filename
        if path.is_symlink() or path.resolve().parent != folder:
            raise ValueError("Archive path escapes backup directory")
        data = path.read_bytes()
        if type(entry.get("bytes")) is not int or len(data) != entry["bytes"] or digest(data) != entry.get("sha256"):
            raise ValueError(f"{entry['key']}: archive integrity failed")
        json.loads(data.decode("utf-8"))
        ttl = entry.get("ttl")
        if type(ttl) is not int or (ttl != -1 and ttl <= 0):
            raise ValueError("Invalid archived TTL")
        verified.append((entry, data))
    return manifest, verified


def backup(redis, output_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = output_root / (stamp + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {"schemaVersion": 1, "complete": False, "createdAt": datetime.now(timezone.utc).isoformat(), "endpointFingerprint": redis.fingerprint, "keys": []}
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for index, key in enumerate(KEYS):
        data, ttl = snapshot(redis, key)
        filename = f"{index + 1:02d}_{key.replace(':', '_')}.json"
        path = folder / filename
        path.write_bytes(data)
        if path.read_bytes() != data:
            raise ValueError(f"{key}: local backup verification failed")
        manifest["keys"].append({"key": key, "file": filename, "bytes": len(data), "sha256": digest(data), "ttl": ttl})
    for entry in manifest["keys"]:
        current, ttl = snapshot(redis, entry["key"])
        if digest(current) != entry["sha256"] or not ttl_matches(ttl, entry["ttl"]):
            raise ValueError(f"{entry['key']}: changed during backup")
    (folder / RESTORE_NAME).write_bytes(Path(__file__).read_bytes())
    (folder / "USAGE.txt").write_text(
        "Run from this directory on your own PC. Python 3.10+; standard library only.\n"
        "python restore_retired_upstash.py --env-file <absolute-path-to-.env> --dry-run\n"
        "python restore_retired_upstash.py --env-file <absolute-path-to-.env> --apply\n"
        "Environment variables take precedence over .env. No existing data is overwritten.\n"
        "Positive TTLs restart from their archived remaining duration on restore.\n"
        "This restores retired data only; current programs continue using v2.\n", encoding="utf-8")
    manifest["complete"] = True
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    try:
        validate_archive(folder)
    except Exception:
        manifest["complete"] = False
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        raise
    return folder


def restore(redis, folder: Path, *, apply: bool = False, report=print) -> list[str]:
    manifest, verified = validate_archive(folder)
    if manifest["endpointFingerprint"] != redis.fingerprint:
        raise ValueError("Target endpoint does not match archive")
    # Preflight every key before the first write; SET NX also protects later races.
    for entry, data in verified:
        key = entry["key"]
        kind = redis.command(["TYPE", key])
        if kind != "none":
            actual, ttl = snapshot(redis, key)
            if actual != data or not ttl_matches(ttl, entry["ttl"]):
                raise ValueError(f"{key}: existing content or TTL conflicts; refusing overwrite")
    if not apply:
        for entry, _ in verified:
            report(f"[dry-run] {entry['key']}: validated; no writes")
        return []
    completed = []
    try:
        for entry, data in verified:
            key = entry["key"]
            if redis.command(["TYPE", key]) != "none":
                actual, ttl = snapshot(redis, key)
                if actual != data or not ttl_matches(ttl, entry["ttl"]):
                    raise ValueError(f"{key}: existing content or TTL conflicts; refusing overwrite")
                completed.append(key)
                report(f"[ok] {key}: identical; skipped")
                continue
            args = ["SET", key, data.decode("utf-8"), "NX"]
            if entry["ttl"] > 0:
                args += ["EX", entry["ttl"]]
            result = redis.command(args)
            if result not in (None, "OK"):
                raise RuntimeError(f"{key}: unexpected SET NX result")
            actual, ttl = snapshot(redis, key)
            if actual != data or not ttl_matches(ttl, entry["ttl"]):
                raise ValueError(f"{key}: post-restore verification failed")
            completed.append(key)
            report(f"[ok] {key}: {'restored' if result == 'OK' else 'identical; skipped'}")
    except Exception:
        report("[stopped] verified completed keys: " + json.dumps(completed))
        raise
    return completed


def main(argv=None) -> int:
    is_restore = Path(__file__).name == RESTORE_NAME
    parser = argparse.ArgumentParser(description="Restore retired keys without overwrite" if is_restore else __doc__)
    parser.add_argument("--env-file", type=Path, default=None if is_restore else Path(__file__).parent / ".env")
    if is_restore:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
    else:
        parser.add_argument("--output-root", type=Path, default=Path(__file__).parent / "recovery_backups" / "upstash-retired")
    args = parser.parse_args(argv)
    if is_restore and args.env_file is None:
        parser.error("restore requires --env-file")
    try:
        redis = Redis(args.env_file)
        if is_restore:
            restore(redis, Path(__file__).parent, apply=args.apply)
        else:
            folder = backup(redis, args.output_root)
            print(f"[ok] verified archive: {folder.resolve()}")
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
