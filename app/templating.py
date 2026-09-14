from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .config import BASE_DIR, settings
from .database import SessionLocal
from .models import Notification, User
from .security import get_csrf_token
from .utils import format_zar, price_range_label, time_ago


def _context(request: Request) -> dict:
    user, unread, flashes = None, 0, []
    if "session" in request.scope:
        flashes = request.session.pop("flash", [])
        user_id = request.session.get("user_id")
        if user_id:
            with SessionLocal() as db:
                user = db.get(User, user_id)
                if user:
                    unread = db.scalar(
                        select(func.count(Notification.id)).where(
                            Notification.user_id == user_id, Notification.is_read.is_(False)
                        )
                    ) or 0
    return {
        "current_user": user,
        "unread_count": unread,
        "flashes": flashes,
        "settings": settings,
        "path": request.url.path,
        "csrf_token": get_csrf_token(request) if "session" in request.scope else "",
    }


templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"), context_processors=[_context])
templates.env.filters["zar"] = format_zar
templates.env.filters["ago"] = time_ago
templates.env.globals["price_range"] = price_range_label


def flash(request: Request, message: str, kind: str = "success") -> None:
    messages = request.session.get("flash", [])
    messages.append({"message": message, "kind": kind})
    request.session["flash"] = messages
