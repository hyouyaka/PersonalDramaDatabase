from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal


ROOT = Path(__file__).resolve().parent
BACKUP_ROOT = ROOT / "recovery_backups" / "upstash_editor"
CURRENT_MIRROR_ROOT = BACKUP_ROOT / "current"
RANK_META_KEY = "ranks:meta"
STAGING_TTL_SECONDS = 24 * 60 * 60
HASH_WRITE_CHUNK_SIZE = 100

INFO_META_KEYS = {
    "missevan:info:v2": "missevan:info:meta:v2",
    "manbo:info:v2": "manbo:info:meta:v2",
}

STRING_SAVE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
local current_meta = redis.call('GET', KEYS[2])
if ARGV[4] == '__missing__' then
  if current_meta and current_meta ~= false then
    return 0
  end
elseif not current_meta or redis.sha1hex(current_meta) ~= ARGV[4] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
return 1
"""

INFO_STRING_SAVE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
local current_meta = redis.call('GET', KEYS[2])
if ARGV[4] == '__missing__' then
  if current_meta and current_meta ~= false then
    return 0
  end
elseif not current_meta or redis.sha1hex(current_meta) ~= ARGV[4] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
if redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('SET', KEYS[3], ARGV[2])
end
return 1
"""

HASH_SAVE_SCRIPT = """
local current_meta = redis.call('HGET', KEYS[1], '__meta__')
if not current_meta or current_meta ~= ARGV[1] then
  return 0
end
local rank_meta = redis.call('GET', KEYS[3])
if ARGV[3] == '__missing__' then
  if rank_meta and rank_meta ~= false then
    return 0
  end
elseif not rank_meta or redis.sha1hex(rank_meta) ~= ARGV[3] then
  return 0
end
redis.call('RENAME', KEYS[2], KEYS[1])
redis.call('PERSIST', KEYS[1])
redis.call('SET', KEYS[3], ARGV[2])
return 1
"""


@dataclass(frozen=True)
class ResourceSpec:
    key: str
    label: str
    redis_type: Literal["string", "hash"]
    kind: str
    rank_scope: Literal["normal", "cv"] | None = None
    local_path: Path | None = None


@dataclass
class LoadedResource:
    spec: ResourceSpec
    payload: object
    hash_meta: dict | None
    raw_string: str | None
    raw_fields: dict[str, str] | None
    content_sha1: str
    byte_count: int
    backup_path: Path
    meta_status: str = "未校验"
    updated_at: str | None = None


@dataclass
class SaveResult:
    content_sha1: str
    byte_count: int
    updated_at: str
    local_error: str | None = None


@dataclass
class CollectionRef:
    name: str
    container: dict | list
    identity_field: str | None
    platform: str | None = None


RESOURCE_SPECS: dict[str, ResourceSpec] = {
    spec.key: spec
    for spec in (
        ResourceSpec(
            "missevan:info:v2",
            "猫耳 Info",
            "string",
            "info_missevan",
            local_path=ROOT / "missevan-drama-info.json",
        ),
        ResourceSpec(
            "manbo:info:v2",
            "漫播 Info",
            "string",
            "info_manbo",
            local_path=ROOT / "manbo-drama-info.json",
        ),
        ResourceSpec(
            "ranks:latest",
            "最新普通榜",
            "string",
            "ranks_latest",
            rank_scope="normal",
            local_path=ROOT / "ranks.json",
        ),
        ResourceSpec(
            "ranks:cv:latest",
            "最新 CV 榜",
            "string",
            "ranks_cv_latest",
            rank_scope="cv",
            local_path=ROOT / "ranks-cv.json",
        ),
        ResourceSpec(
            "ranks:trend:missevan:v2",
            "猫耳趋势榜",
            "hash",
            "trend_normal",
            rank_scope="normal",
        ),
        ResourceSpec(
            "ranks:trend:manbo:v2",
            "漫播趋势榜",
            "hash",
            "trend_normal",
            rank_scope="normal",
        ),
        ResourceSpec(
            "ranks:trend:peak:missevan:v2",
            "猫耳巅峰趋势榜",
            "hash",
            "trend_peak",
            rank_scope="normal",
        ),
        ResourceSpec(
            "ranks:trend:cv:v2",
            "合并 CV 趋势榜",
            "hash",
            "trend_cv",
            rank_scope="cv",
        ),
    )
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def string_cas_token(raw: object) -> str:
    return sha1_text(raw) if isinstance(raw, str) else "__missing__"


def find_list_item_by_identity(items: list, identity_field: str, identity: object) -> object:
    expected = str(identity or "")
    for item in items:
        if isinstance(item, dict) and str(item.get(identity_field) or "") == expected:
            return item
    raise KeyError(expected)


def safe_key_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-") or "upstash-key"


def decode_hgetall(raw: object) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if not isinstance(raw, list) or len(raw) % 2:
        raise RuntimeError(f"Invalid HGETALL response: {type(raw).__name__}")
    return {str(raw[index]): str(raw[index + 1]) for index in range(0, len(raw), 2)}


def hash_digest_input(raw_fields: dict[str, str], *, include_meta: bool = False) -> str:
    pairs = [
        [field, value]
        for field, value in sorted(raw_fields.items())
        if include_meta or field != "__meta__"
    ]
    return compact_json(pairs)


def hash_content_stats(raw_fields: dict[str, str]) -> tuple[str, int]:
    encoded = hash_digest_input(raw_fields)
    return sha1_text(encoded), len(encoded.encode("utf-8"))


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(value, encoding="utf-8")
    os.replace(temp_path, path)


def _write_backup(
    spec: ResourceSpec,
    *,
    raw_string: str | None,
    raw_fields: dict[str, str] | None,
    backup_root: Path,
) -> tuple[Path, str, int]:
    if spec.redis_type == "string":
        if raw_string is None:
            raise RuntimeError(f"{spec.key} is empty.")
        digest = sha1_text(raw_string)
        byte_count = len(raw_string.encode("utf-8"))
        backup_text = raw_string
    else:
        if not raw_fields or "__meta__" not in raw_fields:
            raise RuntimeError(f"{spec.key} is empty or missing __meta__.")
        digest, byte_count = hash_content_stats(raw_fields)
        backup_text = compact_json(
            {
                "redisType": "hash",
                "key": spec.key,
                "fields": [[field, value] for field, value in sorted(raw_fields.items())],
            }
        )
    backup_bytes = backup_text.encode("utf-8")
    backup_digest = hashlib.sha256(backup_bytes).digest()
    key_name = safe_key_name(spec.key)
    candidates = sorted(
        (
            candidate
            for candidate in backup_root.glob(f"*__{key_name}__*.json")
            if candidate.is_file()
        ),
        key=lambda candidate: candidate.stat().st_mtime_ns,
        reverse=True,
    )
    for candidate in candidates:
        if hashlib.sha256(candidate.read_bytes()).digest() == backup_digest:
            return candidate, digest, byte_count

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = backup_root / f"{stamp}__{key_name}__{digest}.json"
    _write_text_atomic(path, backup_text)
    return path, digest, byte_count


def _parse_string_payload(spec: ResourceSpec, raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{spec.key} is not valid JSON: {exc}") from exc


def _parse_hash_payload(spec: ResourceSpec, fields: dict[str, str]) -> tuple[dict[str, object], dict]:
    try:
        meta = json.loads(fields["__meta__"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key} has invalid __meta__.") from exc
    if not isinstance(meta, dict):
        raise RuntimeError(f"{spec.key}.__meta__ must be a JSON object.")
    payload: dict[str, object] = {}
    for field, raw_value in fields.items():
        if field == "__meta__":
            continue
        try:
            payload[field] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{spec.key}[{field!r}] is not valid JSON: {exc}") from exc
    return payload, meta


def _loaded_meta_status(
    spec: ResourceSpec,
    *,
    digest: str,
    byte_count: int,
    hash_meta: dict | None,
    upstash: Callable[[list[object]], object],
) -> tuple[str, str | None]:
    try:
        if spec.redis_type == "hash":
            meta = hash_meta or {}
            updated_at = str(meta.get("updated_at") or "") or None
            if (
                meta.get("contentSha1") != digest
                or int(meta.get("bytes") or -1) != byte_count
            ):
                return "Hash __meta__ 摘要不匹配", updated_at
        elif spec.key in INFO_META_KEYS:
            meta = _decode_json_object(upstash(["GET", INFO_META_KEYS[spec.key]]))
            updated_at = str(meta.get("updatedAt") or "") or None
            if (
                meta.get("dataKey") != spec.key
                or meta.get("contentSha1") != digest
                or int(meta.get("bytes") or -1) != byte_count
            ):
                return "Info Meta 摘要不匹配", updated_at
            return "有效", updated_at
        else:
            updated_at = None

        rank_meta = _decode_json_object(upstash(["GET", RANK_META_KEY]))
        section = rank_meta.get(str(spec.rank_scope))
        resources = section.get("resources") if isinstance(section, dict) else None
        resource = resources.get(spec.key) if isinstance(resources, dict) else None
        if (
            not isinstance(resource, dict)
            or resource.get("contentSha1") != digest
            or int(resource.get("bytes") or -1) != byte_count
        ):
            return "ranks:meta 摘要不匹配", updated_at
        return "有效", updated_at or str(resource.get("updatedAt") or "") or None
    except Exception as exc:
        return f"校验失败：{exc}", None


def load_resource(
    key: str,
    *,
    upstash: Callable[[list[object]], object],
    backup_root: Path = BACKUP_ROOT,
) -> LoadedResource:
    try:
        spec = RESOURCE_SPECS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported Upstash editor key: {key}") from exc
    if spec.redis_type == "string":
        raw = upstash(["GET", key])
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"{key} is empty or unsupported.")
        backup_path, digest, byte_count = _write_backup(
            spec,
            raw_string=raw,
            raw_fields=None,
            backup_root=backup_root,
        )
        payload = _parse_string_payload(spec, raw)
        validate_payload(spec, payload)
        meta_status, updated_at = _loaded_meta_status(
            spec,
            digest=digest,
            byte_count=byte_count,
            hash_meta=None,
            upstash=upstash,
        )
        return LoadedResource(
            spec,
            payload,
            None,
            raw,
            None,
            digest,
            byte_count,
            backup_path,
            meta_status,
            updated_at,
        )

    fields = decode_hgetall(upstash(["HGETALL", key]))
    backup_path, digest, byte_count = _write_backup(
        spec,
        raw_string=None,
        raw_fields=fields,
        backup_root=backup_root,
    )
    payload, meta = _parse_hash_payload(spec, fields)
    validate_payload(spec, payload, hash_meta=meta)
    meta_status, updated_at = _loaded_meta_status(
        spec,
        digest=digest,
        byte_count=byte_count,
        hash_meta=meta,
        upstash=upstash,
    )
    return LoadedResource(
        spec,
        payload,
        meta,
        None,
        fields,
        digest,
        byte_count,
        backup_path,
        meta_status,
        updated_at,
    )


def _require_dict(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _validate_numeric_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"{label} must contain ASCII digits.")
    return text


def validate_payload(spec: ResourceSpec, payload: object, *, hash_meta: dict | None = None) -> None:
    if spec.kind == "info_missevan":
        root = _require_dict(payload, spec.key)
        if len(root) < 100:
            raise ValueError(f"{spec.key} contains fewer than 100 records.")
        for field, record in root.items():
            _validate_numeric_id(field, f"{spec.key} record key")
            item = _require_dict(record, f"{spec.key}[{field}]")
            if _validate_numeric_id(item.get("dramaId"), f"{spec.key}[{field}].dramaId") != field:
                raise ValueError(f"{spec.key}[{field}].dramaId does not match its record key.")
        return
    if spec.kind == "info_manbo":
        root = _require_dict(payload, spec.key)
        records = root.get("records")
        if not isinstance(records, list) or len(records) < 50:
            raise ValueError(f"{spec.key}.records must contain at least 50 records.")
        seen: set[str] = set()
        for index, record in enumerate(records):
            item = _require_dict(record, f"{spec.key}.records[{index}]")
            drama_id = _validate_numeric_id(item.get("dramaId"), f"{spec.key}.records[{index}].dramaId")
            if drama_id in seen:
                raise ValueError(f"Duplicate dramaId in {spec.key}: {drama_id}")
            seen.add(drama_id)
        return
    if spec.kind == "ranks_latest":
        root = _require_dict(payload, spec.key)
        for platform in ("missevan", "manbo"):
            platform_payload = _require_dict(root.get(platform), f"{spec.key}.{platform}")
            _require_dict(platform_payload.get("ranks"), f"{spec.key}.{platform}.ranks")
            _require_dict(platform_payload.get("dramas"), f"{spec.key}.{platform}.dramas")
        return
    if spec.kind == "ranks_cv_latest":
        root = _require_dict(payload, spec.key)
        for group in ("rankings", "paidRankings"):
            group_payload = _require_dict(root.get(group), f"{spec.key}.{group}")
            for platform in ("missevan", "manbo"):
                records = group_payload.get(platform)
                if not isinstance(records, list):
                    raise ValueError(f"{spec.key}.{group}.{platform} must be a JSON array.")
                names: set[str] = set()
                for item in records:
                    record = _require_dict(item, f"{spec.key}.{group}.{platform} item")
                    name = str(record.get("cvName") or "").strip()
                    if not name or name in names:
                        raise ValueError(f"Missing or duplicate cvName in {spec.key}.{group}.{platform}: {name!r}")
                    names.add(name)
        return

    fields = _require_dict(payload, spec.key)
    for field, value in fields.items():
        item = _require_dict(value, f"{spec.key}[{field}]")
        if spec.kind == "trend_normal":
            if str(item.get("id") or "") != str(field):
                raise ValueError(f"{spec.key}[{field}].id does not match its Hash field.")
        elif spec.kind == "trend_peak":
            if str(item.get("name") or "") != str(field):
                raise ValueError(f"{spec.key}[{field}].name does not match its Hash field.")
        elif spec.kind == "trend_cv":
            platform, separator, cv_name = str(field).partition(":")
            if separator != ":" or platform not in ("missevan", "manbo"):
                raise ValueError(f"Invalid CV trend field: {field}")
            if str(item.get("cvName") or "").strip() != cv_name:
                raise ValueError(f"{spec.key}[{field}].cvName does not match its Hash field.")
        if not isinstance(item.get("samples"), dict):
            raise ValueError(f"{spec.key}[{field}].samples must be a JSON object.")
    if hash_meta is not None and not isinstance(hash_meta, dict):
        raise ValueError(f"{spec.key}.__meta__ must be a JSON object.")


def collection_refs(spec: ResourceSpec, payload: object) -> list[CollectionRef]:
    if spec.kind == "info_missevan":
        return [CollectionRef("剧目", _require_dict(payload, spec.key), "dramaId")]
    if spec.kind == "info_manbo":
        root = _require_dict(payload, spec.key)
        return [CollectionRef("剧目", root["records"], "dramaId")]
    if spec.kind == "ranks_latest":
        root = _require_dict(payload, spec.key)
        result: list[CollectionRef] = []
        for platform in ("missevan", "manbo"):
            result.append(CollectionRef(f"{platform} / 榜单", root[platform]["ranks"], None, platform))
            result.append(CollectionRef(f"{platform} / 剧目", root[platform]["dramas"], None, platform))
        return result
    if spec.kind == "ranks_cv_latest":
        root = _require_dict(payload, spec.key)
        return [
            CollectionRef(f"{group} / {platform}", root[group][platform], "cvName", platform)
            for group in ("rankings", "paidRankings")
            for platform in ("missevan", "manbo")
        ]
    return [CollectionRef("趋势实体", _require_dict(payload, spec.key), None)]


def _normalize_string_payload(spec: ResourceSpec, payload: object, now: str) -> tuple[object, str]:
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    root = _require_dict(normalized, spec.key)
    if spec.kind == "info_manbo":
        root["updatedAt"] = now
    elif spec.kind == "ranks_latest":
        meta = root.setdefault("_meta", {})
        if not isinstance(meta, dict):
            meta = {}
            root["_meta"] = meta
        meta["updated_at"] = now
    elif spec.kind == "ranks_cv_latest":
        root["generated_at"] = now
        for group in ("rankings", "paidRankings"):
            for platform in ("missevan", "manbo"):
                for rank, item in enumerate(root[group][platform], 1):
                    item["rank"] = rank
    validate_payload(spec, root)
    return root, compact_json(root)


def _normalize_hash_payload(
    spec: ResourceSpec,
    payload: object,
    previous_meta: dict,
    now: str,
) -> tuple[dict[str, object], dict, dict[str, str], str, int]:
    fields = _require_dict(json.loads(json.dumps(payload, ensure_ascii=False)), spec.key)
    validate_payload(spec, fields, hash_meta=previous_meta)
    encoded_fields = {str(field): compact_json(value) for field, value in sorted(fields.items())}
    digest, byte_count = hash_content_stats(encoded_fields)
    meta = dict(previous_meta)
    meta["version"] = 2
    meta["updated_at"] = now
    meta["revision"] = uuid.uuid4().hex
    meta["contentSha1"] = digest
    meta["entityCount"] = len(fields)
    meta["bytes"] = byte_count
    if spec.kind in ("trend_normal", "trend_peak"):
        dates = sorted(
            {
                str(date)
                for item in fields.values()
                for date in (_require_dict(item, "trend item").get("samples") or {})
            }
        )
        retention = int(meta.get("retentionDates") or 45)
        meta["dates"] = dates[-retention:]
    elif spec.kind == "trend_cv":
        platforms_meta = meta.get("platforms")
        if not isinstance(platforms_meta, dict):
            platforms_meta = {}
            meta["platforms"] = platforms_meta
        for platform in ("missevan", "manbo"):
            platform_fields = {
                field: item for field, item in fields.items() if str(field).startswith(f"{platform}:")
            }
            dates = sorted(
                {
                    str(date)
                    for item in platform_fields.values()
                    for date in (_require_dict(item, "CV trend item").get("samples") or {})
                }
            )
            platform_meta = platforms_meta.get(platform)
            if not isinstance(platform_meta, dict):
                platform_meta = {}
                platforms_meta[platform] = platform_meta
            retention = int(platform_meta.get("retentionDates") or 50)
            platform_meta.update(
                {
                    "updated_at": now,
                    "dates": dates[-retention:],
                    "entityCount": len(platform_fields),
                    "retentionDates": retention,
                }
            )
    raw_fields = {"__meta__": compact_json(meta), **encoded_fields}
    return fields, meta, raw_fields, digest, byte_count


def _decode_json_object(raw: object) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    raise RuntimeError("Expected a JSON object.")


def build_rank_meta_update(
    current: object,
    *,
    scope: str,
    key: str,
    data_type: str,
    content_sha1: str,
    byte_count: int,
    updated_at: str,
) -> dict:
    if scope not in ("normal", "cv"):
        raise ValueError(f"Unsupported rank meta scope: {scope}")
    meta = _decode_json_object(current)
    for name in ("normal", "cv"):
        section = meta.get(name)
        if not isinstance(section, dict):
            section = {"updatedAt": None, "publishedAt": None, "resources": {}}
            meta[name] = section
        if not isinstance(section.get("resources"), dict):
            section["resources"] = {}
    section = meta[scope]
    section["updatedAt"] = updated_at
    section["publishedAt"] = updated_at
    section["resources"][key] = {
        "dataType": data_type,
        "contentSha1": content_sha1,
        "bytes": byte_count,
        "updatedAt": updated_at,
    }
    return meta


def build_info_meta(spec: ResourceSpec, encoded: str, payload: object, now: str) -> dict:
    if spec.kind == "info_missevan":
        count = len(_require_dict(payload, spec.key))
    else:
        count = len(_require_dict(payload, spec.key).get("records") or [])
    return {
        "schemaVersion": 2,
        "dataKey": spec.key,
        "contentSha1": sha1_text(encoded),
        "updatedAt": now,
        "recordCount": count,
        "bytes": len(encoded.encode("utf-8")),
    }


def _verify_info_meta(raw: object, *, key: str, digest: str, byte_count: int, updated_at: str) -> None:
    meta = _decode_json_object(raw)
    if (
        meta.get("dataKey") != key
        or meta.get("contentSha1") != digest
        or int(meta.get("bytes") or -1) != byte_count
        or meta.get("updatedAt") != updated_at
    ):
        raise RuntimeError(f"Remote meta verification failed for {key}.")


def _verify_rank_meta(
    raw: object,
    *,
    scope: str,
    key: str,
    digest: str,
    byte_count: int,
    updated_at: str,
) -> None:
    meta = _decode_json_object(raw)
    section = meta.get(scope)
    resources = section.get("resources") if isinstance(section, dict) else None
    resource = resources.get(key) if isinstance(resources, dict) else None
    if (
        not isinstance(resource, dict)
        or resource.get("contentSha1") != digest
        or int(resource.get("bytes") or -1) != byte_count
        or resource.get("updatedAt") != updated_at
    ):
        raise RuntimeError(f"Remote ranks:meta verification failed for {key}.")


def _write_hash_staging(
    key: str,
    raw_fields: dict[str, str],
    *,
    upstash: Callable[[list[object]], object],
) -> str:
    staging_key = f"{key}:staging:{uuid.uuid4().hex}"
    items = sorted(raw_fields.items())
    try:
        for offset in range(0, len(items), HASH_WRITE_CHUNK_SIZE):
            chunk = items[offset : offset + HASH_WRITE_CHUNK_SIZE]
            args: list[object] = ["HSET", staging_key]
            for field, value in chunk:
                args.extend([field, value])
            result = upstash(args)
            if not isinstance(result, int) or isinstance(result, bool) or result != len(chunk):
                raise RuntimeError(f"Unexpected HSET result for {staging_key}: {result!r}")
            if offset == 0 and int(upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS]) or 0) != 1:
                raise RuntimeError(f"Failed to set staging TTL for {staging_key}.")
        if int(upstash(["HLEN", staging_key]) or 0) != len(items):
            raise RuntimeError(f"Hash verification failed for {staging_key}.")
        samples = ["__meta__"] + [field for field, _value in items if field != "__meta__"][:1]
        values = upstash(["HMGET", staging_key, *samples])
        if not isinstance(values, list) or len(values) != len(samples):
            raise RuntimeError(f"Unable to verify staging Hash {staging_key}.")
        for value in values:
            json.loads(value)
        return staging_key
    except Exception:
        try:
            upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS])
        except Exception:
            pass
        raise


def _mirror_text(spec: ResourceSpec, payload: object, hash_meta: dict | None) -> str:
    if spec.redis_type == "string":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(
        {"__meta__": hash_meta or {}, "entities": payload},
        ensure_ascii=False,
        indent=2,
    )


def _write_local_mirrors(spec: ResourceSpec, payload: object, hash_meta: dict | None) -> None:
    value = _mirror_text(spec, payload, hash_meta)
    if spec.local_path is not None:
        _write_text_atomic(spec.local_path, value)
    _write_text_atomic(CURRENT_MIRROR_ROOT / f"{safe_key_name(spec.key)}.json", value)


def save_resource(
    loaded: LoadedResource,
    payload: object,
    *,
    upstash: Callable[[list[object]], object],
    now: Callable[[], str] = utc_now_iso,
) -> SaveResult:
    timestamp = now()
    spec = loaded.spec
    if spec.redis_type == "string":
        if loaded.raw_string is None:
            raise RuntimeError("Loaded String resource is missing its original body.")
        normalized, encoded = _normalize_string_payload(spec, payload, timestamp)
        digest = sha1_text(encoded)
        byte_count = len(encoded.encode("utf-8"))
        if spec.key in INFO_META_KEYS:
            meta_key = INFO_META_KEYS[spec.key]
            meta = build_info_meta(spec, encoded, normalized, timestamp)
        else:
            meta_key = RANK_META_KEY
            meta = {}
        saved = False
        for _attempt in range(3):
            current_meta = upstash(["GET", meta_key])
            if spec.key not in INFO_META_KEYS:
                meta = build_rank_meta_update(
                    current_meta,
                    scope=str(spec.rank_scope),
                    key=spec.key,
                    data_type="string",
                    content_sha1=digest,
                    byte_count=byte_count,
                    updated_at=timestamp,
                )
            if spec.key in INFO_META_KEYS:
                legacy_key = spec.key.replace(":v2", ":v1")
                command: list[object] = [
                    "EVAL",
                    INFO_STRING_SAVE_SCRIPT,
                    3,
                    spec.key,
                    meta_key,
                    legacy_key,
                    sha1_text(loaded.raw_string),
                    encoded,
                    compact_json(meta),
                    string_cas_token(current_meta),
                ]
            else:
                command = [
                    "EVAL",
                    STRING_SAVE_SCRIPT,
                    2,
                    spec.key,
                    meta_key,
                    sha1_text(loaded.raw_string),
                    encoded,
                    compact_json(meta),
                    string_cas_token(current_meta),
                ]
            result = upstash(command)
            if int(result or 0) == 1:
                saved = True
                break
        if not saved:
            raise RuntimeError(f"Concurrent update detected for {spec.key}; reload before saving.")
        verified = upstash(["GET", spec.key])
        if verified != encoded:
            raise RuntimeError(f"Remote verification failed for {spec.key}.")
        verified_meta = upstash(["GET", meta_key])
        if spec.key in INFO_META_KEYS:
            _verify_info_meta(
                verified_meta,
                key=spec.key,
                digest=digest,
                byte_count=byte_count,
                updated_at=timestamp,
            )
        else:
            _verify_rank_meta(
                verified_meta,
                scope=str(spec.rank_scope),
                key=spec.key,
                digest=digest,
                byte_count=byte_count,
                updated_at=timestamp,
            )
        local_error = None
        try:
            _write_local_mirrors(spec, normalized, None)
        except Exception as exc:
            local_error = str(exc)
        return SaveResult(digest, byte_count, timestamp, local_error)

    if loaded.raw_fields is None or loaded.hash_meta is None:
        raise RuntimeError("Loaded Hash resource is missing its original fields.")
    normalized, meta, raw_fields, digest, byte_count = _normalize_hash_payload(
        spec,
        payload,
        loaded.hash_meta,
        timestamp,
    )
    staging_key = _write_hash_staging(spec.key, raw_fields, upstash=upstash)
    saved = False
    for _attempt in range(3):
        current_rank_meta = upstash(["GET", RANK_META_KEY])
        rank_meta = build_rank_meta_update(
            current_rank_meta,
            scope=str(spec.rank_scope),
            key=spec.key,
            data_type="hash",
            content_sha1=digest,
            byte_count=byte_count,
            updated_at=timestamp,
        )
        result = upstash(
            [
                "EVAL",
                HASH_SAVE_SCRIPT,
                3,
                spec.key,
                staging_key,
                RANK_META_KEY,
                loaded.raw_fields["__meta__"],
                compact_json(rank_meta),
                string_cas_token(current_rank_meta),
            ]
        )
        if int(result or 0) == 1:
            saved = True
            break
    if not saved:
        try:
            upstash(["EXPIRE", staging_key, STAGING_TTL_SECONDS])
        except Exception:
            pass
        raise RuntimeError(f"Concurrent update detected for {spec.key}; reload before saving.")
    verified_fields = decode_hgetall(upstash(["HGETALL", spec.key]))
    verified_digest, verified_bytes = hash_content_stats(verified_fields)
    if verified_digest != digest or verified_bytes != byte_count:
        raise RuntimeError(f"Remote verification failed for {spec.key}.")
    verified_hash_meta = _decode_json_object(verified_fields.get("__meta__"))
    if (
        verified_hash_meta.get("contentSha1") != digest
        or int(verified_hash_meta.get("bytes") or -1) != byte_count
        or verified_hash_meta.get("updated_at") != timestamp
    ):
        raise RuntimeError(f"Remote __meta__ verification failed for {spec.key}.")
    _verify_rank_meta(
        upstash(["GET", RANK_META_KEY]),
        scope=str(spec.rank_scope),
        key=spec.key,
        digest=digest,
        byte_count=byte_count,
        updated_at=timestamp,
    )
    local_error = None
    try:
        _write_local_mirrors(spec, normalized, meta)
    except Exception as exc:
        local_error = str(exc)
    return SaveResult(digest, byte_count, timestamp, local_error)
