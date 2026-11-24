# Fetch the vendored Tailwind build so the UI works with zero network access.
# Run once after cloning (or after a fresh Python venv). Safe to re-run.
#
# Usage:  powershell -ExecutionPolicy Bypass -File tools\fetch_vendor.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "static"
$out = Join-Path $dir "tailwind.js"
$url = "https://cdn.tailwindcss.com/3.4.16"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

if ((Test-Path $out) -and ((Get-Item $out).Length -gt 100000)) {
    Write-Host "already vendored: $out ($((Get-Item $out).Length) bytes) - nothing to do"
    exit 0
}

Write-Host "downloading $url ..."
Invoke-WebRequest $url -OutFile $out -UseBasicParsing
$size = (Get-Item $out).Length
if ($size -lt 100000) {
    Write-Host "WARNING: file looks truncated ($size bytes). Check your network and re-run."
    exit 1
}
Write-Host "done: $out ($size bytes). The page now loads Tailwind from /static/tailwind.js, no CDN needed."
