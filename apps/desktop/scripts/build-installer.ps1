<#
.SYNOPSIS
    Builds ZachCap's Windows installer (NSIS), with the engine and uv inside it.
.DESCRIPTION
    Copies the engine's tracked files (its source, pyproject.toml and uv.lock; no venv or
    caches) and uv into src-tauri\installer\, then runs `tauri build` with
    tauri.installer.conf.json, which turns bundling on and adds both as resources. uv is
    downloaded from its GitHub release at a pinned version and checked against a pinned
    SHA-256. On first launch the installed app sets the engine up with that uv (see set_up in
    src-tauri\src\lib.rs). Prints the installer's path and SHA-256.

    Run from a clean checkout of the commit being released: the BUILD stamp records it, and
    the installed app sets its engine up again whenever the stamp changes. A tree with
    uncommitted changes is refused unless -AllowDirty is given (for a test build).

    ZachCap's own code goes in as a wheel built here with that uv, so the installed app only
    installs pinned, hash-checked files and never fetches a build tool.
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File apps\desktop\scripts\build-installer.ps1
#>
param([switch]$AllowDirty)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$UvVersion = '0.13.9'
$UvZipSha256 = 'edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb'
$UvLicenses = @{
    'LICENSE-MIT'    = '860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c'
    'LICENSE-APACHE' = 'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4'
}

$desktop = Split-Path $PSScriptRoot -Parent
$repo = Split-Path (Split-Path $desktop -Parent) -Parent
$tauri = Join-Path $desktop 'src-tauri'
$stage = Join-Path $tauri 'installer'
$cache = Join-Path $tauri 'target\installer-cache'

function Get-Download([string]$Url, [string]$Path, [string]$Sha256) {
    if (-not (Test-Path $Path) -or (Get-FileHash $Path -Algorithm SHA256).Hash -ne $Sha256) {
        Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
    }
    $actual = (Get-FileHash $Path -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        Remove-Item $Path -Force
        throw "$Url has SHA-256 $actual, expected $Sha256"
    }
}

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $stage 'engine'), (Join-Path $stage 'uv'), $cache | Out-Null

# The engine: what git tracks, so nothing local (a venv, caches, test output) goes along.
$files = git -C $repo ls-files -- engine/pyproject.toml engine/uv.lock engine/README.md engine/src
if ($LASTEXITCODE -ne 0 -or -not $files) { throw 'git ls-files found no engine files' }
foreach ($file in $files) {
    $target = Join-Path $stage ($file -replace '^engine/', 'engine/')
    New-Item -ItemType Directory -Force (Split-Path $target -Parent) | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo $file) -Destination $target
}
$version = (Get-Content (Join-Path $tauri 'tauri.conf.json') -Raw | ConvertFrom-Json).version
$commit = git -C $repo rev-parse --short HEAD
$dirty = if (git -C $repo status --porcelain) { '+local-changes' } else { '' }
if ($dirty -and -not $AllowDirty) { throw 'The working tree has uncommitted changes: commit them, or pass -AllowDirty for a test build' }
$stamp = "$version $commit$dirty $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
[IO.File]::WriteAllText((Join-Path $stage 'engine\BUILD'), $stamp)

# uv, pinned and checked, with its licenses (MIT or Apache-2.0).
$zip = Join-Path $cache "uv-$UvVersion.zip"
Get-Download "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip $UvZipSha256
$unpacked = Join-Path $cache "uv-$UvVersion"
if (Test-Path $unpacked) { Remove-Item $unpacked -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $unpacked
Copy-Item (Join-Path $unpacked 'uv.exe') (Join-Path $stage 'uv\uv.exe')
foreach ($license in $UvLicenses.GetEnumerator()) {
    $path = Join-Path $cache "uv-$UvVersion-$($license.Key)"
    Get-Download "https://raw.githubusercontent.com/astral-sh/uv/$UvVersion/$($license.Key)" $path $license.Value
    Copy-Item $path (Join-Path $stage "uv\$($license.Key)")
}

# ZachCap's own code as a wheel, from the staged copy (only what git tracks).
$uv = Join-Path $stage 'uv\uv.exe'
& $uv build (Join-Path $stage 'engine') --wheel --out-dir (Join-Path $stage 'engine\wheel') --no-config
if ($LASTEXITCODE -ne 0) { throw "uv build failed ($LASTEXITCODE)" }

Write-Host "Staged engine ($(@($files).Count) files and its wheel, BUILD $stamp) and uv $UvVersion"

Push-Location $desktop
try {
    $env:Path = "C:\Program Files\nodejs;$env:Path"
    npx tauri build --config src-tauri/tauri.installer.conf.json
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed ($LASTEXITCODE)" }
}
finally { Pop-Location }

$installer = Get-ChildItem (Join-Path $tauri 'target\release\bundle\nsis') -Filter "*_${version}_x64-setup.exe" |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $installer) { throw 'No installer was built' }
$hash = (Get-FileHash $installer.FullName -Algorithm SHA256).Hash.ToLower()
Write-Host "`nInstaller: $($installer.FullName)"
Write-Host "Size:      $([math]::Round($installer.Length / 1MB, 1)) MB"
Write-Host "SHA-256:   $hash"
