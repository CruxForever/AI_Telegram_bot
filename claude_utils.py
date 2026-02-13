# claude_utils.py  —  замена openai_utils.py на Anthropic Claude API

import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

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

def num_tokens_from_messages(messages: List[Dict[str, Any]], system: str = "") -> int:
    """Approximate token count for system prompt + messages."""
    tokens = len(system) // 4 + 4          # system overhead
    for m in messages:
        tokens += 4                         # per-message overhead
        tokens += len(m.get("content", "")) // 4
    tokens += 2                             # conversation overhead
    return tokens


def _ensure_alternation(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Claude API требует строгое чередование user/assistant.
    Склеиваем подряд идущие сообщения одной роли.
    Гарантируем, что первое сообщение — user.
    """
    if not messages:
        return []
    merged: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})
    # Claude требует, чтобы первое сообщение было user
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(начало диалога)"})
    return merged


def _chat(messages: List[Dict[str, Any]], system: str,
          temperature: float, max_tokens: int,
          tools: Optional[List[Dict[str, Any]]] = None,
          tool_executor=None) -> str:
    if _client is None:
        logger.error("Anthropic client is not configured")
        return "⚠️ Anthropic client is not configured."

    model = os.getenv("CLAUDE_MODEL", CLAUDE_MODEL)
    safe_messages = _ensure_alternation(messages)

    if not safe_messages:
        safe_messages = [{"role": "user", "content": "(пустой контекст)"}]

    try:
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": safe_messages,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        # Цикл tool use: Claude может вызвать инструмент несколько раз
        while True:
            resp = _client.messages.create(**kwargs)

            if resp.stop_reason != "tool_use" or not tool_executor:
                break  # обычный ответ — выходим

            # Claude запросил вызов инструмента — выполняем и передаём результат
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        result = tool_executor(block.name, block.input)
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"
                    logger.info("Tool call: %s(%s) -> %s", block.name, block.input, result[:100])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })

            # Добавляем ответ ассистента + результаты инструментов и делаем следующий запрос
            kwargs["messages"] = kwargs["messages"] + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ]

        # Извлекаем текст из content-блоков
        text_parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts).strip() if text_parts else ""

    except Exception as e:
        logger.exception("Anthropic API error: %r", e)
        return "⚠️ Не удалось получить ответ от модели."


def generate_response(messages: List[Dict[str, Any]], *,
                      system: str = "",
                      temperature: float = 0.5,
                      max_tokens: int = 800,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_executor=None) -> str:
    """Генерация ответа Claude. messages — только user/assistant.
    tools — список инструментов для tool use (опционально).
    tool_executor — callable(tool_name, tool_input) -> str (опционально).
    """
    return _chat(messages, system, temperature, max_tokens,
                 tools=tools, tool_executor=tool_executor)


def summarize_history(
    history: List[Dict[str, Any]],
    user_context: Optional[Dict[str, Any]] = None
) -> str:
    """Суммаризация истории диалога с сохранением персонального контекста."""

    # Улучшенный промпт
    system = """Создай подробное резюме диалога на русском языке.

ВАЖНО - сохрани персональный контекст:
- Имена участников и их стиль общения (формальный/неформальный, эмоциональный/сдержанный)
- Ключевые темы, вопросы и принятые решения
- Эмоциональный тон разговора
- Незавершённые вопросы или задачи
- Интересы и предпочтения пользователей

Формат: 2-4 абзаца, фокус на смысле и контексте личностей, а не на мелких деталях."""

    # Увеличить количество сообщений и токенов
    few = [{"role": m["role"], "content": m["content"]} for m in history[-30:]]

    # Если есть контекст пользователя, добавить в system
    if user_context:
        user_info = "\n\nИнформация о собеседнике:\n"
        if user_context.get("first_name"):
            user_info += f"Имя: {user_context['first_name']}\n"
        if user_context.get("username"):
            user_info += f"Username: @{user_context['username']}\n"
        system += user_info

    return _chat(few, system, temperature=0.3, max_tokens=600)


def create_long_term_summary(
    history: List[Dict[str, Any]],
    user_info: Dict[str, Any]
) -> str:
    """Создаёт долгосрочную сводку о пользователе и его интересах."""

    system = """Проанализируй историю диалогов с пользователем и создай краткий профиль.

Определи и опиши:
1. Основные интересы и темы, которые обсуждались
2. Стиль общения (формальный/неформальный, технический/разговорный, эмоциональный/сдержанный)
3. Ключевые проекты или задачи, над которыми работает пользователь
4. Предпочтения в общении

Формат: 1-2 абзаца, краткий профиль собеседника."""

    # Берем последние 60 сообщений для анализа
    messages = [{"role": m["role"], "content": m["content"]} for m in history[-60:]]

    # Добавить имя пользователя в контекст
    if user_info.get("first_name"):
        system += f"\n\nИмя пользователя: {user_info['first_name']}"

    return _chat(messages, system, temperature=0.3, max_tokens=400)


def extract_topics(messages: List[Dict[str, Any]], max_topics: int = 5) -> List[str]:
    """Извлекает основные темы из последних сообщений."""

    system = f"""Проанализируй последние сообщения и выдели {max_topics} основных тем или ключевых слов.

Верни ТОЛЬКО список тем через запятую, без нумерации и пояснений.
Пример: Python, AWS Lambda, DynamoDB, Claude API, Телеграм боты"""

    few = [{"role": m["role"], "content": m["content"]} for m in messages[-10:]]

    try:
        result = _chat(few, system, temperature=0.2, max_tokens=100)
        # Парсим темы
        topics = [t.strip() for t in result.split(",") if t.strip()]
        return topics[:max_topics]
    except Exception:
        return []
