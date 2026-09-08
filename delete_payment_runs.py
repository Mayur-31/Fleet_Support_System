"""
Deletes specific payment_runs (and their job_expenses) by exact full
UUID — never by week number, never a prefix match, never "all of
them." You must list complete UUIDs (get these from
get_full_run_ids.py), and pass --confirm to actually delete; without
--confirm it only previews.

Run backup_before_cleanup.py first.

Usage:
    # Preview only
    python delete_payment_runs.py --run-ids <uuid1>,<uuid2>,...

    # Actually delete
    python delete_payment_runs.py --run-ids <uuid1>,<uuid2>,... --confirm
"""
import argparse

from database import supabase


def main():
    parser = argparse.ArgumentParser(description="Delete specific payment runs by exact full UUID.")
    parser.add_argument(
        "--run-ids",
        required=True,
        help="Comma-separated FULL payment_run UUIDs to delete (no short prefixes).",
    )
    parser.add_argument("--confirm", action="store_true", help="Actually delete. Without this, preview only.")
    args = parser.parse_args()

    requested_ids = [x.strip() for x in args.run_ids.split(",") if x.strip()]

    # Exact match only -- every requested ID must be a real, full UUID
    # that exists. Anything that doesn't match exactly is reported and
    # skipped rather than silently ignored or fuzzy-matched.
    all_runs = {r["id"]: r for r in supabase.table("payment_runs").select("*").execute().data}

    found = [all_runs[i] for i in requested_ids if i in all_runs]
    not_found = [i for i in requested_ids if i not in all_runs]

    if not_found:
        print("WARNING: the following ID(s) don't exactly match any payment_run and will be skipped:")
        for i in not_found:
            print(f"  - {i}")
        print()

    if not found:
        print("No matching payment runs found. Nothing to do.")
        return

    print(f"{'Full ID':<38} {'Week':<6} {'Status':<11} {'Total':>10}")
    print("-" * 70)
    for r in found:
        print(f"{r['id']:<38} {r['week_number']:<6} {r['status']:<11} {(r['total_amount'] or 0):>10.2f}")

    if not args.confirm:
        print(f"\nPreview only -- {len(found)} run(s) would be deleted, along with their job_expenses.")
        print("Re-run with --confirm to actually delete. Make sure you've run backup_before_cleanup.py first.")
        return

    print()
    for r in found:
        run_id = r["id"]

        supabase.table("job_expenses").delete().eq("payment_run_id", run_id).execute()
        supabase.table("payment_runs").delete().eq("id", run_id).execute()

        # Verify the deletion actually completed, rather than assuming
        # the API call succeeding meant the rows are really gone.
        remaining_expenses = (
            supabase.table("job_expenses").select("id").eq("payment_run_id", run_id).execute().data
        )
        remaining_run = supabase.table("payment_runs").select("id").eq("id", run_id).execute().data

        if remaining_expenses or remaining_run:
            print(
                f"WARNING: run {run_id} (week {r['week_number']}) did NOT fully delete -- "
                f"{len(remaining_expenses)} job_expenses and "
                f"{'1' if remaining_run else '0'} payment_runs row still remain. "
                f"Re-run this script with the same ID to retry just this one."
            )
        else:
            print(f"Deleted and verified: run {run_id} (week {r['week_number']}).")

    print(f"\nDone. Processed {len(found)} payment run(s) -- see above for any warnings.")


if __name__ == "__main__":
    main()