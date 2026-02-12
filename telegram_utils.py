# telegram_utils.py

import os
import json
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN is not set in environment variables")
API_BASE   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL   = API_BASE + "/sendMessage"
ACTION_URL = API_BASE + "/sendChatAction"

REQUEST_TIMEOUT = 30
DEFAULT_PARSE_MODE = os.getenv("TELEGRAM_PARSE_MODE") or None  # None -> plain text

def send_message(
    chat_id: int,
    text: str,
    *,
    chat_type: str,
    thread_id: Optional[int] = None,
    reply_to: Optional[int] = None,
    parse_mode: Optional[str] = DEFAULT_PARSE_MODE,
    disable_web_page_preview: bool = False,
    disable_notification: bool = False
) -> None:
    """Отправляет сообщение в Telegram.
    В обсуждениях/форумах поддерживаются ОДНОВРЕМЕННО message_thread_id и reply_to_message_id.
    Для каналов без thread_id/reply_to — по умолчанию не постим (можно включить при необходимости).
    """
    # ► Фильтр: не постим новый пост в канал
    if chat_type == "channel" and thread_id is None and reply_to is None:
        logger.info("Skip sending: channel post (chat_id=%s)", chat_id)
        return

    payload = {
        "chat_id": chat_id,
        "text":    text,
        "disable_web_page_preview": disable_web_page_preview,
        "disable_notification": disable_notification
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to

    try:
        r = requests.post(SEND_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.warning(
            f"send_message failed for chat_id={chat_id}, "
            f"thread_id={thread_id}, reply_to={reply_to}: {e} — {getattr(r, 'text', '')}"
        )

def send_chat_action(chat_id: int, *, action: str = "typing", thread_id: Optional[int] = None) -> None:
    payload = {
        "chat_id": chat_id,
        "action":  action
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    try:
        r = requests.post(ACTION_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.warning(
            f"send_chat_action failed for chat_id={chat_id}, "
            f"thread_id={thread_id}, action={action}: {e} — {getattr(r, 'text', '')}"
        )
