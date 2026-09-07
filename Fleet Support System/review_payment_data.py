"""
Read-only review of payment_runs and job_expenses. Deletes nothing —
this is purely a report to inform a cleanup decision, per the
explicit "show me before removing anything" requirement.

Usage:
    python review_payment_data.py
"""
from collections import defaultdict

from database import supabase


def main():
    runs = (
        supabase.table("payment_runs")
        .select("id, week_number, status, total_amount, created_at")
        .order("week_number")
        .execute()
        .data
    )

    if not runs:
        print("No payment runs found.")
        return

    # Count actual job_expenses per run, and cross-check the sum against
    # the stored total_amount — a mismatch would indicate a partially
    # failed insert that's worth investigating before deleting anything.
    all_expenses = supabase.table("job_expenses").select("payment_run_id, amount").execute().data
    expenses_by_run = defaultdict(list)
    for e in all_expenses:
        expenses_by_run[e["payment_run_id"]].append(e["amount"])

    print(f"{'Run ID':<10} {'Week':<6} {'Status':<11} {'Stored Total':>13} {'Rows':>6} {'Computed Total':>15}  Created")
    print("-" * 95)

    by_week = defaultdict(list)
    flags = []

    for run in runs:
        run_id_short = run["id"][:8]
        amounts = expenses_by_run.get(run["id"], [])
        computed_total = sum(amounts)
        row_count = len(amounts)

        print(
            f"{run_id_short:<10} {run['week_number']:<6} {run['status']:<11} "
            f"{(run['total_amount'] or 0):>13.2f} {row_count:>6} {computed_total:>15.2f}  {run['created_at']}"
        )

        by_week[run["week_number"]].append(run)

        if row_count == 0:
            flags.append(f"  - Run {run_id_short} (week {run['week_number']}): 0 job_expenses rows — likely a failed/empty insert.")
        if abs((run["total_amount"] or 0) - computed_total) > 0.01:
            flags.append(
                f"  - Run {run_id_short} (week {run['week_number']}): stored total {run['total_amount']} "
                f"doesn't match computed total {computed_total:.2f} — worth investigating."
            )

    print("\n--- Duplicate weeks (more than one run for the same week) ---")
    any_dupes = False
    for week, week_runs in sorted(by_week.items()):
        if len(week_runs) > 1:
            any_dupes = True
            ids = ", ".join(r["id"][:8] for r in week_runs)
            print(f"  Week {week}: {len(week_runs)} runs — {ids}")
    if not any_dupes:
        print("  None.")

    if flags:
        print("\n--- Other flags ---")
        for f in flags:
            print(f)

    print(f"\nTotal payment runs: {len(runs)}")


if __name__ == "__main__":
    main()