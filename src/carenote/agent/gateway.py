"""Provider-agnostic model gateway (design §14).

Every model call in the agent goes through a `ModelGateway`. A request carries trusted
`instructions` (versioned prompt text written by us) and `inputs` that must be `RedactionResult`
objects or `DataTag` markers, so record content cannot reach a provider without passing
`PHIRedactionGateway` first: plain strings are rejected at runtime. Tags carry only typed values
(IDs, enums, timestamps, numbers), so they frame record text without becoming a way around
redaction.

Adapters:

- `OpenAIGateway` calls the Responses API, with JSON-schema structured output for `structured()`,
  retries on rate limits and server errors, and token usage on every result.
- `AnthropicGateway` calls the Messages API; `structured()` forces a single tool call whose input
  schema is the output model, and the same retry and usage rules apply.
- `OpenAICompatibleGateway` calls any Chat Completions service at a configurable base URL:
  hosted ones such as DeepSeek, Qwen or Kimi, or a local Ollama or vLLM server.
- `FallbackGateway` tries a primary gateway and, when its provider fails, a secondary one.
- `RecordedGateway` replays responses from cassette files, so tests, CI and the offline demo need
  no API key. Given a live gateway it records new cassettes instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from carenote.core.config import settings
from carenote.privacy.redaction import RedactionResult, has_markup

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

CASSETTE_DIR = Path(__file__).resolve().parents[3] / "evals" / "cassettes"
# Retried with backoff: timeouts, conflicts, rate limits, server errors and Anthropic's overload.
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

logger = logging.getLogger(__name__)


class ModelNotConfiguredError(RuntimeError):
    pass


class ExternalModelError(RuntimeError):
    pass


class CassetteMissingError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DataTag:
    """Marks where a block of record data starts or ends, e.g. `<entry id=... type=...>`.

    Attribute values must be IDs, enums, timestamps, booleans or integers: never free text, which
    could carry identifiers past the redaction boundary.
    """

    name: str
    attributes: tuple[tuple[str, UUID | Enum | datetime | bool | int], ...] = ()
    closing: bool = False

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError("tag names must be identifiers")
        for key, value in self.attributes:
            if not key.isidentifier() or not isinstance(value, UUID | Enum | datetime | bool | int):
                raise TypeError("tag attributes must be typed values, never free text")

    @classmethod
    def open(cls, name: str, **attributes: UUID | Enum | datetime | bool | int) -> DataTag:
        return cls(name, tuple(attributes.items()))

    @classmethod
    def close(cls, name: str) -> DataTag:
        return cls(name, closing=True)

    @property
    def text(self) -> str:
        if self.closing:
            return f"</{self.name}>"
        rendered = "".join(f' {key}="{_render(value)}"' for key, value in self.attributes)
        return f"<{self.name}{rendered}>"


def _render(value: UUID | Enum | datetime | bool | int) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One model call. `purpose` names the graph node and keys cassettes and traces."""

    purpose: str
    instructions: str
    inputs: Sequence[RedactionResult | DataTag]
    max_output_tokens: int = 4000
    safety_subject: str | None = None

    def __post_init__(self) -> None:
        if not self.purpose or not self.instructions:
            raise ValueError("a model request needs a purpose and instructions")
        for item in self.inputs:
            if not isinstance(item, RedactionResult | DataTag):
                raise TypeError(
                    "model inputs must be RedactionResult or DataTag; redact record text first"
                )
            if isinstance(item, RedactionResult) and has_markup(item.text):
                raise ValueError("model input text must not contain tag-like markup")

    @property
    def input_text(self) -> str:
        return "\n".join(item.text for item in self.inputs)

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
    # When a fallback answered: the model whose provider failed first.
    fallback_from: str | None = None


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


class _HTTPGateway:
    """Shared provider behaviour: key check, retries with exponential backoff, errors, pricing."""

    URL = ""
    KEY_NAME = ""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        client: httpx.Client | None,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None],
        usd_per_million_tokens: tuple[float, float] | None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.Client(timeout=45)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        # (input, output) prices; cost stays unknown rather than guessed when not configured.
        self.usd_per_million_tokens = usd_per_million_tokens
        self.url = self.URL
        self.key_required = True

    def _post(self, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if self.key_required and not self.api_key:
            raise ModelNotConfiguredError(f"{self.KEY_NAME} is not configured")
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(self.url, headers=headers, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ExternalModelError("model provider is unreachable") from exc
            else:
                if response.status_code not in RETRY_STATUS or attempt == self.max_retries:
                    return _json_or_error(response)
            self.sleep(self.backoff_seconds * 2**attempt)
        raise AssertionError("unreachable")

    def _usage(self, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        input_tokens = int(raw.get("input_tokens") or 0)
        output_tokens = int(raw.get("output_tokens") or 0)
        cost = None
        if self.usd_per_million_tokens is not None:
            input_price, output_price = self.usd_per_million_tokens
            cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)


def _json_or_error(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ExternalModelError("external model returned invalid JSON") from exc
    if response.is_error:
        error = payload.get("error") if isinstance(payload, dict) else None
        # Most providers send {"error": {"message": ...}}; some local servers send a plain string.
        if isinstance(error, dict):
            error = error.get("message")
        raise ExternalModelError(str(error or "external model request failed"))
    if not isinstance(payload, dict):
        raise ExternalModelError("external model returned an invalid payload")
    return payload


def _safety_id(request: ModelRequest) -> str | None:
    """A hash of the requesting clinician's ID, for the provider's abuse monitoring."""
    if not request.safety_subject:
        return None
    return hashlib.sha256(request.safety_subject.encode()).hexdigest()


class OpenAIGateway(_HTTPGateway):
    URL = "https://api.openai.com/v1/responses"
    KEY_NAME = "OPENAI_API_KEY"

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
        super().__init__(
            api_key if api_key is not None else settings.openai_api_key,
            model or settings.openai_model,
            client,
            max_retries,
            backoff_seconds,
            sleep,
            usd_per_million_tokens,
        )

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
        return ModelResult(output=output, model=self.model, usage=self._usage(payload.get("usage")))

    def text(self, request: ModelRequest) -> ModelResult[str]:
        payload = self._call(request, None)
        return ModelResult(
            output=self._output_text(payload).strip(),
            model=self.model,
            usage=self._usage(payload.get("usage")),
        )

    def _call(self, request: ModelRequest, text_format: dict[str, Any] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "max_output_tokens": request.max_output_tokens,
            "instructions": request.instructions,
            "input": request.input_text,
        }
        if safety_id := _safety_id(request):
            body["safety_identifier"] = safety_id
        if text_format is not None:
            body["text"] = text_format
        return self._post({"Authorization": f"Bearer {self.api_key}"}, body)

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


class AnthropicGateway(_HTTPGateway):
    """Anthropic Messages API.

    Structured output is a forced call to one tool whose input schema is the output model; the
    tool input is then validated by Pydantic, so a response that does not fit is rejected as with
    any provider.
    """

    URL = "https://api.anthropic.com/v1/messages"
    KEY_NAME = "ANTHROPIC_API_KEY"
    VERSION = "2023-06-01"

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
        super().__init__(
            api_key if api_key is not None else settings.anthropic_api_key,
            model or settings.anthropic_model,
            client,
            max_retries,
            backoff_seconds,
            sleep,
            usd_per_million_tokens,
        )

    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]:
        name = schema.__name__
        tool = {
            "name": name,
            "description": f"Return the {name}.",
            "input_schema": schema.model_json_schema(),
        }
        payload = self._call(
            request, {"tools": [tool], "tool_choice": {"type": "tool", "name": name}}
        )
        calls = [
            block.get("input")
            for block in payload.get("content", [])
            if block.get("type") == "tool_use" and block.get("name") == name
        ]
        if len(calls) != 1:
            raise ExternalModelError(f"model did not return one {name}")
        try:
            output = schema.model_validate(calls[0])
        except ValidationError as exc:
            raise ExternalModelError(f"model output does not match {name}") from exc
        return ModelResult(output=output, model=self.model, usage=self._usage(payload.get("usage")))

    def text(self, request: ModelRequest) -> ModelResult[str]:
        payload = self._call(request, {})
        pieces = [
            str(block["text"])
            for block in payload.get("content", [])
            if block.get("type") == "text" and block.get("text")
        ]
        if not pieces:
            raise ExternalModelError("model returned no text")
        return ModelResult(
            output="\n".join(pieces).strip(),
            model=self.model,
            usage=self._usage(payload.get("usage")),
        )

    def _call(self, request: ModelRequest, extra: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.instructions,
            "messages": [{"role": "user", "content": request.input_text}],
            **extra,
        }
        if safety_id := _safety_id(request):
            body["metadata"] = {"user_id": safety_id}
        headers = {"x-api-key": self.api_key or "", "anthropic-version": self.VERSION}
        return self._post(headers, body)


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
# Reasoning models served through compatible APIs may put their reasoning in the content.
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
STRUCTURED_MODES = ("json_schema", "json_object", "tools")


class OpenAICompatibleGateway(_HTTPGateway):
    """Any service that speaks OpenAI's Chat Completions API, at a configurable base URL.

    One adapter covers hosted services (DeepSeek, Qwen, Kimi, GLM, OpenRouter, Azure and others)
    and local servers (Ollama, vLLM, LM Studio). Services differ in how they return structured
    output, so `structured_mode` picks one:

    - `json_schema`: `response_format` with the strict schema, as OpenAI and recent servers do;
    - `json_object`: JSON mode, with the schema given in the system message;
    - `tools`: a forced function call whose parameters are the schema.

    Whatever comes back is validated by Pydantic. A key is optional for a local server, which
    keeps record text on the machine; any other host receives the redacted request, so it needs
    the same approvals as the other providers.
    """

    KEY_NAME = "CARENOTE_COMPAT_API_KEY"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        structured_mode: str | None = None,
        client: httpx.Client | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        usd_per_million_tokens: tuple[float, float] | None = None,
    ) -> None:
        super().__init__(
            api_key if api_key is not None else settings.compat_api_key,
            model or settings.compat_model or "",
            client,
            max_retries,
            backoff_seconds,
            sleep,
            usd_per_million_tokens,
        )
        base = (base_url or settings.compat_base_url or "").rstrip("/")
        self.structured_mode = structured_mode or settings.compat_structured
        if self.structured_mode not in STRUCTURED_MODES:
            raise ValueError(f"unknown structured mode: {self.structured_mode!r}")
        self.url = f"{base}/chat/completions"
        self.key_required = httpx.URL(base).host not in _LOCAL_HOSTS if base else True
        self._base = base

    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]:
        name = schema.__name__
        system = request.instructions
        extra: dict[str, Any]
        if self.structured_mode == "json_schema":
            extra = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": name,
                        "schema": strict_json_schema(schema),
                        "strict": True,
                    },
                }
            }
        elif self.structured_mode == "json_object":
            schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
            system += (
                f"\n\nRespond with one JSON object, and nothing else, that is a valid {name} "
                f"matching this JSON schema:\n{schema_text}"
            )
            extra = {"response_format": {"type": "json_object"}}
        else:
            extra = {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": f"Return the {name}.",
                            "parameters": schema.model_json_schema(),
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": name}},
            }
        payload = self._call(request, system, extra)
        message = self._message(payload)
        if self.structured_mode == "tools":
            calls = [
                call.get("function", {}).get("arguments")
                for call in message.get("tool_calls") or []
                if call.get("function", {}).get("name") == name
            ]
            if len(calls) != 1:
                raise ExternalModelError(f"model did not return one {name}")
            raw = calls[0] if isinstance(calls[0], str) else json.dumps(calls[0])
        else:
            raw = self._content(message)
        try:
            output = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise ExternalModelError(f"model output does not match {name}") from exc
        return ModelResult(output=output, model=self.model, usage=self._usage_of(payload))

    def text(self, request: ModelRequest) -> ModelResult[str]:
        payload = self._call(request, request.instructions, {})
        return ModelResult(
            output=self._content(self._message(payload)),
            model=self.model,
            usage=self._usage_of(payload),
        )

    def _call(self, request: ModelRequest, system: str, extra: dict[str, Any]) -> dict[str, Any]:
        if not self._base or not self.model:
            raise ModelNotConfiguredError(
                "CARENOTE_COMPAT_BASE_URL and CARENOTE_COMPAT_MODEL are not configured"
            )
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.input_text},
            ],
            **extra,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return self._post(headers, body)

    @staticmethod
    def _message(payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ExternalModelError("model returned no message")
        return choices[0]["message"]

    @staticmethod
    def _content(message: dict[str, Any]) -> str:
        text = _THINKING.sub("", str(message.get("content") or "")).strip()
        fenced = _FENCE.match(text)
        text = fenced.group(1) if fenced else text
        if not text:
            raise ExternalModelError("model returned no text")
        return text

    def _usage_of(self, payload: dict[str, Any]) -> Usage:
        raw = payload.get("usage") or {}
        return self._usage(
            {
                "input_tokens": raw.get("prompt_tokens"),
                "output_tokens": raw.get("completion_tokens"),
            }
        )


class FallbackGateway:
    """Calls `primary`; when its provider fails (`ExternalModelError`), calls `secondary` once.

    A missing key or cassette is a configuration problem, not a provider failure, so it is raised
    rather than hidden by the fallback. The result names the model that answered and, in
    `fallback_from`, the one that failed.
    """

    def __init__(self, primary: ModelGateway, secondary: ModelGateway) -> None:
        self.primary = primary
        self.secondary = secondary

    def structured(self, request: ModelRequest, schema: type[T]) -> ModelResult[T]:
        return self._call(request, lambda gateway: gateway.structured(request, schema))

    def text(self, request: ModelRequest) -> ModelResult[str]:
        return self._call(request, lambda gateway: gateway.text(request))

    def _call(
        self, request: ModelRequest, call: Callable[[ModelGateway], ModelResult[R]]
    ) -> ModelResult[R]:
        try:
            return call(self.primary)
        except ExternalModelError as exc:
            failed = str(getattr(self.primary, "model", type(self.primary).__name__))
            # Provider error messages never contain request text, so they are safe to log.
            logger.warning(
                "%s failed for %s (%s); trying the fallback", failed, request.purpose, exc
            )
        result = call(self.secondary)
        return ModelResult(
            output=result.output,
            model=result.model,
            usage=result.usage,
            cached=result.cached,
            fallback_from=failed,
        )


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
            newline="\n",
        )
        return result


PROVIDERS: dict[str, Callable[[], ModelGateway]] = {
    "openai": OpenAIGateway,
    "anthropic": AnthropicGateway,
    "compatible": OpenAICompatibleGateway,
}


def live_gateway(provider: str | None = None, fallback: str | None = None) -> ModelGateway:
    """The live provider, wrapped with a fallback provider when one is configured."""
    provider = provider or settings.agent_provider
    fallback = settings.agent_fallback if fallback is None else fallback
    for name in (provider, fallback):
        if name and name not in PROVIDERS:
            raise ValueError(f"unknown model provider: {name!r}")
    primary = PROVIDERS[provider]()
    if not fallback or fallback == provider:
        return primary
    return FallbackGateway(primary, PROVIDERS[fallback]())


def gateway_from_settings(mode: str | None = None) -> ModelGateway:
    """Build the gateway named by CARENOTE_AGENT_GATEWAY.

    `recorded` replays cassettes; `openai`, `anthropic` or `compatible` call that provider; `live` calls
    CARENOTE_AGENT_PROVIDER; `record` makes live calls that also write cassettes. Live calls fall
    back to CARENOTE_AGENT_FALLBACK when it names another provider.
    """
    mode = mode or settings.agent_gateway
    if mode == "recorded":
        return RecordedGateway()
    if mode in PROVIDERS:
        return live_gateway(mode)
    if mode == "live":
        return live_gateway()
    if mode == "record":
        return RecordedGateway(record_with=live_gateway())
    raise ValueError(f"unknown agent gateway mode: {mode!r}")
