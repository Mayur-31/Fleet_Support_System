"""
Backs up payment_runs and job_expenses to a local timestamped JSON
file before any cleanup deletion. Read-only against the database —
writes only to a local file. Run this before delete_payment_runs.py.

Usage:
    python backup_before_cleanup.py
"""
import json
import os
from datetime import datetime

from database import supabase


def main():
    os.makedirs("backups", exist_ok=True)

    runs = supabase.table("payment_runs").select("*").execute().data
    expenses = supabase.table("job_expenses").select("*").execute().data

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"backups/payment_data_backup_{timestamp}.json"

    with open(backup_path, "w") as f:
        json.dump({"payment_runs": runs, "job_expenses": expenses}, f, indent=2, default=str)

    print(f"Backed up {len(runs)} payment_runs and {len(expenses)} job_expenses rows to {backup_path}")


if __name__ == "__main__":
    main()