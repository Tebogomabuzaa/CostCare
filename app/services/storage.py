"""Archive uploaded price spreadsheets to Amazon S3 (when UPLOADS_BUCKET is configured)."""

import logging
import re

from ..config import settings
from ..models import utcnow

log = logging.getLogger(__name__)


def archive_upload(content: bytes, filename: str) -> str | None:
    """Store the original file for auditing. Returns the S3 key, or None if not configured/failed."""
    if not settings.uploads_bucket:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename or "upload.xlsx").strip("-") or "upload.xlsx"
    key = f"price-imports/{utcnow():%Y/%m/%d/%H%M%S}-{safe_name}"
    try:
        import boto3

        boto3.client("s3").put_object(
            Bucket=settings.uploads_bucket, Key=key, Body=content, ServerSideEncryption="AES256",
        )
    except Exception as exc:  # never block an import because the archive failed
        log.warning("Could not archive %s to S3: %s", filename, exc)
        return None
    return key
