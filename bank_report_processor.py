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

Multi-run-per-week reconciliation:
    A single week may deliberately contain several payment runs (e.g.
    separate Friday / Saturday / Sunday uploads, all with week_number=37
    and different expense_file_date values). The processor therefore
    fetches every payment_run for the week and disambiguates each bank
    line individually:
        Week + Driver  ->  candidate expenses across all runs
        Amount         ->  filter
        Bank date      ->  break ties (prefers expense_file_date + 1 day,
                            the documented workflow), then same day,
                            then leaves ambiguous if still >1.
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
    """
    Return (majority_week, sorted_list_of_all_weeks).

    A bank file can legitimately contain lines from more than one week
    (e.g. a late week-37 payment alongside week-38 payments). We don't
    reject such files -- we return the majority week (for the report
    header) and the full list of weeks present, so the caller can match
    each line against its OWN week's payment runs.
    """
    weeks_seen = Counter()
    for line in parsed.get('matched_paid_lines', []):
        week = line.get('WeekNumber')
        if week is not None:
            weeks_seen[int(week)] += 1
    if not weeks_seen:
        raise ValueError("No WeekNumber found in Matched Paid lines.")
    majority = weeks_seen.most_common(1)[0][0]
    return majority, sorted(weeks_seen.keys())


def get_response_data(response):
    return response.data if response and hasattr(response, 'data') else []


def _get_existing_bank_line_ids():
    rows = supabase.table("bank_report_lines").select("bank_line_id").execute().data
    return {r["bank_line_id"] for r in rows}


def _narrow_by_date(candidate_expenses, bank_date_iso, payment_runs):
    """
    Narrow amount-matching expenses using the bank value date vs each
    candidate's payment_run.expense_file_date.

    Order of preference (Fleet Support workflow):
      1. bank_date == expense_file_date + 1   (normal next-day)
      2. bank_date == expense_file_date        (same-day, rare)
    Reverse-date (-1) is NOT accepted: unconfirmed by the business and
    could silently match the wrong run. Larger deltas (weekend, bank
    holidays) are not filtered here -- they surface via the reason string
    so Charlotte can review them on the preview page.
    """
    if not bank_date_iso:
        return candidate_expenses

    try:
        bank_date = datetime.strptime(bank_date_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return candidate_expenses

    # Weekend payments land on Monday, so deltas of +1, +2 and +3 are all
    # legitimate (Sat, Sun, Fri expenses paid the following Monday).
    # Prefer the smallest non-zero delta (Sat's expense paid Monday has
    # delta 2, Sun's has delta 1, Fri's has delta 3) -- we prefer +1
    # because Sunday's expense is the "normal" case for a Monday payment.
    delta_1, delta_2, delta_3, exact = [], [], [], []
    for exp in candidate_expenses:
        run_date_str = payment_runs.get(exp["payment_run_id"])
        if not run_date_str:
            continue
        try:
            run_date = datetime.strptime(str(run_date_str), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        delta = (bank_date - run_date).days
        if delta == 1:
            delta_1.append(exp)
        elif delta == 2:
            delta_2.append(exp)
        elif delta == 3:
            delta_3.append(exp)
        elif delta == 0:
            exact.append(exp)

    for bucket in (delta_1, delta_2, delta_3, exact):
        if len(bucket) == 1:
            return bucket
        if len(bucket) > 1:
            return bucket

    return candidate_expenses


def _format_delta(bank_date_iso, expense_date_str):
    """Short '(bank date = expense date + N days)' suffix, or ''."""
    if not bank_date_iso or not expense_date_str:
        return ""
    try:
        bd = datetime.strptime(bank_date_iso, "%Y-%m-%d").date()
        ed = datetime.strptime(str(expense_date_str), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    d = (bd - ed).days
    if d == 0:
        return ", same day"
    if d > 0:
        return f", +{d} day" + ("s" if d != 1 else "")
    return f", {d} day" + ("s" if d != -1 else "")


def process_bank_report(file_path, uploaded_by, dry_run=True, original_filename=None):
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
        week_number, all_weeks = get_week_from_lines(parsed)
        result["week_number"] = week_number
    except Exception as e:
        result["errors"].append(f"Week detection error: {e}")
        return result

    
    # ---- Fetch payment runs for EVERY week present in the file ----
    try:
        payment_runs_by_week = {}
        for w in all_weeks:
            pr_resp = (
                supabase.table("payment_runs")
                .select("id, expense_file_date")
                .eq("week_number", w)
                .execute()
            )
            pr_rows = get_response_data(pr_resp)
            payment_runs_by_week[w] = {
                row["id"]: row.get("expense_file_date") for row in pr_rows
            }
    except Exception as e:
        result["errors"].append(f"Payment run lookup error: {e}")
        return result

    # Flat run_id -> expense_file_date lookup, used by date disambiguation.
    payment_runs = {}
    for week_runs in payment_runs_by_week.values():
        payment_runs.update(week_runs)

    # Legacy result field, kept for downstream callers.
    result["payment_run_id"] = next(iter(payment_runs), None)

    existing_ids = _get_existing_bank_line_ids() if not dry_run else set()
    rows_to_write = []

    # ============================================================
    # Matched lines (from the raw bank file, all payment rows land here)
    # ============================================================
    for line in matched_lines:
        bank_line_id = str(line.get("Id", "")).strip()
        driver_ref = str(line.get("DriverReference") or "").strip() or None
        bank_amount = normalize_amount(line.get("BeneficiaryAmount"))

        if not bank_line_id:
            result["blank_id_count"] += 1
            result["preview_lines"].append({
                "bank_line_id": "", "driver_reference": driver_ref,
                "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
                "status": "skipped",
                "reason": "Missing bank line Id -- not written, no idempotency key.",
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

                # Match against the payment runs for THIS line's week only.
                line_week = line.get("WeekNumber")
                if line_week is not None:
                    week_runs = payment_runs_by_week.get(int(line_week), {})
                    candidate_run_ids = list(week_runs.keys())
                else:
                    candidate_run_ids = list(payment_runs.keys())

                if not candidate_run_ids:
                    expenses = []
                else:
                    exp_resp = (
                        supabase.table("job_expenses")
                        .select("id, amount, status, payment_run_id")
                        .eq("driver_id", driver_id)
                        .in_("payment_run_id", candidate_run_ids)
                        .execute()
                    )
                    expenses = get_response_data(exp_resp)

                if len(expenses) == 0:
                    status, reason = "unmatched", "No expense found for driver in this week"
                else:
                    # ---- Step 1: filter by amount ----
                    amount_matches = []
                    for exp in expenses:
                        expense_amount = normalize_amount(exp.get("amount"))
                        if (
                            bank_amount is not None
                            and expense_amount is not None
                            and abs(expense_amount - bank_amount) <= AMOUNT_TOLERANCE
                        ):
                            amount_matches.append(exp)

                    if bank_amount is None:
                        status, reason = "amount_mismatch", "Invalid/unparseable bank amount"

                    elif len(amount_matches) == 0:
                        if len(expenses) == 1:
                            exp_amt = normalize_amount(expenses[0].get("amount"))
                            status = "amount_mismatch"
                            reason = f"Bank {bank_amount} vs stored {exp_amt}"
                        else:
                            status = "amount_mismatch"
                            reason = (
                                f"Bank {bank_amount} matches none of "
                                f"{len(expenses)} candidate expense(s) for this driver"
                            )

                    elif len(amount_matches) == 1:
                        # Single amount match: accept only if the date
                        # relationship is consistent with the documented
                        # workflow (+1 day or same day). Any other delta
                        # is flagged as ambiguous so Charlotte can review.
                        expense = amount_matches[0]
                        exp_date = payment_runs.get(expense["payment_run_id"])

                        delta_days = None
                        if line.get("BankExecutionDate") and exp_date:
                            try:
                                bd = datetime.strptime(line["BankExecutionDate"], "%Y-%m-%d").date()
                                ed = datetime.strptime(str(exp_date), "%Y-%m-%d").date()
                                delta_days = (bd - ed).days
                            except (ValueError, TypeError):
                                pass

                        if delta_days in (0, 1, 2, 3):
                            matched_expense_id = expense["id"]
                            status = "matched"
                            delta_str = _format_delta(line.get("BankExecutionDate"), exp_date)
                            if expense["status"] == "paid":
                                reason = (
                                    f"Already marked paid by a previous bank report "
                                    f"(expense_file_date={exp_date}{delta_str})."
                                )
                            else:
                                reason = (
                                    f"Amount matches (expense_file_date={exp_date}{delta_str})."
                                )
                        else:
                            status = "ambiguous"
                            reason = (
                                f"Amount matches but date delta is {delta_days} days "
                                f"(bank {line.get('BankExecutionDate')} vs "
                                f"expense_file_date {exp_date}) — needs manual review."
                            )

                    else:
                        # Multiple amount matches: use date to narrow.
                        date_matches = _narrow_by_date(
                            amount_matches, line.get("BankExecutionDate"), payment_runs
                        )

                        if len(date_matches) == 1:
                            expense = date_matches[0]
                            matched_expense_id = expense["id"]
                            status = "matched"
                            exp_date = payment_runs.get(expense["payment_run_id"])
                            delta_str = _format_delta(line.get("BankExecutionDate"), exp_date)
                            if expense["status"] == "paid":
                                reason = (
                                    f"Already marked paid by a previous bank report "
                                    f"(date-disambiguated, expense_file_date={exp_date}{delta_str})."
                                )
                            else:
                                reason = (
                                    f"Amount matches and disambiguated by date "
                                    f"(expense_file_date={exp_date}{delta_str})."
                                )
                        else:
                            candidate_dates = sorted({
                                str(payment_runs.get(e["payment_run_id"]))
                                for e in amount_matches
                            })
                            status = "ambiguous"
                            reason = (
                                f"{len(amount_matches)} expenses with the same amount "
                                f"across runs with expense_file_date={candidate_dates}"
                            )

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
            "week_number": line.get("WeekNumber"),
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

    # ============================================================
    # Unmatched lines (only present in legacy 4-sheet files)
    # ============================================================
    for line in unmatched_lines:
        bank_line_id = str(line.get("Id", "")).strip()
        bank_amount = normalize_amount(line.get("BeneficiaryAmount"))

        if not bank_line_id:
            result["blank_id_count"] += 1
            result["preview_lines"].append({
                "bank_line_id": "", "driver_reference": None,
                "beneficiary_amount": str(bank_amount) if bank_amount is not None else None,
                "status": "skipped",
                "reason": "Missing bank line Id -- not written, no idempotency key.",
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
            "status": "not_applicable", "reason": row["reason"],
            "candidate_expense_id": None,
        })

    # Refuse the whole commit if the file contains duplicate ids within itself.
    id_counts = Counter(row["bank_line_id"] for row in rows_to_write)
    duplicates = [bid for bid, count in id_counts.items() if count > 1]
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

    run_counts = Counter(row["match_status"] for row in new_rows)

    try:
        report_insert = supabase.table("bank_reports").insert({
            "filename": original_filename or os.path.basename(file_path),
            "uploaded_by": uploaded_by,
        }).execute()
        report_id = report_insert.data[0]["id"]
        result["report_id"] = report_id

        for row in new_rows:
            row_with_report = dict(row, bank_report_id=report_id)
            supabase.table("bank_report_lines").insert(row_with_report).execute()

            if row["match_status"] == "matched" and row["matched_job_expense_id"]:
                current = (
                    supabase.table("job_expenses").select("status")
                    .eq("id", row["matched_job_expense_id"]).execute().data[0]
                )
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