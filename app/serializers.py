from .models import Provider, ProviderService, Review
from .utils import price_range_label, time_ago

_PROVIDER_FIELDS = [
    "id", "name", "slug", "provider_type", "description", "specialties", "address", "city", "province",
    "country", "latitude", "longitude", "phone", "email", "website", "booking_url", "avg_wait_minutes",
    "opening_hours", "google_profile_url", "google_place_id", "google_maps_uri", "google_rating",
    "google_review_count", "google_sync_error", "is_partner", "is_verified", "is_demo", "is_active",
]


def _iso(dt):
    return dt.isoformat() if dt else None


def listing_dict(listing: ProviderService) -> dict:
    return {
        "id": listing.id,
        "service_id": listing.service_id,
        "service_name": listing.service.name,
        "category": listing.service.category,
        "price_min": listing.price_min,
        "price_max": listing.price_max,
        "price_label": price_range_label(listing.price_min, listing.price_max),
        "currency": listing.currency,
        "notes": listing.notes,
        "is_available": listing.is_available,
        "source": listing.source,
        "price_updated_at": _iso(listing.price_updated_at),
        "updated_label": time_ago(listing.price_updated_at),
    }


def review_dict(review: Review) -> dict:
    return {
        "id": review.id,
        "source": review.source,
        "author_name": review.author_name,
        "author_url": review.author_url,
        "author_photo_url": review.author_photo_url,
        "rating": review.rating,
        "text": review.text,
        "relative_time": review.relative_time,
        "published_at": _iso(review.published_at),
    }


def provider_dict(provider: Provider, *, services: bool = False, reviews: bool = False) -> dict:
    data = {field: getattr(provider, field) for field in _PROVIDER_FIELDS}
    data.update(
        google_synced_at=_iso(provider.google_synced_at),
        updated_at=_iso(provider.updated_at),
        rating=provider.rating,
        review_count=provider.review_count,
        maps_link=provider.maps_link,
        directions_link=provider.directions_link,
        book_link=provider.book_link,
        url=f"/providers/{provider.slug}",
    )
    if services:
        data["services"] = [
            listing_dict(l) for l in sorted(provider.services, key=lambda l: (l.service.category, l.service.name))
        ]
    if reviews:
        data["reviews"] = [review_dict(r) for r in provider.reviews]
    return data
