"""
Generate a reconciliation summary CSV for a bank report.

One row per bank line, with the match status and the reason. Mirrors the
weekly report conventions: utf-8-sig BOM so Excel opens it cleanly,
QUOTE_ALL so mixed content doesn't break parsing.

Usage:
    from generate_bank_report_summary import generate_bank_report_summary
    result = generate_bank_report_summary(report_id="<uuid>")
"""
import csv
import logging
import os
from datetime import datetime

from database import supabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

HEADER = [
    "Bank Report File",
    "Week",
    "Processed",
    "Bank Execution Date",
    "Beneficiary Name",
    "Driver Reference",
    "Driver Name",
    "Payment Ref",
    "Bank Sort",
    "Bank Account",
    "Amount",
    "Match Status",
    "Matched Expense ID",
    "Reason",
]


def _fetch_report(report_id):
    resp = (
        supabase.table("bank_reports")
        .select("*")
        .eq("id", report_id)
        .execute()
    )
    if not resp.data:
        return None
    return resp.data[0]


def _fetch_lines(report_id):
    resp = (
        supabase.table("bank_report_lines")
        .select(
            "bank_execution_date, beneficiary_name, driver_reference, "
            "driver_name, payment_ref, bank_sort, bank_account, "
            "beneficiary_amount, match_status, matched_job_expense_id, reason"
        )
        .eq("bank_report_id", report_id)
        .order("row_number")
        .execute()
    )
    return resp.data or []


def _fmt_dt(v):
    if not v:
        return ""
    return str(v)


def _fmt_amount(v):
    if v is None:
        return ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if n == int(n):
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def generate_bank_report_summary(report_id: str, output_dir: str = "output") -> dict:
    """
    Build the reconciliation summary CSV for a bank report.

    Returns:
        {
          "filename": str,
          "path": str,
          "row_count": int,
          "week_number": int,
          "summary": {matched, unmatched, ambiguous, amount_mismatch, not_applicable},
        }
    Raises ValueError if the report isn't found.
    """
    report = _fetch_report(report_id)
    if not report:
        raise ValueError(f"Bank report {report_id} not found.")

    lines = _fetch_lines(report_id)
    if not lines:
        raise ValueError(f"Bank report {report_id} has no lines to report.")

    filename_src = report.get("filename") or "bank_report"
    week = report.get("week_number") or 0
    processed = _fmt_dt(report.get("processed_at"))

    rows = []
    counts = {
        "matched": 0, "unmatched": 0, "ambiguous": 0,
        "amount_mismatch": 0, "not_applicable": 0,
    }

    for line in lines:
        status = line.get("match_status") or ""
        if status in counts:
            counts[status] += 1

        rows.append([
            filename_src,
            week,
            processed,
            _fmt_dt(line.get("bank_execution_date")),
            line.get("beneficiary_name") or "",
            line.get("driver_reference") or "",
            line.get("driver_name") or "",
            line.get("payment_ref") or "",
            line.get("bank_sort") or "",
            line.get("bank_account") or "",
            _fmt_amount(line.get("beneficiary_amount")),
            status,
            line.get("matched_job_expense_id") or "",
            line.get("reason") or "",
        ])

    os.makedirs(output_dir, exist_ok=True)
    short_id = str(report_id).split("-")[0]
    filename = f"BankReport_Week{week}_{short_id}.csv"
    path = os.path.join(output_dir, filename)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADER)
        writer.writerows(rows)

    log.info(
        "Bank report summary written: %s (%d lines, matched=%d, unmatched=%d)",
        path, len(rows), counts["matched"], counts["unmatched"],
    )

    return {
        "filename": filename,
        "path": path,
        "row_count": len(rows),
        "week_number": week,
        "summary": counts,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--report-id", required=True)
    args = p.parse_args()
    r = generate_bank_report_summary(args.report_id)
    print(f"\n✅ Wrote {r['row_count']} rows to {r['path']}")
    print(f"   Week {r['week_number']} — {r['summary']}")