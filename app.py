import os
import json
import sqlite3
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, g

app = Flask(__name__)
DATABASE = os.path.join(os.path.dirname(__file__), 'alsondos.db')

# ── Database helpers ──────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    return cur.lastrowid

def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at  TEXT DEFAULT (datetime('now'))
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company     TEXT NOT NULL,
            amount      REAL NOT NULL,
            pay_date    TEXT NOT NULL,
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    ''')
    db.commit()

    # Seed from Excel data if DB is empty
    count = db.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
    if count == 0:
        seed_file = os.path.join(os.path.dirname(__file__), 'seed_data.json')
        if os.path.exists(seed_file):
            with open(seed_file) as f:
                rows = json.load(f)
            for row in rows:
                db.execute('''
                    INSERT INTO sales
                    (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
                     customer,sale_date,travel_date,net,sell,profit,status,remarks)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    row['from_loc'], row['to_loc'], row['via'], row['trip_type'],
                    row['buy_from'], row['company'], row['tickets'], row['customer'],
                    row['sale_date'], row['travel_date'], row['net'], row['sell'],
                    row['profit'], row['status'], row['remarks']
                ))
            db.commit()
            print(f"✅ Seeded {len(rows)} records from Excel")
    db.close()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Dashboard stats
    stats = query_db('''
        SELECT
            COUNT(*) as total_transactions,
            SUM(sell) as total_sell,
            SUM(net) as total_net,
            SUM(profit) as total_profit
        FROM sales
    ''', one=True)
    total_paid = query_db('SELECT COALESCE(SUM(amount),0) as paid FROM payments', one=True)['paid']
    balance = (stats['total_sell'] or 0) - total_paid

    # Monthly stats (current year)
    monthly = query_db('''
        SELECT
            strftime('%m', sale_date) as month,
            SUM(sell) as total_sell,
            SUM(profit) as total_profit,
            COUNT(*) as count
        FROM sales
        WHERE strftime('%Y', sale_date) = strftime('%Y', 'now')
        GROUP BY month
        ORDER BY month
    ''')

    # Top companies
    top_companies = query_db('''
        SELECT company, SUM(sell) as total, COUNT(*) as cnt
        FROM sales
        GROUP BY company
        ORDER BY total DESC
        LIMIT 10
    ''')

    # Deliver tomorrow
    tomorrow = query_db('''
        SELECT company, customer, from_loc, to_loc, travel_date, tickets, status
        FROM sales
        WHERE travel_date = date('now', '+1 day')
        ORDER BY company
    ''')

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]

    return render_template('index.html',
        stats=stats,
        total_paid=total_paid,
        balance=balance,
        monthly=monthly,
        top_companies=top_companies,
        tomorrow=tomorrow,
        companies=companies,
        today=date.today().strftime('%d %B %Y')
    )

@app.route('/add', methods=['GET', 'POST'])
def add_sale():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]
    if request.method == 'POST':
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        execute_db('''
            INSERT INTO sales
            (from_loc,to_loc,via,trip_type,buy_from,company,tickets,
             customer,sale_date,travel_date,net,sell,profit,status,remarks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        return redirect(url_for('sales_report'))
    return render_template('add.html', companies=companies, today=str(date.today()))

@app.route('/edit/<int:sale_id>', methods=['GET', 'POST'])
def edit_sale(sale_id):
    sale = query_db('SELECT * FROM sales WHERE id=?', [sale_id], one=True)
    if not sale:
        return redirect(url_for('sales_report'))
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]
    if request.method == 'POST':
        net  = float(request.form.get('net', 0))
        sell = float(request.form.get('sell', 0))
        execute_db('''
            UPDATE sales SET
                from_loc=?, to_loc=?, via=?, trip_type=?, buy_from=?,
                company=?, tickets=?, customer=?, sale_date=?, travel_date=?,
                net=?, sell=?, profit=?, status=?, remarks=?
            WHERE id=?
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
        return redirect(url_for('sales_report'))
    return render_template('add.html', sale=sale, companies=companies, edit=True)

@app.route('/delete/<int:sale_id>', methods=['POST'])
def delete_sale(sale_id):
    execute_db('DELETE FROM sales WHERE id=?', [sale_id])
    return redirect(url_for('sales_report'))

@app.route('/report')
def sales_report():
    company  = request.args.get('company', '')
    status   = request.args.get('status', '')
    date_from= request.args.get('date_from', '')
    date_to  = request.args.get('date_to', '')

    query  = 'SELECT * FROM sales WHERE 1=1'
    params = []
    if company:
        query += ' AND company=?'; params.append(company)
    if status:
        query += ' AND status=?'; params.append(status)
    if date_from:
        query += ' AND sale_date>=?'; params.append(date_from)
    if date_to:
        query += ' AND sale_date<=?'; params.append(date_to)
    query += ' ORDER BY sale_date DESC, id DESC'

    sales = query_db(query, params)

    totals = {
        'sell':   sum(r['sell'] for r in sales),
        'net':    sum(r['net'] for r in sales),
        'profit': sum(r['profit'] for r in sales),
        'count':  len(sales)
    }

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]

    return render_template('report.html',
        sales=sales, totals=totals, companies=companies,
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to}
    )

@app.route('/statement')
def statement():
    company   = request.args.get('company', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]

    sales, payments, total_invoiced, total_paid, balance = [], [], 0, 0, 0

    if company:
        query  = 'SELECT * FROM sales WHERE company=?'
        params = [company]
        if date_from: query += ' AND sale_date>=?'; params.append(date_from)
        if date_to:   query += ' AND sale_date<=?'; params.append(date_to)
        query += ' ORDER BY sale_date ASC'
        sales = query_db(query, params)

        pay_query  = 'SELECT * FROM payments WHERE company=?'
        pay_params = [company]
        if date_from: pay_query += ' AND pay_date>=?'; pay_params.append(date_from)
        if date_to:   pay_query += ' AND pay_date<=?'; pay_params.append(date_to)
        pay_query += ' ORDER BY pay_date ASC'
        payments = query_db(pay_query, pay_params)

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

@app.route('/payments', methods=['GET', 'POST'])
def payments():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]
    if request.method == 'POST':
        execute_db('''
            INSERT INTO payments (company, amount, pay_date, notes)
            VALUES (?,?,?,?)
        ''', (
            request.form.get('company','').upper(),
            float(request.form.get('amount', 0)),
            request.form.get('pay_date', str(date.today())),
            request.form.get('notes','')
        ))
        return redirect(url_for('payments'))

    all_payments = query_db('SELECT * FROM payments ORDER BY pay_date DESC')
    total_paid = sum(r['amount'] for r in all_payments)
    return render_template('payments.html',
        payments=all_payments, companies=companies,
        total_paid=total_paid, today=str(date.today())
    )

@app.route('/deliver-tomorrow')
def deliver_tomorrow():
    tickets = query_db('''
        SELECT * FROM sales
        WHERE travel_date = date('now', '+1 day')
        ORDER BY company, customer
    ''')
    tomorrow_str = ''
    try:
        from datetime import timedelta
        tomorrow_str = (date.today() + timedelta(days=1)).strftime('%d %B %Y')
    except: pass
    return render_template('deliver.html', tickets=tickets, tomorrow=tomorrow_str)

@app.route('/api/companies')
def api_companies():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]
    return jsonify(companies)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
