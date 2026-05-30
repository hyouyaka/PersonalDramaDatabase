import unittest
from pathlib import Path


class DailyUpdateScriptTests(unittest.TestCase):
    def test_null_danmaku_repair_is_skipped_when_rank_fetch_fails(self):
        script = Path(__file__).with_name("run_daily_update.ps1").read_text(encoding="utf-8")

        self.assertIn(
            '$RankExitCode = Run-Step "fetch_rank_data.py" @("python", "-X", "utf8", "-u", "fetch_rank_data.py", "--force")',
            script,
        )
        self.assertIn("$ExitCodes += $RankExitCode", script)
        self.assertIn("if ([int]$RankExitCode -eq 0) {", script)
        self.assertIn(
            '$ExitCodes += Run-Step "fetch_rank_data.py --repair-null-danmaku" @("python", "-X", "utf8", "-u", "fetch_rank_data.py", "--repair-null-danmaku")',
            script,
        )
        self.assertIn("repair-null-danmaku skipped", script)
        self.assertNotIn(
            '$ExitCodes += Run-Step "fetch_rank_data.py" @("python", "-X", "utf8", "-u", "fetch_rank_data.py", "--force")',
            script,
        )


if __name__ == "__main__":
    unittest.main()
