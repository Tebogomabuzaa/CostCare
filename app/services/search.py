"""AI-powered search: one search bar for procedure + location + budget.

1. ``parse_query`` turns free text ("cheap root canal in Cape Town under R6000") into a
   ``SearchIntent``. It uses OpenAI when OPENAI_API_KEY is set, constrained to the real
   service catalogue in the database, and falls back to a rule-based parser otherwise.
2. ``run_search`` finds providers offering the matched services and ranks them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import distinct, or_, select
from sqlalchemy.orm import Session, contains_eager, selectinload

from ..config import settings
from ..models import Provider, ProviderService, Service
from ..utils import SERVICE_CATEGORIES, format_zar, haversine_km, price_range_label, time_ago

log = logging.getLogger(__name__)

SORTS = {"relevance", "price_asc", "price_desc", "rating", "wait", "distance"}

CITY_ALIASES = {
    "jhb": "Johannesburg", "joburg": "Johannesburg", "jozi": "Johannesburg", "johannesburg": "Johannesburg",
    "cpt": "Cape Town", "cape town": "Cape Town", "kaapstad": "Cape Town",
    "pta": "Pretoria", "tshwane": "Pretoria", "pretoria": "Pretoria",
    "dbn": "Durban", "durban": "Durban", "ethekwini": "Durban",
    "pe": "Gqeberha", "port elizabeth": "Gqeberha", "gqeberha": "Gqeberha",
    "bloem": "Bloemfontein", "bloemfontein": "Bloemfontein",
}

# Generic words that point at a whole category rather than one procedure
CATEGORY_WORDS = {
    "Dental": ["dentist", "dental", "teeth", "tooth", "orthodontist"],
    "Primary Care": ["gp", "general practitioner", "doctor", "consultation", "check up", "checkup", "sick"],
    "Emergency": ["emergency", "casualty", "er", "trauma", "accident", "urgent care"],
    "Imaging": ["scan", "imaging", "radiology", "radiologist"],
    "Pathology": ["lab", "laboratory", "pathology", "blood work"],
    "Surgery": ["surgery", "surgeon", "operation", "theatre"],
    "Maternity": ["maternity", "pregnant", "pregnancy", "birth", "baby", "delivery", "obstetrician"],
    "Eye Care": ["eye", "eyes", "optometrist", "ophthalmologist", "vision"],
    "Rehabilitation": ["physio", "physiotherapy", "physiotherapist", "rehab"],
    "Travel Health": ["travel clinic", "travel", "vaccine", "vaccination", "jab", "malaria"],
    "Specialist": ["specialist"],
}

STOPWORDS = {
    "a", "an", "the", "in", "near", "at", "for", "of", "to", "and", "or", "me", "my", "i", "need", "want",
    "find", "looking", "look", "get", "with", "on", "cost", "costs", "price", "prices", "how", "much",
    "is", "does", "where", "can", "some", "best", "cheap", "cheapest", "affordable", "good", "top", "rated",
    "under", "below", "less", "than", "max", "maximum", "budget", "around", "about", "south", "africa",
    "hospital", "clinic", "centre", "center", "place", "places", "rand", "zar", "stars", "star", "please",
}


@dataclass
class SearchIntent:
    query: str = ""
    service_ids: list[int] = field(default_factory=list)
    service_names: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    location: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_rating: float | None = None
    sort: str = "relevance"
    keywords: list[str] = field(default_factory=list)
    parser: str = "rules"
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- catalogue


@dataclass
class Catalog:
    services: list[Service]
    cities: list[str]

    @classmethod
    def load(cls, db: Session) -> "Catalog":
        services = list(db.scalars(select(Service).order_by(Service.name)))
        cities = [c for c in db.scalars(select(distinct(Provider.city)).where(Provider.city != "")) if c]
        return cls(services=services, cities=sorted(set(cities)))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.+\s]", " ", text.lower())).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _to_amount(num: str, suffix: str | None) -> float | None:
    try:
        value = float(num.replace(" ", "").replace(",", ""))
    except ValueError:
        return None
    return value * 1000 if suffix and suffix.lower() == "k" else value


# --------------------------------------------------------------------------- rule-based parser

_AMOUNT = r"r?\s?(\d[\d\s,]*(?:\.\d+)?)\s?(k)?"


def parse_rules(query: str, catalog: Catalog) -> SearchIntent:
    text = _norm(query)
    intent = SearchIntent(query=query, parser="rules")
    consumed: list[str] = []

    # --- budget
    m = re.search(rf"between\s+{_AMOUNT}\s+(?:and|to|-)\s+{_AMOUNT}", text)
    if m:
        intent.min_price, intent.max_price = _to_amount(m.group(1), m.group(2)), _to_amount(m.group(3), m.group(4))
        consumed.append(m.group(0))
    else:
        m = re.search(rf"(?:under|below|less than|max(?:imum)?|up to|within|budget(?: of)?|cheaper than)\s+{_AMOUNT}", text)
        if m:
            intent.max_price = _to_amount(m.group(1), m.group(2))
            consumed.append(m.group(0))
        m = re.search(rf"(?:over|above|more than|at least|from)\s+{_AMOUNT}", text)
        if m:
            intent.min_price = _to_amount(m.group(1), m.group(2))
            consumed.append(m.group(0))

    # --- rating
    m = re.search(r"(\d(?:\.\d)?)\s?\+?\s?stars?", text) or re.search(r"rated\s+(?:at least\s+)?(\d(?:\.\d)?)", text)
    if m:
        intent.min_rating = min(float(m.group(1)), 5.0)
        consumed.append(m.group(0))
    if re.search(r"\b(top|best|highly|well)[\s-]?rated\b|\bbest\b|\breviews?\b", text):
        intent.sort = "rating"
        intent.min_rating = intent.min_rating or 4.0

    # --- sort
    if re.search(r"\b(cheap|cheapest|affordable|low cost|lowest|budget|inexpensive)\b", text):
        intent.sort = "price_asc"
    elif re.search(r"\b(quick|quickest|fast|fastest|short wait|no wait|urgent|asap|today|now)\b", text):
        intent.sort = "wait"
    elif re.search(r"\b(near me|nearest|closest|nearby)\b", text):
        intent.sort = "distance"

    # --- location: known DB cities first, then aliases
    for city in sorted(catalog.cities, key=len, reverse=True):
        if _contains_phrase(text, city.lower()):
            intent.location = city
            consumed.append(city.lower())
            break
    if intent.location is None:
        for alias, city in sorted(CITY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
            if _contains_phrase(text, alias):
                intent.location = city
                consumed.append(alias)
                break
    if intent.location is None:
        m = re.search(r"\b(?:in|near|around)\s+([a-z][a-z\s]{2,30}?)(?:\s+(?:under|below|for|with|between|that)\b|$)", text)
        if m and m.group(1).strip() not in STOPWORDS and m.group(1).strip() != "me":
            intent.location = m.group(1).strip().title()
            consumed.append(m.group(1))

    remainder = text
    for part in consumed:
        remainder = remainder.replace(part, " ")
    remainder = _norm(remainder)

    # --- services
    scored = []
    for service in catalog.services:
        score = _service_score(remainder, service)
        if score >= 0.72:
            scored.append((score, service))
    scored.sort(key=lambda s: s[0], reverse=True)
    if scored:
        best = scored[0][0]
        # An exact phrase ("mri scan") shouldn't pull in loose one-word matches ("ct scan")
        cutoff = 0.9 if best >= 0.9 else best - 0.12
        chosen = [s for sc, s in scored if sc >= cutoff][:5]
        intent.service_ids = [s.id for s in chosen]
        intent.service_names = [s.name for s in chosen]

    # --- categories (only when no specific procedure was recognised)
    if not intent.service_ids:
        for category, words in CATEGORY_WORDS.items():
            if any(_contains_phrase(remainder, w) for w in words):
                intent.categories.append(category)

    if not intent.service_ids and not intent.categories:
        intent.keywords = [t for t in remainder.split() if len(t) > 2 and t not in STOPWORDS][:6]

    intent.summary = describe(intent)
    return intent


def _service_score(text: str, service: Service) -> float:
    if not text:
        return 0.0
    phrases = [_norm(service.name)] + [_norm(k) for k in service.keywords.split(",") if k.strip()]
    tokens = [t for t in text.split() if t not in STOPWORDS]
    best = 0.0
    for phrase in phrases:
        if not phrase:
            continue
        if _contains_phrase(text, phrase):
            best = max(best, 0.9 + min(len(phrase), 30) / 300)  # longer exact phrase = more specific
            continue
        words = [w for w in phrase.split() if w not in STOPWORDS and len(w) > 2]
        if not words or not tokens:
            continue
        hits = 0.0
        for w in words:
            ratio = max(SequenceMatcher(None, w, t).ratio() for t in tokens)
            if ratio >= 0.84 and len(w) >= 4:  # tolerate typos like "extration"
                hits += ratio
            elif w in tokens:
                hits += 1
        best = max(best, 0.85 * hits / len(words))
    return best


# --------------------------------------------------------------------------- AI parser

_ai_cache: dict[str, dict] = {}
# After an account-level OpenAI error (no credits, invalid key) skip AI for a while, so every search
# doesn't wait on calls that are bound to fail.
AI_PAUSE_SECONDS = 300
_ai_paused_until = 0.0


def _is_account_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "")
    return (status in (401, 403)
            or code in {"insufficient_quota", "credit_balance_exhausted", "invalid_api_key"}
            or (status == 429 and "quota" in str(exc).lower()))

_SYSTEM_PROMPT = """You turn healthcare price searches from travellers and residents in South Africa into JSON filters.
Rules:
- "services": pick ONLY exact names from the provided catalogue that best match the need. Map symptoms to
  likely services (e.g. "tooth pain" -> dental consultation, root canal, extraction; "broke my arm" ->
  emergency visit, x-ray). Return [] if nothing fits.
- "categories": pick from the provided categories only when the request is broad (e.g. "dentist").
- "location": city/suburb/province mentioned, as a proper name, else null. Expand abbreviations (CPT -> Cape Town, JHB -> Johannesburg).
- "min_price"/"max_price": numbers in ZAR ("5k" = 5000), else null.
- "min_rating": 1-5 if a rating is requested ("top rated" = 4), else null.
- "sort": one of relevance, price_asc, price_desc, rating, wait, distance.
- "keywords": other important words (provider names, specialties), max 5.
- "summary": one short friendly sentence describing what you are searching for.
Respond with JSON only."""


def parse_with_ai(query: str, catalog: Catalog) -> dict | None:
    global _ai_paused_until
    if not settings.openai_api_key or time.monotonic() < _ai_paused_until:
        return None
    key = query.strip().lower()
    if key in _ai_cache:
        return _ai_cache[key]
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key, timeout=8, max_retries=1)
        payload = {
            "catalogue": [{"name": s.name, "category": s.category} for s in catalog.services],
            "categories": SERVICE_CATEGORIES,
            "known_cities": catalog.cities,
            "query": query,
        }
        response = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:  # network, auth, quota, bad JSON: fall back to rules
        if _is_account_error(exc):
            _ai_paused_until = time.monotonic() + AI_PAUSE_SECONDS
            # Log the type and code only: auth error messages can echo part of the API key
            log.warning("OpenAI unavailable (%s, %s); using rule-based search for %d minutes",
                        type(exc).__name__, getattr(exc, "code", ""), AI_PAUSE_SECONDS // 60)
        else:
            log.warning("AI query parsing failed, using rule-based parser: %s", exc)
        return None
    if len(_ai_cache) > 500:
        _ai_cache.clear()
    _ai_cache[key] = data
    return data


def _merge_ai(intent: SearchIntent, data: dict, catalog: Catalog) -> SearchIntent:
    by_name = {s.name.lower(): s for s in catalog.services}
    services = [by_name[n.lower()] for n in data.get("services") or [] if isinstance(n, str) and n.lower() in by_name]
    if services:
        intent.service_ids = [s.id for s in services][:6]
        intent.service_names = [s.name for s in services][:6]
        intent.categories = []
        intent.keywords = []
    cats = [c for c in data.get("categories") or [] if c in SERVICE_CATEGORIES]
    if cats and not intent.service_ids:
        intent.categories = cats
        intent.keywords = []
    location = data.get("location")
    if isinstance(location, str) and location.strip():
        intent.location = CITY_ALIASES.get(location.strip().lower(), location.strip())
    for attr in ("min_price", "max_price", "min_rating"):
        value = data.get(attr)
        if isinstance(value, (int, float)) and value > 0:
            setattr(intent, attr, float(value))
    if data.get("sort") in SORTS and data.get("sort") != "relevance":
        intent.sort = data["sort"]
    keywords = [k for k in data.get("keywords") or [] if isinstance(k, str)]
    if keywords and not intent.service_ids and not intent.categories:
        intent.keywords = keywords[:5]
    intent.parser = "ai"
    intent.summary = data.get("summary") or describe(intent)
    return intent


def parse_query(query: str, catalog: Catalog, use_ai: bool = True) -> SearchIntent:
    intent = parse_rules(query, catalog)
    if use_ai and query.strip():
        data = parse_with_ai(query, catalog)
        if data:
            intent = _merge_ai(intent, data, catalog)
    return intent


def describe(intent: SearchIntent) -> str:
    what = ", ".join(intent.service_names) or ", ".join(intent.categories) or " ".join(intent.keywords) or "all services"
    parts = [what]
    if intent.location:
        parts.append(f"in {intent.location}")
    if intent.max_price and intent.min_price:
        parts.append(f"between {format_zar(intent.min_price)} and {format_zar(intent.max_price)}")
    elif intent.max_price:
        parts.append(f"under {format_zar(intent.max_price)}")
    elif intent.min_price:
        parts.append(f"from {format_zar(intent.min_price)}")
    if intent.min_rating:
        parts.append(f"rated {intent.min_rating:g}+")
    return "Searching for " + " ".join(parts)


# --------------------------------------------------------------------------- search execution


def _base_query():
    return (
        select(ProviderService)
        .join(ProviderService.provider)
        .join(ProviderService.service)
        .where(Provider.is_active.is_(True), ProviderService.is_available.is_(True))
        .options(
            contains_eager(ProviderService.provider).selectinload(Provider.reviews),
            contains_eager(ProviderService.service),
        )
    )


def _apply_what(stmt, intent: SearchIntent):
    if intent.service_ids:
        return stmt.where(ProviderService.service_id.in_(intent.service_ids))
    if intent.categories:
        return stmt.where(Service.category.in_(intent.categories))
    if intent.keywords:
        clauses = []
        for word in intent.keywords:
            like = f"%{word}%"
            clauses += [
                Provider.name.ilike(like), Provider.specialties.ilike(like), Provider.provider_type.ilike(like),
                Provider.description.ilike(like), Service.name.ilike(like), Service.keywords.ilike(like),
            ]
        return stmt.where(or_(*clauses))
    return stmt


def _apply_location(stmt, location: str):
    like = f"%{location}%"
    return stmt.where(or_(Provider.city.ilike(like), Provider.address.ilike(like), Provider.province.ilike(like)))


def run_search(
    db: Session,
    intent: SearchIntent,
    *,
    lat: float | None = None,
    lng: float | None = None,
    limit: int = 60,
    category: str | None = None,
) -> dict:
    notice = ""
    stmt = _apply_what(_base_query(), intent)
    if category:
        stmt = stmt.where(Service.category == category)
    if intent.max_price is not None:
        stmt = stmt.where(ProviderService.price_min <= intent.max_price)
    if intent.min_price is not None:
        stmt = stmt.where(ProviderService.price_max >= intent.min_price)

    listings = []
    if intent.location:
        listings = list(db.scalars(_apply_location(stmt, intent.location)).unique())
        if not listings:
            notice = f"No matches in {intent.location} yet — showing results from other locations."
    if not intent.location or not listings:
        listings = list(db.scalars(stmt).unique())

    if intent.min_rating:
        listings = [l for l in listings if (l.provider.rating or 0) >= intent.min_rating]

    items = [_serialize(l, intent, lat, lng) for l in listings]
    _sort(items, intent.sort if intent.sort in SORTS else "relevance", has_location=lat is not None)

    prices = [i["price_min"] for i in items] + [i["price_max"] for i in items]
    return {
        "query": intent.query,
        "intent": intent.to_dict(),
        "count": len(items),
        "notice": notice,
        "price_bounds": [min(prices), max(prices)] if prices else [0, 0],
        "provider_count": len({i["provider"]["id"] for i in items}),
        "results": items[:limit],
    }


def _serialize(listing: ProviderService, intent: SearchIntent, lat, lng) -> dict:
    p, s = listing.provider, listing.service
    distance = None
    if lat is not None and lng is not None and p.latitude is not None and p.longitude is not None:
        distance = round(haversine_km(lat, lng, p.latitude, p.longitude), 1)
    relevance = 1.0
    if intent.service_ids and s.id in intent.service_ids:
        relevance += 1.0 - intent.service_ids.index(s.id) * 0.15  # AI/rules order = best match first
    if intent.location and intent.location.lower() in (p.city or "").lower():
        relevance += 0.5
    relevance += (p.rating or 3.0) / 10 + (0.2 if p.is_partner else 0) + (0.1 if p.is_verified else 0)
    return {
        "listing_id": listing.id,
        "service": {"id": s.id, "name": s.name, "category": s.category},
        "provider": {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "type": p.provider_type,
            "address": p.address,
            "city": p.city,
            "province": p.province,
            "phone": p.phone,
            "rating": p.rating,
            "review_count": p.review_count,
            "rating_source": "Google" if p.google_rating is not None else "CostCare",
            "avg_wait_minutes": p.avg_wait_minutes,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "maps_link": p.maps_link,
            "directions_link": p.directions_link,
            "book_link": p.book_link,
            "is_partner": p.is_partner,
            "is_verified": p.is_verified,
            "is_demo": p.is_demo,
            "url": f"/providers/{p.slug}",
        },
        "price_min": listing.price_min,
        "price_max": listing.price_max,
        "currency": listing.currency,
        "price_label": price_range_label(listing.price_min, listing.price_max),
        "notes": listing.notes,
        "updated_at": listing.price_updated_at.isoformat() if listing.price_updated_at else None,
        "updated_label": time_ago(listing.price_updated_at),
        "distance_km": distance,
        "score": round(relevance, 3),
    }


def _sort(items: list[dict], sort: str, has_location: bool) -> None:
    big = float("inf")
    keys = {
        "price_asc": lambda i: (i["price_min"], i["price_max"]),
        "price_desc": lambda i: (-i["price_max"], -i["price_min"]),
        "rating": lambda i: (-(i["provider"]["rating"] or 0), -(i["provider"]["review_count"] or 0)),
        "wait": lambda i: (i["provider"]["avg_wait_minutes"] if i["provider"]["avg_wait_minutes"] is not None else big),
        "distance": lambda i: (i["distance_km"] if i["distance_km"] is not None else big),
        "relevance": lambda i: (-i["score"], i["price_min"]),
    }
    if sort == "distance" and not has_location:
        sort = "relevance"
    items.sort(key=keys[sort])
