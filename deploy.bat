@echo off
chcp 65001 >nul
cd /d "%~dp0"
set BRANCH=coinglass-monitor

echo ============================================
echo  CoinGlass Monitor - Deploy helper
echo ============================================
echo.
echo [1/3] Pushing %BRANCH% to GitHub...
git push -u origin %BRANCH%
if %errorlevel% neq 0 (
    echo.
    echo Push failed. Make sure:
    echo   1. A remote points at the repo:
    echo      git remote add origin https://github.com/favliy/gate-monitor.git
    echo   2. Or just run:
    echo      git push -u origin %BRANCH%
    pause
    exit /b 1
)
echo.
echo [2/3] Opening Render service selector...
start "" "https://dashboard.render.com/select-repo?type=web"
echo.
echo ============================================
echo  In Render:
echo   1. Connect repository  favliy/gate-monitor
echo   2. Select branch  %BRANCH%
echo   3. Set env vars / secrets:
echo      TELEGRAM_BOT_TOKEN  (your bot token)
echo      TELEGRAM_CHAT_ID   (-1003974837328)
echo   4. Plan: Free   then  Create Web Service
echo ============================================
echo.
pause