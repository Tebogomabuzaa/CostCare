import math
import re
import unicodedata
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import utcnow

PROVIDER_TYPES = [
    "Hospital",
    "Day Hospital",
    "Clinic",
    "GP Practice",
    "Dental Practice",
    "Specialist Practice",
    "Radiology",
    "Pathology Lab",
    "Physiotherapy",
    "Optometrist",
    "Travel Clinic",
    "Pharmacy",
]

SERVICE_CATEGORIES = [
    "Primary Care",
    "Emergency",
    "Dental",
    "Imaging",
    "Pathology",
    "Surgery",
    "Maternity",
    "Eye Care",
    "Rehabilitation",
    "Travel Health",
    "Specialist",
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def unique_slug(db: Session, model, base: str, exclude_id: int | None = None) -> str:
    slug = slugify(base)
    candidate, n = slug, 2
    while True:
        existing = db.scalar(select(model).where(model.slug == candidate))
        if existing is None or existing.id == exclude_id:
            return candidate
        candidate = f"{slug}-{n}"
        n += 1


def format_zar(amount: float | None) -> str:
    if amount is None:
        return "—"
    return "R" + f"{amount:,.0f}".replace(",", " ")


def price_range_label(price_min: float, price_max: float) -> str:
    if abs(price_max - price_min) < 0.5:
        return format_zar(price_min)
    return f"{format_zar(price_min)} – {format_zar(price_max)}"


def time_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    seconds = (utcnow() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    for unit, size in (("year", 31_536_000), ("month", 2_592_000), ("week", 604_800), ("day", 86_400),
                       ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_price(value) -> float | None:
    """Accepts 1250, '1250', 'R1 250,00', 'R 1,250.50', '1.5k'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if not (isinstance(value, float) and math.isnan(value)) else None
    text = str(value).strip().lower().replace(" ", " ")
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("k"):
        multiplier, text = 1000.0, text[:-1]
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else text.replace(",", "")
    try:
        return round(float(text) * multiplier, 2)
    except ValueError:
        return None
