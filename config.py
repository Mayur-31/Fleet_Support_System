"""
Central place for environment/config loading.
"""
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_SECRET_KEY.\n"
        "Copy .env.example to .env and fill in your real project values."
    )

if not FLASK_SECRET_KEY:
    raise RuntimeError(
        "Missing FLASK_SECRET_KEY in .env.\n"
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(24))\"\n"
        "and add it to .env as FLASK_SECRET_KEY=<the value>"
    )

# SMTP config is deliberately optional, unlike the two blocks above.
# If it's missing, email_utils.py falls back to printing links to the
# console instead of raising — that lets the whole forgot-password /
# invite flow be built and tested before real email credentials exist.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = os.environ.get("SMTP_PORT")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("FROM_EMAIL")

SMTP_CONFIGURED = all([SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, FROM_EMAIL])