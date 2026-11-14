"""Native Anthropic Messages API backend (API key auth, no claude harness)."""

import time
from functools import lru_cache

import anthropic

from badk.agents.definition import AgentDef
from badk.config import Settings
from badk.engine.tokens import estimate_messages, estimate_tokens
from badk.errors import BadkError, Code
from badk.exec.document import Message
from badk.router.claude_cli import render_prompt
from badk.router.reply import ModelReply, count, fail


def _fail(code: Code, why: str) -> BadkError:
    return fail("anthropic", code, why)


@lru_cache(maxsize=4)
def _client(key: str, timeout: int) -> anthropic.Anthropic:
    # max_retries=0: the engine owns retries (max_tries), so every try is one traced call.
    # Base URL: SDK default, or ANTHROPIC_BASE_URL read by the SDK itself.
    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)


def complete(agent: AgentDef, messages: list[Message], settings: Settings) -> ModelReply:
    key = settings.llm_api_key.get_secret_value()
    if not key:
        raise _fail(Code.PROVIDER_UNAVAILABLE, "LLM_API_KEY is empty")
    system, user = render_prompt(messages)  # same prompt shape as claude_cli
    t0 = time.perf_counter()
    try:
        resp = _client(key, settings.provider_timeout_sec).messages.create(
            model=str(agent.model),
            max_tokens=settings.llm_max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APITimeoutError as e:
        raise _fail(
            Code.PROVIDER_TIMEOUT, f"no reply within {settings.provider_timeout_sec}s"
        ) from e
    except anthropic.APIStatusError as e:
        raise _fail(Code.PROVIDER_ERROR, f"HTTP {e.status_code}: {str(e.message)[:200]}") from e
    except anthropic.APIError as e:  # connection and other transport errors
        raise _fail(Code.PROVIDER_ERROR, f"transport error: {type(e).__name__}") from e
    latency = int((time.perf_counter() - t0) * 1000)
    # refusal / max_tokens: the reply is returned (usage counted); its bad envelope fails validation
    text = "".join(b.text for b in resp.content if b.type == "text")  # thinking blocks skipped
    u = resp.usage
    tin = count(u.input_tokens, estimate_messages(messages))
    tin += count(u.cache_read_input_tokens, 0) + count(u.cache_creation_input_tokens, 0)
    return ModelReply(text, tin, count(u.output_tokens, estimate_tokens(text)), latency)
