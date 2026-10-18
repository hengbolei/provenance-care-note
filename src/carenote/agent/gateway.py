"""Provider-agnostic model gateway (design §14).

Every model call in the agent goes through a `ModelGateway`. A request carries trusted
`instructions` (versioned prompt text written by us) and `inputs` that must be `RedactionResult`
objects, so record content cannot reach a provider without passing `PHIRedactionGateway` first:
plain strings are rejected at runtime.

Adapters:

- `OpenAIGateway` calls the Responses API, with JSON-schema structured output for `structured()`,
  retries on rate limits and server errors, and token usage on every result.
- `RecordedGateway` replays responses from cassette files, so tests, CI and the offline demo need
  no API key. Given a live gateway it records new cassettes instead.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from carenote.core.config import settings
from carenote.privacy.redaction import RedactionResult

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

CASSETTE_DIR = Path(__file__).resolve().parents[3] / "evals" / "cassettes"


class ModelNotConfiguredError(RuntimeError):
    pass


class ExternalModelError(RuntimeError):
    pass


class CassetteMissingError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One model call. `purpose` names the graph node and keys cassettes and traces."""

    purpose: str
    instructions: str
    inputs: Sequence[RedactionResult]
    max_output_tokens: int = 4000
    safety_subject: str | None = None

    def __post_init__(self) -> None:
        if not self.purpose or not self.instructions:
            raise ValueError("a model request needs a purpose and instructions")
        for item in self.inputs:
            if not isinstance(item, RedactionResult):
                raise TypeError("model inputs must be RedactionResult; redact record text first")

    @property
    def input_text(self) -> str:
        return "\n\n".join(item.text for item in self.inputs)

    def fingerprint(self, output_schema: str) -> str:
        """Stable key for this exact request; only redacted text ever contributes to it."""
        material = json.dumps(
            [self.purpose, output_schema, self.instructions, self.input_text], ensure_ascii=False
        )
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ModelResult(Generic[R]):
    output: R
    model: str
    usage: Usage = field(default_factory=Usage)
    cached: bool = False


class ModelGateway(Protocol):
    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]: ...

    def text(self, request: ModelRequest) -> ModelResult[str]: ...


def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's schema adjusted for strict structured output.

    Strict mode needs every property listed as required and no defaults; optional values are
    still validated by the Pydantic model when the response comes back.
    """

    def adjust(node: Any) -> Any:
        if isinstance(node, dict):
            node = {key: adjust(value) for key, value in node.items() if key != "default"}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node.setdefault("additionalProperties", False)
            return node
        if isinstance(node, list):
            return [adjust(item) for item in node]
        return node

    return adjust(schema.model_json_schema())


class OpenAIGateway:
    URL = "https://api.openai.com/v1/responses"
    RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        usd_per_million_tokens: tuple[float, float] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = model or settings.openai_model
        self.client = client or httpx.Client(timeout=45)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        # (input, output) prices; cost stays unknown rather than guessed when not configured.
        self.usd_per_million_tokens = usd_per_million_tokens

    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]:
        payload = self._call(
            request,
            {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": strict_json_schema(schema),
                    "strict": True,
                }
            },
        )
        raw = self._output_text(payload)
        try:
            output = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise ExternalModelError(f"model output does not match {schema.__name__}") from exc
        return ModelResult(output=output, model=self.model, usage=self._usage(payload))

    def text(self, request: ModelRequest) -> ModelResult[str]:
        payload = self._call(request, None)
        return ModelResult(
            output=self._output_text(payload).strip(), model=self.model, usage=self._usage(payload)
        )

    def _call(self, request: ModelRequest, text_format: dict[str, Any] | None) -> dict[str, Any]:
        if not self.api_key:
            raise ModelNotConfiguredError("OPENAI_API_KEY is not configured")
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "max_output_tokens": request.max_output_tokens,
            "instructions": request.instructions,
            "input": request.input_text,
        }
        if request.safety_subject:
            body["safety_identifier"] = hashlib.sha256(request.safety_subject.encode()).hexdigest()
        if text_format is not None:
            body["text"] = text_format
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, json=body
                )
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ExternalModelError("model provider is unreachable") from exc
            else:
                if response.status_code not in self.RETRY_STATUS or attempt == self.max_retries:
                    return self._json_or_error(response)
            self.sleep(self.backoff_seconds * 2**attempt)
        raise AssertionError("unreachable")

    def _usage(self, payload: dict[str, Any]) -> Usage:
        raw = payload.get("usage") or {}
        input_tokens = int(raw.get("input_tokens") or 0)
        output_tokens = int(raw.get("output_tokens") or 0)
        cost = None
        if self.usd_per_million_tokens is not None:
            input_price, output_price = self.usd_per_million_tokens
            cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)

    @staticmethod
    def _json_or_error(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalModelError("external model returned invalid JSON") from exc
        if response.is_error:
            error = payload.get("error") if isinstance(payload, dict) else None
            message = (error or {}).get("message", "external model request failed")
            raise ExternalModelError(str(message))
        if not isinstance(payload, dict):
            raise ExternalModelError("external model returned an invalid payload")
        return payload

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        if payload.get("output_text"):
            return str(payload["output_text"])
        pieces = [
            str(content["text"])
            for item in payload.get("output", [])
            for content in item.get("content", [])
            if content.get("type") == "output_text" and content.get("text")
        ]
        if not pieces:
            raise ExternalModelError("model returned no text")
        return "\n".join(pieces)


class RecordedGateway:
    """Replays cassettes keyed by request fingerprint; records them when given a live gateway.

    A cassette stores the purpose, schema name, model, usage and output, never the request text.
    """

    def __init__(self, directory: Path = CASSETTE_DIR, record_with: ModelGateway | None = None):
        self.directory = directory
        self.record_with = record_with

    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]:
        def live() -> ModelResult[Any]:
            return self.record_with.structured(request, schema)

        result = self._replay_or_record(request, schema.__name__, live)
        output = result.output
        if not isinstance(output, schema):
            output = schema.model_validate(output)
        return ModelResult(output=output, model=result.model, usage=result.usage, cached=True)

    def text(self, request: ModelRequest) -> ModelResult[str]:
        def live() -> ModelResult[Any]:
            return self.record_with.text(request)

        result = self._replay_or_record(request, "text", live)
        return ModelResult(
            output=str(result.output), model=result.model, usage=result.usage, cached=True
        )

    def path_for(self, request: ModelRequest, schema_name: str) -> Path:
        return self.directory / f"{request.purpose}-{request.fingerprint(schema_name)[:16]}.json"

    def _replay_or_record(
        self,
        request: ModelRequest,
        schema_name: str,
        live: Callable[[], ModelResult[Any]],
    ) -> ModelResult[Any]:
        path = self.path_for(request, schema_name)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("fingerprint") != request.fingerprint(schema_name):
                raise CassetteMissingError(f"cassette {path.name} belongs to another request")
            return ModelResult(
                output=data["output"], model=data["model"], usage=Usage(**data["usage"])
            )
        if self.record_with is None:
            raise CassetteMissingError(
                f"no cassette for {request.purpose} ({path.name}); record it with a live gateway"
            )
        result = live()
        output = result.output
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fingerprint": request.fingerprint(schema_name),
                    "purpose": request.purpose,
                    "schema": schema_name,
                    "model": result.model,
                    "usage": {
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cost_usd": result.usage.cost_usd,
                    },
                    "output": output.model_dump(mode="json")
                    if isinstance(output, BaseModel)
                    else output,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return result
