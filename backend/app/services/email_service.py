"""Email the meeting summary (with the PDF report attached) over SMTP."""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings
from app.models import Meeting

logger = logging.getLogger(__name__)


class EmailNotConfiguredError(Exception):
    pass


class EmailSendError(Exception):
    pass


def _body(meeting: Meeting) -> str:
    lines = [f"Meeting: {meeting.title}", ""]
    if meeting.summary:
        lines += ["Overview", meeting.summary.overview, ""]
        if meeting.summary.decisions:
            lines += ["Decisions"] + [f"- {d}" for d in meeting.summary.decisions] + [""]
    if meeting.action_items:
        lines.append("Action items")
        for a in meeting.action_items:
            extra = ", ".join(x for x in (a.owner, f"due {a.due_date}" if a.due_date else None) if x)
            lines.append(f"- {a.task}" + (f" ({extra})" if extra else ""))
        lines.append("")
    lines.append("The full report is attached as a PDF.")
    return "\n".join(lines)


def send_summary_email(meeting: Meeting, recipients: list[str], pdf_bytes: bytes) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise EmailNotConfiguredError("Email is not configured. Set SMTP_HOST (and credentials) on the server.")
    sender = s.smtp_from or s.smtp_username
    if not sender:
        raise EmailNotConfiguredError("Email is not configured. Set SMTP_FROM on the server.")

    subject = " ".join(f"Meeting summary: {meeting.title}".split())  # no newlines in headers
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, sender, ", ".join(recipients)
    message.set_content(_body(meeting))
    message.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf", filename=f"meeting_{meeting.id}_summary.pdf"
    )

    use_ssl = s.smtp_port == 465
    try:
        client_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with client_cls(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if not use_ssl and s.smtp_use_tls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password or "")
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.exception("Sending summary email for meeting %s failed", meeting.id)
        raise EmailSendError(f"Could not send the email: {exc}") from exc
    logger.info("Summary for meeting %s emailed to %d recipient(s)", meeting.id, len(recipients))
