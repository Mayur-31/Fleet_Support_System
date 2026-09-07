"""
Seeds the users table with the 5 people Ray named: Dale, Zain,
Charlotte, Mina, Paulina. Run this once to create their accounts.

Usage:
    python seed_users.py

You'll be prompted for each person's email and an initial password
(hidden as you type, via getpass — never shown on screen, never
stored in your shell history). They should be told to change it once
they can log in, since there's no "force password change" flow yet.

Safe to re-run: existing emails are skipped, not overwritten, so you
can add a 6th person later without touching the first 5.
"""
import getpass

from werkzeug.security import generate_password_hash

from database import supabase

# Ray confirmed these 5 people; role is "staff" for all of them for now —
# there's no admin/staff distinction enforced anywhere yet, the column
# just exists for when that's needed later.
USERS_TO_SEED = [
    {"full_name": "Dale & Zain", "role": "staff"},
    {"full_name": "Charlotte & Mina", "role": "staff"},
    {"full_name": "Paulina", "role": "staff"},
]


def main():
    existing = supabase.table("users").select("email").execute().data
    existing_emails = {row["email"].lower() for row in existing}

    for person in USERS_TO_SEED:
        print(f"\n--- {person['full_name']} ---")
        email = input("Email: ").strip().lower()

        if not email:
            print("Skipped — no email entered.")
            continue

        if email in existing_emails:
            print(f"Skipped — {email} already has an account.")
            continue

        password = getpass.getpass("Initial password (hidden): ")
        if len(password) < 8:
            print("Skipped — password must be at least 8 characters.")
            continue

        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Skipped — passwords didn't match.")
            continue

        supabase.table("users").insert(
            {
                "full_name": person["full_name"],
                "email": email,
                "password_hash": generate_password_hash(password),
                "role": person["role"],
                "active": True,
            }
        ).execute()
        print(f"Created account for {person['full_name']} ({email}).")

    print("\nDone.")


if __name__ == "__main__":
    main()