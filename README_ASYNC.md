# AI Petrovich — Async (SQS) Release v2 (Claude)
Date: 2025-08-16 12:46:30Z
Migrated to Claude: 2026-02-12

Components:
- webhook_lambda.py — ACKs Telegram and enqueues raw update to SQS.
- worker_lambda.py — SQS-triggered processor (full flow).
- claude_utils.py — Anthropic Claude API integration (replaced openai_utils.py).
- openai_utils.py — (legacy, kept as reference, no longer imported).

ENV (changed):
- ANTHROPIC_API_KEY (required, replaces OPENAI_API_KEY)
- CLAUDE_MODEL (default: claude-sonnet-4-5-20250929, replaces GPT_MODEL)

ENV (unchanged):
- SQS_QUEUE_URL (required for webhook)
- SQS_IS_FIFO=1 (optional, for FIFO queues)
- TELEGRAM_TOKEN, BOT_USERNAME, BOT_ID
- BASE_SYSTEM_PROMPT, MAX_CONTEXT_TOKENS, MAX_OUTPUT_TOKENS
- MIN_MSGS_FOR_SUMMARY, SUMMARY_HISTORY_LIMIT, GROUP_SCOPE_DEFAULT
- DynamoDB table names: USERS_TABLE, CHANNELS_TABLE, etc.

Dependencies (Lambda Layer):
- anthropic (replaces openai + tiktoken)
- boto3, requests

Wiring:
API Gateway -> webhook_lambda -> SQS -> worker_lambda (event source).

https://sqs.eu-west-1.amazonaws.com/803576493743/GPT_Petrovich
