import unittest
from pathlib import Path


class WeeklyCvUpdateScriptTests(unittest.TestCase):
    def test_weekly_script_refreshes_counts_before_building_ranks(self) -> None:
        script = Path(__file__).with_name("run_weekly_cv_update.ps1").read_text(encoding="utf-8")

        self.assertIn("$Root = if ($env:DRAMA_DB_ROOT) { $env:DRAMA_DB_ROOT } else { $ScriptRoot }", script)
        self.assertIn("weekly-cv-update-$Timestamp.log", script)
        self.assertIn("weekly-cv-update-latest.log", script)
        self.assertIn(
            '$RefreshExitCode = Run-Step "refresh_watch_counts.py" @("python", "-X", "utf8", "-u", "refresh_watch_counts.py")',
            script,
        )
        self.assertIn("if ([int]$RefreshExitCode -eq 0) {", script)
        self.assertIn(
            '$BuildExitCode = Run-Step "build_cv_ranks.py" @("python", "-X", "utf8", "-u", "build_cv_ranks.py")',
            script,
        )
        self.assertIn("$ExitCodes += $BuildExitCode", script)
        self.assertIn("if ([int]$BuildExitCode -eq 0) {", script)
        self.assertIn(
            '$ExitCodes += Run-Step "update_rank_meta.py cv" @("python", "-X", "utf8", "-u", "update_rank_meta.py", "cv")',
            script,
        )
        self.assertIn("build_cv_ranks.py skipped", script)
        self.assertIn("update_rank_meta.py cv skipped: build_cv_ranks.py", script)
        self.assertIn("update_rank_meta.py cv skipped: refresh_watch_counts.py", script)


if __name__ == "__main__":
    unittest.main()
