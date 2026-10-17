import hashlib
from typing import Any

import httpx

from carenote.core.config import settings
from carenote.domain.models import TranscriptionResponse, TranscriptSegment
from carenote.privacy.redaction import PHIRedactionGateway, RedactionResult


class ModelNotConfiguredError(RuntimeError):
    pass


class ExternalModelError(RuntimeError):
    pass


class OpenAIClinicalGateway:
    """Small, testable PHI-minimizing boundary around OpenAI APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = model or settings.openai_model
        self.client = client or httpx.Client(timeout=45)
        self.redactor = PHIRedactionGateway()

    def summarize(
        self, raw_text: str, known_names: list[str], safety_subject: str
    ) -> tuple[str, RedactionResult]:
        self._require_key()
        redacted = self.redactor.redact(raw_text, known_names)
        response = self.client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "store": False,
                "max_output_tokens": 800,
                "safety_identifier": hashlib.sha256(safety_subject.encode()).hexdigest(),
                "instructions": (
                    "Create a concise clinical timeline summary from the supplied redacted text. "
                    "Do not infer diagnoses, medication doses, allergies, dates, or certainty that "
                    "are not explicit. Preserve uncertainty and label conflicts for review."
                ),
                "input": redacted.text,
            },
        )
        payload = self._json_or_error(response)
        output_text = payload.get("output_text") or self._extract_output_text(payload)
        if not output_text:
            raise ExternalModelError("model returned no text")
        return str(output_text).strip(), redacted

    def transcribe(self, audio: bytes, content_type: str) -> TranscriptionResponse:
        self._require_key()
        extension = {
            "audio/webm": "webm",
            "audio/wav": "wav",
            "audio/mpeg": "mp3",
            "audio/mp4": "mp4",
            "audio/ogg": "ogg",
        }.get(content_type.split(";", 1)[0].lower(), "webm")
        response = self.client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"model": "gpt-4o-transcribe", "response_format": "json"},
            files={"file": (f"recording.{extension}", audio, content_type)},
        )
        payload = self._json_or_error(response)
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ExternalModelError("transcription returned no text")
        raw_segments = payload.get("segments") or []
        segments = [self._segment(item) for item in raw_segments if isinstance(item, dict)]
        if not segments:
            segments = [TranscriptSegment(text=text)]
        return TranscriptionResponse(
            text=text,
            segments=segments,
            model="gpt-4o-transcribe",
            diarization_available=all(item.speaker != "unknown" for item in segments),
            confidence_available=all(item.confidence is not None for item in segments),
        )

    def _require_key(self) -> None:
        if not self.api_key:
            raise ModelNotConfiguredError("OPENAI_API_KEY is not configured")

    @staticmethod
    def _json_or_error(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalModelError("external model returned invalid JSON") from exc
        if response.is_error:
            message = payload.get("error", {}).get("message", "external model request failed")
            raise ExternalModelError(str(message))
        if not isinstance(payload, dict):
            raise ExternalModelError("external model returned an invalid payload")
        return payload

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        pieces: list[str] = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    pieces.append(str(content["text"]))
        return "\n".join(pieces)

    @staticmethod
    def _segment(item: dict[str, Any]) -> TranscriptSegment:
        confidence = item.get("confidence")
        return TranscriptSegment(
            speaker=str(item.get("speaker") or "unknown"),
            text=str(item.get("text") or ""),
            start_seconds=item.get("start"),
            end_seconds=item.get("end"),
            confidence=confidence if isinstance(confidence, (int, float)) else None,
        )
