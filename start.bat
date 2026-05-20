@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
    echo BOT_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE>.env
    echo Создан файл .env. Впишите BOT_TOKEN и запустите start.bat снова.
    pause
    exit /b 1
)

findstr /c:"BOT_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE" ".env" >nul
if %errorlevel% equ 0 (
    echo Впишите настоящий BOT_TOKEN в файл .env и запустите start.bat снова.
    pause
    exit /b 1
)

py -3.11 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_CMD=py -3.11"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости.
    pause
    exit /b 1
)

%PYTHON_CMD% bot.py
pause
