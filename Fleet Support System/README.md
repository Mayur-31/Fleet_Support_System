# Fleet Support Payments System

Replaces the manual Expenses spreadsheet → back-office → Faster Payments
CSV workflow with a Supabase-backed system.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your real Supabase project
   URL and service_role key. **Never commit `.env` or paste its contents
   anywhere.**
3. Confirm the connection works:
   ```
   python test_connection.py
   ```

## Database schema

Four tables, created via the Supabase SQL Editor:

- `drivers` — driver_code, name
- `driver_bank_details` — driver_id (FK), account_number, sort_code
  (RLS-restricted to service_role only)
- `payment_runs` — one row per weekly batch
- `job_expenses` — one row per driver per payment run, FK to both
  `drivers` and `payment_runs`

All four tables should have Row Level Security enabled with a
service_role-only policy, since this data includes live bank details.

## Weekly workflow

**Step 1 — preview the import (writes nothing):**
```
python import_expenses.py --file data/EXPENSES_FILE.xlsx --week 30
```
Review the printed totals and check for any "driver code not found"
warnings before continuing.

**Step 2 — commit the import for real:**
```
python import_expenses.py --file data/EXPENSES_FILE.xlsx --week 30 --commit
```
This creates a `payment_runs` row and one `job_expenses` row per matched
driver. Note the `payment_run id` it prints.

**Step 3 — generate the bank CSV:**
```
python generate_bank_csv.py --run-id <payment_run_id>
```
Writes `output/FasterPayments_Week{N}.csv`. Drivers with no bank details
on file are skipped and listed as a warning, not silently dropped.

Steps 2 and 3 can also be run together:
```
python process_payments.py --file data/EXPENSES_FILE.xlsx --week 30 --commit
```

## Known open items

- **Reference number format**: the generated CSV uses a placeholder
  reference (`WK{week}-{driver_code}`). The original sample used what
  looks like a sequential invoice number plus week/year — confirm the
  real numbering rule with Charlotte or the bank before this is used
  for an actual payment run.
- **Drivers without bank details**: this is expected for drivers not
  being paid this week. Only add bank details once confirmed by the
  business (Charlotte, the old back-office data, or a driver portal) —
  don't guess or backfill them.
- **Driver portal question**: whether drivers keep separate self-service
  access to update their own bank details once the back office is
  retired is still unconfirmed.

## Project structure

```
fleet-support-system/
├── .env.example
├── .gitignore
├── requirements.txt
├── config.py              # loads and validates env vars
├── database.py            # shared Supabase client
├── test_connection.py     # connection sanity check
├── import_expenses.py     # reads Expenses.xlsx, validates, commits
├── generate_bank_csv.py   # exports the Faster Payments CSV
├── process_payments.py    # runs import + export together
├── data/                  # place weekly Expenses files here (gitignored)
└── output/                # generated CSVs land here (gitignored)
```
