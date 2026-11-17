"""Append-only episode diagnostics. Contains counts, never UID lists or comments."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


DETAIL_PREFIX = "danmaku-details-"


class EpisodeDetailLog:
    def __init__(self, directory: Path, date: str, run_id: str) -> None:
        datetime.strptime(date, "%Y-%m-%d")
        self.path = directory / f"{DETAIL_PREFIX}{date}.jsonl"
        self.run_id = run_id
        self.lock = threading.Lock()
        self.attempts: dict[tuple[str, str, str], int] = {}
        self.failed = False
        # The daily script and its repair subprocess share a run ID.
        if self.path.exists():
            with self.path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        if row.get("run_id") == run_id:
                            key = (row["platform"], row["drama_id"], row["episode_id"])
                            self.attempts[key] = max(self.attempts.get(key, 0), int(row["attempt"]))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue

    def record(self, platform: str, drama_id: str, episode_id: str, title: str,
               fetched_count: int | None, complete: bool) -> None:
        with self.lock:
            if self.failed:
                return
            key = (platform, str(drama_id), str(episode_id))
            attempt = self.attempts.get(key, 0) + 1
            row = {
                "time": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "platform": platform,
                "drama_id": key[1],
                "episode_id": key[2],
                "episode_title": str(title or ""),
                "attempt": attempt,
                "fetched_count": fetched_count,
                "complete": complete,
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a+b") as output:
                    output.seek(0, 2)
                    if output.tell():
                        output.seek(-1, 2)
                        if output.read(1) != b"\n":
                            output.write(b"\n")
                    output.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                self.attempts[key] = attempt
            except OSError as exc:
                self.failed = True
                print(f"[WARN] episode detail log disabled after write failure: {exc}")
