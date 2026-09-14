"""Database tables.

Provider (hospital / clinic / practice)
  └── ProviderService  (one row per service the provider offers, with its price range)
        ├── Service     (shared catalogue entry, e.g. "Root canal treatment")
        └── PriceHistory (every price change, used for user notifications)
  └── Review           (Google reviews captured via the Places API + CostCare reviews)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_country: Mapped[str] = mapped_column(String(80), default="South Africa")
    preferred_city: Mapped[str] = mapped_column(String(80), default="")
    notify_price_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_promotions: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    favorites: Mapped[list["Favorite"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    provider_type: Mapped[str] = mapped_column(String(60), default="Hospital")
    description: Mapped[str] = mapped_column(Text, default="")
    specialties: Mapped[str] = mapped_column(Text, default="")  # comma separated, helps AI matching

    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(80), default="", index=True)
    province: Mapped[str] = mapped_column(String(80), default="")
    country: Mapped[str] = mapped_column(String(80), default="South Africa")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    booking_url: Mapped[str] = mapped_column(String(300), default="")
    avg_wait_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opening_hours: Mapped[str] = mapped_column(Text, default="")  # newline separated

    # Google Business Profile
    google_profile_url: Mapped[str] = mapped_column(String(600), default="")
    google_place_id: Mapped[str] = mapped_column(String(200), default="", index=True)
    google_maps_uri: Mapped[str] = mapped_column(String(600), default="")
    google_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    google_review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    google_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    google_sync_error: Mapped[str] = mapped_column(Text, default="")

    is_partner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    services: Mapped[list["ProviderService"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(back_populates="provider", cascade="all, delete-orphan")

    @property
    def rating(self) -> float | None:
        """Google rating if linked, otherwise the average of CostCare reviews."""
        if self.google_rating is not None:
            return round(self.google_rating, 1)
        own = [r.rating for r in self.reviews if r.source != "google"]
        return round(sum(own) / len(own), 1) if own else None

    @property
    def review_count(self) -> int:
        if self.google_review_count is not None:
            return self.google_review_count
        return len([r for r in self.reviews if r.source != "google"])

    @property
    def maps_link(self) -> str:
        from urllib.parse import quote_plus

        if self.google_maps_uri:
            return self.google_maps_uri
        if self.google_place_id:
            return (
                "https://www.google.com/maps/search/?api=1"
                f"&query={quote_plus(self.name)}&query_place_id={self.google_place_id}"
            )
        query = ", ".join(p for p in [self.name, self.address, self.city] if p)
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"

    @property
    def directions_link(self) -> str:
        from urllib.parse import quote_plus

        dest = (
            f"{self.latitude},{self.longitude}"
            if self.latitude is not None
            else ", ".join(p for p in [self.name, self.address, self.city] if p)
        )
        link = f"https://www.google.com/maps/dir/?api=1&destination={quote_plus(dest)}"
        if self.google_place_id:
            link += f"&destination_place_id={self.google_place_id}"
        return link

    @property
    def book_link(self) -> str:
        if self.booking_url:
            return self.booking_url
        if self.website:
            return self.website
        return f"tel:{self.phone.replace(' ', '')}" if self.phone else ""


class Service(Base):
    """Catalogue of procedures/services, shared by all providers."""

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(180), unique=True)
    category: Mapped[str] = mapped_column(String(80), default="General", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[str] = mapped_column(Text, default="")  # synonyms, comma separated

    listings: Mapped[list["ProviderService"]] = relationship(back_populates="service")


class ProviderService(Base):
    """A service offered by a provider together with its current price range."""

    __tablename__ = "provider_services"
    __table_args__ = (UniqueConstraint("provider_id", "service_id", name="uq_provider_service"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    price_min: Mapped[float] = mapped_column(Float)
    price_max: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="ZAR")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(30), default="admin")  # admin | excel | provider | demo
    price_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="services")
    service: Mapped[Service] = relationship(back_populates="listings")
    history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="PriceHistory.changed_at.desc()"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_service_id: Mapped[int] = mapped_column(
        ForeignKey("provider_services.id", ondelete="CASCADE"), index=True
    )
    old_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    old_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_min: Mapped[float] = mapped_column(Float)
    new_max: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), default="admin")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[ProviderService] = relationship(back_populates="history")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="costcare")  # google | costcare | demo
    author_name: Mapped[str] = mapped_column(String(160), default="")
    author_url: Mapped[str] = mapped_column(String(600), default="")
    author_photo_url: Mapped[str] = mapped_column(String(600), default="")
    rating: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    relative_time: Mapped[str] = mapped_column(String(80), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="reviews")


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "provider_id", name="uq_favorite"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="favorites")
    provider: Mapped[Provider] = relationship()


class RecentView(Base):
    __tablename__ = "recent_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship()


class PlannedTreatment(Base):
    """Cost tracking / payment tracking for a user's planned or completed treatments."""

    __tablename__ = "planned_treatments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_service_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_services.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(200))
    estimated_min: Mapped[float] = mapped_column(Float, default=0)
    estimated_max: Mapped[float] = mapped_column(Float, default=0)
    actual_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_date: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned | paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[ProviderService | None] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(300), default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportJob(Base):
    """An uploaded price spreadsheet. Parsed rows are stored so the admin can preview before applying."""

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | applied | discarded
    rows_json: Mapped[str] = mapped_column(Text, default="[]")
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ContactMessage(Base):
    __tablename__ = "contact_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="contact")  # contact | partner
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50), default="")
    organisation: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginAttempt(Base):
    """Login and password-reset attempts, used for rate limiting across all Lambda instances."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), default="login", index=True)  # login | reset
    email: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PasswordResetToken(Base):
    """One-time password reset links. Only a SHA-256 hash of the token is stored."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
