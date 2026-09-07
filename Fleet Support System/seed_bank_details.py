"""
Bulk-seeds driver_bank_details from a Driver Payments export (the kind
Charlotte shared) rather than entering rows by hand one at a time.

That CSV includes Amazon Id (== our driver_code), Account Number, and
Sort Code per driver. This script matches each row to an existing
driver in the drivers table and inserts bank details for any driver
that doesn't already have them — existing bank_details rows are left
untouched (use --overwrite to update them instead).

Usage:
    # Preview only, writes nothing
    python seed_bank_details.py --file data/Driver_payments_file.csv

    # Actually insert
    python seed_bank_details.py --file data/Driver_payments_file.csv --commit
"""
import argparse
import logging

import pandas as pd

from database import supabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_bank_details(filepath: str) -> pd.DataFrame:
    # dtype=str is critical here: without it, pandas infers Account Number
    # as numeric and silently drops leading zeros (e.g. "03234223" becomes
    # 3234223) — a different, wrong account number.
    df = pd.read_csv(
        filepath,
        dtype={"Amazon Id": str, "Account Number": str, "Sort Code": str},
    )
    required = {"Amazon Id", "Account Number", "Sort Code"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"File is missing expected column(s): {missing}")

    df["Account Number"] = df["Account Number"].str.strip()
    df["Sort Code"] = df["Sort Code"].str.strip()

    # A driver can appear multiple times (one row per invoice) — bank
    # details should be the same each time, so just take the first row.
    return df[["Amazon Id", "Account Number", "Sort Code"]].drop_duplicates(
        subset=["Amazon Id"]
    )


def main():
    parser = argparse.ArgumentParser(description="Bulk-seed driver bank details.")
    parser.add_argument("--file", required=True, help="Path to the Driver Payments CSV")
    parser.add_argument("--commit", action="store_true", help="Actually write to the database")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Update bank details even for drivers that already have a record",
    )
    args = parser.parse_args()

    bank_rows = load_bank_details(args.file)

    all_drivers = supabase.table("drivers").select("id, driver_code").execute().data
    driver_lookup = {d["driver_code"]: d["id"] for d in all_drivers}

    existing = supabase.table("driver_bank_details").select("driver_id").execute().data
    existing_ids = {e["driver_id"] for e in existing}

    to_insert, to_update, unmatched = [], [], []

    for _, row in bank_rows.iterrows():
        code = row["Amazon Id"]
        driver_id = driver_lookup.get(code)
        if not driver_id:
            unmatched.append(code)
            continue

        acc = row["Account Number"]
        sort = row["Sort Code"]
        if not acc.isdigit() or len(acc) != 8:
            log.warning(
                "%s: account number '%s' is not 8 digits — double-check this before relying on it.",
                code, acc,
            )
        if not sort.replace("-", "").isdigit() or len(sort.replace("-", "")) != 6:
            log.warning(
                "%s: sort code '%s' doesn't look like a standard 6-digit sort code — double-check it.",
                code, sort,
            )

        record = {
            "driver_id": driver_id,
            "account_number": acc,
            "sort_code": sort,
        }
        if driver_id in existing_ids:
            to_update.append(record)
        else:
            to_insert.append(record)

    print(f"\n{len(to_insert)} driver(s) to add, {len(to_update)} already on file, "
          f"{len(unmatched)} not found in drivers table")

    if unmatched:
        print("Not found in drivers table (add them first if relevant):")
        for code in unmatched:
            print(f"  - {code}")

    if not args.commit:
        print("\nDry run only — nothing was written. Re-run with --commit to save.")
        return

    for record in to_insert:
        supabase.table("driver_bank_details").insert(record).execute()
    log.info("Inserted bank details for %d driver(s).", len(to_insert))

    if args.overwrite and to_update:
        for record in to_update:
            supabase.table("driver_bank_details").update(
                {"account_number": record["account_number"], "sort_code": record["sort_code"]}
            ).eq("driver_id", record["driver_id"]).execute()
        log.info("Updated bank details for %d existing driver(s).", len(to_update))
    elif to_update:
        print(f"Skipped {len(to_update)} driver(s) who already had bank details "
              f"(use --overwrite to update them instead).")


if __name__ == "__main__":
    main()
