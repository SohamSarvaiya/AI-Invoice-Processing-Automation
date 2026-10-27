from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infrastructure
    database_url: str = "sqlite:///./invoices.db"
    storage_dir: str = "./storage"
    api_key: str = ""  # empty = auth disabled (local dev only)
    max_upload_mb: int = 10

    # Extraction
    extraction_mode: str = "auto"  # auto | llm | rules
    llm_provider: str = "auto"  # auto | anthropic | openai | gemini  (auto = first key found)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"  # Claude model
    openai_model: str = "gpt-5.6-sol"
    gemini_model: str = "gemini-3.5-flash"
    llm_timeout_seconds: float = 90.0
    llm_max_tokens: int = 8000  # covers adaptive thinking + the JSON answer
    llm_effort: str = "low"  # low|medium|high. Blank = omit (use for models that reject output_config)

    # Retry
    max_attempts: int = 3
    retry_base_delay: float = 1.0

    # Validation rules (all deterministic)
    amount_tolerance: Decimal = Decimal("0.05")
    max_invoice_age_days: int = 365
    high_value_threshold: Decimal = Decimal("500000")
    allowed_currencies: str = "INR,USD,EUR,GBP"
    date_dayfirst: bool = True  # 03/04/2026 -> 3 April (India/EU)

    # Approval policy. 0 disables auto-approval: every invoice needs a human.
    auto_approve_limit: Decimal = Decimal("0")

    # ROI assumptions (editable per client, used only in /reports/summary)
    manual_minutes_per_invoice: float = 8.0
    review_minutes_per_invoice: float = 2.0
    correction_minutes_per_invoice: float = 6.0
    hourly_cost: Decimal = Decimal("300")

    @property
    def currencies(self) -> set[str]:
        return {c.strip().upper() for c in self.allowed_currencies.split(",") if c.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
