import unittest

import rank_key_cleanup


class RankKeyCleanupTests(unittest.TestCase):
    def test_cleanup_normal_deletes_fixed_keys_and_at_most_twenty_dated_keys(self) -> None:
        commands: list[list[object]] = []
        dated_keys = [
            *(f"ranks:list:2026-05-{day:02d}:missevan" for day in range(1, 16)),
            *(f"ranks:metrics:2026-05-{day:02d}:missevan" for day in range(1, 16)),
        ]

        def upstash(command: list[object]) -> object:
            commands.append(command)
            if command[0] == "SCAN":
                pattern = str(command[3])
                matches = [key for key in dated_keys if key.startswith(pattern.removesuffix("*"))]
                return ["0", matches]
            if command[0] == "DEL":
                return len(command) - 1
            raise AssertionError(command)

        deleted = rank_key_cleanup.cleanup_legacy_normal_rank_keys(upstash)

        self.assertEqual(
            commands[0],
            ["DEL", "ranks:index", "ranks:partial:missevan", "ranks:partial:manbo"],
        )
        dated_deleted = [
            key
            for command in commands
            if command[0] == "DEL"
            for key in command[1:]
            if key.startswith(("ranks:list:", "ranks:metrics:"))
        ]
        self.assertEqual(len(dated_deleted), 20)
        self.assertEqual(deleted, dated_deleted)

    def test_cleanup_cv_deletes_only_strict_date_keys(self) -> None:
        commands: list[list[object]] = []
        candidates = [
            "ranks:cv:2026-06-10",
            "ranks:cv:latest",
            "ranks:cv:2026-6-10",
            "ranks:cv:2026-06-10:extra",
            "ranks:trend:cv:missevan",
        ]

        def upstash(command: list[object]) -> object:
            commands.append(command)
            if command[0] == "SCAN":
                return ["0", candidates]
            if command[0] == "DEL":
                return len(command) - 1
            raise AssertionError(command)

        deleted = rank_key_cleanup.cleanup_legacy_cv_rank_keys(upstash)

        self.assertEqual(deleted, ["ranks:cv:2026-06-10"])
        self.assertEqual(commands[-1], ["DEL", "ranks:cv:2026-06-10"])

    def test_best_effort_cleanup_warns_instead_of_raising(self) -> None:
        messages: list[str] = []

        def failing_cleanup() -> list[str]:
            raise RuntimeError("temporary cleanup failure")

        result = rank_key_cleanup.run_cleanup_best_effort(failing_cleanup, log=messages.append)

        self.assertEqual(result, [])
        self.assertEqual(messages, ["[upstash] WARN: legacy rank key cleanup failed: temporary cleanup failure"])


if __name__ == "__main__":
    unittest.main()
