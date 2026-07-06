from __future__ import annotations

import re
from collections.abc import Callable


LEGACY_RANK_DELETE_BATCH = 20
LEGACY_NORMAL_FIXED_KEYS = (
    "ranks:index",
    "ranks:partial:missevan",
    "ranks:partial:manbo",
)
NORMAL_DATE_KEY_PATTERN = re.compile(
    r"^ranks:(?:list|metrics):\d{4}-\d{2}-\d{2}:(?:missevan|manbo)$"
)
CV_DATE_KEY_PATTERN = re.compile(r"^ranks:cv:\d{4}-\d{2}-\d{2}$")


def _scan_page(upstash, cursor: str, pattern: str, count: int) -> tuple[str, list[str]]:
    raw = upstash(["SCAN", cursor, "MATCH", pattern, "COUNT", str(max(count, 1))])
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise RuntimeError(f"Unsupported SCAN response: {raw!r}")
    next_cursor, keys = raw
    if not isinstance(keys, (list, tuple)):
        raise RuntimeError(f"Unsupported SCAN keys response: {keys!r}")
    return str(next_cursor), [str(key) for key in keys]


def _collect_matching_keys(
    upstash,
    *,
    scan_patterns: tuple[str, ...],
    key_pattern: re.Pattern[str],
    limit: int,
) -> list[str]:
    matched: list[str] = []
    seen: set[str] = set()
    for scan_pattern in scan_patterns:
        cursor = "0"
        while len(matched) < limit:
            cursor, keys = _scan_page(upstash, cursor, scan_pattern, limit - len(matched))
            for key in keys:
                if key not in seen and key_pattern.fullmatch(key):
                    seen.add(key)
                    matched.append(key)
                    if len(matched) >= limit:
                        break
            if cursor == "0":
                break
        if len(matched) >= limit:
            break
    return matched


def _delete_keys(upstash, keys: list[str]) -> list[str]:
    if keys:
        upstash(["DEL", *keys])
    return keys


def cleanup_legacy_normal_rank_keys(upstash, *, limit: int = LEGACY_RANK_DELETE_BATCH) -> list[str]:
    upstash(["DEL", *LEGACY_NORMAL_FIXED_KEYS])
    keys = _collect_matching_keys(
        upstash,
        scan_patterns=("ranks:list:*", "ranks:metrics:*"),
        key_pattern=NORMAL_DATE_KEY_PATTERN,
        limit=max(limit, 0),
    )
    return _delete_keys(upstash, keys)


def cleanup_legacy_cv_rank_keys(upstash, *, limit: int = LEGACY_RANK_DELETE_BATCH) -> list[str]:
    keys = _collect_matching_keys(
        upstash,
        scan_patterns=("ranks:cv:*",),
        key_pattern=CV_DATE_KEY_PATTERN,
        limit=max(limit, 0),
    )
    return _delete_keys(upstash, keys)


def run_cleanup_best_effort(
    cleanup: Callable[[], list[str]],
    *,
    log: Callable[[str], None] = print,
) -> list[str]:
    try:
        return cleanup()
    except Exception as exc:
        log(f"[upstash] WARN: legacy rank key cleanup failed: {exc}")
        return []
