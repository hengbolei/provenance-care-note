"""The second provider (Anthropic Messages API), the fallback gateway and provider settings."""

import json
import logging
from uuid import uuid4

import httpx
import pytest

from carenote.agent import gateway as gateway_module
from carenote.agent.gateway import (
    AnthropicGateway,
    CassetteMissingError,
    ExternalModelError,
    FallbackGateway,
    ModelNotConfiguredError,
    ModelRequest,
    ModelResult,
    OpenAIGateway,
    RecordedGateway,
    Usage,
    gateway_from_settings,
    live_gateway,
)
from carenote.agent.state import DraftReview
from carenote.privacy.redaction import PHIRedactionGateway

REDACTOR = PHIRedactionGateway()


def _request(text="Maya Chen started metformin 500 mg."):
    return ModelRequest(
        purpose="agent_draft",
        instructions="Draft a review.",
        inputs=[REDACTOR.redact(text, ["Maya Chen"])],
        safety_subject="clinician-1",
    )


def _draft():
    return {
        "claims": [
            {
                "text": "Metformin was started.",
                "category": "medication",
                "risk_level": "moderate",
                "citations": [
                    {"entry_id": str(uuid4()), "start": 0, "end": 17, "quote": "started metformin"}
                ],
            }
        ],
        "open_questions": [],
        "candidate_highlights": [],
    }


def _responses(*replies):
    sent = []
    queue = list(replies)

    def handler(request):
        sent.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def _tool_reply(name, data, input_tokens=200, output_tokens=60):
    return 200, {
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": data}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _anthropic(client, **kwargs):
    return AnthropicGateway(api_key="test-key", model="claude-test", client=client, **kwargs)


# Anthropic adapter


def test_structured_output_is_a_forced_tool_call_validated_by_the_schema():
    client, sent = _responses(_tool_reply("DraftReview", _draft()))

    result = _anthropic(client).structured(_request(), DraftReview)

    assert result.output.claims[0].text == "Metformin was started."
    assert (result.model, result.usage.input_tokens, result.usage.output_tokens) == (
        "claude-test",
        200,
        60,
    )
    [call] = sent
    body = call["body"]
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["anthropic-version"] == AnthropicGateway.VERSION
    assert body["system"] == "Draft a review."
    assert body["tool_choice"] == {"type": "tool", "name": "DraftReview"}
    assert body["tools"][0]["input_schema"]["title"] == "DraftReview"
    assert body["metadata"]["user_id"] != "clinician-1"


def test_only_redacted_text_reaches_anthropic():
    client, sent = _responses(_tool_reply("DraftReview", _draft()))

    _anthropic(client).structured(_request(), DraftReview)

    content = sent[0]["body"]["messages"][0]["content"]
    assert "Maya Chen" not in content
    assert "[REDACTED_NAME]" in content


def test_a_tool_input_that_breaks_the_schema_is_an_error():
    client, _ = _responses(_tool_reply("DraftReview", {"claims": "not a list"}))

    with pytest.raises(ExternalModelError, match="does not match DraftReview"):
        _anthropic(client).structured(_request(), DraftReview)


def test_a_reply_without_the_tool_call_is_an_error():
    client, _ = _responses((200, {"content": [{"type": "text", "text": "Sure!"}], "usage": {}}))

    with pytest.raises(ExternalModelError, match="did not return one DraftReview"):
        _anthropic(client).structured(_request(), DraftReview)


def test_text_calls_join_the_text_blocks():
    reply = {"content": [{"type": "text", "text": " Summary. "}], "usage": {"input_tokens": 5}}
    client, sent = _responses((200, reply))

    result = _anthropic(client).text(_request())

    assert result.output == "Summary."
    assert "tools" not in sent[0]["body"]


def test_overload_is_retried_and_errors_carry_the_provider_message():
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    client, sent = _responses((529, error), (529, error), (529, error))
    waits = []

    with pytest.raises(ExternalModelError, match="Overloaded"):
        _anthropic(client, sleep=waits.append).text(_request())
    assert len(sent) == 3
    assert waits == [1.0, 2.0]


def test_a_missing_anthropic_key_is_reported_before_any_call():
    client, sent = _responses()

    with pytest.raises(ModelNotConfiguredError, match="ANTHROPIC_API_KEY"):
        AnthropicGateway(api_key="", client=client).text(_request())
    assert sent == []


# Fallback


class _Fake:
    def __init__(self, model, error=None):
        self.model = model
        self.error = error
        self.calls = 0

    def structured(self, request, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return ModelResult(
            output=schema.model_validate(_draft()), model=self.model, usage=Usage(10, 5)
        )

    def text(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return ModelResult(output="ok", model=self.model)


def test_the_primary_answers_when_it_can():
    primary, secondary = _Fake("primary"), _Fake("secondary")

    result = FallbackGateway(primary, secondary).structured(_request(), DraftReview)

    assert (result.model, result.fallback_from) == ("primary", None)
    assert secondary.calls == 0


def test_a_provider_failure_falls_back_and_says_so(caplog):
    primary = _Fake("primary", ExternalModelError("model provider is unreachable"))
    secondary = _Fake("secondary")

    with caplog.at_level(logging.WARNING, logger="carenote.agent.gateway"):
        result = FallbackGateway(primary, secondary).structured(_request(), DraftReview)

    assert (result.model, result.fallback_from) == ("secondary", "primary")
    assert result.usage == Usage(10, 5)
    assert "primary failed for agent_draft" in caplog.text
    assert "metformin" not in caplog.text.lower()


@pytest.mark.parametrize(
    "error", [ModelNotConfiguredError("no key"), CassetteMissingError("no cassette")]
)
def test_configuration_errors_are_not_hidden_by_the_fallback(error):
    secondary = _Fake("secondary")

    with pytest.raises(type(error)):
        FallbackGateway(_Fake("primary", error), secondary).text(_request())
    assert secondary.calls == 0


def test_when_both_providers_fail_the_secondary_error_is_raised():
    primary = _Fake("primary", ExternalModelError("first"))
    secondary = _Fake("secondary", ExternalModelError("second"))

    with pytest.raises(ExternalModelError, match="second"):
        FallbackGateway(primary, secondary).text(_request())


def test_a_fallback_answer_records_and_replays_like_any_other(tmp_path):
    primary = _Fake("primary", ExternalModelError("down"))
    live = FallbackGateway(primary, _Fake("secondary"))

    RecordedGateway(tmp_path, record_with=live).structured(_request(), DraftReview)
    replayed = RecordedGateway(tmp_path).structured(_request(), DraftReview)

    assert replayed.model == "secondary"


# Settings


def test_live_gateways_follow_provider_and_fallback_settings():
    assert isinstance(live_gateway("anthropic", ""), AnthropicGateway)
    assert isinstance(live_gateway("openai", "openai"), OpenAIGateway)
    wrapped = live_gateway("openai", "anthropic")
    assert isinstance(wrapped, FallbackGateway)
    assert isinstance(wrapped.primary, OpenAIGateway)
    assert isinstance(wrapped.secondary, AnthropicGateway)
    with pytest.raises(ValueError, match="unknown model provider"):
        live_gateway("mystery", "")


def test_gateway_modes(monkeypatch):
    monkeypatch.setattr(
        gateway_module,
        "settings",
        gateway_module.settings.__class__(agent_provider="anthropic", agent_fallback="openai"),
    )

    assert isinstance(gateway_from_settings("recorded"), RecordedGateway)
    assert isinstance(gateway_from_settings("anthropic"), FallbackGateway)
    live = gateway_from_settings("live")
    assert isinstance(live, FallbackGateway)
    assert isinstance(live.primary, AnthropicGateway)
    recording = gateway_from_settings("record")
    assert isinstance(recording.record_with, FallbackGateway)
    with pytest.raises(ValueError, match="unknown agent gateway mode"):
        gateway_from_settings("bogus")
