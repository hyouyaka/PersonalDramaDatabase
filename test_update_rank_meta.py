import json
import re
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import Mock, patch

import update_rank_meta


class RankMetaTests(unittest.TestCase):
    def test_update_initializes_missing_meta_key(self) -> None:
        upstash = Mock(
            return_value=json.dumps(
                {
                    "normal": {
                        "updatedAt": "2026-06-12T11:30:00+00:00",
                        "publishedAt": "2026-06-12T11:30:00+00:00",
                    },
                    "cv": {"updatedAt": None, "publishedAt": None},
                },
                ensure_ascii=False,
            )
        )

        with patch("builtins.print"):
            result = update_rank_meta.update_rank_meta(
                "normal",
                now=lambda: datetime(2026, 6, 12, 11, 30, tzinfo=timezone.utc),
                upstash=upstash,
            )

        self.assertEqual(result["normal"]["updatedAt"], "2026-06-12T11:30:00+00:00")
        self.assertEqual(result["normal"]["publishedAt"], "2026-06-12T11:30:00+00:00")
        self.assertEqual(result["cv"], {"updatedAt": None, "publishedAt": None})
        upstash.assert_called_once()
        command = upstash.call_args.args[0]
        self.assertEqual(command[0], "EVAL")
        self.assertIn('redis.call("GET", KEYS[1])', command[1])
        self.assertIn('redis.call("SET", KEYS[1], encoded)', command[1])
        self.assertEqual(command[2:], [1, "ranks:meta", "normal", "2026-06-12T11:30:00+00:00"])

    def test_update_preserves_other_scope(self) -> None:
        updated = {
            "normal": {
                "updatedAt": "2026-06-11T10:00:00+08:00",
                "publishedAt": "2026-06-11T10:00:10+08:00",
            },
            "cv": {
                "updatedAt": "2026-06-12T12:04:24+00:00",
                "publishedAt": "2026-06-12T12:04:24+00:00",
            },
        }
        upstash = Mock(return_value=json.dumps(updated, ensure_ascii=False))

        with patch("builtins.print"):
            result = update_rank_meta.update_rank_meta(
                "cv",
                now=lambda: datetime(2026, 6, 12, 12, 4, 24, tzinfo=timezone.utc),
                upstash=upstash,
            )

        self.assertEqual(result["normal"], updated["normal"])
        self.assertEqual(result["cv"]["updatedAt"], "2026-06-12T12:04:24+00:00")
        self.assertEqual(result["cv"]["publishedAt"], "2026-06-12T12:04:24+00:00")

    def test_current_timestamp_has_utc_offset(self) -> None:
        timestamp = update_rank_meta.current_local_iso()

        self.assertRegex(timestamp, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertNotRegex(timestamp, re.escape("+00:00Z"))

    def test_set_failure_raises(self) -> None:
        upstash = Mock(return_value=None)

        with self.assertRaisesRegex(RuntimeError, "Failed to update ranks:meta"):
            update_rank_meta.update_rank_meta(
                "normal",
                now=lambda: datetime(2026, 6, 12, 11, 30, tzinfo=timezone.utc),
                upstash=upstash,
            )

    def test_main_rejects_unsupported_scope(self) -> None:
        with patch.object(update_rank_meta, "upstash_request", side_effect=AssertionError("should not call")):
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    update_rank_meta.parse_args(["bad-scope"])

        self.assertNotEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
