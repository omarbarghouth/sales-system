import os
import json
import psycopg2
import psycopg2.extras
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, Response
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Response
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

app = Flask(__name__)

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
    cur = get_db().cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(query, args)
    db.commit()
    # Return last inserted id if available
    try:
        cur.execute("SELECT lastval()")
        return cur.fetchone()['lastval']
    except Exception:
        db.commit()
        return None

def init_db():
    """Create tables if they don't exist. Called at startup."""
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
            created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )
    ''')

    db.commit()

    # Seed from JSON if DB is empty
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
            db.commit()
            print(f"✅ Seeded {len(rows)} records from JSON")

    cur.close()
    db.close()

# ── Run init at module load (works with gunicorn) ─────────────────────────────
init_db()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    stats = query_db('''
        SELECT
            COUNT(*) as total_transactions,
            SUM(sell) as total_sell,
            SUM(net) as total_net,
            SUM(profit) as total_profit
        FROM sales
    ''', one=True)
    total_paid = query_db(
        'SELECT COALESCE(SUM(amount),0) as paid FROM payments', one=True
    )['paid']
    balance = (stats['total_sell'] or 0) - total_paid

    monthly = query_db('''
        SELECT
            to_char(to_date(sale_date, 'YYYY-MM-DD'), 'MM') as month,
            SUM(sell) as total_sell,
            SUM(profit) as total_profit,
            COUNT(*) as count
        FROM sales
        WHERE to_char(to_date(sale_date, 'YYYY-MM-DD'), 'YYYY') = to_char(NOW(), 'YYYY')
        GROUP BY month
        ORDER BY month
    ''')

    top_companies = query_db('''
        SELECT company, SUM(sell) as total, COUNT(*) as cnt
        FROM sales
        GROUP BY company
        ORDER BY total DESC
        LIMIT 10
    ''')

    tomorrow_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tomorrow = query_db('''
        SELECT company, customer, from_loc, to_loc, travel_date, tickets, status
        FROM sales
        WHERE travel_date = %s
        ORDER BY company
    ''', [tomorrow_date])

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
        return redirect(url_for('sales_report'))
    return render_template('add.html', companies=companies, today=str(date.today()))

@app.route('/edit/<int:sale_id>', methods=['GET', 'POST'])
def edit_sale(sale_id):
    sale = query_db('SELECT * FROM sales WHERE id=%s', [sale_id], one=True)
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
        return redirect(url_for('sales_report'))
    return render_template('add.html', sale=sale, companies=companies, edit=True)

@app.route('/delete/<int:sale_id>', methods=['POST'])
def delete_sale(sale_id):
    execute_db('DELETE FROM sales WHERE id=%s', [sale_id])
    return redirect(url_for('sales_report'))

@app.route('/report')
def sales_report():
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    query  = 'SELECT * FROM sales WHERE 1=1'
    params = []
    if company:
        query += ' AND company=%s'; params.append(company)
    if status:
        query += ' AND status=%s'; params.append(status)
    if date_from:
        query += ' AND sale_date>=%s'; params.append(date_from)
    if date_to:
        query += ' AND sale_date<=%s'; params.append(date_to)
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
        query  = 'SELECT * FROM sales WHERE company=%s'
        params = [company]
        if date_from: query += ' AND sale_date>=%s'; params.append(date_from)
        if date_to:   query += ' AND sale_date<=%s'; params.append(date_to)
        query += ' ORDER BY sale_date ASC'
        sales = query_db(query, params)

        pay_query  = 'SELECT * FROM payments WHERE company=%s'
        pay_params = [company]
        if date_from: pay_query += ' AND pay_date>=%s'; pay_params.append(date_from)
        if date_to:   pay_query += ' AND pay_date<=%s'; pay_params.append(date_to)
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
            VALUES (%s,%s,%s,%s)
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
    tomorrow_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    tickets = query_db('''
        SELECT * FROM sales
        WHERE travel_date = %s
        ORDER BY company, customer
    ''', [tomorrow_date])
    tomorrow_str = (date.today() + timedelta(days=1)).strftime('%d %B %Y')
    return render_template('deliver.html', tickets=tickets, tomorrow=tomorrow_str)

# ── Admin / Data Viewer ───────────────────────────────────────────────────────

@app.route('/admin')
def admin():
    """Admin panel — view all raw data in both tables."""
    # Sales data with optional filters
    company   = request.args.get('company', '')
    status    = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    table     = request.args.get('table', 'sales')  # 'sales' or 'payments'

    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]

    sales_data = []
    payments_data = []

    if table == 'payments':
        pq = 'SELECT * FROM payments WHERE 1=1'
        pp = []
        if company:  pq += ' AND company=%s'; pp.append(company)
        if date_from: pq += ' AND pay_date>=%s'; pp.append(date_from)
        if date_to:   pq += ' AND pay_date<=%s'; pp.append(date_to)
        pq += ' ORDER BY pay_date DESC, id DESC'
        payments_data = query_db(pq, pp)
        total_payments = sum(r['amount'] for r in payments_data)
    else:
        sq = 'SELECT * FROM sales WHERE 1=1'
        sp = []
        if company:   sq += ' AND company=%s'; sp.append(company)
        if status:    sq += ' AND status=%s'; sp.append(status)
        if date_from: sq += ' AND sale_date>=%s'; sp.append(date_from)
        if date_to:   sq += ' AND sale_date<=%s'; sp.append(date_to)
        sq += ' ORDER BY sale_date DESC, id DESC'
        sales_data = query_db(sq, sp)
        total_payments = 0

    # DB summary stats
    db_stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM sales) as sales_count,
            (SELECT COUNT(*) FROM payments) as payments_count,
            (SELECT COALESCE(SUM(sell),0) FROM sales) as total_sell,
            (SELECT COALESCE(SUM(profit),0) FROM sales) as total_profit,
            (SELECT COALESCE(SUM(amount),0) FROM payments) as total_paid
    ''', one=True)

    return render_template('admin.html',
        sales=sales_data,
        payments=payments_data,
        companies=companies,
        db_stats=db_stats,
        table=table,
        filters={'company':company,'status':status,'date_from':date_from,'date_to':date_to},
        total_payments=total_payments if table=='payments' else 0,
        today=date.today().strftime('%d %B %Y')
    )

@app.route('/admin/delete-payment/<int:pay_id>', methods=['POST'])
def delete_payment(pay_id):
    execute_db('DELETE FROM payments WHERE id=%s', [pay_id])
    return redirect(url_for('admin', table='payments'))

@app.route('/api/companies')
def api_companies():
    companies = [r['company'] for r in query_db(
        'SELECT DISTINCT company FROM sales ORDER BY company'
    )]
    return jsonify(companies)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

@app.route('/export/excel')
def export_excel():
    """Export all sales and payments to Excel with 2 sheets."""
    wb = openpyxl.Workbook()

    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill("solid", fgColor="1B3A6B")
    gold_fill    = PatternFill("solid", fgColor="C8A84B")
    center       = Alignment(horizontal="center")
    currency_fmt = '#,##0.00'

    # ── Sheet 1: Sales ──────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Sales"
    sales_headers = ["ID","Sale Date","Company","Customer","From","To","Via",
                     "Trip Type","Buy From","Tickets","Travel Date",
                     "Net (USD)","Sell (USD)","Profit (USD)","Status","Remarks"]
    ws1.append(sales_headers)
    for col in range(1, len(sales_headers)+1):
        c = ws1.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    sales = query_db('SELECT * FROM sales ORDER BY sale_date DESC, id DESC')
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

    # ── Sheet 2: Payments ───────────────────────────────────────────────────
    ws2 = wb.create_sheet("Payments")
    pay_headers = ["ID","Pay Date","Company","Amount (USD)","Notes"]
    ws2.append(pay_headers)
    for col in range(1, len(pay_headers)+1):
        c = ws2.cell(row=1, column=col)
        c.font = header_font; c.fill = header_fill; c.alignment = center

    payments = query_db('SELECT * FROM payments ORDER BY pay_date DESC')
    for p in payments:
        ws2.append([p['id'],p['pay_date'],p['company'],p['amount'],p['notes']])
    for row in ws2.iter_rows(min_row=2, min_col=4, max_col=4):
        for cell in row: cell.number_format = currency_fmt

    tr2 = ws2.max_row + 1
    ws2.cell(row=tr2, column=3, value="TOTAL").font = Font(bold=True)
    c = ws2.cell(row=tr2, column=4, value=sum(p['amount'] for p in payments))
    c.font = Font(bold=True); c.fill = gold_fill; c.number_format = currency_fmt

    for i, w in enumerate([6,12,22,14,30], 1):
        ws2.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # ── Send ────────────────────────────────────────────────────────────────
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"alsondos_{date.today().strftime('%Y%m%d')}.xlsx"
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
