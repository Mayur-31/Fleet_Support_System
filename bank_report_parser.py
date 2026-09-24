"""
Bank Report Parser for Fleet Support System.

Supports two input formats:

1. Raw bank portal report — "Transaction Initiation Detail Report".
   Single sheet. Metadata block, header row, payment rows, totals block.
   A single file can contain multiple transaction blocks ("1 of 4" etc.),
   each with its own repeated header row.

2. Legacy 4-sheet report from the old back office:
   - Driver Type Summary
   - Unmatched Lines
   - Driver Week Breakdown
   - Matched Paid Driver Lines

Both produce the same internal structure, so the processor needs no
format-specific code.

Usage:
    from bank_report_parser import parse_bank_report
    data = parse_bank_report("data/FLT 18.09.2026.xlsx")
"""
import math
import re
import json
import hashlib
import pandas as pd
from pathlib import Path
from datetime import datetime


# ============================================================
# Legacy 4-sheet format
# ============================================================
REQUIRED_SHEETS = {
    "Driver Type Summary",
    "Unmatched Lines",
    "Driver Week Breakdown",
    "Matched Paid Driver Lines",
}

MATCHED_PAID_MIN_COLUMNS = {
    "Id", "RowNumber", "DriverInvoiceId", "BankExecutionDate",
    "BeneficiaryName", "BeneficiaryAmount", "PaymentRef",
    "DriverReference", "WeekNumber", "BankSort", "BankAccount"
}

UNMATCHED_MIN_COLUMNS = {
    "Id", "RowNumber", "BeneficiaryName", "BeneficiaryAmount",
    "BankSort", "BankAccount"
}


# ============================================================
# Raw bank portal format markers
# ============================================================
RAW_TITLE_MARKER = "Transaction Initiation Detail Report"
RAW_HEADER_MARKER = "Beneficiary Bank Identifier"

RAW_END_MARKERS = (
    "Total Amount (Base",
    "Number of Transactions",
    "Total By",
    "Total Number of Transactions",
    "Cross-currency calculations",
    "Filters have been applied",
    "SELECTION CRITERIA",
)


# ============================================================
# Shared helpers
# ============================================================
def _is_missing(value):
    """More robust than a plain `isinstance(value, float) and isnan()` check."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _clean_nans(records: list) -> list:
    cleaned = []
    for record in records:
        clean_record = {}
        for key, value in record.items():
            clean_record[key] = None if _is_missing(value) else value
        cleaned.append(clean_record)
    return cleaned


def _normalize_dates(records: list, date_keys: list) -> list:
    normalized = []
    for record in records:
        clean_record = {}
        for key, value in record.items():
            if key in date_keys and isinstance(value, (pd.Timestamp, datetime)):
                clean_record[key] = value.strftime("%Y-%m-%d")
            else:
                clean_record[key] = value
        normalized.append(clean_record)
    return normalized


def _to_str(value):
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _strip_html(text):
    if not text:
        return text
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_amount(value):
    if _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("£", "").replace("\xa0", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value):
    if _is_missing(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _split_sort_account(value):
    text = _to_str(value)
    if not text or "/" not in text:
        return None, None
    parts = [p.strip() for p in text.split("/")]
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None, None


def _parse_ref(ref):
    if not ref:
        return None, None
    m = re.match(r"^WK(\d+)-(.+)$", str(ref).strip(), re.IGNORECASE)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, None


def _get_cell(row_list, idx):
    if idx is None or idx >= len(row_list):
        return None
    return row_list[idx]


# ============================================================
# Format detection
# ============================================================
def _detect_format(excel_file: pd.ExcelFile) -> str:
    sheets = set(excel_file.sheet_names)

    if REQUIRED_SHEETS.issubset(sheets):
        return "legacy"

    if len(sheets) == 1:
        try:
            preview = pd.read_excel(
                excel_file, sheet_name=excel_file.sheet_names[0],
                header=None, nrows=15,
            )
        except Exception:
            preview = None
        if preview is not None:
            for cell in preview.values.flatten():
                if isinstance(cell, str) and RAW_TITLE_MARKER in cell:
                    return "raw"

    raise ValueError(
        f"Unrecognised bank report format. Sheets found: {excel_file.sheet_names}. "
        f"Expected either the legacy 4-sheet report ({sorted(REQUIRED_SHEETS)}) "
        f"or the raw bank portal report containing the text "
        f"'{RAW_TITLE_MARKER}'."
    )


# ============================================================
# Public entry point
# ============================================================
def parse_bank_report(file_path: str) -> dict:
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    excel_file = pd.ExcelFile(file_path)
    fmt = _detect_format(excel_file)

    if fmt == "legacy":
        return _parse_legacy_format(file_path)
    return _parse_raw_format(file_path, excel_file)


# ============================================================
# Legacy 4-sheet parser (unchanged behaviour)
# ============================================================
def _parse_legacy_format(file_path: Path) -> dict:
    excel_file = pd.ExcelFile(file_path)
    sheets_found = excel_file.sheet_names

    result = {
        "summary": [],
        "unmatched_lines": [],
        "week_breakdown_raw": {},
        "matched_paid_lines": [],
        "sheets_found": sheets_found,
        "source_format": "legacy_4sheet",
    }

    df_summary = pd.read_excel(file_path, sheet_name="Driver Type Summary")
    result["summary"] = _clean_nans(df_summary.to_dict(orient="records"))

    df_unmatched = pd.read_excel(file_path, sheet_name="Unmatched Lines")
    missing_cols = UNMATCHED_MIN_COLUMNS - set(df_unmatched.columns)
    if missing_cols:
        raise ValueError(
            f"Unmatched Lines sheet missing required column(s): {missing_cols}\n"
            f"Found: {list(df_unmatched.columns)}"
        )
    result["unmatched_lines"] = _clean_nans(df_unmatched.to_dict(orient="records"))
    result["unmatched_lines"] = _normalize_dates(
        result["unmatched_lines"], ["BankExecutionDate"]
    )

    df_breakdown = pd.read_excel(
        file_path, sheet_name="Driver Week Breakdown", header=None
    )
    if len(df_breakdown) > 0:
        header_row = df_breakdown.iloc[0].tolist()
        col_names, seen = [], {}
        for col in header_row:
            if col in seen:
                seen[col] += 1
                col_names.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                col_names.append(str(col))
        data_rows = df_breakdown.iloc[1:].values.tolist()
        week_data = []
        for row in data_rows:
            if any(pd.notnull(x) for x in row):
                row_dict = {}
                for i, val in enumerate(row):
                    if i < len(col_names):
                        row_dict[col_names[i]] = None if _is_missing(val) else val
                week_data.append(row_dict)
        result["week_breakdown_raw"] = {"headers": col_names, "rows": week_data}

    df_matched = pd.read_excel(file_path, sheet_name="Matched Paid Driver Lines")
    missing_cols = MATCHED_PAID_MIN_COLUMNS - set(df_matched.columns)
    if missing_cols:
        raise ValueError(
            f"Matched Paid Driver Lines sheet missing required column(s): {missing_cols}\n"
            f"Found: {list(df_matched.columns)}"
        )
    result["matched_paid_lines"] = _clean_nans(df_matched.to_dict(orient="records"))
    result["matched_paid_lines"] = _normalize_dates(
        result["matched_paid_lines"], ["BankExecutionDate"]
    )

    return result


# ============================================================
# Raw bank portal parser
# ============================================================
def _parse_raw_format(file_path: Path, excel_file: pd.ExcelFile) -> dict:
    raw = pd.read_excel(excel_file, sheet_name=excel_file.sheet_names[0], header=None)

    # 1. Locate the FIRST header row
    header_row_idx = None
    for i in range(len(raw)):
        row = raw.iloc[i]
        for cell in row:
            if isinstance(cell, str) and RAW_HEADER_MARKER in cell:
                header_row_idx = i
                break
        if header_row_idx is not None:
            break

    if header_row_idx is None:
        raise ValueError(
            f"Could not find the '{RAW_HEADER_MARKER}' header row in the bank file. "
            f"This may not be a complete Transaction Initiation Detail Report."
        )

    # 2. Build column-name → index map from the first header
    header_row = raw.iloc[header_row_idx]
    col_map = {}
    for idx, cell in enumerate(header_row):
        text = _strip_html(_to_str(cell))
        if not text:
            continue
        if RAW_HEADER_MARKER in text:
            col_map.setdefault("beneficiary_account", idx)
        elif text.startswith("Beneficiary Amount"):
            col_map.setdefault("beneficiary_amount", idx)
        elif "Beneficiary Name" in text:
            col_map.setdefault("beneficiary_name", idx)
        elif "Payment Reference" in text:
            col_map.setdefault("payment_reference", idx)
        elif "Beneficiary Status" in text:
            col_map.setdefault("beneficiary_status", idx)

    required = ["beneficiary_account", "beneficiary_amount", "payment_reference"]
    missing = [k for k in required if k not in col_map]
    if missing:
        raise ValueError(
            f"Bank file header row is missing required column(s): {missing}."
        )

    # 3. Read metadata above the FIRST header (Value Date, Transaction Ref)
    value_date = None
    transaction_ref = None
    for i in range(header_row_idx):
        row_values = raw.iloc[i].tolist()
        for j, cell in enumerate(row_values):
            if not isinstance(cell, str):
                continue
            label = cell.strip()
            if label == "Value Date" and value_date is None:
                for k in range(j + 1, len(row_values)):
                    parsed = _parse_date(row_values[k])
                    if parsed:
                        value_date = parsed
                        break
            elif label == "Transaction Reference Number" and transaction_ref is None:
                for k in range(j + 1, len(row_values)):
                    candidate = _to_str(row_values[k])
                    if candidate:
                        transaction_ref = candidate
                        break

    # 4. Read data rows.
    #    A single file may contain multiple transaction blocks ("1 of 4" etc.),
    #    each with its own repeated header row. We must skip those headers,
    #    otherwise they are parsed as fake data rows and (because they all
    #    share the literal text "Payment Reference" in the reference column)
    #    they hash to the same bank_line_id, triggering a false duplicate.
    payment_lines = []
    for i in range(header_row_idx + 1, len(raw)):
        row_values = raw.iloc[i].tolist()

        # Skip repeated header rows from additional transaction blocks.
        header_probe = _strip_html(
            _to_str(_get_cell(row_values, col_map["beneficiary_account"]))
        )
        if header_probe and RAW_HEADER_MARKER in header_probe:
            continue

        # Stop at any footer marker row
        first_text = None
        for cell in row_values:
            text = _strip_html(_to_str(cell))
            if text:
                first_text = text
                break
        if first_text and any(m in first_text for m in RAW_END_MARKERS):
            break

        ref = _to_str(_get_cell(row_values, col_map["payment_reference"]))
        if not ref:
            continue

        account_cell = _get_cell(row_values, col_map["beneficiary_account"])
        sort_code, account_number = _split_sort_account(account_cell)

        amount = _parse_amount(_get_cell(row_values, col_map["beneficiary_amount"]))

        name = None
        if "beneficiary_name" in col_map:
            name = _strip_html(_to_str(_get_cell(row_values, col_map["beneficiary_name"])))

        status = None
        if "beneficiary_status" in col_map:
            status = _to_str(_get_cell(row_values, col_map["beneficiary_status"]))

        week_number, driver_reference = _parse_ref(ref)

        # Idempotency key: payment ref + account + amount + value date.
        # Deliberately excludes the file-level transaction reference, which
        # the bank may regenerate when the same report is re-downloaded.
        account_key = account_number or "noacct"
        amount_key = f"{amount:.2f}" if amount is not None else "noamt"
        raw_key = f"{ref}|{account_key}|{amount_key}|{value_date or 'nodate'}"
        bank_line_id = hashlib.sha256(raw_key.encode()).hexdigest()[:32]

        payment_lines.append({
            "Id": bank_line_id,
            "RowNumber": i + 1,
            "DriverInvoiceId": None,
            "BankExecutionDate": value_date,
            "BeneficiaryName": name,
            "BeneficiaryAmount": amount,
            "PaymentRef": ref,
            "BeneficiaryStatus": status,
            "DriverType": None,
            "DriverId": None,
            "DriverReference": driver_reference,
            "DriverName": name,
            "InvoiceType": None,
            "WeekNumber": week_number,
            "BeneficiaryStatusAdditionalInfo": None,
            "Note": None,
            "BankSort": sort_code,
            "BankAccount": account_number,
        })

    if not payment_lines:
        raise ValueError(
            "No payment lines could be extracted from the bank file. "
            "Check that the file is a complete Transaction Initiation Detail Report."
        )

    return {
        "summary": [],
        "unmatched_lines": [],
        "week_breakdown_raw": {},
        "matched_paid_lines": payment_lines,
        "sheets_found": excel_file.sheet_names,
        "source_format": "raw_bank_portal",
    }


# ============================================================
# Preserved helpers (unchanged API)
# ============================================================
def validate_file_structure(file_path: str) -> dict:
    try:
        data = parse_bank_report(file_path)
        return {"valid": True, "data": data, "errors": []}
    except (ValueError, FileNotFoundError) as e:
        return {"valid": False, "data": None, "errors": [str(e)]}
    except Exception as e:
        return {"valid": False, "data": None, "errors": [f"Unexpected error: {e}"]}


def check_idempotency_ready(data: dict) -> dict:
    matched_lines = data.get("matched_paid_lines", [])
    unmatched_lines = data.get("unmatched_lines", [])

    results = {
        "all_have_id": True,
        "blank_rows": {"matched_paid_driver_lines": [], "unmatched_lines": []},
        "unique_ids": True,
        "duplicates": [],
    }

    for i, row in enumerate(matched_lines, start=2):
        if not row.get("Id"):
            results["all_have_id"] = False
            results["blank_rows"]["matched_paid_driver_lines"].append(i)

    for i, row in enumerate(unmatched_lines, start=2):
        if not row.get("Id"):
            results["all_have_id"] = False
            results["blank_rows"]["unmatched_lines"].append(i)

    all_ids = []
    for row in matched_lines + unmatched_lines:
        if row.get("Id"):
            all_ids.append(row["Id"])

    if len(all_ids) != len(set(all_ids)):
        results["unique_ids"] = False
        seen, duplicates = set(), set()
        for line_id in all_ids:
            if line_id in seen:
                duplicates.add(line_id)
            seen.add(line_id)
        results["duplicates"] = list(duplicates)

    return results


# ============================================================
# CLI test harness
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python bank_report_parser.py <path_to_excel_file>")
        sys.exit(1)

    file_path = sys.argv[1]
    print(f"📂 Parsing: {file_path}\n")

    try:
        result = parse_bank_report(file_path)
        fmt = result.get("source_format", "unknown")
        print(f"✅ Parsing successful (format: {fmt})\n")
        print(f"  Sheets found: {result['sheets_found']}")
        print(f"  Matched Paid rows: {len(result['matched_paid_lines'])}")
        print(f"  Unmatched rows: {len(result['unmatched_lines'])}")

        print("\n🔍 Idempotency check:")
        id_check = check_idempotency_ready(result)
        print(f"  All rows have Id: {id_check['all_have_id']}")
        print(f"  All Ids unique: {id_check['unique_ids']}")

        if result["matched_paid_lines"]:
            print("\n📄 First 3 lines:")
            for idx, row in enumerate(result["matched_paid_lines"][:3], 1):
                print(f"\n  Row {idx}:")
                for k in ("Id", "PaymentRef", "DriverReference", "WeekNumber",
                          "BeneficiaryAmount", "BankSort", "BankAccount",
                          "BeneficiaryName", "BankExecutionDate"):
                    print(f"    {k}: {row.get(k)}")

        print("\n🔬 JSON serialization test:")
        try:
            json.dumps(result, allow_nan=False)
            print("  ✅ PASSED")
        except (ValueError, TypeError) as e:
            print(f"  ❌ FAILED: {e}")
            sys.exit(1)

    except FileNotFoundError as e:
        print(f"❌ File not found: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"❌ Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"❌ Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)