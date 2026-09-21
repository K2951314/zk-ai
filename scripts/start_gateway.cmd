@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  ZK-AI gateway launcher (Windows)
REM
REM  This file is intentionally pure ASCII. Batch files containing non-ASCII
REM  text can be misparsed by cmd.exe (byte-offset desync around labels and
REM  multi-byte characters), which once created junk files and skipped lines.
REM  Chinese user-facing messages live in scripts/port_guard.py.
REM
REM  Port precedence: 1) CLI arg  2) .env ZKAI_PORT  3) built-in default 8317
REM
REM  Environment: if .venv is missing or broken (e.g. a .venv copied from
REM  another machine - uv trampolines point at the original machine's Python,
REM  so they never survive a copy), the launcher rebuilds it automatically
REM  via "uv sync". One-time prerequisite on a new machine, in PowerShell:
REM      irm https://astral.sh/uv/install.ps1 | iex
REM  No separate Python install is needed; uv downloads Python 3.12+ itself.
REM ===========================================================================

REM Localhost must bypass HTTP_PROXY or the gateway becomes unreachable
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "FINDSTR=%SystemRoot%\System32\findstr.exe"
set "WHERE=%SystemRoot%\System32\where.exe"

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

REM ---- First run: materialise .env / config templates, then tell what to fill ----
.venv\Scripts\python.exe scripts\first_run.py
if errorlevel 1 (
  echo.
  echo   Fill in .env as printed above, then run this script again.
  pause
  goto :end
)

REM ---- Ensure a WORKING .venv (missing or copied-and-broken triggers rebuild) ----
set "VENV_OK="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "pass" >nul 2>&1
  if not errorlevel 1 set "VENV_OK=1"
)
if defined VENV_OK goto :envready

echo.
echo   [INFO ] Python environment .venv is missing or broken.
echo           A .venv copied from another machine never works; it will be
echo           rebuilt here. Python 3.12+ is downloaded automatically.
echo.
%WHERE% uv >nul 2>&1
if errorlevel 1 goto :nouv

echo   Rebuilding with "uv sync" - downloads Python and packages, please wait...
echo.
call uv sync
if errorlevel 1 (
  echo.
  echo   [ERROR] uv sync failed - see the messages above.
  echo.
  pause
  goto :end
)
echo.
echo   .venv rebuilt.
goto :envready

:nouv
echo   [ERROR] uv is not installed. Install it once, in PowerShell:
echo.
echo       irm https://astral.sh/uv/install.ps1 ^| iex
echo.
echo   Then close this window, reopen it (so PATH refreshes) and start again.
echo   No separate Python installation is needed.
echo.
pause
goto :end

:envready
REM ---- Port guard: kills OUR stale instance, refuses on foreign process ----
.venv\Scripts\python.exe scripts\port_guard.py
set GUARD=%ERRORLEVEL%
if "%GUARD%"=="0" goto :start
if "%GUARD%"=="1" goto :busy
goto :failed

:start
echo.
echo   ZK-AI gateway starting in the system tray (no window).
echo   - Port : %ZKAI_PORT%   (CLI arg wins over .env ZKAI_PORT, then default 8317)
echo   - Host : %ZKAI_HOST%   (0.0.0.0 = reachable from other machines on the LAN)
echo   - Tray : bottom-right corner, green=running / blue=stopped
echo   - Stop : right-click tray icon - Quit
echo   - UI   : your browser opens automatically, already signed in with the
echo           admin token from .env (no paste needed).
echo.
REM launch_hidden.py spawns pythonw with NO console window; a bare
REM `start ... pythonw.exe` inherits the batch console and leaves a minimized
REM python.exe window stuck in the taskbar (uv venv launcher is a trampoline).
REM It also returns non-zero when the tray died on startup, and its own output
REM is captured in data\tray_gateway.log - without that, a broken .venv looked
REM exactly like "the gateway crashed silently".
.venv\Scripts\python.exe scripts\launch_hidden.py gateway
if errorlevel 1 goto :trayfailed
REM Wait for the port in the FOREGROUND, then open the console with the admin
REM token: this window stays open while the gateway boots (a few seconds), so a
REM gateway that never comes up is reported here instead of vanishing.
.venv\Scripts\python.exe scripts\open_console.py --port %ZKAI_PORT% --wait 30
if errorlevel 1 goto :nolisten
goto :end

:trayfailed
echo.
echo   [ERROR] The tray process could not start, so no icon and no gateway.
echo           Logs: data\tray_gateway.log  and  data\gateway.log
echo           If it says ModuleNotFoundError, run:  uv sync
echo.
pause
goto :end

:nolisten
echo.
echo   [ERROR] The tray is running but nothing answered on port %ZKAI_PORT%.
echo           Server log: data\gateway.log
echo           Right-click the tray icon - View log, or re-run this script.
echo.
pause
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
