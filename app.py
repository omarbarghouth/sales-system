import os
import json
import psycopg2
import psycopg2.extras
from datetime import date, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, flash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'alsondos-travel-secret-2024')

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    raise Exception("❌ DATABASE_URL not set in environment")
# ── Database helpers ──────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        url = DATABASE_URL
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        db = g._database = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(query, args)
    db.commit()
    try:
        return cur.fetchone()['id']
    except:
        return None

def init_db():
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    conn = psycopg2.connect(url)
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id          SERIAL PRIMARY KEY,
            from_loc    TEXT NOT NULL DEFAULT '',
            to_loc      TEXT NOT NULL DEFAULT '',
            via         TEXT DEFAULT '',
            trip_type   TEXT DEFAULT '',
            buy_from    TEXT DEFAULT '',
            company     TEXT NOT NULL DEFAULT '',
            tickets     INTEGER DEFAULT 1,
            customer    TEXT NOT NULL DEFAULT '',
            sale_date   TEXT NOT NULL DEFAULT '',
            travel_date TEXT DEFAULT '',
            net         REAL NOT NULL DEFAULT 0,
            sell        REAL NOT NULL DEFAULT 0,
            profit      REAL NOT NULL DEFAULT 0,
            status      TEXT DEFAULT 'STILL',
            remarks     TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id          SERIAL PRIMARY KEY,
            company     TEXT NOT NULL DEFAULT '',
            amount      REAL NOT NULL DEFAULT 0,
            pay_date    TEXT NOT NULL DEFAULT '',
            notes       TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')

    conn.commit()

    # Seed from JSON if sales table is empty
    cur.execute('SELECT COUNT(*) as cnt FROM sales')
    count = cur.fetchone()['cnt'] if hasattr(cur.fetchone.__self__, 'fetchone') else 0

    # re-fetch after above call consumed the result
    cur.execute('SELECT COUNT(*) FROM sales')
    count = cur.fetchone()[0]

    if count == 0:
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
                ''', (
                    row['from_loc'], row['to_loc'], row['via'], row['trip_type'],
                    row['buy_from'], row['company'], row['tickets'], row['customer'],
                    row['sale_date'], row['travel_date'], row['net'], row['sell'],
                    row['profit'], row['status'], row['remarks']
                ))
            conn.commit()
            print(f"✅ Seeded {len(rows)} records from Excel data")

    cur.close()
    conn.close()

# Run init on startup (works with gunicorn)
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error: {e}")

# ── Helper ────────────────────────────────────────────────────────────────────
def get_companies():
    return [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]

# ── DASHBOARD ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    stats = query_db('''
        SELECT COUNT(*) as total_transactions,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(net),0) as total_net,
               COALESCE(SUM(profit),0) as total_profit
        FROM sales
    ''', one=True)

    total_paid_row = query_db('SELECT COALESCE(SUM(amount),0) as paid FROM payments', one=True)
    total_paid = float(total_paid_row['paid']) if total_paid_row else 0
    balance = float(stats['total_sell'] or 0) - total_paid

    monthly = query_db('''
        SELECT TO_CHAR(TO_DATE(sale_date,'YYYY-MM-DD'),'MM') as month,
               COALESCE(SUM(sell),0) as total_sell,
               COALESCE(SUM(profit),0) as total_profit,
               COUNT(*) as count
        FROM sales
        WHERE sale_date LIKE %s
        GROUP BY month ORDER BY month
    ''', (f"{date.today().year}%",))

    top_companies = query_db('''
        SELECT company, COALESCE(SUM(sell),0) as total, COUNT(*) as cnt
        FROM sales GROUP BY company ORDER BY total DESC LIMIT 10
    ''')

    tomorrow_str = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tomorrow_tickets = query_db('''
        SELECT * FROM sales WHERE travel_date = %s ORDER BY company
    ''', (tomorrow_str,))

    return render_template('index.html',
        stats=stats, total_paid=total_paid, balance=balance,
        monthly=monthly, top_companies=top_companies,
        tomorrow=tomorrow_tickets,
        today=date.today().strftime('%d %B %Y')
    )

# ── ADD SALE ──────────────────────────────────────────────────────────────────
@app.route('/add', methods=['GET', 'POST'])
def add_sale():
    companies = get_companies()
    if request.method == 'POST':
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        execute_db('''
            INSERT INTO sales
            (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
             customer,sale_date,travel_date,net,sell,profit,status,remarks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            request.form.get('from_loc','').upper(),
            request.form.get('to_loc','').upper(),
            request.form.get('via','').upper(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper(),
            request.form.get('company','').upper(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper(),
            request.form.get('sale_date', str(date.today())),
            request.form.get('travel_date',''),
            net, sell, sell - net,
            request.form.get('status','STILL'),
            request.form.get('remarks','')
        ))
        flash('✅ Transaction added successfully!', 'success')
        return redirect(url_for('sales_report'))
    return render_template('add.html', companies=companies, today=str(date.today()))

# ── EDIT SALE ─────────────────────────────────────────────────────────────────
@app.route('/edit/<int:sale_id>', methods=['GET', 'POST'])
def edit_sale(sale_id):
    sale = query_db('SELECT * FROM sales WHERE id=%s', [sale_id], one=True)
    if not sale:
        return redirect(url_for('sales_report'))
    companies = get_companies()
    if request.method == 'POST':
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        execute_db('''
            UPDATE sales SET
                from_loc=%s, to_loc=%s, via=%s, trip_type=%s, buy_from=%s,
                company=%s, tickets=%s, customer=%s, sale_date=%s, travel_date=%s,
                net=%s, sell=%s, profit=%s, status=%s, remarks=%s
            WHERE id=%s
        ''', (
            request.form.get('from_loc','').upper(),
            request.form.get('to_loc','').upper(),
            request.form.get('via','').upper(),
            request.form.get('trip_type',''),
            request.form.get('buy_from','').upper(),
            request.form.get('company','').upper(),
            int(request.form.get('tickets', 1)),
            request.form.get('customer','').upper(),
            request.form.get('sale_date',''),
            request.form.get('travel_date',''),
            net, sell, sell - net,
            request.form.get('status','STILL'),
            request.form.get('remarks',''),
            sale_id
        ))
        flash('✅ Transaction updated successfully!', 'success')
        return redirect(url_for('sales_report'))
    return render_template('add.html', sale=sale, companies=companies,
                           edit=True, today=str(date.today()))

# ── DELETE SALE ───────────────────────────────────────────────────────────────
@app.route('/delete/<int:sale_id>', methods=['POST'])
def delete_sale(sale_id):
    execute_db('DELETE FROM sales WHERE id=%s', [sale_id])
    flash('🗑 Transaction deleted.', 'info')
    return redirect(url_for('sales_report'))

# ── SALES REPORT ──────────────────────────────────────────────────────────────
@app.route('/report')
def sales_report():
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    query  = 'SELECT * FROM sales WHERE 1=1'
    params = []
    if company:   query += ' AND company=%s';    params.append(company)
    if status:    query += ' AND status=%s';     params.append(status)
    if date_from: query += ' AND sale_date>=%s'; params.append(date_from)
    if date_to:   query += ' AND sale_date<=%s'; params.append(date_to)
    query += ' ORDER BY sale_date DESC, id DESC'

    sales = query_db(query, params)
    totals = {
        'sell':   sum(float(r['sell']) for r in sales),
        'net':    sum(float(r['net']) for r in sales),
        'profit': sum(float(r['profit']) for r in sales),
        'count':  len(sales)
    }
    return render_template('report.html', sales=sales, totals=totals,
        companies=get_companies(),
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to}
    )

# ── STATEMENT ─────────────────────────────────────────────────────────────────
@app.route('/statement')
def statement():
    company   = request.args.get('company', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    sales, payments_list, total_invoiced, total_paid, balance = [], [], 0, 0, 0

    if company:
        q = 'SELECT * FROM sales WHERE company=%s'
        p = [company]
        if date_from: q += ' AND sale_date>=%s'; p.append(date_from)
        if date_to:   q += ' AND sale_date<=%s'; p.append(date_to)
        sales = query_db(q + ' ORDER BY sale_date ASC', p)

        pq = 'SELECT * FROM payments WHERE company=%s'
        pp = [company]
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        payments_list = query_db(pq + ' ORDER BY pay_date ASC', pp)

        total_invoiced = sum(float(r['sell']) for r in sales)
        total_paid     = sum(float(r['amount']) for r in payments_list)
        balance        = total_invoiced - total_paid

    return render_template('statement.html',
        companies=get_companies(), sales=sales, payments=payments_list,
        company=company, total_invoiced=total_invoiced,
        total_paid=total_paid, balance=balance,
        filters={'date_from':date_from,'date_to':date_to},
        today=date.today().strftime('%d %B %Y')
    )

# ── PAYMENTS ──────────────────────────────────────────────────────────────────
@app.route('/payments', methods=['GET', 'POST'])
def payments():
    companies = get_companies()
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        if action == 'delete':
            pay_id = request.form.get('pay_id')
            execute_db('DELETE FROM payments WHERE id=%s', [pay_id])
            flash('🗑 Payment deleted.', 'info')
        elif action == 'edit':
            pay_id = request.form.get('pay_id')
            execute_db('''
                UPDATE payments SET company=%s, amount=%s, pay_date=%s, notes=%s
                WHERE id=%s
            ''', (
                request.form.get('company','').upper(),
                float(request.form.get('amount', 0)),
                request.form.get('pay_date', str(date.today())),
                request.form.get('notes',''),
                pay_id
            ))
            flash('✅ Payment updated successfully!', 'success')
        else:
            execute_db('''
                INSERT INTO payments (company, amount, pay_date, notes)
                VALUES (%s,%s,%s,%s)
            ''', (
                request.form.get('company','').upper(),
                float(request.form.get('amount', 0)),
                request.form.get('pay_date', str(date.today())),
                request.form.get('notes','')
            ))
            flash('✅ Payment recorded successfully!', 'success')
        return redirect(url_for('payments'))

    edit_id   = request.args.get('edit')
    edit_pay  = query_db('SELECT * FROM payments WHERE id=%s', [edit_id], one=True) if edit_id else None
    all_pays  = query_db('SELECT * FROM payments ORDER BY pay_date DESC, id DESC')
    total_paid = sum(float(r['amount']) for r in all_pays)

    # Per company balance
    company_balances = query_db('''
        SELECT s.company,
               COALESCE(SUM(s.sell),0) as total_sell,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.company=s.company),0) as total_paid
        FROM sales s
        GROUP BY s.company
        ORDER BY s.company
    ''')

    return render_template('payments.html',
        payments=all_pays, companies=companies,
        total_paid=total_paid, today=str(date.today()),
        edit_pay=edit_pay, company_balances=company_balances
    )

# ── DELIVER TOMORROW ──────────────────────────────────────────────────────────
@app.route('/deliver-tomorrow')
def deliver_tomorrow():
    tomorrow_str = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tickets = query_db('''
        SELECT * FROM sales WHERE travel_date=%s ORDER BY company, customer
    ''', (tomorrow_str,))
    tomorrow_label = (date.today() + timedelta(days=1)).strftime('%d %B %Y')
    return render_template('deliver.html', tickets=tickets, tomorrow=tomorrow_label)

# ── ADMIN / DATA VIEWER ───────────────────────────────────────────────────────
@app.route('/admin')
def admin():
    table    = request.args.get('table', 'sales')
    search   = request.args.get('search', '')
    page     = int(request.args.get('page', 1))
    per_page = 50
    offset   = (page - 1) * per_page

    if table == 'payments':
        if search:
            rows = query_db(
                'SELECT * FROM payments WHERE company ILIKE %s ORDER BY id DESC LIMIT %s OFFSET %s',
                (f'%{search}%', per_page, offset)
            )
            total = query_db(
                'SELECT COUNT(*) as cnt FROM payments WHERE company ILIKE %s',
                (f'%{search}%',), one=True
            )['cnt']
        else:
            rows = query_db('SELECT * FROM payments ORDER BY id DESC LIMIT %s OFFSET %s',
                            (per_page, offset))
            total = query_db('SELECT COUNT(*) as cnt FROM payments', one=True)['cnt']
        columns = ['id','company','amount','pay_date','notes','created_at']
    else:
        if search:
            rows = query_db(
                '''SELECT * FROM sales WHERE company ILIKE %s OR customer ILIKE %s
                   ORDER BY id DESC LIMIT %s OFFSET %s''',
                (f'%{search}%', f'%{search}%', per_page, offset)
            )
            total = query_db(
                'SELECT COUNT(*) as cnt FROM sales WHERE company ILIKE %s OR customer ILIKE %s',
                (f'%{search}%', f'%{search}%'), one=True
            )['cnt']
        else:
            rows = query_db('SELECT * FROM sales ORDER BY id DESC LIMIT %s OFFSET %s',
                            (per_page, offset))
            total = query_db('SELECT COUNT(*) as cnt FROM sales', one=True)['cnt']
        columns = ['id','company','customer','from_loc','to_loc','sale_date',
                   'travel_date','tickets','net','sell','profit','status','remarks']

    total_pages = (total + per_page - 1) // per_page

    return render_template('admin.html',
        rows=rows, columns=columns, table=table,
        search=search, page=page, total=total,
        total_pages=total_pages, per_page=per_page
    )

@app.route('/admin/delete/<table>/<int:row_id>', methods=['POST'])
def admin_delete(table, row_id):
    if table in ('sales', 'payments'):
        execute_db(f'DELETE FROM {table} WHERE id=%s', [row_id])
        flash(f'🗑 Record deleted from {table}.', 'info')
    return redirect(url_for('admin', table=table))

@app.route('/api/companies')
def api_companies():
    return jsonify(get_companies())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
