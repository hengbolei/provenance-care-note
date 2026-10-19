"""The OpenAI-compatible Chat Completions adapter: hosted services and local servers."""

import json
from uuid import uuid4

import httpx
import pytest

from carenote.agent.gateway import (
    ExternalModelError,
    FallbackGateway,
    ModelNotConfiguredError,
    ModelRequest,
    OpenAICompatibleGateway,
    live_gateway,
)
from carenote.agent.state import DraftReview
from carenote.privacy.redaction import PHIRedactionGateway

REDACTOR = PHIRedactionGateway()


def _request():
    return ModelRequest(
        purpose="agent_draft",
        instructions="Draft a review.",
        inputs=[REDACTOR.redact("Maya Chen started metformin 500 mg.", ["Maya Chen"])],
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
        sent.append(
            {
                "url": str(request.url),
                "headers": request.headers,
                "body": json.loads(request.content),
            }
        )
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def _reply(content=None, tool_calls=None, prompt_tokens=150, completion_tokens=50):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return 200, {
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _gateway(client, mode="json_schema", base_url="https://api.example.com/v1", **kwargs):
    kwargs.setdefault("api_key", "test-key")
    return OpenAICompatibleGateway(
        base_url=base_url, model="example-model", structured_mode=mode, client=client, **kwargs
    )


def test_json_schema_mode_sends_the_strict_schema_and_validates_the_reply():
    client, sent = _responses(_reply(json.dumps(_draft())))

    result = _gateway(client).structured(_request(), DraftReview)

    assert result.output.claims[0].text == "Metformin was started."
    assert (result.model, result.usage.input_tokens, result.usage.output_tokens) == (
        "example-model",
        150,
        50,
    )
    [call] = sent
    assert call["url"] == "https://api.example.com/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer test-key"
    body = call["body"]
    assert body["messages"][0] == {"role": "system", "content": "Draft a review."}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "Maya Chen" not in body["messages"][1]["content"]


def test_json_object_mode_puts_the_schema_in_the_system_message():
    reply = "<think>Let me look at the record.</think>\n```json\n" + json.dumps(_draft()) + "\n```"
    client, sent = _responses(_reply(reply))

    result = _gateway(client, mode="json_object").structured(_request(), DraftReview)

    assert len(result.output.claims) == 1
    body = sent[0]["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert "valid DraftReview" in body["messages"][0]["content"]
    assert '"claims"' in body["messages"][0]["content"]


def test_tools_mode_forces_one_function_call():
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "DraftReview", "arguments": json.dumps(_draft())},
    }
    client, sent = _responses(_reply(None, tool_calls=[call]))

    result = _gateway(client, mode="tools").structured(_request(), DraftReview)

    assert result.output.claims[0].category == "medication"
    body = sent[0]["body"]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "DraftReview"}}
    assert body["tools"][0]["function"]["parameters"]["title"] == "DraftReview"


def test_a_reply_that_breaks_the_schema_or_skips_the_call_is_an_error():
    client, _ = _responses(_reply('{"claims": "none"}'), _reply("No tool call here."))

    with pytest.raises(ExternalModelError, match="does not match"):
        _gateway(client).structured(_request(), DraftReview)
    with pytest.raises(ExternalModelError, match="did not return one"):
        _gateway(client, mode="tools").structured(_request(), DraftReview)


def test_text_calls_return_the_content_without_reasoning():
    client, _ = _responses(_reply("<think>hmm</think> Summary. "))

    assert _gateway(client).text(_request()).output == "Summary."


def test_a_local_server_needs_no_key_and_gets_no_authorization_header():
    client, sent = _responses(_reply("Summary."))

    gateway = _gateway(client, base_url="http://localhost:11434/v1/", api_key="")
    gateway.text(_request())

    assert sent[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in sent[0]["headers"]


def test_a_hosted_service_needs_a_key_and_settings_before_any_call():
    client, sent = _responses()

    with pytest.raises(ModelNotConfiguredError, match="CARENOTE_COMPAT_API_KEY"):
        _gateway(client, api_key="").text(_request())
    with pytest.raises(ModelNotConfiguredError, match="CARENOTE_COMPAT_BASE_URL"):
        OpenAICompatibleGateway(base_url="", model="", api_key="k", client=client).text(_request())
    assert sent == []


def test_string_errors_from_local_servers_are_reported_and_server_errors_retried():
    client, sent = _responses(
        (503, {"error": {"message": "busy"}}),
        (404, {"error": "model 'example-model' not found"}),
    )

    with pytest.raises(ExternalModelError, match="not found"):
        _gateway(client, sleep=lambda _: None).text(_request())
    assert len(sent) == 2


def test_an_unknown_structured_mode_is_rejected():
    with pytest.raises(ValueError, match="structured mode"):
        OpenAICompatibleGateway(base_url="http://localhost:1/v1", model="m", structured_mode="xml")


def test_the_compatible_provider_can_be_primary_or_fallback():
    assert isinstance(live_gateway("compatible", ""), OpenAICompatibleGateway)
    wrapped = live_gateway("openai", "compatible")
    assert isinstance(wrapped, FallbackGateway)
    assert isinstance(wrapped.secondary, OpenAICompatibleGateway)
