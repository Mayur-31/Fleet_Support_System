import argparse
import logging
import os
import sys
import re
from datetime import date as date_type
from typing import Optional
import pandas as pd
 
from database import supabase
 
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
 
REQUIRED_COLUMNS = {"code", "AV"}
 
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[._\s-](\d{1,2})[._\s-](\d{4})(?!\d)")


def parse_expense_date(filename: str) -> Optional[str]:
    """
    Extract the expense date from a filename like 'EXPENSES 15.09.2026.xlsx'.
    Returns an ISO string ('YYYY-MM-DD') or None.
    """
    if not filename:
        return None
    match = _DATE_RE.search(filename)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date_type(year, month, day).isoformat()
    except ValueError:
        return None

def load_and_aggregate(filepath: str) -> pd.DataFrame:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(filepath, dtype={"code": str})
    elif ext == ".xlsx":
        df = pd.read_excel(filepath, dtype={"code": str})
    else:
        raise ValueError(f"Unsupported file type '{ext}' — expected .xlsx or .csv")
 
    missing_cols = REQUIRED_COLUMNS - set(df.columns)
    if missing_cols:
        raise ValueError(
            f"Expenses file is missing expected column(s): {missing_cols}. "
            f"Found columns: {list(df.columns)}"
        )
 
    # The code column is only filled on the first row of each job block;
    # forward-fill so the second (expense add-on) row is attributed correctly.
    df["code_ffill"] = df["code"].ffill()
 
    if df["code_ffill"].isna().any():
        orphan_rows = df[df["code_ffill"].isna()]
        log.warning(
            "Found %d row(s) with no driver code even after forward-fill "
            "(likely blank rows at the top of the file) — these will be skipped.",
            len(orphan_rows),
        )
        df = df.dropna(subset=["code_ffill"])
 
    weekly = df.groupby("code_ffill")["AV"].sum().reset_index()
    weekly.columns = ["driver_code", "total_amount"]
    return weekly
 
 
def match_drivers(weekly: pd.DataFrame):
    """Returns (matched_rows, unmatched_codes)."""
    all_drivers = supabase.table("drivers").select("id, driver_code, name").execute().data
    driver_lookup = {d["driver_code"]: d for d in all_drivers}
 
    matched = []
    unmatched = []
 
    for _, row in weekly.iterrows():
        code = row["driver_code"]
        driver = driver_lookup.get(code)
        if driver:
            matched.append(
                {
                    "driver_id": driver["id"],
                    "driver_code": code,
                    "name": driver["name"],
                    "amount": round(float(row["total_amount"]), 2),
                }
            )
        else:
            unmatched.append(code)
 
    return matched, unmatched
 
 
def commit_to_database(
    week_number: int,
    matched: list,
    expense_file_date: Optional[str] = None,
) -> str:
    """Creates the payment_run and job_expenses rows. Rolls back on failure."""
    run_id = None
    try:
        run = supabase.table("payment_runs").insert(
            {
                "week_number": week_number,
                "status": "draft",
                "expense_file_date": expense_file_date,
            }
        ).execute()
        run_id = run.data[0]["id"]
        log.info("Created payment_run %s for week %s", run_id, week_number)
 
        for row in matched:
            supabase.table("job_expenses").insert(
                {
                    "payment_run_id": run_id,
                    "driver_id": row["driver_id"],
                    "amount": row["amount"],
                }
            ).execute()
 
        total = sum(r["amount"] for r in matched)
        supabase.table("payment_runs").update({"total_amount": total}).eq(
            "id", run_id
        ).execute()
 
        log.info("Inserted %d job_expenses rows. Total: %.2f", len(matched), total)
        return run_id
 
    except Exception:
        log.error("Insert failed partway through — rolling back.", exc_info=True)
        if run_id:
            supabase.table("job_expenses").delete().eq("payment_run_id", run_id).execute()
            supabase.table("payment_runs").delete().eq("id", run_id).execute()
            log.info("Rolled back payment_run %s and its job_expenses.", run_id)
        raise
 
 
def main():
    parser = argparse.ArgumentParser(description="Import a weekly Expenses file.")
    parser.add_argument("--file", required=True, help="Path to the Expenses .xlsx file")
    parser.add_argument("--week", required=True, type=int, help="Week number for this payment run")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Without this flag, only a preview is shown.",
    )
    args = parser.parse_args()
 
    weekly = load_and_aggregate(args.file)
    matched, unmatched = match_drivers(weekly)
 
    print(f"\nWeek {args.week} — parsed {len(weekly)} driver totals from {args.file}\n")
    print(f"{'Driver Code':<12} {'Name':<25} {'Amount':>10}")
    print("-" * 50)
    for row in matched:
        print(f"{row['driver_code']:<12} {row['name']:<25} {row['amount']:>10.2f}")
 
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} driver code(s) not found in the drivers table:")
        for code in unmatched:
            print(f"  - {code}")
        print("These rows will be skipped. Add these drivers first if they should be paid.")
 
    if not matched:
        log.error("No matched drivers — nothing to import.")
        sys.exit(1)
 
    if not args.commit:
        print("\nDry run only — nothing was written. Re-run with --commit to save this to the database.")
        return
 
    expense_file_date = parse_expense_date(args.file)
    if not expense_file_date:
        log.warning(
            "No date found in filename '%s' — payment run will be saved with NULL expense_file_date.",
            args.file,
        )
    run_id = commit_to_database(args.week, matched, expense_file_date)
    print(f"\nDone. payment_run id: {run_id}")
 
 
if __name__ == "__main__":
    main()