# telegram_utils.py

import os
import json
import base64
import logging
import requests
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN is not set in environment variables")
API_BASE   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
FILE_BASE  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"
SEND_URL   = API_BASE + "/sendMessage"
ACTION_URL = API_BASE + "/sendChatAction"
GETFILE_URL = API_BASE + "/getFile"

REQUEST_TIMEOUT = 30
DEFAULT_PARSE_MODE = os.getenv("TELEGRAM_PARSE_MODE") or None  # None -> plain text

# Claude принимает image-блоки только этих типов
_ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def get_file_base64(file_id: str, *, max_bytes: int = 3_500_000) -> Tuple[Optional[str], Optional[str]]:
    """Скачивает файл Telegram по file_id и возвращает (base64, mime) или (None, None).

    Ограничение по размеру (max_bytes) — чтобы не упереться в лимиты Claude/Lambda.
    """
    if not file_id:
        return None, None
    try:
        r = requests.get(GETFILE_URL, params={"file_id": file_id}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        info = r.json()
        if not info.get("ok"):
            logger.warning("getFile not ok: %s", info)
            return None, None
        result = info.get("result", {})
        file_path = result.get("file_path")
        file_size = result.get("file_size") or 0
        if not file_path:
            return None, None
        if file_size and file_size > max_bytes:
            logger.warning("Telegram file too large: %s bytes", file_size)
            return None, None

        fr = requests.get(f"{FILE_BASE}/{file_path}", timeout=REQUEST_TIMEOUT)
        fr.raise_for_status()
        data = fr.content
        if len(data) > max_bytes:
            logger.warning("Downloaded file too large: %s bytes", len(data))
            return None, None

        ext = (file_path.rsplit(".", 1)[-1] if "." in file_path else "").lower()
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/jpeg")
        if mime not in _ALLOWED_IMAGE_MIME:
            mime = "image/jpeg"
        return base64.b64encode(data).decode("ascii"), mime
    except Exception as e:
        logger.warning("get_file_base64(%s) failed: %s", file_id, e)
        return None, None

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
