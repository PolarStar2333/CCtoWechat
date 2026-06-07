@echo off
cd /d "%~dp0"
echo ============================
echo   WeChat + Claude Bridge
echo ============================
echo.
python -u bridge.py %*
pause
