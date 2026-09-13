"""Google Business Profile -> CostCare provider.

Paste any Google Maps / Business Profile link (share links like maps.app.goo.gl, full
/maps/place/ URLs, links with a place_id, or google.com/search?q= links) and we:
  1. resolve it to a Google place_id,
  2. fetch details from the Places API (New): address, coordinates, phone, website,
     opening hours, rating, review count and up to 5 reviews,
  3. store them on the provider so they show up in search results and on the provider page.

Google's terms require showing reviews with author attribution, and cached Places content
(other than the place_id) should be refreshed regularly — use "Sync all" in the admin or
``python manage.py sync-google`` on a schedule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Provider, Review, utcnow
from ..utils import unique_slug

PLACES_BASE = "https://places.googleapis.com/v1"
DETAIL_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "addressComponents", "location", "rating", "userRatingCount",
    "reviews", "googleMapsUri", "websiteUri", "nationalPhoneNumber", "internationalPhoneNumber",
    "regularOpeningHours", "types", "businessStatus",
])
SEARCH_FIELDS = ",".join(f"places.{f}" for f in [
    "id", "displayName", "formattedAddress", "location", "rating", "userRatingCount", "googleMapsUri", "types",
])
SHORT_LINK_HOSTS = ("maps.app.goo.gl", "goo.gl", "g.page", "g.co", "share.google")
PLACE_ID_RE = re.compile(r"^ChIJ[\w-]{10,}$")

GOOGLE_TYPE_MAP = {
    "hospital": "Hospital", "dentist": "Dental Practice", "dental_clinic": "Dental Practice",
    "doctor": "GP Practice", "medical_clinic": "Clinic", "pharmacy": "Pharmacy", "drugstore": "Pharmacy",
    "physiotherapist": "Physiotherapy", "medical_lab": "Pathology Lab",
}


class GooglePlacesError(Exception):
    pass


@dataclass
class ParsedLink:
    original: str
    resolved_url: str = ""
    place_id: str | None = None
    name: str | None = None
    query: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cid: str | None = None


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "CostCare/1.0"})


def parse_profile_link(link: str, client: httpx.Client | None = None) -> ParsedLink:
    link = (link or "").strip()
    if not link:
        raise GooglePlacesError("Please paste a Google Business Profile or Google Maps link.")
    parsed = ParsedLink(original=link)

    if PLACE_ID_RE.match(link):
        parsed.place_id = link
        return parsed
    if link.startswith("places/"):
        parsed.place_id = link.split("/", 1)[1]
        return parsed
    if not re.match(r"^https?://", link, re.I):
        parsed.query = link  # treat plain text as "business name, city"
        return parsed

    url = link
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in SHORT_LINK_HOSTS):
        http = _client(client)
        try:
            url = str(http.get(url).url)
        except httpx.HTTPError as exc:
            raise GooglePlacesError(f"Could not open the short link: {exc}") from exc
        finally:
            if client is None:
                http.close()
    # Consent interstitials wrap the real URL in ?continue=
    if "consent.google" in urlparse(url).netloc:
        url = parse_qs(urlparse(url).query).get("continue", [url])[0]
    parsed.resolved_url = url

    decoded = unquote_plus(url)
    u = urlparse(url)
    qs = parse_qs(u.query)

    for key in ("query_place_id", "place_id", "destination_place_id"):
        if qs.get(key):
            parsed.place_id = qs[key][0]
    m = re.search(r"place_id[:=](ChIJ[\w-]+)", decoded)
    if m and not parsed.place_id:
        parsed.place_id = m.group(1)

    m = re.search(r"/maps/place/([^/@]+)", u.path)
    if m:
        parsed.name = unquote_plus(m.group(1)).strip()
    m = re.search(r"/maps/search/([^/@]+)", u.path)
    if m and not parsed.name:
        parsed.query = unquote_plus(m.group(1)).strip()

    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", decoded) or re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", decoded)
    if m:
        parsed.latitude, parsed.longitude = float(m.group(1)), float(m.group(2))

    for key in ("cid", "ludocid"):
        if qs.get(key):
            parsed.cid = qs[key][0]
    for key in ("q", "query"):
        if qs.get(key) and not parsed.query:
            value = qs[key][0]
            if PLACE_ID_RE.match(value):
                parsed.place_id = parsed.place_id or value
            else:
                parsed.query = value
    return parsed


def _require_key() -> str:
    if not settings.google_maps_api_key:
        raise GooglePlacesError("GOOGLE_MAPS_API_KEY is not configured. Add it to your .env file.")
    return settings.google_maps_api_key


def _raise_for_google(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise GooglePlacesError(f"Google Places API error ({resp.status_code}): {message}")


def search_text(query: str, *, latitude: float | None = None, longitude: float | None = None,
                max_results: int = 5, client: httpx.Client | None = None) -> list[dict]:
    body: dict = {"textQuery": query, "regionCode": "ZA", "languageCode": "en", "pageSize": max_results}
    if latitude is not None and longitude is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": latitude, "longitude": longitude}, "radius": 2000.0}}
    http = _client(client)
    try:
        resp = http.post(
            f"{PLACES_BASE}/places:searchText",
            json=body,
            headers={"X-Goog-Api-Key": _require_key(), "X-Goog-FieldMask": SEARCH_FIELDS},
        )
        _raise_for_google(resp)
        return resp.json().get("places", [])
    finally:
        if client is None:
            http.close()


def get_place_details(place_id: str, client: httpx.Client | None = None) -> dict:
    http = _client(client)
    try:
        resp = http.get(
            f"{PLACES_BASE}/places/{place_id}",
            params={"languageCode": "en", "regionCode": "ZA"},
            headers={"X-Goog-Api-Key": _require_key(), "X-Goog-FieldMask": DETAIL_FIELDS},
        )
        _raise_for_google(resp)
        return resp.json()
    finally:
        if client is None:
            http.close()


def resolve_place_id(parsed: ParsedLink, client: httpx.Client | None = None) -> str:
    if parsed.place_id:
        return parsed.place_id
    text = parsed.name or parsed.query
    if not text:
        if parsed.cid:
            raise GooglePlacesError(
                "This link only contains a Google CID. Open the business in Google Maps, click Share, "
                "and paste that link instead."
            )
        raise GooglePlacesError("Could not find a business name or place ID in that link.")
    places = search_text(text, latitude=parsed.latitude, longitude=parsed.longitude, max_results=1, client=client)
    if not places:
        raise GooglePlacesError(f"Google could not find a place matching “{text}”.")
    return places[0]["id"]


def _component(place: dict, kind: str) -> str:
    for comp in place.get("addressComponents", []):
        if kind in comp.get("types", []):
            return comp.get("longText", "")
    return ""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def apply_place(db: Session, provider: Provider, place: dict, *, overwrite_contact: bool = False) -> int:
    """Copy Google data onto the provider and replace its Google reviews. Returns review count stored."""
    provider.google_place_id = place.get("id", provider.google_place_id)
    provider.google_maps_uri = place.get("googleMapsUri", "")
    provider.google_rating = place.get("rating")
    provider.google_review_count = place.get("userRatingCount")
    provider.google_synced_at = utcnow()
    provider.google_sync_error = ""

    loc = place.get("location") or {}
    if "latitude" in loc:
        provider.latitude, provider.longitude = loc["latitude"], loc["longitude"]

    def fill(attr: str, value: str | None):
        if value and (overwrite_contact or not getattr(provider, attr)):
            setattr(provider, attr, value)

    fill("name", (place.get("displayName") or {}).get("text"))
    fill("address", place.get("formattedAddress"))
    fill("city", _component(place, "locality") or _component(place, "administrative_area_level_2"))
    fill("province", _component(place, "administrative_area_level_1"))
    fill("phone", place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"))
    fill("website", place.get("websiteUri"))
    hours = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    if hours:
        provider.opening_hours = "\n".join(hours)

    provider.reviews[:] = [r for r in provider.reviews if r.source != "google"]
    stored = 0
    for item in place.get("reviews", []):
        author = item.get("authorAttribution") or {}
        text = (item.get("text") or {}).get("text") or (item.get("originalText") or {}).get("text") or ""
        provider.reviews.append(
            Review(
                source="google",
                author_name=author.get("displayName", "Google user"),
                author_url=author.get("uri", ""),
                author_photo_url=author.get("photoUri", ""),
                rating=float(item.get("rating") or 0),
                text=text,
                relative_time=item.get("relativePublishTimeDescription", ""),
                published_at=_parse_time(item.get("publishTime")),
            )
        )
        stored += 1
    return stored


def link_google_profile(db: Session, provider: Provider, link: str, client: httpx.Client | None = None) -> dict:
    parsed = parse_profile_link(link, client=client)
    place_id = resolve_place_id(parsed, client=client)
    place = get_place_details(place_id, client=client)
    provider.google_profile_url = link.strip()
    reviews = apply_place(db, provider, place)
    db.commit()
    return sync_summary(provider, reviews)


def sync_provider(db: Session, provider: Provider, client: httpx.Client | None = None) -> dict:
    try:
        if provider.google_place_id:
            place = get_place_details(provider.google_place_id, client=client)
        elif provider.google_profile_url:
            parsed = parse_profile_link(provider.google_profile_url, client=client)
            place = get_place_details(resolve_place_id(parsed, client=client), client=client)
        else:
            raise GooglePlacesError("No Google profile linked to this provider.")
        reviews = apply_place(db, provider, place)
        db.commit()
        return sync_summary(provider, reviews)
    except GooglePlacesError as exc:
        provider.google_sync_error = str(exc)
        db.commit()
        raise


def sync_summary(provider: Provider, reviews_stored: int) -> dict:
    return {
        "provider_id": provider.id,
        "name": provider.name,
        "place_id": provider.google_place_id,
        "address": provider.address,
        "city": provider.city,
        "latitude": provider.latitude,
        "longitude": provider.longitude,
        "phone": provider.phone,
        "website": provider.website,
        "rating": provider.google_rating,
        "review_count": provider.google_review_count,
        "reviews_stored": reviews_stored,
        "maps_link": provider.maps_link,
        "synced_at": provider.google_synced_at.isoformat() if provider.google_synced_at else None,
    }


def preview_profile(link: str, client: httpx.Client | None = None) -> dict:
    """Resolve a link and return Google's data without saving anything."""
    parsed = parse_profile_link(link, client=client)
    place = get_place_details(resolve_place_id(parsed, client=client), client=client)
    return {
        "place_id": place.get("id"),
        "name": (place.get("displayName") or {}).get("text"),
        "address": place.get("formattedAddress"),
        "location": place.get("location"),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "maps_link": place.get("googleMapsUri"),
        "reviews": [
            {
                "author": (r.get("authorAttribution") or {}).get("displayName"),
                "rating": r.get("rating"),
                "text": (r.get("text") or {}).get("text", ""),
                "when": r.get("relativePublishTimeDescription"),
            }
            for r in place.get("reviews", [])
        ],
    }


def discover_providers(db: Session, query: str, limit: int = 10, client: httpx.Client | None = None) -> list[dict]:
    """Import real facilities from Google (e.g. "private hospitals in Durban"). Prices are added separately."""
    created = []
    for place in search_text(query, max_results=min(limit, 20), client=client):
        if db.scalar(select(Provider).where(Provider.google_place_id == place["id"])):
            continue
        name = (place.get("displayName") or {}).get("text") or "Unnamed facility"
        types = place.get("types", [])
        provider = Provider(
            name=name,
            slug=unique_slug(db, Provider, name),
            provider_type=next((GOOGLE_TYPE_MAP[t] for t in types if t in GOOGLE_TYPE_MAP), "Clinic"),
            google_place_id=place["id"],
        )
        db.add(provider)
        db.flush()
        details = get_place_details(place["id"], client=client)
        apply_place(db, provider, details)
        db.commit()
        created.append(sync_summary(provider, len([r for r in provider.reviews if r.source == "google"])))
    return created
