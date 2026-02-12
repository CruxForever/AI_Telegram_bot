# openai_utils.py

import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# --------- OpenAI SDK compatibility (v1.x and legacy v0.x) ---------
_mode = None  # "v1" | "v0" | None
_client = None

def _init_client():
    global _mode, _client
    if _mode is not None:
        return
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY is missing in environment")
        _mode = None
        _client = None
        return

    try:
        from openai import OpenAI  # type: ignore
        _client = OpenAI(api_key=api_key)
        _mode = "v1"
        logger.info("OpenAI SDK mode: v1")
        return
    except Exception as e:
        logger.warning("OpenAI v1 import failed, falling back to legacy v0.x: %s", e)

    try:
        import openai  # type: ignore
        openai.api_key = api_key
        _client = openai
        _mode = "v0"
        logger.info("OpenAI SDK mode: v0 (legacy)")
    except Exception as e:
        logger.error("OpenAI client init failed: %s", e)
        _mode = None
        _client = None

_init_client()

# ---- tiktoken-based token counting ----
def _encoding_for(model: str):
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model)
    except Exception:
        try:
            import tiktoken
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None

def num_tokens_from_messages(messages: List[Dict[str, Any]], model: Optional[str] = None) -> int:
    enc = _encoding_for(model or GPT_MODEL)
    if enc is None:
        return sum(len(m.get("content", "")) for m in messages) // 4
    tokens = 0
    for m in messages:
        tokens += 4
        tokens += len(enc.encode(m.get("content", "")))
    tokens += 2
    return tokens

def _chat_completion(messages: List[Dict[str, Any]], temperature: float, max_tokens: int) -> str:
    if _mode is None or _client is None:
        logger.error("OpenAI client is not configured (mode=%s)", _mode)
        return "⚠️ OpenAI client is not configured."
    model = os.getenv("GPT_MODEL", GPT_MODEL)
    try:
        # prepare kwargs
        kwargs = {"model": model, "messages": messages}
        # GPT-5 models only support default temperature (1) and reject others
        if not model.startswith("gpt-5"):
            kwargs["temperature"] = temperature

        if _mode == "v1":
            try:
                kwargs["max_completion_tokens"] = max_tokens
                resp = _client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content.strip()
            except Exception as e1:
                logger.warning("max_completion_tokens failed, retrying with max_tokens: %s", e1)
                kwargs.pop("max_completion_tokens", None)
                kwargs["max_tokens"] = max_tokens
                resp = _client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content.strip()
        else:
            try:
                kwargs["max_completion_tokens"] = max_tokens
                resp = _client.ChatCompletion.create(**kwargs)
                return resp["choices"][0]["message"]["content"].strip()
            except Exception as e1:
                logger.warning("max_completion_tokens failed (legacy), retrying with max_tokens: %s", e1)
                kwargs.pop("max_completion_tokens", None)
                kwargs["max_tokens"] = max_tokens
                resp = _client.ChatCompletion.create(**kwargs)
                return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        try:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            body   = getattr(getattr(e, "response", None), "text", None)
            logger.error("OpenAI error: status=%s body=%s exc=%r", status, body, e)
        except Exception:
            logger.exception("OpenAI API error (no body)")
        # Try fallback to Responses API for v1
        if _mode == "v1":
            try:
                resp = _client.responses.create(model=model, input=messages)
                return resp.output[0].content[0].text.strip()
            except Exception as e2:
                logger.error("Responses API fallback failed: %r", e2)
        return "⚠️ Не удалось получить ответ от модели."

def generate_response(messages: List[Dict[str, Any]], *, temperature: float = 0.5, max_tokens: int = 800) -> str:
    return _chat_completion(messages, temperature, max_tokens)

def summarize_history(history: List[Dict[str, Any]]) -> str:
    sys_msg = {"role": "system", "content": "Summarize the dialogue briefly in Russian. Focus on user goals, facts, and decisions."}
    few = [{"role": m["role"], "content": m["content"]} for m in history[-16:]]
    msgs = [sys_msg] + few
    return _chat_completion(msgs, temperature=0.2, max_tokens=200)
