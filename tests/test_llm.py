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
    assert body["model"] == "anthropic/claude-haiku-4.5"
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


# --- agent loop -------------------------------------------------------------------------------

from app.llm import FINAL_TOOL, Tool, WebSearch, _search_count  # noqa: E402


class Args(BaseModel):
    url: str


def _call(name: str, args: dict | str, call_id: str = "c1") -> dict:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def _turn(*calls: dict, searches: int = 0, content: str = "", details: bool = False) -> httpx.Response:
    key = "server_tool_use_details" if details else "server_tool_use"
    return httpx.Response(
        200,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "choices": [{"message": {"content": content, "tool_calls": list(calls)}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost": 0.001,
                key: {"web_search_requests": searches},
            },
        },
    )


def _fetch_tool(seen: list[str]) -> Tool:
    async def run(args: Args) -> str:
        seen.append(args.url)
        if "boom" in args.url:
            raise RuntimeError("connection reset")
        return f"<page>{args.url}</page>"

    return Tool("fetch_page", "Read a page", Args, run)


async def test_agent_runs_tools_and_returns_final_answer():
    requests, fetched = [], []
    replies = iter(
        [
            _turn(_call("fetch_page", {"url": "https://a.ca"}), searches=2),
            _turn(_call("fetch_page", {"url": "https://boom.ca"}, "c2")),
            _turn(_call(FINAL_TOOL, {"score": 80, "reason": "found"}, "c3")),
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return next(replies)

    result = await _llm(handler).agent(
        "lead_research",
        "sys",
        "user",
        Out,
        [_fetch_tool(fetched)],
        max_turns=5,
        web_search=WebSearch(max_searches=3),
    )
    assert result.data == Out(score=80, reason="found")
    assert (result.turns, result.searches, result.cost_usd) == (3, 2, pytest.approx(0.003))
    assert fetched == ["https://a.ca", "https://boom.ca"]
    assert [s["tool"] for s in result.steps] == ["fetch_page", "fetch_page", FINAL_TOOL]

    first, second, third = requests
    names = [t.get("function", {}).get("name") or t["type"] for t in first["tools"]]
    assert names == ["fetch_page", FINAL_TOOL, "openrouter:web_search"]
    search = first["tools"][2]["parameters"]
    assert search["engine"] == "exa" and search["max_uses"] == 3
    assert second["tools"][2]["parameters"]["max_uses"] == 1  # 2 of 3 searches used
    assert third["provider"]["zdr"] is True and "response_format" not in third
    # Tool results go back as tool messages; a failing tool is reported, not fatal.
    tool_msgs = [m for m in third["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "<page>https://a.ca</page>"
    assert "connection reset" in tool_msgs[1]["content"]


async def test_agent_search_dropped_when_budget_used_and_last_turn_forces_answer():
    requests = []
    replies = iter(
        [
            _turn(_call("fetch_page", {"url": "https://a.ca"}), searches=1),
            _turn(content="thinking..."),  # no tool call: nudged to continue
            _turn(_call(FINAL_TOOL, {"score": 5, "reason": "none"})),
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return next(replies)

    result = await _llm(handler).agent(
        "lead_research", "s", "u", Out, [_fetch_tool([])], max_turns=3, web_search=WebSearch(max_searches=1)
    )
    assert result.data.score == 5
    assert all(t["type"] == "function" for t in requests[1]["tools"])  # search budget spent
    assert "tool_choice" not in requests[1]
    assert requests[2]["tool_choice"] == {"type": "function", "function": {"name": FINAL_TOOL}}
    assert requests[2]["messages"][-1]["content"].startswith("Continue with the tools")


async def test_agent_invalid_answer_and_unknown_tool_fed_back_then_fails_with_cost():
    replies = iter(
        [
            _turn(_call(FINAL_TOOL, {"score": "high"})),
            _turn(_call("delete_everything", {})),
            _turn(_call(FINAL_TOOL, "not json")),
        ]
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return next(replies)

    with pytest.raises(LLMError, match="no valid answer after 3 turns") as info:
        await _llm(handler).agent("lead_research", "s", "u", Out, [], max_turns=3)
    assert info.value.cost_usd == pytest.approx(0.003) and info.value.tokens == 330
    tool_msgs = [m["content"] for m in seen[2]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0].startswith("Invalid answer") and tool_msgs[1] == "Unknown tool 'delete_everything'."


async def test_agent_http_failure_keeps_billed_usage():
    replies = iter([_turn(_call("fetch_page", {"url": "https://a.ca"})), httpx.Response(401)])
    with pytest.raises(LLMError, match="HTTP 401") as info:
        await _llm(lambda r: next(replies)).agent(
            "lead_research", "s", "u", Out, [_fetch_tool([])], max_turns=4
        )
    assert info.value.cost_usd == pytest.approx(0.001)


@pytest.mark.parametrize(
    ("usage", "allowed", "expected"),
    [
        # Live OpenRouter responses (2026-10-22) use server_tool_use_details.
        ({"server_tool_use_details": {"web_search_requests": 3}}, 4, 3),
        ({"server_tool_use": {"web_search_requests": 2}}, 4, 2),
        # Searches asked for beyond max_uses are refused and not billed.
        ({"server_tool_use_details": {"web_search_requests": 6}}, 4, 4),
        ({"server_tool_use_details": {"web_search_requests": 2}}, 0, 0),
        ({}, 4, 0),
        ({"server_tool_use_details": {"web_search_requests": "x"}}, 4, 0),
    ],
)
def test_search_count(usage, allowed, expected):
    assert _search_count(usage, allowed) == expected


async def test_agent_search_budget_counts_refused_searches_once():
    """The model asks for more searches than allowed: the budget is spent, not overdrawn."""
    requests = []
    replies = iter(
        [
            _turn(_call("fetch_page", {"url": "https://a.ca"}), searches=6, details=True),
            _turn(_call(FINAL_TOOL, {"score": 5, "reason": "none"})),
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return next(replies)

    result = await _llm(handler).agent(
        "lead_research", "s", "u", Out, [_fetch_tool([])], max_turns=4, web_search=WebSearch(max_searches=4)
    )
    assert result.searches == 4
    assert requests[0]["tools"][2]["parameters"]["max_uses"] == 4
    assert all(t["type"] == "function" for t in requests[1]["tools"])


async def test_drafting_omits_temperature_scoring_keeps_it():
    """Live 2026-10-22: no zero-retention provider of claude-sonnet-5.5 accepts `temperature`."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _reply('{"score": 1, "reason": "x"}')

    llm = _llm(handler)
    await llm.structured("email_draft", "s", "u", Out)
    await llm.structured("lead_scoring", "s", "u", Out)
    assert "temperature" not in seen[0] and seen[0]["model"] == "anthropic/claude-sonnet-5.5"
    assert seen[0]["reasoning"] == {"effort": "low"} and seen[0]["max_tokens"] == 2000
    assert seen[1]["temperature"] == 0.0 and "reasoning" not in seen[1]
