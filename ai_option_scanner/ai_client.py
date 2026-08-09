from __future__ import annotations

import http.client
import json
import logging
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterator

from .ai_providers import DEFAULT_PROVIDER_NAME, AIProvider, load_providers
from .ai_provider_store import get_user_provider
from .concurrency import ai_limiter
from .ai_usage_store import record_ai_usage_event


logger = logging.getLogger(__name__)
_LAST_AI_ERROR = threading.local()
_AI_USAGE_CONTEXT = threading.local()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _provider_has_credentials(provider: AIProvider) -> bool:
    return bool(provider.api_key or os.getenv(provider.api_key_env))


def ask_ai(
    system_prompt: str,
    user_payload: dict[str, Any],
    provider_name: str = DEFAULT_PROVIDER_NAME,
    owner_id: str | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
) -> str | None:
    _set_last_ai_error("")
    user_provider = get_user_provider(owner_id, provider_name)
    if user_provider is not None:
        # A user explicitly chose this provider — honor it, don't silently
        # fail over to a different one behind their back.
        return ask_provider(user_provider, system_prompt, user_payload, owner_id=owner_id, temperature=temperature, response_format=response_format)

    providers = load_providers()
    primary = providers.get(provider_name)
    if primary is None:
        return None

    # Failover chain: primary first, then any other credentialed provider in
    # config order. A single provider outage (timeout / 5xx / rate limit) no
    # longer takes AI features down — we try the next one before giving up.
    chain: list[AIProvider] = [primary]
    for name, provider in providers.items():
        if name != provider_name and _provider_has_credentials(provider):
            chain.append(provider)

    last_error = ""
    for index, provider in enumerate(chain):
        answer = ask_provider(provider, system_prompt, user_payload, owner_id=owner_id, temperature=temperature, response_format=response_format)
        if answer is not None:
            if index > 0:
                logger.info("AI failover succeeded on provider=%s after %d failed attempt(s)", provider.name, index)
            return answer
        last_error = get_last_ai_error()
        if index + 1 < len(chain):
            logger.warning("AI provider=%s failed (%s); failing over to provider=%s", provider.name, last_error or "unknown", chain[index + 1].name)
    return None


def ask_provider(
    provider: AIProvider,
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    owner_id: str | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
) -> str | None:
    if provider.provider_type == "claude":
        return ask_claude_compatible(provider, system_prompt, user_payload, owner_id=owner_id, temperature=temperature)
    return ask_openai_compatible(provider, system_prompt, user_payload, owner_id=owner_id, temperature=temperature, response_format=response_format)


def ask_openai_compatible(
    provider: AIProvider,
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    owner_id: str | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
) -> str | None:
    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return None

    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 2.0))
    body = {
        "model": provider.model,
        "temperature": temp,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    if response_format:
        body["response_format"] = response_format
    request = urllib.request.Request(
        f"{provider.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with ai_limiter.acquire():
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            _record_usage(provider, owner_id=owner_id, usage=result.get("usage") or {}, status="succeeded")
            return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("AI request failed for provider=%s model=%s: %s", provider.name, provider.model, detail)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            detail = str(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("AI request failed for provider=%s model=%s: %s", provider.name, provider.model, detail)
            return None


def ask_claude_compatible(
    provider: AIProvider,
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    owner_id: str | None = None,
    temperature: float | None = None,
) -> str | None:
    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return None

    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 1.0))
    body = {
        "model": provider.model,
        "system": system_prompt,
        "temperature": temp,
        "max_tokens": int(os.getenv("ANTHROPIC_MAX_TOKENS", "2048")),
        "messages": [
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        f"{provider.base_url.rstrip('/')}/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with ai_limiter.acquire():
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            _record_usage(provider, owner_id=owner_id, usage=_claude_usage(result), status="succeeded")
            return _claude_text_content(result)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("AI request failed for provider=%s model=%s type=claude: %s", provider.name, provider.model, detail)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            detail = str(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("AI request failed for provider=%s model=%s type=claude: %s", provider.name, provider.model, detail)
            return None


def _claude_text_content(result: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    text = "\n".join(part for part in parts if part).strip()
    return text or None


def resolve_chat_provider(provider_name: str, owner_id: str | None = None) -> AIProvider | None:
    """Resolve the provider an agentic chat turn should use, honoring a user's
    own provider first, then the named config provider. Returns None when no
    credentialed provider is available."""
    user_provider = get_user_provider(owner_id, provider_name)
    if user_provider is not None:
        return user_provider
    providers = load_providers()
    primary = providers.get(provider_name)
    if primary is not None and _provider_has_credentials(primary):
        return primary
    for provider in providers.values():
        if _provider_has_credentials(provider):
            return provider
    return None


def chat_with_tools(
    provider: AIProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    owner_id: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """One hop of an OpenAI-style tool-calling conversation.

    Returns a dict: {"ok": bool, "supported": bool, "message": <assistant
    message or None>, "error": str}. `supported` is False for providers we
    can't drive with the OpenAI tools schema (e.g. claude-type), letting the
    caller fall back to the non-agentic pipeline. The assistant message may
    contain `tool_calls`; the caller dispatches and appends tool results."""
    if provider.provider_type == "claude":
        # Claude uses a different tool schema/loop; not wired here. Signal the
        # caller to fall back rather than silently degrading.
        return {"ok": False, "supported": False, "message": None, "error": "tool-calling not supported for claude provider"}

    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return {"ok": False, "supported": True, "message": None, "error": "missing api key"}

    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 2.0))
    body = {
        "model": provider.model,
        "temperature": temp,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    request = urllib.request.Request(
        f"{provider.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with ai_limiter.acquire():
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            _record_usage(provider, owner_id=owner_id, usage=result.get("usage") or {}, status="succeeded")
            message = result["choices"][0]["message"]
            return {"ok": True, "supported": True, "message": message, "error": ""}
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("chat_with_tools failed provider=%s model=%s: %s", provider.name, provider.model, detail)
            # A 400 here often means the provider/model rejects the tools schema
            # → treat as unsupported so the caller falls back to the fixed pipeline.
            unsupported = exc.code == 400
            return {"ok": False, "supported": not unsupported, "message": None, "error": detail}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            detail = str(exc)
            _set_last_ai_error(detail)
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
            logger.warning("chat_with_tools failed provider=%s model=%s: %s", provider.name, provider.model, detail)
            return {"ok": False, "supported": True, "message": None, "error": detail}


def ask_deepseek(system_prompt: str, user_payload: dict[str, Any]) -> str | None:
    return ask_ai(system_prompt, user_payload, DEFAULT_PROVIDER_NAME)


@contextmanager
def ai_usage_context(**metadata: Any) -> Iterator[None]:
    previous = getattr(_AI_USAGE_CONTEXT, "value", {}) or {}
    _AI_USAGE_CONTEXT.value = {**previous, **{key: value for key, value in metadata.items() if value not in (None, "")}}
    try:
        yield
    finally:
        _AI_USAGE_CONTEXT.value = previous


def get_last_ai_error() -> str:
    return str(getattr(_LAST_AI_ERROR, "message", "") or "")


def _set_last_ai_error(message: str) -> None:
    _LAST_AI_ERROR.message = message


def _record_usage(provider: AIProvider, *, owner_id: str | None, usage: dict[str, Any], status: str, error: str | None = None) -> None:
    try:
        context = dict(getattr(_AI_USAGE_CONTEXT, "value", {}) or {})
        record_ai_usage_event(
            owner_id=owner_id or context.get("owner_id"),
            provider=provider.name,
            model=provider.model,
            provider_type=provider.provider_type,
            usage=usage,
            context=context,
            status=status,
            error=error,
        )
    except Exception as exc:
        logger.debug("failed to record AI usage: %s", exc)


def _claude_usage(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
    }


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    reason = f"HTTP {exc.code}: {exc.reason}"
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    if not body:
        return reason
    return f"{reason} · {body[:300]}"


def stream_ask_ai(
    system_prompt: str,
    user_payload: dict[str, Any],
    provider_name: str = DEFAULT_PROVIDER_NAME,
    owner_id: str | None = None,
    temperature: float | None = None,
) -> Generator[str, None, None]:
    """Stream AI response tokens via SSE. Yields content delta strings."""
    _set_last_ai_error("")
    user_provider = get_user_provider(owner_id, provider_name)
    if user_provider is not None:
        provider = user_provider
    else:
        providers = load_providers()
        provider = providers.get(provider_name)
        if provider is None:
            return

    if provider.provider_type == "claude":
        yield from _stream_claude(provider, system_prompt, user_payload, owner_id, temperature)
    else:
        yield from _stream_openai(provider, system_prompt, user_payload, owner_id, temperature)


def _stream_openai(
    provider: AIProvider,
    system_prompt: str,
    user_payload: dict[str, Any],
    owner_id: str | None,
    temperature: float | None,
) -> Generator[str, None, None]:
    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return

    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 2.0))
    body = {
        "model": provider.model,
        "temperature": temp,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    url = urllib.parse.urlparse(provider.base_url)
    path = url.path.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    with ai_limiter.acquire():
        try:
            conn = _https_conn(url.netloc)
            conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read().decode("utf-8", errors="replace")[:300]
                _set_last_ai_error(f"HTTP {resp.status}: {detail}")
                _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
                return

            buffer = ""
            for chunk in _read_chunks(resp):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
            conn.close()
        except Exception as exc:
            _set_last_ai_error(str(exc))
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=str(exc))


def stream_chat_messages(
    provider: AIProvider,
    messages: list[dict[str, Any]],
    *,
    owner_id: str | None = None,
    temperature: float | None = None,
) -> Generator[str, None, None]:
    """Stream the final assistant answer for an OpenAI-style message list.
    Used by the agentic chat loop to stream tokens once the model stops
    requesting tools. Claude providers are not supported here (the agent loop
    only runs for OpenAI-compatible providers)."""
    if provider.provider_type == "claude":
        return
    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return
    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 2.0))
    body = {
        "model": provider.model,
        "temperature": temp,
        "stream": True,
        "messages": messages,
    }
    url = urllib.parse.urlparse(provider.base_url)
    path = url.path.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    with ai_limiter.acquire():
        try:
            conn = _https_conn(url.netloc)
            conn.request("POST", path, body=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read().decode("utf-8", errors="replace")[:300]
                _set_last_ai_error(f"HTTP {resp.status}: {detail}")
                _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
                return
            buffer = ""
            for chunk in _read_chunks(resp):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
            conn.close()
        except Exception as exc:
            _set_last_ai_error(str(exc))
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=str(exc))



def _stream_claude(
    provider: AIProvider,
    system_prompt: str,
    user_payload: dict[str, Any],
    owner_id: str | None,
    temperature: float | None,
) -> Generator[str, None, None]:
    api_key = provider.api_key or os.getenv(provider.api_key_env)
    if not api_key:
        return

    temp = provider.temperature if temperature is None else max(0.0, min(float(temperature), 1.0))
    body = {
        "model": provider.model,
        "system": system_prompt,
        "temperature": temp,
        "max_tokens": int(os.getenv("ANTHROPIC_MAX_TOKENS", "2048")),
        "stream": True,
        "messages": [
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    url = urllib.parse.urlparse(provider.base_url)
    path = url.path.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    with ai_limiter.acquire():
        try:
            conn = _https_conn(url.netloc)
            conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read().decode("utf-8", errors="replace")[:300]
                _set_last_ai_error(f"HTTP {resp.status}: {detail}")
                _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=detail)
                return

            buffer = ""
            for chunk in _read_chunks(resp):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    try:
                        obj = json.loads(data)
                        if obj.get("type") == "content_block_delta":
                            text = obj.get("delta", {}).get("text", "")
                            if text:
                                yield text
                    except (json.JSONDecodeError, KeyError):
                        continue
            conn.close()
        except Exception as exc:
            _set_last_ai_error(str(exc))
            _record_usage(provider, owner_id=owner_id, usage={}, status="failed", error=str(exc))


def _https_conn(netloc: str) -> http.client.HTTPSConnection:
    host, _, port = netloc.partition(":")
    return http.client.HTTPSConnection(host, port=int(port) if port else 443, timeout=90)


def _read_chunks(response: http.client.HTTPResponse, chunk_size: int = 4096) -> Generator[bytes, None, None]:
    while True:
        chunk = response.read(chunk_size)
        if not chunk:
            break
        yield chunk
