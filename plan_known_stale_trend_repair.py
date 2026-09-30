"""Prepare a review-only patch for two confirmed stale Manbo trend ranges.

This utility reads an exported trend payload and writes a separate candidate
file. It never connects to Upstash or modifies its input.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


KNOWN_STALE_RANGES = (
    {
        "drama_id": "2149856931232088128",
        "start_date": "2026-08-17",
        "end_date": "2026-09-25",
        "expected_metrics": {"view_count": 855745, "danmaku_uid_count": 185, "pay_count": 2659},
    },
    {
        "drama_id": "2230801998016413748",
        "start_date": "2026-09-22",
        "end_date": "2026-09-25",
        "expected_metrics": {"view_count": 67348, "danmaku_uid_count": 107, "pay_count": 0},
    },
)


def _decode_record(value: Any) -> tuple[dict | None, str]:
    if isinstance(value, dict):
        return value, "object"
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None, "invalid-json"
        return (decoded, "json-string") if isinstance(decoded, dict) else (None, "invalid-shape")
    return None, "invalid-shape"


def _record_mapping(payload: dict) -> dict:
    dramas = payload.get("dramas")
    if isinstance(dramas, dict):
        return dramas
    return payload


def _iter_target_dates(start_date: str, end_date: str):
    from datetime import date, timedelta

    current = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def build_known_stale_repair_candidate(payload: dict) -> tuple[dict, dict]:
    """Return a candidate payload and exact-match audit report; never writes data."""
    candidate = copy.deepcopy(payload)
    records = _record_mapping(candidate)
    report = {"changed": [], "already_null": [], "missing": [], "value_mismatch": [], "invalid": []}

    for target in KNOWN_STALE_RANGES:
        drama_id = target["drama_id"]
        if drama_id not in records:
            report["missing"].extend(
                {"drama_id": drama_id, "date": date}
                for date in _iter_target_dates(target["start_date"], target["end_date"])
            )
            continue

        raw_record = records[drama_id]
        record, encoding = _decode_record(raw_record)
        if not isinstance(record, dict):
            report["invalid"].append({"drama_id": drama_id, "encoding": encoding})
            continue

        original_encoded = raw_record if encoding == "json-string" else None
        samples = record.get("samples")
        for date in _iter_target_dates(target["start_date"], target["end_date"]):
            sample = samples.get(date) if isinstance(samples, dict) else None
            metrics = sample.get("metrics") if isinstance(sample, dict) else None
            if not isinstance(metrics, dict) or any(key not in metrics for key in target["expected_metrics"]):
                report["missing"].append({"drama_id": drama_id, "date": date})
                continue
            expected = target["expected_metrics"]
            values = {key: metrics[key] for key in expected}
            if all(value is None for value in values.values()):
                report["already_null"].append({"drama_id": drama_id, "date": date})
            elif values == expected:
                metrics.update({key: None for key in expected})
                report["changed"].append(
                    {
                        "drama_id": drama_id,
                        "date": date,
                        "before": values,
                        "after": {key: None for key in expected},
                    }
                )
            else:
                report["value_mismatch"].append(
                    {
                        "drama_id": drama_id,
                        "date": date,
                        "expected": expected,
                        "actual": values,
                    }
                )

        if original_encoded is not None:
            records[drama_id] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        else:
            records[drama_id] = record

    return candidate, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON export of ranks:trend:manbo:v2 or legacy trend payload")
    parser.add_argument("--output", type=Path, help="Write a separate review candidate; existing files are refused")
    args = parser.parse_args()

    input_path = args.input.resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Input must be a JSON object")
    candidate, report = build_known_stale_repair_candidate(payload)

    if args.output:
        output_path = args.output.resolve()
        if output_path == input_path:
            raise SystemExit("Output must be a separate file; the input is never overwritten")
        if not output_path.parent.exists():
            raise SystemExit(f"Output directory does not exist: {output_path.parent}")
        with output_path.open("x", encoding="utf-8") as output_file:
            json.dump(candidate, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
        report["candidate_path"] = str(output_path)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
