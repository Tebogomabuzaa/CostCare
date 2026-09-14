"""Admin dashboard (HTML) and admin REST API.

HTML:  /admin/...        (session login, admin accounts only)
API:   /api/admin/...    (session login or header  X-Admin-Token: <ADMIN_API_KEY>)
"""

import json
import logging
from datetime import timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from ..auth import create_password_reset_token, require_admin
from ..security import verify_csrf
from ..config import settings
from ..database import SessionLocal, get_db
from ..models import (
    ContactMessage,
    Favorite,
    ImportJob,
    PlannedTreatment,
    PriceHistory,
    Provider,
    ProviderService,
    RecentView,
    Review,
    Service,
    User,
    utcnow,
)
from ..schemas import DiscoverIn, GoogleLinkIn, ListingIn, ListingPatch, ProviderIn, ProviderPatch, ServiceIn
from ..serializers import listing_dict, provider_dict
from ..services import excel_import, google_places
from ..services.google_places import GooglePlacesError
from ..services.storage import archive_upload
from ..services.pricing import get_or_create_service, upsert_listing
from ..templating import flash, templates
from ..utils import PROVIDER_TYPES, SERVICE_CATEGORIES, parse_price, slugify, unique_slug

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False,
                   dependencies=[Depends(require_admin), Depends(verify_csrf)])
api_router = APIRouter(prefix="/api/admin", tags=["admin"],
                       dependencies=[Depends(require_admin), Depends(verify_csrf)])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# --------------------------------------------------------------------------- shared helpers


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


async def form_data(request: Request) -> dict:
    return dict((await request.form()).items())


def _get_provider(db: Session, provider_id: int) -> Provider:
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(404, "Provider not found")
    return provider


def delete_providers(db: Session, ids: list[int]) -> int:
    if not ids:
        return 0
    listing_ids = select(ProviderService.id).where(ProviderService.provider_id.in_(ids))
    db.execute(update(PlannedTreatment).where(PlannedTreatment.provider_service_id.in_(listing_ids))
               .values(provider_service_id=None))
    db.execute(delete(PriceHistory).where(PriceHistory.provider_service_id.in_(listing_ids)))
    db.execute(delete(Favorite).where(Favorite.provider_id.in_(ids)))
    db.execute(delete(RecentView).where(RecentView.provider_id.in_(ids)))
    db.execute(delete(Review).where(Review.provider_id.in_(ids)))
    db.execute(delete(ProviderService).where(ProviderService.provider_id.in_(ids)))
    count = db.execute(delete(Provider).where(Provider.id.in_(ids))).rowcount
    db.commit()
    db.expire_all()
    return count


def delete_listing(db: Session, listing: ProviderService) -> None:
    db.execute(update(PlannedTreatment).where(PlannedTreatment.provider_service_id == listing.id)
               .values(provider_service_id=None))
    db.delete(listing)
    db.commit()


def sync_providers_background(provider_ids: list[int]) -> None:
    """Runs after the response is sent (Excel imports, sync-all)."""
    with SessionLocal() as db:
        for pid in provider_ids:
            provider = db.get(Provider, pid)
            if provider is None:
                continue
            try:
                google_places.sync_provider(db, provider)
            except (GooglePlacesError, httpx.HTTPError) as exc:
                log.warning("Google sync failed for provider %s: %s", pid, exc)
                db.rollback()


def _clear_google(provider: Provider) -> None:
    provider.google_place_id = ""
    provider.google_maps_uri = ""
    provider.google_rating = None
    provider.google_review_count = None
    provider.google_synced_at = None
    provider.google_sync_error = ""
    provider.reviews[:] = [r for r in provider.reviews if r.source != "google"]


def _link_google_from_form(request: Request, db: Session, provider: Provider, link: str) -> None:
    link = (link or "").strip()
    if link == provider.google_profile_url:
        return
    provider.google_profile_url = link
    _clear_google(provider)
    db.commit()
    if not link:
        return
    if not settings.google_maps_api_key:
        flash(request, "Google link saved. Add GOOGLE_MAPS_API_KEY to .env to capture reviews and location.", "warning")
        return
    try:
        result = google_places.link_google_profile(db, provider, link)
        flash(request, f"Google profile linked: {result['rating'] or '–'}★ from {result['review_count'] or 0} "
                       f"ratings, {result['reviews_stored']} reviews captured.")
    except (GooglePlacesError, httpx.HTTPError) as exc:
        db.rollback()
        provider.google_sync_error = str(exc)
        db.commit()
        flash(request, f"Saved, but the Google sync failed: {exc}", "error")


PROVIDER_TEXT_FIELDS = ["name", "provider_type", "description", "specialties", "address", "city", "province",
                        "country", "phone", "email", "website", "booking_url", "opening_hours"]


def _provider_from_form(provider: Provider, form: dict) -> None:
    for field in PROVIDER_TEXT_FIELDS:
        if field in form:
            setattr(provider, field, str(form[field]).strip())
    for field in ("latitude", "longitude"):
        value = str(form.get(field, "")).strip()
        setattr(provider, field, float(value) if value else None)
    wait = str(form.get("avg_wait_minutes", "")).strip()
    provider.avg_wait_minutes = int(float(wait)) if wait else None
    for field in ("is_partner", "is_verified", "is_active"):
        setattr(provider, field, field in form)


# =========================================================================== HTML admin


@router.get("")
def dashboard(request: Request, db: Session = Depends(get_db)):
    cutoff = utcnow() - timedelta(days=settings.stale_price_days)
    count = lambda stmt: db.scalar(stmt) or 0  # noqa: E731
    stats = {
        "providers": count(select(func.count(Provider.id))),
        "demo": count(select(func.count(Provider.id)).where(Provider.is_demo.is_(True))),
        "listings": count(select(func.count(ProviderService.id))),
        "services": count(select(func.count(Service.id))),
        "stale": count(select(func.count(ProviderService.id)).where(ProviderService.price_updated_at < cutoff)),
        "google_linked": count(select(func.count(Provider.id)).where(Provider.google_place_id != "")),
        "google_pending": count(select(func.count(Provider.id)).where(
            Provider.google_profile_url != "", Provider.google_place_id == "")),
        "google_errors": count(select(func.count(Provider.id)).where(Provider.google_sync_error != "")),
        "users": count(select(func.count(User.id))),
        "messages": count(select(func.count(ContactMessage.id))),
    }
    listing_opts = selectinload(PriceHistory.listing).options(
        selectinload(ProviderService.provider), selectinload(ProviderService.service))
    changes = db.scalars(select(PriceHistory).where(PriceHistory.old_min.is_not(None))
                         .options(listing_opts).order_by(PriceHistory.changed_at.desc()).limit(10)).all()
    stale = db.scalars(
        select(ProviderService).where(ProviderService.price_updated_at < cutoff)
        .options(selectinload(ProviderService.provider), selectinload(ProviderService.service))
        .order_by(ProviderService.price_updated_at).limit(10)
    ).all()
    imports = [(j, json.loads(j.summary_json)) for j in
               db.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(5))]
    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "stats": stats, "changes": changes, "stale": stale, "imports": imports,
    })


@router.get("/providers")
def providers_list(request: Request, q: str = "", city: str = "", db: Session = Depends(get_db)):
    stmt = (select(Provider, func.count(ProviderService.id))
            .outerjoin(ProviderService, ProviderService.provider_id == Provider.id)
            .group_by(Provider.id).order_by(Provider.name).options(selectinload(Provider.reviews)))
    if q:
        stmt = stmt.where(or_(Provider.name.ilike(f"%{q}%"), Provider.address.ilike(f"%{q}%")))
    if city:
        stmt = stmt.where(Provider.city == city)
    cities = sorted({c for c in db.scalars(select(Provider.city)) if c})
    return templates.TemplateResponse(request, "admin/providers.html", {
        "rows": db.execute(stmt).all(), "q": q, "city": city, "cities": cities,
    })


@router.get("/providers/new")
def provider_new(request: Request):
    return templates.TemplateResponse(request, "admin/provider_edit.html", {
        "provider": None, "types": PROVIDER_TYPES, "categories": SERVICE_CATEGORIES,
    })


@router.post("/providers")
def provider_create(request: Request, db: Session = Depends(get_db), form: dict = Depends(form_data)):
    if len(str(form.get("name", "")).strip()) < 2:
        flash(request, "Provider name is required.", "error")
        return _redirect("/admin/providers/new")
    provider = Provider(name=str(form["name"]).strip(), slug="tmp")
    try:
        _provider_from_form(provider, form)
    except ValueError:
        flash(request, "Latitude, longitude and wait time must be numbers.", "error")
        return _redirect("/admin/providers/new")
    provider.slug = unique_slug(db, Provider, f"{provider.name} {provider.city}")
    db.add(provider)
    db.commit()
    flash(request, f"{provider.name} created. Now add its services and prices.")
    _link_google_from_form(request, db, provider, str(form.get("google_profile_url", "")))
    return _redirect(f"/admin/providers/{provider.id}")


@router.get("/providers/{provider_id}")
def provider_edit(provider_id: int, request: Request, db: Session = Depends(get_db)):
    provider = db.scalar(
        select(Provider).where(Provider.id == provider_id)
        .options(selectinload(Provider.services).selectinload(ProviderService.service), selectinload(Provider.reviews))
    )
    if provider is None:
        return _redirect("/admin/providers")
    listings = sorted(provider.services, key=lambda l: (l.service.category, l.service.name))
    return templates.TemplateResponse(request, "admin/provider_edit.html", {
        "provider": provider, "listings": listings, "types": PROVIDER_TYPES, "categories": SERVICE_CATEGORIES,
        "all_services": db.scalars(select(Service).order_by(Service.name)).all(),
        "google_reviews": [r for r in provider.reviews if r.source == "google"],
    })


@router.post("/providers/{provider_id}")
def provider_update(provider_id: int, request: Request, db: Session = Depends(get_db),
                    form: dict = Depends(form_data)):
    provider = db.get(Provider, provider_id)
    if provider is None:
        return _redirect("/admin/providers")
    try:
        _provider_from_form(provider, form)
    except ValueError:
        flash(request, "Latitude, longitude and wait time must be numbers.", "error")
        return _redirect(f"/admin/providers/{provider_id}")
    if not provider.name:
        db.rollback()
        flash(request, "Provider name is required.", "error")
        return _redirect(f"/admin/providers/{provider_id}")
    if form.get("clear_demo"):
        provider.is_demo = False
    db.commit()
    flash(request, "Provider details saved.")
    if "google_profile_url" in form:  # the edit page links Google via the API card instead
        _link_google_from_form(request, db, provider, str(form["google_profile_url"]))
    return _redirect(f"/admin/providers/{provider_id}")


@router.post("/providers/{provider_id}/delete")
def provider_delete(provider_id: int, request: Request, db: Session = Depends(get_db)):
    provider = db.get(Provider, provider_id)
    if provider:
        name = provider.name
        delete_providers(db, [provider_id])
        flash(request, f"{name} deleted.")
    return _redirect("/admin/providers")


@router.post("/providers/{provider_id}/prices")
def price_add(provider_id: int, request: Request, service_name: str = Form(...), category: str = Form(""),
              price_min: str = Form(...), price_max: str = Form(""), notes: str = Form(""),
              db: Session = Depends(get_db)):
    provider = db.get(Provider, provider_id)
    lo, hi = parse_price(price_min), parse_price(price_max) if price_max.strip() else None
    if provider is None or not service_name.strip() or lo is None:
        flash(request, "Service name and a numeric minimum price are required.", "error")
        return _redirect(f"/admin/providers/{provider_id}#prices")
    service, created = get_or_create_service(db, service_name, category or None)
    _, status = upsert_listing(db, provider, service, lo, hi, source="admin", notes=notes.strip())
    db.commit()
    flash(request, f"{service.name}: price {status}{' (new service added to catalogue)' if created else ''}.")
    return _redirect(f"/admin/providers/{provider_id}#prices")


@router.post("/listings/{listing_id}")
def listing_update(listing_id: int, request: Request, price_min: str = Form(...), price_max: str = Form(""),
                   notes: str = Form(""), is_available: str | None = Form(None), db: Session = Depends(get_db)):
    listing = db.get(ProviderService, listing_id)
    if listing is None:
        return _redirect("/admin/providers")
    lo, hi = parse_price(price_min), parse_price(price_max) if price_max.strip() else None
    if lo is None:
        flash(request, "Minimum price must be a number.", "error")
    else:
        _, status = upsert_listing(db, listing.provider, listing.service, lo, hi, source="admin",
                                   notes=notes.strip(), is_available=is_available is not None)
        db.commit()
        flash(request, f"{listing.service.name}: price {status}.")
    return _redirect(f"/admin/providers/{listing.provider_id}#prices")


@router.post("/listings/{listing_id}/delete")
def listing_delete(listing_id: int, request: Request, db: Session = Depends(get_db)):
    listing = db.get(ProviderService, listing_id)
    if listing is None:
        return _redirect("/admin/providers")
    provider_id, name = listing.provider_id, listing.service.name
    delete_listing(db, listing)
    flash(request, f"{name} removed from this provider.")
    return _redirect(f"/admin/providers/{provider_id}#prices")


@router.get("/services")
def services_page(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Service, func.count(ProviderService.id))
        .outerjoin(ProviderService, ProviderService.service_id == Service.id)
        .group_by(Service.id).order_by(Service.category, Service.name)
    ).all()
    return templates.TemplateResponse(request, "admin/services.html", {"rows": rows, "categories": SERVICE_CATEGORIES})


@router.post("/services")
def service_create(request: Request, name: str = Form(...), category: str = Form("General"),
                   keywords: str = Form(""), db: Session = Depends(get_db)):
    service, created = get_or_create_service(db, name, category)
    if keywords.strip():
        service.keywords = keywords.strip()
    db.commit()
    flash(request, f"{service.name} {'added' if created else 'already exists — updated keywords'}.")
    return _redirect("/admin/services")


@router.post("/services/{service_id}")
def service_update(service_id: int, request: Request, name: str = Form(...), category: str = Form(...),
                   keywords: str = Form(""), db: Session = Depends(get_db)):
    service = db.get(Service, service_id)
    if service:
        clash = db.scalar(select(Service).where(Service.name.ilike(name.strip()), Service.id != service_id))
        if clash:
            flash(request, f"Another service is already called {clash.name}.", "error")
        else:
            service.name, service.category, service.keywords = name.strip(), category, keywords.strip()
            service.slug = slugify(service.name)
            db.commit()
            flash(request, f"{service.name} updated.")
    return _redirect("/admin/services")


@router.post("/services/{service_id}/delete")
def service_delete(service_id: int, request: Request, db: Session = Depends(get_db)):
    service = db.get(Service, service_id)
    if service:
        if db.scalar(select(func.count(ProviderService.id)).where(ProviderService.service_id == service_id)):
            flash(request, "This service still has provider prices. Remove those first.", "error")
        else:
            db.delete(service)
            db.commit()
            flash(request, "Service deleted.")
    return _redirect("/admin/services")


# ---- Excel import


@router.get("/import")
def import_page(request: Request, db: Session = Depends(get_db)):
    jobs = [(j, json.loads(j.summary_json)) for j in
            db.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(20))]
    return templates.TemplateResponse(request, "admin/import.html", {"jobs": jobs})


@router.get("/import/template.xlsx")
def import_template(db: Session = Depends(get_db)):
    return Response(excel_import.build_template(db), media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="costcare-price-template.xlsx"'})


@router.get("/export/prices.xlsx")
def export_prices(db: Session = Depends(get_db)):
    name = f"costcare-prices-{utcnow():%Y-%m-%d}.xlsx"
    return Response(excel_import.export_prices(db), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/import")
def import_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        flash(request, "File is larger than 10 MB.", "error")
        return _redirect("/admin/import")
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm", ".csv")):
        flash(request, "Please upload an .xlsx or .csv file.", "error")
        return _redirect("/admin/import")
    try:
        job = excel_import.create_import_job(db, content, file.filename or "upload.xlsx", admin,
                                             archive_key=archive_upload(content, file.filename or "upload.xlsx"))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _redirect("/admin/import")
    return _redirect(f"/admin/import/{job.id}")


@router.get("/import/{job_id}")
def import_preview(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if job is None:
        return _redirect("/admin/import")
    return templates.TemplateResponse(request, "admin/import_preview.html", {
        "job": job, "rows": json.loads(job.rows_json), "errors": json.loads(job.errors_json),
        "summary": json.loads(job.summary_json),
    })


@router.post("/import/{job_id}/apply")
def import_apply(job_id: int, request: Request, background: BackgroundTasks, hide_missing: str | None = Form(None),
                 db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if job is None:
        return _redirect("/admin/import")
    try:
        result = excel_import.apply_import_job(db, job, hide_missing=hide_missing is not None)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/admin/import/{job_id}")
    message = (f"Import applied: {result['created']} new prices, {result['updated']} updated, "
               f"{result['unchanged']} confirmed unchanged, {result['providers_created']} new providers.")
    if result["google_sync_provider_ids"]:
        if settings.google_maps_api_key:
            background.add_task(sync_providers_background, result["google_sync_provider_ids"])
            message += f" Syncing {len(result['google_sync_provider_ids'])} Google profiles in the background."
        else:
            message += " Add GOOGLE_MAPS_API_KEY to capture reviews for the Google links in this file."
    flash(request, message)
    return _redirect(f"/admin/import/{job_id}")


@router.post("/import/{job_id}/discard")
def import_discard(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if job and job.status == "pending":
        job.status = "discarded"
        db.commit()
        flash(request, "Import discarded — nothing was changed.")
    return _redirect("/admin/import")


# ---- misc


@router.get("/messages")
def messages_page(request: Request, db: Session = Depends(get_db)):
    items = db.scalars(select(ContactMessage).order_by(ContactMessage.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request, "admin/messages.html", {"items": items})


def _users(db: Session, q: str = "") -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(500)
    if q:
        stmt = stmt.where(or_(User.email.ilike(f"%{q}%"), User.name.ilike(f"%{q}%")))
    return list(db.scalars(stmt))


@router.get("/users")
def users_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "admin/users.html", {"users": _users(db, q), "q": q, "reset_link": None})


@router.post("/users/{user_id}/reset-link")
def user_reset_link(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        return _redirect("/admin/users")
    token = create_password_reset_token(db, user, by_admin=True)
    link = f"{request.url_for('reset_password_page')}?token={token}"
    # Rendered directly (not flashed) so the link never goes into the session cookie
    return templates.TemplateResponse(request, "admin/users.html", {
        "users": _users(db), "q": "", "reset_for": user, "reset_link": link,
    })


@router.post("/demo/delete")
def demo_delete(request: Request, db: Session = Depends(get_db)):
    ids = list(db.scalars(select(Provider.id).where(Provider.is_demo.is_(True))))
    flash(request, f"Deleted {delete_providers(db, ids)} sample providers.")
    return _redirect("/admin")


@router.post("/google/sync-all")
def google_sync_all(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    if not settings.google_maps_api_key:
        flash(request, "Add GOOGLE_MAPS_API_KEY to .env first.", "error")
        return _redirect("/admin")
    ids = list(db.scalars(select(Provider.id).where(
        or_(Provider.google_place_id != "", Provider.google_profile_url != ""))))
    background.add_task(sync_providers_background, ids)
    flash(request, f"Refreshing Google reviews, ratings and locations for {len(ids)} providers in the background.")
    return _redirect("/admin")


# =========================================================================== REST API


@api_router.get("/providers", summary="List providers (admin)")
def api_providers(q: str = "", city: str = "", db: Session = Depends(get_db)):
    stmt = select(Provider).options(selectinload(Provider.reviews)).order_by(Provider.name)
    if q:
        stmt = stmt.where(Provider.name.ilike(f"%{q}%"))
    if city:
        stmt = stmt.where(Provider.city.ilike(city))
    return [provider_dict(p) for p in db.scalars(stmt)]


@api_router.post("/providers", status_code=201, summary="Create a provider (optionally with a Google profile link)")
def api_create_provider(body: ProviderIn, background: BackgroundTasks, db: Session = Depends(get_db)):
    data = body.model_dump()
    link = data.pop("google_profile_url").strip()
    provider = Provider(**data, slug=unique_slug(db, Provider, f"{body.name} {body.city}"), google_profile_url=link)
    db.add(provider)
    db.commit()
    if link and settings.google_maps_api_key:
        background.add_task(sync_providers_background, [provider.id])
    return provider_dict(provider)


@api_router.get("/providers/{provider_id}")
def api_get_provider(provider_id: int, db: Session = Depends(get_db)):
    return provider_dict(_get_provider(db, provider_id), services=True, reviews=True)


@api_router.patch("/providers/{provider_id}")
def api_update_provider(provider_id: int, body: ProviderPatch, db: Session = Depends(get_db)):
    provider = _get_provider(db, provider_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    db.commit()
    return provider_dict(provider)


@api_router.delete("/providers/{provider_id}", status_code=204)
def api_delete_provider(provider_id: int, db: Session = Depends(get_db)):
    _get_provider(db, provider_id)
    delete_providers(db, [provider_id])
    return Response(status_code=204)


@api_router.post(
    "/providers/{provider_id}/google-profile",
    summary="Link a Google Business Profile and capture its reviews, rating and location",
)
def api_link_google(provider_id: int, body: GoogleLinkIn, db: Session = Depends(get_db)):
    provider = _get_provider(db, provider_id)
    try:
        return google_places.link_google_profile(db, provider, body.url)
    except GooglePlacesError as exc:
        db.rollback()
        provider.google_sync_error = str(exc)
        db.commit()
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        db.rollback()
        raise HTTPException(502, f"Could not reach Google: {exc}") from exc


@api_router.post("/providers/{provider_id}/google-sync", summary="Refresh Google reviews, rating and location")
def api_sync_google(provider_id: int, db: Session = Depends(get_db)):
    provider = _get_provider(db, provider_id)
    try:
        return google_places.sync_provider(db, provider)
    except GooglePlacesError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach Google: {exc}") from exc


@api_router.post("/google/preview", summary="Preview what Google returns for a link, without saving")
def api_google_preview(body: GoogleLinkIn):
    try:
        return google_places.preview_profile(body.url)
    except GooglePlacesError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach Google: {exc}") from exc


@api_router.post("/google/discover", summary="Import real facilities from Google search (no prices)")
def api_google_discover(body: DiscoverIn, db: Session = Depends(get_db)):
    try:
        created = google_places.discover_providers(db, body.query, body.limit)
    except GooglePlacesError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach Google: {exc}") from exc
    return {"created": len(created), "providers": created}


@api_router.post("/google/sync-all", summary="Refresh all linked Google profiles in the background")
def api_google_sync_all(background: BackgroundTasks, db: Session = Depends(get_db)):
    if not settings.google_maps_api_key:
        raise HTTPException(400, "GOOGLE_MAPS_API_KEY is not configured.")
    ids = list(db.scalars(select(Provider.id).where(
        or_(Provider.google_place_id != "", Provider.google_profile_url != ""))))
    background.add_task(sync_providers_background, ids)
    return {"queued": len(ids)}


@api_router.get("/providers/{provider_id}/services", summary="Services & prices offered by a provider")
def api_provider_services(provider_id: int, db: Session = Depends(get_db)):
    provider = _get_provider(db, provider_id)
    return [listing_dict(l) for l in sorted(provider.services, key=lambda l: l.service.name)]


@api_router.put("/providers/{provider_id}/services", summary="Add or update a service price for a provider")
def api_upsert_service_price(provider_id: int, body: ListingIn, db: Session = Depends(get_db)):
    provider = _get_provider(db, provider_id)
    if body.service_id:
        service = db.get(Service, body.service_id)
        if service is None:
            raise HTTPException(404, "Service not found")
    elif body.service_name:
        service, _ = get_or_create_service(db, body.service_name, body.category)
    else:
        raise HTTPException(422, "Provide service_id or service_name")
    try:
        listing, status = upsert_listing(db, provider, service, body.price_min, body.price_max, source="admin",
                                         notes=body.notes, is_available=body.is_available)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"status": status, "listing": listing_dict(listing)}


@api_router.patch("/listings/{listing_id}", summary="Update a price listing")
def api_update_listing(listing_id: int, body: ListingPatch, db: Session = Depends(get_db)):
    listing = db.get(ProviderService, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found")
    lo = body.price_min if body.price_min is not None else listing.price_min
    hi = body.price_max if body.price_max is not None else (lo if body.price_min is not None and lo > listing.price_max else listing.price_max)
    listing, status = upsert_listing(db, listing.provider, listing.service, lo, hi, source="admin",
                                     notes=body.notes, is_available=body.is_available)
    db.commit()
    return {"status": status, "listing": listing_dict(listing)}


@api_router.delete("/listings/{listing_id}", status_code=204)
def api_delete_listing(listing_id: int, db: Session = Depends(get_db)):
    listing = db.get(ProviderService, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found")
    delete_listing(db, listing)
    return Response(status_code=204)


@api_router.get("/listings/{listing_id}/history", summary="Price change history")
def api_listing_history(listing_id: int, db: Session = Depends(get_db)):
    listing = db.get(ProviderService, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found")
    return [{"old_min": h.old_min, "old_max": h.old_max, "new_min": h.new_min, "new_max": h.new_max,
             "source": h.source, "changed_at": h.changed_at.isoformat()} for h in listing.history]


@api_router.get("/services")
def api_services(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "category": s.category, "keywords": s.keywords, "description": s.description}
            for s in db.scalars(select(Service).order_by(Service.name))]


@api_router.post("/services", status_code=201)
def api_create_service(body: ServiceIn, db: Session = Depends(get_db)):
    service, created = get_or_create_service(db, body.name, body.category)
    service.description = body.description or service.description
    service.keywords = body.keywords or service.keywords
    db.commit()
    return {"id": service.id, "name": service.name, "category": service.category, "created": created}


@api_router.post("/import/excel", summary="Upload a price spreadsheet (.xlsx/.csv) and get a preview")
def api_import_excel(file: UploadFile = File(...), db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")
    try:
        job = excel_import.create_import_job(db, content, file.filename or "upload.xlsx", admin,
                                             archive_key=archive_upload(content, file.filename or "upload.xlsx"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.id, "status": job.status, "summary": json.loads(job.summary_json),
            "errors": json.loads(job.errors_json), "rows": json.loads(job.rows_json)[:200]}


@api_router.post("/import/{job_id}/apply", summary="Apply a previewed import")
def api_apply_import(job_id: int, background: BackgroundTasks, hide_missing: bool = False,
                     db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(404, "Import job not found")
    try:
        result = excel_import.apply_import_job(db, job, hide_missing=hide_missing)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result["google_sync_provider_ids"] and settings.google_maps_api_key:
        background.add_task(sync_providers_background, result["google_sync_provider_ids"])
    return result


@api_router.get("/import/template", summary="Download the Excel price template")
def api_template(db: Session = Depends(get_db)):
    return import_template(db)


@api_router.get("/export/prices", summary="Download all current prices as Excel")
def api_export(db: Session = Depends(get_db)):
    return export_prices(db)
