# ============================================================
# refresh-season.ps1
# Fetch raw anime-release data, then rebuild season-data.js
#
# Deliberately 100% ASCII source: PowerShell 5.1 parses non-BOM
# UTF-8 files as ANSI and mis-handles non-ASCII bytes. All Chinese
# text lives in scripts/build-season.js (read by node, always UTF-8).
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File refresh-season.ps1
# Exit:   0 = rebuilt, 1 = every source failed (previous data kept)
# ============================================================

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Invoke-WebRequest floods the progress stream ("reading response stream... n bytes")
# once per chunk; unattended runs should stay quiet.
$ProgressPreference = 'SilentlyContinue'

$Root  = Split-Path -Parent $PSScriptRoot
$Cache = Join-Path $Root '.cache'
$Log   = Join-Path $Cache 'refresh.log'
if (-not (Test-Path $Cache)) { New-Item -ItemType Directory -Force -Path $Cache | Out-Null }

# NOTE: Write-Host / [IO.File] deliberately used instead of Write-Output.
# Write-Output inside Save-Url would join the success stream and turn the
# function's return value into a non-empty array, which is ALWAYS truthy in
# PowerShell -- that silently breaks the "(Save-Url ...) ? $Fetched++ : $Failed++"
# accounting and the all-sources-failed safety net below.
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

function Log {
  param([string]$Message)
  $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
  Write-Host $line
  [IO.File]::AppendAllText($Log, $line + "`r`n", $Utf8NoBom)
}

# --- rotate the log so it never grows without bound ---
if (Test-Path $Log) {
  $old = @(Get-Content -Path $Log -ErrorAction SilentlyContinue)
  if ($old.Count -gt 400) {
    [IO.File]::WriteAllText($Log, (($old[-200..-1] -join "`r`n") + "`r`n"), $Utf8NoBom)
  }
}

# --- local proxy detection ------------------------------------------------
# The panel exe usually runs as SYSTEM (watchdog-spawned), where the per-user
# WinINET proxy (127.0.0.1:10808) is INVISIBLE -- Invoke-WebRequest then goes
# direct, dmhy times out (GFW) and every rebuild fails ("正在重建本季快照"
# banner loops forever). So probe the local proxy explicitly and prefer it;
# fall back to direct per-request when the proxy is down.
$Proxy = $null
foreach ($cand in @('127.0.0.1:10808')) {
  $pp = $cand.Split(':')
  $tc = New-Object Net.Sockets.TcpClient
  try {
    $ar = $tc.BeginConnect($pp[0], [int]$pp[1], $null, $null)
    if ($ar.AsyncWaitHandle.WaitOne(500) -and $tc.Connected) { $Proxy = $cand }
  } catch { } finally { $tc.Close() }
}
if ($Proxy) { Log ('--- proxy: using http://' + $Proxy + ' (local listener detected)') }
else        { Log '--- proxy: none found, going direct' }

$Ua       = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
$DmhyPage = 6

# Resolve node dynamically: newest version dir under the managed runtime root.
# Hardcoding e.g. "22.22.2-3" breaks silently when WorkBuddy upgrades node and
# removes the old version dir (CommandNotFoundException -> season-data.js stale,
# page retries forever). Falls back to PATH "node" if the runtime root is gone.
$Node = $null
$VerRoot = Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions'
if (Test-Path $VerRoot) {
  $Node = Get-ChildItem -Path $VerRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName 'node.exe' } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
}
if (-not $Node) { $Node = 'node' }

Log '=== refresh-season start ==='

# --- clean stale dumps so the build never mixes generations ---
Get-ChildItem -Path (Join-Path $Cache '*') -Include 'dmhy*.html', 'mikan*.xml', 'anibt*' -File -ErrorAction SilentlyContinue |
  Remove-Item -Force -ErrorAction SilentlyContinue

$Fetched = 0
$Failed  = 0

function Save-Url {
  param([string]$Url, [string]$Name)
  # attempt order: explicit local proxy first (if detected), then direct
  $attempts = @()
  if ($Proxy) { $attempts += 'proxy' }
  $attempts += 'direct'
  foreach ($mode in $attempts) {
    try {
      if ($mode -eq 'proxy') {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 30 -UseBasicParsing -UserAgent $Ua -Proxy ('http://' + $Proxy)
      } else {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 30 -UseBasicParsing -UserAgent $Ua
      }
      if ($r.Content.Length -lt 2000) {
        Log ("  SKIP {0,-16} (too small: {1}b)" -f $Name, $r.Content.Length)
        return $false
      }
      [IO.File]::WriteAllText((Join-Path $Cache $Name), $r.Content, $Utf8NoBom)
      Log ("  OK   {0,-16} {1,8} bytes ({2})" -f $Name, $r.Content.Length, $mode)
      return $true
    } catch {
      Log ("  FAIL {0,-16} [{1}] {2}" -f $Name, $mode, $_.Exception.Message)
    }
  }
  return $false
}

Log '--- fetching dmhy (anime / new season) ---'
for ($i = 1; $i -le $DmhyPage; $i++) {
  if ($i -eq 1) {
    $url = 'https://share.dmhy.org/topics/list/sort_id/2'
  } else {
    $url = 'https://share.dmhy.org/topics/list/sort_id/2/page/' + $i
  }
  if (Save-Url -Url $url -Name ('dmhy_p' + $i + '.html')) { $Fetched++ } else { $Failed++ }
}

Log '--- fetching mikan (classic rss) ---'
if (Save-Url -Url 'https://mikanime.tv/RSS/Classic' -Name 'mikan_classic.xml') { $Fetched++ } else { $Failed++ }

# ---- AniBT open API: no auth required, and the only source that returns
# ---- fansub group / anime / episode as real fields instead of a filename to regex.
Log '--- fetching anibt (open api) ---'
if (Save-Url -Url 'https://anibt.net/rss/magnets.xml'      -Name 'anibt_magnets.xml')  { $Fetched++ } else { $Failed++ }
if (Save-Url -Url 'https://anibt.net/api/seasons/anime'   -Name 'anibt_seasons.json') { $Fetched++ } else { $Failed++ }
if (Save-Url -Url 'https://anibt.net/api/subtitle-groups' -Name 'anibt_groups.json')  { $Fetched++ } else { $Failed++ }

Log ("--- fetch summary: {0} ok / {1} failed ---" -f $Fetched, $Failed)

if ($Fetched -eq 0) {
  Log 'ERROR: every source failed, keeping previous season-data.js untouched.'
  exit 1
}

Log '--- building season-data.js ---'
& $Node (Join-Path $PSScriptRoot 'build-season.js')
if ($LASTEXITCODE -ne 0) {
  Log ("ERROR: build-season.js exited with code {0}" -f $LASTEXITCODE)
  exit $LASTEXITCODE
}

# --- mirror the fresh files next to the packaged exe ---
# The server resolves season-data.js beside the exe BEFORE the copy frozen
# inside it, so mirroring here keeps AnimeFansubGuide.exe current without a
# rebuild. Note the exe, not the project root, is what the user double-clicks.
$Dist = Join-Path $Root 'dist'
if (Test-Path $Dist) {
  foreach ($f in @('season-data.js', 'anime-fansub-guide.html')) {
    $src = Join-Path $Root $f
    if (Test-Path $src) {
      try {
        Copy-Item -Path $src -Destination (Join-Path $Dist $f) -Force
        Log ("  MIRROR {0} -> dist\" -f $f)
      } catch {
        Log ("  MIRROR {0} FAILED: {1}" -f $f, $_.Exception.Message)
      }
    }
  }
}

Log '--- done ---'
exit 0
