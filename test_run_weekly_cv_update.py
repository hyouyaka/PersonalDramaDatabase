import unittest
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class WeeklyCvUpdateScriptTests(unittest.TestCase):
    def execute_weekly(self, refresh_exit_code=0):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to execute the weekly wrapper")
        script = Path(__file__).with_name("run_weekly_cv_update.ps1").resolve()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "commands.jsonl"
            env = {
                **os.environ, "DRAMA_DB_ROOT": tmp,
                "WEEKLY_TEST_SCRIPT": str(script), "WEEKLY_TEST_COMMAND_LOG": str(log),
                "WEEKLY_TEST_REFRESH_EXIT_CODE": str(refresh_exit_code),
            }
            command = r'''
function global:python {
    $NativeArgs = @($args | ForEach-Object { $_ })
    $Line = ConvertTo-Json -InputObject $NativeArgs -Compress
    [System.IO.File]::AppendAllText($env:WEEKLY_TEST_COMMAND_LOG, $Line + [Environment]::NewLine)
    $global:LASTEXITCODE = if ($NativeArgs[3] -eq 'refresh_watch_counts.py') {
        [int]$env:WEEKLY_TEST_REFRESH_EXIT_CODE
    } else { 0 }
}
& $env:WEEKLY_TEST_SCRIPT
'''
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return result, calls

    def test_executed_weekly_script_only_dispatches_counts_and_rank_jobs(self) -> None:
        result, calls = self.execute_weekly()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([c[:3] for c in calls], [["-X", "utf8", "-u"]] * 5)
        self.assertEqual([c[3] for c in calls], [
            "refresh_watch_counts.py", "build_cv_ranks.py", "build_weekly_growth_ranks.py",
            "update_rank_meta.py", "update_rank_meta.py",
        ])
        self.assertEqual(calls[0][4:], ["--refresh-all"])
        self.assertEqual(calls[1][4:], [])
        self.assertEqual(calls[2][4], "--expected-end-date")
        self.assertRegex(calls[2][5], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(calls[3][4:], ["cv"])
        self.assertEqual(calls[4][4:], ["watchcountGrowth"])

    def test_failed_watchcount_refresh_skips_all_rank_jobs(self) -> None:
        result, calls = self.execute_weekly(refresh_exit_code=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(calls, [["-X", "utf8", "-u", "refresh_watch_counts.py", "--refresh-all"]])

    def test_weekly_script_refreshes_counts_before_building_ranks(self) -> None:
        script = Path(__file__).with_name("run_weekly_cv_update.ps1").read_text(encoding="utf-8")

        self.assertIn("$Root = if ($env:DRAMA_DB_ROOT) { $env:DRAMA_DB_ROOT } else { $ScriptRoot }", script)
        self.assertIn("weekly-cv-update-$Timestamp.log", script)
        self.assertIn("weekly-cv-update-latest.log", script)
        self.assertIn(
            '$RefreshExitCode = Run-Step "refresh_watch_counts.py --refresh-all" @("python", "-X", "utf8", "-u", "refresh_watch_counts.py", "--refresh-all")',
            script,
        )
        self.assertIn("if ([int]$RefreshExitCode -eq 0) {", script)
        self.assertIn(
            '$BuildExitCode = Run-Step "build_cv_ranks.py" @("python", "-X", "utf8", "-u", "build_cv_ranks.py")',
            script,
        )
        self.assertIn("$ExitCodes += $BuildExitCode", script)
        self.assertIn("if ([int]$BuildExitCode -eq 0) {", script)
        self.assertIn("$ExpectedHistoryDate = [DateTime]::UtcNow.ToString('yyyy-MM-dd')", script)
        self.assertIn(
            '$GrowthExitCode = Run-Step "build_weekly_growth_ranks.py" @("python", "-X", "utf8", "-u", "build_weekly_growth_ranks.py", "--expected-end-date", $ExpectedHistoryDate)',
            script,
        )
        self.assertIn("if ([int]$GrowthExitCode -eq 0) {", script)
        self.assertIn(
            '$ExitCodes += Run-Step "update_rank_meta.py cv" @("python", "-X", "utf8", "-u", "update_rank_meta.py", "cv")',
            script,
        )
        self.assertIn(
            '$ExitCodes += Run-Step "update_rank_meta.py watchcountGrowth" @("python", "-X", "utf8", "-u", "update_rank_meta.py", "watchcountGrowth")',
            script,
        )
        self.assertIn("build_cv_ranks.py skipped", script)
        self.assertIn("build_weekly_growth_ranks.py skipped: build_cv_ranks.py", script)
        self.assertIn("build_weekly_growth_ranks.py skipped: refresh_watch_counts.py", script)
        self.assertIn("update_rank_meta.py cv skipped: build_weekly_growth_ranks.py", script)
        self.assertIn("update_rank_meta.py cv skipped: build_cv_ranks.py", script)
        self.assertIn("update_rank_meta.py cv skipped: refresh_watch_counts.py", script)
        self.assertIn("update_rank_meta.py watchcountGrowth skipped: build_weekly_growth_ranks.py", script)
        self.assertIn("update_rank_meta.py watchcountGrowth skipped: build_cv_ranks.py", script)
        self.assertIn("update_rank_meta.py watchcountGrowth skipped: refresh_watch_counts.py", script)


if __name__ == "__main__":
    unittest.main()
