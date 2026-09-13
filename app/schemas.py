from pydantic import BaseModel, Field


class ProviderIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    provider_type: str = "Hospital"
    description: str = ""
    specialties: str = ""
    address: str = ""
    city: str = ""
    province: str = ""
    country: str = "South Africa"
    latitude: float | None = None
    longitude: float | None = None
    phone: str = ""
    email: str = ""
    website: str = ""
    booking_url: str = ""
    avg_wait_minutes: int | None = Field(None, ge=0)
    opening_hours: str = ""
    google_profile_url: str = ""
    is_partner: bool = False
    is_verified: bool = False
    is_active: bool = True


class ProviderPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    provider_type: str | None = None
    description: str | None = None
    specialties: str | None = None
    address: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    booking_url: str | None = None
    avg_wait_minutes: int | None = Field(None, ge=0)
    opening_hours: str | None = None
    is_partner: bool | None = None
    is_verified: bool | None = None
    is_active: bool | None = None


class GoogleLinkIn(BaseModel):
    url: str = Field(
        min_length=3,
        description="Google Business Profile / Google Maps share link, a place ID (ChIJ...), or 'Business name, City'.",
        examples=["https://maps.app.goo.gl/AbCdEf123"],
    )


class DiscoverIn(BaseModel):
    query: str = Field(min_length=3, examples=["private hospitals in Cape Town"])
    limit: int = Field(10, ge=1, le=20)


class ListingIn(BaseModel):
    service_id: int | None = None
    service_name: str | None = Field(None, description="Used when service_id is not given; created if new.")
    category: str | None = None
    price_min: float = Field(ge=0)
    price_max: float | None = Field(None, ge=0)
    notes: str | None = None
    is_available: bool | None = None


class ListingPatch(BaseModel):
    price_min: float | None = Field(None, ge=0)
    price_max: float | None = Field(None, ge=0)
    notes: str | None = None
    is_available: bool | None = None


class ServiceIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    category: str = "General"
    description: str = ""
    keywords: str = ""


class PlanIn(BaseModel):
    listing_id: int
    planned_date: str = ""
