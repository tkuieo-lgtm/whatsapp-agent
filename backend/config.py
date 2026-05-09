from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    owner_phone: str
    bot_name: str = "מקס"
    anthropic_api_key: str
    database_url: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    tavily_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    # Web chat
    chat_password: str = "changeme"
    # Telegram
    telegram_bot_token: Optional[str] = None
    owner_telegram_id: Optional[str] = None
    whatsapp_service_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"
    timezone: str = "Asia/Jerusalem"
    morning_summary_hour: int = 7
    morning_summary_minute: int = 30
    weekly_summary_day: int = 4
    weekly_summary_hour: int = 17
    reminder_check_hours: int = 4
    reminder_threshold_hours: int = 6
    claude_model: str = "claude-sonnet-4-6"
    claude_rate_limit_per_hour: int = 100

    class Config:
        env_file = ("../.env", ".env")


settings = Settings()
