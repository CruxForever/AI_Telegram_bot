# Руководство по развёртыванию

Пошаговая инструкция по развёртыванию AI Petrovich на AWS.

## Предварительные требования

- AWS аккаунт с правами на создание Lambda, SQS, DynamoDB
- AWS CLI установлен и настроен (`aws configure`)
- Docker Desktop (для сборки Lambda Layer)
- Telegram Bot Token (получить у [@BotFather](https://t.me/botfather))
- Anthropic API Key ([console.anthropic.com](https://console.anthropic.com))

## Шаг 1: Создание Telegram бота

### 1.1. Создание бота

```bash
# Откройте @BotFather в Telegram
# Отправьте: /newbot
# Следуйте инструкциям, получите токен
```

**Сохраните:**
- `TELEGRAM_TOKEN` - токен бота
- `BOT_USERNAME` - username бота (без @)

### 1.2. Получение Bot ID

```bash
# Замените <TOKEN> на ваш токен
curl https://api.telegram.org/bot<TOKEN>/getMe

# Ответ:
# {
#   "ok": true,
#   "result": {
#     "id": 123456789,  # ← Это BOT_ID
#     "username": "your_bot",
#     ...
#   }
# }
```

**Сохраните:** `BOT_ID` - ID бота

## Шаг 2: Создание DynamoDB таблиц

### 2.1. Users

```bash
aws dynamodb create-table \
    --table-name Users \
    --attribute-definitions \
        AttributeName=user_id,AttributeType=S \
    --key-schema \
        AttributeName=user_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
```

### 2.2. Channels

```bash
aws dynamodb create-table \
    --table-name Channels \
    --attribute-definitions \
        AttributeName=channel_id,AttributeType=S \
    --key-schema \
        AttributeName=channel_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
```

### 2.3. Threads

```bash
aws dynamodb create-table \
    --table-name Threads \
    --attribute-definitions \
        AttributeName=thread_id,AttributeType=S \
    --key-schema \
        AttributeName=thread_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
```

### 2.4. Messages (с TTL)

```bash
aws dynamodb create-table \
    --table-name Messages \
    --attribute-definitions \
        AttributeName=dialog_key,AttributeType=S \
        AttributeName=timestamp,AttributeType=N \
    --key-schema \
        AttributeName=dialog_key,KeyType=HASH \
        AttributeName=timestamp,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1

# Включить TTL (удаление через 1 год автоматически)
aws dynamodb update-time-to-live \
    --table-name Messages \
    --time-to-live-specification \
        Enabled=true,AttributeName=expire_at \
    --region us-east-1
```

### 2.5. Summaries (с TTL)

```bash
aws dynamodb create-table \
    --table-name Summaries \
    --attribute-definitions \
        AttributeName=dialog_key,AttributeType=S \
        AttributeName=timestamp,AttributeType=N \
    --key-schema \
        AttributeName=dialog_key,KeyType=HASH \
        AttributeName=timestamp,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1

# Включить TTL
aws dynamodb update-time-to-live \
    --table-name Summaries \
    --time-to-live-specification \
        Enabled=true,AttributeName=expire_at \
    --region us-east-1
```

### 2.6. Settings

```bash
aws dynamodb create-table \
    --table-name Settings \
    --attribute-definitions \
        AttributeName=dialog_key,AttributeType=S \
    --key-schema \
        AttributeName=dialog_key,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
```

**Проверка:**
```bash
aws dynamodb list-tables --region us-east-1
# Должны быть: Users, Channels, Threads, Messages, Summaries, Settings
```

## Шаг 3: Сборка Lambda Layer

### 3.1. Сборка через Docker

**Windows:**
```cmd
cd "слой"
build_layer.bat
```

**Linux/Mac:**
```bash
cd слой
chmod +x build_layer.sh
./build_layer.sh
```

**Результат:** файл `lambda_layer.zip` (~20-30 MB)

### 3.2. Загрузка Layer в AWS

```bash
aws lambda publish-layer-version \
    --layer-name telegram-claude-dependencies \
    --description "Anthropic SDK, requests, boto3 для Python 3.11" \
    --zip-file fileb://lambda_layer.zip \
    --compatible-runtimes python3.11 \
    --region us-east-1

# Ответ:
# {
#   "LayerArn": "arn:aws:lambda:us-east-1:...:layer:telegram-claude-dependencies",
#   "LayerVersionArn": "arn:aws:lambda:us-east-1:...:layer:telegram-claude-dependencies:1",  # ← Сохраните
#   ...
# }
```

**Сохраните:** `LayerVersionArn`

## Шаг 4: Создание SQS очереди

### 4.1. Создание FIFO очереди

```bash
aws sqs create-queue \
    --queue-name telegram-bot-queue.fifo \
    --attributes '{
        "FifoQueue": "true",
        "ContentBasedDeduplication": "true",
        "VisibilityTimeout": "70",
        "MessageRetentionPeriod": "86400"
    }' \
    --region us-east-1

# Ответ:
# {
#   "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789/telegram-bot-queue.fifo"  # ← Сохраните
# }
```

**Сохраните:** `QueueUrl`

### 4.2. Получение Queue ARN

```bash
aws sqs get-queue-attributes \
    --queue-url <QueueUrl> \
    --attribute-names QueueArn \
    --region us-east-1

# Ответ:
# {
#   "Attributes": {
#     "QueueArn": "arn:aws:sqs:us-east-1:123456789:telegram-bot-queue.fifo"  # ← Сохраните
#   }
# }
```

**Сохраните:** `QueueArn`

## Шаг 5: Создание IAM Role для Lambda

### 5.1. Создание Trust Policy

Создайте файл `lambda-trust-policy.json`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 5.2. Создание роли

```bash
aws iam create-role \
    --role-name TelegramBotLambdaRole \
    --assume-role-policy-document file://lambda-trust-policy.json

# Сохраните RoleArn из ответа
```

### 5.3. Прикрепление политик

```bash
# Базовая Lambda execution policy
aws iam attach-role-policy \
    --role-name TelegramBotLambdaRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# DynamoDB доступ
aws iam attach-role-policy \
    --role-name TelegramBotLambdaRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess

# SQS доступ
aws iam attach-role-policy \
    --role-name TelegramBotLambdaRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonSQSFullAccess
```

**Сохраните:** `RoleArn` (например: `arn:aws:iam::123456789:role/TelegramBotLambdaRole`)

## Шаг 6: Создание Lambda функций

### 6.1. Подготовка webhook_lambda

```bash
# Создайте zip с webhook_lambda.py
zip webhook_lambda.zip webhook_lambda.py

# Создайте Lambda функцию
aws lambda create-function \
    --function-name telegram-webhook-handler \
    --runtime python3.11 \
    --role <RoleArn> \
    --handler webhook_lambda.lambda_handler \
    --zip-file fileb://webhook_lambda.zip \
    --timeout 10 \
    --memory-size 128 \
    --environment Variables="{
        SQS_QUEUE_URL=<QueueUrl>,
        SQS_IS_FIFO=1
    }" \
    --region us-east-1

# Сохраните FunctionArn из ответа
```

**Добавьте разрешение для API Gateway (позже):**
```bash
aws lambda add-permission \
    --function-name telegram-webhook-handler \
    --statement-id apigateway-invoke \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --region us-east-1
```

### 6.2. Подготовка worker_lambda

```bash
# Создайте zip со всеми файлами
zip -r worker_lambda.zip \
    worker_lambda.py \
    claude_utils.py \
    dynamo_utils.py \
    telegram_utils.py

# Создайте Lambda функцию
aws lambda create-function \
    --function-name telegram-worker \
    --runtime python3.11 \
    --role <RoleArn> \
    --handler worker_lambda.lambda_handler \
    --zip-file fileb://worker_lambda.zip \
    --timeout 60 \
    --memory-size 512 \
    --layers <LayerVersionArn> \
    --environment Variables="{
        ANTHROPIC_API_KEY=<YOUR_KEY>,
        TELEGRAM_TOKEN=<YOUR_TOKEN>,
        BOT_USERNAME=<BOT_USERNAME>,
        BOT_ID=<BOT_ID>,
        BASE_SYSTEM_PROMPT=Ты дружелюбный AI ассистент,
        CLAUDE_MODEL=claude-sonnet-4-5-20250929,
        MAX_CONTEXT_TOKENS=6000,
        MAX_OUTPUT_TOKENS=800,
        MIN_MSGS_FOR_SUMMARY=12,
        SUMMARY_HISTORY_LIMIT=60,
        USERS_TABLE=Users,
        CHANNELS_TABLE=Channels,
        THREADS_TABLE=Threads,
        MESSAGES_TABLE=Messages,
        SUMMARIES_TABLE=Summaries,
        SETTINGS_TABLE=Settings,
        GROUP_SCOPE_DEFAULT=hybrid
    }" \
    --region us-east-1
```

**⚠️ ВАЖНО:** Замените `<YOUR_KEY>` и `<YOUR_TOKEN>` на реальные значения!

### 6.3. Подключение SQS к worker_lambda

```bash
aws lambda create-event-source-mapping \
    --function-name telegram-worker \
    --event-source-arn <QueueArn> \
    --batch-size 1 \
    --region us-east-1
```

## Шаг 7: Создание API Gateway

### 7.1. Создание REST API

```bash
aws apigateway create-rest-api \
    --name telegram-bot-api \
    --description "Webhook для Telegram бота" \
    --region us-east-1

# Сохраните api-id из ответа
```

### 7.2. Получение root resource

```bash
aws apigateway get-resources \
    --rest-api-id <api-id> \
    --region us-east-1

# Сохраните id root resource (обычно первый в списке)
```

### 7.3. Создание /webhook ресурса

```bash
aws apigateway create-resource \
    --rest-api-id <api-id> \
    --parent-id <root-resource-id> \
    --path-part webhook \
    --region us-east-1

# Сохраните id нового ресурса
```

### 7.4. Создание POST метода

```bash
# Создание метода
aws apigateway put-method \
    --rest-api-id <api-id> \
    --resource-id <webhook-resource-id> \
    --http-method POST \
    --authorization-type NONE \
    --region us-east-1

# Интеграция с Lambda
aws apigateway put-integration \
    --rest-api-id <api-id> \
    --resource-id <webhook-resource-id> \
    --http-method POST \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/<webhook-lambda-arn>/invocations \
    --region us-east-1
```

### 7.5. Деплой API

```bash
aws apigateway create-deployment \
    --rest-api-id <api-id> \
    --stage-name prod \
    --region us-east-1

# URL будет: https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/webhook
```

**Сохраните:** webhook URL

## Шаг 8: Настройка Telegram Webhook

### 8.1. Установка webhook

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook" \
    -H "Content-Type: application/json" \
    -d '{
        "url": "https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/webhook",
        "max_connections": 40,
        "drop_pending_updates": false
    }'

# Ответ:
# {
#   "ok": true,
#   "result": true,
#   "description": "Webhook was set"
# }
```

### 8.2. Проверка webhook

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getWebhookInfo"

# Должен быть:
# {
#   "ok": true,
#   "result": {
#     "url": "https://...",
#     "has_custom_certificate": false,
#     "pending_update_count": 0
#   }
# }
```

## Шаг 9: Тестирование

### 9.1. Отправьте сообщение боту

```
Откройте Telegram → найдите вашего бота → напишите "Привет"
```

### 9.2. Проверьте CloudWatch Logs

**webhook_lambda:**
```bash
aws logs tail /aws/lambda/telegram-webhook-handler --follow --region us-east-1
```

**worker_lambda:**
```bash
aws logs tail /aws/lambda/telegram-worker --follow --region us-east-1
```

### 9.3. Проверьте DynamoDB

```bash
# Проверка Users
aws dynamodb scan --table-name Users --region us-east-1

# Проверка Messages
aws dynamodb scan --table-name Messages --region us-east-1
```

## Шаг 10: Мониторинг

### 10.1. CloudWatch Dashboards

Создайте dashboard для мониторинга:
- Lambda invocations
- Lambda errors
- Lambda duration
- SQS ApproximateNumberOfMessages
- DynamoDB consumed capacity

### 10.2. CloudWatch Alarms

Настройте алармы:
```bash
# Пример: алерт при ошибках Lambda
aws cloudwatch put-metric-alarm \
    --alarm-name telegram-worker-errors \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 1 \
    --metric-name Errors \
    --namespace AWS/Lambda \
    --period 60 \
    --statistic Sum \
    --threshold 5 \
    --dimensions Name=FunctionName,Value=telegram-worker \
    --region us-east-1
```

## Обновление кода

### Обновление worker_lambda

```bash
# Пересоздайте zip
zip -r worker_lambda.zip \
    worker_lambda.py \
    claude_utils.py \
    dynamo_utils.py \
    telegram_utils.py

# Обновите функцию
aws lambda update-function-code \
    --function-name telegram-worker \
    --zip-file fileb://worker_lambda.zip \
    --region us-east-1
```

### Обновление webhook_lambda

```bash
zip webhook_lambda.zip webhook_lambda.py

aws lambda update-function-code \
    --function-name telegram-webhook-handler \
    --zip-file fileb://webhook_lambda.zip \
    --region us-east-1
```

### Обновление Lambda Layer

```bash
# Пересоберите layer
cd слой
build_layer.bat

# Опубликуйте новую версию
aws lambda publish-layer-version \
    --layer-name telegram-claude-dependencies \
    --zip-file fileb://lambda_layer.zip \
    --compatible-runtimes python3.11 \
    --region us-east-1

# Обновите worker_lambda новым LayerVersionArn
aws lambda update-function-configuration \
    --function-name telegram-worker \
    --layers <NEW_LayerVersionArn> \
    --region us-east-1
```

## Удаление всех ресурсов

```bash
# Lambda
aws lambda delete-function --function-name telegram-webhook-handler --region us-east-1
aws lambda delete-function --function-name telegram-worker --region us-east-1

# Lambda Layer (все версии)
aws lambda delete-layer-version --layer-name telegram-claude-dependencies --version-number 1 --region us-east-1

# SQS
aws sqs delete-queue --queue-url <QueueUrl> --region us-east-1

# API Gateway
aws apigateway delete-rest-api --rest-api-id <api-id> --region us-east-1

# DynamoDB (ОСТОРОЖНО: удалятся все данные!)
aws dynamodb delete-table --table-name Users --region us-east-1
aws dynamodb delete-table --table-name Channels --region us-east-1
aws dynamodb delete-table --table-name Threads --region us-east-1
aws dynamodb delete-table --table-name Messages --region us-east-1
aws dynamodb delete-table --table-name Summaries --region us-east-1
aws dynamodb delete-table --table-name Settings --region us-east-1

# IAM Role
aws iam detach-role-policy --role-name TelegramBotLambdaRole --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam detach-role-policy --role-name TelegramBotLambdaRole --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess
aws iam detach-role-policy --role-name TelegramBotLambdaRole --policy-arn arn:aws:iam::aws:policy/AmazonSQSFullAccess
aws iam delete-role --role-name TelegramBotLambdaRole

# Удалить webhook из Telegram
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

## Troubleshooting

### Бот не отвечает

1. Проверьте webhook:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```

2. Проверьте CloudWatch Logs webhook_lambda

3. Проверьте SQS очередь:
   ```bash
   aws sqs get-queue-attributes \
       --queue-url <QueueUrl> \
       --attribute-names All \
       --region us-east-1
   ```

4. Проверьте CloudWatch Logs worker_lambda

### Lambda timeout

- Увеличьте timeout до 90 секунд
- Проверьте latency Claude API
- Проверьте размер контекста (MAX_CONTEXT_TOKENS)

### DynamoDB throttling

- Переключитесь на Provisioned mode с Auto Scaling
- Или увеличьте On-Demand capacity limits

### Высокая стоимость

- Проверьте количество запросов к Claude API
- Уменьшите MAX_CONTEXT_TOKENS
- Уменьшите max_tokens в саммаризации
- Проверьте TTL в DynamoDB (должен удалять старые данные)

---

**Последнее обновление:** 2025-01-20
**Версия:** 1.1.0
