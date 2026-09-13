import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "CostCare"
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-change-me")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'costcare.db'}")
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@costcare.local")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin12345")
    admin_api_key: str = os.getenv("ADMIN_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    google_maps_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    seed_demo_data: bool = _bool("SEED_DEMO_DATA", True)
    default_country: str = "South Africa"
    currency: str = "ZAR"
    # Prices older than this are flagged as stale in the admin dashboard
    stale_price_days: int = int(os.getenv("STALE_PRICE_DAYS", "90"))


settings = Settings()
