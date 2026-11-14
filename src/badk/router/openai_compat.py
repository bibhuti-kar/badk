"""OpenAI-compatible chat backend (design 8.1). The only module that speaks `chat/completions`."""

import time
from functools import lru_cache
from typing import Any

import httpx

from badk.agents.definition import AgentDef
from badk.config import Settings
from badk.engine.tokens import estimate_messages, estimate_tokens
from badk.errors import BadkError, Code
from badk.exec.document import Message
from badk.router.reply import ModelReply, count, fail


def wire_messages(messages: list[Message]) -> list[dict[str, str]]:
    """Adapt the engine payload for local chat templates (payload itself stays as designed).

    Leading system messages merge into one (prefix bytes first, joined by a blank line); a
    trailing system marker with no user turn after the system block is sent as role user.
    """
    n = 0
    while n < len(messages) and messages[n].role == "system":
        n += 1
    if n == len(messages) and n >= 2:
        n -= 1  # system-only payload (post-compression marker): the last one becomes the user turn
    rest = [{"role": m.role, "content": m.content} for m in messages[n:]]
    out = [{"role": "system", "content": "\n\n".join(m.content for m in messages[:n])}] if n else []
    if rest and rest[-1]["role"] == "system" and all(m["role"] != "user" for m in rest):
        rest[-1]["role"] = "user"
    return out + rest


def _fail(code: Code, why: str) -> BadkError:
    return fail("openai", code, why)


@lru_cache(maxsize=4)
def _client(timeout: int) -> httpx.Client:
    # Deviation from design 8.1 "client per call": one pooled client per timeout value (keep-alive).
    return httpx.Client(timeout=timeout)


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> dict[str, Any]:
    try:
        resp = _client(timeout).post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise _fail(Code.PROVIDER_TIMEOUT, f"no reply within {timeout}s") from e
    except httpx.HTTPError as e:
        raise _fail(Code.PROVIDER_ERROR, f"transport error: {e}") from e
    if resp.status_code >= 400:
        raise _fail(Code.PROVIDER_ERROR, f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise _fail(Code.PROVIDER_ERROR, "reply is not JSON") from e
    if not isinstance(data, dict):
        raise _fail(Code.PROVIDER_ERROR, "reply is not a JSON object")
    return data


def _text(data: dict[str, Any]) -> str:
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise _fail(Code.PROVIDER_ERROR, "reply has no choices[0].message.content") from e
    if not isinstance(text, str):
        raise _fail(Code.PROVIDER_ERROR, "reply content is not a string")
    return text


def complete(agent: AgentDef, messages: list[Message], settings: Settings) -> ModelReply:
    url = settings.llm_base_url.rstrip("/") + "/v1/chat/completions"
    key = settings.llm_api_key.get_secret_value()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {
        "model": agent.model,
        "messages": wire_messages(messages),
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    t0 = time.perf_counter()
    data = _post(url, headers, body, settings.provider_timeout_sec)
    latency = int((time.perf_counter() - t0) * 1000)
    text = _text(data)
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return ModelReply(
        text,
        count(usage.get("prompt_tokens"), estimate_messages(messages)),
        count(usage.get("completion_tokens"), estimate_tokens(text)),
        latency,
    )
