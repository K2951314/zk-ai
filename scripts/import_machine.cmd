@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  One-click import on the NEW machine (Windows)
REM
REM  Pure ASCII + CRLF on purpose: batch files with non-ASCII bytes can be
REM  misparsed by cmd.exe (byte-offset desync). All Chinese text lives in
REM  scripts/migrate.py.
REM
REM  Usage: double-click this file, then drag the transfer .zip into the
REM  window (or pass it as the first argument). Anything that would be
REM  overwritten is backed up first into .\imports_backup\.
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

set "ARCHIVE=%~1"
if "%ARCHIVE%"=="" (
  echo.
  echo   Drag the transfer .zip file into this window, then press Enter:
  echo.
  set /p "ARCHIVE=archive path: "
)
REM Drag-and-drop wraps paths in quotes; strip them.
set "ARCHIVE=%ARCHIVE:"=%"

if "%ARCHIVE%"=="" (
  echo.
  echo   [ERROR] no archive given.
  echo.
  pause
  goto :end
)

".venv\Scripts\python.exe" scripts\migrate.py import "%ARCHIVE%"
if errorlevel 1 (
  echo.
  echo   Import failed - see the message above.
  echo.
  pause
  goto :end
)

echo.
pause
goto :end

:end
endlocal