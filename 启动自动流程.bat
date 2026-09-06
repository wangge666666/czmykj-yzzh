@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv-workflow\Scripts\python.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装运行环境.ps1"
    if errorlevel 1 (
        echo.
        echo 安装失败，请保留此窗口并检查上方错误。
        pause
        exit /b 1
    )
)

echo 正在启动本地网页：http://127.0.0.1:7860
echo 浏览器会自动打开；关闭本窗口即可停止服务。
echo.
".venv-workflow\Scripts\python.exe" "%~dp0web_app.py"

if errorlevel 1 (
    echo.
    echo 网页服务启动失败，请检查上方错误。
    pause
)
