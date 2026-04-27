# ALSONDOS TRAVEL — DEPLOYMENT GUIDE
# Deploy to Render.com (Free)
# ============================================================

## PROJECT STRUCTURE
alsondos/
├── app.py              ← Main Flask application
├── requirements.txt    ← Python dependencies
├── seed_data.json      ← Your 1,636 Excel records (auto-imported)
├── static/
│   └── style.css       ← All styling
└── templates/
    ├── base.html       ← Navigation & layout
    ├── index.html      ← Dashboard
    ├── add.html        ← Add / Edit sale
    ├── report.html     ← Sales report with filters
    ├── statement.html  ← Company statement (no profit shown)
    ├── payments.html   ← Record payments
    └── deliver.html    ← Deliver tomorrow

## STEP 1 — UPLOAD TO GITHUB
1. Go to github.com → Sign in → New repository
2. Name it: alsondos-travel
3. Set to: Public
4. Click "Create repository"
5. Upload ALL files keeping the same folder structure

## STEP 2 — DEPLOY ON RENDER
1. Go to render.com → Sign in (use GitHub account)
2. Click "New +" → "Web Service"
3. Connect your GitHub repository: alsondos-travel
4. Fill in settings:
   - Name:          alsondos-travel
   - Region:        Frankfurt (EU) — closest to Jordan
   - Branch:        main
   - Runtime:       Python 3
   - Build Command: pip install -r requirements.txt
   - Start Command: python app.py
5. Click "Create Web Service"
6. Wait 2-3 minutes → your app is live!

## STEP 3 — YOUR APP URL
Render gives you a free URL like:
https://alsondos-travel.onrender.com

Share this link with your 3 team members.
Everyone can use it from any device — phone, tablet, laptop.

## FEATURES
✅ Dashboard with KPIs and monthly analysis
✅ Add / Edit / Delete transactions
✅ Sales report with filters (company, date, status)
✅ Company Statement — professional, NO profit/net shown to client
✅ Payments tracking — balance updates automatically
✅ Deliver Tomorrow — shows tickets due tomorrow every day
✅ Print any page as PDF (File > Print > Save as PDF)
✅ All 1,636 existing records imported from your Excel

## IMPORTANT NOTES
- The database (SQLite) resets on Render free tier if inactive
- For permanent data: upgrade to Render paid plan ($7/month)
  OR use PostgreSQL (free on Render) — just ask and I will update the code
- For production with real data: use PostgreSQL

## ENVIRONMENT VARIABLES (Render)
No extra variables needed — app auto-detects PORT from Render.
