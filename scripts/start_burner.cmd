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
REM  Chinese user-facing messages live in scripts/start_msg.py.
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
.venv\Scripts\python.exe scripts\start_msg.py venv-broken-burner
.venv\Scripts\python.exe scripts\start_msg.py rebuilding-burner
echo.
%WHERE% uv >nul 2>&1
if errorlevel 1 goto :nouv

call uv sync
if errorlevel 1 (
  echo.
.venv\Scripts\python.exe scripts\start_msg.py sync-failed
  echo.
  pause
  goto :end
)
echo.
.venv\Scripts\python.exe scripts\start_msg.py venv-rebuilt
goto :envready

:nouv
.venv\Scripts\python.exe scripts\start_msg.py uv-missing
echo       irm https://astral.sh/uv/install.ps1 ^| iex
echo.
.venv\Scripts\python.exe scripts\start_msg.py uv-reopen
echo.
pause
goto :end

:envready
echo.
.venv\Scripts\python.exe scripts\start_msg.py banner --mode burner
.venv\Scripts\python.exe scripts\launch_hidden.py burner %*
if errorlevel 1 (
  echo.
  .venv\Scripts\python.exe scripts\start_msg.py tray-failed-burner
  echo.
  pause
)
goto :end

:end
endlocal
