"""Public JSON API used by the customer interface (and available to partners/apps)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..auth import get_current_user, require_user
from ..database import get_db
from ..models import Favorite, Notification, PlannedTreatment, Provider, ProviderService, Service, User
from ..schemas import PlanIn
from ..serializers import provider_dict
from ..services.search import SORTS, Catalog, describe, parse_query, run_search

router = APIRouter(prefix="/api", tags=["public"])


@router.get("/search", summary="AI search for procedures, locations and prices in one query")
def search(
    q: str = Query("", max_length=300, description="Free text, e.g. 'root canal in Cape Town under R8000'"),
    location: str | None = Query(None, description="Overrides the location detected in q"),
    category: str | None = None,
    min_price: float | None = Query(None, ge=0),
    max_price: float | None = Query(None, ge=0),
    min_rating: float | None = Query(None, ge=0, le=5),
    sort: str | None = Query(None, description="relevance | price_asc | price_desc | rating | wait | distance"),
    lat: float | None = None,
    lng: float | None = None,
    ai: bool = Query(True, description="Use OpenAI to interpret the query when configured"),
    limit: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    intent = parse_query(q, Catalog.load(db), use_ai=ai)
    overridden = False
    if location:
        intent.location, overridden = location, True
    if min_price is not None:
        intent.min_price, overridden = min_price, True
    if max_price is not None:
        intent.max_price, overridden = max_price, True
    if min_rating:
        intent.min_rating, overridden = min_rating, True
    if sort in SORTS:
        intent.sort = sort
    if overridden:
        intent.summary = describe(intent)

    result = run_search(db, intent, lat=lat, lng=lng, limit=limit, category=category or None)
    favorite_ids = set(db.scalars(select(Favorite.provider_id).where(Favorite.user_id == user.id))) if user else set()
    for item in result["results"]:
        item["provider"]["is_favorite"] = item["provider"]["id"] in favorite_ids
    return result


@router.get("/services", summary="Service catalogue with price ranges across providers")
def services(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Service, func.count(ProviderService.id), func.min(ProviderService.price_min), func.max(ProviderService.price_max))
        .outerjoin(ProviderService, (ProviderService.service_id == Service.id) & ProviderService.is_available.is_(True))
        .group_by(Service.id)
        .order_by(Service.category, Service.name)
    ).all()
    return [
        {"id": s.id, "name": s.name, "category": s.category, "keywords": s.keywords,
         "provider_count": count, "price_from": lo, "price_to": hi}
        for s, count, lo, hi in rows
    ]


@router.get("/providers", summary="List providers")
def providers(city: str = "", q: str = "", db: Session = Depends(get_db)):
    stmt = select(Provider).where(Provider.is_active.is_(True)).options(selectinload(Provider.reviews))
    if city:
        stmt = stmt.where(Provider.city.ilike(city))
    if q:
        stmt = stmt.where(or_(Provider.name.ilike(f"%{q}%"), Provider.specialties.ilike(f"%{q}%")))
    return [provider_dict(p) for p in db.scalars(stmt.order_by(Provider.name))]


@router.get("/providers/{key}", summary="Provider details with services, prices and reviews (id or slug)")
def provider_detail(key: str, db: Session = Depends(get_db)):
    cond = Provider.id == int(key) if key.isdigit() else Provider.slug == key
    provider = db.scalar(
        select(Provider).where(cond, Provider.is_active.is_(True))
        .options(selectinload(Provider.services).selectinload(ProviderService.service), selectinload(Provider.reviews))
    )
    if provider is None:
        raise HTTPException(404, "Provider not found")
    return provider_dict(provider, services=True, reviews=True)


@router.post("/favorites/{provider_id}", summary="Toggle a saved provider")
def toggle_favorite(provider_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if db.get(Provider, provider_id) is None:
        raise HTTPException(404, "Provider not found")
    existing = db.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.provider_id == provider_id))
    if existing:
        db.delete(existing)
        db.commit()
        return {"favorite": False}
    db.add(Favorite(user_id=user.id, provider_id=provider_id))
    db.commit()
    return {"favorite": True}


@router.get("/favorites")
def list_favorites(db: Session = Depends(get_db), user: User = Depends(require_user)):
    favs = db.scalars(select(Favorite).where(Favorite.user_id == user.id)
                      .options(selectinload(Favorite.provider).selectinload(Provider.reviews)))
    return [provider_dict(f.provider) for f in favs if f.provider]


@router.post("/planned", summary="Add a provider's service price to the user's cost tracker")
def add_planned(body: PlanIn, db: Session = Depends(get_db), user: User = Depends(require_user)):
    listing = db.get(ProviderService, body.listing_id)
    if listing is None:
        raise HTTPException(404, "Price listing not found")
    existing = db.scalar(select(PlannedTreatment).where(
        PlannedTreatment.user_id == user.id, PlannedTreatment.provider_service_id == listing.id,
        PlannedTreatment.status == "planned"))
    if existing:
        return {"id": existing.id, "created": False}
    plan = PlannedTreatment(
        user_id=user.id, provider_service_id=listing.id,
        label=f"{listing.service.name} at {listing.provider.name}",
        estimated_min=listing.price_min, estimated_max=listing.price_max, planned_date=body.planned_date,
    )
    db.add(plan)
    db.commit()
    return {"id": plan.id, "created": True}


@router.get("/notifications")
def notifications(db: Session = Depends(get_db), user: User = Depends(require_user)):
    items = db.scalars(select(Notification).where(Notification.user_id == user.id)
                       .order_by(Notification.created_at.desc()).limit(50))
    return [{"id": n.id, "title": n.title, "body": n.body, "link": n.link, "is_read": n.is_read,
             "created_at": n.created_at.isoformat()} for n in items]
