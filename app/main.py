import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import AdminRequired, LoginRequired, ensure_admin_account
from .config import BASE_DIR, settings
from .database import SessionLocal, engine, init_db
from .routers import admin, api, pages
from .seed import seed_catalogue, seed_demo


def startup(with_demo_data: bool | None = None) -> None:
    """Create tables, the first admin, the service catalogue and (optionally) sample data.

    Called by the ASGI lifespan locally. On AWS Lambda it runs at cold start for the SQLite demo
    database, and as a one-off setup task (after each deploy) for Supabase Postgres.
    """
    init_db()
    with SessionLocal() as db:
        ensure_admin_account(db)
        seed_catalogue(db)
        if settings.seed_demo_data if with_demo_data is None else with_demo_data:
            seed_demo(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    yield


app = FastAPI(
    title="CostCare API",
    description="Healthcare price transparency for travellers in South Africa.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")

app.include_router(pages.router)
app.include_router(api.router)
app.include_router(admin.router)
app.include_router(admin.api_router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


@app.exception_handler(AdminRequired)
async def _admin_required(request: Request, exc: AdminRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    return pages.templates.TemplateResponse(
        request, "error.html", {"title": "Admin access required", "message": "Your account is not an admin."},
        status_code=403,
    )


@app.get("/healthz", tags=["public"], summary="Health check: which database is connected, and record counts")
def healthz():
    from sqlalchemy import func, select, text

    from .models import Provider, ProviderService

    body = {"status": "ok", "database": engine.dialect.name, "stage": os.getenv("STAGE", "local")}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            body["providers"] = db.scalar(select(func.count(Provider.id))) or 0
            body["prices"] = db.scalar(select(func.count(ProviderService.id))) or 0
    except Exception as exc:  # report the failure type only; messages can include host names
        return JSONResponse({**body, "status": "error", "error": type(exc).__name__}, status_code=503)
    return body
