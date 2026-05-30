$ErrorActionPreference = 'Continue'

$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = $Utf8NoBom
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$Root = 'C:\MMDatabaseUpdate\PersonalDramaDatabase'
$LogDir = Join-Path $Root 'logs'
$Timestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$LogPath = Join-Path $LogDir "daily-update-$Timestamp.log"
$LatestLogPath = Join-Path $LogDir 'daily-update-latest.log'

New-Item -ItemType Directory -Force $LogDir | Out-Null
Set-Location $Root

Set-Content -Path $LatestLogPath -Value "=== Start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding utf8

function Write-Log {
    param([string]$Text)
    $Text |
        Tee-Object -FilePath $LogPath -Append |
        Tee-Object -FilePath $LatestLogPath -Append |
        Out-Null
}

function Run-Step {
    param(
        [string]$Name,
        [string[]]$Command
    )

    Write-Log "=== $Name $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

    & $Command[0] @($Command[1..($Command.Length - 1)]) 2>&1 |
        Tee-Object -FilePath $LogPath -Append |
        Tee-Object -FilePath $LatestLogPath -Append |
        Out-Null

    $Code = $LASTEXITCODE
    Write-Log "=== $Name exit=$Code $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
    return $Code
}

$ExitCodes = @()
$ExitCodes += Run-Step "fetch_ongoing.py" @("python", "-X", "utf8", "-u", "fetch_ongoing.py")
$RankExitCode = Run-Step "fetch_rank_data.py" @("python", "-X", "utf8", "-u", "fetch_rank_data.py", "--force")
$ExitCodes += $RankExitCode
$ExitCodes += Run-Step "sync_new_drama_ids.py" @("python", "-X", "utf8", "-u", "sync_new_drama_ids.py", "--backfill-ranks")
if ([int]$RankExitCode -eq 0) {
    $ExitCodes += Run-Step "fetch_rank_data.py --repair-null-danmaku" @("python", "-X", "utf8", "-u", "fetch_rank_data.py", "--repair-null-danmaku")
} else {
    Write-Log "=== fetch_rank_data.py --repair-null-danmaku skipped: fetch_rank_data.py exit=$RankExitCode ==="
}

$FailedExitCodes = $ExitCodes | Where-Object { [int]$_ -ne 0 }
$FinalCode = if ($FailedExitCodes.Count -gt 0) { 1 } else { 0 }
Write-Log "=== Done final_exit=$FinalCode $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
exit $FinalCode
