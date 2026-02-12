@echo off
REM Скрипт для сборки Lambda Layer через Docker (Windows)

echo === Сборка Lambda Layer для Python 3.11 ===

SET IMAGE_NAME=lambda-layer-builder
SET CONTAINER_NAME=layer-builder-temp

REM Удаляем старый контейнер если есть
docker rm -f %CONTAINER_NAME% 2>nul

REM Собираем Docker образ
echo Шаг 1: Сборка Docker образа...
docker build -t %IMAGE_NAME% .
if errorlevel 1 (
    echo Ошибка при сборке образа!
    exit /b 1
)

REM Создаем контейнер
echo Шаг 2: Создание контейнера...
docker create --name %CONTAINER_NAME% %IMAGE_NAME%
if errorlevel 1 (
    echo Ошибка при создании контейнера!
    exit /b 1
)

REM Копируем layer из контейнера
echo Шаг 3: Извлечение layer...
docker cp %CONTAINER_NAME%:/opt/python ./python
if errorlevel 1 (
    echo Ошибка при копировании файлов!
    docker rm %CONTAINER_NAME%
    exit /b 1
)

REM Создаем zip архив (используем PowerShell)
echo Шаг 4: Создание ZIP архива...
powershell -Command "Compress-Archive -Path ./python -DestinationPath lambda_layer.zip -Force"
if errorlevel 1 (
    echo Ошибка при создании ZIP!
    docker rm %CONTAINER_NAME%
    rmdir /s /q python
    exit /b 1
)

REM Очищаем
echo Шаг 5: Очистка...
docker rm %CONTAINER_NAME%
rmdir /s /q python

echo === Готово! ===
echo Lambda Layer сохранен в: lambda_layer.zip
dir lambda_layer.zip
