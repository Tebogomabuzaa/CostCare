from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import AdminRequired, LoginRequired, ensure_admin_account
from .config import BASE_DIR, settings
from .database import SessionLocal, init_db
from .routers import admin, api, pages
from .seed import seed_catalogue, seed_demo


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_admin_account(db)
        seed_catalogue(db)
        if settings.seed_demo_data:
            seed_demo(db)
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
