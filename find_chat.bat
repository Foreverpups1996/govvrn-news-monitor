@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Find Telegram chat_id ===
echo.
echo 1) The contact must FIRST send /start to your bot.
echo 2) Paste the bot token from BotFather below and press Enter.
echo.
set /p TOKEN=Token: 
if "%TOKEN%"=="" (
    echo Token is empty. Try again.
    pause
    exit /b 1
)
set TELEGRAM_BOT_TOKEN=%TOKEN%
echo.
py news_monitor.py --find-chat
echo.
pause
