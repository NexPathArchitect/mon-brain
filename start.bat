@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title mon_brain launcher

set "PY=%~dp0.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:5000"
set "NOBROWSER=0"
if /i "%~1"=="nobrowser" set "NOBROWSER=1"

echo ============================================
echo   mon_brain - local note brain on Ollama
echo ============================================
echo.

if not exist "%PY%" (
  echo [X] venv not found: %PY%
  echo     Create .venv first and install requests / pypdf.
  echo.
  pause
  exit /b 1
)

rem ---------- 1. Ollama ----------
netstat -ano | findstr /r /c:":11434 .*LISTENING" >nul
if not errorlevel 1 goto ollama_ok

echo [*] Ollama is not running, starting "ollama serve" in background ...
where ollama >nul 2>nul
if errorlevel 1 (
  echo [X] ollama command not found.
  echo     Start the Ollama desktop app, or add it to PATH.
  echo.
  pause
  exit /b 1
)
start "Ollama" /min cmd /c "ollama serve"

set /a n=0
:wait_ollama
timeout /t 1 /nobreak >nul
netstat -ano | findstr /r /c:":11434 .*LISTENING" >nul
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
echo [OK] Ollama is up on 127.0.0.1:11434

rem ---------- 2. web server ----------
netstat -ano | findstr /r /c:":5000 .*LISTENING" >nul
if not errorlevel 1 goto web_running

echo [*] Starting the web server ...
start "mon_brain web" /D "%~dp0" "%PY%" app_web.py

set /a m=0
:wait_web
timeout /t 1 /nobreak >nul
netstat -ano | findstr /r /c:":5000 .*LISTENING" >nul
if not errorlevel 1 goto web_ok
set /a m+=1
if !m! geq 30 (
  echo [X] Web server did not listen on port 5000 within 30s.
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
echo [OK] Web server already running, reusing it

:open
echo.
echo   Workbench : %URL%   (app_web.py opens it automatically)
echo   Docs      : %URL%/docs
echo   Stop      : close the "mon_brain web" window
echo.
if "%NOBROWSER%"=="1" exit /b 0
exit /b 0
