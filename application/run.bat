@echo off
setlocal enabledelayedexpansion

echo.
echo 🚀 Запуск приложения 3D Реконструкция...
echo.

REM Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не установлен
    pause
    exit /b 1
)

REM Проверяем, что мы в нужной директории
if not exist "backend\main.py" (
    echo ❌ Ошибка: запускайте скрипт из директории application
    pause
    exit /b 1
)

REM Создаём виртуальное окружение, если его нет
if not exist "backend\venv" (
    echo 📦 Создаём виртуальное окружение...
    python -m venv backend\venv
)

REM Активируем виртуальное окружение
echo ✅ Активируем виртуальное окружение...
call backend\venv\Scripts\activate.bat

REM Устанавливаем зависимости
if not exist "backend\venv\pyvenv.cfg" (
    echo 📦 Устанавливаем зависимости...
    pip install -q -r backend\requirements.txt
)

REM Запускаем сервер
echo.
echo 🌐 Запускаем сервер на http://localhost:8000
echo.
echo Интерфейс доступен по адресу: http://localhost:8000
echo.
echo Для остановки нажмите Ctrl+C
echo.

cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
