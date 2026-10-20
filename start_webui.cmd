@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 正在准备运行环境...
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, multipart, playwright, m3u8, av, imageio_ffmpeg, yanhe_sentinel" >nul 2>&1
if errorlevel 1 (
  echo 首次安装工作台依赖，需要网络，请稍候...
  ".venv\Scripts\python.exe" -m pip install -e ".[web,media]"
  if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -m yanhe_sentinel webui
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo 启动未完成。请检查 Python 或网络，具体步骤见 README.md。
pause
exit /b 1
