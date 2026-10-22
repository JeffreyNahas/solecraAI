import json
import time

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import LLM, LLMError


class Out(BaseModel):
    score: int
    reason: str


def _settings(**kw):
    return Settings(
        _env_file=None, openrouter_api_key="k", llm_fallback_models="a/fallback-1, b/fallback-2", **kw
    )


def _reply(content: str, cost: float = 0.0001, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost},
        },
    )


def _llm(handler, settings=None):
    async def no_sleep(_):
        return None

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLM(settings or _settings(), http=http, sleep=no_sleep)


async def test_structured_output_payload_and_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _reply('{"score": 72, "reason": "new condos"}')

    result = await _llm(handler).structured("lead_scoring", "sys", "user", Out)
    assert result.data == Out(score=72, reason="new condos")
    assert (result.prompt_tokens, result.completion_tokens, result.cost_usd) == (100, 20, 0.0001)
    body = seen[0]
    assert body["models"] == ["anthropic/claude-haiku-4.5", "a/fallback-1", "b/fallback-2"]
    assert body["provider"]["data_collection"] == "deny" and body["provider"]["zdr"] is True
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] and schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {"score", "reason"}


async def test_invalid_output_retried_once_then_fails():
    replies = iter([_reply("not json"), _reply('{"score": 50, "reason": "ok"}')])
    result = await _llm(lambda r: next(replies)).structured("lead_scoring", "s", "u", Out)
    assert result.data.score == 50 and result.cost_usd == pytest.approx(0.0002)

    with pytest.raises(LLMError, match="invalid structured output") as err:
        await _llm(lambda r: _reply('{"score": "high"}')).structured("lead_scoring", "s", "u", Out)
    assert err.value.tokens == 240 and err.value.cost_usd == pytest.approx(0.0002)  # both calls billed


async def test_server_errors_retried_client_errors_not():
    replies = iter([httpx.Response(503), _reply('{"score": 1, "reason": "x"}')])
    assert (await _llm(lambda r: next(replies)).structured("lead_scoring", "s", "u", Out)).data.score == 1

    calls = []

    def bad(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMError, match="HTTP 401"):
        await _llm(bad).structured("lead_scoring", "s", "u", Out)
    assert len(calls) == 1


async def test_rate_limit_waits_until_reset():
    waits = []

    async def record(seconds):
        waits.append(seconds)

    reset_ms = str(int((time.time() + 20) * 1000))
    replies = iter(
        [
            httpx.Response(429, headers={"X-RateLimit-Reset": reset_ms}),
            httpx.Response(429, headers={"X-RateLimit-Reset": str(int((time.time() + 3600) * 1000))}),
            _reply('{"score": 3, "reason": "x"}'),
        ]
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(replies)))
    result = await LLM(_settings(), http=http, sleep=record).structured("lead_scoring", "s", "u", Out)
    assert result.data.score == 3
    assert 15 < waits[0] <= 21  # waits for the window to reset, not 1 s
    assert waits[1] == 65.0  # capped


async def test_missing_key_fails_fast():
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        no_key = Settings(_env_file=None, openrouter_api_key="")
        await _llm(lambda r: _reply("{}"), no_key).structured("lead_scoring", "s", "u", Out)
