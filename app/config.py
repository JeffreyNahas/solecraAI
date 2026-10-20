"""Settings loaded from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_phone(raw: str) -> str:
    """Keep digits only. Meta sends numbers like '15145551234' (no '+')."""
    return "".join(ch for ch in raw if ch.isdigit())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres, e.g. postgresql://solecra:secret@postgres:5432/solecra
    database_url: str = "postgresql://solecra:solecra@localhost:5432/solecra"

    # WhatsApp Cloud API (Meta)
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v23.0"
    # When true, outbound WhatsApp messages are logged instead of sent.
    whatsapp_dry_run: bool = False

    # Owner allowlist: "15145551234:Name,15145556789:Name"
    owner_phones: str = ""

    # Optional: save raw webhook bodies here (dev only) to build test fixtures.
    webhook_capture_dir: str | None = None

    # Later phases
    openrouter_api_key: str = ""

    log_level: str = "INFO"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_max_attempts: int = 3

    @field_validator("database_url")
    @classmethod
    def _strip_driver(cls, v: str) -> str:
        # Accept either form; we store the plain libpq URL.
        return v.replace("postgresql+psycopg://", "postgresql://")

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    def owners(self) -> dict[str, str]:
        """Parse OWNER_PHONES into {phone: name}."""
        result: dict[str, str] = {}
        for item in self.owner_phones.split(","):
            item = item.strip()
            if not item:
                continue
            phone, _, name = item.partition(":")
            phone = normalize_phone(phone)
            if phone:
                result[phone] = name.strip() or phone
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
