"""Single entry point for every LLM call in the product (CLAUDE.md §4).

- OpenRouter's OpenAI-compatible chat API; provider and model can change without touching callers.
- Model per step lives in ROUTES (overridable from settings); fallbacks via OpenRouter `models`.
- Every call asks for structured JSON matching a Pydantic model and validates it (one retry).
- `agent()` runs a bounded tool loop (our own tools + OpenRouter web search) that must end with
  a final answer matching a Pydantic model.
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
        "lead_research": Route(model=settings.llm_model_research, temperature=0.0, max_tokens=1500),
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


@dataclass(frozen=True)
class Tool:
    """A tool the model may call in `agent()`. `run` gets validated arguments and returns text."""

    name: str
    description: str
    parameters: type[BaseModel]
    run: Callable[[Any], Awaitable[str]]


@dataclass(frozen=True)
class WebSearch:
    """OpenRouter's server-side web search tool, run by OpenRouter inside a model call."""

    max_searches: int = 4  # per agent run, all turns together
    max_results: int = 5
    engine: str = "exa"  # fixed engine: predictable price (≈ $0.007 per search)


@dataclass
class AgentResult[T: BaseModel]:
    data: T
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    turns: int
    searches: int
    steps: list[dict[str, str]]  # tool calls made, in order (name + short args), for the record


FINAL_TOOL = "submit_answer"


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
            body = await self._post(route, messages, {"response_format": _response_format(schema)})
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

    async def agent[T: BaseModel](
        self,
        step: str,
        system: str,
        user: str,
        schema: type[T],
        tools: list[Tool],
        *,
        max_turns: int,
        web_search: WebSearch | None = None,
    ) -> AgentResult[T]:
        """Bounded tool loop. Each turn is one model call; the model calls our tools (run here),
        searches the web (run by OpenRouter), and must finish by calling `submit_answer` with
        arguments matching `schema`. The last turn forces that call. Raises LLMError (with the
        usage already billed) if no valid answer comes back."""
        if not self.settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        route = routes(self.settings)[step]
        by_name = {t.name: t for t in tools}
        final = {
            "type": "function",
            "function": {
                "name": FINAL_TOOL,
                "description": "Submit the final answer. Call this exactly once, when done.",
                "parameters": _strict_schema(schema),
            },
        }
        function_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": _strict_schema(t.parameters),
                },
            }
            for t in tools
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        total_in = total_out = searches = 0
        total_cost = 0.0
        steps: list[dict[str, str]] = []
        model = route.model

        for turn in range(1, max_turns + 1):
            last_turn = turn == max_turns
            request_tools: list[dict[str, Any]] = [*function_tools, final]
            allowed = 0
            if web_search and searches < web_search.max_searches and not last_turn:
                allowed = web_search.max_searches - searches
                request_tools.append(_web_search_tool(web_search, allowed))
            extra: dict[str, Any] = {"tools": request_tools}
            if last_turn:
                extra["tool_choice"] = {"type": "function", "function": {"name": FINAL_TOOL}}
            try:
                body = await self._post(route, messages, extra)
            except LLMError as exc:
                raise LLMError(str(exc), tokens=total_in + total_out, cost_usd=total_cost) from exc
            usage = body.get("usage") or {}
            total_in += int(usage.get("prompt_tokens") or 0)
            total_out += int(usage.get("completion_tokens") or 0)
            total_cost += float(usage.get("cost") or 0.0)
            searches += _search_count(usage, allowed)
            model = body.get("model") or model
            message = (body.get("choices") or [{}])[0].get("message") or {}
            calls = [c for c in message.get("tool_calls") or [] if c.get("type", "function") == "function"]
            if not calls:
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append(
                    {"role": "user", "content": f"Continue with the tools, then call {FINAL_TOOL}."}
                )
                continue
            messages.append(
                {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
            )
            for call in calls:
                name = call.get("function", {}).get("name", "")
                raw_args = call.get("function", {}).get("arguments") or "{}"
                steps.append({"tool": name, "args": raw_args[:300]})
                if name == FINAL_TOOL:
                    try:
                        data = schema.model_validate_json(raw_args)
                    except ValidationError as exc:
                        output = f"Invalid answer, fix it and call {FINAL_TOOL} again: {exc}"
                    else:
                        return AgentResult(
                            data, model, total_in, total_out, total_cost, turn, searches, steps
                        )
                elif name in by_name:
                    output = await _run_tool(by_name[name], raw_args)
                else:
                    output = f"Unknown tool {name!r}."
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})

        raise LLMError(
            f"{step}: no valid answer after {max_turns} turns",
            tokens=total_in + total_out,
            cost_usd=total_cost,
        )

    async def _post(
        self, route: Route, messages: list[dict[str, Any]], extra: dict[str, Any]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            # `model` is required alongside `models`: OpenRouter rejects a web-search request
            # (HTTP 400 "invalid request") that names its model only in the `models` list.
            "model": route.model,
            "models": [route.model, *self.settings.fallback_models()],
            "messages": messages,
            "temperature": route.temperature,
            "max_tokens": route.max_tokens,
            # Zero data retention: never route to providers that store or train on prompts.
            "provider": {"data_collection": "deny", "zdr": True, "require_parameters": True},
            "usage": {"include": True},
            **extra,
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


async def _run_tool(tool: Tool, raw_args: str) -> str:
    """Run one of our tools. Errors go back to the model as text; they never end the loop."""
    try:
        args = tool.parameters.model_validate_json(raw_args)
    except ValidationError as exc:
        return f"Invalid arguments for {tool.name}: {exc}"
    try:
        return await tool.run(args)
    except Exception as exc:  # a tool failure is information for the model, not a crash
        log.warning("tool %s failed: %r", tool.name, exc)
        return f"{tool.name} failed: {type(exc).__name__}: {exc}"


def _web_search_tool(cfg: WebSearch, remaining: int) -> dict[str, Any]:
    return {
        "type": "openrouter:web_search",
        "parameters": {"engine": cfg.engine, "max_results": cfg.max_results, "max_uses": remaining},
    }


def _search_count(usage: dict[str, Any], allowed: int) -> int:
    """Web searches billed in one call.

    OpenRouter reports searches under `server_tool_use_details` (docs say `server_tool_use`; both
    are read). The count includes searches the model asked for after `max_uses` was reached, which
    OpenRouter refuses and doesn't bill (checked live, 2026-10-22), so it is capped at `allowed`.
    """
    server = usage.get("server_tool_use_details") or usage.get("server_tool_use") or {}
    try:
        requested = int(server.get("web_search_requests") or 0)
    except (TypeError, ValueError):
        return 0
    return min(requested, allowed)


def _response_format(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
    }


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON schema in the strict form structured outputs expect (no extra keys, all required)."""
    js = schema.model_json_schema()
    js["additionalProperties"] = False
    js["required"] = list(js.get("properties", {}))
    return js
