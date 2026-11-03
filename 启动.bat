@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem use python on PATH, fall back to the py launcher
set "PY=python"
where python >nul 2>nul || set "PY=py"

rem if 8765 is already serving, do not start a second web instance
netstat -ano | findstr /R /C:"127.0.0.1:8765 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting web ...  http://127.0.0.1:8765
  start "AI web" cmd /k ""%PY%" run.py --no-browser"
  ping -n 3 127.0.0.1 >nul
) else (
  echo Web already running on 8765, skip.
)

echo Starting QQ bot ...
start "AI qqbot" cmd /k ""%PY%" qqbot\main.py"

start "" http://127.0.0.1:8765
exit
