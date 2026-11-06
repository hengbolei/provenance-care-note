import os
from dataclasses import dataclass
from secrets import token_urlsafe


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Provenance Care Note"
    environment: str = os.getenv("CARENOTE_ENVIRONMENT", "development")
    token_secret: str = os.getenv("CARENOTE_TOKEN_SECRET", token_urlsafe(48))
    token_ttl_seconds: int = int(os.getenv("CARENOTE_TOKEN_TTL_SECONDS", "1800"))
    database_url: str | None = os.getenv("CARENOTE_DATABASE_URL")
    encryption_key: str | None = os.getenv("CARENOTE_ENCRYPTION_KEY")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("CARENOTE_OPENAI_MODEL", "gpt-5-mini")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    anthropic_model: str = os.getenv("CARENOTE_ANTHROPIC_MODEL", "claude-sonnet-5-5")
    # Any OpenAI Chat Completions-compatible service, hosted or local (e.g. Ollama, vLLM).
    compat_base_url: str | None = os.getenv("CARENOTE_COMPAT_BASE_URL")
    compat_api_key: str | None = os.getenv("CARENOTE_COMPAT_API_KEY")
    compat_model: str | None = os.getenv("CARENOTE_COMPAT_MODEL")
    # json_schema, json_object or tools: how the service returns structured output.
    compat_structured: str = os.getenv("CARENOTE_COMPAT_STRUCTURED", "json_schema")
    # recorded: replay cassettes only (default, no key needed); openai, anthropic or compatible:
    # live calls to that provider; live: live calls to agent_provider; record: live calls that
    # also write cassettes.
    agent_gateway: str = os.getenv("CARENOTE_AGENT_GATEWAY", "recorded")
    agent_provider: str = os.getenv("CARENOTE_AGENT_PROVIDER", "openai")
    # The provider to try when the live one fails; empty for no fallback.
    agent_fallback: str = os.getenv("CARENOTE_AGENT_FALLBACK", "")
    audit_retention_days: int = int(os.getenv("CARENOTE_AUDIT_RETENTION_DAYS", "2555"))
    public_base_url: str = os.getenv("CARENOTE_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    # A shared public demo (e.g. a hosted Space): shows a banner, resets the in-memory store to
    # the synthetic seed every `demo_reset_minutes`, and serves cookies over HTTPS only.
    public_demo: bool = os.getenv("CARENOTE_PUBLIC_DEMO", "") == "1"
    demo_reset_minutes: int = int(os.getenv("CARENOTE_DEMO_RESET_MINUTES", "30"))

    @property
    def secure_cookies(self) -> bool:
        return self.public_demo or self.environment not in {"development", "test"}

    def validate_production(self) -> None:
        if self.public_demo:
            # Visitors share one store that is wiped on a timer: never a place for real data.
            if self.environment == "production" or self.database_url:
                raise RuntimeError(
                    "CARENOTE_PUBLIC_DEMO runs on the in-memory demo store only; unset "
                    "CARENOTE_DATABASE_URL and use a non-production environment"
                )
            if self.demo_reset_minutes < 1:
                raise RuntimeError("CARENOTE_DEMO_RESET_MINUTES must be at least 1")
        if self.environment != "production":
            return
        missing = [
            name
            for name, value in {
                "CARENOTE_TOKEN_SECRET": os.getenv("CARENOTE_TOKEN_SECRET"),
                "CARENOTE_DATABASE_URL": self.database_url,
                "CARENOTE_ENCRYPTION_KEY": self.encryption_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("missing production settings: " + ", ".join(missing))
        if not self.public_base_url.startswith("https://"):
            raise RuntimeError("production CARENOTE_PUBLIC_BASE_URL must use HTTPS")


settings = Settings()
