import dataclasses
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password, verify_password
from app.models import User


def _fresh_client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _page_token(c: TestClient, url: str = "/contact") -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get(url).text).group(1)


def _make_user(db, email: str, password: str) -> User:
    user = User(email=email, name="Test", password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def test_form_post_without_csrf_token_is_rejected(client):
    with _fresh_client() as c:
        form = {"name": "A", "email": "a@example.com", "message": "Hello"}
        assert c.post("/contact", data=form, follow_redirects=False).status_code == 403
        assert c.post("/contact", data={**form, "csrf_token": "wrong"}, follow_redirects=False).status_code == 403
        ok = c.post("/contact", data={**form, "csrf_token": _page_token(c)}, follow_redirects=False)
        assert ok.status_code == 303


def test_logged_in_api_calls_need_csrf_header(user_client):
    token = user_client.headers.pop("X-CSRF-Token")
    try:
        r = user_client.post("/api/favorites/1")
        assert r.status_code == 403 and "Security check" in r.json()["detail"]
    finally:
        user_client.headers["X-CSRF-Token"] = token
    assert user_client.post("/api/favorites/1").status_code == 200


def test_login_locks_after_repeated_failures(client, db):
    _make_user(db, "lockout@test.local", "rightpassword1")
    with _fresh_client() as c:
        token = _page_token(c, "/login")
        wrong = {"email": "lockout@test.local", "password": "nope", "csrf_token": token}
        for _ in range(5):
            c.post("/login", data=wrong, follow_redirects=False)
        right = {**wrong, "password": "rightpassword1"}
        c.post("/login", data=right, follow_redirects=False)
        # Still locked out even with the correct password
        assert c.get("/dashboard", follow_redirects=False).status_code == 303
        assert "Too many failed login attempts" in c.get("/login").text


def test_successful_login_is_not_blocked_by_a_few_failures(client, db):
    _make_user(db, "fewfails@test.local", "rightpassword1")
    with _fresh_client() as c:
        token = _page_token(c, "/login")
        for _ in range(2):
            c.post("/login", data={"email": "fewfails@test.local", "password": "x", "csrf_token": token})
        c.post("/login", data={"email": "fewfails@test.local", "password": "rightpassword1", "csrf_token": token})
        assert c.get("/dashboard", follow_redirects=False).status_code == 200


def test_change_password(user_client, db):
    bad = user_client.post("/dashboard/password", data={
        "current_password": "wrong", "new_password": "newpassword1", "confirm_password": "newpassword1"})
    assert "current password is incorrect" in bad.text
    mismatch = user_client.post("/dashboard/password", data={
        "current_password": "password123", "new_password": "newpassword1", "confirm_password": "different1"})
    assert "don&#39;t match" in mismatch.text or "don't match" in mismatch.text
    user_client.post("/dashboard/password", data={
        "current_password": "password123", "new_password": "newpassword1", "confirm_password": "newpassword1"})
    user = db.scalar(select(User).where(User.email == user_client.email))
    db.refresh(user)
    assert verify_password("newpassword1", user.password_hash)


def test_admin_reset_link_is_single_use(admin_client, db):
    user = _make_user(db, "forgot@test.local", "oldpassword1")
    page = admin_client.post(f"/admin/users/{user.id}/reset-link")
    assert page.status_code == 200
    link = re.search(r'(http[^"<\s]+/reset-password\?token=[^"<\s]+)', page.text).group(1)
    token = link.split("token=", 1)[1]

    with _fresh_client() as c:
        assert "Choose a new password" in c.get(f"/reset-password?token={token}").text
        csrf = _page_token(c)
        form = {"token": token, "new_password": "brandnewpass1", "confirm_password": "brandnewpass1", "csrf_token": csrf}
        first = c.post("/reset-password", data=form, follow_redirects=False)
        assert first.headers["location"] == "/login"
        again = c.post("/reset-password", data=form, follow_redirects=False)
        assert again.headers["location"] == "/forgot-password"
        assert "Link expired" in c.get(f"/reset-password?token={token}").text

    db.refresh(user)
    assert verify_password("brandnewpass1", user.password_hash)


def test_forgot_password_emails_link_only_for_real_accounts(monkeypatch, client, db):
    from app.services import mailer

    _make_user(db, "emailme@test.local", "oldpassword1")
    monkeypatch.setattr(mailer, "settings", dataclasses.replace(mailer.settings, mail_from="no-reply@costcare.test"))
    sent = []
    monkeypatch.setattr(mailer, "send_password_reset", lambda to, link: sent.append((to, link)) or True)

    with _fresh_client() as c:
        csrf = _page_token(c, "/forgot-password")
        for email in ("emailme@test.local", "nobody@test.local"):
            r = c.post("/forgot-password", data={"email": email, "csrf_token": csrf})
            assert "If an account exists" in r.text

    assert len(sent) == 1 and sent[0][0] == "emailme@test.local"
    assert "/reset-password?token=" in sent[0][1]


def test_forgot_password_without_email_setup_explains(client):
    with _fresh_client() as c:
        assert "isn&#39;t switched on yet" in c.get("/forgot-password").text or "isn't switched on yet" in c.get("/forgot-password").text
