@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  One-click import on the NEW machine (Windows)
REM
REM  Pure ASCII + CRLF on purpose: batch files with non-ASCII bytes can be
REM  misparsed by cmd.exe (byte-offset desync). All Chinese text lives in
REM  scripts/start_msg.py.
REM
REM  Usage: double-click this file, then drag the transfer .zip into the
REM  window (or pass it as the first argument). Anything that would be
REM  overwritten is backed up first into .\imports_backup\.
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

set "ARCHIVE=%~1"
REM The Python helper prints the Chinese prompt and reads stdin; for /f takes
REM the value back into the batch variable. No English prompt string here
REM (AGENTS.md: .cmd must stay pure ASCII).
if "%ARCHIVE%"=="" (
  echo.
.venv\Scripts\python.exe scripts\start_msg.py drag-zip
  echo.
set "ARCHIVE="
for /f "usebackq delims=" %%a in (`.venv\Scripts\python.exe scripts\start_msg.py ask-zip`) do set "ARCHIVE=%%a"
)
REM Drag-and-drop wraps paths in quotes; strip them.
set "ARCHIVE=%ARCHIVE:"=%"

if "%ARCHIVE%"=="" (
  echo.
.venv\Scripts\python.exe scripts\start_msg.py no-archive
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
.venv\Scripts\python.exe scripts\start_msg.py overwrite-guard
set "ANSWER="
for /f "usebackq delims=" %%a in (`.venv\Scripts\python.exe scripts\start_msg.py ask-overwrite`) do set "ANSWER=%%a"
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
.venv\Scripts\python.exe scripts\start_msg.py failed-see-above
echo.
pause
goto :end

:end
endlocal