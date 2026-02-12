# dynamo_utils.py

import os
import time
import logging
from typing import Optional, List, Dict, Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

DDB_ENDPOINT_URL = os.getenv("DDB_ENDPOINT_URL")  # allow local testing
dynamodb = boto3.resource("dynamodb", endpoint_url=DDB_ENDPOINT_URL) if DDB_ENDPOINT_URL else boto3.resource("dynamodb")

USERS_TABLE     = os.getenv("USERS_TABLE", "Users")
CHANNELS_TABLE  = os.getenv("CHANNELS_TABLE", "Channels")
THREADS_TABLE   = os.getenv("THREADS_TABLE", "Threads")
MESSAGES_TABLE  = os.getenv("MESSAGES_TABLE", "Messages")
SUMMARIES_TABLE = os.getenv("SUMMARIES_TABLE", "Summaries")
SETTINGS_TABLE  = os.getenv("SETTINGS_TABLE",  "Settings")

users_tbl     = dynamodb.Table(USERS_TABLE)
channels_tbl  = dynamodb.Table(CHANNELS_TABLE)
threads_tbl   = dynamodb.Table(THREADS_TABLE)
messages_tbl  = dynamodb.Table(MESSAGES_TABLE)
summaries_tbl = dynamodb.Table(SUMMARIES_TABLE)
settings_tbl  = dynamodb.Table(SETTINGS_TABLE)

# ---------- Users / Channels / Threads ----------

def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = users_tbl.get_item(Key={"user_id": user_id})
        return r.get("Item")
    except Exception as e:
        logger.warning(f"get_user({user_id}) failed: {e}")
        return None

def save_user(
    user_id: str,
    username: Optional[str],
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    profile: Optional[Dict[str, Any]] = None
) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Инициализация структуры profile
    default_profile = {
        "first_name": first_name or "",
        "last_name": last_name or "",
        "communication_style": "",
        "interests": [],
        "long_term_summary": "",
        "last_topics": [],
        "message_count": 0,
    }

    item = {
        "user_id": user_id,
        "username": username or "",
        "profile": profile or default_profile,
        "created_at": now,
        "updated_at": now,
    }
    try:
        users_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_user({user_id}) failed: {e}")

def get_channel(channel_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = channels_tbl.get_item(Key={"channel_id": channel_id})
        return r.get("Item")
    except Exception as e:
        logger.warning(f"get_channel({channel_id}) failed: {e}")
        return None

def save_channel(channel_id: str, channel_name: Optional[str]) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "channel_id": channel_id,
        "channel_name": channel_name or "",
        "channel_summary": "",
        "created_at": now,
        "updated_at": now,
    }
    try:
        channels_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_channel({channel_id}) failed: {e}")

def get_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = threads_tbl.get_item(Key={"thread_id": thread_id})
        return r.get("Item")
    except Exception as e:
        logger.warning(f"get_thread({thread_id}) failed: {e}")
        return None

def save_thread(thread_id: str, thread_title: Optional[str] = "") -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "thread_id": thread_id,
        "thread_title": thread_title or "",
        "thread_summary": "",
        "meta": {},
        "created_at": now,
        "updated_at": now,
    }
    try:
        threads_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_thread({thread_id}) failed: {e}")

def update_user_names(
    user_id: str,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str]
) -> None:
    """Обновляет имя и username пользователя, если они изменились."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    update_expr_parts = ["updated_at = :u"]
    expr_vals = {":u": now}
    expr_names = {}

    if username is not None:
        update_expr_parts.append("username = :un")
        expr_vals[":un"] = username or ""

    if first_name is not None:
        update_expr_parts.append("#p.#fn = :fn")
        expr_vals[":fn"] = first_name or ""
        expr_names["#p"] = "profile"
        expr_names["#fn"] = "first_name"

    if last_name is not None:
        if "#p" not in expr_names:
            expr_names["#p"] = "profile"
        update_expr_parts.append("#p.#ln = :ln")
        expr_vals[":ln"] = last_name or ""
        expr_names["#ln"] = "last_name"

    try:
        users_tbl.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeValues=expr_vals,
            ExpressionAttributeNames=expr_names if expr_names else None,
        )
    except Exception as e:
        logger.warning(f"update_user_names({user_id}) failed: {e}")

def update_user_profile(
    user_id: str,
    *,
    communication_style: Optional[str] = None,
    interests: Optional[List[str]] = None,
    long_term_summary: Optional[str] = None,
    last_topics: Optional[List[str]] = None,
    increment_messages: bool = False,
) -> None:
    """Обновляет профиль пользователя с долгосрочной информацией."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    update_expr_parts = ["updated_at = :u"]
    expr_vals = {":u": now}
    expr_names = {"#p": "profile"}

    if communication_style is not None:
        update_expr_parts.append("#p.communication_style = :cs")
        expr_vals[":cs"] = communication_style

    if interests is not None:
        update_expr_parts.append("#p.interests = :int")
        expr_vals[":int"] = interests

    if long_term_summary is not None:
        update_expr_parts.append("#p.long_term_summary = :lts")
        expr_vals[":lts"] = long_term_summary

    if last_topics is not None:
        update_expr_parts.append("#p.last_topics = :lt")
        expr_vals[":lt"] = last_topics

    if increment_messages:
        update_expr_parts.append("#p.message_count = if_not_exists(#p.message_count, :zero) + :one")
        expr_vals[":zero"] = 0
        expr_vals[":one"] = 1

    try:
        users_tbl.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeValues=expr_vals,
            ExpressionAttributeNames=expr_names,
        )
    except Exception as e:
        logger.warning(f"update_user_profile({user_id}) failed: {e}")

def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """Возвращает профиль пользователя или None."""
    user = get_user(user_id)
    if user:
        return user.get("profile", {})
    return None

# ---------- Messages / Summaries ----------

def save_message(
    dialog_key: str,
    role: str,
    content: str,
    *,
    from_user: Optional[str] = None,
    from_username: Optional[str] = None,
    to_user: Optional[str] = None,
) -> None:
    ts_ms = int(time.time() * 1000)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # TTL: 1 year in seconds from now
    expire_at = int(time.time()) + 365 * 24 * 3600
    item = {
        "dialog_key": dialog_key,
        "timestamp": ts_ms,
        "role": role,
        "content": content,
        "from_user": from_user or "",
        "from_username": from_username or "",
        "to_user": to_user or "",
        "created_at": now,
        "expire_at": expire_at,
    }
    try:
        messages_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_message({dialog_key}, {role}) failed: {e}")

def get_dialog_history(dialog_key: str, *, limit: int = 50, consistent_read: bool = False) -> List[Dict[str, Any]]:
    try:
        r = messages_tbl.query(
            KeyConditionExpression=Key("dialog_key").eq(dialog_key),
            ScanIndexForward=False,  # newest first
            Limit=limit,
            ConsistentRead=consistent_read
        )
        items = r.get("Items", [])
        items.sort(key=lambda x: x["timestamp"])  # ascending for chat order
        return items
    except Exception as e:
        logger.warning(f"get_dialog_history({dialog_key}) failed: {e}")
        return []

def save_summary(dialog_key: str, summary: str) -> None:
    ts_ms = int(time.time() * 1000)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # TTL: 1 year in seconds from now
    expire_at = int(time.time()) + 365 * 24 * 3600
    item = {
        "dialog_key": dialog_key,
        "timestamp": ts_ms,
        "summary": summary,
        "created_at": now,
        "expire_at": expire_at,
    }
    try:
        summaries_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_summary({dialog_key}) failed: {e}")

def get_latest_summary(dialog_key: str) -> Optional[str]:
    try:
        r = summaries_tbl.query(
            KeyConditionExpression=Key("dialog_key").eq(dialog_key),
            ScanIndexForward=False,  # newest first
            Limit=1
        )
        items = r.get("Items", [])
        if items:
            return items[0].get("summary")
        return None
    except Exception as e:
        logger.warning(f"get_latest_summary({dialog_key}) failed: {e}")
        return None

# ---------- Settings ----------

def get_settings(dialog_key: str) -> Optional[Dict[str, Any]]:
    try:
        r = settings_tbl.get_item(Key={"dialog_key": dialog_key})
        return r.get("Item")
    except Exception as e:
        logger.warning(f"get_settings({dialog_key}) failed: {e}")
        return None

def save_settings(dialog_key: str, *, mode: str, meta: Optional[Dict[str, Any]] = None) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "dialog_key": dialog_key,
        "mode": (mode or "").lower(),
        "meta": meta or {},
        "created_at": now,
        "updated_at": now,
    }
    try:
        settings_tbl.put_item(Item=item)
    except Exception as e:
        logger.warning(f"save_settings({dialog_key}) failed: {e}")

def update_settings(dialog_key: str, **kwargs) -> Dict[str, Any]:
    # Supports updating 'mode' and 'meta'
    mode = kwargs.get("mode")
    meta = kwargs.get("meta")
    expr = []
    vals: Dict[str, Any] = {":u": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    names: Dict[str, str] = {}
    if mode is not None:
        expr.append("#m = :m")
        vals[":m"] = (mode or "").lower()
        names["#m"] = "mode"
    if meta is not None:
        expr.append("#mt = :mt")
        vals[":mt"] = meta
        names["#mt"] = "meta"
    expr.append("updated_at = :u")
    update_expr = "SET " + ", ".join(expr)
    try:
        r = settings_tbl.update_item(
            Key={"dialog_key": dialog_key},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=vals,
            ExpressionAttributeNames=names or None,
            ReturnValues="ALL_NEW",
        )
        return r.get("Attributes", {})
    except Exception as e:
        logger.warning(f"update_settings({dialog_key}) failed: {e}")
        # Fallback: create with defaults if missing
        try:
            save_settings(dialog_key, mode=mode or "mention", meta=meta or {})
        except Exception:
            pass
        return get_settings(dialog_key) or {"dialog_key": dialog_key, "mode": mode or "mention", "meta": meta or {}}
