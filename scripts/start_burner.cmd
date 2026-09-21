@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  ZK-AI SenseNova credit burner launcher (Windows)
REM
REM  Burns sensenova-6.8-flash-lite credits continuously in the background.
REM  (SenseNova converts flash-lite usage into credits usable by kimi-k3 and
REM  other models, so idle quota should be burned on flash-lite.)
REM
REM  This file is intentionally pure ASCII + CRLF. Batch files containing
REM  non-ASCII text can be misparsed by cmd.exe (byte-offset desync), which
REM  once created junk files and skipped lines. See start_gateway.cmd.
REM  Chinese user-facing messages live in scripts/burn_sensenova.py.
REM
REM  Usage:
REM     scripts\start_burner.cmd                       (default settings)
REM     scripts\start_burner.cmd --concurrency 16      (extra args pass through)
REM
REM  A minimized console window opens. Closing that window (or pressing
REM  Ctrl+C inside it) stops the burner. Live log: data\burn_sensenova.log
REM ===========================================================================

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "WHERE=%SystemRoot%\System32\where.exe"

REM ---- Ensure a WORKING .venv (missing or copied-and-broken triggers rebuild) ----
set "VENV_OK="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "pass" >nul 2>&1
  if not errorlevel 1 set "VENV_OK=1"
)
if defined VENV_OK goto :envready

echo.
echo   [INFO ] Python environment .venv is missing or broken.
echo           Rebuilding with "uv sync" - downloads Python and packages...
echo.
%WHERE% uv >nul 2>&1
if errorlevel 1 goto :nouv

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
echo.
pause
goto :end

:envready
echo.
echo   Starting burner in the system tray (no window).
echo   - Tray : bottom-right corner, green=running / blue=stopped
echo   - Log  : data\burn_sensenova.log
echo   - Stop : right-click tray icon - Quit
echo.
.venv\Scripts\python.exe scripts\launch_hidden.py burner %*
if errorlevel 1 (
  echo.
  echo   [ERROR] The tray process could not start, so no icon and no burner.
  echo           Logs: data\tray_burner.log  and  data\burn_sensenova.log
  echo           If it says ModuleNotFoundError, run:  uv sync
  echo.
  pause
)
goto :end

:end
endlocal
