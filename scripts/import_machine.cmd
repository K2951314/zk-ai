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
if not errorlevel 1 goto :importdone
REM Exit code 3 = the overwrite guard (target already has .env / config\*.yaml),
REM which is the only failure worth answering with --overwrite. 1 (gateway still
REM running) and 2 (wrong passphrase / truncated zip) just get reported as-is.
if errorlevel 3 goto :askoverwrite
goto :importfailed

:askoverwrite
echo.
echo   (Those files already exist - that is the overwrite guard, not a failure.)
set "ANSWER="
set /p "ANSWER=   Retry with --overwrite? current files are backed up first [y/N]: "
if /i not "%ANSWER%"=="y" goto :importfailed
echo.
".venv\Scripts\python.exe" scripts\migrate.py import "%ARCHIVE%" --overwrite
if errorlevel 1 goto :importfailed

:importdone
echo.
pause
goto :end

:importfailed
echo.
echo   Import failed - see the message above.
echo.
pause
goto :end

:end
endlocal