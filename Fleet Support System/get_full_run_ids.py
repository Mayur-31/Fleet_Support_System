"""
Read-only. Prints the full UUID for each payment run, so you have
exact IDs to use with delete_payment_runs.py (which requires full
UUIDs, not short prefixes, for a destructive operation).

Usage:
    python get_full_run_ids.py
"""
from database import supabase


def main():
    runs = (
        supabase.table("payment_runs")
        .select("id, week_number, status, total_amount")
        .order("week_number")
        .execute()
        .data
    )

    print(f"{'Full ID':<38} {'Week':<6} {'Status':<11} {'Total':>10}")
    print("-" * 70)
    for r in runs:
        print(f"{r['id']:<38} {r['week_number']:<6} {r['status']:<11} {(r['total_amount'] or 0):>10.2f}")


if __name__ == "__main__":
    main()