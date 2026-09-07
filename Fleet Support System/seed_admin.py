"""
One-time script to create the first admin account. None of the 5
accounts seed_users.py created (Dale, Zain, Charlotte, Mina, Paulina)
are admins — Ray only specified who *uses* the system, not who
manages accounts. Run this once for yourself (or anyone else you
want as an initial admin); after that, further accounts should go
through the Manage Users page in the app itself, not this script.

Usage:
    python seed_admin.py
"""
import getpass

from werkzeug.security import generate_password_hash

from database import supabase


def main():
    print("Create an admin account.\n")

    full_name = input("Full name: ").strip()
    email = input("Email: ").strip().lower()

    if not full_name or not email:
        print("Full name and email are required. Aborting.")
        return

    existing = supabase.table("users").select("id").eq("email", email).execute()
    if existing.data:
        print(f"An account with email {email} already exists — aborting.")
        return

    password = getpass.getpass("Password (hidden): ")
    if len(password) < 8:
        print("Password must be at least 8 characters — aborting.")
        return

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match — aborting.")
        return

    supabase.table("users").insert(
        {
            "full_name": full_name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "admin",
            "active": True,
        }
    ).execute()

    print(f"\nAdmin account created for {full_name} ({email}).")


if __name__ == "__main__":
    main()