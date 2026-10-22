"""Single entry point for every LLM call in the product (CLAUDE.md §4).

- OpenRouter's OpenAI-compatible chat API; provider and model can change without touching callers.
- Model per step lives in ROUTES (overridable from settings); fallbacks via OpenRouter `models`.
- Every call asks for structured JSON matching a Pydantic model and validates it (one retry).
- Zero data retention: providers that store or train on prompts are excluded.
- Usage and cost are returned so the caller can record them on its run.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# A 429 says when the per-minute window resets; wait for it, but never longer than this.
MAX_RATE_LIMIT_WAIT = 65.0


@dataclass(frozen=True)
class Route:
    model: str
    temperature: float = 0.0
    max_tokens: int = 400


def routes(settings: Settings) -> dict[str, Route]:
    """Model per step. Cheap models for bulk steps; stronger ones for client-facing drafting (later)."""
    return {
        "lead_scoring": Route(model=settings.llm_model_scoring, temperature=0.0, max_tokens=300),
    }


@dataclass
class LLMResult[T: BaseModel]:
    data: T
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class LLMError(Exception):
    """A failed step. Carries the usage already billed for it (e.g. invalid output, then retry)."""

    def __init__(self, message: str, tokens: int = 0, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.tokens = tokens
        self.cost_usd = cost_usd


class LLM:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self.sleep = sleep

    async def aclose(self) -> None:
        await self.http.aclose()

    async def structured[T: BaseModel](
        self, step: str, system: str, user: str, schema: type[T]
    ) -> LLMResult[T]:
        """Run one step and return its validated structured output."""
        if not self.settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        route = routes(self.settings)[step]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        total_in = total_out = 0
        total_cost = 0.0
        for attempt in (1, 2):
            body = await self._post(route, messages, schema)
            usage = body.get("usage") or {}
            total_in += int(usage.get("prompt_tokens") or 0)
            total_out += int(usage.get("completion_tokens") or 0)
            total_cost += float(usage.get("cost") or 0.0)
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                data = schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt == 2:
                    raise LLMError(
                        f"{step}: invalid structured output after retry: {exc}",
                        tokens=total_in + total_out,
                        cost_usd=total_cost,
                    ) from exc
                log.warning("%s: invalid structured output, retrying once", step)
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Invalid: {exc}. Reply with JSON matching the schema only."},
                ]
                continue
            return LLMResult(data, body.get("model") or route.model, total_in, total_out, total_cost)
        raise AssertionError("unreachable")

    async def _post(
        self, route: Route, messages: list[dict[str, str]], schema: type[BaseModel]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "models": [route.model, *self.settings.fallback_models()],
            "messages": messages,
            "temperature": route.temperature,
            "max_tokens": route.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
            },
            # Zero data retention: never route to providers that store or train on prompts.
            "provider": {"data_collection": "deny", "zdr": True, "require_parameters": True},
            "usage": {"include": True},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "X-Title": "Solecra agents",
        }
        last = ""
        for attempt in range(1, 4):
            try:
                resp = await self.http.post(API_URL, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last = f"network error: {exc!r}"
            else:
                if resp.status_code < 300:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
                if resp.status_code == 429 and attempt < 3:
                    wait = _rate_limit_wait(resp)
                    log.warning("OpenRouter rate limit; waiting %.0f s", wait)
                    await self.sleep(wait)
                    continue
            if attempt < 3:
                await self.sleep(2 ** (attempt - 1))
        raise LLMError(f"OpenRouter call failed: {last}")


def _rate_limit_wait(resp: httpx.Response) -> float:
    """Seconds until the rate-limit window resets (`X-RateLimit-Reset`, epoch ms), else Retry-After."""
    try:
        if reset := resp.headers.get("x-ratelimit-reset"):
            wait = float(reset) / 1000 - time.time() + 0.5
        else:
            wait = float(resp.headers.get("retry-after") or 2)
    except ValueError:
        wait = 2.0
    return min(max(wait, 1.0), MAX_RATE_LIMIT_WAIT)


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON schema in the strict form structured outputs expect (no extra keys, all required)."""
    js = schema.model_json_schema()
    js["additionalProperties"] = False
    js["required"] = list(js.get("properties", {}))
    return js
