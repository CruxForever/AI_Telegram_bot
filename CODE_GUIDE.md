# Руководство по коду

Подробные объяснения ключевой логики для быстрого понимания кода.

## worker_lambda.py

### Функция: dialog_key_for() (строка 39)

**Назначение:** Формирование уникального ключа диалога для изоляции контекстов.

**Логика:**
```python
def dialog_key_for(chat_type, chat_id, user_id, thread_id):
    if chat_type == "private" and user_id:
        return str(user_id)  # Приватный чат → user_id
    if thread_id:
        return f"{chat_id}:{thread_id}"  # Тред в форуме → chat:thread
    return str(chat_id)  # Обычная группа → chat_id
```

**Примеры:**
- Приватный чат с Иваном: `"123456789"`
- Группа "Разработчики": `"-1001234567890"`
- Тред "Python" в форуме: `"-1001234567890:42"`

**Почему важно:**
- Обеспечивает изоляцию диалогов
- Используется как PK в Messages и Summaries
- Используется как MessageGroupId в SQS FIFO

---

### Функция: detect_mention() (строка 49)

**Назначение:** Определение, упомянут ли бот в сообщении.

**Проверяет:**
1. Текст содержит @bot_username
2. Entities типа mention/text_mention/bot_command с @bot_username
3. Reply на сообщение бота (по bot_id или username)

**Пример:**
```python
# Случай 1: прямое упоминание
detect_mention("@mybot привет", [], "mybot") → True

# Случай 2: entity
detect_mention("привет", [{"type": "mention", "offset": 0, "length": 6}], "mybot") → True (если entity = "@mybot")

# Случай 3: reply на бота
detect_mention("ответ", [], "mybot", reply_to={"from_id": 123, "from_is_bot": True}, bot_id=123) → True
```

---

### Функция: should_respond_by_mode() (строка 70)

**Назначение:** Определить, должен ли бот ответить based on настройках.

**Режимы:**
- `off` → никогда не отвечать
- `always` → всегда отвечать (игнорирует mentioned)
- `mention` → только если mentioned
- `""` (пусто) → как always

**Особенность:** В private чатах всегда True (независимо от mode).

---

### Функция: _parse_update() (строка 128)

**Назначение:** Парсинг Telegram update в удобный формат.

**Извлекает:**
- `chat_id`, `chat_type`, `message_id`, `thread_id`
- `user_id`, `username`, `first_name`, `last_name` (для персонализации)
- `text`, `entities`
- `reply_to` (информация о reply сообщении)

**Важно:**
- Поддерживает `message` и `channel_post`
- `first_name`/`last_name` добавлены в v1.1.0 для персонализации
- `reply_to` содержит `from_id`, `from_username`, `from_is_bot`

---

### Функция: _process_one() (строка 161) - ГЛАВНАЯ ФУНКЦИЯ

Обрабатывает одно сообщение от начала до конца. Это core бизнес-логика бота.

#### STEP 0: Парсинг (строка 162)
```python
parsed = _parse_update(update_raw)
# Извлекает все нужные поля из JSON
```

#### STEP 1: Обеспечение сущностей (строки 180-190)
```python
if chat_type == "private":
    # Создать или обновить запись User
    if not get_user(user_id):
        save_user(user_id, username, first_name, last_name)
    else:
        # Обновить имена, если изменились
        update_user_names(user_id, username, first_name, last_name)
```

**Почему обновляем:** Пользователь мог изменить имя/username в Telegram.

#### STEP 2: Сохранение сообщения (строки 192-205)
```python
save_message(
    dkey,              # dialog_key для группировки
    "user",            # role
    text,              # содержимое
    from_user=user_id,
    from_username=username
)
```

**Важно:**
- Сохраняется сразу, до проверки should_respond
- Поле `from_username` сохраняется для будущего использования
- TTL = 1 год (автоматическое удаление)

#### STEP 3: Получение настроек (строки 207-216)
```python
settings = get_settings(dkey)
if not settings:
    # Создать с defaults
    mode = default_mode_for(chat_type)  # "always" для private, "mention" для groups
    save_settings(dkey, mode=mode, meta=None)
```

#### STEP 4: Проверка режима (строки 265-273)
```python
mentioned = detect_mention(text, entities, BOT_USERNAME, reply_to=reply_to, bot_id=BOT_ID)
mode = settings.get("mode")
if not should_respond_by_mode(mode, chat_type, mentioned):
    return "Skipped"  # Не отвечаем
```

**CONTEXT:** Здесь решается, стоит ли тратить ресурсы на генерацию ответа.

#### STEP 5: Формирование контекста (строки 275-440)

**5.1. Базовый system prompt:**
```python
system_parts = []
if BASE_SYSTEM_PROMPT:
    system_parts.append(BASE_SYSTEM_PROMPT)
```

**5.2. Краткая сводка:**
```python
summary = get_latest_summary(dkey)
if summary:
    system_parts.append(f"Dialog summary: {summary}")
```

**5.3. Кеширование профилей (строки 287-299):**
```python
user_profiles_cache = {}  # В рамках execution

def get_cached_profile(uid):
    if uid not in user_profiles_cache:
        profile = get_user_profile(uid)
        user_profiles_cache[uid] = profile
    return user_profiles_cache.get(uid)
```

**ВАЖНО:** Кеш живёт только в рамках одного execution. При следующем сообщении создаётся заново.

**Зачем:** В группе из 10 участников без кеша было бы 10 * N вызовов get_user_profile() при формировании префиксов. С кешем: только 10 (один раз на участника).

**5.4. Персональный контекст для private (строки 301-342):**
```python
if chat_type == "private" and user_id:
    user_profile = get_cached_profile(str(user_id))
    if user_profile and any(user_profile.values()):
        # Формируем блок с:
        # - Стилем общения
        # - Интересами
        # - Долгосрочной памятью
        # - Последними темами
        system_parts.append(profile_context)
```

**5.5. Карта участников для groups (строки 354-388):**
```python
if chat_type != "private":
    # Собрать всех участников из истории
    participants_info = {}
    for m in history:
        if m["role"] == "user":
            fu = m["from_user"]
            profile = get_cached_profile(fu)
            participants_info[fu] = {
                "first_name": profile["first_name"],
                ...
            }
    # Форм

ировать карту:
    # "Участники беседы:"
    # "- Иван (@ivan), ID:123 — текущий автор"
    # "- Мария (@maria), ID:456 — участник"
```

**5.6. Scope инструкции для groups (строки 390-408):**
```python
scope = settings.get("meta", {}).get("group_scope") or GROUP_SCOPE_DEFAULT

if scope == "initiator":
    # Учитывать только сообщения текущего автора
elif scope == "thread":
    # Учитывать всех участников
else:  # hybrid
    # Приоритет автору, но учитывать контекст других
```

**5.7. Обогащение префиксов (строки 410-440):**
```python
for m in history:
    if m["role"] == "user":
        # Получить профиль
        profile = get_cached_profile(m["from_user"])
        name = profile["first_name"] or username or user_id

        # Формат: [Имя (@username), ID:123]
        prefix = f"[{name}"
        if username and name != username:
            prefix += f" (@{username})"
        prefix += f", ID:{user_id}]"

        content = f"{prefix} {original_content}"
```

**До:** `[user:123456] Привет`
**После:** `[Иван (@ivan), ID:123456] Привет`

**5.8. Фильтрация по scope (строки 435-438):**
```python
if chat_type != "private":
    if scope == "initiator" and m["role"] == "user" and str(user_id) != fu:
        continue  # Пропустить сообщения других пользователей
```

**CONTEXT:** В режиме `initiator` модель видит только сообщения текущего автора. В `thread` и `hybrid` - всех.

#### STEP 6: Trim контекста (строки 444-476)

**6.1. Подсчёт токенов:**
```python
def total_tokens():
    return num_tokens_from_messages(_view(chat_msgs), system=system_prompt)
```

**6.2. Умное удаление:**
```python
while total_tokens() > MAX_CONTEXT_TOKENS and len(chat_msgs) > 1:
    # В hybrid mode сначала удаляем сообщения не-инициаторов
    if chat_type != "private" and scope == "hybrid":
        if _drop_oldest_non_initiator_user():
            continue
    # Потом удаляем пары user+assistant
    _drop_oldest_turn()
```

**Логика:** Сохраняем максимум контекста от текущего автора запроса, жертвуя сообщениями других участников.

**6.3. Саммаризация обрезанного (строки 478-487):**
```python
if removed_turns:
    # Создать summary из удалённых сообщений
    sm = summarize_history(removed_turns[-16:])
    if sm:
        save_summary(dkey, sm)
        # Добавить в system prompt
        system_parts.append(f"Earlier dialog summary: {sm}")
```

**CONTEXT:** Удалённые сообщения не теряются - они сжимаются в summary.

#### STEP 7: Генерация ответа (строка 492)
```python
ai_resp = generate_response(messages, system=system_prompt, max_tokens=MAX_OUTPUT_TOKENS)
```

**Latency:** 5-30 секунд (зависит от Claude API).

**Fallback:**
```python
if ai_resp.strip().lower() in {"assistant", "system", "user", ""}:
    # Модель вернула placeholder вместо текста
    ai_resp = "⚠️ Пустой ответ модели. Зафиксировал это в логах."
```

#### STEP 8: Отправка в Telegram (строки 502-507)
```python
for part in split_telegram(ai_resp):
    send_message(chat_id, part, chat_type=chat_type, thread_id=thread_id, reply_to=msg_id)
```

**split_telegram():** Разбивает длинные сообщения на части по 4000 символов (лимит Telegram).

#### STEP 9: Обновление профилей (строки 509-551)

**9.1. Счётчик сообщений:**
```python
if user_id:
    update_user_profile(str(user_id), increment_messages=True)
```

**9.2. Краткая саммаризация (каждые 12+ сообщений):**
```python
full = get_dialog_history(dkey, limit=MIN_MSGS_FOR_SUMMARY * 2)
if len(full) >= MIN_MSGS_FOR_SUMMARY:
    sm = summarize_history(full[-SUMMARY_HISTORY_LIMIT:])
    save_summary(dkey, sm)
```

**9.3. Долгосрочный профиль (каждые 50+ сообщений, только private):**
```python
if chat_type == "private" and user_id and len(full) >= 50:
    user_info = {
        "first_name": profile["first_name"],
        "username": username,
    }

    # Создать долгосрочную сводку (анализ интересов, стиля)
    long_summary = create_long_term_summary(full[-80:], user_info)

    # Извлечь темы
    recent_topics = extract_topics(full[-20:])

    # Обновить профиль
    update_user_profile(
        str(user_id),
        long_term_summary=long_summary,
        last_topics=recent_topics
    )
```

**ВАЖНО:** Это происходит асинхронно после отправки ответа пользователю.

---

### Функция: split_telegram() (строка 116)

**Назначение:** Разбить длинное сообщение на части для Telegram.

**Лимит Telegram:** 4096 символов, но используем 4000 для запаса.

**Алгоритм:**
```python
def split_telegram(text, limit=4000):
    buf = []
    total = 0
    for line in text.splitlines(True):  # Сохраняем \n
        if total + len(line) > limit and buf:
            yield "".join(buf)  # Отправить часть
            buf, total = [], 0
        buf.append(line)
        total += len(line)
    if buf:
        yield "".join(buf)  # Последняя часть
```

**Особенность:** Разбивает по строкам, не ломает слова.

---

## claude_utils.py

### Функция: _init_client() (строка 15)

**Назначение:** Ленивая инициализация Anthropic SDK client.

**Почему ленивая:**
```python
_client = None  # Global

def _init_client():
    global _client
    if _client is not None:
        return  # Уже инициализирован

    api_key = os.getenv("ANTHROPIC_API_KEY")
    _client = anthropic.Anthropic(api_key=api_key)
```

**CONTEXT:** Lambda может переиспользовать execution environment. Client создаётся один раз при первом вызове, потом переиспользуется.

---

### Функция: _ensure_alternation() (строка 48)

**Назначение:** Claude API требует строгое чередование user/assistant.

**Проблема:**
```python
# Невалидно для Claude:
[{"role": "user", "content": "A"},
 {"role": "user", "content": "B"},  # ← две user подряд
 {"role": "assistant", "content": "C"}]
```

**Решение:**
```python
# Склеиваем подряд идущие сообщения
[{"role": "user", "content": "A\nB"},  # ← склеено
 {"role": "assistant", "content": "C"}]
```

**Также проверяет:**
```python
if merged[0]["role"] != "user":
    # Claude требует, чтобы первое сообщение было user
    merged.insert(0, {"role": "user", "content": "(начало диалога)"})
```

---

### Функция: summarize_history() (строка 114)

**Назначение:** Создать сводку диалога с сохранением персонального контекста.

**Улучшения в v1.1.0:**
- **30 сообщений** вместо 16 (+87%)
- **600 токенов** вместо 200 (+200%)
- **Умный промпт** с инструкцией сохранять имена и стиль
- **user_context** опционально добавляет информацию о пользователе

**Промпт:**
```
Создай подробное резюме диалога на русском языке.

ВАЖНО - сохрани персональный контекст:
- Имена участников и их стиль общения
- Ключевые темы, вопросы и принятые решения
- Эмоциональный тон разговора
- Незавершённые вопросы или задачи
- Интересы и предпочтения пользователей
```

**Температура:** 0.3 (более детерминированный результат, чем 0.5).

---

### Функция: create_long_term_summary() (строка 147)

**Назначение:** Создать долгосрочный профиль пользователя.

**Анализирует:**
- **60 последних сообщений** (больший контекст, чем краткая сводка)
- **Стиль общения** (формальный/неформальный, технический/разговорный)
- **Интересы и темы**
- **Проекты и задачи**

**Выход:** 1-2 абзаца профиля (400 токенов).

**Пример результата:**
```
Иван - разработчик с техническим стилем общения. Предпочитает прямые,
конкретные ответы без лишних слов. Работает над телеграм-ботом с
асинхронной обработкой через AWS SQS. Интересуется AWS Lambda, DynamoDB,
Claude API, Docker. Обсуждал вопросы сборки Lambda Layer, персонализации
контекста модели, сохранения истории чатов.
```

---

### Функция: extract_topics() (строка 173)

**Назначение:** Извлечь ключевые темы из недавних сообщений.

**Промпт:**
```
Проанализируй последние сообщения и выдели 5 основных тем или ключевых слов.

Верни ТОЛЬКО список тем через запятую, без нумерации и пояснений.
Пример: Python, AWS Lambda, DynamoDB, Claude API, Телеграм боты
```

**Парсинг:**
```python
result = _chat(...)  # "Python, AWS, Docker, DynamoDB"
topics = [t.strip() for t in result.split(",") if t.strip()]
return topics[:max_topics]  # ["Python", "AWS", "Docker", "DynamoDB"]
```

**Обработка ошибок:**
```python
try:
    ...
except Exception:
    return []  # Пустой список при ошибке
```

**CONTEXT:** Не критично если упадёт - просто не обновятся темы.

---

## Ключевые паттерны и best practices

### 1. Graceful degradation

Код не падает при ошибках DynamoDB/Claude API:

```python
try:
    save_summary(dkey, sm)
except Exception as e:
    logger.warning("Save summary failed: %s", e)
    # Продолжаем работу
```

### 2. Defensive programming

Проверки на None и пустые значения:

```python
if user_profile and any(user_profile.values()):
    # Используем только если профиль не пустой
    ...
```

### 3. Кеширование в рамках execution

```python
user_profiles_cache = {}  # Локальный словарь
def get_cached_profile(uid):
    # Избегает повторных запросов к DynamoDB
    ...
```

### 4. Lazy evaluation

```python
def total_tokens():
    return num_tokens_from_messages(...)  # Вычисляется только когда вызывается

while total_tokens() > MAX:  # Пересчитывается на каждой итерации
    drop_oldest()
```

### 5. Early return

```python
if not (chat_id and msg_id):
    return "No-op"  # Выходим рано, если нет нужных данных

if not should_respond_by_mode(...):
    return "Skipped"  # Не тратим ресурсы
```

---

**Последнее обновление:** 2025-01-20
**Версия:** 1.1.0
