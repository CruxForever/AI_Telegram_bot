# Сборка Lambda Layer для Python 3.11

Этот проект создает Lambda Layer с зависимостями для Telegram-бота с Claude API.

## Зависимости

- `anthropic` - SDK для работы с Claude API
- `requests` - для HTTP запросов к Telegram API
- `boto3` - для работы с DynamoDB (опционально, обычно уже есть в Lambda)

## Использование

### Для Windows

```cmd
build_layer.bat
```

### Для Linux/Mac

```bash
chmod +x build_layer.sh
./build_layer.sh
```

## Что делает скрипт

1. Собирает Docker образ на базе `public.ecr.aws/lambda/python:3.11`
2. Устанавливает зависимости из `requirements.txt` в `/opt/python`
3. Копирует собранные библиотеки из контейнера
4. Создает ZIP архив `lambda_layer.zip`
5. Очищает временные файлы

## Загрузка в AWS

После создания `lambda_layer.zip`:

### Через AWS CLI

```bash
aws lambda publish-layer-version \
    --layer-name telegram-claude-dependencies \
    --description "Anthropic, Requests для Python 3.11" \
    --zip-file fileb://lambda_layer.zip \
    --compatible-runtimes python3.11
```

### Через AWS Console

1. Откройте AWS Lambda Console
2. Перейдите в раздел "Layers"
3. Нажмите "Create layer"
4. Загрузите файл `lambda_layer.zip`
5. Укажите совместимую среду выполнения: `python3.11`

## Подключение Layer к Lambda функции

После публикации layer:

1. Откройте вашу Lambda функцию
2. Прокрутите вниз до раздела "Layers"
3. Нажмите "Add a layer"
4. Выберите "Custom layers"
5. Выберите созданный layer `telegram-claude-dependencies`

## Требования

- Docker Desktop (для Windows/Mac) или Docker Engine (для Linux)
- Достаточно места на диске (~100MB для образа и layer)

## Размер Layer

Ожидаемый размер готового layer: ~20-30 MB

## Troubleshooting

### Docker не запускается
Убедитесь, что Docker Desktop запущен

### Ошибка при создании ZIP (Windows)
Скрипт использует PowerShell для создания архива. Убедитесь, что PowerShell доступен.

### Layer слишком большой
Dockerfile уже настроен для очистки ненужных файлов (__pycache__, tests, etc.)
