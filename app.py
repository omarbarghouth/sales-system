import os
import json
import io
import logging
import psycopg2
import psycopg2.extras
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from datetime import date, timedelta, datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, g, Response, session, flash)
import bcrypt

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'alsondos-secret-change-in-production-2024')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PER_PAGE = 50  # rows per page

# ── Database helpers ──────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg2.connect(
            os.environ.get("DATABASE_URL"),
            cursor_factory=psycopg2.extras.RealDictCursor
        )
        db.autocommit = False
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        if exception:
            db.rollback()
        db.close()

def query_db(query, args=(), one=False):
    try:
        cur = get_db().cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        logger.error(f"query_db error: {e} | query: {query}")
        raise

def execute_db(query, args=()):
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(query, args)
        db.commit()
        try:
            cur.execute("SELECT lastval()")
            return cur.fetchone()['lastval']
        except Exception:
            db.commit()
            return None
    except Exception as e:
        logger.error(f"execute_db error: {e} | query: {query}")
        raise

def paginate(query, params, page, per_page=PER_PAGE):
    """Returns (rows, total_count, total_pages)"""
    count_q = f"SELECT COUNT(*) as cnt FROM ({query}) sub"
    total = query_db(count_q, params, one=True)['cnt']
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    paginated_q = query + f" LIMIT {per_page} OFFSET {(page-1)*per_page}"
    rows = query_db(paginated_q, params)
    return rows, total, total_pages

def init_db():
    db = psycopg2.connect(os.environ.get("DATABASE_URL"))
    cur = db.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id          SERIAL PRIMARY KEY,
            from_loc    TEXT NOT NULL,
            to_loc      TEXT NOT NULL,
            via         TEXT DEFAULT '',
            trip_type   TEXT DEFAULT '',
            buy_from    TEXT DEFAULT '',
            company     TEXT NOT NULL,
            tickets     INTEGER DEFAULT 1,
            customer    TEXT NOT NULL,
            sale_date   TEXT NOT NULL,
            travel_date TEXT DEFAULT '',
            net         REAL NOT NULL DEFAULT 0,
            sell        REAL NOT NULL DEFAULT 0,
            profit      REAL NOT NULL DEFAULT 0,
            status      TEXT DEFAULT 'STILL',
            remarks     TEXT DEFAULT '',
            deleted     BOOLEAN DEFAULT FALSE,
            created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id          SERIAL PRIMARY KEY,
            company     TEXT NOT NULL,
            amount      REAL NOT NULL,
            pay_date    TEXT NOT NULL,
            notes       TEXT DEFAULT '',
            deleted     BOOLEAN DEFAULT FALSE,
            created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            created_at    TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username    TEXT NOT NULL,
            action      TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            record_id   INTEGER,
            detail      TEXT DEFAULT '',
            created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    ''')

    # Add deleted column FIRST (before indexes) — handles old schema upgrades
    for tbl in ('sales', 'payments'):
        cur.execute(f"""
            DO $$ BEGIN
                ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;
            EXCEPTION WHEN duplicate_column THEN NULL; END $$;
        """)
        cur.execute(f"""
            DO $$ BEGIN
                ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE;
            EXCEPTION WHEN duplicate_column THEN NULL; END $$;
        """)

    # New ticket tracking columns
    new_cols = [
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS return_date         TEXT DEFAULT ''",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS return_supplier     TEXT DEFAULT ''",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS outbound_delivery   TEXT DEFAULT ''",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS return_delivery     TEXT DEFAULT ''",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS outbound_status     TEXT DEFAULT 'PENDING'",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS return_status       TEXT DEFAULT 'PENDING'",
    ]
    for col in new_cols:
        cur.execute(f"DO $$ BEGIN {col}; EXCEPTION WHEN duplicate_column THEN NULL; END $$;")

    db.commit()

    # Indexes for performance
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_sales_company    ON sales(company)",
        "CREATE INDEX IF NOT EXISTS idx_sales_sale_date  ON sales(sale_date)",
        "CREATE INDEX IF NOT EXISTS idx_sales_status     ON sales(status)",
        "CREATE INDEX IF NOT EXISTS idx_sales_travel     ON sales(travel_date)",
        "CREATE INDEX IF NOT EXISTS idx_sales_deleted    ON sales(deleted)",
        "CREATE INDEX IF NOT EXISTS idx_sales_archived   ON sales(is_archived)",
        "CREATE INDEX IF NOT EXISTS idx_payments_archived ON payments(is_archived)",
        "CREATE INDEX IF NOT EXISTS idx_payments_company ON payments(company)",
        "CREATE INDEX IF NOT EXISTS idx_payments_date    ON payments(pay_date)",
        "CREATE INDEX IF NOT EXISTS idx_audit_user       ON audit_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_table      ON audit_logs(table_name)",
        "CREATE INDEX IF NOT EXISTS idx_audit_created    ON audit_logs(created_at)",
    ]
    for idx in indexes:
        cur.execute(idx)

    db.commit()

    cur.execute('SELECT COUNT(*) FROM users')
    if cur.fetchone()[0] == 0:
        pw_hash = bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt()).decode()
        cur.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
                    ('admin', pw_hash, 'admin'))
        db.commit()
        print("✅ Default admin: username=admin password=admin123")

    cur.execute('SELECT COUNT(*) FROM sales')
    if cur.fetchone()[0] == 0:
        seed_file = os.path.join(os.path.dirname(__file__), 'seed_data.json')
        if os.path.exists(seed_file):
            with open(seed_file) as f:
                rows = json.load(f)
            for row in rows:
                cur.execute('''
                    INSERT INTO sales
                    (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
                     customer,sale_date,travel_date,net,sell,profit,status,remarks)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ''', (row['from_loc'],row['to_loc'],row['via'],row['trip_type'],
                      row['buy_from'],row['company'],row['tickets'],row['customer'],
                      row['sale_date'],row['travel_date'],row['net'],row['sell'],
                      row['profit'],row['status'],row['remarks']))
            db.commit()
            print(f"✅ Seeded {len(rows)} records")

    cur.close()
    db.close()

init_db()

# ── Audit log helper ──────────────────────────────────────────────────────────
def log_action(action, table_name, record_id=None, detail=''):
    try:
        uid      = session.get('user_id')
        uname    = session.get('username', 'system')
        execute_db('''
            INSERT INTO audit_logs (user_id, username, action, table_name, record_id, detail)
            VALUES (%s,%s,%s,%s,%s,%s)
        ''', (uid, uname, action, table_name, record_id, detail))
    except Exception as e:
        logger.error(f"Audit log failed: {e}")

def compute_ticket_status(outbound_delivery, return_delivery):
    """Auto-compute outbound/return status based on today's date."""
    today_str = str(date.today())
    outbound_status = 'DONE' if outbound_delivery and today_str >= outbound_delivery else 'PENDING'
    return_status   = 'DONE' if return_delivery  and today_str >= return_delivery  else 'PENDING'
    # Overall status: DONE only when all applicable sectors done
    if return_delivery:
        overall = 'DONE' if outbound_status == 'DONE' and return_status == 'DONE' else 'STILL'
    else:
        overall = 'DONE' if outbound_status == 'DONE' else 'STILL'
    return outbound_status, return_status, overall

# ── Input validation ──────────────────────────────────────────────────────────
def validate_sale_form(form):
    errors = []
    if not form.get('from_loc','').strip():
        errors.append('From location is required.')
    if not form.get('to_loc','').strip():
        errors.append('To location is required.')
    if not form.get('company','').strip():
        errors.append('Company is required.')
    if not form.get('customer','').strip():
        errors.append('Customer name is required.')
    if not form.get('sale_date','').strip():
        errors.append('Sale date is required.')
    try:
        net  = float(form.get('net', 0))
        sell = float(form.get('sell', 0))
        if net < 0:  errors.append('Net cost cannot be negative.')
        if sell < 0: errors.append('Sell price cannot be negative.')
    except ValueError:
        errors.append('Net and Sell must be valid numbers.')
    try:
        tickets = int(form.get('tickets', 1))
        if tickets < 1: errors.append('Tickets must be at least 1.')
    except ValueError:
        errors.append('Tickets must be a valid number.')
    return errors

# ── Auth helpers ──────────────────────────────────────────────────────────────
def get_current_user():
    if 'user_id' not in session:
        return None
    return query_db('SELECT * FROM users WHERE id=%s', [session['user_id']], one=True)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        if session.get('user_role') != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

@app.context_processor
def inject_user():
    return {
        'current_user': get_current_user(),
        'is_admin': session.get('user_role') == 'admin',
        'logged_in': 'user_id' in session
    }

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='Page not found.'), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 error: {e}")
    return render_template('error.html', code=500, msg='Internal server error. Please try again.'), 500

# ── Auth Routes ───────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '').encode()
        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('login.html', next=request.args.get('next',''))
        user = query_db('SELECT * FROM users WHERE username=%s', [username], one=True)
        if user and bcrypt.checkpw(password, user['password_hash'].encode()):
            session.clear()
            session['user_id']   = user['id']
            session['username']  = user['username']
            session['user_role'] = user['role']
            session.permanent    = True
            log_action('LOGIN', 'users', user['id'], f"User {username} logged in")
            next_page = request.form.get('next') or url_for('index')
            return redirect(next_page)
        flash('Invalid username or password.', 'danger')
    return render_template('login.html', next=request.args.get('next', ''))

@app.route('/logout')
def logout():
    log_action('LOGOUT', 'users', session.get('user_id'), f"User {session.get('username')} logged out")
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# ── User Management ───────────────────────────────────────────────────────────
@app.route('/users')
@admin_required
def manage_users():
    users = query_db("""
        SELECT u.id, u.username, u.role, u.created_at,
               COALESCE(u.full_name,'') as full_name,
               COALESCE(u.is_active, TRUE) as is_active,
               COALESCE(u.commission_rate, 20.0) as commission_rate,
               COALESCE(s.txn_count, 0)     as txn_count,
               COALESCE(s.total_sell, 0)    as total_sell,
               COALESCE(s.total_profit, 0)  as total_profit
        FROM users u
        LEFT JOIN (
            SELECT created_by_user_id,
                   COUNT(*) as txn_count,
                   SUM(sell) as total_sell,
                   SUM(profit) as total_profit
            FROM sales WHERE deleted=FALSE
            GROUP BY created_by_user_id
        ) s ON u.id = s.created_by_user_id
        ORDER BY u.id
    """)
    users = [dict(u) for u in users]
    for u in users:
        r = float(u.get('commission_rate') or 20)
        u['commission'] = round(float(u.get('total_profit') or 0) * r / 100, 2)
    return render_template('users.html', users=users)

@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    role     = request.form.get('role', 'user')
    if not username or not password:
        flash('Username and password are required.', 'danger')
        return redirect(url_for('manage_users'))
    if len(password) < 6:
        flash('Password must be at least 6 characters.', 'danger')
        return redirect(url_for('manage_users'))
    if query_db('SELECT id FROM users WHERE username=%s', [username], one=True):
        flash(f'Username "{username}" already exists.', 'danger')
        return redirect(url_for('manage_users'))
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    uid = execute_db('INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)',
                     (username, pw_hash, role))
    log_action('CREATE', 'users', uid, f"Created user {username} with role {role}")
    flash(f'User "{username}" created successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('manage_users'))
    u = query_db('SELECT username FROM users WHERE id=%s', [user_id], one=True)
    execute_db('DELETE FROM users WHERE id=%s', [user_id])
    log_action('DELETE', 'users', user_id, f"Deleted user {u['username'] if u else user_id}")
    flash('User deleted.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/change-password', methods=['POST'])
@login_required
def change_password():
    current = request.form.get('current_password', '').encode()
    new_pw  = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    user = query_db('SELECT * FROM users WHERE id=%s', [session['user_id']], one=True)
    if not bcrypt.checkpw(current, user['password_hash'].encode()):
        flash('Current password is incorrect.', 'danger')
    elif new_pw != confirm:
        flash('New passwords do not match.', 'danger')
    elif len(new_pw) < 6:
        flash('Password must be at least 6 characters.', 'danger')
    else:
        pw_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
        execute_db('UPDATE users SET password_hash=%s WHERE id=%s', (pw_hash, session['user_id']))
        log_action('UPDATE', 'users', session['user_id'], "Password changed")
        flash('Password changed successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/reset-pw/<int:uid>', methods=['POST'])
@admin_required
def admin_reset_pw(uid):
    new_pw = request.form.get('new_password','')
    if len(new_pw) < 6:
        flash('Password must be at least 6 characters.', 'danger')
        return redirect(url_for('manage_users'))
    pw_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    execute_db('UPDATE users SET password_hash=%s WHERE id=%s', (pw_hash, uid))
    u = query_db('SELECT username FROM users WHERE id=%s', [uid], one=True)
    log_action('UPDATE', 'users', uid, f"Password reset for {u['username'] if u else uid}")
    flash(f'Password reset successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/toggle/<int:uid>', methods=['POST'])
@admin_required
def toggle_user(uid):
    if uid == session.get('user_id'):
        flash('You cannot disable your own account.', 'danger')
        return redirect(url_for('manage_users'))
    action = request.form.get('action', 'disable')
    active = (action == 'enable')
    execute_db('UPDATE users SET is_active=%s WHERE id=%s', (active, uid))
    u = query_db('SELECT username FROM users WHERE id=%s', [uid], one=True)
    msg = 'enabled' if active else 'disabled'
    log_action('UPDATE', 'users', uid, f"Account {msg}: {u['username'] if u else uid}")
    flash(f'Account {msg} successfully.', 'success')
    return redirect(url_for('manage_users'))

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    stats = query_db('''
        SELECT COUNT(*) as total_transactions,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(net),0) as total_net,
               COALESCE(SUM(profit),0) as total_profit
        FROM sales WHERE deleted=FALSE AND is_archived=FALSE
    ''', one=True)
    total_paid = query_db(
        'SELECT COALESCE(SUM(amount),0) as paid FROM payments WHERE deleted=FALSE AND is_archived=FALSE', one=True
    )['paid']
    balance = (stats['total_sell'] or 0) - total_paid

    monthly = query_db('''
        SELECT to_char(to_date(sale_date,'YYYY-MM-DD'),'MM') as month,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(profit),0) as total_profit,
               COUNT(*) as count
        FROM sales
        WHERE deleted=FALSE AND is_archived=FALSE
          AND to_char(to_date(sale_date,'YYYY-MM-DD'),'YYYY') = to_char(NOW(),'YYYY')
        GROUP BY month ORDER BY month
    ''')

    top_companies = query_db('''
        SELECT company, COALESCE(SUM(sell),0) as total, COUNT(*) as cnt
        FROM sales WHERE deleted=FALSE AND is_archived=FALSE
        GROUP BY company ORDER BY total DESC LIMIT 10
    ''')

    tomorrow_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tomorrow = query_db('''
        SELECT company, customer, from_loc, to_loc, travel_date, tickets, status
        FROM sales WHERE travel_date=%s AND deleted=FALSE ORDER BY company
    ''', [tomorrow_date])

    # Recent activity from audit log
    recent_logs = query_db('''
        SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 8
    ''')

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]

    # Status distribution for pie chart
    status_counts = query_db("""
        SELECT
            SUM(CASE WHEN status='DONE'   THEN 1 ELSE 0 END) as done_count,
            SUM(CASE WHEN status='STILL'  THEN 1 ELSE 0 END) as still_count,
            SUM(CASE WHEN status NOT IN ('DONE','STILL') THEN 1 ELSE 0 END) as other_count
        FROM sales WHERE deleted=FALSE AND is_archived=FALSE
    """, one=True)
    if status_counts:
        stats = dict(stats)
        stats['done_count']  = status_counts['done_count']  or 0
        stats['still_count'] = status_counts['still_count'] or 0
        stats['other_count'] = status_counts['other_count'] or 0

    # Outstanding balances by company - correct SQL with HAVING
    outstanding_by_company = query_db("""
        SELECT s.company,
               s.sell      AS total_sell,
               COALESCE(p.paid, 0) AS total_paid,
               s.sell - COALESCE(p.paid, 0) AS balance
        FROM (
            SELECT company, SUM(sell) AS sell
            FROM sales WHERE deleted=FALSE AND is_archived=FALSE
            GROUP BY company
        ) s
        LEFT JOIN (
            SELECT company, SUM(amount) AS paid
            FROM payments WHERE deleted=FALSE AND is_archived=FALSE
            GROUP BY company
        ) p ON s.company = p.company
        WHERE s.sell - COALESCE(p.paid, 0) > 0.01
        ORDER BY balance DESC
        LIMIT 10
    """) or []

    # Pre-process chart data as plain Python lists (avoids Decimal/RealDictRow JSON issues)
    chart_month_labels  = [str(r['month']) for r in monthly]
    chart_month_sells   = [float(r['total_sell'] or 0) for r in monthly]
    chart_month_profits = [float(r['total_profit'] or 0) for r in monthly]
    chart_company_names = [str(r['company']) for r in top_companies]
    chart_company_totals= [float(r['total'] or 0) for r in top_companies]
    has_monthly_data    = any(v > 0 for v in chart_month_sells)

    # Recent transactions with agent names
    recent_txns = query_db("""
        SELECT s.*,
               COALESCE(u.full_name, s.created_by_username, '—') AS agent_display,
               COALESCE(s.created_by_username, '—') AS agent_name
        FROM sales s
        LEFT JOIN users u ON s.created_by_user_id = u.id
        WHERE s.deleted=FALSE AND s.is_archived=FALSE
        ORDER BY s.created_at DESC, s.id DESC
        LIMIT 12
    """) or []

    return render_template('index.html',
        stats=stats, total_paid=total_paid, balance=balance,
        monthly=monthly, top_companies=top_companies,
        tomorrow=tomorrow, companies=companies,
        recent_logs=recent_logs,
        recent_txns=recent_txns,
        outstanding_by_company=outstanding_by_company,
        chart_month_labels=chart_month_labels,
        chart_month_sells=chart_month_sells,
        chart_month_profits=chart_month_profits,
        chart_company_names=chart_company_names,
        chart_company_totals=chart_company_totals,
        has_monthly_data=has_monthly_data,
        today=date.today().strftime('%d %B %Y')
    )

# ── Sales ─────────────────────────────────────────────────────────────────────
@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_sale():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', companies=companies,
                                   today=str(date.today()), form=request.form)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))

        outbound_delivery = request.form.get('outbound_delivery','').strip()
        return_delivery   = request.form.get('return_delivery','').strip()
        return_date       = request.form.get('return_date','').strip()
        return_supplier   = request.form.get('return_supplier','').upper().strip()

        outbound_status, return_status, overall = compute_ticket_status(
            outbound_delivery, return_delivery
        )

        new_id = execute_db('''
            INSERT INTO sales
            (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
             customer,sale_date,travel_date,return_date,return_supplier,
             outbound_delivery,return_delivery,outbound_status,return_status,
             net,sell,profit,status,remarks,
             created_by_user_id,created_by_username)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            request.form.get('from_loc','').upper().strip(),
            request.form.get('to_loc','').upper().strip(),
            request.form.get('via','').upper().strip(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper().strip(),
            request.form.get('company','').upper().strip(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper().strip(),
            request.form.get('sale_date', str(date.today())),
            request.form.get('travel_date','').strip(),
            return_date, return_supplier,
            outbound_delivery, return_delivery,
            outbound_status, return_status,
            net, sell, sell - net,
            overall,
            request.form.get('remarks','').strip(),
            session.get('user_id'), session.get('username','')
        ))
        log_action('CREATE', 'sales', new_id,
                   f"{request.form.get('customer','').upper()} | "
                   f"{request.form.get('from_loc','').upper()}-{request.form.get('to_loc','').upper()} | "
                   f"Sell:{sell}")
        flash('Sale added successfully.', 'success')
        return redirect(url_for('sales_report'))
    return render_template('add.html', companies=companies, today=str(date.today()), form={})

@app.route('/edit/<int:sale_id>', methods=['GET', 'POST'])
@admin_required
def edit_sale(sale_id):
    sale = query_db('SELECT * FROM sales WHERE id=%s AND deleted=FALSE', [sale_id], one=True)
    if not sale:
        flash('Sale not found.', 'danger')
        return redirect(url_for('sales_report'))
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', sale=sale, companies=companies, edit=True)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))

        outbound_delivery = request.form.get('outbound_delivery','').strip()
        return_delivery   = request.form.get('return_delivery','').strip()
        return_date       = request.form.get('return_date','').strip()
        return_supplier   = request.form.get('return_supplier','').upper().strip()

        outbound_status, return_status, overall = compute_ticket_status(
            outbound_delivery, return_delivery
        )

        execute_db('''
            UPDATE sales SET
                from_loc=%s, to_loc=%s, via=%s, trip_type=%s, buy_from=%s,
                company=%s, tickets=%s, customer=%s, sale_date=%s, travel_date=%s,
                return_date=%s, return_supplier=%s,
                outbound_delivery=%s, return_delivery=%s,
                outbound_status=%s, return_status=%s,
                net=%s, sell=%s, profit=%s, status=%s, remarks=%s
            WHERE id=%s
        ''', (
            request.form.get('from_loc','').upper().strip(),
            request.form.get('to_loc','').upper().strip(),
            request.form.get('via','').upper().strip(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper().strip(),
            request.form.get('company','').upper().strip(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper().strip(),
            request.form.get('sale_date',''),
            request.form.get('travel_date','').strip(),
            return_date, return_supplier,
            outbound_delivery, return_delivery,
            outbound_status, return_status,
            net, sell, sell - net,
            overall,
            request.form.get('remarks','').strip(),
            sale_id
        ))
        log_action('UPDATE', 'sales', sale_id,
                   f"{request.form.get('customer','').upper()} | Sell:{sell}")
        flash('Sale updated successfully.', 'success')
        return redirect(url_for('sales_report'))
    return render_template('add.html', sale=sale, companies=companies, edit=True)

@app.route('/delete/<int:sale_id>', methods=['POST'])
@admin_required
def delete_sale(sale_id):
    sale = query_db('SELECT customer, company FROM sales WHERE id=%s', [sale_id], one=True)
    # Soft delete
    execute_db('UPDATE sales SET deleted=TRUE WHERE id=%s', [sale_id])
    log_action('DELETE', 'sales', sale_id,
               f"{sale['customer'] if sale else ''} | {sale['company'] if sale else ''}")
    flash('Sale deleted.', 'success')
    return redirect(url_for('sales_report'))

# ── Sales Report (paginated) ──────────────────────────────────────────────────
@app.route('/report')
@login_required
def sales_report():
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    agent_id  = request.args.get('agent_id', '')      # NEW: filter by sales agent
    page      = max(1, int(request.args.get('page', 1)))

    base_q  = '''
        SELECT s.*,
               COALESCE(s.created_by_username, '—') AS agent_name,
               COALESCE(u.full_name, s.created_by_username, '—') AS agent_display
        FROM sales s
        LEFT JOIN users u ON s.created_by_user_id = u.id
        WHERE s.deleted=FALSE AND s.is_archived=FALSE
    '''
    count_q = 'SELECT COALESCE(SUM(sell),0) as sell, COALESCE(SUM(net),0) as net, COALESCE(SUM(profit),0) as profit, COUNT(*) as cnt FROM sales WHERE deleted=FALSE AND is_archived=FALSE'
    params  = []
    cparams = []

    if company:
        base_q  += ' AND s.company=%s'; count_q += ' AND company=%s'
        params.append(company); cparams.append(company)
    if status:
        base_q  += ' AND s.status=%s';  count_q += ' AND status=%s'
        params.append(status); cparams.append(status)
    if date_from:
        base_q  += ' AND s.sale_date>=%s'; count_q += ' AND sale_date>=%s'
        params.append(date_from); cparams.append(date_from)
    if date_to:
        base_q  += ' AND s.sale_date<=%s'; count_q += ' AND sale_date<=%s'
        params.append(date_to); cparams.append(date_to)
    if agent_id:
        base_q  += ' AND s.created_by_user_id=%s'; count_q += ' AND created_by_user_id=%s'
        params.append(agent_id); cparams.append(agent_id)

    base_q += ' ORDER BY s.sale_date DESC, s.id DESC'

    agg = query_db(count_q, cparams, one=True)
    totals = {'sell': agg['sell'], 'net': agg['net'],
              'profit': agg['profit'], 'count': agg['cnt']}

    sales, total_rows, total_pages = paginate(base_q, params, page)

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    # All users for agent filter dropdown (admin only)
    agents = []
    if session.get('user_role') == 'admin':
        agents = query_db('SELECT id, username, COALESCE(full_name,username) AS display FROM users ORDER BY username') or []

    return render_template('report.html',
        sales=sales, totals=totals, companies=companies, agents=agents,
        filters={'company':company,'status':status,'date_from':date_from,
                 'date_to':date_to, 'agent_id':agent_id},
        page=page, total_pages=total_pages, total_rows=total_rows
    )

# ── Statement ─────────────────────────────────────────────────────────────────
@app.route('/statement')
@login_required
def statement():
    company   = request.args.get('company', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    sales, payments, total_invoiced, total_paid, balance = [], [], 0, 0, 0
    if company:
        q = 'SELECT * FROM sales WHERE deleted=FALSE AND is_archived=FALSE AND company=%s'
        p = [company]
        if date_from: q += ' AND sale_date>=%s'; p.append(date_from)
        if date_to:   q += ' AND sale_date<=%s'; p.append(date_to)
        q += ' ORDER BY sale_date ASC'
        sales = query_db(q, p)

        pq = 'SELECT * FROM payments WHERE deleted=FALSE AND is_archived=FALSE AND company=%s'
        pp = [company]
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        pq += ' ORDER BY pay_date ASC'
        payments = query_db(pq, pp)

        total_invoiced = sum(r['sell'] for r in sales)
        total_paid     = sum(r['amount'] for r in payments)
        balance        = total_invoiced - total_paid

    return render_template('statement.html',
        companies=companies, sales=sales, payments=payments,
        company=company, total_invoiced=total_invoiced,
        total_paid=total_paid, balance=balance,
        filters={'date_from':date_from,'date_to':date_to},
        today=date.today().strftime('%d %B %Y')
    )

# ── Payments (paginated) ──────────────────────────────────────────────────────
@app.route('/payments', methods=['GET', 'POST'])
@login_required
def payments():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        if session.get('user_role') != 'admin':
            flash('Admin access required to record payments.', 'danger')
            return redirect(url_for('payments'))
        company_val = request.form.get('company','').upper().strip()
        amount_val  = request.form.get('amount','').strip()
        pay_date    = request.form.get('pay_date', str(date.today()))
        if not company_val:
            flash('Company is required.', 'danger')
            return redirect(url_for('payments'))
        try:
            amount = float(amount_val)
            if amount <= 0: raise ValueError
        except ValueError:
            flash('Amount must be a positive number.', 'danger')
            return redirect(url_for('payments'))
        new_id = execute_db(
            'INSERT INTO payments (company, amount, pay_date, notes) VALUES (%s,%s,%s,%s)',
            (company_val, amount, pay_date, request.form.get('notes','').strip())
        )
        log_action('CREATE', 'payments', new_id, f"{company_val} | Amount:{amount}")
        flash('Payment recorded successfully.', 'success')
        return redirect(url_for('payments'))

    page = max(1, int(request.args.get('page', 1)))
    base_q = 'SELECT * FROM payments WHERE deleted=FALSE AND is_archived=FALSE ORDER BY pay_date DESC, id DESC'
    all_payments, total_rows, total_pages = paginate(base_q, [], page)
    total_paid = query_db(
        'SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE deleted=FALSE AND is_archived=FALSE', one=True
    )['t']
    return render_template('payments.html',
        payments=all_payments, companies=companies,
        total_paid=total_paid, today=str(date.today()),
        page=page, total_pages=total_pages, total_rows=total_rows
    )

@app.route('/payments/edit/<int:pay_id>', methods=['GET', 'POST'])
@admin_required
def edit_payment(pay_id):
    payment = query_db('SELECT * FROM payments WHERE id=%s AND deleted=FALSE', [pay_id], one=True)
    if not payment:
        flash('Payment not found.', 'danger')
        return redirect(url_for('payments'))
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            if amount <= 0: raise ValueError
        except ValueError:
            flash('Amount must be a positive number.', 'danger')
            return render_template('edit_payment.html', payment=payment,
                                   companies=companies, today=str(date.today()))
        execute_db('''
            UPDATE payments SET company=%s, amount=%s, pay_date=%s, notes=%s WHERE id=%s
        ''', (
            request.form.get('company','').upper().strip(),
            amount,
            request.form.get('pay_date', str(date.today())),
            request.form.get('notes','').strip(),
            pay_id
        ))
        log_action('UPDATE', 'payments', pay_id, f"Amount:{amount}")
        flash('Payment updated successfully.', 'success')
        return redirect(url_for('payments'))
    return render_template('edit_payment.html',
        payment=payment, companies=companies, today=str(date.today()))

@app.route('/payments/delete/<int:pay_id>', methods=['POST'])
@admin_required
def delete_payment_page(pay_id):
    p = query_db('SELECT company, amount FROM payments WHERE id=%s', [pay_id], one=True)
    execute_db('UPDATE payments SET deleted=TRUE WHERE id=%s', [pay_id])
    log_action('DELETE', 'payments', pay_id,
               f"{p['company'] if p else ''} | Amount:{p['amount'] if p else ''}")
    flash('Payment deleted.', 'success')
    return redirect(url_for('payments'))

# ── Deliver Tomorrow ──────────────────────────────────────────────────────────
@app.route('/deliver-tomorrow')
@login_required
def deliver_tomorrow():
    tomorrow_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    today_str     = str(date.today())

    # Auto-update outbound statuses (active only)
    execute_db('''
        UPDATE sales SET outbound_status='DONE'
        WHERE outbound_delivery != '' AND outbound_delivery <= %s
          AND outbound_status='PENDING' AND deleted=FALSE AND is_archived=FALSE
    ''', [today_str])
    # Auto-update return statuses (active only)
    execute_db('''
        UPDATE sales SET return_status='DONE'
        WHERE return_delivery != '' AND return_delivery <= %s
          AND return_status='PENDING' AND deleted=FALSE AND is_archived=FALSE
    ''', [today_str])
    # Update overall status to DONE when all sectors complete
    execute_db('''
        UPDATE sales SET status='DONE'
        WHERE outbound_status='DONE'
          AND (return_delivery='' OR return_status='DONE')
          AND status != 'DONE' AND deleted=FALSE AND is_archived=FALSE
    ''')

    # Outbound tickets due tomorrow — show regardless of archive status
    outbound_tickets = query_db('''
        SELECT * FROM sales
        WHERE outbound_delivery=%s AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])

    # Return tickets due tomorrow — show regardless of archive status
    return_tickets = query_db('''
        SELECT * FROM sales
        WHERE return_delivery=%s AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])

    # Old-style tickets by travel_date — show regardless of archive status
    travel_date_tickets = query_db('''
        SELECT * FROM sales
        WHERE travel_date=%s
          AND (outbound_delivery IS NULL OR outbound_delivery='')
          AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])

    tomorrow_str = (date.today() + timedelta(days=1)).strftime('%d %B %Y')
    return render_template('deliver.html',
        outbound_tickets=outbound_tickets,
        return_tickets=return_tickets,
        travel_date_tickets=travel_date_tickets,
        tomorrow=tomorrow_str
    )

@app.route('/deliver-tomorrow/send-email', methods=['POST'])
@login_required
def send_deliver_email():
    import smtplib
    import threading
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    tomorrow_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tomorrow_str  = (date.today() + timedelta(days=1)).strftime('%d %B %Y')

    # Gather all tickets
    outbound_tickets = query_db('''
        SELECT * FROM sales WHERE outbound_delivery=%s AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])
    return_tickets = query_db('''
        SELECT * FROM sales WHERE return_delivery=%s AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])
    travel_date_tickets = query_db('''
        SELECT * FROM sales WHERE travel_date=%s
          AND (outbound_delivery IS NULL OR outbound_delivery='')
          AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])

    all_tickets = list(outbound_tickets) + list(return_tickets) + list(travel_date_tickets)

    if not all_tickets:
        flash('No tickets to send — delivery list is empty for tomorrow.', 'warning')
        return redirect(url_for('deliver_tomorrow'))

    # Build HTML email table
    rows_html = ''
    for i, t in enumerate(all_tickets, 1):
        status = t.get('outbound_status') or t.get('status') or 'PENDING'
        color = '#1E7B34' if status == 'DONE' else '#E67E22'
        rows_html += f"""
        <tr style="background:{'#f0fff4' if i%2==0 else '#ffffff'}">
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center">{i}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;font-weight:bold">{t['company']}</td>
            <td style="padding:8px 12px;border:1px solid #ddd">{t['customer']}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center"><strong>{t['from_loc']}</strong></td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center"><strong>{t['to_loc']}</strong></td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center">{t.get('via') or '—'}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center">{t.get('travel_date') or t.get('return_date') or '—'}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center">{t.get('buy_from') or t.get('return_supplier') or '—'}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center">{t['tickets']}</td>
            <td style="padding:8px 12px;border:1px solid #ddd;text-align:center;color:{color};font-weight:bold">{status}</td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f4f7fc">
    <div style="max-width:900px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)">
      <div style="background:#1B3A6B;padding:24px 28px">
        <h1 style="color:#C8A84B;margin:0;font-size:20px">✈ ALSONDOS TRAVEL</h1>
        <h2 style="color:#fff;margin:8px 0 0;font-size:16px">🚨 Delivery List — {tomorrow_str}</h2>
        <p style="color:rgba(255,255,255,.7);margin:4px 0 0;font-size:13px">Total tickets: {len(all_tickets)}</p>
      </div>
      <div style="padding:20px">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#1B3A6B;color:#fff">
              <th style="padding:10px 12px;border:1px solid #1B3A6B">#</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Company</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Customer</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">From</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">To</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Via</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Travel Date</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Buy From</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Tkts</th>
              <th style="padding:10px 12px;border:1px solid #1B3A6B">Status</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
          <tfoot>
            <tr style="background:#1B3A6B;color:#fff">
              <td colspan="8" style="padding:10px 12px;border:1px solid #1B3A6B;font-weight:bold">TOTAL TICKETS</td>
              <td style="padding:10px 12px;border:1px solid #1B3A6B;font-weight:bold;text-align:center">{sum(t['tickets'] for t in all_tickets)}</td>
              <td style="border:1px solid #1B3A6B"></td>
            </tr>
          </tfoot>
        </table>
        <p style="color:#6B7A99;font-size:12px;margin-top:16px;text-align:center">
          Generated by ALSONDOS TRAVEL System — {tomorrow_str} | Sent by {session.get('username','system')}
        </p>
      </div>
    </div>
    </body></html>"""

    # Get email settings from env
    smtp_host     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port     = int(os.environ.get('SMTP_PORT', 587))
    smtp_user     = os.environ.get('SMTP_USER', '')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    email_to      = os.environ.get('NOTIFY_EMAIL', smtp_user)

    if not smtp_user or not smtp_password:
        flash('Email not configured. Set SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL in Render environment variables.', 'danger')
        return redirect(url_for('deliver_tomorrow'))

    # Build message before thread starts
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'ALSONDOS Delivery List {tomorrow_str} ({len(all_tickets)} tickets)'
    msg['From']    = smtp_user
    msg['To']      = email_to
    msg.attach(MIMEText(html_body, 'html'))
    msg_string = msg.as_string()

    # Send in background so request returns immediately (avoids 502 timeout)
    def send_background():
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(smtp_user, smtp_password)
                srv.sendmail(smtp_user, email_to, msg_string)
            logger.info(f"Email sent OK to {email_to}")
        except Exception as ex:
            logger.error(f"Background email failed: {ex}")

    import threading
    threading.Thread(target=send_background, daemon=True).start()

    log_action('EMAIL', 'sales', None,
               f"Delivery list {tomorrow_str} queued to {email_to} ({len(all_tickets)} tickets)")
    flash(f'Email sending to {email_to} — {len(all_tickets)} tickets. Check inbox in 30 seconds.', 'success')
    return redirect(url_for('deliver_tomorrow'))

# ── Admin DB viewer ───────────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin():
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    table     = request.args.get('table', 'sales')
    page      = max(1, int(request.args.get('page', 1)))

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]

    sales_data, payments_data = [], []
    total_pages = total_rows = total_payments = 1

    if table == 'payments':
        pq = 'SELECT * FROM payments WHERE deleted=FALSE AND is_archived=FALSE'
        pp = []
        if company:   pq += ' AND company=%s';   pp.append(company)
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        pq += ' ORDER BY pay_date DESC, id DESC'
        payments_data, total_rows, total_pages = paginate(pq, pp, page)
        total_payments = sum(r['amount'] for r in payments_data)
    else:
        sq = 'SELECT * FROM sales WHERE deleted=FALSE AND is_archived=FALSE'
        sp = []
        if company:   sq += ' AND company=%s';    sp.append(company)
        if status:    sq += ' AND status=%s';     sp.append(status)
        if date_from: sq += ' AND sale_date>=%s'; sp.append(date_from)
        if date_to:   sq += ' AND sale_date<=%s'; sp.append(date_to)
        sq += ' ORDER BY sale_date DESC, id DESC'
        sales_data, total_rows, total_pages = paginate(sq, sp, page)

    db_stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM sales WHERE deleted=FALSE AND is_archived=FALSE) as sales_count,
            (SELECT COUNT(*) FROM payments WHERE deleted=FALSE AND is_archived=FALSE) as payments_count,
            (SELECT COALESCE(SUM(sell),0) FROM sales WHERE deleted=FALSE AND is_archived=FALSE) as total_sell,
            (SELECT COALESCE(SUM(profit),0) FROM sales WHERE deleted=FALSE AND is_archived=FALSE) as total_profit,
            (SELECT COALESCE(SUM(amount),0) FROM payments WHERE deleted=FALSE AND is_archived=FALSE) as total_paid
    ''', one=True)

    return render_template('admin.html',
        sales=sales_data, payments=payments_data,
        companies=companies, db_stats=db_stats, table=table,
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to},
        total_payments=total_payments, today=date.today().strftime('%d %B %Y'),
        page=page, total_pages=total_pages, total_rows=total_rows
    )

@app.route('/admin/delete-payment/<int:pay_id>', methods=['POST'])
@admin_required
def delete_payment(pay_id):
    p = query_db('SELECT company, amount FROM payments WHERE id=%s', [pay_id], one=True)
    execute_db('UPDATE payments SET deleted=TRUE WHERE id=%s', [pay_id])
    log_action('DELETE', 'payments', pay_id,
               f"{p['company'] if p else ''} | {p['amount'] if p else ''}")
    return redirect(url_for('admin', table='payments'))

# ── Audit Log ─────────────────────────────────────────────────────────────────
@app.route('/audit')
@admin_required
def audit_log():
    action     = request.args.get('action', '')
    table_name = request.args.get('table_name', '')
    username   = request.args.get('username', '')
    date_from  = request.args.get('date_from', '')
    date_to    = request.args.get('date_to', '')
    page       = max(1, int(request.args.get('page', 1)))

    q = 'SELECT * FROM audit_logs WHERE 1=1'
    p = []
    if action:     q += ' AND action=%s';             p.append(action)
    if table_name: q += ' AND table_name=%s';         p.append(table_name)
    if username:   q += ' AND username ILIKE %s';     p.append(f'%{username}%')
    if date_from:  q += ' AND created_at>=%s';        p.append(date_from)
    if date_to:    q += ' AND created_at<=%s';        p.append(date_to + ' 23:59:59')
    q += ' ORDER BY created_at DESC'

    logs, total_rows, total_pages = paginate(q, p, page)

    return render_template('audit.html',
        logs=logs, page=page, total_pages=total_pages, total_rows=total_rows,
        filters={'action':action,'table_name':table_name,'username':username,
                 'date_from':date_from,'date_to':date_to}
    )

# ── Excel Export ──────────────────────────────────────────────────────────────
@app.route('/export/excel')
@login_required
def export_excel():
    wb = openpyxl.Workbook()
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill("solid", fgColor="1B3A6B")
    gold_fill    = PatternFill("solid", fgColor="C8A84B")
    center       = Alignment(horizontal="center")
    currency_fmt = '#,##0.00'

    ws1 = wb.active
    ws1.title = "Sales"
    hdrs = ["ID","Sale Date","Company","Customer","From","To","Via",
            "Trip Type","Buy From","Tickets","Travel Date",
            "Net (JOD)","Sell (JOD)","Profit (JOD)","Status","Remarks","Sales Agent"]
    ws1.append(hdrs)
    for col in range(1, len(hdrs)+1):
        c = ws1.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    sales = query_db('''
        SELECT s.*, COALESCE(u.full_name, s.created_by_username, '—') AS agent_display
        FROM sales s
        LEFT JOIN users u ON s.created_by_user_id = u.id
        WHERE s.deleted=FALSE AND s.is_archived=FALSE
        ORDER BY s.sale_date DESC, s.id DESC
    ''')
    for s in sales:
        ws1.append([s['id'],s['sale_date'],s['company'],s['customer'],
                    s['from_loc'],s['to_loc'],s['via'],s['trip_type'],
                    s['buy_from'],s['tickets'],s['travel_date'],
                    s['net'],s['sell'],s['profit'],s['status'],
                    s['remarks'], s['agent_display']])
    for row in ws1.iter_rows(min_row=2, min_col=12, max_col=14):
        for cell in row: cell.number_format = currency_fmt

    tr = ws1.max_row + 1
    ws1.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True)
    for col, attr in [(12,'net'),(13,'sell'),(14,'profit')]:
        c = ws1.cell(row=tr, column=col, value=sum(s[attr] for s in sales))
        c.font = Font(bold=True); c.fill = gold_fill; c.number_format = currency_fmt
    for i, w in enumerate([6,12,20,25,8,8,8,10,10,8,12,13,13,13,10,20,16], 1):
        ws1.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws2 = wb.create_sheet("Payments")
    pay_hdrs = ["ID","Pay Date","Company","Amount (JOD)","Notes"]
    ws2.append(pay_hdrs)
    for col in range(1, len(pay_hdrs)+1):
        c = ws2.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    pmts = query_db('SELECT * FROM payments WHERE deleted=FALSE AND is_archived=FALSE ORDER BY pay_date DESC')
    for p in pmts:
        ws2.append([p['id'],p['pay_date'],p['company'],p['amount'],p['notes']])
    for row in ws2.iter_rows(min_row=2, min_col=4, max_col=4):
        for cell in row: cell.number_format = currency_fmt

    tr2 = ws2.max_row + 1
    ws2.cell(row=tr2, column=3, value="TOTAL").font = Font(bold=True)
    c = ws2.cell(row=tr2, column=4, value=sum(p['amount'] for p in pmts))
    c.font = Font(bold=True); c.fill = gold_fill; c.number_format = currency_fmt
    for i, w in enumerate([6,12,22,14,30], 1):
        ws2.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    log_action('EXPORT', 'sales', None, 'Excel export downloaded')
    output = io.BytesIO()
    wb.save(output); output.seek(0)
    filename = f"alsondos_{date.today().strftime('%Y%m%d')}.xlsx"
    return Response(output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route('/admin/reset-data', methods=['GET', 'POST'])
@admin_required
def reset_data():
    if request.method == 'POST':
        confirm = request.form.get('confirm_text', '').strip()
        if confirm != 'DELETE ALL DATA':
            flash('Confirmation text incorrect. Nothing was deleted.', 'danger')
            return redirect(url_for('reset_data'))

        # Export to Excel first automatically before deleting
        # Then permanently delete all sales, payments, audit logs
        db = get_db()
        cur = db.cursor()

        # Get counts before delete for the log
        cur.execute('SELECT COUNT(*) FROM sales')
        sales_count = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM payments')
        pay_count = cur.fetchone()[0]

        # Hard delete everything — permanent, not soft delete
        cur.execute('DELETE FROM sales')
        cur.execute('DELETE FROM payments')
        cur.execute('DELETE FROM audit_logs')

        # Reset auto-increment sequences so IDs start from 1 again
        cur.execute("ALTER SEQUENCE sales_id_seq RESTART WITH 1")
        cur.execute("ALTER SEQUENCE payments_id_seq RESTART WITH 1")
        cur.execute("ALTER SEQUENCE audit_logs_id_seq RESTART WITH 1")

        db.commit()

        # Log the reset action (this will be the first entry in fresh audit log)
        log_action('RESET', 'system', None,
                   f"Full data reset by {session.get('username')} — "
                   f"deleted {sales_count} sales and {pay_count} payments")

        flash(f'✅ All data cleared successfully. {sales_count} sales and {pay_count} payments deleted. System starts fresh from today.', 'success')
        return redirect(url_for('index'))

    # GET — show confirmation page
    stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM sales) as sales_count,
            (SELECT COUNT(*) FROM payments) as payments_count,
            (SELECT COALESCE(SUM(sell),0) FROM sales) as total_sell,
            (SELECT COALESCE(SUM(amount),0) FROM payments) as total_paid
    ''', one=True)
    return render_template('reset_data.html', stats=stats)

@app.route('/archive', methods=['GET'])
@admin_required
def archive():
    company   = request.args.get('company', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    table     = request.args.get('table', 'sales')
    page      = max(1, int(request.args.get('page', 1)))

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE is_archived=TRUE ORDER BY company'
    )]

    sales_data, payments_data = [], []
    total_pages = total_rows = 1
    total_payments = 0

    if table == 'payments':
        pq = 'SELECT * FROM payments WHERE deleted=FALSE AND is_archived=TRUE'
        pp = []
        if company:   pq += ' AND company=%s';   pp.append(company)
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        pq += ' ORDER BY pay_date DESC, id DESC'
        payments_data, total_rows, total_pages = paginate(pq, pp, page)
        total_payments = sum(r['amount'] for r in payments_data)
    else:
        sq = 'SELECT * FROM sales WHERE deleted=FALSE AND is_archived=TRUE'
        sp = []
        if company:   sq += ' AND company=%s';    sp.append(company)
        if date_from: sq += ' AND sale_date>=%s'; sp.append(date_from)
        if date_to:   sq += ' AND sale_date<=%s'; sp.append(date_to)
        sq += ' ORDER BY sale_date DESC, id DESC'
        sales_data, total_rows, total_pages = paginate(sq, sp, page)

    archive_stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM sales WHERE is_archived=TRUE AND deleted=FALSE) as sales_count,
            (SELECT COUNT(*) FROM payments WHERE is_archived=TRUE AND deleted=FALSE) as payments_count,
            (SELECT COALESCE(SUM(sell),0) FROM sales WHERE is_archived=TRUE AND deleted=FALSE) as total_sell,
            (SELECT COALESCE(SUM(profit),0) FROM sales WHERE is_archived=TRUE AND deleted=FALSE) as total_profit,
            (SELECT COALESCE(SUM(amount),0) FROM payments WHERE is_archived=TRUE AND deleted=FALSE) as total_paid
    ''', one=True)

    return render_template('archive.html',
        sales=sales_data, payments=payments_data,
        companies=companies, archive_stats=archive_stats,
        table=table, total_payments=total_payments,
        filters={'company':company,'date_from':date_from,'date_to':date_to},
        page=page, total_pages=total_pages, total_rows=total_rows,
        today=date.today().strftime('%d %B %Y')
    )

@app.route('/archive/do-archive', methods=['GET', 'POST'])
@admin_required
def do_archive():
    if request.method == 'POST':
        archive_date = request.form.get('archive_date', '').strip()
        confirm_text = request.form.get('confirm_text', '').strip()

        if not archive_date:
            flash('Please select an archive date.', 'danger')
            return redirect(url_for('do_archive'))

        if confirm_text != 'ARCHIVE':
            flash('Confirmation text incorrect. Nothing was archived.', 'danger')
            return redirect(url_for('do_archive'))

        # Count what will be archived
        sales_count = query_db(
            'SELECT COUNT(*) as cnt FROM sales WHERE sale_date < %s AND is_archived=FALSE AND deleted=FALSE',
            [archive_date], one=True
        )['cnt']
        pay_count = query_db(
            'SELECT COUNT(*) as cnt FROM payments WHERE pay_date < %s AND is_archived=FALSE AND deleted=FALSE',
            [archive_date], one=True
        )['cnt']

        # Archive sales before the date
        execute_db(
            'UPDATE sales SET is_archived=TRUE WHERE sale_date < %s AND deleted=FALSE',
            [archive_date]
        )
        # Archive payments before the date
        execute_db(
            'UPDATE payments SET is_archived=TRUE WHERE pay_date < %s AND deleted=FALSE',
            [archive_date]
        )

        log_action('ARCHIVE', 'system', None,
                   f"Archived {sales_count} sales and {pay_count} payments before {archive_date}")

        flash(f'✅ Successfully archived {sales_count} sales and {pay_count} payments before {archive_date}. Main system now shows only new data.', 'success')
        return redirect(url_for('index'))

    # GET — show archive form with preview
    preview_date = request.args.get('preview_date', '')
    preview = None
    if preview_date:
        preview = query_db('''
            SELECT
                (SELECT COUNT(*) FROM sales WHERE sale_date < %s AND is_archived=FALSE AND deleted=FALSE) as sales_count,
                (SELECT COUNT(*) FROM payments WHERE pay_date < %s AND is_archived=FALSE AND deleted=FALSE) as payments_count,
                (SELECT COALESCE(SUM(sell),0) FROM sales WHERE sale_date < %s AND is_archived=FALSE AND deleted=FALSE) as total_sell,
                (SELECT COALESCE(SUM(amount),0) FROM payments WHERE pay_date < %s AND is_archived=FALSE AND deleted=FALSE) as total_paid
        ''', [preview_date, preview_date, preview_date, preview_date], one=True)

    return render_template('do_archive.html',
        preview=preview, preview_date=preview_date)

@app.route('/archive/restore-all-sales', methods=['POST'])
@admin_required
def restore_all_sales():
    result = query_db('SELECT COUNT(*) as cnt FROM sales WHERE is_archived=TRUE AND deleted=FALSE', one=True)
    count = result['cnt']
    execute_db('UPDATE sales SET is_archived=FALSE WHERE is_archived=TRUE AND deleted=FALSE')
    log_action('RESTORE', 'sales', None, f"Restored ALL {count} archived sales to active system")
    flash(f'✅ {count} archived sales restored to active system.', 'success')
    return redirect(url_for('archive', table='sales'))

@app.route('/archive/restore-all-payments', methods=['POST'])
@admin_required
def restore_all_payments():
    result = query_db('SELECT COUNT(*) as cnt FROM payments WHERE is_archived=TRUE AND deleted=FALSE', one=True)
    count = result['cnt']
    execute_db('UPDATE payments SET is_archived=FALSE WHERE is_archived=TRUE AND deleted=FALSE')
    log_action('RESTORE', 'payments', None, f"Restored ALL {count} archived payments to active system")
    flash(f'✅ {count} archived payments restored to active system.', 'success')
    return redirect(url_for('archive', table='payments'))

@app.route('/archive/restore-sale/<int:sale_id>', methods=['POST'])
@admin_required
def restore_sale(sale_id):
    execute_db('UPDATE sales SET is_archived=FALSE WHERE id=%s', [sale_id])
    log_action('RESTORE', 'sales', sale_id, f"Sale #{sale_id} restored from archive")
    flash(f'Sale #{sale_id} restored to active system.', 'success')
    return redirect(url_for('archive', table='sales'))

@app.route('/archive/restore-payment/<int:pay_id>', methods=['POST'])
@admin_required
def restore_payment(pay_id):
    execute_db('UPDATE payments SET is_archived=FALSE WHERE id=%s', [pay_id])
    log_action('RESTORE', 'payments', pay_id, f"Payment #{pay_id} restored from archive")
    flash(f'Payment #{pay_id} restored to active system.', 'success')
    return redirect(url_for('archive', table='payments'))

@app.route('/archive/delete-sale/<int:sale_id>', methods=['POST'])
@admin_required
def archive_delete_sale(sale_id):
    s = query_db('SELECT customer, company FROM sales WHERE id=%s', [sale_id], one=True)
    execute_db('UPDATE sales SET deleted=TRUE WHERE id=%s', [sale_id])
    log_action('DELETE', 'sales', sale_id,
               f"Permanently deleted archived sale: {s['customer'] if s else ''}")
    flash(f'Sale #{sale_id} permanently deleted.', 'success')
    return redirect(url_for('archive', table='sales'))

@app.route('/archive/delete-payment/<int:pay_id>', methods=['POST'])
@admin_required
def archive_delete_payment(pay_id):
    p = query_db('SELECT company, amount FROM payments WHERE id=%s', [pay_id], one=True)
    execute_db('UPDATE payments SET deleted=TRUE WHERE id=%s', [pay_id])
    log_action('DELETE', 'payments', pay_id,
               f"Permanently deleted archived payment: {p['company'] if p else ''}")
    flash(f'Payment #{pay_id} permanently deleted.', 'success')
    return redirect(url_for('archive', table='payments'))

@app.route('/api/companies')
@login_required
def api_companies():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    return jsonify(companies)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# ══════════════════════════════════════════════════════════════════════════════
#  EXTENSION v2 — User System · Invoices · Vouchers · Packages · Commission
# ══════════════════════════════════════════════════════════════════════════════

def init_extension_db():
    """Run once to add new tables and columns to existing schema."""
    db  = psycopg2.connect(os.environ.get("DATABASE_URL"))
    cur = db.cursor()

    # ── Add created_by_user to sales ─────────────────────────────────────────
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE sales ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER
                REFERENCES users(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_column THEN NULL; END $$;
    """)
    # ── Add split cost columns ────────────────────────────────────────────────
    for _col in [
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS outbound_cost REAL DEFAULT 0",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS return_cost   REAL DEFAULT 0",
    ]:
        cur.execute(f"DO $$ BEGIN {_col}; EXCEPTION WHEN duplicate_column THEN NULL; END $$;")
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE sales ADD COLUMN IF NOT EXISTS created_by_username TEXT DEFAULT '';
        EXCEPTION WHEN duplicate_column THEN NULL; END $$;
    """)

    # ── Extend users table ────────────────────────────────────────────────────
    extra_user_cols = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS commission_rate REAL DEFAULT 20.0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    ]
    for c in extra_user_cols:
        cur.execute(f"DO $$ BEGIN {c}; EXCEPTION WHEN duplicate_column THEN NULL; END $$;")

    # ── Invoices table ────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id              SERIAL PRIMARY KEY,
            invoice_number  TEXT NOT NULL UNIQUE,
            sale_id         INTEGER REFERENCES sales(id) ON DELETE SET NULL,
            created_by_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_by      TEXT NOT NULL DEFAULT '',
            company         TEXT NOT NULL DEFAULT '',
            customer        TEXT NOT NULL DEFAULT '',
            service_desc    TEXT DEFAULT '',
            amount          REAL NOT NULL DEFAULT 0,
            discount        REAL NOT NULL DEFAULT 0,
            total           REAL NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'UNPAID',
            invoice_date    TEXT NOT NULL,
            due_date        TEXT DEFAULT '',
            notes           TEXT DEFAULT '',
            deleted         BOOLEAN DEFAULT FALSE,
            created_at      TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    """)

    # ── Hotel Vouchers table ──────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id              SERIAL PRIMARY KEY,
            voucher_number  TEXT NOT NULL UNIQUE,
            sale_id         INTEGER REFERENCES sales(id) ON DELETE SET NULL,
            created_by_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_by      TEXT NOT NULL DEFAULT '',
            guest_name      TEXT NOT NULL DEFAULT '',
            num_guests      INTEGER DEFAULT 1,
            hotel_name      TEXT NOT NULL DEFAULT '',
            hotel_address   TEXT DEFAULT '',
            hotel_phone     TEXT DEFAULT '',
            hotel_contact   TEXT DEFAULT '',
            room_type       TEXT DEFAULT '',
            meal_plan       TEXT DEFAULT '',
            checkin_date    TEXT DEFAULT '',
            checkout_date   TEXT DEFAULT '',
            nights          INTEGER DEFAULT 0,
            include_transfer BOOLEAN DEFAULT FALSE,
            include_tours   BOOLEAN DEFAULT FALSE,
            include_insurance BOOLEAN DEFAULT FALSE,
            arrival_flight  TEXT DEFAULT '',
            pickup_sign     TEXT DEFAULT '',
            driver_contact  TEXT DEFAULT '',
            pickup_time     TEXT DEFAULT '',
            vehicle_type    TEXT DEFAULT '',
            emergency_contact TEXT DEFAULT '',
            cancellation_policy TEXT DEFAULT '',
            remarks         TEXT DEFAULT '',
            deleted         BOOLEAN DEFAULT FALSE,
            created_at      TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    """)

    # ── Packages table ────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id              SERIAL PRIMARY KEY,
            package_number  TEXT NOT NULL UNIQUE,
            sale_id         INTEGER REFERENCES sales(id) ON DELETE SET NULL,
            created_by_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_by      TEXT NOT NULL DEFAULT '',
            company         TEXT DEFAULT '',
            customer        TEXT DEFAULT '',
            package_date    TEXT DEFAULT '',
            -- Flight
            airline         TEXT DEFAULT '',
            flight_route    TEXT DEFAULT '',
            departure_date  TEXT DEFAULT '',
            return_date     TEXT DEFAULT '',
            baggage         TEXT DEFAULT '',
            pnr             TEXT DEFAULT '',
            -- Hotel
            hotel_name      TEXT DEFAULT '',
            room_type       TEXT DEFAULT '',
            meal_plan       TEXT DEFAULT '',
            checkin_date    TEXT DEFAULT '',
            checkout_date   TEXT DEFAULT '',
            nights          INTEGER DEFAULT 0,
            -- Transfer
            transfer_type   TEXT DEFAULT '',
            pickup_time     TEXT DEFAULT '',
            vehicle_type    TEXT DEFAULT '',
            -- Tours
            tour_name       TEXT DEFAULT '',
            tour_date       TEXT DEFAULT '',
            tour_included   BOOLEAN DEFAULT FALSE,
            -- Financials
            net_cost        REAL DEFAULT 0,
            sell_price      REAL DEFAULT 0,
            profit          REAL DEFAULT 0,
            status          TEXT DEFAULT 'ACTIVE',
            notes           TEXT DEFAULT '',
            deleted         BOOLEAN DEFAULT FALSE,
            created_at      TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    """)

    # ── Sequence helpers ──────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS doc_sequences (
            doc_type TEXT PRIMARY KEY,
            last_num INTEGER DEFAULT 0
        )
    """)
    for dt in ('INV', 'VCH', 'PKG'):
        cur.execute("""
            INSERT INTO doc_sequences (doc_type, last_num)
            VALUES (%s, 0) ON CONFLICT (doc_type) DO NOTHING
        """, [dt])

    # ── Indexes ───────────────────────────────────────────────────────────────
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_sales_user ON sales(created_by_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(created_by_id)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_sale ON invoices(sale_id)",
        "CREATE INDEX IF NOT EXISTS idx_vouchers_user ON vouchers(created_by_id)",
        "CREATE INDEX IF NOT EXISTS idx_vouchers_sale ON vouchers(sale_id)",
        "CREATE INDEX IF NOT EXISTS idx_packages_user ON packages(created_by_id)",
    ]:
        cur.execute(idx)

    db.commit()
    cur.close()
    db.close()

try:
    init_extension_db()
    logger.info("Extension DB initialised OK")
except Exception as _ext_err:
    logger.error(f"Extension DB init error: {_ext_err}")


# ── Sequence helper ───────────────────────────────────────────────────────────
def next_doc_number(doc_type: str) -> str:
    """Atomically increment and return formatted doc number e.g. INV-0042"""
    try:
        db  = get_db()
        cur = db.cursor()
        cur.execute("""
            UPDATE doc_sequences SET last_num = last_num + 1
            WHERE doc_type = %s RETURNING last_num
        """, [doc_type])
        row = cur.fetchone()
        db.commit()
        num = row['last_num'] if row else 1
        return f"{doc_type}-{str(num).zfill(4)}"
    except Exception:
        import uuid
        return f"{doc_type}-{uuid.uuid4().hex[:6].upper()}"


# ── Ownership guard — employees see only their own sales ─────────────────────
def own_sale_required(f):
    """Allow admins full access; users only access their own sales."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        sale_id = kwargs.get('sale_id')
        if sale_id and session.get('user_role') != 'admin':
            s = query_db('SELECT created_by_user_id FROM sales WHERE id=%s AND deleted=FALSE',
                         [sale_id], one=True)
            if not s or s['created_by_user_id'] != session['user_id']:
                flash('Access denied — you can only manage your own transactions.', 'danger')
                return redirect(url_for('my_sales'))
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════════════
#  EMPLOYEE DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/my')
@login_required
def employee_dashboard():
    if session.get('user_role') == 'admin':
        return redirect(url_for('index'))

    uid = session['user_id']
    today_str = date.today().strftime('%d %B %Y')

    # ── My stats ──────────────────────────────────────────────────────────────
    my_stats = query_db("""
        SELECT COUNT(*) as total_txns,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(profit),0) as total_profit
        FROM sales
        WHERE deleted=FALSE AND is_archived=FALSE
          AND created_by_user_id=%s
    """, [uid], one=True)
    my_stats = dict(my_stats)
    commission_rate = 20.0
    user_row = query_db('SELECT commission_rate FROM users WHERE id=%s', [uid], one=True)
    if user_row and user_row['commission_rate']:
        commission_rate = float(user_row['commission_rate'])
    my_stats['commission'] = round(float(my_stats['total_profit'] or 0) * commission_rate / 100, 3)
    my_stats['commission_rate'] = commission_rate

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    my_monthly = query_db("""
        SELECT to_char(to_date(sale_date,'YYYY-MM-DD'),'MM') as month,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(profit),0) as total_profit,
               COUNT(*) as count
        FROM sales
        WHERE deleted=FALSE AND is_archived=FALSE
          AND created_by_user_id=%s
          AND to_char(to_date(sale_date,'YYYY-MM-DD'),'YYYY') = to_char(NOW(),'YYYY')
        GROUP BY month ORDER BY month
    """, [uid])

    # ── Status counts ─────────────────────────────────────────────────────────
    sc = query_db("""
        SELECT
          SUM(CASE WHEN status='DONE'  THEN 1 ELSE 0 END) as done_count,
          SUM(CASE WHEN status='STILL' THEN 1 ELSE 0 END) as still_count,
          SUM(CASE WHEN status NOT IN ('DONE','STILL') THEN 1 ELSE 0 END) as other_count
        FROM sales WHERE deleted=FALSE AND is_archived=FALSE AND created_by_user_id=%s
    """, [uid], one=True)
    if sc:
        my_stats['done_count']  = int(sc['done_count']  or 0)
        my_stats['still_count'] = int(sc['still_count'] or 0)
        my_stats['other_count'] = int(sc['other_count'] or 0)

    # ── Recent transactions ───────────────────────────────────────────────────
    recent = query_db("""
        SELECT * FROM sales
        WHERE deleted=FALSE AND created_by_user_id=%s
        ORDER BY created_at DESC LIMIT 10
    """, [uid])

    # ── Upcoming departures ───────────────────────────────────────────────────
    upcoming = query_db("""
        SELECT * FROM sales
        WHERE deleted=FALSE AND is_archived=FALSE
          AND created_by_user_id=%s
          AND travel_date >= %s
        ORDER BY travel_date ASC LIMIT 8
    """, [uid, str(date.today())])

    # ── My invoices ───────────────────────────────────────────────────────────
    my_invoices = query_db("""
        SELECT * FROM invoices WHERE deleted=FALSE AND created_by_id=%s
        ORDER BY created_at DESC LIMIT 5
    """, [uid]) or []

    # ── My vouchers ───────────────────────────────────────────────────────────
    my_vouchers = query_db("""
        SELECT * FROM vouchers WHERE deleted=FALSE AND created_by_id=%s
        ORDER BY created_at DESC LIMIT 5
    """, [uid]) or []

    # Chart data
    chart_labels  = [str(r['month']) for r in my_monthly]
    chart_sells   = [float(r['total_sell'] or 0) for r in my_monthly]
    chart_profits = [float(r['total_profit'] or 0) for r in my_monthly]
    has_chart     = any(v > 0 for v in chart_sells)

    return render_template('employee_dashboard.html',
        my_stats=my_stats, my_monthly=my_monthly, recent=recent,
        upcoming=upcoming, my_invoices=my_invoices, my_vouchers=my_vouchers,
        chart_labels=chart_labels, chart_sells=chart_sells,
        chart_profits=chart_profits, has_chart=has_chart,
        today=today_str, commission_rate=commission_rate,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MY SALES (employee view of own transactions)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/my/sales')
@login_required
def my_sales():
    if session.get('user_role') == 'admin':
        return redirect(url_for('sales_report'))
    uid       = session['user_id']
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    page      = max(1, int(request.args.get('page', 1)))

    q = 'SELECT * FROM sales WHERE deleted=FALSE AND created_by_user_id=%s'
    cq = ('SELECT COALESCE(SUM(sell),0) as sell, COALESCE(SUM(profit),0) as profit,'
          ' COUNT(*) as cnt FROM sales WHERE deleted=FALSE AND created_by_user_id=%s')
    params = [uid]
    if company:
        q += ' AND company=%s'; cq += ' AND company=%s'; params.append(company)
    if status:
        q += ' AND status=%s';  cq += ' AND status=%s';  params.append(status)
    if date_from:
        q += ' AND sale_date>=%s'; cq += ' AND sale_date>=%s'; params.append(date_from)
    if date_to:
        q += ' AND sale_date<=%s'; cq += ' AND sale_date<=%s'; params.append(date_to)
    q += ' ORDER BY sale_date DESC, id DESC'

    agg = query_db(cq, params, one=True)
    totals = {'sell': float(agg['sell'] or 0), 'profit': float(agg['profit'] or 0), 'count': int(agg['cnt'] or 0)}
    sales, total_rows, total_pages = paginate(q, params, page)

    companies_q = query_db("""
        SELECT DISTINCT company FROM sales
        WHERE deleted=FALSE AND created_by_user_id=%s ORDER BY company
    """, [uid])
    companies = [r['company'] for r in companies_q]

    return render_template('my_sales.html',
        sales=sales, totals=totals, companies=companies,
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to},
        page=page, total_pages=total_pages, total_rows=total_rows,
    )


# ── Override add_sale to capture user ─────────────────────────────────────────
# Monkey-patch the existing add_sale to store user info
_orig_add_sale_func = app.view_functions.get('add_sale')

@app.route('/add-v2', methods=['GET', 'POST'])
@login_required
def add_sale_v2():
    """Extended add sale that captures which user created it."""
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', companies=companies,
                                   today=str(date.today()), form=request.form)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        outbound_delivery = request.form.get('outbound_delivery','').strip()
        return_delivery   = request.form.get('return_delivery','').strip()
        return_date       = request.form.get('return_date','').strip()
        return_supplier   = request.form.get('return_supplier','').upper().strip()
        outbound_status, return_status, overall = compute_ticket_status(
            outbound_delivery, return_delivery)
        _oc = float(request.form.get('outbound_cost', 0) or 0)
        _rc = float(request.form.get('return_cost', 0) or 0)
        new_id = execute_db('''
            INSERT INTO sales
            (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
             customer,sale_date,travel_date,return_date,return_supplier,
             outbound_delivery,return_delivery,outbound_status,return_status,
             net,sell,profit,status,remarks,
             outbound_cost,return_cost,
             created_by_user_id,created_by_username)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            request.form.get('from_loc','').upper().strip(),
            request.form.get('to_loc','').upper().strip(),
            request.form.get('via','').upper().strip(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper().strip(),
            request.form.get('company','').upper().strip(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper().strip(),
            request.form.get('sale_date', str(date.today())),
            request.form.get('travel_date','').strip(),
            return_date, return_supplier,
            outbound_delivery, return_delivery,
            outbound_status, return_status,
            net, sell, sell - net, overall,
            request.form.get('remarks','').strip(),
            _oc, _rc,
            session.get('user_id'), session.get('username','')
        ))
        log_action('CREATE', 'sales', new_id,
                   f"{request.form.get('customer','').upper()} | Sell:{sell}")
        flash('Transaction added successfully.', 'success')
        redirect_to = url_for('my_sales') if session.get('user_role') != 'admin' else url_for('sales_report')
        return redirect(redirect_to)
    return render_template('add.html', companies=companies, today=str(date.today()), form={})


@app.route('/my/edit/<int:sale_id>', methods=['GET', 'POST'])
@login_required
@own_sale_required
def my_edit_sale(sale_id):
    sale = query_db('SELECT * FROM sales WHERE id=%s AND deleted=FALSE', [sale_id], one=True)
    if not sale:
        flash('Transaction not found.', 'danger')
        return redirect(url_for('my_sales'))
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', sale=sale, companies=companies, edit=True)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        outbound_delivery = request.form.get('outbound_delivery','').strip()
        return_delivery   = request.form.get('return_delivery','').strip()
        return_date       = request.form.get('return_date','').strip()
        return_supplier   = request.form.get('return_supplier','').upper().strip()
        outbound_status, return_status, overall = compute_ticket_status(
            outbound_delivery, return_delivery)
        execute_db('''
            UPDATE sales SET
                from_loc=%s,to_loc=%s,via=%s,trip_type=%s,buy_from=%s,
                company=%s,tickets=%s,customer=%s,sale_date=%s,travel_date=%s,
                return_date=%s,return_supplier=%s,
                outbound_delivery=%s,return_delivery=%s,
                outbound_status=%s,return_status=%s,
                net=%s,sell=%s,profit=%s,status=%s,remarks=%s
            WHERE id=%s
        ''', (
            request.form.get('from_loc','').upper().strip(),
            request.form.get('to_loc','').upper().strip(),
            request.form.get('via','').upper().strip(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper().strip(),
            request.form.get('company','').upper().strip(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper().strip(),
            request.form.get('sale_date',''),
            request.form.get('travel_date','').strip(),
            return_date, return_supplier,
            outbound_delivery, return_delivery,
            outbound_status, return_status,
            net, sell, sell - net, overall,
            request.form.get('remarks','').strip(), sale_id
        ))
        log_action('UPDATE', 'sales', sale_id, f"Sell:{sell}")
        flash('Transaction updated.', 'success')
        return redirect(url_for('my_sales'))
    return render_template('add.html', sale=sale, companies=companies, edit=True)


# ══════════════════════════════════════════════════════════════════════════════
#  INVOICES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/invoices')
@login_required
def invoice_list():
    uid = session['user_id']
    is_admin = session.get('user_role') == 'admin'
    status    = request.args.get('status', '')
    company   = request.args.get('company', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    page      = max(1, int(request.args.get('page', 1)))

    q = 'SELECT * FROM invoices WHERE deleted=FALSE'
    if not is_admin:
        q += ' AND created_by_id=%s'
        params = [uid]
    else:
        params = []
    if status:  q += ' AND status=%s';   params.append(status)
    if company: q += ' AND company ILIKE %s'; params.append(f'%{company}%')
    if date_from: q += ' AND invoice_date>=%s'; params.append(date_from)
    if date_to:   q += ' AND invoice_date<=%s'; params.append(date_to)
    q += ' ORDER BY created_at DESC'

    invoices, total_rows, total_pages = paginate(q, params, page)
    total_amount = sum(float(i['total'] or 0) for i in invoices)

    return render_template('invoices.html',
        invoices=invoices, total_amount=total_amount,
        filters={'status':status,'company':company,'date_from':date_from,'date_to':date_to},
        page=page, total_pages=total_pages, total_rows=total_rows,
    )


@app.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def new_invoice():
    sale_id   = request.args.get('sale_id', '')
    sale      = query_db('SELECT * FROM sales WHERE id=%s AND deleted=FALSE', [sale_id], one=True) if sale_id else None
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        amount    = float(request.form.get('amount', 0))
        discount  = float(request.form.get('discount', 0))
        total     = round(amount - discount, 3)
        inv_num   = next_doc_number('INV')
        sid       = request.form.get('sale_id') or None
        new_id = execute_db("""
            INSERT INTO invoices
            (invoice_number,sale_id,created_by_id,created_by,company,customer,
             service_desc,amount,discount,total,status,invoice_date,due_date,notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            inv_num, sid,
            session['user_id'], session.get('username',''),
            request.form.get('company','').upper().strip(),
            request.form.get('customer','').upper().strip(),
            request.form.get('service_desc','').strip(),
            amount, discount, total,
            request.form.get('status','UNPAID'),
            request.form.get('invoice_date', str(date.today())),
            request.form.get('due_date',''),
            request.form.get('notes','').strip()
        ))
        log_action('CREATE', 'invoices', new_id, f"Invoice {inv_num} | {total} JOD")
        flash(f'Invoice {inv_num} created successfully.', 'success')
        return redirect(url_for('view_invoice', inv_id=new_id))
    # Load transactions for pre-selected company (from sale or URL param)
    company_filter = request.args.get('company', '')
    if sale and sale['company']:
        company_filter = sale['company']
    company_transactions = []
    if company_filter:
        company_transactions = query_db("""
            SELECT id, sale_date, customer, from_loc, to_loc, sell, status
            FROM sales
            WHERE deleted=FALSE AND is_archived=FALSE
              AND company=%s
            ORDER BY sale_date DESC
            LIMIT 50
        """, [company_filter]) or []

    return render_template('invoice_form.html',
        sale=sale, companies=companies, today=str(date.today()),
        company_filter=company_filter,
        company_transactions=company_transactions)


@app.route('/invoices/<int:inv_id>')
@login_required
def view_invoice(inv_id):
    inv = query_db('SELECT * FROM invoices WHERE id=%s AND deleted=FALSE', [inv_id], one=True)
    if not inv:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('invoice_list'))
    if session.get('user_role') != 'admin' and inv['created_by_id'] != session['user_id']:
        flash('Access denied.', 'danger')
        return redirect(url_for('invoice_list'))
    sale = query_db('SELECT * FROM sales WHERE id=%s', [inv['sale_id']], one=True) if inv['sale_id'] else None
    return render_template('invoice_view.html', inv=inv, sale=sale)


@app.route('/invoices/<int:inv_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_invoice(inv_id):
    inv = query_db('SELECT * FROM invoices WHERE id=%s AND deleted=FALSE', [inv_id], one=True)
    if not inv:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('invoice_list'))
    if session.get('user_role') != 'admin' and inv['created_by_id'] != session['user_id']:
        flash('Access denied.', 'danger')
        return redirect(url_for('invoice_list'))
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        amount   = float(request.form.get('amount', 0))
        discount = float(request.form.get('discount', 0))
        total    = round(amount - discount, 3)
        execute_db("""
            UPDATE invoices SET
                company=%s,customer=%s,service_desc=%s,amount=%s,discount=%s,total=%s,
                status=%s,invoice_date=%s,due_date=%s,notes=%s
            WHERE id=%s
        """, (
            request.form.get('company','').upper().strip(),
            request.form.get('customer','').upper().strip(),
            request.form.get('service_desc','').strip(),
            amount, discount, total,
            request.form.get('status','UNPAID'),
            request.form.get('invoice_date', str(date.today())),
            request.form.get('due_date',''),
            request.form.get('notes','').strip(), inv_id
        ))
        log_action('UPDATE', 'invoices', inv_id, f"Total:{total}")
        flash('Invoice updated.', 'success')
        return redirect(url_for('view_invoice', inv_id=inv_id))
    return render_template('invoice_form.html',
        inv=inv, companies=companies, today=str(date.today()))


@app.route('/invoices/<int:inv_id>/delete', methods=['POST'])
@login_required
def delete_invoice(inv_id):
    inv = query_db('SELECT * FROM invoices WHERE id=%s', [inv_id], one=True)
    if inv and (session.get('user_role') == 'admin' or inv['created_by_id'] == session['user_id']):
        execute_db('UPDATE invoices SET deleted=TRUE WHERE id=%s', [inv_id])
        log_action('DELETE', 'invoices', inv_id, f"Invoice {inv['invoice_number']}")
        flash('Invoice deleted.', 'success')
    return redirect(url_for('invoice_list'))



@app.route('/api/company-transactions')
@login_required
def api_company_transactions():
    """AJAX: return transactions for a company (for invoice form auto-load)."""
    company = request.args.get('company', '').strip()
    if not company:
        return {'transactions': []}
    rows = query_db("""
        SELECT id, sale_date, customer, from_loc, to_loc, via, sell, profit, status, tickets
        FROM sales
        WHERE deleted=FALSE AND is_archived=FALSE AND company=%s
        ORDER BY sale_date DESC
        LIMIT 100
    """, [company]) or []
    data = []
    for r in rows:
        data.append({
            'id':         r['id'],
            'date':       r['sale_date'],
            'customer':   r['customer'],
            'route':      f"{r['from_loc'] or ''} → {r['to_loc'] or ''}",
            'sell':       float(r['sell'] or 0),
            'status':     r['status'],
            'tickets':    r['tickets'],
        })
    from flask import jsonify
    return jsonify({'transactions': data, 'company': company})

# ══════════════════════════════════════════════════════════════════════════════
#  HOTEL VOUCHERS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/vouchers')
@login_required
def voucher_list():
    uid      = session['user_id']
    is_admin = session.get('user_role') == 'admin'
    page     = max(1, int(request.args.get('page', 1)))
    q = 'SELECT * FROM vouchers WHERE deleted=FALSE'
    params = []
    if not is_admin:
        q += ' AND created_by_id=%s'; params.append(uid)
    q += ' ORDER BY created_at DESC'
    vouchers, total_rows, total_pages = paginate(q, params, page)
    return render_template('vouchers.html',
        vouchers=vouchers, page=page,
        total_pages=total_pages, total_rows=total_rows)


@app.route('/vouchers/new', methods=['GET', 'POST'])
@login_required
def new_voucher():
    sale_id = request.args.get('sale_id', '')
    sale    = query_db('SELECT * FROM sales WHERE id=%s AND deleted=FALSE', [sale_id], one=True) if sale_id else None
    if request.method == 'POST':
        vcn    = next_doc_number('VCH')
        sid    = request.form.get('sale_id') or None
        new_id = execute_db("""
            INSERT INTO vouchers
            (voucher_number,sale_id,created_by_id,created_by,
             guest_name,num_guests,hotel_name,hotel_address,hotel_phone,hotel_contact,
             room_type,meal_plan,checkin_date,checkout_date,nights,
             include_transfer,include_tours,include_insurance,
             arrival_flight,pickup_sign,driver_contact,pickup_time,vehicle_type,
             emergency_contact,cancellation_policy,remarks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            vcn, sid,
            session['user_id'], session.get('username',''),
            request.form.get('guest_name','').strip(),
            int(request.form.get('num_guests', 1)),
            request.form.get('hotel_name','').strip(),
            request.form.get('hotel_address','').strip(),
            request.form.get('hotel_phone','').strip(),
            request.form.get('hotel_contact','').strip(),
            request.form.get('room_type','').strip(),
            request.form.get('meal_plan','').strip(),
            request.form.get('checkin_date','').strip(),
            request.form.get('checkout_date','').strip(),
            int(request.form.get('nights', 0) or 0),
            request.form.get('include_transfer') == 'on',
            request.form.get('include_tours') == 'on',
            request.form.get('include_insurance') == 'on',
            request.form.get('arrival_flight','').strip(),
            request.form.get('pickup_sign','').strip(),
            request.form.get('driver_contact','').strip(),
            request.form.get('pickup_time','').strip(),
            request.form.get('vehicle_type','').strip(),
            request.form.get('emergency_contact','').strip(),
            request.form.get('cancellation_policy','').strip(),
            request.form.get('remarks','').strip()
        ))
        log_action('CREATE', 'vouchers', new_id, f"Voucher {vcn}")
        flash(f'Voucher {vcn} created.', 'success')
        return redirect(url_for('view_voucher', vch_id=new_id))
    return render_template('voucher_form.html', sale=sale, today=str(date.today()))


@app.route('/vouchers/<int:vch_id>')
@login_required
def view_voucher(vch_id):
    vch = query_db('SELECT * FROM vouchers WHERE id=%s AND deleted=FALSE', [vch_id], one=True)
    if not vch:
        flash('Voucher not found.', 'danger')
        return redirect(url_for('voucher_list'))
    if session.get('user_role') != 'admin' and vch['created_by_id'] != session['user_id']:
        flash('Access denied.', 'danger')
        return redirect(url_for('voucher_list'))
    sale = query_db('SELECT * FROM sales WHERE id=%s', [vch['sale_id']], one=True) if vch['sale_id'] else None
    return render_template('voucher_view.html', vch=vch, sale=sale)


@app.route('/vouchers/<int:vch_id>/delete', methods=['POST'])
@login_required
def delete_voucher(vch_id):
    vch = query_db('SELECT * FROM vouchers WHERE id=%s', [vch_id], one=True)
    if vch and (session.get('user_role') == 'admin' or vch['created_by_id'] == session['user_id']):
        execute_db('UPDATE vouchers SET deleted=TRUE WHERE id=%s', [vch_id])
        log_action('DELETE', 'vouchers', vch_id, f"Voucher {vch['voucher_number']}")
        flash('Voucher deleted.', 'success')
    return redirect(url_for('voucher_list'))


# ══════════════════════════════════════════════════════════════════════════════
#  PACKAGES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/packages')
@login_required
def package_list():
    uid      = session['user_id']
    is_admin = session.get('user_role') == 'admin'
    page     = max(1, int(request.args.get('page', 1)))
    q = 'SELECT * FROM packages WHERE deleted=FALSE'
    params = []
    if not is_admin:
        q += ' AND created_by_id=%s'; params.append(uid)
    q += ' ORDER BY created_at DESC'
    pkgs, total_rows, total_pages = paginate(q, params, page)
    return render_template('packages.html', pkgs=pkgs, page=page,
                           total_pages=total_pages, total_rows=total_rows)


@app.route('/packages/new', methods=['GET', 'POST'])
@login_required
def new_package():
    sale_id  = request.args.get('sale_id', '')
    sale     = query_db('SELECT * FROM sales WHERE id=%s AND deleted=FALSE', [sale_id], one=True) if sale_id else None
    companies= [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE AND is_archived=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        net  = float(request.form.get('net_cost', 0) or 0)
        sell = float(request.form.get('sell_price', 0) or 0)
        pkg_num = next_doc_number('PKG')
        sid     = request.form.get('sale_id') or None
        new_id  = execute_db("""
            INSERT INTO packages
            (package_number,sale_id,created_by_id,created_by,company,customer,package_date,
             airline,flight_route,departure_date,return_date,baggage,pnr,
             hotel_name,room_type,meal_plan,checkin_date,checkout_date,nights,
             transfer_type,pickup_time,vehicle_type,
             tour_name,tour_date,tour_included,
             net_cost,sell_price,profit,status,notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            pkg_num, sid,
            session['user_id'], session.get('username',''),
            request.form.get('company','').upper().strip(),
            request.form.get('customer','').upper().strip(),
            request.form.get('package_date', str(date.today())),
            request.form.get('airline','').strip(),
            request.form.get('flight_route','').strip(),
            request.form.get('departure_date','').strip(),
            request.form.get('return_date','').strip(),
            request.form.get('baggage','').strip(),
            request.form.get('pnr','').upper().strip(),
            request.form.get('hotel_name','').strip(),
            request.form.get('room_type','').strip(),
            request.form.get('meal_plan','').strip(),
            request.form.get('checkin_date','').strip(),
            request.form.get('checkout_date','').strip(),
            int(request.form.get('nights', 0) or 0),
            request.form.get('transfer_type','').strip(),
            request.form.get('pickup_time','').strip(),
            request.form.get('vehicle_type','').strip(),
            request.form.get('tour_name','').strip(),
            request.form.get('tour_date','').strip(),
            request.form.get('tour_included') == 'on',
            net, sell, round(sell - net, 3),
            'ACTIVE',
            request.form.get('notes','').strip()
        ))
        log_action('CREATE', 'packages', new_id, f"Package {pkg_num}")
        flash(f'Package {pkg_num} created.', 'success')
        return redirect(url_for('view_package', pkg_id=new_id))
    return render_template('package_form.html',
        sale=sale, companies=companies, today=str(date.today()))


@app.route('/packages/<int:pkg_id>')
@login_required
def view_package(pkg_id):
    pkg = query_db('SELECT * FROM packages WHERE id=%s AND deleted=FALSE', [pkg_id], one=True)
    if not pkg:
        flash('Package not found.', 'danger')
        return redirect(url_for('package_list'))
    if session.get('user_role') != 'admin' and pkg['created_by_id'] != session['user_id']:
        flash('Access denied.', 'danger')
        return redirect(url_for('package_list'))
    return render_template('package_view.html', pkg=pkg)


@app.route('/packages/<int:pkg_id>/delete', methods=['POST'])
@login_required
def delete_package(pkg_id):
    pkg = query_db('SELECT * FROM packages WHERE id=%s', [pkg_id], one=True)
    if pkg and (session.get('user_role') == 'admin' or pkg['created_by_id'] == session['user_id']):
        execute_db('UPDATE packages SET deleted=TRUE WHERE id=%s', [pkg_id])
        log_action('DELETE', 'packages', pkg_id, f"Package {pkg['package_number']}")
        flash('Package deleted.', 'success')
    return redirect(url_for('package_list'))


# ══════════════════════════════════════════════════════════════════════════════
#  COMMISSION REPORT
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/my/commission')
@login_required
def my_commission():
    uid  = session['user_id']
    year = int(request.args.get('year', date.today().year))

    user_row = query_db('SELECT * FROM users WHERE id=%s', [uid], one=True)
    rate = float(user_row['commission_rate'] or 20) if user_row else 20.0

    monthly = query_db("""
        SELECT to_char(to_date(sale_date,'YYYY-MM-DD'),'MM') as month,
               to_char(to_date(sale_date,'YYYY-MM-DD'),'Month') as month_name,
               COALESCE(SUM(sell),0)   as total_sell,
               COALESCE(SUM(profit),0) as total_profit,
               COUNT(*) as count
        FROM sales
        WHERE deleted=FALSE AND created_by_user_id=%s
          AND to_char(to_date(sale_date,'YYYY-MM-DD'),'YYYY') = %s
        GROUP BY month, month_name ORDER BY month
    """, [uid, str(year)])

    monthly = [dict(r) for r in monthly]
    for m in monthly:
        m['commission'] = round(float(m['total_profit'] or 0) * rate / 100, 3)

    totals = {
        'sell':       sum(float(m['total_sell'] or 0)   for m in monthly),
        'profit':     sum(float(m['total_profit'] or 0) for m in monthly),
        'commission': sum(m['commission'] for m in monthly),
        'count':      sum(int(m['count'] or 0) for m in monthly),
    }
    return render_template('commission_report.html',
        monthly=monthly, totals=totals, rate=rate, year=year)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — EMPLOYEE OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/employees')
@admin_required
def admin_employees():
    employees = query_db("""
        SELECT u.id, u.username, u.full_name, u.email, u.role,
               u.commission_rate, u.is_active, u.created_at,
               COALESCE(s.txn_count, 0) as txn_count,
               COALESCE(s.total_sell, 0) as total_sell,
               COALESCE(s.total_profit, 0) as total_profit
        FROM users u
        LEFT JOIN (
            SELECT created_by_user_id,
                   COUNT(*) as txn_count,
                   SUM(sell) as total_sell,
                   SUM(profit) as total_profit
            FROM sales WHERE deleted=FALSE AND is_archived=FALSE
            GROUP BY created_by_user_id
        ) s ON u.id = s.created_by_user_id
        ORDER BY total_sell DESC NULLS LAST
    """)
    employees = [dict(e) for e in employees]
    rate = 20.0
    for e in employees:
        r = float(e['commission_rate'] or 20)
        e['commission'] = round(float(e['total_profit'] or 0) * r / 100, 3)

    total_sell   = sum(float(e['total_sell']   or 0) for e in employees)
    total_profit = sum(float(e['total_profit'] or 0) for e in employees)
    total_comm   = sum(e['commission'] for e in employees)

    return render_template('admin_employees.html',
        employees=employees,
        total_sell=total_sell, total_profit=total_profit, total_comm=total_comm,
    )


@app.route('/admin/employees/<int:uid>/sales')
@admin_required
def admin_employee_sales(uid):
    emp  = query_db('SELECT * FROM users WHERE id=%s', [uid], one=True)
    if not emp:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_employees'))
    page = max(1, int(request.args.get('page', 1)))
    q    = 'SELECT * FROM sales WHERE deleted=FALSE AND created_by_user_id=%s ORDER BY sale_date DESC, id DESC'
    sales, total_rows, total_pages = paginate(q, [uid], page)
    totals = {
        'sell':   sum(float(s['sell']   or 0) for s in sales),
        'profit': sum(float(s['profit'] or 0) for s in sales),
        'count':  total_rows,
    }
    rate = float(emp['commission_rate'] or 20)
    totals['commission'] = round(totals['profit'] * rate / 100, 3)
    return render_template('admin_employee_sales.html',
        emp=emp, sales=sales, totals=totals,
        page=page, total_pages=total_pages, total_rows=total_rows, rate=rate,
    )


@app.route('/admin/employees/<int:uid>/commission-rate', methods=['POST'])
@admin_required
def set_commission_rate(uid):
    try:
        rate = float(request.form.get('commission_rate', 20))
        rate = max(0, min(100, rate))
        execute_db('UPDATE users SET commission_rate=%s WHERE id=%s', (rate, uid))
        flash(f'Commission rate updated to {rate}%.', 'success')
    except ValueError:
        flash('Invalid commission rate.', 'danger')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/<int:uid>/update', methods=['POST'])
@admin_required
def admin_update_employee(uid):
    full_name = request.form.get('full_name','').strip()
    email     = request.form.get('email','').strip()
    phone     = request.form.get('phone','').strip()
    execute_db('UPDATE users SET full_name=%s, email=%s, phone=%s WHERE id=%s',
               (full_name, email, phone, uid))
    flash('Employee profile updated.', 'success')
    return redirect(url_for('admin_employees'))

