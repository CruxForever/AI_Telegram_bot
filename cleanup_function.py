# cleanup_function.py

import logging, os, boto3
from datetime import datetime, timedelta

logger = logging.getLogger(); logger.setLevel(logging.INFO)
MESSAGES_TABLE = os.getenv("MESSAGES_TABLE","Messages")

def cleanup_handler(event, context):
    # Рекомендуется включить DynamoDB TTL на поле created_at/expire_at.
    # При включенном TTL DynamoDB удаляет записи автоматически.
    logger.info("Cleanup invoked; relying on DynamoDB TTL.")
    return {"statusCode":200,"body":"Done"}
