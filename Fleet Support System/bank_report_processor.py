"""
bank_report_processor.py -- Bank report reconciliation.

Usage:
    python bank_report_processor.py <file> --uploaded-by <uuid>
    python bank_report_processor.py <file> --uploaded-by <uuid> --commit

Writes directly to bank_reports / bank_report_lines / job_expenses via
ordered Supabase table calls (no RPC -- see project history for why).
match_status is always one of exactly five values, matching the live
CHECK constraint: matched, unmatched, ambiguous, amount_mismatch,
not_applicable.

Lines with a blank bank_line_id are counted but never written to
bank_report_lines -- there's nothing to key idempotency on, and the
unique constraint means a second blank line in the same file would
otherwise collide. Duplicate bank_line_id values within one file are
checked before any write happens -- the whole commit is refused rather
than partially applied, since bank_line_id is the idempotency key and
a same-file collision means something is wrong with the export itself,
not something to paper over line by line.
"""
import os
import sys
import json
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from collections import Counter

from dotenv import load_dotenv
load_dotenv()

from database import supabase
from bank_report_parser import parse_bank_report

AMOUNT_TOLERANCE = Decimal('0.01')


def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def normalize_amount(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if isinstance(value, str):
        cleaned = value.strip().replace('£', '').replace('$', '').replace(',', '').replace(' ', '')
        if cleaned == '':
            return None
        try:
            return Decimal(cleaned).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except Exception:
            return None
    return None


def get_week_from_lines(parsed):
    """Only Matched Paid Driver Lines determine the week -- Unmatched
    Lines has no WeekNumber column at all in the real file structure."""
    weeks_seen = set()
    for line in parsed.get('matched_paid_lines', []):
        week = line.get('WeekNumber')
        if week is not None:
            weeks_seen.add(int(week))
    if not weeks_seen:
        raise ValueError("No WeekNumber found in Matched Paid Driver Lines.")
    if len(weeks_seen) > 1:
        raise ValueError(f"File contains multiple week numbers: {sorted(weeks_seen)} -- expected one.")
    return weeks_seen.pop()


def get_response_data(response):
    return response.data if response and hasattr(response, 'data') else []


def _get_existing_bank_line_ids():
    rows = supabase.table("bank_report_lines").select("bank_line_id").execute().data
    return {r["bank_line_id"] for r in rows}


def process_bank_report(file_path, uploaded_by, dry_run=True):
    result = {
        "success": False, "dry_run": dry_run, "message": "", "report_id": None,
        "week_number": None, "payment_run_id": None,
        "filename": os.path.basename(file_path), "errors": [],
        "matched_count": 0, "unmatched_count": 0, "ambiguous_count": 0,
        "amount_mismatch_count": 0, "not_applicable_count": 0,
        "already_processed_count": 0, "blank_id_count": 0,
        "total_lines": 0, "preview_lines": [],
    }

    if not os.path.isfile(file_path):
        result["errors"].append(f"File not found: {file_path}")
        return result
    if not is_valid_uuid(uploaded_by):
        result["errors"].append(f"Invalid uploaded-by UUID: {uploaded_by}")
        return result

    try:
        parsed = parse_bank_report(file_path)
    except Exception as e:
        result["errors"].append(f"Parser error: {e}")
        return result

    matched_lines = parsed.get("matched_paid_lines", [])
    unmatched_lines = parsed.get("unmatched_lines", [])
    result["total_lines"] = len(matched_lines) + len(unmatched_lines)

    try:
        week_number = get_week_from_lines(parsed)
        result["week_number"] = week_number
    except Exception as e:
        result["errors"].append(f"Week detection error: {e}")
        return result

    try:
        pr_resp = supabase.table("payment_runs").select("id").eq("week_number", week_number).execute()
        pr_rows = get_response_data(pr_resp)
        if not pr_rows:
            result["errors"].append(f"No payment run found for week {week_number}.")
            return result
        if len(pr_rows) > 1:
            ids = [row["id"] for row in pr_rows]
            result["errors"].append(f"Multiple payment runs for week {week_number}. Ambiguous. IDs: {ids}")
            return result
        payment_run_id = pr_rows[0]["id"]
        result["payment_run_id"] = payment_run_id
    except Exception as e:
        result["errors"].append(f"Payment run lookup error: {e}")
        return result

    existing_ids = _get_existing_bank_line_ids() if not dry_run else set()

    rows_to_write = []  # only lines with a non-blank bank_line_id land here

    for line in matched_lines:
        bank_line_id = str(line.get("Id", "")).strip()
        driver_ref = str(line.get("DriverReference") or "").strip() or None
        bank_amount = normalize_amount(line.get("BeneficiaryAmount"))

        if not bank_line_id:
            result["blank_id_count"] += 1
            result["preview_lines"].append({
                "bank_line_id": "", "driver_reference": driver_ref,
                "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
                "status": "skipped", "reason": "Missing bank line Id -- not written, no idempotency key.",
                "candidate_expense_id": None,
            })
            continue

        status, reason, matched_expense_id = None, None, None

        if not driver_ref:
            status, reason = "unmatched", "Missing DriverReference"
        else:
            drv_resp = supabase.table("drivers").select("id").eq("driver_code", driver_ref).execute()
            drv_rows = get_response_data(drv_resp)
            if len(drv_rows) == 0:
                status, reason = "unmatched", "Driver not found"
            elif len(drv_rows) > 1:
                status, reason = "ambiguous", "Multiple drivers found"
            else:
                driver_id = drv_rows[0]["id"]
                exp_resp = supabase.table("job_expenses").select("id, amount, status") \
                    .eq("driver_id", driver_id).eq("payment_run_id", payment_run_id).execute()
                expenses = get_response_data(exp_resp)

                if len(expenses) == 0:
                    status, reason = "unmatched", "No expense found"
                elif len(expenses) > 1:
                    status, reason = "ambiguous", "Multiple expenses found"
                else:
                    expense = expenses[0]
                    expense_amount = normalize_amount(expense.get("amount"))
                    matched_expense_id = expense["id"]

                    if bank_amount is None:
                        status, reason = "amount_mismatch", "Invalid/unparseable bank amount"
                    elif expense_amount is None:
                        status, reason = "amount_mismatch", "Invalid/unparseable stored amount"
                    elif abs(expense_amount - bank_amount) <= AMOUNT_TOLERANCE:
                        status = "matched"
                        reason = (
                            "Already marked paid by a previous bank report -- recorded again, not re-applied."
                            if expense["status"] == "paid" else "Amount matches"
                        )
                    else:
                        status = "amount_mismatch"
                        reason = f"Bank {bank_amount} vs stored {expense_amount}"

        result[f"{status}_count"] += 1
        row = {
            "bank_line_id": bank_line_id,
            "sheet_source": "matched_paid_driver_lines",
            "row_number": line.get("RowNumber"),
            "driver_invoice_id": line.get("DriverInvoiceId"),
            "payment_ref": line.get("PaymentRef"),
            "bank_execution_date": line.get("BankExecutionDate"),
            "beneficiary_name": line.get("BeneficiaryName"),
            "beneficiary_amount": float(bank_amount) if bank_amount is not None else None,
            "beneficiary_status": line.get("BeneficiaryStatus"),
            "driver_type": line.get("DriverType"),
            "driver_reference": driver_ref,
            "driver_name": line.get("DriverName"),
            "week_number": week_number,
            "bank_sort": line.get("BankSort"),
            "bank_account": line.get("BankAccount"),
            "match_status": status,
            "matched_job_expense_id": matched_expense_id,
            "reason": reason,
        }
        rows_to_write.append(row)
        result["preview_lines"].append({
            "bank_line_id": bank_line_id, "driver_reference": driver_ref,
            "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
            "status": status, "reason": reason, "candidate_expense_id": matched_expense_id,
        })

    for line in unmatched_lines:
        bank_line_id = str(line.get("Id", "")).strip()
        bank_amount = normalize_amount(line.get("BeneficiaryAmount"))

        if not bank_line_id:
            result["blank_id_count"] += 1
            result["preview_lines"].append({
                "bank_line_id": "", "driver_reference": None,
                "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
                "status": "skipped", "reason": "Missing bank line Id -- not written, no idempotency key.",
                "candidate_expense_id": None,
            })
            continue

        result["not_applicable_count"] += 1
        row = {
            "bank_line_id": bank_line_id,
            "sheet_source": "unmatched_lines",
            "row_number": line.get("RowNumber"),
            "driver_invoice_id": line.get("DriverInvoiceId"),
            "payment_ref": line.get("PaymentRef"),
            "bank_execution_date": line.get("BankExecutionDate"),
            "beneficiary_name": line.get("BeneficiaryName"),
            "beneficiary_amount": float(bank_amount) if bank_amount is not None else None,
            "beneficiary_status": line.get("BeneficiaryStatus"),
            "driver_type": None, "driver_reference": None, "driver_name": None,
            "week_number": None,
            "bank_sort": line.get("BankSort"),
            "bank_account": line.get("BankAccount"),
            "match_status": "not_applicable",
            "matched_job_expense_id": None,
            "reason": "Administrative/non-driver bank line.",
        }
        rows_to_write.append(row)
        result["preview_lines"].append({
            "bank_line_id": bank_line_id, "driver_reference": None,
            "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
            "status": "not_applicable", "reason": row["reason"], "candidate_expense_id": None,
        })

    # Duplicate bank_line_id WITHIN this file -- refuse the whole commit
    # rather than partially apply it. A collision here means something
    # is wrong with the export itself, not a per-line judgment call.
    id_counts = Counter(row["bank_line_id"] for row in rows_to_write)
    duplicates = [bank_id for bank_id, count in id_counts.items() if count > 1]
    if duplicates:
        result["errors"].append(
            f"File contains duplicate bank_line_id value(s) within itself: {duplicates}. "
            f"Refusing to process -- nothing was written."
        )
        return result

    if dry_run:
        result["success"] = True
        result["message"] = "Dry run completed. No database writes."
        return result

    new_rows = [row for row in rows_to_write if row["bank_line_id"] not in existing_ids]
    result["already_processed_count"] = len(rows_to_write) - len(new_rows)

    if not new_rows:
        result["success"] = True
        result["message"] = "All lines already processed in a previous run -- no new report created."
        return result

    # Per-run counts (only what THIS run actually writes) -- kept separate
    # from the full-file classification counts above, so a partial rerun
    # (some lines already processed, some new) doesn't produce a
    # bank_reports summary claiming more was written than actually was.
    run_counts = Counter(row["match_status"] for row in new_rows)

    try:
        report_insert = supabase.table("bank_reports").insert({
            "filename": os.path.basename(file_path),
            "uploaded_by": uploaded_by,
        }).execute()
        report_id = report_insert.data[0]["id"]
        result["report_id"] = report_id

        for row in new_rows:
            row_with_report = dict(row, bank_report_id=report_id)
            supabase.table("bank_report_lines").insert(row_with_report).execute()

            if row["match_status"] == "matched" and row["matched_job_expense_id"]:
                current = supabase.table("job_expenses").select("status") \
                    .eq("id", row["matched_job_expense_id"]).execute().data[0]
                if current["status"] != "paid":
                    supabase.table("job_expenses").update({"status": "paid"}) \
                        .eq("id", row["matched_job_expense_id"]).execute()

        supabase.table("bank_reports").update({
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "week_number": week_number,
            "matched_count": run_counts.get("matched", 0),
            "unmatched_count": run_counts.get("unmatched", 0),
            "ambiguous_count": run_counts.get("ambiguous", 0),
            "amount_mismatch_count": run_counts.get("amount_mismatch", 0),
            "not_applicable_count": run_counts.get("not_applicable", 0),
        }).eq("id", report_id).execute()

        result["success"] = True
        result["message"] = f"Bank report processed successfully. {len(new_rows)} new line(s) written."
    except Exception as e:
        result["success"] = False
        result["errors"].append(
            f"Commit error: {e} -- if this happened mid-write, delete the bank_reports row "
            f"'{result.get('report_id')}' (cascades to its lines) and re-run; already-inserted "
            f"lines from this attempt will be skipped correctly on retry."
        )

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Excel bank report file")
    parser.add_argument("--uploaded-by", required=True, help="User UUID")
    parser.add_argument("--commit", action="store_true", help="Commit (default: dry-run)")
    args = parser.parse_args()

    if not is_valid_uuid(args.uploaded_by):
        print("Invalid uploaded-by UUID")
        sys.exit(1)

    result = process_bank_report(args.file, args.uploaded_by, dry_run=not args.commit)
    print(json.dumps(result, indent=2, default=str))
    if not args.commit and result.get("success"):
        print("\nDry run completed. Use --commit to apply.")
    if args.commit and result.get("success"):
        print("\nLive processing completed.")
    if not result.get("success"):
        print("\nFailed.")
        for err in result.get("errors", []):
            print(f"  - {err}")
        sys.exit(1)