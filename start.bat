@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title mon_brain launcher

set "PY=%~dp0.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:5000"
set "OLLAMA=http://127.0.0.1:11434"

echo ============================================
echo   mon_brain - local note brain on Ollama
echo ============================================
echo.

rem ---------- 1. Python venv ----------
if not exist "%PY%" (
  echo [X] venv not found: %PY%
  echo     Create .venv first and install requests / pypdf.
  echo.
  pause
  exit /b 1
)

rem ---------- 2. GPU preflight (report only) ----------
where nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo [!] nvidia-smi not found - Ollama will fall back to CPU. Expect 5-20x slower runs.
) else (
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader > "%TEMP%\mb_gpu.txt" 2>nul
  set /p GPUINFO=<"%TEMP%\mb_gpu.txt"
  del "%TEMP%\mb_gpu.txt" >nul 2>nul
  if defined GPUINFO (echo [OK] GPU: !GPUINFO!) else (echo [!] nvidia-smi present but returned nothing - check the driver.)
)

rem ---------- 3. Ollama ----------
curl -s -m 3 "%OLLAMA%/api/version" | findstr /c:"version" >nul 2>nul
if not errorlevel 1 goto ollama_ok

echo [*] Ollama is not running, starting "ollama serve" with GPU flags ...
where ollama >nul 2>nul
if errorlevel 1 (
  echo [X] ollama command not found.
  echo     Start the Ollama desktop app, or add it to PATH.
  echo.
  pause
  exit /b 1
)
rem These flags must be set before the server starts; inherited env may be stale.
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_MAX_LOADED_MODELS=1"
set "OLLAMA_KEEP_ALIVE=30m"
start "Ollama" /min cmd /c "ollama serve"

set /a n=0
:wait_ollama
timeout /t 1 /nobreak >nul
curl -s -m 3 "%OLLAMA%/api/version" | findstr /c:"version" >nul 2>nul
if not errorlevel 1 goto ollama_ok
set /a n+=1
if !n! geq 45 (
  echo [X] Ollama did not come up within 45s. Run "ollama serve" manually to see the error.
  echo.
  pause
  exit /b 1
)
goto wait_ollama

:ollama_ok
echo [OK] Ollama is up on %OLLAMA%

rem ---------- 4. required models (warn only) ----------
set "MISSING="
for %%m in (qwen2.5:latest gemma3:12b qwen3:14b) do (
  curl -s -m 5 "%OLLAMA%/api/tags" | findstr /c:"%%m" >nul 2>nul
  if errorlevel 1 set "MISSING=!MISSING! %%m"
)
if defined MISSING (
  echo [!] Missing model^(s^):!MISSING!
  echo     Run: ollama pull ^<model^> before using that preset.
) else (
  echo [OK] All preset models present: qwen2.5:latest / gemma3:12b / qwen3:14b
)

rem ---------- 5. web server ----------
curl -s -m 3 "%URL%/api/state" | findstr /c:"modes" >nul 2>nul
if not errorlevel 1 goto web_running

netstat -ano | findstr /r /c:":5000 .*LISTENING" >nul
if not errorlevel 1 (
  echo [X] Port 5000 is taken by another program, not mon_brain.
  echo     Close it, or change PORT at the top of app_web.py.
  echo.
  pause
  exit /b 1
)

echo [*] Starting the web server ...
start "mon_brain web" /D "%~dp0" "%PY%" app_web.py

set /a m=0
:wait_web
timeout /t 1 /nobreak >nul
curl -s -m 3 "%URL%/api/state" | findstr /c:"modes" >nul 2>nul
if not errorlevel 1 goto web_ok
set /a m+=1
if !m! geq 30 (
  echo [X] Web server did not answer on port 5000 within 30s.
  echo     Check the errors in the "mon_brain web" window.
  echo.
  pause
  exit /b 1
)
goto wait_web

:web_ok
echo [OK] Web server is running
goto open

:web_running
echo [OK] mon_brain web server already running, reusing it

:open
echo.
echo   Workbench : %URL%   (app_web.py opens it automatically)
echo   Docs      : %URL%/docs
echo   Stop      : close the "mon_brain web" window
echo.
exit /b 0
