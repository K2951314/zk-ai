@echo off
REM ===========================================================================
REM  ZK-AI 网关启动脚本
REM
REM  双击即可启动。密钥从 .env 读取，不需要在这里写任何 key。
REM
REM  启动前会检查 8000 端口：
REM    - 是本程序的旧实例  -> 自动结束它，然后启动
REM    - 是其他程序占用    -> 打印提示并退出（不会误杀别人的进程）
REM ===========================================================================
setlocal
cd /d "%~dp0.."

REM 本机设置了 HTTP_PROXY，必须让 localhost 绕过代理，否则连不上本地网关
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM 切到 UTF-8 代码页，否则中文提示在 cmd 里会变成乱码
chcp 65001 >nul 2>&1

REM ---- 端口检查 ----
.venv\Scripts\python.exe scripts\port_guard.py
set GUARD=%ERRORLEVEL%
if "%GUARD%"=="0" goto :start
if "%GUARD%"=="1" goto :busy
if "%GUARD%"=="2" goto :failed

:start
echo.
echo   ZK-AI 网关启动中...   http://127.0.0.1:8000
echo   - 健康检查: http://127.0.0.1:8000/health
echo   - 模型列表: http://127.0.0.1:8000/v1/models
echo   - 停止服务: 按 Ctrl+C
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info
goto :end

:busy
echo   已取消启动。
pause
goto :end

:failed
echo   已取消启动。
pause
goto :end

:end
endlocal
