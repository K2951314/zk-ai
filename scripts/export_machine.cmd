@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  One-click export for moving to a new machine (Windows)
REM
REM  Pure ASCII + CRLF on purpose: batch files with non-ASCII bytes can be
REM  misparsed by cmd.exe (byte-offset desync). All Chinese text lives in
REM  scripts/migrate.py.
REM
REM  What it does: packs .env (every API key), config/*.yaml, and the local
REM  database into a single encrypted zip in .\exports\.
REM  What it NEVER does: stop the gateway, or delete anything on this machine.
REM ===========================================================================

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   [ERROR] .venv not found. Run scripts\start_gateway.cmd once first,
  echo           it rebuilds the environment automatically.
  echo.
  pause
  goto :end
)

echo.
echo   Exporting this machine's ZK-AI identity (keys + config + database).
echo   You will be asked for a transfer password - remember it, the new
echo   machine needs the same one. Input is hidden.
echo.
".venv\Scripts\python.exe" scripts\migrate.py export
if errorlevel 1 (
  echo.
  echo   Export failed - see the message above.
  echo.
  pause
  goto :end
)

echo.
explorer "%CD%\exports"
goto :end

:end
endlocal