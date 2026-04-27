import os
import json
import psycopg2
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, jsonify, g

app = Flask(__name__)

# ── Database ─────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = psycopg2.connect(os.environ.get("DATABASE_URL"))
    return g.db

@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('db', None)
    if db:
        db.close()

def query_db(query, args=(), one=False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, args)
    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    return (rows[0] if rows else None) if one else rows

def execute_db(query, args=()):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, args)
    conn.commit()
    cur.close()

# ── Init DB ─────────────────────────────
def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY,
        from_loc TEXT,
        to_loc TEXT,
        via TEXT,
        trip_type TEXT,
        buy_from TEXT,
        company TEXT,
        tickets INTEGER,
        customer TEXT,
        sale_date DATE,
        travel_date DATE,
        net REAL,
        sell REAL,
        profit REAL,
        status TEXT,
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        company TEXT,
        amount REAL,
        pay_date DATE,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    cur.close()

# ── Routes ─────────────────────────────

@app.route('/')
def index():
    stats = query_db("""
        SELECT COUNT(*) as total_transactions,
               SUM(sell) as total_sell,
               SUM(net) as total_net,
               SUM(profit) as total_profit
        FROM sales
    """, one=True)

    total_paid = query_db("SELECT COALESCE(SUM(amount),0) as paid FROM payments", one=True)['paid']
    balance = (stats['total_sell'] or 0) - total_paid

    return render_template("index.html", stats=stats, total_paid=total_paid, balance=balance)

@app.route('/add', methods=['POST'])
def add_sale():
    net = float(request.form.get('net', 0))
    sell = float(request.form.get('sell', 0))

    execute_db("""
        INSERT INTO sales (from_loc,to_loc,company,customer,sale_date,net,sell,profit)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        request.form.get('from_loc'),
        request.form.get('to_loc'),
        request.form.get('company'),
        request.form.get('customer'),
        request.form.get('sale_date'),
        net, sell, sell - net
    ))

    return redirect('/report')

@app.route('/report')
def report():
    sales = query_db("SELECT * FROM sales ORDER BY id DESC")
    return render_template("report.html", sales=sales)

@app.route('/payments', methods=['GET','POST'])
def payments():
    if request.method == 'POST':
        execute_db("""
            INSERT INTO payments (company, amount, pay_date)
            VALUES (%s,%s,%s)
        """, (
            request.form.get('company'),
            float(request.form.get('amount')),
            request.form.get('pay_date')
        ))

    data = query_db("SELECT * FROM payments ORDER BY id DESC")
    return render_template("payments.html", payments=data)

# ── Run ─────────────────────────────
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
