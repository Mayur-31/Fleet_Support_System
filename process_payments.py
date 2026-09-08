"""
Convenience wrapper for the full weekly workflow: import the Expenses
file, then generate the Faster Payments CSV — in one command.

For finer control (e.g. reviewing a dry run before committing), use
import_expenses.py and generate_bank_csv.py directly instead.

Usage:
    python process_payments.py --file data/EXPENSES_20_07_2026.xlsx --week 30 --commit
"""
import argparse

import import_expenses
import generate_bank_csv


def main():
    parser = argparse.ArgumentParser(description="Import expenses and export the bank CSV in one step.")
    parser.add_argument("--file", required=True, help="Path to the Expenses .xlsx file")
    parser.add_argument("--week", required=True, type=int, help="Week number for this payment run")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database and generate the CSV. Without this, only a preview is shown.",
    )
    args = parser.parse_args()

    weekly = import_expenses.load_and_aggregate(args.file)
    matched, unmatched = import_expenses.match_drivers(weekly)

    print(f"\nWeek {args.week} — parsed {len(weekly)} driver totals from {args.file}\n")
    print(f"{'Driver Code':<12} {'Name':<25} {'Amount':>10}")
    print("-" * 50)
    for row in matched:
        print(f"{row['driver_code']:<12} {row['name']:<25} {row['amount']:>10.2f}")

    if unmatched:
        print(f"\nWARNING: {len(unmatched)} driver code(s) not found in the drivers table:")
        for code in unmatched:
            print(f"  - {code}")

    if not matched:
        print("\nNo matched drivers — stopping.")
        return

    if not args.commit:
        print("\nDry run only — nothing was written. Re-run with --commit to import and export for real.")
        return

    run_id = import_expenses.commit_to_database(args.week, matched)
    print(f"\nImported. payment_run id: {run_id}")

    print("\nGenerating Faster Payments CSV...")
    run = generate_bank_csv.fetch_payment_run(run_id)
    rows = generate_bank_csv.fetch_rows(run_id)
    csv_rows, skipped = generate_bank_csv.build_csv_rows(rows, run["week_number"])

    if not csv_rows:
        print("No drivers had bank details on file — nothing to export.")
        return

    import csv as csv_module
    output_path = f"output/FasterPayments_Week{run['week_number']}.csv"
    with open(output_path, "w", newline="") as f:
        csv_module.writer(f).writerows(csv_rows)

    print(f"Wrote {len(csv_rows)} row(s) to {output_path}")
    if skipped:
        print(f"Skipped {len(skipped)} driver(s) with no bank details: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
