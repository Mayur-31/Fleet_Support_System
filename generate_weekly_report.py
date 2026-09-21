"""
Generates the Weekly Report CSV for a given payment week.

Format matches Charlotte's export (1).csv exactly - column order, quoting,
BOM, and value formats.
"""
import csv
import logging
import os
from datetime import datetime

from database import supabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Column order matches Charlotte's export (1).csv exactly.
HEADER = [
    "#", "Actions", "Driver", "Invoice Type", "Ref", "Invoice Date",
    "Week Number", "Total Due", "Paid On", "Accepted By DriverOn",
    "Payment Gross", "Payment Vat", "Payment Net",
    "Charge Gross", "Charge Vat", "Charge Net",
    "Published On", "Final Invoice", "Payment Portal Company",
    "Depot", "Amazon Id", "Email", "Account Number", "Sort Code",
    "Is Active", "Van Inspection Status", "Payment Requested On",
]

# Constants observed in Charlotte's export - all rows share these.
DEPOT = "MOVEMENTS"
PAYMENT_PORTAL_COMPANY = ""    # blank in every row of the old export
EMAIL_DOMAIN = "@UKXD.CO.UK"


def _fetch_runs_for_week(week_number):
    resp = (
        supabase.table("payment_runs")
        .select("id, week_number, expense_file_date")
        .eq("week_number", week_number)
        .order("expense_file_date")
        .execute()
    )
    return resp.data or []


def _fetch_expenses_for_runs(run_ids):
    resp = (
        supabase.table("job_expenses")
        .select("id, amount, payment_run_id, driver_id, status")
        .in_("payment_run_id", run_ids)
        .order("id")
        .execute()
    )
    return resp.data or []


def _fetch_drivers(driver_ids):
    if not driver_ids:
        return {}
    resp = (
        supabase.table("drivers")
        .select("id, name, driver_code")
        .in_("id", driver_ids)
        .execute()
    )
    return {r["id"]: r for r in (resp.data or [])}


def _fetch_bank_details(driver_ids):
    if not driver_ids:
        return {}
    resp = (
        supabase.table("driver_bank_details")
        .select("driver_id, sort_code, account_number")
        .in_("driver_id", driver_ids)
        .execute()
    )
    result = {}
    for row in (resp.data or []):
        result.setdefault(row["driver_id"], row)
    return result


def _fetch_paid_dates(expense_ids):
    if not expense_ids:
        return {}
    resp = (
        supabase.table("bank_report_lines")
        .select("matched_job_expense_id, bank_execution_date")
        .in_("matched_job_expense_id", expense_ids)
        .execute()
    )
    result = {}
    for row in (resp.data or []):
        eid = row["matched_job_expense_id"]
        if not eid:
            continue
        bed = row.get("bank_execution_date")
        existing = result.get(eid)
        if existing is None or (bed and bed < existing):
            result[eid] = bed
    return result


def _iso_datetime(d):
    """Format a date as 'YYYY-MM-DDTHH:MM:SSZ' (matches old export)."""
    if not d:
        return ""
    s = str(d)
    if "T" in s:
        return s
    return f"{s}T00:00:00Z"


def _amount_str(value):
    """'95' for integers, '95.50' for decimals - matching old export format."""
    if value is None:
        return "0"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    if n == int(n):
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def generate_weekly_report(week_number: int, output_dir: str = "output") -> dict:
    runs = _fetch_runs_for_week(week_number)
    if not runs:
        raise ValueError(f"No payment runs found for week {week_number}.")

    run_map = {r["id"]: r for r in runs}
    run_ids = list(run_map.keys())

    expenses = _fetch_expenses_for_runs(run_ids)
    if not expenses:
        raise ValueError(f"No expenses found for week {week_number}.")

    driver_ids = list({e["driver_id"] for e in expenses if e.get("driver_id")})
    drivers = _fetch_drivers(driver_ids)
    bank_details = _fetch_bank_details(driver_ids)

    paid_expense_ids = [e["id"] for e in expenses if e.get("status") == "paid"]
    paid_on_map = _fetch_paid_dates(paid_expense_ids)

    rows = []
    missing_driver = []

    for exp in expenses:
        driver_id = exp.get("driver_id")
        driver = drivers.get(driver_id)
        if not driver:
            missing_driver.append(exp["id"])
            continue

        bank = bank_details.get(driver_id, {})
        run = run_map.get(exp["payment_run_id"], {})

        amount_str = _amount_str(exp.get("amount"))
        driver_code = driver["driver_code"]

        # Ref: Charlotte's old file uses "invoice#-week-year". We don't have
        # invoice numbers, so we use the same format the bank CSV uses
        # ("WK{week}-{code}"). Charlotte can tell us if she needs the old format.
        ref = f"WK{week_number}-{driver_code}"

        invoice_date = _iso_datetime(run.get("expense_file_date"))
        paid_on = _iso_datetime(paid_on_map.get(exp["id"]))

        # Email: derived from driver code + standard domain.
        # Charlotte's file has "{code}@UKXD.CO.UK" for every row.
        email = f"{driver_code}{EMAIL_DOMAIN}"

        rows.append([
            "",                              # #
            "",                              # Actions (no invoice number)
            driver["name"],                  # Driver
            "Invoice",                       # Invoice Type (constant in old file)
            ref,                             # Ref
            invoice_date,                    # Invoice Date
            week_number,                     # Week Number
            amount_str,                      # Total Due
            paid_on,                         # Paid On
            "",                              # Accepted By DriverOn (not tracked)
            amount_str,                      # Payment Gross
            "0",                             # Payment Vat
            amount_str,                      # Payment Net
            "0",                             # Charge Gross
            "0",                             # Charge Vat
            "0",                             # Charge Net
            "",                              # Published On (not tracked)
            "false",                         # Final Invoice (constant in old file)
            PAYMENT_PORTAL_COMPANY,          # Payment Portal Company (blank in old file)
            DEPOT,                           # Depot (constant "MOVEMENTS")
            driver_code,                     # Amazon Id (driver code)
            email,                           # Email
            bank.get("account_number", ""),  # Account Number
            bank.get("sort_code", ""),       # Sort Code
            "Yes",                           # Is Active
            "",                              # Van Inspection Status
            "",                              # Payment Requested On
        ])

    if missing_driver:
        raise ValueError(
            f"{len(missing_driver)} expense(s) reference missing drivers: "
            f"{missing_driver[:5]}{'...' if len(missing_driver) > 5 else ''}"
        )

    os.makedirs(output_dir, exist_ok=True)
    filename = f"WeeklyReport_Week{week_number}.csv"
    path = os.path.join(output_dir, filename)

    # utf-8-sig writes the BOM; QUOTE_ALL matches Charlotte's file exactly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADER)
        writer.writerows(rows)

    total = sum(float(e.get("amount") or 0) for e in expenses)
    log.info("Weekly report written: %s (%d rows, total %.2f)", path, len(rows), total)

    return {
        "filename": filename,
        "path": path,
        "row_count": len(rows),
        "total_amount": round(total, 2),
        "week_number": week_number,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--week", required=True, type=int)
    args = p.parse_args()
    r = generate_weekly_report(args.week)
    print(f"\n✅ Wrote {r['row_count']} rows to {r['path']}")
    print(f"   Week {r['week_number']}, total £{r['total_amount']:.2f}")