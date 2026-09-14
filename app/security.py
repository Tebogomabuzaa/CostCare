"""CSRF protection: a random token stored in the signed session cookie.

Every unsafe request (POST/PUT/PATCH/DELETE) from a browser must send the same token, either as the
``csrf_token`` form field or the ``X-CSRF-Token`` header. Pages expose it in
``<meta name="csrf-token">``; app.js adds it to forms and fetch calls automatically.
"""

import hmac
import secrets

from fastapi import Request

CSRF_SESSION_KEY = "csrf_token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class CSRFError(Exception):
    """Raised when an unsafe request has no valid CSRF token. Handled in main.py."""


def get_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method not in UNSAFE_METHODS:
        return
    if request.headers.get("X-Admin-Token"):
        return  # scripts using the admin API key: no browser cookies involved
    if request.url.path.startswith("/api/") and not request.session.get("user_id"):
        return  # anonymous API calls have no session to abuse; auth checks reject them
    expected = request.session.get(CSRF_SESSION_KEY, "")
    sent = request.headers.get("X-CSRF-Token", "")
    if not sent and request.headers.get("content-type", "").startswith(FORM_TYPES):
        sent = str((await request.form()).get("csrf_token", ""))
    if not expected or not sent or not hmac.compare_digest(sent, expected):
        raise CSRFError()
