"""Persist Manbo lead-review IDs atomically with the corresponding info publication."""
from __future__ import annotations

from platform_sync import is_excluded_drama, is_numeric_drama_id

MANBO_CV_REVIEW_KEY = "manbo:cv:review:pending"

# Validate the Set type before writing anything: Redis Lua does not roll back
# earlier writes if a later command fails with WRONGTYPE.
MANBO_INFO_WITH_CV_REVIEW_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '__missing__' then
  if current and current ~= false then return 0 end
elseif not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
local decisions = cjson.decode(ARGV[4])
local queue_type = redis.call('TYPE', KEYS[3]).ok
if queue_type ~= 'none' and queue_type ~= 'set' then
  return redis.error_reply('CV review queue must be a Redis Set')
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
for drama_id, required in pairs(decisions) do
  if required then
    redis.call('SADD', KEYS[3], drama_id)
  else
    redis.call('SREM', KEYS[3], drama_id)
  end
end
return 1
"""


def manbo_cv_review_decisions(payload: dict) -> dict[str, bool]:
    """Only explicit decisions affect the queue; old/unobserved records stay untouched."""
    decisions = {}
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        drama_id = str(record.get("dramaId") or "").strip()
        required = record.get("mainCvReviewRequired")
        if is_numeric_drama_id(drama_id) and isinstance(required, bool):
            decisions[drama_id] = required and not is_excluded_drama("manbo", drama_id)
    return decisions
