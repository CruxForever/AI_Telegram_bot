
import datetime
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from dynamo_utils import (
    get_user, save_user,
    get_channel, save_channel,
    get_thread, save_thread,
    save_message, get_dialog_history,
    get_latest_summary, get_latest_summary_item, save_summary,
    get_settings, save_settings, update_settings,
    update_user_names,
    update_user_profile,
    get_user_profile,
    get_user_facts, add_user_fact, remove_user_facts,
)
from claude_utils import (
    num_tokens_from_messages,
    generate_response,
    summarize_history,
    create_long_term_summary,
    extract_topics,
)
from telegram_utils import send_message, send_chat_action, get_file_base64, get_file_bytes

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "6000"))
MAX_OUTPUT_TOKENS  = int(os.getenv("MAX_OUTPUT_TOKENS",  "800"))
MIN_MSGS_FOR_SUMMARY   = int(os.getenv("MIN_MSGS_FOR_SUMMARY", "12"))
SUMMARY_HISTORY_LIMIT  = int(os.getenv("SUMMARY_HISTORY_LIMIT", "60"))
# Троттлинг: не перегенерировать краткую сводку чаще, чем раз в N секунд (раньше — каждый ход)
SUMMARY_MIN_INTERVAL_SEC = int(os.getenv("SUMMARY_MIN_INTERVAL_SEC", "600"))
# Долгосрочный профиль (private) обновляется раз в LONG_TERM_EVERY сообщений
LONG_TERM_EVERY = int(os.getenv("LONG_TERM_EVERY", "50"))
BASE_SYSTEM_PROMPT     = os.getenv("BASE_SYSTEM_PROMPT", "").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@").lower()
BOT_ID = int(os.getenv("BOT_ID", "0")) or None
GROUP_SCOPE_DEFAULT = os.getenv("GROUP_SCOPE_DEFAULT", "hybrid").lower()

OPENWEATHERMAP_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY", "")
WEATHER_DEFAULT_CITY   = os.getenv("WEATHER_DEFAULT_CITY", "Moscow")

# Веб-поиск (серверный инструмент Anthropic). Включён по умолчанию, отключается env-флагом.
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1") == "1"
WEB_SEARCH_MAX_USES = int(os.getenv("WEB_SEARCH_MAX_USES", "3"))

# Распознавание изображений
VISION_ENABLED = os.getenv("VISION_ENABLED", "1") == "1"

# Распознавание голосовых: Claude API аудио не принимает, транскрибируем через
# OpenAI Whisper (requests уже в слое, новых зависимостей нет).
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "1") == "1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
VOICE_MAX_DURATION_SEC = int(os.getenv("VOICE_MAX_DURATION_SEC", "300"))


def _transcribe_voice(file_id: str, duration: int) -> Optional[str]:
    """Скачивает голосовое из Telegram и транскрибирует через OpenAI STT.
    Возвращает текст или None (нет ключа / слишком длинное / ошибка)."""
    if not OPENAI_API_KEY:
        logger.warning("Voice skipped: OPENAI_API_KEY is not set")
        return None
    if duration and duration > VOICE_MAX_DURATION_SEC:
        logger.warning("Voice skipped: too long (%ds > %ds)", duration, VOICE_MAX_DURATION_SEC)
        return None
    data, file_path = get_file_bytes(file_id)
    if not data:
        return None
    ext = (file_path.rsplit(".", 1)[-1] if file_path and "." in file_path else "ogg").lower()
    if ext == "oga":  # telegram-голосовые приходят .oga; OpenAI знает это как ogg
        ext = "ogg"
    try:
        import requests
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            data={"model": STT_MODEL, "response_format": "text"},
            files={"file": (f"voice.{ext}", data, f"audio/{ext}")},
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("STT error %s: %s", r.status_code, r.text[:200])
            return None
        transcript = (r.text or "").strip()
        return transcript or None
    except Exception as e:
        logger.warning("STT request failed: %s", e)
        return None

# Стиль-файрвол: история переписки даётся модели как факты/контекст, но её ФОРМА не должна
# копироваться. Ставится последним блоком системного промпта (макс. салиентность), чтобы
# разорвать петлю самоповтора шаблонного формата ответов (см. CHANGELOG 1.3.1).
STYLE_ANCHOR = (
    "Важно про стиль ответа: историю переписки ниже используй только как факты и контекст — "
    "НЕ копируй её формат и оформление. Каждый ответ пиши заново, живым разговорным текстом. "
    "Не используй шаблонные секции и заголовки («Краткий вывод», «Критика», «Почему это хорошо», "
    "«Альтернативы», «Итог», «Уверенность»), не начинай ответ с «Ясно —», не оценивай свою "
    "уверенность и не разбирай свои ошибки, если тебя об этом прямо не попросили."
)

# ---- Weather tool ----

def _fetch_weather(city: str) -> str:
    """Запрашивает текущую погоду через OpenWeatherMap API."""
    if not OPENWEATHERMAP_API_KEY:
        return "Погода недоступна: не настроен OPENWEATHERMAP_API_KEY"
    try:
        import requests
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?q={city}&appid={OPENWEATHERMAP_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url, timeout=5)
        d = r.json()
        if r.status_code == 200:
            return (
                f"Погода в {d['name']}: {d['weather'][0]['description']}, "
                f"{d['main']['temp']:.0f}°C (ощущается {d['main']['feels_like']:.0f}°C), "
                f"влажность {d['main']['humidity']}%, ветер {d['wind']['speed']} м/с"
            )
        elif r.status_code == 404:
            return f"Город '{city}' не найден. Уточни название."
        else:
            return f"Ошибка погоды: {d.get('message', r.status_code)}"
    except Exception as e:
        logger.warning("Weather fetch failed: %s", e)
        return f"Не удалось получить погоду: {e}"


def _fetch_forecast(city: str, day: str = "tomorrow") -> str:
    """Прогноз погоды на сегодня/завтра через OpenWeatherMap (5-day/3-hour)."""
    if not OPENWEATHERMAP_API_KEY:
        return "Прогноз недоступен: не настроен OPENWEATHERMAP_API_KEY"
    try:
        import requests
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?q={city}&appid={OPENWEATHERMAP_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url, timeout=6)
        d = r.json()
        if r.status_code == 404:
            return f"Город '{city}' не найден. Уточни название."
        if r.status_code != 200:
            return f"Ошибка прогноза: {d.get('message', r.status_code)}"

        # Локальное время города из смещения в ответе
        tz = datetime.timezone(datetime.timedelta(seconds=int(d.get("city", {}).get("timezone", 0))))
        today = datetime.datetime.now(tz).date()
        target = today + datetime.timedelta(days=0 if day == "today" else 1)

        temps, descs = [], {}
        for item in d.get("list", []):
            t = datetime.datetime.fromtimestamp(item["dt"], tz)
            if t.date() != target:
                continue
            temps.append(item["main"]["temp"])
            desc = item["weather"][0]["description"]
            descs[desc] = descs.get(desc, 0) + 1
        if not temps:
            return f"Нет данных прогноза на {target.strftime('%d.%m')} для {city}."

        main_desc = max(descs, key=descs.get) if descs else ""
        name = d.get("city", {}).get("name", city)
        label = "сегодня" if day == "today" else "завтра"
        return (
            f"Прогноз в {name} на {label} ({target.strftime('%d.%m')}): "
            f"{main_desc}, от {min(temps):.0f}°C до {max(temps):.0f}°C"
        )
    except Exception as e:
        logger.warning("Forecast fetch failed: %s", e)
        return f"Не удалось получить прогноз: {e}"


WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Получить текущую погоду в указанном городе",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "Название города (например: Moscow, Москва, London, Санкт-Петербург)",
            }
        },
        "required": ["city"],
    },
}

FORECAST_TOOL = {
    "name": "get_forecast",
    "description": "Получить прогноз погоды на сегодня или завтра в указанном городе",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "Название города (например: Moscow, Москва, London)",
            },
            "day": {
                "type": "string",
                "enum": ["today", "tomorrow"],
                "description": "На какой день нужен прогноз (по умолчанию tomorrow)",
            },
        },
        "required": ["city"],
    },
}

REMEMBER_TOOL = {
    "name": "remember_fact",
    "description": (
        "Сохранить в долговременную память важный устойчивый факт о собеседнике "
        "(предпочтение, деталь о работе/жизни, договорённость), чтобы помнить его в будущих беседах. "
        "Используй, когда собеседник просит запомнить или сообщает значимую устойчивую информацию о себе. "
        "Не сохраняй сиюминутное и неважное."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "Краткий факт от третьего лица, напр.: 'Любит крепкий чёрный кофе без сахара' "
                    "или 'Работает в отделе контроллинга'"
                ),
            }
        },
        "required": ["fact"],
    },
}

FORGET_TOOL = {
    "name": "forget_fact",
    "description": (
        "Удалить ранее сохранённый факт о собеседнике из долговременной памяти. "
        "Используй, когда собеседник просит что-то забыть или информация устарела."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Ключевые слова факта, который нужно забыть. Пусто — забыть всё.",
            }
        },
        "required": [],
    },
}

# Серверный инструмент Anthropic — выполняется на стороне API.
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}


def _make_tool_executor(user_id: Optional[str]):
    """Создаёт роутер инструментов, замкнутый на текущего пользователя (для памяти)."""

    def _route(tool_name: str, tool_input: Dict[str, Any]) -> str:
        if tool_name == "get_weather":
            return _fetch_weather(tool_input.get("city") or WEATHER_DEFAULT_CITY)
        if tool_name == "get_forecast":
            return _fetch_forecast(
                tool_input.get("city") or WEATHER_DEFAULT_CITY,
                (tool_input.get("day") or "tomorrow"),
            )
        if tool_name == "remember_fact":
            if not user_id:
                return "Не могу сохранить: неизвестен пользователь."
            fact = (tool_input.get("fact") or "").strip()
            if not fact:
                return "Пустой факт — нечего запоминать."
            return "Запомнил." if add_user_fact(user_id, fact) else "Не удалось сохранить факт."
        if tool_name == "forget_fact":
            if not user_id:
                return "Не могу: неизвестен пользователь."
            n = remove_user_facts(user_id, tool_input.get("query"))
            return f"Забыл ({n})." if n else "Подходящих фактов не нашёл."
        return f"Неизвестный инструмент: {tool_name}"

    return _route


def dialog_key_for(chat_type: Optional[str], chat_id: int, user_id: Optional[int], thread_id: Optional[int]) -> str:
    if chat_type == "private" and user_id:
        return str(user_id)
    if thread_id:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)

def default_mode_for(chat_type: Optional[str]) -> str:
    return "always" if chat_type == "private" else "mention"

def detect_mention(text: str, entities: list, bot_username: str,
                   *, reply_to: Optional[Dict[str, Any]] = None,
                   bot_id: Optional[int] = None) -> bool:
    if not text:
        text = ""
    low = text.lower()
    uname = (bot_username or "").lower()
    if uname and ("@" + uname) in low:
        return True
    for e in (entities or []):
        et = e.get("type")
        if et in ("mention", "text_mention", "bot_command"):
            try:
                off, ln = int(e.get("offset", 0)), int(e.get("length", 0))
                frag = text[off:off+ln].lower()
            except Exception:
                frag = ""
            if uname and (("@" + uname) in frag):
                return True
    if reply_to and reply_to.get("from_is_bot"):
        if bot_id and reply_to.get("from_id") == bot_id:
            return True
        if uname and (reply_to.get("from_username", "").lower() == uname):
            return True
    return False

def should_respond_by_mode(mode: str, chat_type: Optional[str], mentioned: bool) -> bool:
    mode = (mode or "").lower()
    if mode == "off":
        return False
    if chat_type == "private":
        return True
    if mode in ("always", ""):
        return True
    if mode == "mention":
        return bool(mentioned)
    return bool(mentioned)

def parse_mode_command(text: str, bot_username: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    if not t.startswith("/mode"):
        return None
    parts = t.split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if ("@" + bot_username) in cmd or cmd == "/mode":
        if len(parts) >= 2:
            candidate = parts[1].lower()
            if candidate in ("always", "mention", "off"):
                return candidate
    return None

def parse_scope_command(text: str, bot_username: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    if not t.startswith("/scope"):
        return None
    parts = t.split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if ("@" + bot_username) in cmd or cmd == "/scope":
        if len(parts) >= 2:
            candidate = parts[1].lower()
            if candidate in ("initiator", "thread", "hybrid"):
                return candidate
    return None

def split_telegram(text: str, limit: int = 4000):
    if not text:
        return
    buf, total = [], 0
    for line in text.splitlines(True):
        if total + len(line) > limit and buf:
            yield "".join(buf)
            buf, total = [], 0
        buf.append(line); total += len(line)
    if buf:
        yield "".join(buf)

def _parse_update(raw: str) -> Dict[str, Any]:
    update = json.loads(raw)
    msg = (update or {}).get("message") or (update or {}).get("edited_message") or (update or {}).get("channel_post") or {}
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    message_id = msg.get("message_id")
    thread_id = msg.get("message_thread_id")
    from_user = msg.get("from") or {}
    user_id = from_user.get("id")
    username = from_user.get("username")
    first_name = from_user.get("first_name")
    last_name = from_user.get("last_name")
    text = msg.get("text") or msg.get("caption") or ""
    entities = msg.get("entities") or msg.get("caption_entities") or []
    reply_msg = msg.get("reply_to_message") or {}
    reply_from = reply_msg.get("from") or {}

    # Изображение: telegram присылает photo как массив размеров — берём крупнейший
    # в пределах бюджета по размеру; альтернативно — документ с image/* mime.
    photo_file_id = None
    photos = msg.get("photo") or []
    if photos:
        ranked = sorted(photos, key=lambda p: (p.get("file_size") or (p.get("width", 0) * p.get("height", 0))))
        chosen = None
        for p in reversed(ranked):  # от крупного к мелкому
            fs = p.get("file_size") or 0
            if fs == 0 or fs <= 3_500_000:
                chosen = p
                break
        if chosen is None:
            chosen = ranked[0]
        photo_file_id = chosen.get("file_id")
    if not photo_file_id:
        doc = msg.get("document") or {}
        if str(doc.get("mime_type") or "").startswith("image/"):
            photo_file_id = doc.get("file_id")

    # Голосовое сообщение (voice note)
    voice = msg.get("voice") or {}
    voice_file_id = voice.get("file_id")
    voice_duration = int(voice.get("duration") or 0)

    return {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_id": message_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "text": text,
        "entities": entities,
        "photo_file_id": photo_file_id,
        "voice_file_id": voice_file_id,
        "voice_duration": voice_duration,
        "reply_to": {
            "from_id": reply_from.get("id"),
            "from_username": (reply_from.get("username") or ""),
            "from_is_bot": bool(reply_from.get("is_bot")),
        },
    }

def _process_one(update_raw: str) -> str:
    parsed = _parse_update(update_raw)
    logger.info("STEP0 parsed")

    chat_id   = parsed["chat_id"]
    chat_type = parsed["chat_type"]
    msg_id    = parsed["message_id"]
    thread_id = parsed["thread_id"]
    text      = parsed["text"]
    user_id   = parsed["user_id"]
    username  = parsed["username"]
    first_name = parsed.get("first_name")
    last_name = parsed.get("last_name")
    entities  = parsed.get("entities") or []
    reply_to  = parsed.get("reply_to") or {}
    photo_file_id = parsed.get("photo_file_id")
    has_image = bool(photo_file_id and VISION_ENABLED)

    if not (chat_id and msg_id):
        logger.info("No chat/message id → skip")
        return "No-op"

    # STEP0v: голосовое → текст (до сохранения в историю и mention-детекции)
    voice_file_id = parsed.get("voice_file_id")
    if voice_file_id and VOICE_ENABLED:
        if chat_type == "private":
            try:
                send_chat_action(chat_id, action="typing", thread_id=thread_id)
            except Exception:
                pass
        transcript = _transcribe_voice(voice_file_id, parsed.get("voice_duration") or 0)
        if transcript:
            voice_text = f"[голосовое сообщение] {transcript}"
            text = f"{text}\n{voice_text}".strip() if (text or "").strip() else voice_text
            logger.info("STEP0v voice transcribed (%ds, %d chars)",
                        parsed.get("voice_duration") or 0, len(transcript))
        else:
            logger.warning("STEP0v voice transcription failed/skipped")
            # В личке молчание хуже честного ответа; в группах тихо пропускаем
            if chat_type == "private" and not (text or "").strip():
                try:
                    send_message(chat_id, "Не получилось разобрать голосовое — продублируй текстом, пожалуйста.",
                                 chat_type=chat_type, thread_id=thread_id, reply_to=msg_id)
                except Exception as e:
                    logger.warning("send_message(voice-fail) failed: %s", e)
                return "Voice transcription failed"

    # Для сообщения с картинкой без подписи сохраняем плейсхолдер, чтобы история не была пустой
    stored_text = text
    if has_image and not (text or "").strip():
        stored_text = "[изображение]"

    dkey = dialog_key_for(chat_type, chat_id, user_id, thread_id)
    logger.info("ctx dkey=%s chat=%s/%s msg=%s", dkey, chat_type, chat_id, msg_id)

    try:
        if chat_type == "private" and user_id:
            if not get_user(str(user_id)):
                save_user(str(user_id), username, first_name=first_name, last_name=last_name)
            else:
                # Обновить имя, если изменилось
                update_user_names(str(user_id), username, first_name, last_name)
        else:
            if not get_channel(str(chat_id)): save_channel(str(chat_id), None)
            if thread_id:
                thread_key = f"{chat_id}:{thread_id}"
                if not get_thread(thread_key): save_thread(thread_key, "")
        logger.info("STEP1 ensured entities")
    except Exception as e:
        logger.warning("STEP1 ensure entities failed: %s", e)

    try:
        if (stored_text or "").strip():
            save_message(
                dkey,
                "user",
                stored_text,
                from_user=str(user_id) if user_id else None,
                from_username=username,
            )
            logger.info("STEP2 saved incoming")
        else:
            logger.info("STEP2 skip saving empty user message")
    except Exception as e:
        logger.warning("STEP2 save incoming failed: %s", e)

    try:
        st = get_settings(dkey)
        if not st:
            mode = default_mode_for(chat_type)
            save_settings(dkey, mode=mode, meta=None)
            st = {"dialog_key": dkey, "mode": mode, "meta": {}}
        logger.info("STEP3 settings mode=%s", st.get("mode"))
    except Exception as e:
        logger.warning("STEP3 settings failed: %s", e)
        st = {"dialog_key": dkey, "mode": default_mode_for(chat_type), "meta": {}}

    try:
        cmd_mode = parse_mode_command(text, BOT_USERNAME) if BOT_USERNAME else None
        if cmd_mode:
            updated = update_settings(dkey, mode=cmd_mode)
            try:
                send_message(chat_id, f"Режим обновлён: {updated.get('mode','?')}", chat_type=chat_type, thread_id=thread_id, reply_to=msg_id)
            except Exception as e:
                logger.warning("send_message(/mode) failed: %s", e)
            return "OK (/mode)"
    except Exception as e:
        logger.warning("Mode command handling failed: %s", e)

    try:
        cmd_scope = parse_scope_command(text, BOT_USERNAME) if BOT_USERNAME else None
        if cmd_scope and chat_type != "private":
            # merge meta
            meta = (st or {}).get("meta") or {}
            meta["group_scope"] = cmd_scope
            updated = update_settings(dkey, meta=meta)
            try:
                send_message(chat_id, f"Скоуп обновлён: group_scope={cmd_scope}", chat_type=chat_type, thread_id=thread_id, reply_to=msg_id)
            except Exception as e:
                logger.warning("send_message(/scope) failed: %s", e)
            return "OK (/scope)"
    except Exception as e:
        logger.warning("Scope command handling failed: %s", e)

    try:
        send_chat_action(chat_id, action="typing", thread_id=thread_id)
    except Exception:
        pass

    mentioned = detect_mention(text or "", entities, BOT_USERNAME, reply_to=reply_to, bot_id=BOT_ID) if BOT_USERNAME else False
    try:
        mode = (st or {}).get("mode") or default_mode_for(chat_type)
        if not should_respond_by_mode(mode, chat_type, mentioned):
            logger.info("STEP4 skip by mode=%s; mentioned=%s; text=%r", mode, mentioned, (text[:80] if text else ""))
            return "Skipped"
        logger.info("STEP4 mention ok (mode=%s, mentioned=%s)", mode, mentioned)
    except Exception as e:
        logger.warning("STEP4 gate failed (continue anyway): %s", e)

    # Стабильный префикс (BASE_SYSTEM_PROMPT) идёт первым — удобно для будущего prompt caching.
    # Волатильную дату/время добавляем в самом КОНЦЕ системного промпта (см. ниже), чтобы она
    # не ломала кэшируемый префикс.
    system_parts = []
    if BASE_SYSTEM_PROMPT:
        system_parts.append(BASE_SYSTEM_PROMPT)
    try:
        summary_item = get_latest_summary_item(dkey)
    except Exception as e:
        logger.warning("Get summary failed: %s", e)
        summary_item = None
    summary = (summary_item or {}).get("summary")
    if summary:
        system_parts.append(f"Dialog summary: {summary}")

    # Кешируем профили всех участников диалога для использования в контексте
    user_profiles_cache = {}  # {user_id: profile_data}

    def get_cached_profile(uid: str) -> Optional[Dict[str, Any]]:
        """Получить профиль из кеша или загрузить из БД."""
        if uid not in user_profiles_cache:
            profile = get_user_profile(uid)
            user_profiles_cache[uid] = profile
        return user_profiles_cache.get(uid)

    # НОВОЕ: Долгосрочная память о пользователе (для private чатов)
    if chat_type == "private" and user_id:
        try:
            user_profile = get_cached_profile(str(user_id))
            if user_profile and any(user_profile.values()):  # Если профиль не пустой
                profile_parts = []

                first_name_p = user_profile.get("first_name", "")
                username_str = f"@{username}" if username else ""

                header = "О собеседнике"
                if first_name_p:
                    header += f" ({first_name_p})"
                if username_str:
                    header += f" {username_str}"
                header += ":"

                profile_parts.append(header)

                # Стиль общения
                comm_style = user_profile.get("communication_style", "").strip()
                if comm_style:
                    profile_parts.append(f"- Стиль общения: {comm_style}")

                # Интересы
                interests = user_profile.get("interests", [])
                if interests:
                    profile_parts.append(f"- Интересы: {', '.join(interests)}")

                # Долгосрочная память
                long_summary = user_profile.get("long_term_summary", "").strip()
                if long_summary:
                    profile_parts.append(f"- Контекст прошлых бесед: {long_summary}")

                # Последние темы
                last_topics = user_profile.get("last_topics", [])
                if last_topics:
                    profile_parts.append(f"- Последние темы: {', '.join(last_topics)}")

                # Явно сохранённые факты (долговременная память)
                facts = user_profile.get("facts", [])
                if facts:
                    profile_parts.append("- Что важно помнить: " + "; ".join(facts))

                if len(profile_parts) > 1:  # Если есть хоть что-то кроме заголовка
                    profile_parts.append("\nОтвечай персонализированно, учитывая этот контекст и стиль собеседника.")
                    system_parts.append("\n".join(profile_parts))
        except Exception as e:
            logger.warning("Failed to add user profile context: %s", e)

    history = get_dialog_history(dkey, limit=120, consistent_read=True)

    # Determine group scope
    scope = ((st or {}).get("meta") or {}).get("group_scope") or GROUP_SCOPE_DEFAULT
    if chat_type != "private" and user_id:
        # НОВОЕ: Собираем информацию об участниках
        try:
            # Получить уникальных участников из истории
            participants_info = {}
            for m in history:
                if m.get("role") == "user":
                    fu = m.get("from_user", "").strip()
                    if fu and fu not in participants_info:
                        profile = get_cached_profile(fu)
                        fu_username = m.get("from_username", "")
                        participants_info[fu] = {
                            "user_id": fu,
                            "username": fu_username,
                            "first_name": profile.get("first_name", "") if profile else "",
                            "last_name": profile.get("last_name", "") if profile else "",
                        }

            # Формируем карту участников
            if participants_info:
                participants_lines = ["Участники беседы:"]
                for uid, info in participants_info.items():
                    name = info.get("first_name") or info.get("username") or f"User_{uid}"
                    username_str = f"@{info['username']}" if info.get("username") else ""

                    # Определяем роль
                    if str(uid) == str(user_id):
                        role = "текущий автор запроса"
                    else:
                        role = "участник"

                    line = f"- {name}"
                    if username_str:
                        line += f" ({username_str})"
                    line += f", ID:{uid}"
                    if role:
                        line += f" — {role}"

                    participants_lines.append(line)

                participants_map = "\n".join(participants_lines)
                system_parts.append(participants_map + "\n")
        except Exception as e:
            logger.warning("Failed to build participants map: %s", e)

        # Scope инструкции
        if scope == "initiator":
            scope_msg = (
                f"Текущий автор запроса: ID={user_id}\n"
                f"Используй в качестве входа только сообщения от текущего автора (помеченные соответствующим префиксом).\n"
                f"Игнорируй сообщения других участников, если явно не указан иной контекст."
            )
        elif scope == "thread":
            scope_msg = (
                f"Групповой диалог (тема/тред). Текущий автор: ID={user_id}.\n"
                f"Учитывай реплики всех участников, различай говорящих по именам и префиксам. Отвечай автору запроса."
            )
        else:  # hybrid
            scope_msg = (
                f"Гибридный режим: приоритет реплик текущего автора (ID={user_id}), но учитывай контекст других участников.\n"
                f"Различай говорящих по именам и префиксам. Отвечай автору запроса."
            )

        system_parts.append(scope_msg)

        # Долговременная память о текущем авторе (работает и в группах)
        try:
            author_prof = get_cached_profile(str(user_id))
            author_facts = (author_prof or {}).get("facts", [])
            if author_facts:
                system_parts.append("Важно помнить о текущем авторе: " + "; ".join(author_facts))
        except Exception as e:
            logger.warning("Failed to add author facts: %s", e)

    chat_msgs = []
    for m in history:
        if m.get("role") in ("user", "assistant"):
            content = m.get("content", "")
            fu = (m.get("from_user") or "").strip() if m.get("role") == "user" else ""

            if m.get("role") == "user" and fu:
                # УЛУЧШЕННЫЙ ПРЕФИКС: получаем профиль и формируем читаемое имя
                profile = get_cached_profile(fu)
                fu_username = m.get("from_username", "")

                if profile:
                    first_name_m = profile.get("first_name", "")
                    name_display = first_name_m or fu_username or fu
                else:
                    name_display = fu_username or fu

                # Формат: [Имя (@username), ID:123456]
                prefix = f"[{name_display}"
                if fu_username and name_display != fu_username:
                    prefix += f" (@{fu_username})"
                prefix += f", ID:{fu}]"

                content = f"{prefix} {content}"

            # Filtering by scope (только в группах)
            if chat_type != "private":
                if scope == "initiator" and m.get("role") == "user" and fu and str(user_id) != fu:
                    continue

            chat_msgs.append({"role": m["role"], "content": content, "_fu": fu})

    system_prompt = "\n\n".join(system_parts)

    def total_tokens():
        return num_tokens_from_messages(_view(chat_msgs), system=system_prompt)

    removed_turns = []

    def _drop_oldest_turn():
        nonlocal chat_msgs, removed_turns
        if not chat_msgs:
            return
        if len(chat_msgs) >= 2 and chat_msgs[0]["role"] == "user" and chat_msgs[1]["role"] == "assistant":
            removed_turns.append(chat_msgs.pop(0))
            removed_turns.append(chat_msgs.pop(0))
        else:
            removed_turns.append(chat_msgs.pop(0))

    def _drop_oldest_non_initiator_user():
        nonlocal chat_msgs, removed_turns
        for i, mm in enumerate(chat_msgs):
            fu = (mm.get("_fu") or "").strip()
            if mm.get("role") == "user" and fu and str(user_id) != fu:
                removed_turns.append(chat_msgs.pop(i))
                return True
        return False

    # Prepare a view for token counting
    def _view(messages_list):
        return [{"role": x["role"], "content": x["content"]} for x in messages_list]

    # Trim with preference depending on scope
    while total_tokens() > MAX_CONTEXT_TOKENS and len(chat_msgs) > 1:
        if chat_type != "private" and scope == "hybrid" and _drop_oldest_non_initiator_user():
            continue
        _drop_oldest_turn()

    if removed_turns:
        try:
            trimmed_simple = _view(removed_turns[-16:])
            sm = summarize_history(trimmed_simple)
            if sm:
                save_summary(dkey, sm)
                system_parts.append(f"Earlier dialog summary: {sm}")
                system_prompt = "\n\n".join(system_parts)
        except Exception as e:
            logger.warning("Trim summary failed: %s", e)

    messages = _view(chat_msgs)
    logger.info("STEP5 messages_ready=%d tokens~%d", len(messages), total_tokens())

    # --- Инструменты (tool use) ---
    client_tools = []
    if OPENWEATHERMAP_API_KEY:
        client_tools.append(WEATHER_TOOL)
        client_tools.append(FORECAST_TOOL)
    if user_id:  # инструменты памяти требуют известного пользователя
        client_tools.append(REMEMBER_TOOL)
        client_tools.append(FORGET_TOOL)
    server_tools = [WEB_SEARCH_TOOL] if WEB_SEARCH_ENABLED else []
    active_tools = (client_tools + server_tools) or None

    # Подсказка модели о доступных инструментах
    hints = []
    if server_tools:
        hints.append("Если для ответа нужны свежие или точные факты из интернета — используй веб-поиск (web_search).")
    if any(t.get("name") == "remember_fact" for t in client_tools):
        hints.append(
            "Если собеседник сообщает важную устойчивую информацию о себе или просит запомнить/забыть — "
            "пользуйся инструментами памяти (remember_fact/forget_fact)."
        )
    if hints:
        system_prompt = system_prompt + "\n\n" + "\n".join(hints)

    # Текущая дата/время МСК — волатильно, поэтому в самом конце (не ломает будущий кэш префикса)
    _now_msk = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    system_prompt = system_prompt + "\n\n" + f"Текущая дата и время: {_now_msk.strftime('%d.%m.%Y %H:%M')} (МСК)"

    # Стиль-файрвол — последним, чтобы быть самой «свежей» инструкцией для модели
    system_prompt = system_prompt + "\n\n" + STYLE_ANCHOR

    # --- Изображение: подмешиваем в последнее сообщение пользователя ---
    if has_image:
        b64, mime = get_file_base64(photo_file_id)
        if b64:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    txt = messages[i]["content"]
                    blocks = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}]
                    if isinstance(txt, str) and txt.strip():
                        blocks.append({"type": "text", "text": txt})
                    else:
                        blocks.append({"type": "text", "text": "[пользователь прислал изображение]"})
                    messages[i]["content"] = blocks
                    break
            logger.info("STEP5b image attached (%s)", mime)
        else:
            logger.warning("STEP5b image download failed, proceeding text-only")

    ai_resp = generate_response(
        messages,
        system=system_prompt,
        max_tokens=MAX_OUTPUT_TOKENS,
        tools=active_tools,
        tool_executor=_make_tool_executor(str(user_id) if user_id else None) if active_tools else None,
    )
    if (ai_resp or "").strip().lower() in {"assistant","system","user",""}:
        logger.warning("STEP6 non-text placeholder from model: %r", ai_resp)
        ai_resp = "⚠️ Пустой ответ модели. Зафиксировал это в логах."
    logger.info("STEP6 ai_len=%d", len(ai_resp))

    try:
        save_message(dkey, "assistant", ai_resp, to_user=str(user_id) if user_id else None)
    except Exception as e:
        logger.warning("Save assistant failed: %s", e)
    try:
        for part in (p for p in split_telegram(ai_resp) if p is not None):
            send_message(chat_id, part, chat_type=chat_type, thread_id=thread_id, reply_to=msg_id)
        logger.info("STEP7 sent")
    except Exception as e:
        logger.exception("TELEGRAM SEND FAILED: %r", e)

    # НОВОЕ: Обновить счётчик сообщений пользователя
    if user_id:
        try:
            update_user_profile(str(user_id), increment_messages=True)
        except Exception as e:
            logger.warning("Failed to increment message count: %s", e)

    # STEP8: обновление памяти.
    #  - Краткая сводка троттлится по времени (раньше регенерилась КАЖДЫЙ ход — лишний вызов Claude).
    #  - Долгосрочный профиль (private) обновляется раз в LONG_TERM_EVERY сообщений
    #    (раньше ветка была мёртвой: full брался с limit=24, а проверялось len>=50).
    try:
        full = get_dialog_history(dkey, limit=MIN_MSGS_FOR_SUMMARY * 2, consistent_read=True)
        if len(full) >= MIN_MSGS_FOR_SUMMARY:
            last_ts = (summary_item or {}).get("timestamp")
            now_ms = int(time.time() * 1000)
            stale = (last_ts is None) or (now_ms - int(last_ts) > SUMMARY_MIN_INTERVAL_SEC * 1000)
            if stale:
                sm = summarize_history(full[-SUMMARY_HISTORY_LIMIT:])
                if sm:
                    save_summary(dkey, sm)
                logger.info("STEP8 summary refreshed")
            else:
                logger.info("STEP8 summary skipped (throttled)")

        # Долгосрочный профиль — только private, раз в LONG_TERM_EVERY сообщений
        if chat_type == "private" and user_id:
            prof = get_user_profile(str(user_id)) or {}
            mc = int(prof.get("message_count", 0) or 0)
            if mc >= LONG_TERM_EVERY and mc % LONG_TERM_EVERY == 0:
                try:
                    hist = get_dialog_history(dkey, limit=80, consistent_read=True)
                    user_info = {"first_name": prof.get("first_name", ""), "username": username}
                    long_summary = create_long_term_summary(hist[-80:], user_info)
                    recent_topics = extract_topics(hist[-20:])
                    if long_summary or recent_topics:
                        update_user_profile(
                            str(user_id),
                            long_term_summary=long_summary if long_summary else None,
                            last_topics=recent_topics if recent_topics else None,
                        )
                    logger.info("Updated long-term profile for user %s (mc=%d)", user_id, mc)
                except Exception as e:
                    logger.warning("Failed to update long-term profile: %s", e)

        logger.info("STEP8 done")
    except Exception as e:
        logger.warning("Update summary failed: %s", e)

    return "OK"

def lambda_handler(event, context):
    try:
        records = event.get("Records", [])
    except Exception:
        records = []
    if not records:
        logger.info("No SQS records")
        return {"statusCode": 200, "body": "no records"}

    for r in records:
        try:
            body = r.get("body")
            result = _process_one(body)
            logger.info("DONE record %s -> %s", r.get("messageId"), result)
        except Exception as e:
            logger.exception("Record failed: %r", e)

    return {"statusCode": 200, "body": "ok"}
