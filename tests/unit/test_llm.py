import json
from types import SimpleNamespace

import httpx
import pytest

from research_agent.adapters.llm import LLM, strict_schema
from research_agent.contracts import ResearchRequest, UnderstandingDecision
from research_agent.errors import Stopped, ToolFailure


def test_invalid_reasoning_configuration_is_rejected_before_any_http(settings, monkeypatch):
    from research_agent.adapters.llm import EFFORT

    monkeypatch.setitem(EFFORT, "repair", "low plus accidental prompt text")
    with pytest.raises(ValueError, match="reasoning configuration"):
        settings.snapshot()


async def test_openrouter_adapter_locks_model_and_excludes_secrets(settings):
    bodies = []

    def response(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-5.6-luna",
                "id": "fixture-provider-request",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "{}", "reasoning": "DO NOT SAVE PRIVATE REASONING"},
                    }
                ],
                "usage": {"total_tokens": 111, "prompt_tokens": 100, "completion_tokens": 11},
            },
        )

    llm = LLM(settings, SimpleNamespace(config=settings.snapshot()), transport=httpx.MockTransport(response))
    body, reserved = llm.build(
        "understand",
        ResearchRequest(query="What is a compiler?").model_dump(mode="json"),
        UnderstandingDecision,
    )
    result = await llm.request(body, 10)
    assert body["model"] == "openai/gpt-5.6-luna"
    assert "temperature" not in body
    assert body["reasoning"]["exclude"] is True
    assert reserved <= 3800
    assert "DO NOT SAVE" not in json.dumps(result)
    assert result["usage"]["total_tokens"] == 111
    await llm.close()


async def test_wrong_model_never_silently_substitutes(settings):
    def response(request):
        return httpx.Response(
            200,
            json={
                "model": "openai/other-model",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 4},
            },
        )

    llm = LLM(settings, SimpleNamespace(config=settings.snapshot()), transport=httpx.MockTransport(response))
    with pytest.raises(ToolFailure, match="unauthorized_model"):
        await llm.request({"model": settings.model}, 1)
    await llm.close()


@pytest.mark.parametrize(
    "model,reported,effort",
    [
        ("openai/gpt-6-astra", "openai/gpt-6-astra-20260903", "high"),
        ("openai/gpt-5.6-luna", "openai/gpt-5.6-luna-20260709", "low"),
    ],
)
async def test_selected_model_and_effort_are_sent_and_reported(settings, model, reported, effort):
    from research_agent.config import Settings

    settings.model = model
    config = Settings(model=model).snapshot()
    calls = []

    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": reported,
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 20},
            },
        )

    llm = LLM(settings, SimpleNamespace(config=config), transport=httpx.MockTransport(response))
    try:
        body, reservation = llm.build("writer", {"query": "Explain a compiler"}, UnderstandingDecision)
        result = await llm.request(body, 1)
        assert calls[0]["model"] == model
        assert calls[0]["reasoning"] == {"effort": effort, "exclude": True}
        assert result["model"] == reported and result["reasoning_effort"] == effort
        assert "temperature" not in body and "tools" not in body
        assert reservation > body["max_tokens"]
        if model == "openai/gpt-6-astra":
            assert set(config["reasoning_efforts"].values()) == {"high"}
            assert body["max_tokens"] == 24000
    finally:
        await llm.close()


async def test_astra_cannot_silently_fall_back_to_luna(settings):
    settings.model = "openai/gpt-6-astra"

    def response(request):
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-5.6-luna",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 20},
            },
        )

    llm = LLM(settings, SimpleNamespace(config=settings.snapshot()), transport=httpx.MockTransport(response))
    try:
        with pytest.raises(ToolFailure, match="unauthorized_model"):
            await llm.request({"model": settings.model}, 1)
    finally:
        await llm.close()


async def test_large_constraints_fail_closed_without_silent_truncation(settings):
    llm = LLM(settings, SimpleNamespace(config=settings.snapshot()))
    with pytest.raises(Stopped, match="input_budget_exceeded"):
        llm.build("understand", {"query": "中文研究" * 10000}, UnderstandingDecision)
    await llm.close()


def test_every_object_in_provider_schema_is_strict():
    schema = strict_schema(UnderstandingDecision.model_json_schema())

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


async def test_truncated_audit_is_returned_for_packet_split_without_identical_retry(settings):
    from research_agent.contracts import BatchParagraphAudit

    calls = []

    async def run(*args, **kwargs):
        calls.append(args[0])
        return {"finish_reason": "length", "content": '{"paragraphs":[', "usage": {"total_tokens": 100}}

    llm = LLM(settings, SimpleNamespace(config=settings.snapshot(), run=run))
    try:
        with pytest.raises(ToolFailure, match="audit_output_truncated"):
            await llm.generate("audit", "report_verifier", {}, BatchParagraphAudit)
        assert calls == ["audit"]
    finally:
        await llm.close()
