"""Tracing for agent runs (design §12).

Every run is one OpenTelemetry trace whose trace ID is the run ID, so spans from separate graph
segments (start, revisions, commit, even after a restart) land in the same trace, and audit
events, which carry the run ID, join to it directly.

- Each automated node is a span `agent.<node>`; model calls are child spans `agent.model` with
  GenAI attributes (model, purpose, prompt version, token use); tool calls are child spans
  `agent.tool.<name>` with their result count.
- Spans carry identifiers, counts, durations and model names only. No record text, claim text or
  quote is ever put on a span, so traces need no redaction.
- Each finished node also becomes a `TimelineStep`, kept on the run for the review panel.

Spans are exported only when an OTLP endpoint is configured (`OTEL_EXPORTER_OTLP_ENDPOINT`, e.g. a
Langfuse or Jaeger collector) and the optional `otel` extra is installed; otherwise they are
created and dropped, and the timeline still works.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags
from pydantic import BaseModel

from carenote.agent.gateway import ModelGateway, ModelRequest, ModelResult

logger = logging.getLogger(__name__)
TRACER_NAME = "carenote.agent"


class TimelineStep(BaseModel):
    node: str
    started_at: datetime
    duration_ms: int
    model: str | None = None
    prompt: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    unsupported_claims: int | None = None


def trace_id_for(run_id: UUID) -> str:
    return run_id.hex


def _run_context(run_id: UUID):
    """A remote parent that pins every span of this run to trace ID = run ID."""
    span_id = (run_id.int & ((1 << 64) - 1)) or 1
    parent = SpanContext(
        trace_id=run_id.int,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(parent))


@dataclass
class _NodeTrace:
    run_id: UUID
    node: str
    started_at: datetime
    prompt: str | None = None
    model: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[tuple[str, int]] = field(default_factory=list)
    unsupported_claims: int | None = None


_current: ContextVar[_NodeTrace | None] = ContextVar("carenote_agent_node", default=None)


class RunTracer:
    def __init__(
        self,
        tracer: trace.Tracer | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tracer = tracer
        self.now = now

    @property
    def tracer(self) -> trace.Tracer:
        # Resolved lazily so a provider configured after import is still used.
        return self._tracer or trace.get_tracer(TRACER_NAME)

    @contextmanager
    def node(self, run_id: UUID, name: str, prompt: str | None = None) -> Iterator[_NodeTrace]:
        step = _NodeTrace(run_id=run_id, node=name, started_at=self.now(), prompt=prompt)
        token = _current.set(step)
        started = time.perf_counter()
        with self.tracer.start_as_current_span(
            f"agent.{name}",
            context=_run_context(run_id),
            attributes={"carenote.run_id": str(run_id), "carenote.node": name},
        ) as span:
            try:
                yield step
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                _current.reset(token)
                for tool, count in step.tool_calls:
                    with self.tracer.start_as_current_span(
                        f"agent.tool.{tool}",
                        attributes={"carenote.tool": tool, "carenote.result_count": count},
                    ):
                        pass
                attributes = {
                    "carenote.tool_calls": len(step.tool_calls),
                    "gen_ai.usage.input_tokens": step.input_tokens,
                    "gen_ai.usage.output_tokens": step.output_tokens,
                }
                if step.unsupported_claims is not None:
                    attributes["carenote.unsupported_claims"] = step.unsupported_claims
                span.set_attributes(attributes)
                step.duration_ms = int((time.perf_counter() - started) * 1000)

    def model_call(self, request: ModelRequest, call: Callable[[], ModelResult]) -> ModelResult:
        step = _current.get()
        with self.tracer.start_as_current_span(
            "agent.model",
            attributes={"carenote.purpose": request.purpose, "gen_ai.operation.name": "chat"},
        ) as span:
            result = call()
            span.set_attributes(
                {
                    "gen_ai.response.model": result.model,
                    "gen_ai.usage.input_tokens": result.usage.input_tokens,
                    "gen_ai.usage.output_tokens": result.usage.output_tokens,
                    "carenote.cached": result.cached,
                    **({"carenote.prompt": step.prompt} if step and step.prompt else {}),
                    **(
                        {"carenote.fallback_from": result.fallback_from}
                        if result.fallback_from
                        else {}
                    ),
                }
            )
        if step is not None:
            step.model = result.model
            step.input_tokens += result.usage.input_tokens
            step.output_tokens += result.usage.output_tokens
        return result


def timeline_step(step: _NodeTrace) -> TimelineStep:
    return TimelineStep(
        node=step.node,
        started_at=step.started_at,
        duration_ms=step.duration_ms,
        model=step.model,
        prompt=step.prompt,
        input_tokens=step.input_tokens,
        output_tokens=step.output_tokens,
        tool_calls=len(step.tool_calls),
        unsupported_claims=step.unsupported_claims,
    )


class TracingGateway:
    """Wraps a gateway so every model call becomes an `agent.model` span in the current node."""

    def __init__(self, inner: ModelGateway, tracer: RunTracer) -> None:
        self.inner = inner
        self.tracer = tracer

    def structured(self, request: ModelRequest, schema):
        return self.tracer.model_call(request, lambda: self.inner.structured(request, schema))

    def text(self, request: ModelRequest):
        return self.tracer.model_call(request, lambda: self.inner.text(request))


def configure_tracing(service_name: str = "provenance-care-note") -> bool:
    """Export spans over OTLP when an endpoint is configured; return whether export is on."""
    if not (
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTLP endpoint set but the 'otel' extra is not installed; spans not exported"
        )
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True
