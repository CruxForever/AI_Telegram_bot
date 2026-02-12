
import json
import os
import logging
import boto3
from typing import Any, Dict, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL")
SQS_IS_FIFO = os.getenv("SQS_IS_FIFO", "0") == "1"

sqs = boto3.client("sqs")

def _parse_for_keys(update: Dict[str, Any]) -> Dict[str, Any]:
    msg = (update or {}).get("message") or (update or {}).get("channel_post") or {}
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    thread_id = msg.get("message_thread_id")
    from_user = msg.get("from") or {}
    user_id = from_user.get("id")
    update_id = update.get("update_id")
    if chat_type == "private" and user_id:
        dkey = str(user_id)
    elif thread_id:
        dkey = f"{chat_id}:{thread_id}"
    else:
        dkey = str(chat_id)
    return {
        "dialog_key": str(dkey) if dkey is not None else "",
        "chat_id": str(chat_id) if chat_id is not None else "",
        "chat_type": str(chat_type) if chat_type is not None else "",
        "thread_id": str(thread_id) if thread_id is not None else "",
        "user_id": str(user_id) if user_id is not None else "",
        "update_id": str(update_id) if update_id is not None else "",
    }

def lambda_handler(event, context):
    try:
        body = event.get("body") if isinstance(event, dict) else None
        update = json.loads(body) if isinstance(body, str) else (event if isinstance(event, dict) else {})
        logger.info("WEBHOOK IN: %s", (body[:500] if isinstance(body, str) else str(event)[:500]))
    except Exception as e:
        logger.exception("Bad webhook payload: %r", e)
        return {"statusCode": 200, "body": "ignored"}

    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        return {"statusCode": 500, "body": "SQS not configured"}

    keys = _parse_for_keys(update)
    params = {
        "QueueUrl": SQS_QUEUE_URL,
        "MessageBody": json.dumps(update, ensure_ascii=False),
        "MessageAttributes": {
            "dialog_key": {"DataType": "String", "StringValue": keys["dialog_key"]},
            "chat_type":  {"DataType": "String", "StringValue": keys["chat_type"]},
            "chat_id":    {"DataType": "String", "StringValue": keys["chat_id"]},
            "thread_id":  {"DataType": "String", "StringValue": keys["thread_id"]},
            "user_id":    {"DataType": "String", "StringValue": keys["user_id"]},
            "update_id":  {"DataType": "String", "StringValue": keys["update_id"]},
        },
    }
    if SQS_IS_FIFO:
        params["MessageGroupId"] = keys["dialog_key"] or (keys["chat_id"] or "default")
        params["MessageDeduplicationId"] = keys["update_id"] or params["MessageGroupId"]

    try:
        resp = sqs.send_message(**params)
        logger.info("ENQUEUED to SQS: %s", resp.get("MessageId"))
        return {"statusCode": 200, "body": "queued"}
    except Exception as e:
        logger.exception("SQS send failed: %r", e)
        return {"statusCode": 500, "body": "enqueue failed"}
