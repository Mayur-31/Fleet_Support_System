"""
Generates the Faster Payments bank-upload CSV for a given payment run.

Usage:
    python generate_bank_csv.py --run-id <payment_run_id>

Output format matches the sample Charlotte shared:
    sort_code, account_number, driver_name, amount, reference, 99

Known open question (confirm with Charlotte/the bank before relying on
this for a real payment): the exact reference number scheme. The sample
looked like an incrementing invoice number plus week/year
(e.g. "1420891-29-2026"). This script generates a placeholder reference
of "WK{week}-{driver_code}" until the real numbering rule is confirmed —
do not treat the reference column as production-ready yet.
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
    result = supabase.table("payment_runs").select("*").eq("id", run_id).execute()
    if not result.data:
        raise ValueError(f"No payment_run found with id {run_id}")
    return result.data[0]


def fetch_rows(run_id: str) -> list:
    result = (
        supabase.table("job_expenses")
        .select(
            """
            amount,
            drivers (
                name,
                driver_code,
                driver_bank_details (
                    account_number,
                    sort_code
                )
            )
            """
        )
        .eq("payment_run_id", run_id)
        .execute()
    )
    return result.data


def build_csv_rows(rows: list, week_number: int):
    """Returns (csv_rows, skipped_driver_names)."""
    csv_rows = []
    skipped = []

    for r in rows:
        driver = r["drivers"]
        bank = driver.get("driver_bank_details")

        if not bank:
            skipped.append(driver["name"])
            continue

        reference = f"WK{week_number}-{driver['driver_code']}"
        csv_rows.append(
            [
                bank["sort_code"],
                driver["name"],
                bank["account_number"],
                f"{r['amount']:.2f}",
                reference,
                99,
            ]
        )

    return csv_rows, skipped

def generate_csv(run_id: str, output_dir: str = "output"):
    """
    Generate the Faster Payments CSV for a payment run.
    Returns a dict with filename, path, row_count, skipped, and week_number.
    Raises ValueError on failure.
    """
    run = fetch_payment_run(run_id)
    week_number = run["week_number"]

    rows = fetch_rows(run_id)
    if not rows:
        raise ValueError("No job_expenses rows found for this payment run.")

    csv_rows, skipped = build_csv_rows(rows, week_number)
    if not csv_rows:
        raise ValueError("No drivers had bank details — nothing to export.")

    os.makedirs(output_dir, exist_ok=True)

    csv_filename = f"FasterPayments_Week{week_number}_{run_id[:8]}.csv"
    output_path = os.path.join(output_dir, csv_filename)

    # UTF-8 encoding ensures safe handling of names with special characters
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(csv_rows)

    return {
        "filename": csv_filename,
        "path": output_path,
        "row_count": len(csv_rows),
        "skipped": skipped,
        "week_number": week_number,
    }

def main():
    parser = argparse.ArgumentParser(description="Generate the Faster Payments CSV for a payment run.")
    parser.add_argument("--run-id", required=True, help="payment_runs.id to export")
    args = parser.parse_args()

    try:
        result = generate_csv(args.run_id)
        print(f"\nWrote {result['row_count']} row(s) to {result['path']}")
        if result['skipped']:
            print(f"Skipped {len(result['skipped'])} driver(s) with no bank details: {', '.join(result['skipped'])}")
    except ValueError as e:
        log.error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
