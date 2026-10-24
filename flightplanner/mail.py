"""Send email over SMTP (a Gmail App Password for now; a real email service later).

Settings come from the environment: SMTP_USER and SMTP_PASSWORD (required),
SMTP_HOST (default smtp.gmail.com), SMTP_PORT (587), MAIL_FROM (default
SMTP_USER) and MAIL_FROM_NAME.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr


class MailNotConfigured(RuntimeError):
    pass


def message(to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["To"], m["Subject"] = to, subject
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def send_messages(msgs: list[EmailMessage]) -> None:
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not user or not pw:
        raise MailNotConfigured("SMTP_USER and SMTP_PASSWORD must be set to send email.")
    sender = formataddr((os.environ.get("MAIL_FROM_NAME") or "Weekend Flying Outlook",
                         os.environ.get("MAIL_FROM") or user))
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")),
                      timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        for m in msgs:
            if "From" not in m:
                m["From"] = sender
            s.send_message(m)
