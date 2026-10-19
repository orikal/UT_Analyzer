#Requires -Version 5.1
<#
.SYNOPSIS
    Builds the UT Analyzer Windows release: app bundle and installer.

.DESCRIPTION
    Run from the repository root on Windows:
        .\scripts\build_release.ps1

    Prerequisites:
        - Python 3.11-3.13 (3.13 supported; use -Python if default python is 3.14+)
        - Inno Setup 6 (optional, for installer)
#>
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Error $msg; exit 1 }

$config = Get-Content "$Root\release\versions.json" | ConvertFrom-Json
$AppVersion = $config.application.version

Write-Step "UT Analyzer Release Build v$AppVersion"

# 1. Clean build environment
Write-Step "Cleaning previous build artifacts"
@("build\pyinstaller", "dist\UTAnalyzer", "dist\installer") | ForEach-Object {
    $p = Join-Path $Root $_
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
Get-ChildItem "$Root\dist\UTAnalyzer-*.exe" -ErrorAction SilentlyContinue | Remove-Item -Force

# 2. Create venv and install pinned dependencies
Write-Step "Creating release virtual environment"
$venv = "$Root\build\release-venv"
if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
& $Python -m venv $venv
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r "$Root\requirements-lock.txt"
if ($LASTEXITCODE -ne 0) { Fail "Failed to install pinned dependencies (check Python version: 3.11-3.13)" }
& $py -m pip install pyinstaller==6.21.0 pytest==8.3.4

# 3. Run tests
if (-not $SkipTests) {
    Write-Step "Running automated tests"
    & $py -m pytest "$Root\tests" -q
    if ($LASTEXITCODE -ne 0) { Fail "Tests failed" }
}

# 4. Stamp build date
Write-Step "Stamping build date"
$versionsPath = "$Root\release\versions.json"
$versions = Get-Content $versionsPath -Raw | ConvertFrom-Json
$versions.application.build_date = (Get-Date -Format "yyyy-MM-dd")
$json = $versions | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($versionsPath, $json, [System.Text.UTF8Encoding]::new($false))

# 5. Build standalone application with PyInstaller
Write-Step "Building standalone application (PyInstaller)"
$pyinstaller = Join-Path $venv "Scripts\pyinstaller.exe"
& $pyinstaller "$Root\build\ut_analyzer.spec" --distpath "$Root\dist" --workpath "$Root\build\pyinstaller" --noconfirm
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed" }
$builtExe = Get-ChildItem "$Root\dist\UTAnalyzer-*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $builtExe) { Fail "UTAnalyzer exe not found after PyInstaller build" }
$ExeName = $builtExe.BaseName

# Copy default-config with stamped versions (one-folder builds only)
if (Test-Path "$Root\dist\UTAnalyzer") {
    New-Item -ItemType Directory -Force -Path "$Root\dist\UTAnalyzer\default-config" | Out-Null
    Copy-Item "$Root\release\versions.json" "$Root\dist\UTAnalyzer\default-config\versions.json" -Force
}

# 6. Verify license files
Write-Step "Verifying license files"
if (-not (Test-Path "$Root\licenses\THIRD-PARTY-NOTICES.txt")) {
    Fail "Missing license file: THIRD-PARTY-NOTICES.txt"
}

# 7. Smoke test packaged executable
Write-Step "Smoke testing packaged executable"
if (-not (Test-Path $builtExe.FullName)) {
    Fail "Packaged executable missing"
}

# 8. Create installer with Inno Setup
if (-not $SkipInstaller) {
    Write-Step "Creating Windows installer"
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($iscc) {
        & $iscc "/DMyAppVersion=$AppVersion" "$Root\installer\ut_analyzer.iss"
        if ($LASTEXITCODE -ne 0) { Fail "Inno Setup build failed" }
    } else {
        Write-Warning "Inno Setup not found. Skipping installer creation. Install from https://jrsoftware.org/isinfo.php"
    }
}

# 9. Produce checksums
Write-Step "Producing release checksums"
$checksumFile = "$Root\dist\SHA256SUMS.txt"
Remove-Item $checksumFile -ErrorAction SilentlyContinue
Get-ChildItem "$Root\dist" -Recurse -File | Where-Object {
    $_.Extension -in @(".exe", ".zip", ".msi")
} | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    Add-Content $checksumFile "$hash  $($_.Name)"
}

Write-Step "Release build complete"
Write-Host "Output: $Root\dist" -ForegroundColor Green
Write-Host "  Application: dist\$ExeName.exe"
Write-Host "  Installer:   dist\installer\UTAnalyzer-Setup-$AppVersion.exe"
