$ErrorActionPreference = 'Continue'

$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = $Utf8NoBom
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = if ($env:DRAMA_DB_ROOT) { $env:DRAMA_DB_ROOT } else { $ScriptRoot }
$LogDir = Join-Path $Root 'logs'
$Timestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$LogPath = Join-Path $LogDir "weekly-cv-update-$Timestamp.log"
$LatestLogPath = Join-Path $LogDir 'weekly-cv-update-latest.log'

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
$RefreshExitCode = Run-Step "refresh_watch_counts.py" @("python", "-X", "utf8", "-u", "refresh_watch_counts.py")
$ExitCodes += $RefreshExitCode

if ([int]$RefreshExitCode -eq 0) {
    $BuildExitCode = Run-Step "build_cv_ranks.py" @("python", "-X", "utf8", "-u", "build_cv_ranks.py")
    $ExitCodes += $BuildExitCode
    if ([int]$BuildExitCode -eq 0) {
        $ExitCodes += Run-Step "update_rank_meta.py cv" @("python", "-X", "utf8", "-u", "update_rank_meta.py", "cv")
    } else {
        Write-Log "=== update_rank_meta.py cv skipped: build_cv_ranks.py exit=$BuildExitCode ==="
    }
} else {
    Write-Log "=== build_cv_ranks.py skipped: refresh_watch_counts.py exit=$RefreshExitCode ==="
    Write-Log "=== update_rank_meta.py cv skipped: refresh_watch_counts.py exit=$RefreshExitCode ==="
}

$FailedExitCodes = $ExitCodes | Where-Object { [int]$_ -ne 0 }
$FinalCode = if ($FailedExitCodes.Count -gt 0) { 1 } else { 0 }
Write-Log "=== Done final_exit=$FinalCode $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
exit $FinalCode
