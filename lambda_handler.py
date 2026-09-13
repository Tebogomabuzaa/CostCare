"""AWS Lambda entry points.

  handler       API Gateway (HTTP API) -> FastAPI app via Mangum (website + REST API)
  sync_handler  EventBridge daily schedule -> refresh Google Business Profile data

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

from app.main import app, startup  # noqa: E402


def _startup_with_retry() -> None:
    # Two cold starts seeding an empty database at the same moment can collide; retry once.
    try:
        startup()
    except Exception:
        log.exception("Startup failed, retrying once")
        startup()


_startup_with_retry()
_asgi = Mangum(app, lifespan="off")


def handler(event, context):
    return _asgi(event, context)


def sync_handler(event, context):
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
