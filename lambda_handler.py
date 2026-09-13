"""AWS Lambda entry points.

  handler       API Gateway (HTTP API) -> FastAPI app via Mangum (website + REST API)
  sync_handler  EventBridge daily schedule -> refresh Google Business Profile data.
                Also runs the one-off database setup task: {"task": "setup", "seed_demo_data": true}

Secrets (SECRET_KEY, DATABASE_URL, API keys, admin credentials) are read from AWS Secrets
Manager once per cold start, before the app's settings are imported.
"""

import json
import logging
import os

log = logging.getLogger()
log.setLevel(logging.INFO)


def _load_secrets() -> None:
    arns = [a.strip() for a in os.getenv("APP_SECRET_ARNS", "").split(",") if a.strip()]
    if not arns:
        return
    import boto3

    client = boto3.client("secretsmanager")
    for arn in arns:
        values = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
        for key, value in values.items():
            if isinstance(value, str) and value.strip():  # blank = keep the default from the environment
                os.environ[key] = value.strip()


_load_secrets()

from mangum import Mangum  # noqa: E402

from app.database import engine  # noqa: E402
from app.main import app, startup  # noqa: E402


def _startup_with_retry(**kwargs) -> None:
    # Two instances seeding an empty database at the same moment can collide; retry once.
    try:
        startup(**kwargs)
    except Exception:
        log.exception("Startup failed, retrying once")
        startup(**kwargs)


# The SQLite demo database lives in /tmp and is empty on every cold start, so set it up here.
# Supabase Postgres is set up once by the "setup" task instead: doing it on every cold start would
# add dozens of round trips to a database in another region and can exceed Lambda's init timeout.
if engine.dialect.name == "sqlite":
    _startup_with_retry()

_asgi = Mangum(app, lifespan="off")


def handler(event, context):
    return _asgi(event, context)


def sync_handler(event, context):
    if isinstance(event, dict) and event.get("task") == "setup":
        return _setup(bool(event.get("seed_demo_data", True)))
    return _sync_google()


def _setup(seed_demo_data: bool) -> dict:
    """Create tables, admin account and catalogue (and sample providers). Safe to run repeatedly."""
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import Provider, ProviderService

    _startup_with_retry(with_demo_data=seed_demo_data)
    with SessionLocal() as db:
        result = {
            "task": "setup",
            "database": engine.dialect.name,
            "providers": db.scalar(select(func.count(Provider.id))) or 0,
            "prices": db.scalar(select(func.count(ProviderService.id))) or 0,
        }
    log.info(json.dumps(result))
    return result


def _sync_google() -> dict:
    from sqlalchemy import or_, select

    from app.config import settings
    from app.database import SessionLocal
    from app.models import Provider
    from app.services import google_places

    if not settings.google_maps_api_key:
        log.info("GOOGLE_MAPS_API_KEY is not set; skipping Google sync")
        return {"synced": 0, "failed": 0, "skipped": "GOOGLE_MAPS_API_KEY not configured"}

    synced = failed = 0
    with SessionLocal() as db:
        providers = db.scalars(select(Provider).where(
            or_(Provider.google_place_id != "", Provider.google_profile_url != ""))).all()
        for provider in providers:
            try:
                google_places.sync_provider(db, provider)
                synced += 1
            except Exception as exc:  # keep going; the error is stored on the provider
                db.rollback()
                failed += 1
                log.warning("Google sync failed for provider %s: %s", provider.id, exc)
    result = {"synced": synced, "failed": failed}
    log.info(json.dumps({"task": "google-sync", **result}))
    return result
