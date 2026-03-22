from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from config.settings import settings

logger = logging.getLogger(__name__)


class MailConfigurationError(RuntimeError):
    pass


async def send_email(email_receiver: str, subject: str, body: str) -> None:
    if not all([settings.email_sender, settings.smtp_username, settings.smtp_password]):
        raise MailConfigurationError("SMTP settings are incomplete")

    message = EmailMessage()
    message["From"] = settings.email_sender
    message["To"] = email_receiver
    message["Subject"] = subject
    message.add_alternative(body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as smtp:
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    logger.info("Email sent", extra={"recipient": email_receiver, "subject": subject})
