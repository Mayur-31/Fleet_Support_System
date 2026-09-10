"""
Flask entry point for the Fleet Support Payments web app.

Task 1: home page.
Task 2: upload page.
Task 3: review page with bank-details check.
Task 4: generate payment run + bank CSV, with duplicate-run protection.
Task 5: payment run history page.
Forgot Password / Reset Password: self-service email-based recovery,
added alongside the existing Change Password / Manage Users features
(not yet removed — kept until the new flow is confirmed working).
"""
import csv as csv_module
import os
import uuid

from flask import Flask, render_template, request, flash, redirect, url_for, send_from_directory, session
from flask_wtf import CSRFProtect
from werkzeug.utils import secure_filename

import auth
import email_utils
import generate_bank_csv
import import_expenses
from config import FLASK_SECRET_KEY
from database import supabase

import json
from datetime import datetime, timedelta, timezone
from bank_report_processor import process_bank_report


app = Flask(__name__)

app.config["SECRET_KEY"] = FLASK_SECRET_KEY
csrf = CSRFProtect(app)

# Production cookie hardening -- only takes effect once actually served
# over HTTPS via kamal-proxy; harmless during local http:// testing since
# browsers just won't set the cookie without TLS, which is correct.
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {".xlsx", ".csv"}
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


def allowed_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename)
    return ext.lower() in ALLOWED_EXTENSIONS

PREVIEW_DIR = os.path.join(os.path.dirname(__file__), 'previews')
PREVIEW_EXPIRY_HOURS = 24
os.makedirs(PREVIEW_DIR, exist_ok=True)

def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def store_preview(token, file_path, result, uploaded_by, filename):
    safe_filename = secure_filename(filename) or 'bank_report.xlsx'
    meta = {
        'token': token,
        'filename': safe_filename,
        'uploaded_by': uploaded_by,
        'result': result,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'file_path': file_path
    }
    meta_path = os.path.join(PREVIEW_DIR, f"{token}.json")
    with open(meta_path, 'w') as f:
        json.dump(meta, f)
    return True

def get_preview(token):
    if not is_valid_uuid(token):
        return None
    meta_path = os.path.join(PREVIEW_DIR, f"{token}.json")
    if not os.path.exists(meta_path):
        return None

    try:
        with open(meta_path, 'r') as f:
            meta = json.load(f)
    except (json.JSONDecodeError, TypeError):
        delete_preview(token)
        return None

    required = ['token', 'filename', 'uploaded_by', 'result', 'created_at', 'file_path']
    if not all(k in meta for k in required):
        delete_preview(token)
        return None

    # Secure path containment with error handling
    try:
        preview_root = os.path.realpath(PREVIEW_DIR)
        file_root = os.path.realpath(meta['file_path'])
        if os.path.commonpath([preview_root, file_root]) != preview_root:
            delete_preview(token)
            return None
    except (ValueError, TypeError, OSError):
        delete_preview(token)
        return None

    # Timezone‑aware expiry check
    try:
        created = datetime.fromisoformat(meta['created_at'])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created = created.astimezone(timezone.utc)
    except (ValueError, TypeError):
        delete_preview(token)
        return None

    if datetime.now(timezone.utc) - created > timedelta(hours=PREVIEW_EXPIRY_HOURS):
        delete_preview(token)
        return None

    if not os.path.exists(meta['file_path']):
        delete_preview(token)
        return None

    return meta

def delete_preview(token):
    if not is_valid_uuid(token):
        return
    for ext in ['.json', '.xlsx']:
        path = os.path.join(PREVIEW_DIR, f"{token}{ext}")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

def cleanup_old_previews():
    now = datetime.now(timezone.utc)
    for fname in os.listdir(PREVIEW_DIR):
        if fname.endswith('.json'):
            path = os.path.join(PREVIEW_DIR, fname)
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                created = datetime.fromisoformat(data.get('created_at', '2000-01-01'))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created = created.astimezone(timezone.utc)
                if now - created > timedelta(hours=PREVIEW_EXPIRY_HOURS):
                    token = fname.replace('.json', '')
                    delete_preview(token)
            except Exception:
                app.logger.warning(f"Could not read preview metadata: {path}")
                try:
                    os.remove(path)
                except OSError:
                    pass

# ========== DATETIME FILTER ==========
@app.template_filter('datetime')
def format_datetime(value):
    if value is None:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M')
    return value

@app.get("/up")
def health_check():
    """Kamal-proxy hits this before routing traffic to a new container.
    Deliberately unauthenticated and doesn't touch Supabase -- it should
    reflect whether Flask itself came up, not database reachability."""
    return "OK", 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        next_url = request.args.get("next", "")
        next_url = next_url if auth.is_safe_next_path(next_url) else ""
        return render_template("login.html", next_url=next_url)

    email = request.form.get("email", "")
    password = request.form.get("password", "")
    next_url = request.form.get("next", "")
    safe_next = next_url if auth.is_safe_next_path(next_url) else ""

    user = auth.verify_login(email, password)
    if not user:
        flash("Incorrect email or password.", "danger")
        return redirect(url_for("login", next=safe_next))

    session["user_id"] = user["id"]
    session["user_name"] = user["full_name"]
    session["role"] = user["role"]

    return redirect(safe_next or url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = request.form.get("email", "").strip()

    # Always the same message regardless of whether the email exists —
    # this is what prevents the page being used to check which emails
    # are registered. The actual different-behavior-or-not happens
    # below, invisibly to the response.
    if email:
        raw_token = auth.create_reset_token(email)
        if raw_token:
            reset_link = url_for("reset_password", token=raw_token, _external=True)
            email_utils.send_password_reset_email(email, reset_link)

    flash(
        "If an account exists for this email address, you will receive a password reset link.",
        "info",
    )
    return redirect(url_for("login"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if request.method == "GET":
        # Verified here (read-only, doesn't mark used) so a dead/expired
        # link bounces to login immediately instead of showing a form
        # that will just fail on submit.
        if not auth.verify_reset_token(token):
            flash("This reset link is invalid or has expired. Please request a new one.", "danger")
            return redirect(url_for("forgot_password"))
        return render_template("reset_password.html", token=token)

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    success, message = auth.reset_password_with_token(token, new_password, confirm_password)
    flash(message, "success" if success else "danger")

    if success:
        return redirect(url_for("login"))
    return redirect(url_for("reset_password", token=token))

@app.route("/admin/users")
@auth.admin_required
def admin_users():
    users = auth.list_users()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/new", methods=["GET", "POST"])
@auth.admin_required
def admin_create_user():
    if request.method == "GET":
        return render_template("admin_create_user.html")

    full_name = request.form.get("full_name", "")
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    success, message = auth.create_user(full_name, email, password, confirm_password)
    flash(message, "success" if success else "danger")

    if success:
        return redirect(url_for("admin_users"))
    return redirect(url_for("admin_create_user"))


@app.route("/admin/users/<user_id>/toggle-active", methods=["POST"])
@auth.admin_required
def admin_toggle_user_active(user_id):
    if user_id == session.get("user_id"):
        flash("You can't deactivate your own account.", "danger")
        return redirect(url_for("admin_users"))

    new_state = request.form.get("active") == "true"
    updated = auth.set_user_active(user_id, new_state)
    if updated:
        flash("User updated.", "success")
    else:
        flash("That user could not be found — no changes made.", "danger")
    return redirect(url_for("admin_users"))


@app.route("/")
@auth.login_required
def home():
    return render_template("home.html")


@app.route("/upload", methods=["GET", "POST"])
@auth.login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html", results=None)

    week_number = request.form.get("week_number", "").strip()
    file = request.files.get("expenses_file")

    if not week_number or not week_number.isdigit():
        flash("Please enter a valid week number.", "danger")
        return redirect(url_for("upload"))

    if not file or file.filename == "":
        flash("Please choose an Excel file to upload.", "danger")
        return redirect(url_for("upload"))

    if not allowed_file(file.filename):
        flash("Only .xlsx or .csv files are supported.", "danger")
        return redirect(url_for("upload"))

    safe_name = secure_filename(file.filename)
    saved_name = f"{uuid.uuid4().hex}_{safe_name}"
    saved_path = os.path.join(UPLOAD_FOLDER, saved_name)
    file.save(saved_path)

    try:
        weekly = import_expenses.load_and_aggregate(saved_path)
        matched, unmatched = import_expenses.match_drivers(weekly)
    except ValueError as e:
        flash(f"Could not read this file: {e}", "danger")
        return redirect(url_for("upload"))
    except Exception:
        flash("Something went wrong reading this file. Please check its format and try again.", "danger")
        return redirect(url_for("upload"))

    results = {
        "week_number": week_number,
        "matched": matched,
        "unmatched": unmatched,
        "total": sum(row["amount"] for row in matched),
        "source_file": safe_name,
        "saved_path": saved_path,
    }

    return render_template("upload.html", results=results)


def _reparse_staged_file(saved_path: str):
    weekly = import_expenses.load_and_aggregate(saved_path)
    return import_expenses.match_drivers(weekly)


@app.route("/review", methods=["GET", "POST"])
@auth.login_required
def review():
    if request.method == "GET":
        flash("Please upload an Expenses file first.", "warning")
        return redirect(url_for("upload"))

    saved_path = request.form.get("saved_path", "")
    week_number = request.form.get("week_number", "")

    if not saved_path or not os.path.exists(saved_path):
        flash("The uploaded file could not be found — please upload it again.", "danger")
        return redirect(url_for("upload"))

    try:
        matched, unmatched = _reparse_staged_file(saved_path)
    except Exception:
        flash("Could not re-read the uploaded file. Please upload it again.", "danger")
        return redirect(url_for("upload"))

    if not matched:
        flash("No matched drivers found — nothing to review.", "danger")
        return redirect(url_for("upload"))

    driver_ids = [row["driver_id"] for row in matched]
    bank_records = (
        supabase.table("driver_bank_details")
        .select("driver_id")
        .in_("driver_id", driver_ids)
        .execute()
        .data
    )
    has_bank_details = {row["driver_id"] for row in bank_records}

    for row in matched:
        row["bank_available"] = row["driver_id"] in has_bank_details

    missing_count = sum(1 for row in matched if not row["bank_available"])

    return render_template(
        "review.html",
        matched=matched,
        unmatched=unmatched,
        total=sum(row["amount"] for row in matched),
        missing_count=missing_count,
        week_number=week_number,
        saved_path=saved_path,
    )


@app.route("/generate-payment-run", methods=["GET", "POST"])
@auth.login_required
def generate_payment_run():
    if request.method == "GET":
        flash("Please upload and review an Expenses file first.", "warning")
        return redirect(url_for("upload"))

    saved_path = request.form.get("saved_path", "")
    week_number = request.form.get("week_number", "")
    confirmed = request.form.get("confirm") == "yes"

    if not saved_path or not os.path.exists(saved_path):
        flash("The uploaded file could not be found — please upload it again.", "danger")
        return redirect(url_for("upload"))

    if not week_number.isdigit():
        flash("Missing or invalid week number — please upload again.", "danger")
        return redirect(url_for("upload"))

    try:
        matched, unmatched = _reparse_staged_file(saved_path)
    except Exception:
        flash("Could not re-read the uploaded file. Please upload it again.", "danger")
        return redirect(url_for("upload"))

    if not matched:
        flash("No matched drivers found — nothing to generate.", "danger")
        return redirect(url_for("upload"))

    if not confirmed:
        existing_runs = (
            supabase.table("payment_runs")
            .select("id, status, created_at")
            .eq("week_number", int(week_number))
            .execute()
            .data
        )
        if existing_runs:
            return render_template(
                "confirm_duplicate.html",
                existing_runs=existing_runs,
                week_number=week_number,
                saved_path=saved_path,
            )

    try:
        run_id = import_expenses.commit_to_database(int(week_number), matched)
    except Exception:
        flash("Something went wrong creating the payment run. Nothing was saved.", "danger")
        return redirect(url_for("upload"))

    # ----- Generate CSV using the unified generator -----
    try:
        result = generate_bank_csv.generate_csv(run_id)
    except ValueError as e:
        app.logger.exception(f"CSV generation failed for run {run_id}: {e}")
        flash(
            f"Payment run created (ID: {run_id}) but the CSV could not be generated. "
            "The error has been logged. Please contact support if the problem persists.",
            "danger",
        )
        return redirect(url_for("home"))
    except Exception as e:
        app.logger.exception(f"Unexpected error during CSV generation for run {run_id}: {e}")
        flash(
            f"Payment run created (ID: {run_id}) but an unexpected error occurred. "
            "The error has been logged. Please contact support.",
            "danger",
        )
        return redirect(url_for("home"))

    # Mark the run as generated only after CSV is successfully written
    supabase.table("payment_runs").update({"status": "generated"}).eq("id", run_id).execute()

    return render_template(
        "generate_result.html",
        run_id=run_id,
        week_number=result["week_number"],
        row_count=result["row_count"],
        skipped=result["skipped"],
        csv_filename=result["filename"],
        total=sum(r["amount"] for r in matched),
    )


@app.route("/download/<path:filename>")
@auth.login_required
def download_csv(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename or not os.path.exists(os.path.join("output", safe_name)):
        flash("That file could not be found.", "danger")
        return redirect(url_for("home"))
    return send_from_directory("output", safe_name, as_attachment=True)


@app.route("/payment-runs")
@auth.login_required
def payment_runs():
    runs = (
        supabase.table("payment_runs")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
    )

    for run in runs:
        expected_filename = f"FasterPayments_Week{run['week_number']}_{run['id'][:8]}.csv"
        run["csv_filename"] = expected_filename
        run["csv_exists"] = os.path.exists(os.path.join("output", expected_filename))

    return render_template("payment_runs.html", runs=runs)

@app.route('/bank-reports')
@auth.login_required
@auth.active_user_required
def bank_report_list():
    try:
        resp = supabase.table('bank_reports').select('*').order('uploaded_at', desc=True).execute()
        reports = resp.data if resp.data else []
    except Exception as e:
        app.logger.exception("Failed to fetch bank reports")
        flash('Unable to load bank reports. Please try again.', 'danger')
        reports = []
    return render_template('bank_report_list.html', reports=reports)

@app.route('/bank-reports/upload', methods=['GET', 'POST'])
@auth.login_required
@auth.active_user_required
def bank_report_upload():
    cleanup_old_previews()

    if request.method == 'GET':
        return render_template('bank_report_upload.html')

    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(request.url)

    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(request.url)

    if not file.filename.lower().endswith('.xlsx'):
        flash('Bank report must be an .xlsx file.', 'danger')
        return redirect(request.url)

    user_id = session.get('user_id')
    if not user_id:
        flash('User not authenticated', 'danger')
        return redirect(url_for('login'))

    token = str(uuid.uuid4())
    preview_file_path = os.path.join(PREVIEW_DIR, f"{token}.xlsx")
    file.save(preview_file_path)

    try:
        result = process_bank_report(preview_file_path, user_id, dry_run=True)

        if not result.get('success'):
            errors = result.get('errors') or ['Unknown error']
            flash(f"Preview failed: {errors[0]}", 'danger')
            delete_preview(token)
            return redirect(request.url)

        store_preview(token, preview_file_path, result, user_id, file.filename)
        return render_template('bank_report_preview.html', result=result, token=token)

    except Exception as e:
        app.logger.exception("Bank report upload preview failed")
        flash('An error occurred during preview. Please try again.', 'danger')
        delete_preview(token)
        return redirect(request.url)

@app.route('/bank-reports/confirm', methods=['POST'])
@auth.login_required
@auth.active_user_required
def bank_report_confirm():
    token = request.form.get('token')
    if not token:
        flash('Missing preview token', 'danger')
        return redirect(url_for('bank_report_list'))

    preview = get_preview(token)
    if not preview:
        flash('Preview expired or not found. Please upload again.', 'warning')
        return redirect(url_for('bank_report_upload'))

    user_id = session.get('user_id')
    if preview.get('uploaded_by') != user_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('bank_report_list'))

    file_path = preview['file_path']
    if not os.path.exists(file_path):
        flash('Preview file missing. Please upload again.', 'danger')
        delete_preview(token)
        return redirect(url_for('bank_report_upload'))

    original_result = preview.get('result', {})

    try:
        commit_result = process_bank_report(file_path, user_id, dry_run=False)

        if commit_result.get('success'):
            delete_preview(token)

            total_lines = commit_result.get('total_lines', 0)
            already_processed = commit_result.get('already_processed_count', 0)

            if already_processed == total_lines and total_lines > 0:
                flash(
                    f'This bank report has already been processed. '
                    f'All {total_lines} lines were already present – no new records created.',
                    'info'
                )
                return redirect(url_for('bank_report_list'))

            report_id = commit_result.get('report_id')
            if report_id:
                flash(
                    f'Bank report processed successfully. '
                    f'{commit_result["matched_count"]} matched, '
                    f'{commit_result["not_applicable_count"]} administrative.',
                    'success'
                )
                return redirect(url_for('bank_report_detail', report_id=report_id))
            else:
                flash('Bank report processed, but no report ID was returned.', 'warning')
                return redirect(url_for('bank_report_list'))
        else:
            errors = commit_result.get('errors') or ['Unknown error']
            flash(f"Commit failed: {errors[0]}", 'danger')
            return render_template('bank_report_preview.html', result=commit_result, token=token)

    except Exception as e:
        app.logger.exception("Bank report commit failed")

        # Build a safe error result that still shows the preview
        error_result = dict(original_result)
        error_result['success'] = False
        error_result['errors'] = [
            'An internal error occurred while committing. '
            'DO NOT RETRY until the database state has been checked. '
            'Please review the logs and verify affected expense records.'
        ]
        # Ensure all expected template keys exist
        for key in ['filename', 'week_number', 'matched_count', 'unmatched_count',
                    'amount_mismatch_count', 'not_applicable_count', 'ambiguous_count',
                    'preview_lines']:
            if key not in error_result:
                error_result[key] = None

        flash(
            'Commit failed. Do not retry yet. Please verify the bank report and affected expense records '
            'before attempting another commit.',
            'danger'
        )
        return render_template('bank_report_preview.html', result=error_result, token=token)

@app.route('/bank-reports/<uuid:report_id>')
@auth.login_required
@auth.active_user_required
def bank_report_detail(report_id):
    try:
        report_resp = supabase.table('bank_reports').select('*').eq('id', str(report_id)).execute()
        if not report_resp.data:
            flash('Report not found', 'danger')
            return redirect(url_for('bank_report_list'))
        report = report_resp.data[0]
    except Exception as e:
        app.logger.exception(f"Failed to fetch report {report_id}")
        flash('Unable to load report details.', 'danger')
        return redirect(url_for('bank_report_list'))

    try:
        lines_resp = supabase.table('bank_report_lines').select('*').eq('bank_report_id', str(report_id)).execute()
        lines = lines_resp.data or []
    except Exception as e:
        app.logger.exception(f"Failed to fetch lines for report {report_id}")
        flash('Unable to load report lines.', 'danger')
        lines = []

    return render_template('bank_report_detail.html', report=report, lines=lines)

@app.route("/admin/users/<user_id>/delete", methods=["POST"])
@auth.login_required   # Explicitly check login first
@auth.admin_required   # Then check admin role
def admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("You can't delete your own account.", "danger")
        return redirect(url_for("admin_users"))

    deleted = auth.delete_user(user_id)
    if deleted:
        flash("User deleted successfully.", "success")
    else:
        flash("That user could not be found — no changes made.", "danger")
    return redirect(url_for("admin_users"))

# ========== DELETE PAYMENT RUN ==========
@app.route('/payment-runs/<uuid:run_id>/delete', methods=['POST'])
@auth.login_required
@auth.active_user_required
def delete_payment_run(run_id):
    run_id_str = str(run_id)

    try:
        # 1. Verify the payment run exists
        run_resp = supabase.table('payment_runs') \
            .select('id, week_number') \
            .eq('id', run_id_str) \
            .execute()
        if not run_resp.data:
            flash('Payment run not found.', 'danger')
            return redirect(url_for('payment_runs'))

        week_number = run_resp.data[0]['week_number']

        # 2. Get all expense IDs for this run
        exp_resp = supabase.table('job_expenses') \
            .select('id') \
            .eq('payment_run_id', run_id_str) \
            .execute()
        expense_ids = [row['id'] for row in (exp_resp.data or [])]

        # 3. Check if any expense is referenced by a bank_report_line
        if expense_ids:
            dep_resp = supabase.table('bank_report_lines') \
                .select('matched_job_expense_id') \
                .in_('matched_job_expense_id', expense_ids) \
                .execute()
            if dep_resp.data:
                flash(
                    f'Cannot delete payment run for Week {week_number} because '
                    'some expenses have already been reconciled against a bank report. '
                    'Please delete the associated bank report(s) first.',
                    'danger'
                )
                return redirect(url_for('payment_runs'))

        # 4. Delete job_expenses (now safe)
        supabase.table('job_expenses') \
            .delete() \
            .eq('payment_run_id', run_id_str) \
            .execute()

        # 5. Delete the payment run
        supabase.table('payment_runs') \
            .delete() \
            .eq('id', run_id_str) \
            .execute()

        flash(f'Payment run for Week {week_number} deleted successfully.', 'success')

    except Exception as e:
        # Log the error server‑side
        app.logger.exception(f'Error deleting payment run {run_id_str}: {e}')
        flash(
            'The payment run could not be deleted because it is still '
            'being used by another record. Please check its reconciliation '
            'or contact an administrator.',
            'danger'
        )

    return redirect(url_for('payment_runs'))

# ========== DELETE BANK REPORT ==========
@app.route('/bank-reports/<uuid:report_id>/delete', methods=['POST'])
@auth.login_required
@auth.active_user_required
def delete_bank_report(report_id):
    """
    Delete a bank report and its lines.
    If 'revert_expenses' is checked, set matched job_expenses back to 'unpaid'.
    """
    report_id_str = str(report_id)
    revert = request.form.get('revert_expenses') == 'on'   # checkbox value

    # 1. Verify report exists
    report_resp = supabase.table('bank_reports').select('id, filename, week_number').eq('id', report_id_str).execute()
    if not report_resp.data:
        flash('Bank report not found.', 'danger')
        return redirect(url_for('bank_report_list'))

    # 2. Optionally revert linked expenses to 'unpaid'
    if revert:
        lines_resp = supabase.table('bank_report_lines') \
            .select('matched_job_expense_id') \
            .eq('bank_report_id', report_id_str) \
            .not_.is_('matched_job_expense_id', 'null') \
            .execute()
        expense_ids = [row['matched_job_expense_id'] for row in lines_resp.data]
        if expense_ids:
            supabase.table('job_expenses') \
                .update({'status': 'unpaid'}) \
                .in_('id', expense_ids) \
                .execute()

    # 3. Delete lines (explicit; FK cascade also does this)
    supabase.table('bank_report_lines').delete().eq('bank_report_id', report_id_str).execute()

    # 4. Delete the report
    supabase.table('bank_reports').delete().eq('id', report_id_str).execute()

    flash(f'Bank report "{report_resp.data[0]["filename"]}" (Week {report_resp.data[0]["week_number"]}) deleted successfully.', 'success')
    return redirect(url_for('bank_report_list'))


if __name__ == "__main__":
    app.run(debug=True)