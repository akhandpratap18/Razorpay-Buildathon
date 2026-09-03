"""Recoup — Application Configuration.

All secrets are loaded from environment variables only.
Never hardcode credentials here.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings — loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_env: str = Field(default="development")
    app_secret_key: str = Field(default="change-me-in-production")
    ops_api_token: str = Field(default="ops_dev_secret_123")
    log_level: str = Field(default="INFO")
    sentry_dsn: str = Field(default="")

    # ── Razorpay ─────────────────────────────────────────────────────────────
    razorpay_key_id: str = Field(default="")
    razorpay_key_secret: str = Field(default="")
    razorpay_webhook_secret: str = Field(default="")

    # ── Supabase / Postgres ───────────────────────────────────────────────────
    supabase_url: str = Field(default="https://msrtgoybhkvgffkttkzi.supabase.co")
    supabase_anon_key: str = Field(default="")
    supabase_service_role_key: str = Field(default="")
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:@db.msrtgoybhkvgffkttkzi.supabase.co:5432/postgres"
    )

    # ── Groq ─────────────────────────────────────────────────────────────────
    groq_api_key: str = Field(default="")
    groq_primary_model: str = Field(default="openai/gpt-oss-120b")
    groq_fallback_model: str = Field(default="openai/gpt-oss-20b")

    # ── Resend ───────────────────────────────────────────────────────────────
    resend_api_key: str = Field(default="")
    resend_from_email: str = Field(default="onboarding@resend.dev")
    frontend_url: str = Field(default="https://razorpay-buildathon.onrender.com")

    # ── LangGraph / Recovery limits ──────────────────────────────────────────
    max_recovery_attempts: int = Field(default=2)
    recovery_link_ttl_hours: int = Field(default=24)
    max_split_legs: int = Field(default=3)
    kokoro_tts_url: str = Field(default="")
    sarvam_api_key: str = Field(default="")


settings = Settings()
