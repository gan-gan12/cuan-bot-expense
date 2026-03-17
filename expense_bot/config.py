from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_url: str
    bot_timezone: str
    telegram_webhook_secret: str
    webhook_setup_secret: str
    public_base_url: str
    # OCR settings
    ocr_backend: str           # "moondream" | "gemini" | "custom_url" | "hf_custom"
    ocr_backend_url: str       # URL backend OCR sendiri (untuk custom_url)
    custom_payload_mode: str   # "json_base64" | "multipart"
    huggingface_api_token: str
    gemini_api_key: str
    # Legacy / hf_custom backend
    florence_endpoint_url: str
    florence_model_id: str
    # Misc
    quickchart_url: str
    port: int
    allowed_telegram_users: list[int]

    @property
    def resolved_public_base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        vercel_url = os.getenv("VERCEL_URL", "").strip()
        if not vercel_url:
            return ""
        if vercel_url.startswith("http://") or vercel_url.startswith("https://"):
            return vercel_url.rstrip("/")
        return f"https://{vercel_url}".rstrip("/")

    @property
    def webhook_url(self) -> str:
        base_url = self.resolved_public_base_url
        if not base_url:
            return ""
        return f"{base_url}/telegram/webhook"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        bot_timezone=os.getenv("BOT_TIMEZONE", "Asia/Jakarta").strip(),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip(),
        webhook_setup_secret=os.getenv("WEBHOOK_SETUP_SECRET", "").strip(),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip(),
        ocr_backend=os.getenv("OCR_BACKEND", "moondream").strip(),
        ocr_backend_url=os.getenv("OCR_BACKEND_URL", "").strip(),
        custom_payload_mode=os.getenv("OCR_BACKEND_PAYLOAD", "json_base64").strip(),
        huggingface_api_token=os.getenv("HUGGINGFACE_API_TOKEN", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        florence_endpoint_url=os.getenv("FLORENCE_ENDPOINT_URL", "").strip(),
        florence_model_id=os.getenv("FLORENCE_MODEL_ID", "").strip(),
        quickchart_url=os.getenv("QUICKCHART_URL", "https://quickchart.io/chart").strip(),
        port=int(os.getenv("PORT", "8000")),
        allowed_telegram_users=_parse_allowed_users(os.getenv("ALLOWED_TELEGRAM_USERS", "")),
    )


def _parse_allowed_users(raw: str) -> list[int]:
    clean = raw.strip()
    if not clean:
        return []
    users = []
    for part in clean.split(","):
        p = part.strip()
        if p.isdigit():
            users.append(int(p))
    return users
