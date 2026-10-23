"""Settings loaded from environment variables (see .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_phone(raw: str) -> str:
    """Keep digits only. Meta sends numbers like '15145551234' (no '+')."""
    return "".join(ch for ch in raw if ch.isdigit())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.gmail"), extra="ignore")

    # Postgres, e.g. postgresql://solecra:secret@postgres:5432/solecra
    database_url: str = "postgresql://solecra:solecra@localhost:5432/solecra"

    # WhatsApp Cloud API (Meta)
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v23.0"
    # Language code of the approved templates, exactly as Meta lists it: "English" is `en`,
    # "English (US)" is `en_US`, "English (UK)" is `en_GB`, "French" is `fr`.
    whatsapp_template_language: str = "en_US"
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
    llm_model_drafting: str = "anthropic/claude-sonnet-5.5"  # client-facing text: Sonnet-class (§4)
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
    outreach_draft_expiry_business_days: int = Field(default=2, ge=1)
    outreach_consent_max_age_days: int = Field(default=30, ge=1)  # older evidence is re-checked
    # Numbers an email may contain besides the lead's own facts (owners' approved list), and names
    # it must never contain (suppliers, brands). Comma-separated. CLAUDE.md §2 rules 3 and 4.
    outreach_allowed_numbers: str = ""
    outreach_blocked_terms: str = ""

    # Company identity for every outreach email (CASL: who sends, on whose behalf, how to reach
    # them). Values live in .env only. Sending refuses to run if any is empty.
    company_name: str = "Solecra"
    company_sender_name: str = ""
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_website: str = ""
    # Public https base URL for unsubscribe links (step 7 domain; a tunnel URL in dev).
    public_base_url: str = ""
    # test: only EMAIL_TEST_RECIPIENTS can ever receive outreach email; approved emails to anyone
    # else are held. live needs step 7 + step 15 sign-off (checked by the sender, #49).
    email_mode: Literal["test", "live"] = "test"
    email_test_recipients: str = ""  # comma-separated
    # Sending schedule (decided 2026-10-22): weekdays 09:00–16:00, 15–35 min random gap between emails.
    email_send_start_hour: int = Field(default=9, ge=0, le=23)
    email_send_end_hour: int = Field(default=16, ge=1, le=24)
    email_gap_min_minutes: int = Field(default=15, ge=0)
    email_gap_max_minutes: int = Field(default=35, ge=0)
    # Test mode only: send as soon as the job runs (any hour, no gap) so the whole path can be tried.
    email_test_ignores_schedule: bool = False

    # Gmail sending (#49). Kept in .env.gmail, read by the scheduler container only (the API never
    # sends email). Created by `python -m scripts.gmail_auth`.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_sender: str = ""  # the Gmail address the emails are sent from

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

    def test_recipients(self) -> set[str]:
        return {e.strip().lower() for e in self.email_test_recipients.split(",") if e.strip()}

    def allowed_numbers(self) -> set[str]:
        return {n.strip() for n in self.outreach_allowed_numbers.split(",") if n.strip()}

    def blocked_terms(self) -> list[str]:
        return [t.strip() for t in self.outreach_blocked_terms.split(",") if t.strip()]

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
