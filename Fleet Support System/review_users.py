"""
Read-only review of the users table. Modifies nothing — this is
purely a report, per the explicit "show me before changing anything"
requirement.

Usage:
    python review_users.py
"""
from database import supabase

EXPECTED_STAFF = {"dale", "zain", "charlotte", "mina", "paulina"}


def main():
    users = (
        supabase.table("users")
        .select("id, full_name, email, role, active, created_at")
        .order("full_name")
        .execute()
        .data
    )

    if not users:
        print("No users found.")
        return

    print(f"{'Name':<20} {'Email':<35} {'Role':<8} {'Status':<10} Created")
    print("-" * 95)

    found_staff_names = set()
    admins = []

    for u in users:
        status = "Active" if u["active"] else "Deactivated"
        print(f"{u['full_name']:<20} {u['email']:<35} {u['role']:<8} {status:<10} {u['created_at']}")

        first_name = u["full_name"].strip().split()[0].lower() if u["full_name"].strip() else ""
        if first_name in EXPECTED_STAFF:
            found_staff_names.add(first_name)

        if u["role"] == "admin":
            admins.append(u)

    print(f"\n--- Expected staff check ---")
    missing = EXPECTED_STAFF - found_staff_names
    if missing:
        print(f"NOT FOUND: {', '.join(sorted(missing))} — no account matching this name exists yet.")
    else:
        print("All 5 expected staff names (Dale, Zain, Charlotte, Mina, Paulina) have a matching account.")

    print(f"\n--- Admin accounts ---")
    if not admins:
        print("WARNING: no admin account exists at all.")
    else:
        for a in admins:
            print(f"  {a['full_name']} ({a['email']}) — {'Active' if a['active'] else 'Deactivated'}")

    print(f"\n--- Possible test/leftover accounts (not matching any expected staff name, not admin) ---")
    any_flagged = False
    for u in users:
        first_name = u["full_name"].strip().split()[0].lower() if u["full_name"].strip() else ""
        if u["role"] != "admin" and first_name not in EXPECTED_STAFF:
            any_flagged = True
            print(f"  {u['full_name']} ({u['email']}) — {'Active' if u['active'] else 'Deactivated'}, role={u['role']}")
    if not any_flagged:
        print("  None.")

    print(f"\nTotal users: {len(users)}")


if __name__ == "__main__":
    main()