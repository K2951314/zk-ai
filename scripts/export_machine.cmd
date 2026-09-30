@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  One-click export for moving to a new machine (Windows)
REM
REM  Pure ASCII + CRLF on purpose: batch files with non-ASCII bytes can be
REM  misparsed by cmd.exe (byte-offset desync). All Chinese text lives in
REM  scripts/start_msg.py.
REM
REM  What it does: packs .env (every API key), config/*.yaml, and the local
REM  database into a single encrypted zip in .\exports\.
REM  What it NEVER does: stop the gateway, or delete anything on this machine.
REM ===========================================================================

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
  echo.
.venv\Scripts\python.exe scripts\start_msg.py venv-missing-cmd
  echo.
  pause
  goto :end
)

echo.
.venv\Scripts\python.exe scripts\start_msg.py export-banner
echo.
".venv\Scripts\python.exe" scripts\migrate.py export
if errorlevel 1 (
  echo.
.venv\Scripts\python.exe scripts\start_msg.py failed-see-above
  echo.
  pause
  goto :end
)

echo.
explorer "%CD%\exports"
goto :end

:end
endlocal