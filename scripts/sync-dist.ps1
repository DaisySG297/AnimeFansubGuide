# sync-dist.ps1 - publish the front-end page from the project root into dist/
#
# WHY THIS EXISTS
# ----------------
# _resource_path() prefers the copy that sits in the runtime data dir (dist/)
# over the one in the project root, by design: the exe must pick up user-edited
# files without a rebuild. The side effect is that editing
# <root>/anime-fansub-guide.html alone changes NOTHING at runtime - the stale
# dist/ copy keeps winning, and if it still references variables that were
# removed (e.g. the deleted SEASON / state inside initLiveOnly) the whole
# inline <script> aborts with a ReferenceError and the page breaks.
#
# Run this after every front-end edit:
#     powershell -File scripts/sync-dist.ps1
#
# It backs up the current dist copy, verifies tag balance, copies, re-verifies
# and checks that removed identifiers have not crept back in.

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot
$src     = Join-Path $root 'anime-fansub-guide.html'
$distDir = Join-Path $root 'dist'
$dst     = Join-Path $distDir 'anime-fansub-guide.html'

if (-not (Test-Path $src)) { throw "source page not found: $src" }
if (-not (Test-Path $distDir)) { New-Item -ItemType Directory -Path $distDir | Out-Null }

# identifiers that belong to features removed on 2026-10-14; if any of them
# reappears in the source, a stale block was pasted back in.
$deadTokens = @('initLiveOnly', 'liveOnlyChip', 'TIER_ORDER', 'markTier',
                'cmdText', 'planOne', 'quickSub')

function Get-Md5([string]$p) {
    (Get-FileHash -Path $p -Algorithm MD5).Hash
}

# 1) structural check on the source BEFORE copying: syntax, stray identifiers,
#    missing DOM ids, view wiring and dangling top-level calls. Editing a page
#    that cannot even initialise must never reach the served copy.
$pager = Join-Path $PSScriptRoot 'page-check.js'
if ((Test-Path $pager) -and (Get-Command node -ErrorAction SilentlyContinue)) {
    Push-Location $root
    try {
        & node $pager $src
        if ($LASTEXITCODE -ne 0) { throw 'page-check.js found structural problems - dist not updated' }
    }
    finally { Pop-Location }
} else {
    Write-Host '[skip] page-check.js or node not available' -ForegroundColor DarkGray
}

# 2) dead-token scan
$text = Get-Content -Path $src -Raw
$hits = @()
foreach ($t in $deadTokens) {
    if ($text -match "(?<![A-Za-z0-9_])$t(?![A-Za-z0-9_])") { $hits += $t }
}
if ($hits.Count -gt 0) {
    throw ("source still references removed identifiers: " + ($hits -join ', '))
}
Write-Host '[ok] no removed identifiers in source' -ForegroundColor Green

# 3) copy when the two copies actually differ
if ((Test-Path $dst) -and ((Get-Md5 $src) -eq (Get-Md5 $dst))) {
    Write-Host '[ok] dist copy already up to date - nothing to do' -ForegroundColor Green
    exit 0
}

if (Test-Path $dst) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak = "$dst.bak-$stamp"
    Copy-Item $dst $bak -Force
    Write-Host "[bak] previous dist copy -> $([System.IO.Path]::GetFileName($bak))"
}

Copy-Item $src $dst -Force

$srcMd5 = Get-Md5 $src
$dstMd5 = Get-Md5 $dst
if ($srcMd5 -ne $dstMd5) { throw "copy mismatch: $srcMd5 vs $dstMd5" }

Write-Host "[ok] synced  $([System.IO.Path]::GetFileName($dst))  ($((Get-Item $src).Length) bytes)" -ForegroundColor Green
Write-Host "     md5 = $srcMd5"
Write-Host '     page is served with Cache-Control: no-store, a plain reload is enough'
