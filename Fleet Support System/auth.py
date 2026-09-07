"""
Authentication helpers: verifying a login attempt against the users
table, and a decorator to require a logged-in session on a route.

Kept separate from app.py the same way import_expenses.py and
generate_bank_csv.py are — so the auth logic is self-contained and
testable on its own, and app.py just wires it into routes.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import session, redirect, url_for, flash, request
from werkzeug.security import check_password_hash, generate_password_hash

from database import supabase

# Deliberately simple — "something@something.something" with no
# whitespace. Not RFC-5322-complete, but that's fine here: this is a
# sanity check against direct-POST garbage, not the sole guard against
# a genuinely malformed address (the database and any email delivery
# later would reject those too).
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

RESET_TOKEN_LIFETIME_MINUTES = 60


def verify_login(email: str, password: str):
    """
    Checks email + password against the users table.
    Returns the user record (dict) on success, or None on any failure —
    wrong email, wrong password, or an inactive account all fail the
    same way, so a login attempt can't be used to probe which emails
    exist in the system.
    """
    email = email.strip().lower()
    result = (
        supabase.table("users")
        .select("id, full_name, email, password_hash, role, active")
        .eq("email", email)
        .execute()
    )

    if not result.data:
        return None

    user = result.data[0]

    if not user["active"]:
        return None

    if not check_password_hash(user["password_hash"], password):
        return None

    return user


def login_required(view_func):
    """
    Decorator for routes that require a logged-in session. Redirects to
    the login page if there's no session, remembering where the user
    was trying to go (via ?next=...) so login sends them back there
    instead of always landing on Home.
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def is_safe_next_path(next_url: str) -> bool:
    """
    Only allow redirecting back to a local path after login (e.g.
    "/upload"). Without this check, a crafted link like
    /login?next=https://evil.example could redirect a user off-site
    immediately after they log in — a classic open-redirect. Rejects
    anything that isn't a same-site path, including protocol-relative
    URLs like "//evil.example" which browsers treat as external.
    """
    if not next_url:
        return False
    if not next_url.startswith("/"):
        return False
    if next_url.startswith("//"):
        return False
    return True


def is_valid_password(password: str) -> bool:
    """
    Minimum bar for any new password: at least 8 characters. Kept
    deliberately simple and consistent with what seed_users.py already
    enforced for the original 5 accounts — every password check in the
    app runs through this one function, so strengthening the rule
    later (requiring digits/symbols, checking a breached-password
    list, etc.) only needs to change it here.
    """
    return len(password) >= 8


def change_password(user_id: str, current_password: str, new_password: str, confirm_password: str):
    """
    Task 6B. Returns (success: bool, message: str) — never raises, so
    every failure path has a message safe to show the user directly.
    Only ever changes the account matching user_id, which the caller
    must take from the session, never from form input — otherwise
    one logged-in user could change another user's password.

    NOTE: kept for now per explicit instruction, alongside the new
    email-based reset flow below. Not yet removed.
    """
    if new_password != confirm_password:
        return False, "New password and confirmation do not match."

    if not is_valid_password(new_password):
        return False, "New password must be at least 8 characters."

    result = supabase.table("users").select("id, password_hash").eq("id", user_id).execute()
    if not result.data:
        return False, "Account not found."

    user = result.data[0]

    if not check_password_hash(user["password_hash"], current_password):
        return False, "Current password is incorrect."

    if check_password_hash(user["password_hash"], new_password):
        return False, "New password must be different from your current password."

    supabase.table("users").update(
        {"password_hash": generate_password_hash(new_password)}
    ).eq("id", user_id).execute()

    return True, "Password changed successfully."


def admin_required(view_func):
    """
    Task 6C, hardened: re-checks the current user's active status and
    role directly from the database on every admin route, instead of
    trusting session["role"] alone.

    NOTE: kept for now per explicit instruction, alongside the new
    email-based reset flow. Not yet removed.
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))

        result = supabase.table("users").select("active, role").eq("id", user_id).execute()
        current = result.data[0] if result.data else None

        if not current or not current["active"] or current["role"] != "admin":
            flash("You don't have permission to access that page.", "danger")
            return redirect(url_for("home"))

        return view_func(*args, **kwargs)

    return wrapped


def list_users():
    return (
        supabase.table("users")
        .select("id, full_name, email, role, active, created_at")
        .order("full_name")
        .execute()
        .data
    )


def create_user(full_name: str, email: str, password: str, confirm_password: str):
    """
    Task 6C. New accounts are always created with role="staff".
    Returns (success: bool, message: str).

    NOTE: kept for now per explicit instruction. The Manage Users page
    that calls this will be removed once invitation-based registration
    (a later step) replaces it.
    """
    full_name = full_name.strip()
    email = email.strip().lower()

    if not full_name:
        return False, "Full name is required."
    if not email:
        return False, "Email is required."
    if not EMAIL_PATTERN.match(email):
        return False, "That doesn't look like a valid email address."
    if password != confirm_password:
        return False, "Password and confirmation do not match."
    if not is_valid_password(password):
        return False, "Password must be at least 8 characters."

    existing = supabase.table("users").select("id").eq("email", email).execute()
    if existing.data:
        return False, f"An account with email {email} already exists."

    supabase.table("users").insert(
        {
            "full_name": full_name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "staff",
            "active": True,
        }
    ).execute()

    return True, f"Account created for {full_name}."


def set_user_active(user_id: str, active: bool) -> bool:
    """
    Returns True if a user was actually updated, False if user_id
    didn't match any row.

    NOTE: kept for now per explicit instruction.
    """
    existing = supabase.table("users").select("id").eq("id", user_id).execute()
    if not existing.data:
        return False

    supabase.table("users").update({"active": active}).eq("id", user_id).execute()
    return True


# ---------------------------------------------------------------------
# Forgot Password / Reset Password (new)
# ---------------------------------------------------------------------
# Design: the raw token only ever exists in memory and in the email
# link. Only its SHA-256 hash is stored in password_reset_tokens —
# same reasoning as password hashing: if the database were ever
# exposed, nothing usable for an account takeover would be sitting in
# it. A token is valid only if it exists, hasn't been used, and hasn't
# expired; all three are checked every time.

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_reset_token(email: str):
    """
    If an active account exists for this email, creates a reset token
    and returns the RAW token (to go in the email link). Returns None
    if no such account exists — but the caller (the /forgot-password
    route) must show the same generic message either way, so this
    function's return value is never used to reveal account existence
    to the end user, only to decide whether to actually send an email.
    """
    email = email.strip().lower()
    result = supabase.table("users").select("id, active").eq("email", email).execute()

    if not result.data or not result.data[0]["active"]:
        return None

    user_id = result.data[0]["id"]
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES)

    supabase.table("password_reset_tokens").insert(
        {
            "user_id": user_id,
            "token_hash": _hash_token(raw_token),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    return raw_token


def verify_reset_token(raw_token: str):
    """
    Returns the token row (dict with id, user_id) if raw_token is a
    real, unused, unexpired token — otherwise None. Used both to check
    a link before showing the reset form (GET) and again before
    actually changing the password (POST), since a token could expire
    or get used by a second click in between.
    """
    if not raw_token:
        return None

    token_hash = _hash_token(raw_token)
    result = (
        supabase.table("password_reset_tokens")
        .select("id, user_id, expires_at, used_at")
        .eq("token_hash", token_hash)
        .execute()
    )

    if not result.data:
        return None

    token_row = result.data[0]

    if token_row["used_at"]:
        return None

    expires_at = datetime.fromisoformat(token_row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) > expires_at:
        return None

    return token_row


def reset_password_with_token(raw_token: str, new_password: str, confirm_password: str):
    """
    Returns (success: bool, message: str). Re-verifies the token (not
    just trusts a value passed from the GET page), sets the new
    password, and marks the token used in the same operation — a
    second attempt with the same token, even seconds later, fails.
    """
    token_row = verify_reset_token(raw_token)
    if not token_row:
        return False, "This reset link is invalid or has expired. Please request a new one."

    if new_password != confirm_password:
        return False, "New password and confirmation do not match."

    if not is_valid_password(new_password):
        return False, "Password must be at least 8 characters."

    supabase.table("users").update(
        {"password_hash": generate_password_hash(new_password)}
    ).eq("id", token_row["user_id"]).execute()

    supabase.table("password_reset_tokens").update(
        {"used_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", token_row["id"]).execute()

    return True, "Your password has been reset. You can now log in with your new password."

def delete_user(user_id: str) -> bool:
    """
    Permanently deletes a user account and its associated reset tokens.
    Returns True if deletion succeeded, False if user not found.
    """
    # 1. Check if user exists
    existing = supabase.table("users").select("id").eq("id", user_id).execute()
    if not existing.data:
        return False

    # 2. Delete related reset tokens first (safety net, even if ON DELETE CASCADE is set)
    supabase.table("password_reset_tokens").delete().eq("user_id", user_id).execute()

    # 3. Delete the user
    supabase.table("users").delete().eq("id", user_id).execute()
    return True