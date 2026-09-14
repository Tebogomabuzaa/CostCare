"""Transactional email via Amazon SES. Disabled unless MAIL_FROM is set to a verified SES identity."""

import logging

from ..config import settings

log = logging.getLogger(__name__)


def email_enabled() -> bool:
    return bool(settings.mail_from)


def send_email(to: str, subject: str, text: str) -> bool:
    if not settings.mail_from:
        return False
    try:
        import boto3

        boto3.client("sesv2").send_email(
            FromEmailAddress=settings.mail_from,
            Destination={"ToAddresses": [to]},
            Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": text}}}},
        )
    except Exception as exc:  # never reveal delivery problems to the requester
        log.warning("Could not send email to %s: %s", to, exc)
        return False
    return True


def send_password_reset(to: str, link: str) -> bool:
    return send_email(
        to,
        "Reset your CostCare password",
        "Someone (hopefully you) asked to reset the password for your CostCare account.\n\n"
        f"Choose a new password here. The link works once and expires in 1 hour:\n{link}\n\n"
        "If you didn't ask for this, you can ignore this email. Your password won't change.",
    )
