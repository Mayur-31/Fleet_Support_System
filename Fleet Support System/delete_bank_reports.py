"""
Delete a bank report and optionally revert linked expenses to 'unpaid'.
Usage:
    python delete_bank_reports.py --report-id <uuid> [--revert-expenses] [--confirm]
"""

import sys
import uuid
import argparse
from database import supabase

def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def delete_bank_report(report_id, revert_expenses=False, confirm=False):
    if not is_valid_uuid(report_id):
        print("❌ Invalid report ID.")
        return

    # Check if report exists
    resp = supabase.table('bank_reports').select('id, week_number, filename').eq('id', report_id).execute()
    if not resp.data:
        print(f"❌ Bank report {report_id} not found.")
        return

    report = resp.data[0]
    print(f"📄 Report: {report['filename']} (Week {report['week_number']})")

    if not confirm:
        print("⚠️  Dry run – add --confirm to actually delete.")
        return

    # (Optional) Revert expenses linked to matched lines
    if revert_expenses:
        # Get all matched expense IDs from lines of this report
        lines_resp = supabase.table('bank_report_lines') \
            .select('matched_job_expense_id') \
            .eq('bank_report_id', report_id) \
            .not_.is_('matched_job_expense_id', 'null') \
            .execute()
        expense_ids = [row['matched_job_expense_id'] for row in lines_resp.data]

        if expense_ids:
            print(f"🔄 Reverting {len(expense_ids)} expenses to 'unpaid'...")
            supabase.table('job_expenses') \
                .update({'status': 'unpaid'}) \
                .in_('id', expense_ids) \
                .execute()
            print("✅ Expenses reverted.")

    # Delete lines (cascade will happen if FK is set, but we delete explicitly)
    print("🗑️  Deleting bank report lines...")
    supabase.table('bank_report_lines').delete().eq('bank_report_id', report_id).execute()

    print("🗑️  Deleting bank report...")
    supabase.table('bank_reports').delete().eq('id', report_id).execute()

    print(f"✅ Bank report {report_id} deleted successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-id', required=True, help='UUID of the bank report to delete')
    parser.add_argument('--revert-expenses', action='store_true', help='Set linked expenses to unpaid')
    parser.add_argument('--confirm', action='store_true', help='Actually perform deletion')
    args = parser.parse_args()

    delete_bank_report(args.report_id, args.revert_expenses, args.confirm)