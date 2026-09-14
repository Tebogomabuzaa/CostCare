import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session, selectinload

from ..auth import (
    client_ip,
    create_password_reset_token,
    find_valid_reset_token,
    get_current_user,
    hash_password,
    invalidate_reset_tokens,
    password_problem,
    record_login_attempt,
    record_reset_request,
    require_user,
    too_many_failed_logins,
    too_many_reset_requests,
    verify_password,
)
from ..config import settings
from ..database import get_db
from ..models import (
    ContactMessage,
    Favorite,
    LoginAttempt,
    Notification,
    PlannedTreatment,
    Provider,
    ProviderService,
    RecentView,
    Review,
    Service,
    User,
    utcnow,
)
from ..security import verify_csrf
from ..services import mailer
from ..templating import flash, templates
from ..utils import SERVICE_CATEGORIES, parse_price

router = APIRouter(include_in_schema=False, dependencies=[Depends(verify_csrf)])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SEARCH_EXAMPLES = [
    "Root canal in Cape Town under R8000",
    "Cheapest GP visit in Johannesburg",
    "MRI scan in Durban",
    "Yellow fever vaccine Pretoria",
    "Top rated dentist",
]

FAQS = [
    ("Are the prices exact quotes?",
     "No. They are estimated ranges supplied by providers and partners. Your final bill depends on your assessment, "
     "the practitioners involved and any medical aid cover. Always confirm with the provider before treatment."),
    ("How often are prices updated?",
     "Partner providers update their price lists through our dashboard, and every price shows when it was last "
     "updated. Save a provider to be notified when its prices change."),
    ("Where do the reviews come from?",
     "Ratings and reviews marked 'Google' come from each facility's Google Business Profile. Other reviews are "
     "written by CostCare users."),
    ("Do I need medical aid or travel insurance?",
     "Private healthcare in South Africa can be expensive. Travellers should carry insurance that covers medical "
     "treatment and evacuation. CostCare helps you estimate what you'd pay out of pocket."),
    ("What should I do in an emergency?",
     "Call 112 from a mobile phone or 10177 for an ambulance. Don't delay emergency care to compare prices."),
    ("I'm a provider. How do I list my prices?",
     "Apply on the About & Partners page. Once approved we'll set up your listing and you can upload price lists "
     "in Excel."),
]

CONTENT_PAGES = {
    "privacy": ("Privacy Policy", [
        ("What we collect", "The account details you give us (name, email, preferences), providers you save, "
         "treatments you add to your cost plan, and basic logs used to keep the service secure."),
        ("How we use it", "To show your dashboard, send price-change notifications you opted into, and improve "
         "search results. We do not sell personal information."),
        ("Health information", "CostCare shows prices, not medical records. Please don't enter medical details in "
         "free-text fields."),
        ("Your rights (POPIA)", "You can ask us to access, correct or delete your personal information at any time "
         "through the Contact page."),
    ]),
    "terms": ("Terms of Service", [
        ("Estimates, not quotes", "Prices on CostCare are estimated ranges. Final costs depend on your clinical "
         "assessment, the practitioners involved and your cover. Always confirm with the provider."),
        ("Not medical advice", "CostCare helps you plan costs. It does not recommend treatment. In an emergency "
         "call 112 from a mobile or 10177 for an ambulance."),
        ("Reviews", "Google reviews are shown from Google Maps with attribution. Community reviews must be honest "
         "and respectful; we remove abusive content."),
        ("Providers", "Partner providers are responsible for keeping their listed prices accurate and current."),
    ]),
    "faq": ("Frequently asked questions", FAQS),
}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(url: str | None, default: str) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else default


def _cities(db: Session) -> list[str]:
    return list(db.scalars(
        select(distinct(Provider.city)).where(Provider.is_active.is_(True), Provider.city != "").order_by(Provider.city)
    ))


# --------------------------------------------------------------------------- public pages


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    stats = {
        "providers": db.scalar(select(func.count(Provider.id)).where(Provider.is_active.is_(True))) or 0,
        "prices": db.scalar(select(func.count(ProviderService.id)).where(ProviderService.is_available.is_(True))) or 0,
        "cities": len(_cities(db)),
        "last_update": db.scalar(select(func.max(ProviderService.price_updated_at))),
    }
    popular = db.execute(
        select(Service.name, Service.category, func.min(ProviderService.price_min),
               func.max(ProviderService.price_max), func.count(ProviderService.id))
        .join(ProviderService, ProviderService.service_id == Service.id)
        .join(Provider, Provider.id == ProviderService.provider_id)
        .where(ProviderService.is_available.is_(True), Provider.is_active.is_(True))
        .group_by(Service.id, Service.name, Service.category)
        .order_by(func.count(ProviderService.id).desc(), Service.name)
        .limit(8)
    ).all()
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "popular": popular, "examples": SEARCH_EXAMPLES, "cities": _cities(db),
    })


@router.get("/find")
def find(request: Request, q: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "find.html", {
        "q": q, "cities": _cities(db), "categories": SERVICE_CATEGORIES,
    })


@router.get("/providers/{slug}")
def provider_page(slug: str, request: Request, db: Session = Depends(get_db),
                  user: User | None = Depends(get_current_user)):
    provider = db.scalar(
        select(Provider)
        .where(Provider.slug == slug, Provider.is_active.is_(True))
        .options(selectinload(Provider.services).selectinload(ProviderService.service), selectinload(Provider.reviews))
    )
    if provider is None:
        return templates.TemplateResponse(request, "error.html", {
            "title": "Provider not found", "message": "This provider may have been removed.",
        }, status_code=404)

    is_favorite = False
    if user:
        db.execute(delete(RecentView).where(RecentView.user_id == user.id, RecentView.provider_id == provider.id))
        db.add(RecentView(user_id=user.id, provider_id=provider.id))
        db.commit()
        is_favorite = db.scalar(select(Favorite.id).where(
            Favorite.user_id == user.id, Favorite.provider_id == provider.id)) is not None

    groups: dict[str, list[ProviderService]] = {}
    available = [l for l in provider.services if l.is_available]
    for listing in sorted(available, key=lambda l: (l.service.category, l.service.name)):
        groups.setdefault(listing.service.category, []).append(listing)

    return templates.TemplateResponse(request, "provider.html", {
        "provider": provider,
        "groups": groups,
        "last_updated": max((l.price_updated_at for l in available), default=None),
        "google_reviews": [r for r in provider.reviews if r.source == "google"],
        "other_reviews": sorted([r for r in provider.reviews if r.source != "google"],
                                key=lambda r: r.created_at, reverse=True),
        "is_favorite": is_favorite,
    })


@router.post("/providers/{slug}/reviews")
def add_review(slug: str, request: Request, rating: int = Form(...), text: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(require_user)):
    provider = db.scalar(select(Provider).where(Provider.slug == slug))
    if provider is None:
        return _redirect("/find")
    db.add(Review(
        provider_id=provider.id, user_id=user.id, source="costcare",
        author_name=user.name or user.email.split("@")[0], rating=max(1, min(5, rating)), text=text.strip()[:2000],
    ))
    db.commit()
    flash(request, "Thanks — your review has been posted.")
    return _redirect(f"/providers/{slug}#reviews")


@router.get("/about")
def about(request: Request, db: Session = Depends(get_db)):
    partners = db.scalar(select(func.count(Provider.id)).where(Provider.is_partner.is_(True))) or 0
    return templates.TemplateResponse(request, "about.html", {"partner_count": partners})


@router.post("/partners/apply")
def partner_apply(request: Request, name: str = Form(...), email: str = Form(...), phone: str = Form(""),
                  organisation: str = Form(...), message: str = Form(""), db: Session = Depends(get_db)):
    if not EMAIL_RE.match(email):
        flash(request, "Please enter a valid email address.", "error")
        return _redirect("/about#partner")
    db.add(ContactMessage(kind="partner", name=name.strip(), email=email.strip(), phone=phone.strip(),
                          organisation=organisation.strip(), subject="Partnership application", message=message.strip()))
    db.commit()
    flash(request, "Thank you! Our partnerships team will contact you within 2 business days.")
    return _redirect("/about#partner")


@router.get("/contact")
def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html", {"faqs": FAQS})


@router.post("/contact")
def contact_submit(request: Request, name: str = Form(...), email: str = Form(...), subject: str = Form(""),
                   message: str = Form(...), db: Session = Depends(get_db)):
    if not EMAIL_RE.match(email) or not message.strip():
        flash(request, "Please enter a valid email and a message.", "error")
        return _redirect("/contact")
    db.add(ContactMessage(kind="contact", name=name.strip(), email=email.strip(), subject=subject.strip(),
                          message=message.strip()))
    db.commit()
    flash(request, "Message sent. We usually reply within one business day.")
    return _redirect("/contact")


def _content_page(request: Request, key: str):
    title, sections = CONTENT_PAGES[key]
    return templates.TemplateResponse(request, "page.html", {"title": title, "sections": sections})


@router.get("/privacy")
def privacy(request: Request):
    return _content_page(request, "privacy")


@router.get("/terms")
def terms(request: Request):
    return _content_page(request, "terms")


@router.get("/faq")
def faq(request: Request):
    return _content_page(request, "faq")


# --------------------------------------------------------------------------- auth


@router.get("/login")
def login_page(request: Request, next: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": next})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""),
          db: Session = Depends(get_db)):
    email = email.strip().lower()
    ip = client_ip(request)
    retry_url = f"/login?next={next}" if next else "/login"
    if too_many_failed_logins(db, email, ip):
        flash(request, "Too many failed login attempts. Wait 15 minutes and try again, or reset your password.", "error")
        return _redirect(retry_url)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        record_login_attempt(db, email, ip, succeeded=False)
        flash(request, "Incorrect email or password.", "error")
        return _redirect(retry_url)
    record_login_attempt(db, email, ip, succeeded=True)
    request.session["user_id"] = user.id
    return _redirect(_safe_next(next, "/admin" if user.is_admin else "/dashboard"))


@router.get("/signup")
def signup_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "signup.html", {"cities": _cities(db)})


@router.post("/signup")
def signup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
           preferred_city: str = Form(""), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        flash(request, "Please enter a valid email address.", "error")
        return _redirect("/signup")
    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return _redirect("/signup")
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "An account with that email already exists. Please log in.", "error")
        return _redirect("/login")
    user = User(email=email, name=name.strip(), password_hash=hash_password(password), preferred_city=preferred_city)
    db.add(user)
    db.commit()
    request.session["user_id"] = user.id
    flash(request, f"Welcome to CostCare, {user.name or 'traveller'}!")
    return _redirect("/dashboard")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _redirect("/")


RESET_SENT_MESSAGE = "If an account exists for that email, we've sent a password reset link. It expires in 1 hour."


@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"email_enabled": mailer.email_enabled()})


@router.post("/forgot-password")
def forgot_password(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    if not mailer.email_enabled():
        flash(request, "Password reset by email isn't switched on yet. Contact us and an administrator will send "
                       "you a reset link.", "warning")
        return _redirect("/forgot-password")
    email, ip = email.strip().lower(), client_ip(request)
    if not too_many_reset_requests(db, email, ip):
        record_reset_request(db, email, ip)
        user = db.scalar(select(User).where(User.email == email))
        if user:
            token = create_password_reset_token(db, user)
            mailer.send_password_reset(user.email, f"{request.url_for('reset_password_page')}?token={token}")
    # Same answer whether or not the account exists (or the limit was hit), so emails can't be probed
    flash(request, RESET_SENT_MESSAGE)
    return _redirect("/login")


@router.get("/reset-password", name="reset_password_page")
def reset_password_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "reset_password.html", {
        "token": token, "valid": find_valid_reset_token(db, token) is not None,
    })


@router.post("/reset-password")
def reset_password(request: Request, token: str = Form(...), new_password: str = Form(...),
                   confirm_password: str = Form(...), db: Session = Depends(get_db)):
    row = find_valid_reset_token(db, token)
    if row is None:
        flash(request, "This reset link is invalid, already used, or expired. Request a new one.", "error")
        return _redirect("/forgot-password")
    problem = password_problem(new_password, confirm_password)
    if problem:
        flash(request, problem, "error")
        return _redirect(f"/reset-password?token={token}")
    user = db.get(User, row.user_id)
    user.password_hash = hash_password(new_password)
    row.used_at = utcnow()
    invalidate_reset_tokens(db, user)
    db.execute(delete(LoginAttempt).where(LoginAttempt.kind == "login", LoginAttempt.email == user.email))
    db.commit()
    flash(request, "Password updated. You can log in with your new password.")
    return _redirect("/login")


# --------------------------------------------------------------------------- dashboard


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    provider_opts = (selectinload(Provider.reviews), selectinload(Provider.services))
    favorites = db.scalars(
        select(Favorite).where(Favorite.user_id == user.id)
        .options(selectinload(Favorite.provider).options(*provider_opts)).order_by(Favorite.created_at.desc())
    ).all()
    recent = db.scalars(
        select(RecentView).where(RecentView.user_id == user.id)
        .options(selectinload(RecentView.provider)).order_by(RecentView.viewed_at.desc()).limit(8)
    ).all()
    planned = db.scalars(
        select(PlannedTreatment).where(PlannedTreatment.user_id == user.id)
        .options(selectinload(PlannedTreatment.listing).options(
            selectinload(ProviderService.provider), selectinload(ProviderService.service)))
        .order_by(PlannedTreatment.status.desc(), PlannedTreatment.created_at.desc())
    ).all()
    notifications = db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(15)
    ).all()
    upcoming = [p for p in planned if p.status == "planned"]
    totals = {
        "planned_min": sum(p.estimated_min for p in upcoming),
        "planned_max": sum(p.estimated_max for p in upcoming),
        "paid": sum((p.actual_cost if p.actual_cost is not None else p.estimated_max) for p in planned if p.status == "paid"),
    }
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "favorites": [f for f in favorites if f.provider], "recent": [r for r in recent if r.provider],
        "planned": planned, "notifications": notifications, "totals": totals, "cities": _cities(db),
    })


@router.post("/dashboard/profile")
def update_profile(request: Request, name: str = Form(""), preferred_country: str = Form("South Africa"),
                   preferred_city: str = Form(""), notify_price_changes: str | None = Form(None),
                   notify_promotions: str | None = Form(None), db: Session = Depends(get_db),
                   user: User = Depends(require_user)):
    user.name = name.strip()
    user.preferred_country = preferred_country
    user.preferred_city = preferred_city
    user.notify_price_changes = notify_price_changes is not None
    user.notify_promotions = notify_promotions is not None
    db.commit()
    flash(request, "Profile updated.")
    return _redirect("/dashboard#settings")


@router.post("/dashboard/planned")
def add_planned(request: Request, label: str = Form(...), estimated_min: str = Form(""), estimated_max: str = Form(""),
                planned_date: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_user)):
    lo = parse_price(estimated_min) or 0
    hi = parse_price(estimated_max) or lo
    db.add(PlannedTreatment(user_id=user.id, label=label.strip()[:200], estimated_min=min(lo, hi),
                            estimated_max=max(lo, hi), planned_date=planned_date))
    db.commit()
    flash(request, "Added to your cost tracker.")
    return _redirect("/dashboard#costs")


@router.post("/dashboard/planned/{plan_id}")
def update_planned(plan_id: int, request: Request, status: str = Form("planned"), actual_cost: str = Form(""),
                   planned_date: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_user)):
    plan = db.get(PlannedTreatment, plan_id)
    if plan and plan.user_id == user.id:
        plan.status = "paid" if status == "paid" else "planned"
        plan.actual_cost = parse_price(actual_cost)
        plan.planned_date = planned_date
        db.commit()
        flash(request, "Cost tracker updated.")
    return _redirect("/dashboard#costs")


@router.post("/dashboard/planned/{plan_id}/delete")
def delete_planned(plan_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    plan = db.get(PlannedTreatment, plan_id)
    if plan and plan.user_id == user.id:
        db.delete(plan)
        db.commit()
        flash(request, "Removed from your cost tracker.")
    return _redirect("/dashboard#costs")


@router.post("/dashboard/notifications/read")
def mark_notifications_read(db: Session = Depends(get_db), user: User = Depends(require_user)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))):
        n.is_read = True
    db.commit()
    return _redirect("/dashboard#notifications")


@router.post("/dashboard/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    confirm_password: str = Form(...), db: Session = Depends(get_db),
                    user: User = Depends(require_user)):
    if not verify_password(current_password, user.password_hash):
        flash(request, "Your current password is incorrect.", "error")
        return _redirect("/dashboard#security")
    problem = password_problem(new_password, confirm_password)
    if problem:
        flash(request, problem, "error")
        return _redirect("/dashboard#security")
    user.password_hash = hash_password(new_password)
    invalidate_reset_tokens(db, user)
    db.commit()
    flash(request, "Password changed.")
    return _redirect("/dashboard#security")
