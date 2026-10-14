# ============================================================
# build-exe.ps1 -- package the local server into a single exe
#
# Deliberately 100% ASCII source: PowerShell 5.1 parses non-BOM UTF-8
# as ANSI and mis-handles non-ASCII bytes. All Chinese text lives in
# the .py / .html files, which are read as UTF-8 by their own runtimes.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File build-exe.ps1
# Output: <root>\dist\AnimeFansubGuide.exe
# ============================================================

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Py   = if ($env:AFG_PY) { $env:AFG_PY } else { 'python' }
$Dist = Join-Path $Root 'dist'

if (-not (Test-Path $Py)) { throw ("python not found: " + $Py) }
if (-not (Test-Path (Join-Path $Root 'app\anime_fansub_server.py'))) { throw 'server source missing' }

# build dirs are kept outside the source list so a rebuild never picks up
# its own extraction output
if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Force -Path $Dist | Out-Null }
$Exe = Join-Path $Dist 'AnimeFansubGuide.exe'
if (Test-Path $Exe) { Remove-Item $Exe -Force }

# keep the live config: it holds apiKey / standbyCleanup / https / auth etc. and
# must survive rebuilds (a first-run regeneration would silently reset every
# setting). Backup path is TIMESTAMPED and never deleted.
#
# 2026-10-07 incident: the old convention used a FIXED path in %TEMP%
# (afg-config-backup.json). A stale copy taken before the https/auth work was
# later restored over the live config -> port 7790 + Basic auth silently
# disappeared and the public HTTPS portal stopped answering. Never restore
# a backup you did not take in this very run.
$cfg = Join-Path $Dist 'config.json'
$cfgBakDir = Join-Path $Dist 'config-backups'
if (-not (Test-Path $cfgBakDir)) { New-Item -ItemType Directory -Force -Path $cfgBakDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$cfgBak = Join-Path $cfgBakDir ("config-" + $stamp + ".json")
if (Test-Path $cfg) { Copy-Item $cfg $cfgBak -Force }
# --console: keep the persistent console window the user asked for
# (closing that window stops the service; also the only place the startup
#  banner / live log is visible). --noconsole silently dropped this behaviour.
& $Py -m PyInstaller --noconfirm --clean --onefile --console `
  --name AnimeFansubGuide `
  --add-data "$Root\anime-fansub-guide.html;." `
  --add-data "$Root\season-data.js;." `
  --distpath $Dist `
  --workpath "$Root\.build\work" `
  --specpath "$Root\.build" `
  "$Root\app\anime_fansub_server.py"
if ($LASTEXITCODE -ne 0) { throw ("pyinstaller failed: " + $LASTEXITCODE) }

# Stage editable copies beside the exe. The server resolves these BEFORE the
# copies frozen inside it, which is what lets scripts\refresh-season.ps1 keep
# the page current without repackaging the exe.
foreach ($f in @('anime-fansub-guide.html', 'season-data.js')) {
  Copy-Item (Join-Path $Root $f) $Dist -Force
}
# restore the live config wiped during packaging (keep the timestamped backup)
if (Test-Path $cfgBak) {
  Copy-Item $cfgBak $cfg -Force
}

# surface the exposure-critical settings: a silent config regression here means
# the public HTTPS portal (7790) or the Basic auth gate is gone.
try {
  $live = Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
  $httpsTxt = 'off'
  if ($live.https -and $live.https.enable) { $httpsTxt = 'on:' + $live.https.port }
  $authTxt = 'off'
  if ($live.auth -and $live.auth.enable) { $authTxt = 'on:' + $live.auth.username }
  Write-Output ("config: bind=" + $live.bind + " http=" + $live.port + " https=" + $httpsTxt + " auth=" + $authTxt)
  Write-Output ("config backup kept: " + $cfgBak)
} catch {
  Write-Output ("config: UNREADABLE -> " + $_.Exception.Message)
}

Write-Output ''
Write-Output ("built: " + $Exe)
Get-Item $Exe | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime | Format-List
