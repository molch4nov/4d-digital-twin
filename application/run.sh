#!/bin/bash

# Запуск приложения 3D Реконструкция

echo "🚀 Запуск приложения 3D Реконструкция..."
echo ""

# Проверяем Python
if ! command -v python &> /dev/null; then
    echo "❌ Python не установлен"
    exit 1
fi

# Проверяем, что мы в нужной директории
if [ ! -f "backend/main.py" ]; then
    echo "❌ Ошибка: запускайте скрипт из директории application"
    exit 1
fi

# Создаём виртуальное окружение, если его нет
if [ ! -d "backend/venv" ]; then
    echo "📦 Создаём виртуальное окружение..."
    python -m venv backend/venv
fi

# Активируем виртуальное окружение
echo "✅ Активируем виртуальное окружение..."
source backend/venv/bin/activate

# Устанавливаем зависимости
if [ ! -f "backend/venv/pyvenv.cfg" ]; then
    echo "📦 Устанавливаем зависимости..."
    pip install -q -r backend/requirements.txt
fi

# Запускаем сервер
echo ""
echo "🌐 Запускаем сервер на http://localhost:8000"
echo ""
echo "Интерфейс доступен по адресу: http://localhost:8000"
echo ""
echo "Для остановки нажмите Ctrl+C"
echo ""

cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
