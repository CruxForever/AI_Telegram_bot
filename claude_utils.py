# claude_utils.py  —  замена openai_utils.py на Anthropic Claude API

import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# Режим размышлений. У Sonnet 5 / Opus 4.6+ / Fable adaptive-thinking включён по умолчанию
# при ОТСУТСТВИИ параметра — поэтому задаём его явно.
#   "disabled" — быстро и дёшево, весь MAX_OUTPUT_TOKENS идёт в ответ (поведение как на Haiku).
#   "adaptive" — глубже рассуждает, но часть выходного бюджета уходит на мысли (тогда стоит
#                поднять MAX_OUTPUT_TOKENS, иначе ответ может обрезаться).
_THINKING_MODE = os.getenv("THINKING_MODE", "disabled").lower()

# --------- Anthropic SDK init ---------
_client = None


def _init_client():
    global _client
    if _client is not None:
        return
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is missing in environment")
        return
    try:
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key)
        logger.info("Anthropic client initialized, model=%s", CLAUDE_MODEL)
    except Exception as e:
        logger.error("Anthropic client init failed: %s", e)


_init_client()


# ---- Approximate token counting ----
# Claude не имеет публичного локального токенизатора (как tiktoken у OpenAI).
# Используем приближение ~4 символа = 1 токен (аналогично fallback в старом коде).

def _content_len(content: Any) -> int:
    """Длина контента в символах. Поддерживает как строку, так и список блоков
    (мультимодальное сообщение: text/image)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                total += len(b.get("text", ""))
            elif btype == "image":
                total += 6400  # ~1600 токенов на изображение (грубая оценка)
        return total
    return 0


def num_tokens_from_messages(messages: List[Dict[str, Any]], system: str = "") -> int:
    """Approximate token count for system prompt + messages."""
    tokens = len(system) // 4 + 4          # system overhead
    for m in messages:
        tokens += 4                         # per-message overhead
        tokens += _content_len(m.get("content", "")) // 4
    tokens += 2                             # conversation overhead
    return tokens


def _to_blocks(content: Any) -> List[Dict[str, Any]]:
    """Приводит контент к списку блоков (для склейки мультимодальных сообщений)."""
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content if isinstance(content, str) else str(content)}]


def _ensure_alternation(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Claude API требует строгое чередование user/assistant.
    Склеиваем подряд идущие сообщения одной роли.
    Гарантируем, что первое сообщение — user.
    Корректно работает и с мультимодальным контентом (список блоков).
    """
    if not messages:
        return []
    merged: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if merged and merged[-1]["role"] == role:
            prev = merged[-1]["content"]
            if isinstance(prev, list) or isinstance(content, list):
                merged[-1]["content"] = _to_blocks(prev) + _to_blocks(content)
            else:
                merged[-1]["content"] = prev + "\n" + content
        else:
            merged.append({"role": role, "content": content})
    # Claude требует, чтобы первое сообщение было user
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(начало диалога)"})
    return merged


def _client_tools_only(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Оставляет только клиентские инструменты (без серверных, у которых есть поле "type",
    напр. web_search_20250305). Используется для graceful-degradation при ошибке."""
    if not tools:
        return None
    client_only = [t for t in tools if "type" not in t]
    return client_only or None


def _extract_text(resp) -> str:
    """Достаёт текстовые блоки из ответа Claude (игнорируя tool_use/web_search блоки).

    Склеиваем БЕЗ разделителя: при веб-поиске/цитатах Sonnet 5 разбивает ответ на
    несколько text-блоков посреди предложения — вставка "\\n" уводила бы знаки
    препинания на новую строку. Модель сама расставляет переносы внутри блоков.
    """
    text_parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text" and hasattr(block, "text"):
            text_parts.append(block.text)
    return "".join(text_parts).strip()


def _chat(messages: List[Dict[str, Any]], system: str, max_tokens: int,
          tools: Optional[List[Dict[str, Any]]] = None,
          tool_executor=None) -> str:
    if _client is None:
        logger.error("Anthropic client is not configured")
        return "⚠️ Anthropic client is not configured."

    model = os.getenv("CLAUDE_MODEL", CLAUDE_MODEL)
    safe_messages = _ensure_alternation(messages)

    if not safe_messages:
        safe_messages = [{"role": "user", "content": "(пустой контекст)"}]

    def _run(active_tools: Optional[List[Dict[str, Any]]]):
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(safe_messages),
        }
        # Современные модели (Sonnet 5, Opus 4.6+, Fable) отклоняют temperature/top_p/top_k
        # с 400 — не передаём их. Режимом рассуждений управляем через thinking.
        kwargs["thinking"] = (
            {"type": "adaptive"} if _THINKING_MODE == "adaptive" else {"type": "disabled"}
        )
        if system:
            kwargs["system"] = system
        if active_tools:
            kwargs["tools"] = active_tools

        # Цикл tool use: Claude может вызвать клиентский инструмент несколько раз.
        # Серверные инструменты (web_search) выполняются на стороне API.
        for _ in range(8):  # защита от зацикливания
            resp = _client.messages.create(**kwargs)

            # Долгий серверный инструмент мог приостановить ход — продолжаем
            if resp.stop_reason == "pause_turn":
                kwargs["messages"] = kwargs["messages"] + [
                    {"role": "assistant", "content": resp.content},
                ]
                continue

            if resp.stop_reason != "tool_use" or not tool_executor:
                return resp

            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        result = tool_executor(block.name, block.input)
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"
                    logger.info("Tool call: %s(%s) -> %s", block.name, block.input, str(result)[:100])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })

            kwargs["messages"] = kwargs["messages"] + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ]

        return resp  # исчерпали лимит итераций — возвращаем последний ответ

    # Основная попытка → деградация: при ошибке убираем серверные инструменты,
    # затем все инструменты, чтобы пользователь всё равно получил ответ.
    try:
        resp = _run(tools)
    except Exception as e:
        logger.exception("Anthropic API error (tools=%s): %r", bool(tools), e)
        if tools:
            try:
                logger.warning("Retry with client-only tools")
                resp = _run(_client_tools_only(tools))
            except Exception as e2:
                logger.warning("Retry client-only failed: %r; retry with no tools", e2)
                try:
                    resp = _run(None)
                except Exception as e3:
                    logger.exception("Anthropic API final error: %r", e3)
                    return "⚠️ Не удалось получить ответ от модели."
        else:
            return "⚠️ Не удалось получить ответ от модели."

    return _extract_text(resp)


def generate_response(messages: List[Dict[str, Any]], *,
                      system: str = "",
                      max_tokens: int = 800,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_executor=None) -> str:
    """Генерация ответа Claude. messages — только user/assistant.
    tools — список инструментов для tool use (опционально).
    tool_executor — callable(tool_name, tool_input) -> str (опционально).
    """
    return _chat(messages, system, max_tokens,
                 tools=tools, tool_executor=tool_executor)


def _plain_text(content: Any) -> str:
    """Текстовое представление контента для саммаризации (отбрасывает изображения)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[изображение]")
        return " ".join(parts)
    return str(content)


def summarize_history(
    history: List[Dict[str, Any]],
    user_context: Optional[Dict[str, Any]] = None
) -> str:
    """Суммаризация истории диалога с сохранением персонального контекста."""

    # Нейтральная фактическая сводка — служебная заметка для памяти, НЕ ответ пользователю.
    # Важно: без шаблонной формы, чтобы стиль оформления не «протекал» в будущие ответы.
    system = """Сделай нейтральную фактическую сводку диалога на русском языке.

Сохрани по сути: ключевые темы и вопросы, договорённости и решения, факты о собеседнике,
незавершённые задачи, интересы и предпочтения участников.

Пиши нейтральной прозой от третьего лица, обычным сплошным текстом (2-4 предложения).
СТРОГО: без заголовков и секций, без слов «Итог», «Вывод», «Критика», «Уверенность»,
без списков и markdown, без оценок. НЕ копируй стиль и формат реплик из диалога —
если ответы в нём шаблонные, это старый дефект, не воспроизводи его.
Это служебная заметка для памяти, а не реплика в чате."""

    # Увеличить количество сообщений и токенов
    few = [{"role": m["role"], "content": _plain_text(m.get("content", ""))} for m in history[-30:]]

    # Если есть контекст пользователя, добавить в system
    if user_context:
        user_info = "\n\nИнформация о собеседнике:\n"
        if user_context.get("first_name"):
            user_info += f"Имя: {user_context['first_name']}\n"
        if user_context.get("username"):
            user_info += f"Username: @{user_context['username']}\n"
        system += user_info

    return _chat(few, system, max_tokens=600)


def create_long_term_summary(
    history: List[Dict[str, Any]],
    user_info: Dict[str, Any]
) -> str:
    """Создаёт долгосрочную сводку о пользователе и его интересах."""

    system = """Составь нейтральный фактический профиль собеседника по истории диалогов.

Опиши по сути: основные интересы и темы, над чем работает (проекты/задачи),
предпочтения в общении, важные устойчивые детали о человеке.

Пиши нейтральной прозой от третьего лица, 1-2 абзаца сплошным текстом.
СТРОГО: без заголовков, секций, списков и markdown, без слов «Итог/Вывод/Критика».
Это служебная заметка для памяти, а не реплика в чате."""

    # Берем последние 60 сообщений для анализа
    messages = [{"role": m["role"], "content": _plain_text(m.get("content", ""))} for m in history[-60:]]

    # Добавить имя пользователя в контекст
    if user_info.get("first_name"):
        system += f"\n\nИмя пользователя: {user_info['first_name']}"

    return _chat(messages, system, max_tokens=400)


def extract_topics(messages: List[Dict[str, Any]], max_topics: int = 5) -> List[str]:
    """Извлекает основные темы из последних сообщений."""

    system = f"""Проанализируй последние сообщения и выдели {max_topics} основных тем или ключевых слов.

Верни ТОЛЬКО список тем через запятую, без нумерации и пояснений.
Пример: Python, AWS Lambda, DynamoDB, Claude API, Телеграм боты"""

    few = [{"role": m["role"], "content": _plain_text(m.get("content", ""))} for m in messages[-10:]]

    try:
        result = _chat(few, system, max_tokens=100)
        # Парсим темы
        topics = [t.strip() for t in result.split(",") if t.strip()]
        return topics[:max_topics]
    except Exception:
        return []


# Подмножество эмодзи, разрешённых Telegram для реакций (простые однокодовые + ❤️).
# Модель выбирает РОВНО один из этого списка; worker дополнительно валидирует.
REACTION_EMOJIS = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🎉", "🤩",
    "🙏", "👌", "😍", "💯", "🤣", "🏆", "🤨", "😐", "😈", "😭", "🤓", "👀",
    "😇", "🤝", "🤗", "🫡", "🤪", "🗿", "😎", "😡", "🙈", "🥱", "💅", "👻",
]


def choose_reaction(text: str) -> str:
    """Подбирает уместный эмодзи-реакцию на сообщение или '' (реагировать не стоит).

    Дешёвый отдельный вызов — используется, когда бот прочитал сообщение, но НЕ отвечает
    текстом. Выборочно: на большинство проходных сообщений возвращает ''.
    """
    if not (text or "").strip():
        return ""
    allowed = " ".join(REACTION_EMOJIS)
    system = (
        "Ты — Петрович в мессенджере. Тебе показывают сообщение, на которое ты НЕ отвечаешь "
        "текстом, и ты решаешь, поставить ли эмодзи-реакцию.\n"
        f"Разрешённые эмодзи (выбери РОВНО ОДИН): {allowed}\n"
        "Правило: реагируй ТОЛЬКО когда есть явный повод — эмоция, юмор, новость, достижение, "
        "что-то яркое или трогательное. На обычные, нейтральные, технические и проходные "
        "сообщения реакция НЕ нужна. Сомневаешься — не реагируй.\n"
        "Ответь либо ОДНИМ эмодзи из списка, либо словом NONE. Без пояснений."
    )
    few = [{"role": "user", "content": (text or "")[:500]}]
    try:
        result = (_chat(few, system, max_tokens=12) or "").strip()
    except Exception:
        return ""
    if not result or "NONE" in result.upper():
        return ""
    for e in REACTION_EMOJIS:
        if e in result:
            return e
    return ""
