"""
Generates the Faster Payments bank-upload CSV for a given payment run.

Usage:
    python generate_bank_csv.py --run-id <payment_run_id>

Output format (verified with Week 33 sample):
    sort_code, driver_name, account_number, amount, reference, 99
"""
import argparse
import csv
import logging
import os
import sys

from database import supabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def fetch_payment_run(run_id: str) -> dict:
    """Fetch the payment run record."""
    result = supabase.table("payment_runs").select("*").eq("id", run_id).execute()
    if not result.data:
        raise ValueError(f"No payment_run found with id {run_id}")
    return result.data[0]


def fetch_expenses(run_id: str) -> list:
    """
    Fetch all expenses for a payment run, ordered by ID for deterministic output.
    Returns list of {id, driver_id, amount}.
    """
    result = (
        supabase.table("job_expenses")
        .select("id, driver_id, amount")
        .eq("payment_run_id", run_id)
        .order("id")          # deterministic ordering
        .execute()
    )
    return result.data or []


def fetch_drivers_batch(driver_ids: list) -> dict:
    """
    Fetch driver names and codes for a list of driver_ids.
    Returns dict mapping driver_id -> {name, driver_code}.
    """
    if not driver_ids:
        return {}

    result = (
        supabase.table("drivers")
        .select("id, name, driver_code")
        .in_("id", driver_ids)
        .execute()
    )
    return {row["id"]: row for row in (result.data or [])}


def fetch_bank_details_batch(driver_ids: list) -> dict:
    """
    Fetch bank details for a list of driver_ids.
    Returns dict mapping driver_id -> {sort_code, account_number}.
    Raises ValueError if any driver has duplicate bank records.
    """
    if not driver_ids:
        return {}

    result = (
        supabase.table("driver_bank_details")
        .select("driver_id, sort_code, account_number")   # account_name removed
        .in_("driver_id", driver_ids)
        .execute()
    )

    # Check for duplicate bank records per driver
    bank_map = {}
    duplicates = []
    for row in (result.data or []):
        d_id = row["driver_id"]
        if d_id in bank_map:
            duplicates.append(d_id)
        else:
            bank_map[d_id] = row

    if duplicates:
        raise ValueError(
            f"Duplicate bank records found for driver(s): {', '.join(duplicates)}. "
            "Please ensure each driver has exactly one bank record."
        )

    return bank_map


def generate_csv(run_id: str, output_dir: str = "output") -> dict:
    """
    Generate the Faster Payments CSV for a payment run.
    Returns a dict with filename, path, row_count, skipped, and week_number.
    Raises ValueError if any expense lacks a driver or valid bank details.
    """
    run = fetch_payment_run(run_id)
    week_number = run["week_number"]

    expenses = fetch_expenses(run_id)
    if not expenses:
        raise ValueError("No job_expenses rows found for this payment run.")

    # Collect unique driver IDs
    driver_ids = list({exp["driver_id"] for exp in expenses if exp.get("driver_id")})

    # Batch fetch drivers and bank details
    drivers = fetch_drivers_batch(driver_ids)
    try:
        bank_details = fetch_bank_details_batch(driver_ids)
    except ValueError as e:
        raise ValueError(f"Bank details issue: {e}")

    csv_rows = []
    missing = []

    for exp in expenses:
        driver_id = exp.get("driver_id")
        if not driver_id:
            missing.append(f"Expense {exp['id']} has no driver_id")
            continue

        driver = drivers.get(driver_id)
        if not driver:
            missing.append(f"Expense {exp['id']} references missing driver {driver_id}")
            continue

        bank = bank_details.get(driver_id)
        if not bank:
            missing.append(f"Driver {driver['name']} ({driver_id}) has no bank details")
            continue

        # Validate required bank fields are present (sort_code and account_number)
        if not bank.get("sort_code") or not bank.get("account_number"):
            missing.append(
                f"Driver {driver['name']} ({driver_id}) has incomplete bank details "
                "(missing sort_code or account_number)"
            )
            continue

        reference = f"WK{week_number}-{driver['driver_code']}"
        csv_rows.append(
            [
                bank["sort_code"],
                driver["name"],                     # <-- Use driver name instead of account_name
                bank["account_number"],
                f"{exp['amount']:.2f}",
                reference,
                99,
            ]
        )

    if missing:
        raise ValueError(
            f"Cannot generate CSV: {len(missing)} issue(s) found:\n" + "\n".join(missing)
        )

    if not csv_rows:
        raise ValueError("No valid CSV rows could be generated – check expense data.")

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    csv_filename = f"FasterPayments_Week{week_number}_{run_id[:8]}.csv"
    output_path = os.path.join(output_dir, csv_filename)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(csv_rows)

    return {
        "filename": csv_filename,
        "path": output_path,
        "row_count": len(csv_rows),
        "skipped": missing,   # empty if success
        "week_number": week_number,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate the Faster Payments CSV for a payment run."
    )
    parser.add_argument("--run-id", required=True, help="payment_runs.id to export")
    args = parser.parse_args()

    try:
        result = generate_csv(args.run_id)
        print(f"\n✅ Wrote {result['row_count']} row(s) to {result['path']}")
    except ValueError as e:
        log.error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()