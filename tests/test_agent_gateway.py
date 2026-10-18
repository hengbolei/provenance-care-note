import json
from uuid import uuid4

import httpx
import pytest

from carenote.agent.gateway import (
    CassetteMissingError,
    DataTag,
    ExternalModelError,
    ModelNotConfiguredError,
    ModelRequest,
    ModelResult,
    OpenAIGateway,
    RecordedGateway,
    Usage,
    strict_json_schema,
)
from carenote.agent.state import DraftReview
from carenote.domain.models import Role
from carenote.privacy.redaction import PHIRedactionGateway
from carenote.services.ai import OpenAIClinicalGateway

REDACTOR = PHIRedactionGateway()


def _request(text="Maya Chen started metformin 500 mg.", purpose="draft"):
    return ModelRequest(
        purpose=purpose,
        instructions="Draft a review.",
        inputs=[REDACTOR.redact(text, ["Maya Chen"])],
        safety_subject="clinician-1",
    )


def _draft_json():
    entry_id = str(uuid4())
    return {
        "claims": [
            {
                "text": "Metformin was started.",
                "category": "medication",
                "risk_level": "moderate",
                "citations": [
                    {"entry_id": entry_id, "start": 16, "end": 33, "quote": "started metformin"}
                ],
            }
        ],
        "open_questions": [],
        "candidate_highlights": [],
    }


def _responses(*replies):
    """A transport that returns the given (status, json) replies in order and records requests."""
    sent = []
    queue = list(replies)

    def handler(request):
        sent.append(json.loads(request.content))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def _ok(output, input_tokens=120, output_tokens=40):
    text = output if isinstance(output, str) else json.dumps(output)
    return 200, {
        "output_text": text,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _gateway(client, **kwargs):
    return OpenAIGateway(api_key="test-key", model="test-model", client=client, **kwargs)


# Redaction boundary


def test_requests_reject_unredacted_strings():
    with pytest.raises(TypeError):
        ModelRequest(purpose="draft", instructions="Draft.", inputs=["Maya Chen is dizzy."])


def test_only_redacted_text_reaches_the_provider():
    client, sent = _responses(_ok(_draft_json()))

    _gateway(client).structured(_request(), DraftReview)

    assert "Maya Chen" not in json.dumps(sent[0])
    assert sent[0]["input"] == "[REDACTED_NAME] started metformin 500 mg."
    assert sent[0]["store"] is False
    assert sent[0]["safety_identifier"] != "clinician-1"


# OpenAI adapter


def test_structured_output_is_requested_with_a_strict_schema_and_validated():
    client, sent = _responses(_ok(_draft_json()))

    result = _gateway(client).structured(_request(), DraftReview)

    text_format = sent[0]["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["name"] == "DraftReview"
    assert text_format["strict"] is True
    assert isinstance(result.output, DraftReview)
    assert result.usage == Usage(input_tokens=120, output_tokens=40, cost_usd=None)


def test_output_that_breaks_the_schema_is_an_error():
    bad = _draft_json() | {"confidence": 0.9}
    client, _ = _responses(_ok(bad))

    with pytest.raises(ExternalModelError):
        _gateway(client).structured(_request(), DraftReview)


def test_cost_is_computed_only_when_prices_are_configured():
    client, _ = _responses(_ok("Summary.", input_tokens=1_000_000, output_tokens=500_000))

    result = _gateway(client, usd_per_million_tokens=(0.25, 2.0)).text(_request())

    assert result.output == "Summary."
    assert result.usage.cost_usd == pytest.approx(1.25)


def test_rate_limits_and_server_errors_are_retried_with_backoff():
    error = {"error": {"message": "slow down"}}
    client, sent = _responses((429, error), (503, error), _ok("Summary."))
    waits = []

    result = _gateway(client, sleep=waits.append).text(_request())

    assert result.output == "Summary."
    assert len(sent) == 3
    assert waits == [1.0, 2.0]


def test_retries_stop_at_the_limit():
    error = {"error": {"message": "overloaded"}}
    client, sent = _responses((503, error), (503, error), (503, error))

    with pytest.raises(ExternalModelError, match="overloaded"):
        _gateway(client, sleep=lambda _: None).text(_request())
    assert len(sent) == 3


def test_client_errors_are_not_retried():
    client, sent = _responses((400, {"error": {"message": "bad request"}}))

    with pytest.raises(ExternalModelError, match="bad request"):
        _gateway(client, sleep=lambda _: None).text(_request())
    assert len(sent) == 1


def test_missing_api_key_is_reported_before_any_call():
    client, sent = _responses()

    with pytest.raises(ModelNotConfiguredError):
        OpenAIGateway(api_key="", client=client).text(_request())
    assert sent == []


def test_strict_schema_requires_every_property_and_drops_defaults():
    schema = strict_json_schema(DraftReview)

    assert schema["required"] == ["claims", "open_questions", "candidate_highlights"]
    assert "default" not in json.dumps(schema)
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])


# Recorded adapter


class _CountingGateway:
    def __init__(self):
        self.calls = 0

    def structured(self, request, schema):
        self.calls += 1
        return ModelResult(
            output=schema.model_validate(_draft_json()),
            model="live-model",
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.001),
        )

    def text(self, request):
        self.calls += 1
        return ModelResult(output="Live summary.", model="live-model", usage=Usage(1, 1))


def test_recorded_gateway_records_once_then_replays_offline(tmp_path):
    live = _CountingGateway()
    recorded = RecordedGateway(tmp_path, record_with=live).structured(_request(), DraftReview)
    replayed = RecordedGateway(tmp_path).structured(_request(), DraftReview)

    assert live.calls == 1
    assert replayed.output == recorded.output
    assert replayed.cached
    assert replayed.usage == Usage(input_tokens=10, output_tokens=5, cost_usd=0.001)
    assert replayed.model == "live-model"


def test_cassettes_never_store_request_text(tmp_path):
    RecordedGateway(tmp_path, record_with=_CountingGateway()).text(_request())

    [cassette] = tmp_path.iterdir()
    stored = cassette.read_text(encoding="utf-8")
    assert "metformin" not in stored
    assert "Draft a review" not in stored
    assert cassette.name.startswith("draft-")


def test_a_changed_request_misses_its_cassette(tmp_path):
    RecordedGateway(tmp_path, record_with=_CountingGateway()).text(_request())

    with pytest.raises(CassetteMissingError):
        RecordedGateway(tmp_path).text(_request("Maya Chen started lisinopril."))


# Existing ingest summary now runs on the gateway


def test_clinical_gateway_summarize_redacts_and_uses_the_model_gateway():
    client, sent = _responses(_ok("Dizziness reported; review required."))
    gateway = OpenAIClinicalGateway(api_key="test-key", model="test-model", client=client)

    summary, redaction = gateway.summarize(
        "Maya Chen called 91234567 about dizziness.", ["Maya Chen"], "clinician-1"
    )

    assert summary == "Dizziness reported; review required."
    assert redaction.counts["name"] == 1
    assert sent[0]["input"] == "[REDACTED_NAME] called [REDACTED_PHONE] about dizziness."
    assert sent[0]["max_output_tokens"] == 800
    assert "text" not in sent[0]


def test_data_tags_frame_redacted_text_with_typed_values_only():
    entry_id = uuid4()
    request = ModelRequest(
        purpose="draft",
        instructions="Draft.",
        inputs=[
            DataTag.open("entry", id=entry_id, author=Role.PATIENT, internal=False),
            REDACTOR.redact("Maya Chen feels dizzy.", ["Maya Chen"]),
            DataTag.close("entry"),
        ],
    )

    assert request.input_text == (
        f'<entry id="{entry_id}" author="patient" internal="false">\n'
        "[REDACTED_NAME] feels dizzy.\n</entry>"
    )
    with pytest.raises(TypeError):
        DataTag.open("entry", title="Maya Chen")
    with pytest.raises(ValueError):
        DataTag.open("entry><script")
