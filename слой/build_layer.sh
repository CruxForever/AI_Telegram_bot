#!/bin/bash
# Скрипт для сборки Lambda Layer через Docker

set -e

echo "=== Сборка Lambda Layer для Python 3.11 ==="

# Имя образа и контейнера
IMAGE_NAME="lambda-layer-builder"
CONTAINER_NAME="layer-builder-temp"

# Удаляем старый контейнер если есть
docker rm -f $CONTAINER_NAME 2>/dev/null || true

# Собираем Docker образ
echo "Шаг 1: Сборка Docker образа..."
docker build -t $IMAGE_NAME .

# Создаем контейнер
echo "Шаг 2: Создание контейнера..."
docker create --name $CONTAINER_NAME $IMAGE_NAME

# Копируем layer из контейнера
echo "Шаг 3: Извлечение layer..."
docker cp $CONTAINER_NAME:/opt/python ./python

# Создаем zip архив
echo "Шаг 4: Создание ZIP архива..."
zip -r lambda_layer.zip python/

# Очищаем
echo "Шаг 5: Очистка..."
docker rm $CONTAINER_NAME
rm -rf python/

echo "=== Готово! ==="
echo "Lambda Layer сохранен в: lambda_layer.zip"
echo "Размер файла:"
ls -lh lambda_layer.zip
