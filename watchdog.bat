# Gate Monitor Watchdog
cd /d "D:\Backup\Documents\New project"
:loop
echo %date% %time% Starting monitor...
python main.py >> logs\monitor.log 2>&1
echo %date% %time% Exit code: %ERRORLEVEL% - restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
