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

    # LLMs via OpenRouter (app/llm.py). Model per step is in the routing table there;
    # these override it without a code change.
    openrouter_api_key: str = ""
    llm_model_scoring: str = "anthropic/claude-haiku-4.5"
    llm_model_research: str = "anthropic/claude-haiku-4.5"
    llm_fallback_models: str = ""  # comma-separated OpenRouter model ids, tried in order

    # Permits ingestion and scoring (step 9). Open decisions: keep configurable (CLAUDE.md §11).
    permits_lookback_days: int = Field(default=30, ge=1)
    permits_max_km: float = Field(default=30.0, gt=0)
    permits_min_units: int = Field(default=4, ge=1)
    lead_score_threshold: int = Field(default=60, ge=0, le=100)
    scoring_max_per_run: int = Field(default=100, ge=1)

    # Lead research (step 10): leads researched per daily run (≈ $0.05–0.10 each), model turns and
    # web searches allowed per lead. Decided 2026-10-22: 15 per day.
    research_max_per_day: int = Field(default=15, ge=1)
    research_max_turns: int = Field(default=8, ge=2)
    research_max_searches: int = Field(default=4, ge=0)
    research_max_usd_per_day: float = Field(default=1.5, gt=0)  # LLM + search spend, Montréal day
    outreach_daily_cap: int = Field(default=15, ge=0)  # new outreach emails/day (owners can change it)

    # Run tracking: after this many failures in a row, open/update a GitHub issue.
    run_failure_issue_threshold: int = Field(default=3, ge=1)
    # Fine-grained token with Issues read/write on this repo only. Empty = reporter off (log only).
    github_issues_token: str = ""
    github_repo: str = "JeffreyNahas/solecraAI"
    timezone: str = "America/Montreal"

    log_level: str = "INFO"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_max_attempts: int = 3

    @field_validator("database_url")
    @classmethod
    def _strip_driver(cls, v: str) -> str:
        # Accept either form; we store the plain libpq URL.
        return v.replace("postgresql+psycopg://", "postgresql://")

    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

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
