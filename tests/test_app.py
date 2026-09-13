from sqlalchemy import select

from app.models import Notification, PlannedTreatment, Provider, ProviderService, User


def test_public_pages_render(client, db):
    slug = db.scalar(select(Provider.slug).where(Provider.is_demo.is_(True)))
    for url in ["/", "/find", "/find?q=mri", "/about", "/contact", "/faq", "/privacy", "/terms", "/login",
                "/signup", f"/providers/{slug}", "/docs"]:
        r = client.get(url)
        assert r.status_code == 200, url


def test_unknown_provider_is_404(client):
    assert client.get("/providers/does-not-exist").status_code == 404


def test_dashboard_requires_login(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_non_admin_cannot_open_admin(user_client):
    assert user_client.get("/admin").status_code == 403
    assert user_client.get("/api/admin/providers").status_code == 403


def test_user_flow_favorite_plan_and_price_notification(user_client, admin_client, db):
    listing = db.scalar(select(ProviderService).order_by(ProviderService.id))

    assert user_client.post(f"/api/favorites/{listing.provider_id}").json() == {"favorite": True}
    assert user_client.post("/api/planned", json={"listing_id": listing.id}).json()["created"] is True
    assert user_client.post("/api/planned", json={"listing_id": listing.id}).json()["created"] is False

    # Provider page records recently viewed, dashboard shows everything
    user_client.get(f"/providers/{listing.provider.slug}")
    page = user_client.get("/dashboard")
    assert page.status_code == 200
    assert listing.provider.name in page.text

    # Admin changes the price through the dashboard form -> user notified
    r = admin_client.post(f"/admin/listings/{listing.id}",
                          data={"price_min": str(listing.price_min + 100), "price_max": str(listing.price_max + 100),
                                "notes": "", "is_available": "on"}, follow_redirects=False)
    assert r.status_code == 303
    user = db.scalar(select(User).where(User.email == user_client.email))
    db.expire_all()
    assert db.scalar(select(Notification).where(Notification.user_id == user.id)) is not None
    assert db.scalar(select(PlannedTreatment).where(PlannedTreatment.user_id == user.id)) is not None
    assert "Price increased" in user_client.get("/dashboard").text

    assert user_client.post(f"/api/favorites/{listing.provider_id}").json() == {"favorite": False}


def test_admin_pages_render(admin_client, db):
    pid = db.scalar(select(Provider.id))
    for url in ["/admin", "/admin/providers", "/admin/providers/new", f"/admin/providers/{pid}",
                "/admin/services", "/admin/import", "/admin/messages"]:
        assert admin_client.get(url).status_code == 200, url


def test_admin_create_provider_and_add_price(admin_client, db):
    r = admin_client.post("/admin/providers", data={
        "name": "Sea Point Test Clinic", "provider_type": "Clinic", "city": "Cape Town", "is_active": "on",
        "latitude": "", "longitude": "", "avg_wait_minutes": "15",
    }, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    r = admin_client.post(f"/admin/providers/{pid}/prices", data={
        "service_name": "Wound suturing", "category": "", "price_min": "R900", "price_max": "2 000", "notes": "",
    }, follow_redirects=False)
    assert r.status_code == 303

    data = admin_client.get("/api/search", params={"q": "stitches in cape town"}).json()
    assert any(i["provider"]["name"] == "Sea Point Test Clinic" and i["price_max"] == 2000 for i in data["results"])

    # Saving details from the edit page must not wipe Google data
    db.expire_all()
    provider = db.get(Provider, pid)
    provider.google_place_id = "ChIJkeepme0000000"
    db.commit()
    admin_client.post(f"/admin/providers/{pid}", data={"name": "Sea Point Test Clinic", "city": "Cape Town",
                                                       "latitude": "", "longitude": "", "avg_wait_minutes": ""})
    db.expire_all()
    assert db.get(Provider, pid).google_place_id == "ChIJkeepme0000000"


def test_contact_and_partner_forms(client, admin_client):
    client.post("/contact", data={"name": "A", "email": "a@example.com", "subject": "Hi", "message": "Question"})
    client.post("/partners/apply", data={"name": "B", "email": "b@example.com", "organisation": "B Clinic"})
    page = admin_client.get("/admin/messages").text
    assert "Question" in page and "B Clinic" in page
