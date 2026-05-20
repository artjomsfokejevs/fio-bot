# v3 Team Announcement — Card Audit

## EN

Hi team! FIO v3 is now live on Fly.io — we've added a month-close tool for corporate cards.
🔗 https://fio-amitours.fly.dev
👤 tester
🔑 xddhfnVnio5KS0oumeyn

**What's new:**
✓ New **Card Audit** tab — month-close workflow for corporate cards
✓ CSV import from banks: Mercury / Revolut Business / Stripe / Airwallex / generic (format auto-detected)
✓ Auto-reconcile: matches card transactions to approved invoices (amount ±0.01 EUR, date ±3 days, vendor similarity)
✓ Department breakdown via BT4YOU map (card_holder → department → profit_center)
✓ KPI dashboard: Total / Unmatched / Suggested / Matched / Manual / Excluded
✓ Period filter by month + bar chart by department
✓ Manual actions per transaction: assign card holder, link invoice manually, exclude
✓ CSV export with corporate header and profit_center breakdown — ready for accounting

**How to use:**
1. Close the month → export CSV from all corporate cards
2. Drag-and-drop into the **Card Audit** tab → click **Reconcile**
3. Check Unmatched — these are transactions without invoices
4. Suggested — the bot found a likely match, confirm in one click
5. Export CSV → hand over to accounting

## RU

Привет! FIO v3 теперь в проде на Fly.io — добавили инструмент закрытия месяца по корпоративным картам.
🔗 https://fio-amitours.fly.dev
👤 tester
🔑 xddhfnVnio5KS0oumeyn

**Что нового:**
✓ Новая закладка **Card Audit** — закрытие месяца по картам
✓ CSV-импорт с банков: Mercury / Revolut Business / Stripe / Airwallex / generic (формат определяется автоматически)
✓ Auto-reconcile, разбивка по департаментам, KPI-дэшборд, ручные действия
✓ Export CSV с корпоративной шапкой — готов для бухгалтерии
