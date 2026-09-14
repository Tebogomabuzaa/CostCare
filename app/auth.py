import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import LoginAttempt, PasswordResetToken, User, utcnow

MAX_FAILED_LOGINS_PER_EMAIL = 5
MAX_FAILED_LOGINS_PER_IP = 20
LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_RESET_REQUESTS_PER_EMAIL = 3
MAX_RESET_REQUESTS_PER_IP = 10
RESET_TOKEN_TTL = timedelta(hours=1)

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


# --------------------------------------------------------------------------- passwords


def password_problem(password: str, confirm: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if password != confirm:
        return "The two passwords don't match."
    return None


# --------------------------------------------------------------------------- rate limiting


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _recent_count(db: Session, kind: str, since, **match) -> int:
    stmt = select(func.count(LoginAttempt.id)).where(LoginAttempt.kind == kind, LoginAttempt.created_at >= since)
    if kind == "login":
        stmt = stmt.where(LoginAttempt.succeeded.is_(False))
    for column, value in match.items():
        stmt = stmt.where(getattr(LoginAttempt, column) == value)
    return db.scalar(stmt) or 0


def too_many_failed_logins(db: Session, email: str, ip: str) -> bool:
    since = utcnow() - LOGIN_LOCKOUT_WINDOW
    return (_recent_count(db, "login", since, email=email) >= MAX_FAILED_LOGINS_PER_EMAIL
            or _recent_count(db, "login", since, ip=ip) >= MAX_FAILED_LOGINS_PER_IP)


def record_login_attempt(db: Session, email: str, ip: str, succeeded: bool) -> None:
    db.add(LoginAttempt(kind="login", email=email, ip=ip[:64], succeeded=succeeded))
    if succeeded:  # a successful login clears earlier failures for that account
        db.execute(delete(LoginAttempt).where(
            LoginAttempt.kind == "login", LoginAttempt.email == email, LoginAttempt.succeeded.is_(False)))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < utcnow() - timedelta(days=1)))
    db.commit()


def too_many_reset_requests(db: Session, email: str, ip: str) -> bool:
    since = utcnow() - timedelta(hours=1)
    return (_recent_count(db, "reset", since, email=email) >= MAX_RESET_REQUESTS_PER_EMAIL
            or _recent_count(db, "reset", since, ip=ip) >= MAX_RESET_REQUESTS_PER_IP)


def record_reset_request(db: Session, email: str, ip: str) -> None:
    db.add(LoginAttempt(kind="reset", email=email, ip=ip[:64], succeeded=True))
    db.commit()


# --------------------------------------------------------------------------- password reset links


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_password_reset_token(db: Session, user: User, by_admin: bool = False) -> str:
    """Create a one-time token (valid 1 hour) and invalidate any earlier unused ones."""
    token = secrets.token_urlsafe(32)
    db.execute(update(PasswordResetToken)
               .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
               .values(used_at=utcnow()))
    db.add(PasswordResetToken(user_id=user.id, token_hash=_hash_token(token),
                              expires_at=utcnow() + RESET_TOKEN_TTL, created_by_admin=by_admin))
    db.commit()
    return token


def find_valid_reset_token(db: Session, token: str) -> PasswordResetToken | None:
    if not token:
        return None
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash_token(token)))
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        return None
    return row


def invalidate_reset_tokens(db: Session, user: User) -> None:
    db.execute(update(PasswordResetToken)
               .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
               .values(used_at=utcnow()))
