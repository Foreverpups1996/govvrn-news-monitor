@echo off
chcp 65001 >nul

echo === Upload project to GitHub ===
echo.
echo 1) Create a repository at https://github.com/new (choose Private).
echo 2) Copy its address, e.g. https://github.com/LOGIN/govvrn-news-monitor.git
echo.
set /p REPO=Repository URL: 
if "%REPO%"=="" (
    echo Repository URL is empty. Try again.
    pause
    exit /b 1
)

cd /d "%~dp0"
git init
git add .
git commit -m "govvrn.ru news monitor"
git branch -M main
git remote add origin %REPO%
git push -u origin main

echo.
echo Done! Next steps:
echo  1) Repo - Settings - Secrets and variables - Actions - New repository secret:
echo       TELEGRAM_BOT_TOKEN = your bot token
echo       TELEGRAM_CHAT_ID   = numeric chat id
echo  2) Actions tab - Enable workflows
echo.
pause
