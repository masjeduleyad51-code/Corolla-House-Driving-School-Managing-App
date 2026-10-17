# Corolla House – Driving School Manager

Python (Flask) + SQLite. Free to run, no external services needed.

## Run it
    python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    python app.py                                        # http://127.0.0.1:5000

First run prints the owner login (email `owner@corollahouse.local` + a random password).
Set your own with `ADMIN_EMAIL` / `ADMIN_PASSWORD` before the first run. Change it under **Users**.
Add instructors under **Users**; they only see their own lessons, hours, car checks and clients (no money, stats or users).

## What is inside
- **Bookings:** double-booking blocked **inside the database** (instructor, car and client triggers), off-road cars and approved leave blocked, 48-hour late-cancel rule (50%), waiting list with WhatsApp link, lesson requests from clients
- **Clients:** details, consent, skills checklist, notes, payments and balance, GDPR export and erase (anonymises, keeps accounts)
- **Client portal:** give a client a login on their page. They see upcoming and past lessons, progress and balance, can cancel (fee rule applies) and request lessons
- **Fleet:** MOT/insurance/tax/service alerts at 30 days, status, 10-second car check **with photo**, wash log, supplies with low-stock alerts, cost and profit per car
- **Team:** shift planner with Mon–Fri template, leave approval, monthly hours, utilisation, pay summary (hourly **or per-lesson commission**, set per instructor under Users)
- **Money:** expenses, payments, CSV for the accountant
- **Stats:** revenue vs expenses, utilisation, busiest days, **hours heatmap**, **expense pie**, profit by car, funnel, cancellation rate, suggestions, database backup
- **Installable app (PWA):** "Add to home screen" on a phone; icons and service worker included. Pages with personal data are never cached.

## Test
    python tests.py

## Go live for free
- **PythonAnywhere** (free tier, easiest): upload the folder, create a Flask web app pointing at `app.py`.
- **Render / Koyeb**: start command `gunicorn app:app`. Free disks can reset, so use a persistent disk or set `DB_PATH`, and download a backup from **Stats** weekly.
- Set `SECRET_KEY` and `HTTPS_ONLY=1` in production. Always serve over HTTPS.

## Online payments (optional, Stripe)
Off by default. To turn on: create a Stripe account, set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, and point a Stripe webhook for `checkout.session.completed` at `https://YOUR-SITE/stripe/webhook`. Clients then get a **Pay now** button for their balance, and paid amounts are recorded automatically (once per payment). Stripe charges a fee per payment. Test with Stripe test keys first: the webhook signature check is unit tested, but I could not test a live checkout.

## Photos
Car-check photos are stored in `uploads/` next to the database (`UPLOAD_DIR` to change). Only JPEG, PNG and WebP up to 8 MB are accepted, and only logged-in staff can view them. Back this folder up too.

## Email reminders (optional, free with Brevo)
Set `SMTP_HOST SMTP_USER SMTP_PASS MAIL_FROM`, then run daily from cron: `flask --app app reminders`
Without SMTP settings it does a dry run and prints who would be emailed.

## Not included yet
SMS (it costs per message; email and WhatsApp links cover reminders). Check whether you need ICO registration.
