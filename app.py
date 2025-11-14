from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3, os, math
from datetime import datetime, date, timedelta
import config, smtplib

DB_PATH = os.path.join(os.getcwd(), "leave.db")

app = Flask(__name__)
app.secret_key = 'change-this-secret-in-prod'

# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    seed_needed = not os.path.exists(DB_PATH)
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        role TEXT,
        join_date TEXT,
        entitlement INTEGER,
        current_balance REAL DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_name TEXT,
        leave_type TEXT,
        start_date TEXT,
        end_date TEXT,
        days REAL,
        status TEXT,
        reason TEXT,
        applied_on TEXT
    )''')

    conn.commit()

    if seed_needed:
        for emp in config.EMPLOYEES:
            c.execute(
                "INSERT OR IGNORE INTO employees (name, role, join_date, entitlement, current_balance) VALUES (?, ?, ?, ?, ?)",
                (emp['name'], emp['role'], emp['join_date'], emp['entitlement'], 0)
            )
        conn.commit()

    conn.close()

# ---------------- Accrual ----------------
def calc_accrual_for_year(emp_row, year):
    pattern = None
    ent = None
    for e in config.EMPLOYEES:
        if e['name'] == emp_row['name']:
            pattern = e['accrual_pattern']
            ent = e['entitlement']
            break

    if pattern is None:
        pattern = {m: 2 for m in range(1, 13)}
        ent = 24

    join_date = datetime.strptime(emp_row['join_date'], '%Y-%m-%d').date()
    total = 0.0

    for m in range(1, 13):
        month_start = date(year, m, 1)
        if month_start < join_date.replace(day=1):
            continue
        if year < config.SYSTEM_START_YEAR:
            continue
        total += float(pattern.get(m, 0))

    if ent is not None:
        total = min(total, float(ent))

    return round(total, 2)

def update_all_balances():
    conn = get_db()
    c = conn.cursor()

    emps = c.execute("SELECT * FROM employees").fetchall()
    for e in emps:
        accr = calc_accrual_for_year(e, config.SYSTEM_START_YEAR)
        c.execute("UPDATE employees SET current_balance=? WHERE id=?", (accr, e['id']))

    conn.commit()
    conn.close()

# ---------------- Utilities ----------------
def calc_days_inclusive(start, end):
    if end < start:
        return 0
    return (end - start).days + 1

def send_email(subject, body):
    if not config.ENABLE_EMAIL:
        return
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(config.ADMIN_EMAIL, "YOUR_PASSWORD")
        msg = f"Subject: {subject}\n\n{body}"
        server.sendmail(config.ADMIN_EMAIL, config.ADMIN_EMAIL, msg)
        server.quit()
    except Exception as e:
        print("Email error:", e)

# ---------------- Initialize DB under gunicorn (Render) ----------------
with app.app_context():
    try:
        init_db()
        update_all_balances()
        print("Database initialized on Render.")
    except Exception as e:
        print("DB init error:", e)

# ---------------- Routes ----------------
@app.route('/')
def home():
    return redirect(url_for('apply'))

@app.route('/balance/<name>')
def balance(name):
    conn = get_db()
    row = conn.execute("SELECT current_balance FROM employees WHERE name=?", (name,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'balance': 0})
    return jsonify({'balance': round(row['current_balance'], 2)})

@app.route('/apply', methods=['GET','POST'])
def apply():
    conn = get_db()
    employees = conn.execute("SELECT name FROM employees ORDER BY name").fetchall()
    conn.close()

    if request.method == 'POST':
        emp = request.form['employee']
        ltype = request.form['leave_type']
        s = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
        e = datetime.strptime(request.form['end_date'], '%Y-%m-%d').date()

        half = request.form.get('half') == 'on'
        days = calc_days_inclusive(s, e)
        if half:
            days -= 0.5

        conn = get_db()
        bal_row = conn.execute("SELECT current_balance FROM employees WHERE name=?", (emp,)).fetchone()
        bal = bal_row['current_balance'] if bal_row else 0
        warning = bal < days

        conn.execute("""
            INSERT INTO leave_requests
            (employee_name, leave_type, start_date, end_date, days, status, reason, applied_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (emp, ltype, s.isoformat(), e.isoformat(), days, 'Pending',
              request.form.get('reason',''), datetime.now().isoformat()))
        conn.commit()
        conn.close()

        if warning:
            flash(f"Warning: Applying {days} days but only {bal} available.", "warning")
        else:
            flash("Leave applied successfully!", "success")

        send_email("New Leave Request", f"{emp} applied for {days} days ({ltype})")
        return redirect(url_for('apply'))

    return render_template('apply_leave.html', employees=employees)

@app.route('/history/<name>')
def history(name):
    conn = get_db()
    leaves = conn.execute("SELECT * FROM leave_requests WHERE employee_name=? ORDER BY applied_on DESC", (name,)).fetchall()
    conn.close()
    return render_template('history.html', leaves=leaves, name=name)

@app.route('/admin')
def admin():
    conn = get_db()
    leaves = conn.execute("SELECT * FROM leave_requests ORDER BY applied_on DESC").fetchall()
    emps = conn.execute("SELECT * FROM employees ORDER BY name").fetchall()
    conn.close()
    return render_template('admin_dashboard.html', leaves=leaves, employees=emps)

@app.route('/approve/<int:lid>')
def approve(lid):
    conn = get_db()
    lr = conn.execute("SELECT * FROM leave_requests WHERE id=?", (lid,)).fetchone()
    if lr and lr['status'] == 'Pending':
        conn.execute("UPDATE leave_requests SET status='Approved' WHERE id=?", (lid,))
        conn.execute("UPDATE employees SET current_balance = current_balance - ? WHERE name=?",
                     (lr['days'], lr['employee_name']))
        conn.commit()
    conn.close()
    flash("Leave approved", "success")
    return redirect(url_for('admin'))

@app.route('/reject/<int:lid>')
def reject(lid):
    conn = get_db()
    conn.execute("UPDATE leave_requests SET status='Rejected' WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    flash("Leave rejected", "info")
    return redirect(url_for('admin'))

@app.route('/update_entitlement', methods=['POST'])
def update_entitlement():
    name = request.form['name']
    new_ent = request.form['entitlement']
    try:
        ent_val = int(new_ent)
    except:
        ent_val = None
    conn = get_db()
    conn.execute("UPDATE employees SET entitlement=? WHERE name=?", (ent_val, name))
    conn.commit()
    conn.close()
    flash("Entitlement updated.", "info")
    return redirect(url_for('admin'))
