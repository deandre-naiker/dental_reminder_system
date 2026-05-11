from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from datetime import date, timedelta, datetime
import sqlite3
import os
from functools import wraps
import re
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import time
from functools import wraps


def retry_on_lock(max_retries=10, delay=1.0):
    """Retry database operation if locked"""
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

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

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
    print("⚠️ WhatsApp disabled - add Twilio credentials to enable")


# ============================================
# DATABASE FUNCTIONS
# ============================================
def get_db():
    conn = sqlite3.connect('dental.db', timeout =120)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database with multi-user support"""
    conn = get_db()

    # Users table for staff accounts
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT DEFAULT 'staff',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Patients table with reminder_type column
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
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY (created_by) REFERENCES users (id)
    )''')

    # Add missing columns if they don't exist
    try:
        conn.execute("ALTER TABLE patients ADD COLUMN reminder_type TEXT DEFAULT 'both'")
    except:
        pass
    try:
        conn.execute("ALTER TABLE patients ADD COLUMN whatsapp_joined INTEGER DEFAULT 0")
    except:
        pass

    # Reminder logs table
    conn.execute('''CREATE TABLE IF NOT EXISTS reminder_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER,
        patient_name TEXT,
        reminder_type TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        method TEXT,
        status TEXT,
        error_details TEXT,
        sent_by INTEGER,
        FOREIGN KEY (sent_by) REFERENCES users (id)
    )''')

    # Activity logs table
    conn.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        user_name TEXT,
        action TEXT,
        details TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Appointments table
    conn.execute('''CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER,
        appointment_date DATE,
        appointment_time TIME,
        type TEXT,
        status TEXT DEFAULT 'scheduled',
        notes TEXT,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (patient_id) REFERENCES patients (id)
    )''')

    # Insert default admin user if not exists
    admin_exists = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin_exists:
        admin_password = generate_password_hash("admin123")
        conn.execute(
            "INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
            ('admin', admin_password, 'Administrator', 'admin')
        )
        print("✅ Default admin user created: username='admin', password='admin123'")
        print("⚠️ PLEASE CHANGE DEFAULT PASSWORD AFTER FIRST LOGIN!")

    conn.commit()
    conn.close()


# ============================================
# AUTHENTICATION DECORATORS
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
    """Log staff activity"""
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


@app.route('/country_codes')
@login_required
def country_codes():
    """Show country codes reference"""
    return render_template('country_codes.html')


def format_phone_number(phone):
    """Format phone number for WhatsApp (works for any country)"""
    if not phone:
        return ""

    # Remove spaces, dashes, parentheses
    cleaned = re.sub(r'[\s\-\(\)]', '', str(phone))

    # If already has +, keep it
    if cleaned.startswith('+'):
        return cleaned

    # If number starts with 00 (international), convert to +
    if cleaned.startswith('00'):
        return '+' + cleaned[2:]

    # If number starts with 0 (local format)
    if cleaned.startswith('0') and len(cleaned) >= 9:
        # ZA (South Africa) - this is the default but make it flexible
        # Return as is with +27, but let WhatsApp handle it
        return '+' + cleaned
    # For numbers that seem to have international format already
    elif len(cleaned) >= 10 and not cleaned.startswith('0'):
        # Assume it already has country code
        return '+' + cleaned

    # Default - just add plus
    return '+' + cleaned

# ============================================
# AUTH ROUTES
# ============================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role']

            log_activity(user['id'], user['full_name'], 'login', f"Logged in from {request.remote_addr}")
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
# STAFF MANAGEMENT ROUTES
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
def add_staff():
    username = request.form['username']
    password = generate_password_hash(request.form['password'])
    full_name = request.form['full_name']
    role = request.form['role']

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
            (username, password, full_name, role)
        )
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
# PATIENT MANAGEMENT ROUTES
# ============================================
@app.route('/')
@login_required
def index():
    conn = get_db()
    today = date.today()
    upcoming = today + timedelta(days=7)

    due_soon = conn.execute(
        """SELECT * FROM patients 
           WHERE is_active = 1 
           AND (
               (reminder_type IN ('both', 'cleaning') AND next_cleaning BETWEEN ? AND ?)
               OR 
               (reminder_type IN ('both', 'checkup') AND next_checkup BETWEEN ? AND ?)
           )
           ORDER BY 
               CASE 
                   WHEN reminder_type IN ('both', 'cleaning') THEN next_cleaning 
                   ELSE next_checkup 
               END ASC
           LIMIT 20""",
        (today, upcoming, today, upcoming)
    ).fetchall()

    today_appointments = conn.execute(
        """SELECT a.*, p.name as patient_name, p.phone 
           FROM appointments a 
           JOIN patients p ON a.patient_id = p.id 
           WHERE a.appointment_date = ? AND a.status = 'scheduled'
           ORDER BY a.appointment_time""",
        (today,)
    ).fetchall()

    stats = {
        'total': conn.execute("SELECT COUNT(*) as count FROM patients WHERE is_active=1").fetchone()['count'],
        'due_this_week': len(due_soon),
        'appointments_today': len(today_appointments),
        'staff_count': conn.execute("SELECT COUNT(*) as count FROM users WHERE is_active=1").fetchone()['count']
    }

    recent_activity = conn.execute(
        "SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    conn.close()

    return render_template('index.html',
                           due_soon=due_soon,
                           stats=stats,
                           today_appointments=today_appointments,
                           recent_activity=recent_activity)


@app.route('/patients')
@login_required
def patients():
    conn = get_db()
    search = request.args.get('search', '')

    if search:
        all_patients = conn.execute(
            """SELECT * FROM patients 
               WHERE is_active=1 AND (name LIKE ? OR phone LIKE ? OR email LIKE ?)
               ORDER BY name""",
            (f'%{search}%', f'%{search}%', f'%{search}%')
        ).fetchall()
    else:
        all_patients = conn.execute(
            "SELECT * FROM patients WHERE is_active=1 ORDER BY name"
        ).fetchall()

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
            """INSERT INTO patients 
               (name, phone, email, reminder_type, last_cleaning, last_checkup, notes, created_by) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, phone, email, reminder_type, last_cleaning, last_checkup, notes, session['user_id'])
        )

        patient_id = cursor.lastrowid

        if last_cleaning and reminder_type in ['both', 'cleaning']:
            last_date = datetime.strptime(last_cleaning, '%Y-%m-%d').date()
            next_cleaning = last_date + timedelta(days=180)
            conn.execute("UPDATE patients SET next_cleaning=? WHERE id=?", (next_cleaning, patient_id))

        if last_checkup and reminder_type in ['both', 'checkup']:
            last_date = datetime.strptime(last_checkup, '%Y-%m-%d').date()
            next_checkup = last_date + timedelta(days=90)
            conn.execute("UPDATE patients SET next_checkup=? WHERE id=?", (next_checkup, patient_id))

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

        # Update patient basic info
        conn.execute(
            """UPDATE patients 
               SET name=?, phone=?, email=?, notes=?, reminder_type=?, 
                   last_cleaning=?, last_checkup=?, updated_at=CURRENT_TIMESTAMP 
               WHERE id=?""",
            (name, phone, email, notes, reminder_type, last_cleaning, last_checkup, patient_id)
        )

        # Clear existing next dates
        conn.execute("UPDATE patients SET next_cleaning=NULL, next_checkup=NULL WHERE id=?", (patient_id,))

        # Recalculate next dates
        if last_cleaning and reminder_type in ['both', 'cleaning']:
            last_date = datetime.strptime(last_cleaning, '%Y-%m-%d').date()
            next_cleaning = last_date + timedelta(days=180)
            conn.execute("UPDATE patients SET next_cleaning=? WHERE id=?", (next_cleaning, patient_id))

        if last_checkup and reminder_type in ['both', 'checkup']:
            last_date = datetime.strptime(last_checkup, '%Y-%m-%d').date()
            next_checkup = last_date + timedelta(days=90)
            conn.execute("UPDATE patients SET next_checkup=? WHERE id=?", (next_checkup, patient_id))

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
        conn.execute(
            "UPDATE patients SET last_cleaning=?, next_cleaning=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (visit_date, next_date, patient_id)
        )
        flash('Cleaning recorded! Next cleaning in 6 months.', 'success')
    else:
        next_date = date_obj + timedelta(days=90)
        conn.execute(
            "UPDATE patients SET last_checkup=?, next_checkup=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (visit_date, next_date, patient_id)
        )
        flash('Checkup recorded! Next checkup in 3 months.', 'success')

    conn.commit()
    patient = conn.execute("SELECT name FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()

    log_activity(session['user_id'], session['full_name'], 'record_visit',
                 f"Recorded {visit_type} for {patient['name']} on {visit_date}")

    return redirect(url_for('patients'))


# ============================================
# APPOINTMENT ROUTES
# ============================================
@app.route('/schedule_appointment/<int:patient_id>', methods=['POST'])
@login_required
def schedule_appointment(patient_id):
    appointment_date = request.form['appointment_date']
    appointment_time = request.form['appointment_time']
    appointment_type = request.form['appointment_type']
    notes = request.form.get('notes', '')

    conn = get_db()
    conn.execute(
        """INSERT INTO appointments 
           (patient_id, appointment_date, appointment_time, type, notes, created_by) 
           VALUES (?, ?, ?, ?, ?, ?)""",
        (patient_id, appointment_date, appointment_time, appointment_type, notes, session['user_id'])
    )
    conn.commit()
    conn.close()

    flash('Appointment scheduled!', 'success')
    return redirect(url_for('patients'))


# ============================================
# LOGS AND REPORTS ROUTES
# ============================================
@app.route('/reminder_logs')
@login_required
def reminder_logs():
    conn = get_db()
    logs = conn.execute(
        """SELECT * FROM reminder_logs 
           ORDER BY sent_at DESC LIMIT 200"""
    ).fetchall()
    conn.close()
    return render_template('logs.html', logs=logs)


@app.route('/activity_logs')
@login_required
@admin_required
def activity_logs():
    conn = get_db()
    logs = conn.execute(
        """SELECT * FROM activity_logs 
           ORDER BY created_at DESC LIMIT 500"""
    ).fetchall()
    conn.close()
    return render_template('activity_logs.html', logs=logs)


# ============================================
# API ROUTES
# ============================================
@app.route('/api/patients/search')
@login_required
def api_search_patients():
    query = request.args.get('q', '')
    conn = get_db()
    patients = conn.execute(
        """SELECT id, name, phone, next_cleaning, next_checkup 
           FROM patients 
           WHERE is_active=1 AND name LIKE ? 
           LIMIT 20""",
        (f'%{query}%',)
    ).fetchall()
    conn.close()
    return jsonify([dict(p) for p in patients])


# ============================================
# WHATSAPP ROUTES
# ============================================
@app.route('/whatsapp_reminder/<int:patient_id>')
@login_required
def whatsapp_reminder(patient_id):
    """Open WhatsApp with pre-filled reminder message"""
    conn = get_db()
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()

    if not patient:
        flash('Patient not found', 'danger')
        return redirect(url_for('patients'))

    if not patient['phone']:
        flash('Patient has no phone number. Please add a phone number first.', 'warning')
        return redirect(url_for('edit_patient', patient_id=patient_id))

    reminder_type = request.args.get('type', 'both')
    return render_template('whatsapp_reminder.html', patient=patient, reminder_type=reminder_type)


@app.route('/send_whatsapp/<int:patient_id>', methods=['POST'])
@login_required
def send_whatsapp_manual(patient_id):
    """Manually send WhatsApp reminder (for Twilio users)"""
    if not whatsapp_enabled:
        flash('WhatsApp is not configured', 'danger')
        return redirect(url_for('patients'))

    conn = get_db()
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    conn.close()

    if not patient['phone']:
        flash('Patient has no phone number', 'danger')
        return redirect(url_for('patients'))

    next_date = None
    reminder_type = None

    if patient['next_cleaning']:
        next_date = patient['next_cleaning']
        reminder_type = '6-month cleaning'
    elif patient['next_checkup']:
        next_date = patient['next_checkup']
        reminder_type = '3-month checkup'

    if next_date and reminder_type:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            phone_number = format_phone_number(patient['phone'])

            message = client.messages.create(
                body=f"Reminder: {patient['name']}, your {reminder_type} is due on {next_date}. Please contact us to schedule.",
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:{phone_number}"
            )

            conn = get_db()
            conn.execute(
                "INSERT INTO reminder_logs (patient_id, patient_name, reminder_type, method, status, sent_by) VALUES (?, ?, ?, ?, ?, ?)",
                (patient_id, patient['name'], reminder_type, 'whatsapp', 'sent', session['user_id'])
            )
            conn.commit()
            conn.close()

            flash(f'WhatsApp reminder sent to {patient["name"]}!', 'success')
        except Exception as e:
            flash(f'Failed to send: {str(e)}', 'danger')
    else:
        flash('No upcoming appointments found', 'warning')

    return redirect(url_for('patients'))


# ============================================
# AUTOMATIC REMINDER JOB
# ============================================
def check_and_send_reminders():
    """Background job for automatic reminders - respects reminder_type"""
    if not whatsapp_enabled:
        return

    print(f"Running automatic reminder check at {datetime.now()}")
    conn = get_db()
    patients = conn.execute(
        "SELECT * FROM patients WHERE is_active = 1 AND phone IS NOT NULL"
    ).fetchall()

    today = date.today()
    upcoming = today + timedelta(days=2)

    for patient in patients:
        reminders = []

        if patient['reminder_type'] in ['both', 'cleaning'] and patient['next_cleaning']:
            next_date = datetime.strptime(patient['next_cleaning'], '%Y-%m-%d').date()
            if today <= next_date <= upcoming:
                reminders.append(('6-month cleaning', next_date))

        if patient['reminder_type'] in ['both', 'checkup'] and patient['next_checkup']:
            next_date = datetime.strptime(patient['next_checkup'], '%Y-%m-%d').date()
            if today <= next_date <= upcoming:
                reminders.append(('3-month checkup', next_date))

        for reminder_type, due_date in reminders:
            try:
                from twilio.rest import Client
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                phone_number = format_phone_number(patient['phone'])

                client.messages.create(
                    body=f"🦷 Dental Reminder: {patient['name']}, your {reminder_type} is due on {due_date.strftime('%B %d, %Y')}. Please call us to schedule.",
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=f"whatsapp:{phone_number}"
                )

                conn.execute(
                    "INSERT INTO reminder_logs (patient_id, patient_name, reminder_type, method, status) VALUES (?, ?, ?, ?, ?)",
                    (patient['id'], patient['name'], reminder_type, 'whatsapp', 'sent')
                )
                conn.commit()
                print(f"Auto-reminder sent to {patient['name']}")
            except Exception as e:
                print(f"Auto-reminder failed for {patient['name']}: {e}")

    conn.close()


@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Allow any logged-in user to change their own password"""
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


@app.route('/country_codes')
@login_required
def country_codes():
    """Show country codes reference"""
    return render_template('country_codes.html')

# ============================================
# MAIN EXECUTION
# ============================================
if __name__ == '__main__':
    init_db()

    # Setup scheduler for automatic reminders
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_and_send_reminders, trigger="cron", hour=9, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

    print("\n" + "=" * 60)
    print("🦷 DENTAL REMINDER SYSTEM - Mobile & Multi-Staff Ready")
    print("=" * 60)
    print(f"\n📱 Access from any device on your network:")
    print(f"   http://localhost:5000")
    print(f"\n👥 Default Admin Login:")
    print(f"   Username: admin")
    print(f"   Password: admin123")
    print(f"\n📱 To send WhatsApp from your work number (0677977404):")
    print(f"   1. Go to https://web.whatsapp.com on this computer")
    print(f"   2. Scan QR code with your work phone")
    print(f"   3. Click WhatsApp buttons in the app to send messages from your work number")
    print(f"\n⚠️  IMPORTANT: Change default password after first login!")
    print("=" * 60 + "\n")

    app.run(debug=True, host='0.0.0.0', port=5000)


    @app.route('/change_password', methods=['GET', 'POST'])
    @login_required
    def change_password():
        """Allow any logged-in user to change their own password"""
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