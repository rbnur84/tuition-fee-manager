from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests
import qrcode
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, redirect, render_template, request,
    send_file, url_for, Response, session
)

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", str(INSTANCE_DIR / "tuition_fee.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-this-local-app-secret")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            guardian TEXT DEFAULT '',
            monthly_fee INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL REFERENCES students(student_id),
            fee_month TEXT NOT NULL,
            amount INTEGER NOT NULL,
            paid_at TEXT NOT NULL,
            UNIQUE(student_id, fee_month)
        );
        CREATE TABLE IF NOT EXISTS sms_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            fee_month TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            response TEXT DEFAULT '',
            sent_at TEXT NOT NULL,
            UNIQUE(student_id, fee_month, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_payments_month ON payments(fee_month);
        CREATE INDEX IF NOT EXISTS idx_sms_month_kind ON sms_logs(fee_month, kind);
        """)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def valid_month(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""))


def pretty_month(value: str) -> str:
    try:
        y, m = value.split("-")
        return f"{MONTH_NAMES[int(m) - 1]} {y}"
    except Exception:
        return value


def bd_phone(value: str) -> str:
    return re.sub(r"[\s-]", "", value or "")


def sms_settings() -> dict:
    return {
        "url": os.getenv("SMS_API_URL", "").strip(),
        "key": os.getenv("SMS_API_KEY", "").strip(),
        "sender": os.getenv("SMS_SENDER_ID", "").strip(),
    }


def sms_configured() -> bool:
    cfg = sms_settings()
    return bool(cfg["url"] and cfg["key"])


def send_sms(phone: str, message: str) -> tuple[str, str]:
    """Generic JSON SMS gateway adapter. Exact payload may need provider-specific mapping."""
    cfg = sms_settings()
    if not cfg["url"] or not cfg["key"]:
        return "not_configured", "SMS gateway is not configured"

    payload = {
        os.getenv("SMS_API_KEY_FIELD", "api_key"): cfg["key"],
        os.getenv("SMS_SENDER_FIELD", "senderid"): cfg["sender"],
        os.getenv("SMS_NUMBER_FIELD", "number"): bd_phone(phone),
        os.getenv("SMS_MESSAGE_FIELD", "message"): message,
    }
    headers = {"Accept": "application/json"}
    auth_header = os.getenv("SMS_AUTH_HEADER", "").strip()
    if auth_header:
        prefix = os.getenv("SMS_AUTH_PREFIX", "Bearer").strip()
        headers[auth_header] = f"{prefix} {cfg['key']}".strip()
        if os.getenv("SMS_AUTH_HEADER_ONLY", "0") == "1":
            payload.pop(os.getenv("SMS_API_KEY_FIELD", "api_key"), None)

    try:
        response = requests.post(cfg["url"], json=payload, headers=headers, timeout=20)
        body = response.text[:1000]
        if 200 <= response.status_code < 300:
            return "sent", f"HTTP {response.status_code}: {body}"
        return "failed", f"HTTP {response.status_code}: {body}"
    except requests.RequestException as exc:
        return "failed", str(exc)[:1000]


def log_and_send(student: sqlite3.Row, fee_month: str, kind: str, message: str,
                 allow_existing_retry: bool = False) -> str:
    """A kind/month/student combination is attempted at most once automatically."""
    with get_db() as db:
        existing = db.execute(
            "SELECT status FROM sms_logs WHERE student_id=? AND fee_month=? AND kind=?",
            (student["student_id"], fee_month, kind)
        ).fetchone()
        if existing and not allow_existing_retry:
            return existing["status"]
        if existing and allow_existing_retry:
            db.execute("DELETE FROM sms_logs WHERE student_id=? AND fee_month=? AND kind=?",
                       (student["student_id"], fee_month, kind))

        status, response_text = send_sms(student["phone"], message)
        db.execute("""INSERT INTO sms_logs
            (student_id, phone, fee_month, kind, message, status, response, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (student["student_id"], student["phone"], fee_month, kind,
             message, status, response_text, now_text()))
        return status


def confirmation_message(student: sqlite3.Row, fee_month: str, amount: int) -> str:
    return (f"প্রিয় অভিভাবক, {student['name']}-এর {pretty_month(fee_month)} মাসের "
            f"টিউশন ফি {amount} টাকা গ্রহণ করা হয়েছে। ধন্যবাদ।")


def reminder_message(student: sqlite3.Row, fee_month: str) -> str:
    return (f"প্রিয় অভিভাবক, {student['name']}-এর {pretty_month(fee_month)} মাসের "
            "টিউশন ফি এখনো বকেয়া রয়েছে। অনুগ্রহ করে পরিশোধ করুন। ধন্যবাদ।")


def process_due_reminders(force_month: Optional[str] = None) -> int:
    """Run once per unpaid student per month, on/after the 15th."""
    today = date.today()
    fee_month = force_month or today.strftime("%Y-%m")
    if not valid_month(fee_month):
        return 0
    if force_month is None and today.day < 15:
        return 0
    if not sms_configured():
        return 0

    with get_db() as db:
        unpaid = db.execute("""
            SELECT s.* FROM students s
            LEFT JOIN payments p
              ON p.student_id=s.student_id AND p.fee_month=?
            WHERE s.active=1 AND p.id IS NULL
            ORDER BY s.name COLLATE NOCASE
        """, (fee_month,)).fetchall()

    sent_count = 0
    for student in unpaid:
        with get_db() as db:
            prior = db.execute(
                "SELECT id FROM sms_logs WHERE student_id=? AND fee_month=? AND kind='reminder'",
                (student["student_id"], fee_month)
            ).fetchone()
        if prior:
            continue
        status = log_and_send(student, fee_month, "reminder", reminder_message(student, fee_month))
        if status == "sent":
            sent_count += 1
    return sent_count


def reminder_worker() -> None:
    while True:
        try:
            process_due_reminders()
        except Exception as exc:
            app.logger.exception("Reminder scheduler error: %s", exc)
        time.sleep(60)


init_db()
# A single Gunicorn worker is recommended so this scheduler is not duplicated.
threading.Thread(target=reminder_worker, daemon=True, name="tuition-reminder-scheduler").start()


@app.before_request
def require_admin_login():
    """Protect hosted student records when an ADMIN_PASSWORD is configured."""
    public_endpoints = {"login", "static", "manifest", "service_worker", "health"}
    if request.endpoint in public_endpoints:
        return None
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    require_login = os.getenv("REQUIRE_LOGIN", "0").lower() in {"1", "true", "yes"}
    if require_login and not password:
        return ("Server setup incomplete: set ADMIN_PASSWORD in hosting environment.", 503)
    if password and not session.get("admin_logged_in"):
        return redirect(url_for("login", next=request.full_path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    configured = os.getenv("ADMIN_PASSWORD", "").strip()
    if not configured:
        flash("ADMIN_PASSWORD সেট করা নেই। Hosting-এর Environment Variables-এ একটি শক্তিশালী পাসওয়ার্ড সেট করো।", "warning")
        return render_template("login.html", setup_missing=True)
    if request.method == "POST":
        from hmac import compare_digest
        if compare_digest(request.form.get("password", ""), configured):
            session["admin_logged_in"] = True
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            return redirect(target)
        flash("পাসওয়ার্ড সঠিক নয়।", "danger")
    return render_template("login.html", setup_missing=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return {"status": "ok", "app": "tuition-fee-manager"}


@app.route("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest")


@app.route("/service-worker.js")
def service_worker():
    response = app.send_static_file("service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.context_processor
def inject_globals():
    return {
        "current_month": datetime.now().strftime("%Y-%m"),
        "pretty_month": pretty_month,
        "sms_ready": sms_configured(),
        "today_text": date.today().strftime("%d %B %Y"),
    }


@app.route("/")
def dashboard():
    fee_month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    if not valid_month(fee_month):
        fee_month = datetime.now().strftime("%Y-%m")
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) n FROM students WHERE active=1").fetchone()["n"]
        paid = db.execute("""SELECT COUNT(*) n FROM payments p
            JOIN students s ON s.student_id=p.student_id
            WHERE p.fee_month=? AND s.active=1""", (fee_month,)).fetchone()["n"]
        amount = db.execute("""SELECT COALESCE(SUM(amount),0) n FROM payments p
            JOIN students s ON s.student_id=p.student_id
            WHERE p.fee_month=? AND s.active=1""", (fee_month,)).fetchone()["n"]
        unpaid = db.execute("""SELECT s.* FROM students s
            LEFT JOIN payments p ON p.student_id=s.student_id AND p.fee_month=?
            WHERE s.active=1 AND p.id IS NULL ORDER BY s.name COLLATE NOCASE LIMIT 8""",
            (fee_month,)).fetchall()
        recent = db.execute("""SELECT p.*,s.name FROM payments p
            JOIN students s ON s.student_id=p.student_id
            WHERE p.fee_month=? ORDER BY p.paid_at DESC LIMIT 8""", (fee_month,)).fetchall()
        reminders = db.execute("SELECT COUNT(*) n FROM sms_logs WHERE fee_month=? AND kind='reminder' AND status='sent'",
                               (fee_month,)).fetchone()["n"]
    return render_template("dashboard.html", fee_month=fee_month, total=total,
                           paid=paid, unpaid_count=max(total-paid, 0), amount=amount,
                           unpaid=unpaid, recent=recent, reminders=reminders)


@app.route("/students")
def students():
    query = request.args.get("q", "").strip()
    with get_db() as db:
        if query:
            rows = db.execute("""SELECT * FROM students
                WHERE student_id LIKE ? OR name LIKE ? OR phone LIKE ?
                ORDER BY active DESC, name COLLATE NOCASE""",
                (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
        else:
            rows = db.execute("SELECT * FROM students ORDER BY active DESC, name COLLATE NOCASE").fetchall()
    return render_template("students.html", students=rows, query=query)


@app.route("/students/new", methods=["GET", "POST"])
def student_new():
    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        name = request.form.get("name", "").strip()
        phone = bd_phone(request.form.get("phone", ""))
        guardian = request.form.get("guardian", "").strip()
        try:
            fee = int(request.form.get("monthly_fee", "0"))
        except ValueError:
            fee = -1
        if not student_id or not name or not phone or fee < 0:
            flash("Student ID, নাম, মোবাইল নম্বর এবং সঠিক মাসিক ফি দিতে হবে।", "danger")
            return render_template("student_form.html", student=request.form, editing=False)
        try:
            with get_db() as db:
                db.execute("""INSERT INTO students
                    (student_id,name,phone,guardian,monthly_fee,active,created_at)
                    VALUES (?,?,?,?,?,1,?)""",
                    (student_id, name, phone, guardian, fee, now_text()))
            flash("শিক্ষার্থী যোগ হয়েছে। এখন তার QR Code তৈরি করতে পারবে।", "success")
            return redirect(url_for("students"))
        except sqlite3.IntegrityError:
            flash("এই Student ID আগে থেকেই আছে।", "danger")
    return render_template("student_form.html", student=None, editing=False)


@app.route("/students/<student_id>/edit", methods=["GET", "POST"])
def student_edit(student_id: str):
    with get_db() as db:
        student = db.execute("SELECT * FROM students WHERE student_id=?", (student_id,)).fetchone()
    if not student:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = bd_phone(request.form.get("phone", ""))
        guardian = request.form.get("guardian", "").strip()
        try:
            fee = int(request.form.get("monthly_fee", "0"))
        except ValueError:
            fee = -1
        if not name or not phone or fee < 0:
            flash("নাম, মোবাইল নম্বর এবং সঠিক মাসিক ফি দিতে হবে।", "danger")
            return render_template("student_form.html", student=student, editing=True)
        with get_db() as db:
            db.execute("UPDATE students SET name=?,phone=?,guardian=?,monthly_fee=? WHERE student_id=?",
                       (name, phone, guardian, fee, student_id))
        flash("শিক্ষার্থীর তথ্য আপডেট হয়েছে।", "success")
        return redirect(url_for("students"))
    return render_template("student_form.html", student=student, editing=True)


@app.route("/students/<student_id>/toggle", methods=["POST"])
def student_toggle(student_id: str):
    with get_db() as db:
        student = db.execute("SELECT active FROM students WHERE student_id=?", (student_id,)).fetchone()
        if not student:
            abort(404)
        db.execute("UPDATE students SET active=? WHERE student_id=?", (0 if student["active"] else 1, student_id))
    flash("শিক্ষার্থীর Active Status পরিবর্তন হয়েছে।", "success")
    return redirect(url_for("students"))


@app.route("/students/<student_id>/qr.png")
def student_qr(student_id: str):
    with get_db() as db:
        student = db.execute("SELECT student_id FROM students WHERE student_id=?", (student_id,)).fetchone()
    if not student:
        abort(404)
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(student_id)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", download_name=f"{student_id}-qr.png")


@app.route("/scan", methods=["GET", "POST"])
def scan():
    student = None
    fee_month = request.values.get("fee_month", datetime.now().strftime("%Y-%m"))
    student_id = request.values.get("student_id", "").strip()
    if not valid_month(fee_month):
        fee_month = datetime.now().strftime("%Y-%m")
    if request.method == "POST" and student_id:
        with get_db() as db:
            student = db.execute("SELECT * FROM students WHERE student_id=?", (student_id,)).fetchone()
            if student:
                paid = db.execute("SELECT * FROM payments WHERE student_id=? AND fee_month=?",
                                  (student_id, fee_month)).fetchone()
                student = dict(student)
                student["payment"] = dict(paid) if paid else None
            else:
                flash("এই ID পাওয়া যায়নি। আগে শিক্ষার্থীকে Student List-এ যোগ করো।", "danger")
    return render_template("scan.html", student=student, student_id=student_id, fee_month=fee_month)


@app.route("/payment", methods=["POST"])
def payment():
    student_id = request.form.get("student_id", "").strip()
    fee_month = request.form.get("fee_month", "")
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = -1
    if not valid_month(fee_month) or amount < 0:
        flash("মাস অথবা টাকার পরিমাণ সঠিক নয়।", "danger")
        return redirect(url_for("scan", student_id=student_id, fee_month=fee_month))
    with get_db() as db:
        student = db.execute("SELECT * FROM students WHERE student_id=? AND active=1", (student_id,)).fetchone()
        if not student:
            flash("Active শিক্ষার্থী পাওয়া যায়নি।", "danger")
            return redirect(url_for("scan"))
        existing = db.execute("SELECT id FROM payments WHERE student_id=? AND fee_month=?",
                              (student_id, fee_month)).fetchone()
        if existing:
            flash("এই মাসের বেতন আগে থেকেই Paid করা আছে।", "warning")
            return redirect(url_for("scan", student_id=student_id, fee_month=fee_month))
        db.execute("INSERT INTO payments(student_id,fee_month,amount,paid_at) VALUES(?,?,?,?)",
                   (student_id, fee_month, amount, now_text()))
    status = log_and_send(student, fee_month, "confirmation",
                          confirmation_message(student, fee_month, amount))
    if status == "sent":
        flash(f"পেমেন্ট সম্পন্ন। Confirmation SMS পাঠানো হয়েছে — {pretty_month(fee_month)}।", "success")
    elif status == "not_configured":
        flash("পেমেন্ট সংরক্ষণ হয়েছে। SMS Gateway সেট করা নেই, তাই SMS যায়নি।", "warning")
    else:
        flash("পেমেন্ট সংরক্ষণ হয়েছে, কিন্তু Confirmation SMS পাঠানো যায়নি। SMS Log দেখো।", "warning")
    return redirect(url_for("scan", student_id=student_id, fee_month=fee_month))


@app.route("/reports")
def reports():
    fee_month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    if not valid_month(fee_month):
        fee_month = datetime.now().strftime("%Y-%m")
    with get_db() as db:
        rows = db.execute("""SELECT s.student_id,s.name,s.phone,s.monthly_fee,
            p.amount,p.paid_at FROM students s
            LEFT JOIN payments p ON p.student_id=s.student_id AND p.fee_month=?
            WHERE s.active=1 ORDER BY s.name COLLATE NOCASE""", (fee_month,)).fetchall()
        sms_logs = db.execute("SELECT * FROM sms_logs WHERE fee_month=? ORDER BY id DESC LIMIT 100",
                              (fee_month,)).fetchall()
    return render_template("reports.html", rows=rows, fee_month=fee_month, sms_logs=sms_logs)


@app.route("/reports.csv")
def report_csv():
    fee_month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    if not valid_month(fee_month):
        abort(400)
    with get_db() as db:
        rows = db.execute("""SELECT s.student_id,s.name,s.phone,s.guardian,s.monthly_fee,
            p.amount,p.paid_at FROM students s
            LEFT JOIN payments p ON p.student_id=s.student_id AND p.fee_month=?
            WHERE s.active=1 ORDER BY s.name COLLATE NOCASE""", (fee_month,)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Student ID", "Name", "Phone", "Guardian", "Monthly Fee", "Paid Amount", "Paid At", "Status"])
    for r in rows:
        writer.writerow([r["student_id"], r["name"], r["phone"], r["guardian"], r["monthly_fee"],
                         r["amount"] or "", r["paid_at"] or "", "PAID" if r["paid_at"] else "UNPAID"])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=tuition-{fee_month}.csv"})


@app.route("/import", methods=["GET", "POST"])
def import_students():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("একটি CSV ফাইল নির্বাচন করো।", "danger")
            return redirect(url_for("import_students"))
        try:
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline="")
            reader = csv.DictReader(stream)
            required = {"student_id", "name", "phone", "monthly_fee"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError("CSV header অবশ্যই student_id,name,phone,monthly_fee থাকতে হবে। guardian ঐচ্ছিক।")
            inserted = 0
            with get_db() as db:
                for row in reader:
                    sid = (row.get("student_id") or "").strip()
                    name = (row.get("name") or "").strip()
                    phone = bd_phone(row.get("phone") or "")
                    guardian = (row.get("guardian") or "").strip()
                    if not sid or not name or not phone:
                        continue
                    fee = int(row.get("monthly_fee") or 0)
                    db.execute("""INSERT INTO students(student_id,name,phone,guardian,monthly_fee,active,created_at)
                        VALUES(?,?,?,?,?,1,?)
                        ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,phone=excluded.phone,
                        guardian=excluded.guardian,monthly_fee=excluded.monthly_fee,active=1""",
                        (sid, name, phone, guardian, fee, now_text()))
                    inserted += 1
            flash(f"{inserted} জন শিক্ষার্থীর তথ্য Import/Update হয়েছে।", "success")
        except Exception as exc:
            flash(f"CSV Import করা যায়নি: {exc}", "danger")
        return redirect(url_for("students"))
    return render_template("import.html")


@app.route("/sample.csv")
def sample_csv():
    data = "student_id,name,phone,guardian,monthly_fee\nST-001,Rahim,01700000000,Karim,1000\nST-002,Karima,01800000000,Salma,1200\n"
    return Response("\ufeff" + data, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_students.csv"})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        values = {
            "SMS_API_URL": request.form.get("sms_api_url", "").strip(),
            "SMS_SENDER_ID": request.form.get("sms_sender_id", "").strip(),
        }
        new_api_key = request.form.get("sms_api_key", "").strip()
        existing = {}
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip().strip('"').strip("'")
        existing.update(values)
        # An empty password field means keep the existing API key unchanged.
        if new_api_key:
            existing["SMS_API_KEY"] = new_api_key
        else:
            existing.setdefault("SMS_API_KEY", os.getenv("SMS_API_KEY", ""))
        text = "# Local Tuition Fee Manager SMS settings\n" + "".join(
            f'{k}="{v.replace(chr(34), chr(92)+chr(34))}"\n' for k, v in existing.items()
        )
        ENV_PATH.write_text(text, encoding="utf-8")
        load_dotenv(ENV_PATH, override=True)
        flash("SMS Gateway settings সংরক্ষণ হয়েছে। Provider-এর API format আলাদা হলে adapter update করতে হবে।", "success")
        return redirect(url_for("settings"))
    cfg = sms_settings()
    return render_template("settings.html", cfg=cfg)


@app.route("/sms/retry/<int:log_id>", methods=["POST"])
def retry_sms(log_id: int):
    with get_db() as db:
        log = db.execute("SELECT * FROM sms_logs WHERE id=?", (log_id,)).fetchone()
        if not log:
            abort(404)
        student = db.execute("SELECT * FROM students WHERE student_id=?", (log["student_id"],)).fetchone()
        if not student:
            flash("শিক্ষার্থী পাওয়া যায়নি।", "danger")
            return redirect(url_for("reports", month=log["fee_month"]))
        status = log_and_send(student, log["fee_month"], log["kind"], log["message"], allow_existing_retry=True)
    flash("SMS retry status: " + status, "success" if status == "sent" else "warning")
    return redirect(url_for("reports", month=log["fee_month"]))


@app.route("/backup")
def backup():
    if not DB_PATH.exists():
        abort(404)
    return send_file(DB_PATH, as_attachment=True, download_name="tuition_fee_backup.db")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, use_reloader=False)
