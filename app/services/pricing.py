"""Single place where prices change, so every change is recorded and users get notified."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Favorite,
    Notification,
    PlannedTreatment,
    PriceHistory,
    Provider,
    ProviderService,
    Service,
    User,
    utcnow,
)
from ..utils import price_range_label, slugify


def get_or_create_service(db: Session, name: str, category: str | None = None) -> tuple[Service, bool]:
    name = " ".join(name.split())
    service = db.scalar(select(Service).where(Service.name.ilike(name)))
    if service:
        if category and service.category == "General":
            service.category = category
        return service, False
    slug, n = slugify(name), 2
    while db.scalar(select(Service).where(Service.slug == slug)):
        slug = f"{slugify(name)}-{n}"
        n += 1
    service = Service(name=name, slug=slug, category=category or "General")
    db.add(service)
    db.flush()
    return service, True


def upsert_listing(
    db: Session,
    provider: Provider,
    service: Service,
    price_min: float,
    price_max: float | None = None,
    *,
    source: str = "admin",
    notes: str | None = None,
    is_available: bool | None = None,
) -> tuple[ProviderService, str]:
    """Create or update a provider's price for a service.

    Returns (listing, status) where status is "created", "updated" or "unchanged".
    An unchanged price still refreshes ``price_updated_at`` because the provider re-confirmed it.
    """
    if price_max is None:
        price_max = price_min
    if price_min < 0 or price_max < 0:
        raise ValueError("Prices cannot be negative")
    if price_min > price_max:
        price_min, price_max = price_max, price_min

    listing = None
    if provider.id is not None and service.id is not None:
        listing = db.scalar(
            select(ProviderService).where(
                ProviderService.provider_id == provider.id, ProviderService.service_id == service.id
            )
        )
    now = utcnow()

    if listing is None:
        listing = ProviderService(
            provider=provider,
            service=service,
            price_min=price_min,
            price_max=price_max,
            source=source,
            notes=notes or "",
            is_available=True if is_available is None else is_available,
            price_updated_at=now,
        )
        db.add(listing)
        db.flush()
        db.add(PriceHistory(listing=listing, new_min=price_min, new_max=price_max, source=source, changed_at=now))
        return listing, "created"

    if notes is not None:
        listing.notes = notes
    if is_available is not None:
        listing.is_available = is_available

    changed = abs(listing.price_min - price_min) > 0.005 or abs(listing.price_max - price_max) > 0.005
    listing.price_updated_at = now
    listing.source = source
    if not changed:
        return listing, "unchanged"

    old_min, old_max = listing.price_min, listing.price_max
    listing.price_min, listing.price_max = price_min, price_max
    db.add(
        PriceHistory(
            listing=listing, old_min=old_min, old_max=old_max,
            new_min=price_min, new_max=price_max, source=source, changed_at=now,
        )
    )
    notify_price_change(db, listing, old_min, old_max)
    return listing, "updated"


def notify_price_change(db: Session, listing: ProviderService, old_min: float, old_max: float) -> int:
    """Notify users who saved this provider or planned this treatment."""
    fav_users = select(Favorite.user_id).where(Favorite.provider_id == listing.provider_id)
    plan_users = select(PlannedTreatment.user_id).where(
        PlannedTreatment.provider_service_id == listing.id, PlannedTreatment.status == "planned"
    )
    users = db.scalars(
        select(User).where(
            User.notify_price_changes.is_(True),
            (User.id.in_(fav_users)) | (User.id.in_(plan_users)),
        )
    ).all()
    direction = "decreased" if listing.price_max < old_max else "increased"
    for user in users:
        db.add(
            Notification(
                user_id=user.id,
                title=f"Price {direction}: {listing.service.name}",
                body=(
                    f"{listing.provider.name} updated the price from "
                    f"{price_range_label(old_min, old_max)} to "
                    f"{price_range_label(listing.price_min, listing.price_max)}."
                ),
                link=f"/providers/{listing.provider.slug}",
            )
        )
    return len(users)
