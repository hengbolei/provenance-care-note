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
    audit_retention_days: int = int(os.getenv("CARENOTE_AUDIT_RETENTION_DAYS", "2555"))
    public_base_url: str = os.getenv("CARENOTE_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

    @property
    def secure_cookies(self) -> bool:
        return self.environment not in {"development", "test"}

    def validate_production(self) -> None:
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
