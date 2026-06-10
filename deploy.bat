@echo off
chcp 65001 >nul
cd /d "D:\Backup\Documents\New project"

echo ============================================
echo  Gate.io Monitor - 一键部署脚本
echo ============================================
echo.

echo [1/3] 推送到 GitHub...
git push origin main
if %errorlevel% neq 0 (
    echo.
    echo 推送失败！请检查:
    echo   1. GitHub 仓库是否存在: https://github.com/favliy/gate-monitor
    echo   2. 如果不存在，去 https://github.com/new 创建新仓库
    echo   3. 然后修改远程地址: git remote set-url origin 新地址
    pause
    exit /b 1
)
echo 推送成功！
echo.

echo [2/3] 打开 Render 部署页面...
start "" "https://dashboard.render.com/select-repo?type=web"
echo.
echo ============================================
echo  请在打开的 Render 页面中:
echo   1. 连接仓库 favliy/gate-monitor
echo   2. 手动填入两个密钥:
echo      TELEGRAM_BOT_TOKEN = 8946385457:AAFG3RKJmDqmScuebTbKfw_zDvAE1zrLq6w
echo      TELEGRAM_CHAT_ID = -1003974837328
echo   3. 选 Free 计划，点 Create Web Service
echo ============================================
echo.
echo [3/3] 部署后，打开 UptimeRobot 保持在线:
start "" "https://uptimerobot.com"
echo.
pause
