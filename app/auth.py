import hashlib
import hmac
import secrets

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(calc, digest)


class LoginRequired(Exception):
    """Raised when a page/API needs a signed-in user. Handled in main.py."""


class AdminRequired(Exception):
    """Raised when an admin-only page/API is accessed without admin rights."""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    return db.get(User, user_id) if user_id else None


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.headers.get("X-Admin-Token", "")
    if settings.admin_api_key and token and hmac.compare_digest(token, settings.admin_api_key):
        admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id))
        if admin:
            return admin
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired()
    if not user.is_admin:
        raise AdminRequired()
    return user


def ensure_admin_account(db: Session) -> None:
    if not settings.admin_email or not settings.admin_password:
        return
    user = db.scalar(select(User).where(User.email == settings.admin_email.lower()))
    if user is None:
        db.add(
            User(
                email=settings.admin_email.lower(),
                name="CostCare Admin",
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
            )
        )
        db.commit()
