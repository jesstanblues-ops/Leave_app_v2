from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
import sqlite3, os
from datetime import datetime, date
import config, smtplib

DB_PATH = os.path.join(os.getcwd(), "leave.db")
app = Flask(__name__)
app.secret_key = "change-this-secret-in-prod"


# -------------------------
# Database helpers
# -------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    seed_needed = not os.path.exists(DB_PATH)
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            role TEXT,
            join_date TEXT,
            entitlement INTEGER,
            phone TEXT,
            current_balance REAL DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT,
            leave_type TEXT,
            start_date TEXT,
            end_date TEXT,
            days REAL,
            status TEXT,
            reason TEXT,
            applied_on TEXT
        )
    """)

    conn.commit()

    if seed_needed:
        for emp in config.EMPLOYEES:
            c.execute("""
                INSERT OR IGNORE INTO employees
                (name, role, join_date, entitlement, phone, current_balance)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (emp['name'], emp['role'], emp['join_date'],
                  emp.get('entitlement'), emp.get('phone'), 0))
        conn.commit()

    conn.close()


# -------------------------
# Prorated accrual (up to current month)
# -------------------------
def calc_prorated_balance(emp):
    today = date.today()
    year = today.year

    # find accrual pattern for the employee
    pattern = None
    entitlement = emp["entitlement"]
    for e in config.EMPLOYEES:
        if e["name"] == emp["name"]:
            pattern = e.get("accrual_pattern")
            break

    if pattern is None:
        pattern = {m: 2 for m in range(1, 13)}

    join_date = datetime.strptime(emp["join_date"], "%Y-%m-%d").date()
    total = 0.0

    for m in range(1, today.month + 1):
        month_start = date(year, m, 1)
        if month_start < join_date.replace(day=1):
            continue
        if year < config.SYSTEM_START_YEAR:
            continue
        total += float(pattern.get(m, 0))

    if entitlement is not None:
        total = min(total, float(entitlement))

    return round(total, 2)


def update_balances():
    conn = get_db()
    c = conn.cursor()
    emps = c.execute("SELECT * FROM employees").fetchall()
    for e in emps:
        pror = calc_prorated_balance(e)
        c.execute("UPDATE employees SET current_balance=? WHERE id=?", (pror, e['id']))
    conn.commit()
    conn.close()


# -------------------------
# Utilities
# -------------------------
def working_days(start, end):
    if end < start:
        return 0
    return (end - start).days + 1


# -------------------------
# Email (safe: password from env)
# -------------------------
def send_email(subject, body, to=None):
    """
    Sends email. If 'to' is None the email goes to config.ADMIN_EMAIL.
    Requires EMAIL_PASSWORD set in environment.
    """
    if not getattr(config, "ENABLE_EMAIL", False):
        print("Email disabled in config.")
        return

    try:
        smtp_server = getattr(config, "SMTP_SERVER", "smtp.gmail.com")
        smtp_port = getattr(config, "SMTP_PORT", 587)
        password = os.environ.get("EMAIL_PASSWORD")
        if not password:
            print("Email error: EMAIL_PASSWORD env var not set.")
            return

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(config.ADMIN_EMAIL, password)

        recipient = to if to else config.ADMIN_EMAIL
        msg = f"Subject: {subject}\n\n{body}"
        server.sendmail(config.ADMIN_EMAIL, recipient, msg)
        server.quit()
        print(f"Email sent to {recipient}: {subject}")
    except Exception as e:
        print("Email error:", e)


# -------------------------
# Admin login routes
# -------------------------
@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password")
        if pw and os.environ.get("ADMIN_PASSWORD") and pw == os.environ.get("ADMIN_PASSWORD"):
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))
        else:
            error = "Incorrect password"
    return render_template("admin_login.html", error=error)


@app.route("/admin_logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("Logged out.")
    return redirect(url_for("admin_login"))


# -------------------------
# Routes
# -------------------------
@app.route("/")
def home():
    return redirect(url_for("apply_leave"))


@app.route("/balance/<name>")
def balance(name):
    conn = get_db()
    row = conn.execute("SELECT current_balance FROM employees WHERE name=?", (name,)).fetchone()
    conn.close()
    return jsonify({"balance": round(row["current_balance"], 2) if row else 0})


@app.route("/apply", methods=["GET", "POST"])
def apply_leave():
    conn = get_db()
    employees = conn.execute("SELECT name FROM employees ORDER BY name").fetchall()
    conn.close()

    if request.method == "POST":
        emp = request.form["employee"]
        leave_type = request.form["leave_type"]
        s = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
        e = datetime.strptime(request.form["end_date"], "%Y-%m-%d").date()
        half = request.form.get("half", "no") == "yes"
        reason = request.form.get("reason", "")

        days = working_days(s, e)
        if half:
            days -= 0.5

        conn = get_db()
        row = conn.execute("SELECT current_balance FROM employees WHERE name=?", (emp,)).fetchone()
        bal = row["current_balance"] if row else 0
        warning = bal < days

        conn.execute("""
            INSERT INTO leave_requests
            (employee_name, leave_type, start_date, end_date, days, status, reason, applied_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (emp, leave_type, s.isoformat(), e.isoformat(), days, "Pending", reason, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        if warning:
            flash(f"Warning: Applying {days} days but only {bal} available.", "warning")
        else:
            flash("Leave applied.", "success")

        # Email admin only when leave is applied
        send_email("New leave request", f"{emp} applied for {days} days ({leave_type}).")

        return redirect(url_for("apply_leave"))

    return render_template("apply_leave.html", employees=employees)


@app.route("/history/<name>")
def history(name):
    conn = get_db()
    leaves = conn.execute("SELECT * FROM leave_requests WHERE employee_name=? ORDER BY applied_on DESC", (name,)).fetchall()
    conn.close()
    return render_template("history.html", leaves=leaves, name=name)


@app.route("/admin")
def admin():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    leaves = conn.execute("SELECT * FROM leave_requests ORDER BY applied_on DESC").fetchall()
    emps = conn.execute("SELECT * FROM employees ORDER BY name").fetchall()
    conn.close()
    return render_template("admin_dashboard.html", leaves=leaves, employees=emps)


@app.route("/approve/<int:lid>")
def approve(lid):
    conn = get_db()
    lr = conn.execute("SELECT * FROM leave_requests WHERE id=?", (lid,)).fetchone()
    emp = conn.execute("SELECT * FROM employees WHERE name=?", (lr["employee_name"],)).fetchone() if lr else None

    if lr and lr["status"] == "Pending":
        conn.execute("UPDATE leave_requests SET status='Approved' WHERE id=?", (lid,))
        conn.execute("UPDATE employees SET current_balance = current_balance - ? WHERE name=?", (lr["days"], lr["employee_name"]))
        conn.commit()

        # Email employee to claycorp177@gmail.com when approved
        send_email("Leave Approved",
                   f"{lr['employee_name']}'s leave ({lr['start_date']} to {lr['end_date']}) has been APPROVED.",
                   to="claycorp177@gmail.com")

    conn.close()
    flash("Leave approved.", "success")
    return redirect(url_for("admin"))


@app.route("/reject/<int:lid>")
def reject(lid):
    conn = get_db()
    lr = conn.execute("SELECT * FROM leave_requests WHERE id=?", (lid,)).fetchone()
    emp = conn.execute("SELECT * FROM employees WHERE name=?", (lr["employee_name"],)).fetchone() if lr else None

    if lr:
        conn.execute("UPDATE leave_requests SET status='Rejected' WHERE id=?", (lid,))
        conn.commit()

        # Email employee to claycorp177@gmail.com when rejected
        send_email("Leave Rejected",
                   f"{lr['employee_name']}'s leave ({lr['start_date']} to {lr['end_date']}) has been REJECTED.",
                   to="claycorp177@gmail.com")

    conn.close()
    flash("Leave rejected.", "info")
    return redirect(url_for("admin"))


@app.route("/update_entitlement", methods=["POST"])
def update_entitlement():
    name = request.form["name"]
    new_ent = request.form["entitlement"]
    try:
        ent_val = int(new_ent)
    except:
        ent_val = None
    conn = get_db()
    conn.execute("UPDATE employees SET entitlement=? WHERE name=?", (ent_val, name))
    conn.commit()
    conn.close()
    flash("Entitlement updated.", "info")
    return redirect(url_for("admin"))


# -------------------------
# Initialize DB (Render/Gunicorn safe)
# -------------------------
with app.app_context():
    try:
        init_db()
        update_balances()
    except Exception as e:
        print("Init error:", e)


# -------------------------
# Run locally
# -------------------------
if __name__ == "__main__":
    init_db()
    update_balances()
    app.run(debug=True, host="0.0.0.0")
