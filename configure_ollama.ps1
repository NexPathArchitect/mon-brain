# Configure Ollama for this laptop's GPU (RTX 5070 Ti Laptop, 12 GB VRAM).
# Writes user-level environment variables and restarts the Ollama app so they take effect.
# Run: powershell -ExecutionPolicy Bypass -File .\configure_ollama.ps1

$ErrorActionPreference = "Stop"

# KV cache is halved by q8_0, flash attention is faster and lighter, and a single
# loaded model keeps a 12B model fully inside 12 GB of VRAM.
$vars = @{
    OLLAMA_FLASH_ATTENTION   = "1"
    OLLAMA_KV_CACHE_TYPE     = "q8_0"
    OLLAMA_NUM_PARALLEL      = "1"
    OLLAMA_MAX_LOADED_MODELS = "1"
    OLLAMA_KEEP_ALIVE        = "30m"
}

foreach ($k in $vars.Keys) {
    [Environment]::SetEnvironmentVariable($k, $vars[$k], "User")
    Set-Item -Path ("Env:" + $k) -Value $vars[$k]
    Write-Host ("set {0}={1}" -f $k, $vars[$k])
}

Get-Process | Where-Object { $_.ProcessName -like "ollama*" } | Stop-Process -Force
Start-Sleep -Seconds 2

$app = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
if (Test-Path $app) {
    Start-Process -FilePath $app
    Start-Sleep -Seconds 8
    Write-Host "`nOllama restarted. Current state:"
    ollama ps
} else {
    Write-Host "Ollama app not found at $app; start it manually."
}
Write-Host "`nExpect '100% GPU' in the PROCESSOR column while a model is loaded."
