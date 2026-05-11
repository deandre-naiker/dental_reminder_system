from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from datetime import date, timedelta, datetime
import sqlite3
import os
from functools import wraps
import re
import time
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)


# ============================================
# DATABASE CONNECTION
# ============================================
def get_db():
    conn = sqlite3.connect('dental.db', timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def retry_on_lock(max_retries=10, delay=1.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))
                        continue
                    raise
            return None

        return wrapper

    return decorator


# ============================================
# WHATSAPP CONFIGURATION
# ============================================
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"

if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    from twilio.rest import Client

    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    whatsapp_enabled = True
else:
    whatsapp_enabled = False


# ============================================
# DATABASE INITIALIZATION
# ============================================
def init_db():
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT DEFAULT 'staff',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        last_cleaning DATE,
        last_checkup DATE,
        next_cleaning DATE,
        next_checkup DATE,
        reminder_type TEXT DEFAULT 'both',
        whatsapp_joined INTEGER DEFAULT 0,
        notes TEXT,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1
    )''')

    try:
        conn.execute("ALTER TABLE patients ADD COLUMN reminder_type TEXT DEFAULT 'both'")
    except:
        pass

    conn.execute('''CREATE TABLE IF NOT EXISTS reminder_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER,
        patient_name TEXT,
        reminder_type TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        method TEXT,
        status TEXT,
        error_details TEXT,
        sent_by INTEGER
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        user_name TEXT,
        action TEXT,
        details TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER,
        appointment_date DATE,
        appointment_time TIME,
        type TEXT,
        status TEXT DEFAULT 'scheduled',
        notes TEXT,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    admin_exists = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin_exists:
        admin_password = generate_password_hash("admin123")
        conn.execute(
            "INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
            ('admin', admin_password, 'Administrator', 'admin')
        )
        print("✅ Default admin user created")

    conn.commit()
    conn.close()


# ============================================
# AUTHENTICATION
# ============================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to continue', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def log_activity(user_id, user_name, action, details="", ip=None):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO activity_logs (user_id, user_name, action, details, ip_address) VALUES (?, ?, ?, ?, ?)",
            (user_id, user_name, action, details, ip or request.remote_addr)
        )
        conn.commit()
        conn.close()
    except:
        pass


# ============================================
# AUTH ROUTES
# ============================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role']
            log_activity(user['id'], user['full_name'], 'login', 'Logged in')
            flash(f'Welcome back, {user["full_name"]}!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_activity(session['user_id'], session.get('full_name', 'Unknown'), 'logout', 'Logged out')
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('login'))


# ============================================
# CHANGE PASSWORD
# ============================================
@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']

        if new_password != confirm_password:
            flash('New passwords do not match', 'danger')
            return redirect(url_for('change_password'))

        if len(new_password) < 4:
            flash('Password must be at least 4 characters', 'danger')
            return redirect(url_for('change_password'))

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()

        if check_password_hash(user['password'], current_password):
            hashed_new = generate_password_hash(new_password)
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_new, session['user_id']))
            conn.commit()
            conn.close()
            flash('Password changed successfully! Please login again.', 'success')
            return redirect(url_for('logout'))
        else:
            conn.close()
            flash('Current password is incorrect', 'danger')

    return render_template('change_password.html')


# ============================================
# COUNTRY CODES
# ============================================
@app.route('/country_codes')
@login_required
def country_codes():
    return render_template('country_codes.html')


# ============================================
# STAFF MANAGEMENT
# ============================================
@app.route('/staff')
@login_required
@admin_required
def manage_staff():
    conn = get_db()
    staff = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template('staff.html', staff=staff)


@app.route('/add_staff', methods=['POST'])
@login_required
@admin_required
@retry_on_lock()
def add_staff():
    username = request.form['username']
    password = generate_password_hash(request.form['password'])
    full_name = request.form['full_name']
    role = request.form['role']

    try:
        conn = get_db()
        conn.execute("INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
                     (username, password, full_name, role))
        conn.commit()
        conn.close()
        flash(f'Staff member {full_name} added successfully', 'success')
        log_activity(session['user_id'], session['full_name'], 'add_staff', f"Added {full_name}")
    except sqlite3.IntegrityError:
        flash('Username already exists', 'danger')

    return redirect(url_for('manage_staff'))


@app.route('/remove_staff/<int:user_id>', methods=['POST'])
@login_required
@admin_required
@retry_on_lock()
def remove_staff(user_id):
    if user_id == session['user_id']:
        flash('You cannot remove your own account', 'danger')
        return redirect(url_for('manage_staff'))

    conn = get_db()
    user = conn.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()

    if user:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
        conn.commit()
        log_activity(session['user_id'], session['full_name'], 'remove_staff', f"Removed staff: {user['full_name']}")
        flash(f'Staff member {user["full_name"]} has been removed', 'success')
    else:
        flash('Staff member not found', 'danger')

    conn.close()
    return redirect(url_for('manage_staff'))


# ============================================
# PATIENT MANAGEMENT
# ============================================
@app.route('/')
@login_required
def index():
    conn = get_db()
    today = date.today()
    upcoming = today + timedelta(days=7)

    due_soon = conn.execute(
        """SELECT * FROM patients WHERE is_active = 1 
           AND ((reminder_type IN ('both', 'cleaning') AND next_cleaning BETWEEN ? AND ?)
                OR (reminder_type IN ('both', 'checkup') AND next_checkup BETWEEN ? AND ?))
           LIMIT 20""",
        (today, upcoming, today, upcoming)
    ).fetchall()

    today_appointments = conn.execute(
        """SELECT a.*, p.name as patient_name, p.phone 
           FROM appointments a JOIN patients p ON a.patient_id = p.id 
           WHERE a.appointment_date = ? AND a.status = 'scheduled'""",
        (today,)
    ).fetchall()

    stats = {
        'total': conn.execute("SELECT COUNT(*) as count FROM patients WHERE is_active=1").fetchone()['count'],
        'due_this_week': len(due_soon),
        'appointments_today': len(today_appointments),
        'staff_count': conn.execute("SELECT COUNT(*) as count FROM users WHERE is_active=1").fetchone()['count']
    }

    recent_activity = conn.execute("SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT 10").fetchall()
    conn.close()

    return render_template('index.html', due_soon=due_soon, stats=stats,
                           today_appointments=today_appointments, recent_activity=recent_activity)


@app.route('/patients')
@login_required
def patients():
    conn = get_db()
    search = request.args.get('search', '')

    if search:
        all_patients = conn.execute(
            "SELECT * FROM patients WHERE is_active=1 AND (name LIKE ? OR phone LIKE ? OR email LIKE ?) ORDER BY name",
            (f'%{search}%', f'%{search}%', f'%{search}%')
        ).fetchall()
    else:
        all_patients = conn.execute("SELECT * FROM patients WHERE is_active=1 ORDER BY name").fetchall()

    conn.close()
    return render_template('patients.html', patients=all_patients, search=search)


@app.route('/add_patient', methods=['GET', 'POST'])
@login_required
@retry_on_lock()
def add_patient():
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form.get('phone') or None
        email = request.form.get('email') or None
        reminder_type = request.form.get('reminder_type', 'both')
        last_cleaning = request.form.get('last_cleaning') or None
        last_checkup = request.form.get('last_checkup') or None
        notes = request.form.get('notes') or None

        conn = get_db()
        cursor = conn.execute(
            "INSERT INTO patients (name, phone, email, reminder_type, last_cleaning, last_checkup, notes, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, phone, email, reminder_type, last_cleaning, last_checkup, notes, session['user_id'])
        )

        patient_id = cursor.lastrowid

        if last_cleaning and reminder_type in ['both', 'cleaning']:
            last_date = datetime.strptime(last_cleaning, '%Y-%m-%d').date()
            conn.execute("UPDATE patients SET next_cleaning=? WHERE id=?",
                         (last_date + timedelta(days=180), patient_id))

        if last_checkup and reminder_type in ['both', 'checkup']:
            last_date = datetime.strptime(last_checkup, '%Y-%m-%d').date()
            conn.execute("UPDATE patients SET next_checkup=? WHERE id=?", (last_date + timedelta(days=90), patient_id))

        conn.commit()
        conn.close()

        log_activity(session['user_id'], session['full_name'], 'add_patient', f"Added patient: {name}")
        flash(f'Patient {name} added successfully!', 'success')
        return redirect(url_for('patients'))

    return render_template('add_patient.html')


@app.route('/edit_patient/<int:patient_id>', methods=['GET', 'POST'])
@login_required
@retry_on_lock()
def edit_patient(patient_id):
    conn = get_db()

    if request.method == 'POST':
        name = request.form['name']
        phone = request.form.get('phone') or None
        email = request.form.get('email') or None
        notes = request.form.get('notes') or None
        reminder_type = request.form.get('reminder_type')
        last_cleaning = request.form.get('last_cleaning') or None
        last_checkup = request.form.get('last_checkup') or None

        conn.execute(
            "UPDATE patients SET name=?, phone=?, email=?, notes=?, reminder_type=?, last_cleaning=?, last_checkup=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (name, phone, email, notes, reminder_type, last_cleaning, last_checkup, patient_id)
        )

        conn.execute("UPDATE patients SET next_cleaning=NULL, next_checkup=NULL WHERE id=?", (patient_id,))

        if last_cleaning and reminder_type in ['both', 'cleaning']:
            last_date = datetime.strptime(last_cleaning, '%Y-%m-%d').date()
            conn.execute("UPDATE patients SET next_cleaning=? WHERE id=?",
                         (last_date + timedelta(days=180), patient_id))

        if last_checkup and reminder_type in ['both', 'checkup']:
            last_date = datetime.strptime(last_checkup, '%Y-%m-%d').date()
            conn.execute("UPDATE patients SET next_checkup=? WHERE id=?", (last_date + timedelta(days=90), patient_id))

        conn.commit()
        conn.close()

        log_activity(session['user_id'], session['full_name'], 'edit_patient', f"Edited patient: {name}")
        flash(f'Patient {name} updated successfully!', 'success')
        return redirect(url_for('patients'))

    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()
    return render_template('edit_patient.html', patient=patient)


@app.route('/delete_patient/<int:patient_id>', methods=['POST'])
@login_required
@retry_on_lock()
def delete_patient(patient_id):
    conn = get_db()
    patient = conn.execute("SELECT name FROM patients WHERE id=?", (patient_id,)).fetchone()

    if patient:
        conn.execute("UPDATE patients SET is_active=0 WHERE id=?", (patient_id,))
        conn.commit()
        log_activity(session['user_id'], session['full_name'], 'delete_patient', f"Deleted patient: {patient['name']}")
        flash(f'Patient {patient["name"]} has been deleted', 'success')
    else:
        flash('Patient not found', 'danger')

    conn.close()
    return redirect(url_for('patients'))


@app.route('/update_visit/<int:patient_id>', methods=['POST'])
@login_required
@retry_on_lock()
def update_visit(patient_id):
    visit_type = request.form['visit_type']
    visit_date = request.form['visit_date']

    conn = get_db()
    date_obj = datetime.strptime(visit_date, '%Y-%m-%d').date()

    if visit_type == 'cleaning':
        next_date = date_obj + timedelta(days=180)
        conn.execute("UPDATE patients SET last_cleaning=?, next_cleaning=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (visit_date, next_date, patient_id))
        flash('Cleaning recorded! Next cleaning in 6 months.', 'success')
    else:
        next_date = date_obj + timedelta(days=90)
        conn.execute("UPDATE patients SET last_checkup=?, next_checkup=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (visit_date, next_date, patient_id))
        flash('Checkup recorded! Next checkup in 3 months.', 'success')

    conn.commit()
    patient = conn.execute("SELECT name FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()

    log_activity(session['user_id'], session['full_name'], 'record_visit',
                 f"Recorded {visit_type} for {patient['name']} on {visit_date}")
    return redirect(url_for('patients'))


@app.route('/schedule_appointment/<int:patient_id>', methods=['POST'])
@login_required
@retry_on_lock()
def schedule_appointment(patient_id):
    appointment_date = request.form['appointment_date']
    appointment_time = request.form['appointment_time']
    appointment_type = request.form['appointment_type']
    notes = request.form.get('notes', '')

    conn = get_db()
    conn.execute(
        "INSERT INTO appointments (patient_id, appointment_date, appointment_time, type, notes, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (patient_id, appointment_date, appointment_time, appointment_type, notes, session['user_id'])
    )
    conn.commit()
    conn.close()

    flash('Appointment scheduled!', 'success')
    return redirect(url_for('patients'))


@app.route('/reminder_logs')
@login_required
def reminder_logs():
    conn = get_db()

    # Get filter parameters
    search = request.args.get('search', '')
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')

    # Build query with filters
    query = "SELECT * FROM reminder_logs WHERE 1=1"
    params = []

    if search:
        query += " AND patient_name LIKE ?"
        params.append(f'%{search}%')

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)

    if type_filter:
        query += " AND reminder_type LIKE ?"
        params.append(f'%{type_filter}%')

    query += " ORDER BY sent_at DESC LIMIT 500"

    logs = conn.execute(query, params).fetchall()

    # Get stats
    stats = {
        'total': conn.execute("SELECT COUNT(*) as count FROM reminder_logs").fetchone()['count'],
        'sent': conn.execute("SELECT COUNT(*) as count FROM reminder_logs WHERE status = 'sent'").fetchone()['count'],
        'failed': conn.execute("SELECT COUNT(*) as count FROM reminder_logs WHERE status = 'failed'").fetchone()[
            'count']
    }

    conn.close()

    return render_template('reminder_logs.html', logs=logs, stats=stats,
                           search=search, status=status_filter, type=type_filter)


@app.route('/activity_logs')
@login_required
@admin_required
def activity_logs():
    conn = get_db()
    logs = conn.execute("SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT 500").fetchall()
    conn.close()
    return render_template('activity_logs.html', logs=logs)


@app.route('/whatsapp_reminder/<int:patient_id>')
@login_required
def whatsapp_reminder(patient_id):
    conn = get_db()
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()

    if not patient:
        flash('Patient not found', 'danger')
        return redirect(url_for('patients'))

    if not patient['phone']:
        flash('Patient has no phone number', 'warning')
        return redirect(url_for('patients'))

    return render_template('whatsapp_reminder.html', patient=patient, reminder_type='both')


@app.route('/log_reminder/<int:patient_id>', methods=['POST'])
@login_required
def log_reminder(patient_id):
    """Manually log a reminder that was sent"""
    reminder_type = request.form.get('reminder_type', 'both')

    conn = get_db()
    patient = conn.execute("SELECT name FROM patients WHERE id = ?", (patient_id,)).fetchone()

    if patient:
        conn.execute(
            "INSERT INTO reminder_logs (patient_id, patient_name, reminder_type, method, status, sent_by) VALUES (?, ?, ?, ?, ?, ?)",
            (patient_id, patient['name'], reminder_type, 'whatsapp', 'sent', session['user_id'])
        )
        conn.commit()
        flash(f'✅ Reminder to {patient["name"]} logged successfully!', 'success')
    else:
        flash('Patient not found', 'danger')

    conn.close()
    return redirect(url_for('patients'))

# ============================================
# MAIN
# ============================================
if __name__ == '__main__':
    init_db()

    scheduler = BackgroundScheduler()
    scheduler.add_job(func=lambda: None, trigger="cron", hour=9, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

    print("\n" + "=" * 60)
    print("🦷 BRILLIANT BITES DENTAL REMINDER SYSTEM")
    print("=" * 60)
    print("\n📱 Access from any device on your network:")
    print("   http://localhost:5000")
    print("\n👥 Default Admin Login:")
    print("   Username: admin")
    print("   Password: admin123")
    print("=" * 60 + "\n")

    app.run(debug=True, host='0.0.0.0', port=5000)