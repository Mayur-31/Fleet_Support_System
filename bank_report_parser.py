"""
Bank Report Parser for Fleet Support System.

Reads the bank's post-payment Excel report (4 sheets):
- Driver Type Summary
- Unmatched Lines
- Driver Week Breakdown
- Matched Paid Driver Lines

Validates structure, extracts data, and returns a clean dictionary.
Does NOT write to the database — that's the processor's job.

Usage:
    from bank_report_parser import parse_bank_report
    data = parse_bank_report("data/FLT_19.08.2026_2.xlsx")
"""
import math
import json
import pandas as pd
from pathlib import Path
from datetime import datetime

# Required sheets (exact names from Charlotte's sample)
REQUIRED_SHEETS = {
    "Driver Type Summary",
    "Unmatched Lines",
    "Driver Week Breakdown",
    "Matched Paid Driver Lines",
}

# Minimum required columns for Matched Paid Driver Lines
# (we only check these exist; extra columns are ignored)
MATCHED_PAID_MIN_COLUMNS = {
    "Id", "RowNumber", "DriverInvoiceId", "BankExecutionDate",
    "BeneficiaryName", "BeneficiaryAmount", "PaymentRef",
    "DriverReference", "WeekNumber", "BankSort", "BankAccount"
}

# Minimum required columns for Unmatched Lines
UNMATCHED_MIN_COLUMNS = {
    "Id", "RowNumber", "BeneficiaryName", "BeneficiaryAmount",
    "BankSort", "BankAccount"
}


def _clean_nans(records: list) -> list:
    """
    Converts NaN -> None on already-materialized dicts.
    
    Why this is needed:
    Using df.where(pd.notnull(df), None).to_dict(orient="records") looks
    correct, but pandas preserves the float dtype for numeric columns.
    The None gets silently coerced back to float('nan') to maintain the dtype.
    
    This function iterates over the finalized dicts (where pandas' dtype
    coercion no longer applies) and replaces any float('nan') with None.
    
    Args:
        records: List of dicts from pandas .to_dict(orient="records")
    
    Returns:
        List of dicts with NaN values replaced by None
    """
    cleaned = []
    for record in records:
        clean_record = {}
        for key, value in record.items():
            # Check if it's a float and is NaN (including numpy.nan)
            if isinstance(value, float) and math.isnan(value):
                clean_record[key] = None
            else:
                clean_record[key] = value
        cleaned.append(clean_record)
    return cleaned


def _normalize_dates(records: list, date_keys: list) -> list:
    """
    Converts pandas Timestamp objects to ISO 8601 date strings.
    
    This ensures the JSON serialization test (allow_nan=False) doesn't
    fail because of pandas Timestamp objects (which are not JSON-serializable).
    
    Args:
        records: List of dicts
        date_keys: List of keys that contain dates (e.g., ["BankExecutionDate"])
    
    Returns:
        List of dicts with dates normalized to ISO strings
    """
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


def parse_bank_report(file_path: str) -> dict:
    """
    Parses the bank report Excel file and returns structured data.

    Returns:
        dict: {
            "summary": list of dicts (Driver Type Summary),
            "unmatched_lines": list of dicts,
            "week_breakdown_raw": dict with "headers" and "rows" (positional),
            "matched_paid_lines": list of dicts,
            "sheets_found": list of sheet names
        }

    Raises:
        ValueError: If file is missing required sheets or columns.
        FileNotFoundError: If file doesn't exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Read all sheets
    excel_file = pd.ExcelFile(file_path)
    sheets_found = excel_file.sheet_names
    missing_sheets = REQUIRED_SHEETS - set(sheets_found)
    if missing_sheets:
        raise ValueError(f"Missing required sheet(s): {missing_sheets}")

    result = {
        "summary": [],
        "unmatched_lines": [],
        "week_breakdown_raw": {},
        "matched_paid_lines": [],
        "sheets_found": sheets_found,
    }

    # ============================================================
    # 1. Driver Type Summary
    # ============================================================
    df_summary = pd.read_excel(file_path, sheet_name="Driver Type Summary")
    summary_records = df_summary.to_dict(orient="records")
    result["summary"] = _clean_nans(summary_records)

    # ============================================================
    # 2. Unmatched Lines
    # ============================================================
    df_unmatched = pd.read_excel(file_path, sheet_name="Unmatched Lines")
    missing_cols = UNMATCHED_MIN_COLUMNS - set(df_unmatched.columns)
    if missing_cols:
        raise ValueError(
            f"Unmatched Lines sheet missing required column(s): {missing_cols}\n"
            f"Found: {list(df_unmatched.columns)}"
        )
    unmatched_records = df_unmatched.to_dict(orient="records")
    result["unmatched_lines"] = _clean_nans(unmatched_records)

    # Normalize dates in Unmatched Lines (BankExecutionDate)
    result["unmatched_lines"] = _normalize_dates(result["unmatched_lines"], ["BankExecutionDate"])

    # ============================================================
    # 3. Driver Week Breakdown (positional parsing)
    # ============================================================
    # This sheet has duplicate column names ("Week", "Amount" repeated 3 times).
    # We read it without headers and parse positionally.
    df_breakdown = pd.read_excel(file_path, sheet_name="Driver Week Breakdown", header=None)

    if len(df_breakdown) > 0:
        header_row = df_breakdown.iloc[0].tolist()
        # Create unique column names: if duplicate, add suffix
        col_names = []
        seen = {}
        for col in header_row:
            if col in seen:
                seen[col] += 1
                col_names.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                col_names.append(str(col))
        # Read the data rows (index 1 onwards)
        data_rows = df_breakdown.iloc[1:].values.tolist()
        # Convert to list of dicts with the generated column names
        week_data = []
        for row in data_rows:
            if any(pd.notnull(x) for x in row):  # skip empty rows
                row_dict = {}
                for i, val in enumerate(row):
                    if i < len(col_names):
                        if pd.isna(val):
                            row_dict[col_names[i]] = None
                        else:
                            row_dict[col_names[i]] = val
                week_data.append(row_dict)
        result["week_breakdown_raw"] = {
            "headers": col_names,
            "rows": week_data
        }

    # ============================================================
    # 4. Matched Paid Driver Lines
    # ============================================================
    df_matched = pd.read_excel(file_path, sheet_name="Matched Paid Driver Lines")
    missing_cols = MATCHED_PAID_MIN_COLUMNS - set(df_matched.columns)
    if missing_cols:
        raise ValueError(
            f"Matched Paid Driver Lines sheet missing required column(s): {missing_cols}\n"
            f"Found: {list(df_matched.columns)}"
        )
    matched_records = df_matched.to_dict(orient="records")
    result["matched_paid_lines"] = _clean_nans(matched_records)

    # Normalize dates in Matched Paid Driver Lines
    result["matched_paid_lines"] = _normalize_dates(result["matched_paid_lines"], ["BankExecutionDate"])

    return result


def validate_file_structure(file_path: str) -> dict:
    """
    Lightweight validation: just checks sheets and basic headers.
    Returns a dict with "valid": bool, "data": dict (if valid), "errors": list.
    """
    try:
        data = parse_bank_report(file_path)
        return {"valid": True, "data": data, "errors": []}
    except (ValueError, FileNotFoundError) as e:
        return {"valid": False, "data": None, "errors": [str(e)]}
    except Exception as e:
        return {"valid": False, "data": None, "errors": [f"Unexpected error: {e}"]}


def check_idempotency_ready(data: dict) -> dict:
    """
    Checks if the bank report's Id column is suitable for idempotency.
    Scans BOTH matched_paid_lines and unmatched_lines together.

    Returns a dict with:
        - "all_have_id": bool
        - "blank_rows": dict with sheet_name -> list of row numbers
        - "unique_ids": bool
        - "duplicates": list of duplicate Id values
    """
    matched_lines = data.get("matched_paid_lines", [])
    unmatched_lines = data.get("unmatched_lines", [])

    results = {
        "all_have_id": True,
        "blank_rows": {"matched_paid_driver_lines": [], "unmatched_lines": []},
        "unique_ids": True,
        "duplicates": [],
    }

    # Check Matched Paid Driver Lines
    for i, row in enumerate(matched_lines, start=2):
        line_id = row.get("Id")
        if line_id is None or str(line_id).strip() == "":
            results["all_have_id"] = False
            results["blank_rows"]["matched_paid_driver_lines"].append(i)

    # Check Unmatched Lines
    for i, row in enumerate(unmatched_lines, start=2):
        line_id = row.get("Id")
        if line_id is None or str(line_id).strip() == "":
            results["all_have_id"] = False
            results["blank_rows"]["unmatched_lines"].append(i)

    # Collect ALL IDs (from both sheets) to check for duplicates across the entire report
    all_ids = []
    for row in matched_lines:
        line_id = row.get("Id")
        if line_id is not None and str(line_id).strip() != "":
            all_ids.append(line_id)
    for row in unmatched_lines:
        line_id = row.get("Id")
        if line_id is not None and str(line_id).strip() != "":
            all_ids.append(line_id)

    if len(all_ids) != len(set(all_ids)):
        results["unique_ids"] = False
        seen = set()
        duplicates = set()
        for line_id in all_ids:
            if line_id in seen:
                duplicates.add(line_id)
            seen.add(line_id)
        results["duplicates"] = list(duplicates)

    return results


# ============================================================
# Quick test when run directly
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python bank_report_parser.py <path_to_excel_file>")
        sys.exit(1)

    file_path = sys.argv[1]
    print(f"📂 Parsing: {file_path}\n")

    try:
        # 1. Parse the file
        result = parse_bank_report(file_path)

        print("✅ Parsing successful!\n")

        # 2. Summary stats
        print(f"  Sheets found: {result['sheets_found']}")
        print(f"  Driver Type Summary rows: {len(result['summary'])}")
        print(f"  Unmatched Lines rows: {len(result['unmatched_lines'])}")
        print(f"  Week Breakdown rows: {len(result['week_breakdown_raw'].get('rows', []))}")
        print(f"  Matched Paid Driver Lines rows: {len(result['matched_paid_lines'])}")

        # 3. Idempotency check (now covers both sheets)
        print("\n🔍 Idempotency Check:")
        id_check = check_idempotency_ready(result)

        if not id_check["all_have_id"]:
            for sheet, rows in id_check["blank_rows"].items():
                if rows:
                    print(f"  ⚠️ WARNING: {sheet} has {len(rows)} blank Id values at rows: {rows}")
        else:
            print("  ✅ All rows across both sheets have an Id value.")

        if not id_check["unique_ids"]:
            print(f"  ⚠️ WARNING: Duplicate Id values found across the report: {id_check['duplicates']}")
        else:
            print("  ✅ All Id values are unique across both sheets.")

        # 4. Sample from Matched Paid Driver Lines
        if result["matched_paid_lines"]:
            print("\n📄 Sample Matched Paid Driver Lines (first 2 rows):")
            for idx, row in enumerate(result["matched_paid_lines"][:2], 1):
                print(f"\n  Row {idx}:")
                print(f"    Id: {row.get('Id')}")
                print(f"    DriverReference: {row.get('DriverReference')}")
                print(f"    WeekNumber: {row.get('WeekNumber')}")
                print(f"    BeneficiaryAmount: {row.get('BeneficiaryAmount')}")
                print(f"    DriverInvoiceId: {row.get('DriverInvoiceId')}")
                print(f"    PaymentRef: {row.get('PaymentRef')}")

        # 5. Sample Unmatched Lines (CRITICAL: Check NaN -> None fix)
        if result["unmatched_lines"]:
            print("\n📄 Sample Unmatched Lines (first row):")
            row = result["unmatched_lines"][0]
            print(f"    BeneficiaryName: {row.get('BeneficiaryName')}")
            print(f"    BeneficiaryAmount: {row.get('BeneficiaryAmount')}")
            print(f"    Note: {row.get('Note')}")  # Should print "None", not "nan"

        # 6. JSON Serialization Test (STRICT: no default=str)
        print("\n🔬 JSON Serialization Test:")
        try:
            # allow_nan=False will raise ValueError if any NaN is present
            # No default=str so dates must be normalized
            json_string = json.dumps(result, allow_nan=False)
            print("  ✅ JSON serialization PASSED (no NaN values found).")
        except ValueError as e:
            print(f"  ❌ JSON serialization FAILED: {e}")
            print("     The parser still contains NaN values. Fix them before proceeding.")
            sys.exit(1)
        except TypeError as e:
            print(f"  ❌ JSON serialization FAILED due to unsupported type: {e}")
            print("     This likely means a pandas Timestamp or other non-serializable object slipped through.")
            print("     Check the _normalize_dates() function.")
            sys.exit(1)

        # 7. Final recommendation
        print("\n" + "=" * 60)
        if id_check["all_have_id"] and id_check["unique_ids"]:
            print("✅ Parser ready — Id column is suitable for idempotency.")
            print("   Proceed to build the processor.")
        else:
            print("⚠️ Idempotency issues detected. Review warnings above.")
            print("   The processor will need to handle these edge cases.")

    except FileNotFoundError as e:
        print(f"❌ File not found: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"❌ Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)