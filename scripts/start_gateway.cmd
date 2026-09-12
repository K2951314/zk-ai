@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  ZK-AI gateway launcher (Windows)
REM
REM  This file is intentionally pure ASCII. Batch files containing non-ASCII
REM  text can be misparsed by cmd.exe (byte-offset desync around labels and
REM  multi-byte characters), which once created junk files in the startup
REM  directory and silently skipped lines. Chinese user-facing messages live
REM  in scripts/port_guard.py, which handles UTF-8 correctly on its own.
REM
REM  Port precedence: 1) CLI arg  2) .env ZKAI_PORT  3) built-in default 8317
REM  Example:  start_gateway.cmd 9000
REM ===========================================================================

REM Localhost must bypass HTTP_PROXY or the gateway becomes unreachable
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "FINDSTR=%SystemRoot%\System32\findstr.exe"

REM ---- Resolve port: CLI arg, then .env, then default 8317 ----
set "ZKAI_PORT="
if not "%~1"=="" set "ZKAI_PORT=%~1"
if not defined ZKAI_PORT if exist ".env" (
  for /f "tokens=1,* delims==" %%a in ('%FINDSTR% /b /l "ZKAI_PORT=" .env') do if not defined ZKAI_PORT set "ZKAI_PORT=%%b"
)
if not defined ZKAI_PORT set "ZKAI_PORT=8317"

REM ---- Resolve listen host: .env ZKAI_HOST, then default 127.0.0.1 ----
set "ZKAI_HOST="
if exist ".env" (
  for /f "tokens=1,* delims==" %%a in ('%FINDSTR% /b /l "ZKAI_HOST=" .env') do if not defined ZKAI_HOST set "ZKAI_HOST=%%b"
)
if not defined ZKAI_HOST set "ZKAI_HOST=127.0.0.1"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   [ERROR] .venv not found. Run "uv sync" in the project root first.
  echo.
  pause
  goto :end
)

REM ---- Port guard: kills OUR stale instance, refuses on foreign process ----
.venv\Scripts\python.exe scripts\port_guard.py
set GUARD=%ERRORLEVEL%
if "%GUARD%"=="0" goto :start
if "%GUARD%"=="1" goto :busy
goto :failed

:start
echo.
echo   ZK-AI gateway starting...   http://127.0.0.1:%ZKAI_PORT%
echo   - Port : %ZKAI_PORT%   (CLI arg wins over .env ZKAI_PORT, then default 8317)
echo   - Host : %ZKAI_HOST%   (0.0.0.0 = reachable from other machines on the LAN)
echo   - Stop : press Ctrl+C
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host %ZKAI_HOST% --port %ZKAI_PORT% --log-level info
goto :end

:busy
echo   Startup cancelled.
pause
goto :end

:failed
echo   Startup cancelled.
pause
goto :end

:end
endlocal
