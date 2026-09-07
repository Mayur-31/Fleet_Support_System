"""
Sends transactional emails (password reset, account invites) via SMTP.

Dev fallback: if SMTP_HOST/PORT/USERNAME/PASSWORD/FROM_EMAIL aren't all
set in .env, send_email() prints the message to the console instead of
raising. This lets the whole forgot-password / invite flow be built
and tested end-to-end before real email credentials exist — but it's
loud about doing so (a clearly labeled banner every time), so nobody
mistakes console output for a real delivered email once this is
running somewhere real.

Uses smtplib from the standard library — no new dependency needed.
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    SMTP_CONFIGURED,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    FROM_EMAIL,
)


def send_email(to_email: str, subject: str, body_text: str) -> bool:
    """
    Returns True if the email was sent (or, in dev mode, printed)
    without error. Never raises — a failed send shouldn't crash the
    request that triggered it; the caller decides what to tell the
    user (and per the forgot-password requirement, that's always the
    same generic message regardless of whether this succeeds).
    """
    if not SMTP_CONFIGURED:
        print("\n" + "=" * 60)
        print("DEV MODE — no SMTP configured, email not actually sent.")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
        print("-" * 60)
        print(body_text)
        print("=" * 60 + "\n")
        return True

    try:
        msg = MIMEMultipart()
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())

        return True
    except Exception as e:
        # Logged server-side only — never include the token/link itself
        # in a log message, and never surface SMTP internals to the user.
        print(f"Failed to send email to {to_email}: {e}")
        return False


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    subject = "Fleet Support Payments — Password Reset"
    body = (
        "You requested a password reset for your Fleet Support Payments account.\n\n"
        f"Click this link to set a new password:\n{reset_link}\n\n"
        "This link expires in 60 minutes and can only be used once.\n\n"
        "If you didn't request this, you can ignore this email — "
        "your password will not be changed."
    )
    return send_email(to_email, subject, body)


def send_invite_email(to_email: str, full_name: str, invite_link: str) -> bool:
    subject = "You've been invited to Fleet Support Payments"
    body = (
        f"Hi {full_name},\n\n"
        "You've been invited to create an account for Fleet Support Payments.\n\n"
        f"Click this link to set up your account:\n{invite_link}\n\n"
        "This link expires in 7 days and can only be used once."
    )
    return send_email(to_email, subject, body)