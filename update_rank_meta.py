from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime

from sync_new_drama_ids import ROOT, configure_stdio, load_env_file, upstash_request


RANK_META_KEY = "ranks:meta"
SCOPES = ("normal", "cv")
UPDATE_RANK_META_SCRIPT = r'''
local raw = redis.call("GET", KEYS[1])
local meta = {}
if raw and raw ~= false and raw ~= "" then
  meta = cjson.decode(raw)
end

local function ensure_section(name)
  if type(meta[name]) ~= "table" then
    meta[name] = {updatedAt = cjson.null, publishedAt = cjson.null}
  end
end

ensure_section("normal")
ensure_section("cv")
meta[ARGV[1]] = {updatedAt = ARGV[2], publishedAt = ARGV[2]}

local encoded = cjson.encode(meta)
redis.call("SET", KEYS[1], encoded)
return encoded
'''


def current_local_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _decode_meta(raw: object) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, dict):
        data = raw
    else:
        raise RuntimeError(f"Unsupported payload type for {RANK_META_KEY}: {type(raw).__name__}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{RANK_META_KEY} must be a JSON object.")
    return data


def _normalize_timestamp(value: str | datetime) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.astimezone()
    return value.replace(microsecond=0).isoformat()


def update_rank_meta(
    scope: str,
    *,
    now: Callable[[], str | datetime] = current_local_iso,
    upstash: Callable[[list[object]], object] = upstash_request,
) -> dict:
    if scope not in SCOPES:
        raise ValueError(f"Unsupported rank meta scope: {scope}")

    timestamp = _normalize_timestamp(now())
    result = upstash(["EVAL", UPDATE_RANK_META_SCRIPT, 1, RANK_META_KEY, scope, timestamp])
    if result in (None, ""):
        raise RuntimeError(f"Failed to update {RANK_META_KEY}: {result!r}")
    meta = _decode_meta(result)
    print(f"[ok] updated {RANK_META_KEY}.{scope}: {timestamp}")
    return meta


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Upstash rank publish metadata")
    parser.add_argument("scope", choices=SCOPES, help="Rank metadata scope to update")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_stdio()
    load_env_file(ROOT / ".env")
    update_rank_meta(args.scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
