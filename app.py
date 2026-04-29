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
    db.commit()

    # Indexes for performance
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_sales_company    ON sales(company)",
        "CREATE INDEX IF NOT EXISTS idx_sales_sale_date  ON sales(sale_date)",
        "CREATE INDEX IF NOT EXISTS idx_sales_status     ON sales(status)",
        "CREATE INDEX IF NOT EXISTS idx_sales_travel     ON sales(travel_date)",
        "CREATE INDEX IF NOT EXISTS idx_sales_deleted    ON sales(deleted)",
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
    users = query_db('SELECT id, username, role, created_at FROM users ORDER BY id')
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

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    stats = query_db('''
        SELECT COUNT(*) as total_transactions,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(net),0) as total_net,
               COALESCE(SUM(profit),0) as total_profit
        FROM sales WHERE deleted=FALSE
    ''', one=True)
    total_paid = query_db(
        'SELECT COALESCE(SUM(amount),0) as paid FROM payments WHERE deleted=FALSE', one=True
    )['paid']
    balance = (stats['total_sell'] or 0) - total_paid

    monthly = query_db('''
        SELECT to_char(to_date(sale_date,'YYYY-MM-DD'),'MM') as month,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(profit),0) as total_profit,
               COUNT(*) as count
        FROM sales
        WHERE deleted=FALSE
          AND to_char(to_date(sale_date,'YYYY-MM-DD'),'YYYY') = to_char(NOW(),'YYYY')
        GROUP BY month ORDER BY month
    ''')

    top_companies = query_db('''
        SELECT company, COALESCE(SUM(sell),0) as total, COUNT(*) as cnt
        FROM sales WHERE deleted=FALSE
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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]

    return render_template('index.html',
        stats=stats, total_paid=total_paid, balance=balance,
        monthly=monthly, top_companies=top_companies,
        tomorrow=tomorrow, companies=companies,
        recent_logs=recent_logs,
        today=date.today().strftime('%d %B %Y')
    )

# ── Sales ─────────────────────────────────────────────────────────────────────
@app.route('/add', methods=['GET', 'POST'])
@admin_required
def add_sale():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', companies=companies,
                                   today=str(date.today()), form=request.form)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        new_id = execute_db('''
            INSERT INTO sales
            (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
             customer,sale_date,travel_date,net,sell,profit,status,remarks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            request.form.get('travel_date',''),
            net, sell, sell - net,
            request.form.get('status','STILL'),
            request.form.get('remarks','').strip()
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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]
    if request.method == 'POST':
        errors = validate_sale_form(request.form)
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('add.html', sale=sale, companies=companies, edit=True)
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        execute_db('''
            UPDATE sales SET
                from_loc=%s, to_loc=%s, via=%s, trip_type=%s, buy_from=%s,
                company=%s, tickets=%s, customer=%s, sale_date=%s, travel_date=%s,
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
            request.form.get('travel_date',''),
            net, sell, sell - net,
            request.form.get('status','STILL'),
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
    page      = max(1, int(request.args.get('page', 1)))

    base_q  = 'SELECT * FROM sales WHERE deleted=FALSE'
    count_q = 'SELECT COALESCE(SUM(sell),0) as sell, COALESCE(SUM(net),0) as net, COALESCE(SUM(profit),0) as profit, COUNT(*) as cnt FROM sales WHERE deleted=FALSE'
    params  = []

    if company:
        base_q  += ' AND company=%s'; count_q += ' AND company=%s'; params.append(company)
    if status:
        base_q  += ' AND status=%s';  count_q += ' AND status=%s';  params.append(status)
    if date_from:
        base_q  += ' AND sale_date>=%s'; count_q += ' AND sale_date>=%s'; params.append(date_from)
    if date_to:
        base_q  += ' AND sale_date<=%s'; count_q += ' AND sale_date<=%s'; params.append(date_to)

    base_q += ' ORDER BY sale_date DESC, id DESC'

    # Totals (all matching rows, not just current page)
    agg = query_db(count_q, params, one=True)
    totals = {'sell': agg['sell'], 'net': agg['net'],
              'profit': agg['profit'], 'count': agg['cnt']}

    sales, total_rows, total_pages = paginate(base_q, params, page)

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]
    return render_template('report.html',
        sales=sales, totals=totals, companies=companies,
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to},
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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]
    sales, payments, total_invoiced, total_paid, balance = [], [], 0, 0, 0
    if company:
        q = 'SELECT * FROM sales WHERE deleted=FALSE AND company=%s'
        p = [company]
        if date_from: q += ' AND sale_date>=%s'; p.append(date_from)
        if date_to:   q += ' AND sale_date<=%s'; p.append(date_to)
        q += ' ORDER BY sale_date ASC'
        sales = query_db(q, p)

        pq = 'SELECT * FROM payments WHERE deleted=FALSE AND company=%s'
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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
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
    base_q = 'SELECT * FROM payments WHERE deleted=FALSE ORDER BY pay_date DESC, id DESC'
    all_payments, total_rows, total_pages = paginate(base_q, [], page)
    total_paid = query_db(
        'SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE deleted=FALSE', one=True
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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
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
    tickets = query_db('''
        SELECT * FROM sales WHERE travel_date=%s AND deleted=FALSE
        ORDER BY company, customer
    ''', [tomorrow_date])
    tomorrow_str = (date.today() + timedelta(days=1)).strftime('%d %B %Y')
    return render_template('deliver.html', tickets=tickets, tomorrow=tomorrow_str)

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
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]

    sales_data, payments_data = [], []
    total_pages = total_rows = total_payments = 1

    if table == 'payments':
        pq = 'SELECT * FROM payments WHERE deleted=FALSE'
        pp = []
        if company:   pq += ' AND company=%s';   pp.append(company)
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        pq += ' ORDER BY pay_date DESC, id DESC'
        payments_data, total_rows, total_pages = paginate(pq, pp, page)
        total_payments = sum(r['amount'] for r in payments_data)
    else:
        sq = 'SELECT * FROM sales WHERE deleted=FALSE'
        sp = []
        if company:   sq += ' AND company=%s';    sp.append(company)
        if status:    sq += ' AND status=%s';     sp.append(status)
        if date_from: sq += ' AND sale_date>=%s'; sp.append(date_from)
        if date_to:   sq += ' AND sale_date<=%s'; sp.append(date_to)
        sq += ' ORDER BY sale_date DESC, id DESC'
        sales_data, total_rows, total_pages = paginate(sq, sp, page)

    db_stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM sales WHERE deleted=FALSE) as sales_count,
            (SELECT COUNT(*) FROM payments WHERE deleted=FALSE) as payments_count,
            (SELECT COALESCE(SUM(sell),0) FROM sales WHERE deleted=FALSE) as total_sell,
            (SELECT COALESCE(SUM(profit),0) FROM sales WHERE deleted=FALSE) as total_profit,
            (SELECT COALESCE(SUM(amount),0) FROM payments WHERE deleted=FALSE) as total_paid
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
            "Net (USD)","Sell (USD)","Profit (USD)","Status","Remarks"]
    ws1.append(hdrs)
    for col in range(1, len(hdrs)+1):
        c = ws1.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    sales = query_db('SELECT * FROM sales WHERE deleted=FALSE ORDER BY sale_date DESC, id DESC')
    for s in sales:
        ws1.append([s['id'],s['sale_date'],s['company'],s['customer'],
                    s['from_loc'],s['to_loc'],s['via'],s['trip_type'],
                    s['buy_from'],s['tickets'],s['travel_date'],
                    s['net'],s['sell'],s['profit'],s['status'],s['remarks']])
    for row in ws1.iter_rows(min_row=2, min_col=12, max_col=14):
        for cell in row: cell.number_format = currency_fmt

    tr = ws1.max_row + 1
    ws1.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True)
    for col, attr in [(12,'net'),(13,'sell'),(14,'profit')]:
        c = ws1.cell(row=tr, column=col, value=sum(s[attr] for s in sales))
        c.font = Font(bold=True); c.fill = gold_fill; c.number_format = currency_fmt
    for i, w in enumerate([6,12,20,25,8,8,8,10,10,8,12,13,13,13,10,20], 1):
        ws1.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws2 = wb.create_sheet("Payments")
    pay_hdrs = ["ID","Pay Date","Company","Amount (USD)","Notes"]
    ws2.append(pay_hdrs)
    for col in range(1, len(pay_hdrs)+1):
        c = ws2.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    pmts = query_db('SELECT * FROM payments WHERE deleted=FALSE ORDER BY pay_date DESC')
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

@app.route('/api/companies')
@login_required
def api_companies():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales WHERE deleted=FALSE ORDER BY company'
    )]
    return jsonify(companies)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
