# Архитектура AI Petrovich

Детальное описание архитектуры проекта и принципов проектирования.

## Содержание

- [Обзор системы](#обзор-системы)
- [Компоненты](#компоненты)
- [Потоки данных](#потоки-данных)
- [Модель данных](#модель-данных)
- [Персонализация](#персонализация)
- [Масштабируемость](#масштабируемость)
- [Принципы проектирования](#принципы-проектирования)

## Обзор системы

### Высокоуровневая архитектура

```
┌──────────┐
│ Telegram │
│   User   │
└────┬─────┘
     │ Message
     ↓
┌──────────────────┐
│  Telegram Bot    │
│   API Server     │
└────┬─────────────┘
     │ Webhook POST
     ↓
┌──────────────────┐      ┌─────────────┐
│   API Gateway    │──────│   Lambda    │
│   /webhook       │      │  Function   │
└──────────────────┘      │ Authorizer  │
     │                    └─────────────┘
     ↓
┌──────────────────┐
│ webhook_lambda   │
│  - Parse update  │
│  - Validate      │
│  - Enqueue       │
└────┬─────────────┘
     │ SendMessage
     ↓
┌──────────────────┐
│   Amazon SQS     │
│   FIFO Queue     │
│  - Deduplication │
│  - GroupId       │
└────┬─────────────┘
     │ Event Source Mapping
     │ (batch_size=1)
     ↓
┌──────────────────┐      ┌─────────────┐
│ worker_lambda    │◄─────│  DynamoDB   │
│  - Process       │      │   Tables    │
│  - Context       │      │  - Users    │
│  - Generate      │      │  - Messages │
│  - Respond       │      │  - Settings │
└────┬─────────────┘      └─────────────┘
     │ API Call
     ↓
┌──────────────────┐
│  Anthropic       │
│  Claude API      │
│  Sonnet 4.5      │
└────┬─────────────┘
     │ Response
     ↓
┌──────────────────┐
│  Telegram Bot    │
│  sendMessage API │
└────┬─────────────┘
     │
     ↓
┌──────────────────┐
│    Telegram      │
│      User        │
└──────────────────┘
```

### Почему асинхронная архитектура?

1. **Telegram webhook timeout** - 60 секунд
2. **Claude API latency** - может быть 10-30 секунд
3. **DynamoDB operations** - дополнительное время
4. **Масштабируемость** - независимая обработка сообщений

**Решение:** Webhook быстро отвечает 200 OK, а обработка происходит асинхронно через SQS.

## Компоненты

### 1. webhook_lambda (Entry Point)

**Назначение:** Быстрый приём webhook, минимальная обработка, отправка в очередь.

**Функции:**
- Парсинг Telegram update
- Извлечение dialog_key для группировки
- Отправка в SQS с MessageGroupId (для FIFO)
- Дедупликация через update_id

**Latency:** < 100ms

**Файл:** `webhook_lambda.py` (77 строк)

**Ключевые функции:**
```python
def _parse_for_keys(update):
    # Извлекает: dialog_key, chat_id, user_id, thread_id
    # Используется для MessageGroupId в FIFO
    pass

def lambda_handler(event, context):
    # 1. Parse webhook body
    # 2. Send to SQS
    # 3. Return 200 OK immediately
    pass
```

**Зависимости:**
- boto3 (SQS client)

### 2. worker_lambda (Core Logic)

**Назначение:** Обработка сообщений, формирование контекста, генерация ответов.

**Latency:** 5-30 секунд (зависит от Claude API)

**Файл:** `worker_lambda.py` (570+ строк)

#### Основной поток (_process_one):

```python
def _process_one(update_raw: str) -> str:
    # STEP 0: Parse update
    parsed = _parse_update(update_raw)
    # Извлекает: chat_id, user_id, username, first_name, last_name, text

    # STEP 1: Ensure entities (Users/Channels/Threads)
    # Создаёт или обновляет записи в DynamoDB
    if chat_type == "private":
        save_user() or update_user_names()

    # STEP 2: Save incoming message
    save_message(dkey, "user", text, from_user=user_id, from_username=username)

    # STEP 3: Get settings (mode, scope)
    settings = get_settings(dkey)
    # mode: always | mention | off
    # meta.group_scope: initiator | thread | hybrid

    # STEP 4: Check if should respond
    mentioned = detect_mention(text, entities, BOT_USERNAME)
    if not should_respond_by_mode(mode, chat_type, mentioned):
        return "Skipped"

    # STEP 5: Build context
    # 5.1. Load user profile (for private) or participants map (for groups)
    # 5.2. Get dialog history (limit=120)
    # 5.3. Enrich message prefixes with names
    # 5.4. Trim to MAX_CONTEXT_TOKENS

    # STEP 6: Generate response
    ai_resp = generate_response(messages, system=system_prompt)

    # STEP 7: Send to Telegram
    send_message(chat_id, ai_resp, thread_id=thread_id, reply_to=msg_id)

    # STEP 8: Update profiles and summaries
    # - Increment message_count
    # - Create summary (every 12+ messages)
    # - Update long_term profile (every 50+ messages)

    return "OK"
```

#### Формирование контекста (ключевая логика):

```python
# Кеширование профилей
user_profiles_cache = {}
def get_cached_profile(uid):
    # Загружает профиль один раз за execution
    pass

# Для private чатов
if chat_type == "private":
    user_profile = get_cached_profile(user_id)
    # Добавляет в system prompt:
    # - Стиль общения
    # - Интересы
    # - Долгосрочную память
    # - Последние темы

# Для групповых чатов
if chat_type != "private":
    participants_info = {}
    # Собирает всех участников из истории
    for m in history:
        if m["role"] == "user":
            profile = get_cached_profile(m["from_user"])
            participants_info[user_id] = {
                "first_name": profile["first_name"],
                ...
            }
    # Формирует карту: "Участники беседы: - Иван (@ivan), ID:123 ..."

# Обогащение префиксов
for m in history:
    if m["role"] == "user":
        profile = get_cached_profile(m["from_user"])
        name = profile["first_name"] or username or user_id
        prefix = f"[{name} (@{username}), ID:{user_id}]"
        content = f"{prefix} {original_content}"
```

**Зависимости:**
- dynamo_utils (CRUD операции)
- claude_utils (генерация, саммаризация)
- telegram_utils (отправка сообщений)

### 3. claude_utils (AI Integration)

**Назначение:** Обёртка над Anthropic Claude API, саммаризация, извлечение тем.

**Файл:** `claude_utils.py` (189 строк)

**Функции:**

```python
def generate_response(messages, system, max_tokens=800):
    # Генерация ответа пользователю
    # - Ensure alternation (user/assistant)
    # - Call Claude API
    # - Extract text from response
    pass

def summarize_history(history, user_context=None):
    # Краткая сводка диалога
    # - 30 последних сообщений
    # - 600 токенов
    # - Сохраняет имена и стиль
    pass

def create_long_term_summary(history, user_info):
    # Долгосрочный профиль пользователя
    # - 60 последних сообщений
    # - 400 токенов
    # - Анализ интересов, стиля, проектов
    pass

def extract_topics(messages, max_topics=5):
    # Извлечение ключевых тем
    # - 10 последних сообщений
    # - 100 токенов
    # - Возвращает список строк
    pass
```

**Модель:** claude-sonnet-4-5-20250929

**Почему Sonnet, а не Opus:**
- Баланс качества и скорости
- Latency: ~10-15 секунд vs ~20-30 для Opus
- Стоимость: ~5x дешевле
- Контекст: 200k токенов (достаточно)

### 4. dynamo_utils (Data Layer)

**Назначение:** Абстракция над DynamoDB, CRUD операции.

**Файл:** `dynamo_utils.py` (234 строки)

**Паттерны:**

```python
# Get operations
def get_user(user_id):
    return users_tbl.get_item(Key={"user_id": user_id})

# Save operations
def save_user(user_id, username, first_name, last_name):
    # Put with default profile structure
    pass

# Update operations
def update_user_profile(user_id, *, long_term_summary=None, last_topics=None):
    # UpdateExpression for partial updates
    # Using ExpressionAttributeNames for nested fields
    pass

# Query operations
def get_dialog_history(dialog_key, limit=50, consistent_read=False):
    # Query with ScanIndexForward=False (newest first)
    # Then sort ascending for chat order
    pass
```

**Оптимизации:**
- `consistent_read=True` только при формировании контекста
- Batch operations не используются (не нужны для 1 user за раз)
- TTL для автоматической очистки старых сообщений

### 5. telegram_utils (Telegram API)

**Назначение:** Отправка сообщений в Telegram.

**Файл:** `telegram_utils.py` (79 строк)

**Функции:**

```python
def send_message(chat_id, text, *, chat_type, thread_id, reply_to):
    # Фильтр: не создаёт новые посты в каналах
    if chat_type == "channel" and not thread_id and not reply_to:
        return  # Skip

    # Поддержка тредов и reply
    payload = {
        "chat_id": chat_id,
        "text": text,
        "message_thread_id": thread_id,  # Для форумов
        "reply_to_message_id": reply_to,  # Для ответов
    }
    requests.post(SEND_URL, json=payload)

def send_chat_action(chat_id, *, action="typing", thread_id):
    # Показывает "печатает..." пока генерируется ответ
    pass
```

## Потоки данных

### Поток 1: Новое сообщение от пользователя

```
User → Telegram → Webhook → webhook_lambda
                                  ↓
                          SQS (FIFO, GroupId=dialog_key)
                                  ↓
                             worker_lambda
                                  ↓
                    ┌───────────────────────────┐
                    │  1. Parse update          │
                    │  2. Save to Messages      │
                    │  3. Load history          │
                    │  4. Build context         │
                    │  5. Call Claude API       │
                    │  6. Save assistant msg    │
                    │  7. Send to Telegram      │
                    │  8. Update profile        │
                    └───────────────────────────┘
                                  ↓
                    User receives response
```

### Поток 2: Обновление профиля пользователя

```
worker_lambda processes message
        ↓
Increment message_count (every message)
        ↓
IF len(history) >= 12:
    ├─→ summarize_history() → Save to Summaries
    │
IF len(history) >= 50 (private chat):
    ├─→ create_long_term_summary() → Update Users.profile.long_term_summary
    └─→ extract_topics() → Update Users.profile.last_topics
```

### Поток 3: Формирование контекста для Claude

```
Load from DynamoDB:
  ├─→ Settings (mode, scope)
  ├─→ Latest Summary
  ├─→ Dialog History (120 messages)
  └─→ User Profiles (all participants)

Build System Prompt:
  ├─→ BASE_SYSTEM_PROMPT
  ├─→ Summary (if exists)
  ├─→ User Profile (for private) OR Participants Map (for groups)
  └─→ Scope instructions (for groups)

Build Messages:
  ├─→ Enrich with names: [Иван (@ivan), ID:123] Text
  ├─→ Filter by scope (initiator/thread/hybrid)
  └─→ Trim to MAX_CONTEXT_TOKENS (6000)

Send to Claude:
  └─→ messages + system + max_tokens=800
```

## Модель данных

### DynamoDB Tables Design

#### Users
```
PK: user_id (String)

Attributes:
- username (String)
- profile (Map):
  - first_name (String)
  - last_name (String)
  - communication_style (String)
  - interests (List<String>)
  - long_term_summary (String)
  - last_topics (List<String>)
  - message_count (Number)
- created_at (String, ISO)
- updated_at (String, ISO)
```

#### Messages
```
PK: dialog_key (String) - user_id для private, chat_id для групп
SK: timestamp (Number) - milliseconds

Attributes:
- role (String) - "user" | "assistant"
- content (String) - текст сообщения
- from_user (String) - user_id отправителя
- from_username (String) - @username
- to_user (String) - для assistant: кому адресован
- created_at (String, ISO)
- expire_at (Number, TTL) - Unix timestamp + 1 год

Indexes: None (query by PK+SK достаточно)
```

#### Settings
```
PK: dialog_key (String)

Attributes:
- mode (String) - "always" | "mention" | "off"
- meta (Map):
  - group_scope (String) - "initiator" | "thread" | "hybrid"
- created_at (String, ISO)
- updated_at (String, ISO)
```

#### Summaries
```
PK: dialog_key (String)
SK: timestamp (Number) - milliseconds

Attributes:
- summary (String) - текст сводки
- created_at (String, ISO)
- expire_at (Number, TTL)
```

### Ключи dialog_key

**Логика:**
```python
def dialog_key_for(chat_type, chat_id, user_id, thread_id):
    if chat_type == "private":
        return str(user_id)  # Приватный чат = user_id
    elif thread_id:
        return f"{chat_id}:{thread_id}"  # Тред в группе/форуме
    else:
        return str(chat_id)  # Группа/канал без треда
```

**Примеры:**
- Private: `123456789`
- Group: `-1001234567890`
- Thread: `-1001234567890:42`

Это обеспечивает:
- Изоляцию диалогов
- Правильную работу в форумах
- Уникальность MessageGroupId для FIFO

## Персонализация

### Уровни контекста

#### 1. Краткосрочный (текущая сессия)
- История 120 последних сообщений
- Trim до 6000 токенов
- Обогащение префиксами с именами

#### 2. Среднесрочный (последние сессии)
- Summaries из обрезанных сообщений
- Краткая сводка каждые 12+ сообщений
- Добавляется в system prompt

#### 3. Долгосрочный (весь опыт общения)
- User profile с:
  - Стиль общения (определяется автоматически)
  - Интересы (извлекаются из диалогов)
  - Долгосрочная сводка (каждые 50 сообщений)
  - Последние темы (каждые 50 сообщений)
- Сохраняется между сессиями

### Персонализация в действии

**Первое сообщение:**
```
System: <BASE_SYSTEM_PROMPT>

[Иван (@ivan_petrov), ID:123456] Привет!
```

**После 50 сообщений:**
```
System: <BASE_SYSTEM_PROMPT>

О собеседнике (Иван @ivan_petrov):
- Стиль общения: Технический, прямой, без лишних слов
- Интересы: Python, AWS Lambda, DynamoDB, Claude API
- Контекст прошлых бесед: Иван разрабатывает телеграм-бота
  с асинхронной обработкой через SQS. Обсуждали Lambda Layer,
  Docker, персонализацию контекста модели.
- Последние темы: requirements.txt, Dockerfile, Python 3.11

Dialog summary: Иван спрашивал о сборке Lambda Layer через Docker...

[Иван (@ivan_petrov), ID:123456] Помнишь про тот баг?
```

## Масштабируемость

### Горизонтальное масштабирование

**SQS + Lambda:**
- SQS FIFO гарантирует порядок внутри MessageGroupId
- Lambda автоматически масштабируется до 1000 concurrent executions
- Каждый dialog_key обрабатывается последовательно (FIFO)
- Разные dialog_key параллельно

**Пример:**
- 1000 пользователей пишут одновременно
- 1000 Lambda instances запускаются параллельно
- Каждый обрабатывает свой dialog_key

**DynamoDB:**
- On-demand billing mode - автоматическое масштабирование
- Нет лимитов на RCU/WCU
- Query по PK+SK эффективны

### Вертикальное масштабирование

**Lambda Memory:**
- Текущая: 512 MB
- Можно увеличить до 10 GB
- CPU масштабируется пропорционально памяти

**Почему 512 MB достаточно:**
- Профили кешируются (не более 10-20 KB на пользователя)
- История 120 сообщений ≈ 50-100 KB
- Claude API SDK ≈ 50 MB
- Всего: ~150 MB peak usage

### Оптимизации

**Кеширование профилей:**
```python
user_profiles_cache = {}  # В рамках execution
# Избегает повторных запросов к DynamoDB
# Для группы из 10 участников: 1 execution = 10 get_user_profile() = 10 reads
# С кешем: 1 execution = 10 уникальных users = 10 reads (те же, но без дублей)
```

**Consistent reads только когда нужно:**
```python
get_dialog_history(dkey, consistent_read=True)  # При формировании контекста
get_dialog_history(dkey, consistent_read=False)  # При обновлении summary
```

**Trim с приоритетом:**
```python
while total_tokens() > MAX_CONTEXT_TOKENS:
    # 1. Сначала удаляет сообщения не-инициаторов (hybrid mode)
    if scope == "hybrid":
        _drop_oldest_non_initiator_user()
    # 2. Потом удаляет пары user+assistant
    else:
        _drop_oldest_turn()
```

## Принципы проектирования

### 1. Separation of Concerns
- Webhook - только приём и отправка
- Worker - вся бизнес-логика
- Utils - переиспользуемые компоненты

### 2. Fail-Safe
- Webhook всегда возвращает 200 OK (иначе Telegram повторит)
- SQS хранит message до обработки (visibility timeout)
- TTL в DynamoDB для автоматической очистки

### 3. Idempotency
- SQS FIFO ContentBasedDeduplication
- MessageDeduplicationId = update_id от Telegram

### 4. Performance
- Асинхронная обработка
- Кеширование в рамках execution
- Эффективные DynamoDB queries

### 5. Cost Optimization
- On-demand DynamoDB (платим за использование)
- Lambda timeout 60 сек (не больше, чем нужно)
- TTL для автоматической очистки (не платим за storage старых данных)

### 6. Extensibility
- Легко добавить новые таблицы
- Модульная структура utils
- Конфигурация через env vars

---

**Последнее обновление:** 2025-01-20
**Версия:** 1.1.0
