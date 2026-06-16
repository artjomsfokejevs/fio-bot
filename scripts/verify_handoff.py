"""Full handoff verification — run via flyctl ssh.

Validates every deliverable from today's 7 functional commits is live.
"""
import json
import os
import os.path
import sqlite3
import inspect

PASS = "[OK]  "
FAIL = "[FAIL]"
results = []


def check(name, cond, detail=""):
    tag = PASS if cond else FAIL
    line = "  " + tag + "  " + name
    if detail:
        line += "  :: " + detail
    print(line)
    results.append((tag, name))


print()
print("=" * 70)
print("COMMIT 5e61fcc - Phase 1: Policies tab + Approved-by-Accounting + Alveda")
print("=" * 70)

from routes import policy as policy_routes
check("policy_bp blueprint", hasattr(policy_routes, "policy_bp"))

conn = sqlite3.connect("/app/data/fio.db")
n = conn.execute("SELECT COUNT(*) FROM policy_rules WHERE active=1").fetchone()[0]
check("policy_rules seeded (>=4)", n >= 4, str(n) + " active rules")

for tbl in ("policy_violation_approvals", "policy_rules_history"):
    try:
        conn.execute("SELECT COUNT(*) FROM " + tbl).fetchone()
        check(tbl + " table", True)
    except Exception as e:
        check(tbl + " table", False, str(e))

from services import classifier, policy_rules
effective = policy_rules.get_effective_policies()
check("classifier reads DB via get_effective_policies", "office_supplies" in effective)

from services import roles
check("admin sees 'policies' tab", "policies" in roles.TAB_ACCESS["admin"])
check("bookkeeper sees 'policies' tab", "policies" in roles.TAB_ACCESS["bookkeeper"])
check("holding_ceo sees 'policies' tab", "policies" in roles.TAB_ACCESS["holding_ceo"])

print()
print("=" * 70)
print("COMMIT 700408b - Filters + Partial payments + M&G stubs")
print("=" * 70)

from services import db
docs = db.get_documents(q=".pdf", sort="amount_desc", date_from="2026-01-01")
check("db.get_documents accepts q/sort/date_from", isinstance(docs, list))

for tbl in ("partial_payments",):
    try:
        conn.execute("SELECT COUNT(*) FROM " + tbl).fetchone()
        check(tbl + " table", True)
    except Exception as e:
        check(tbl + " table", False, str(e))

for col in ("is_internal",):
    try:
        conn.execute("SELECT " + col + " FROM documents LIMIT 1").fetchone()
        check("documents." + col + " column", True)
    except Exception as e:
        check("documents." + col + " column", False, str(e))

from routes import payments as payments_routes
check("payments_bp blueprint", hasattr(payments_routes, "payments_bp"))

from routes import mng as mng_routes
check("mng_bp blueprint", hasattr(mng_routes, "mng_bp"))

print()
print("=" * 70)
print("COMMIT 86956ac - Phase 2: Slack + bell + bank archives + salaries + SMTP")
print("=" * 70)

try:
    conn.execute("SELECT COUNT(*) FROM notifications").fetchone()
    check("notifications table", True)
except Exception as e:
    check("notifications table", False, str(e))

from services import notifications
check("notifications.create callable", callable(notifications.create))

try:
    conn.execute("SELECT is_salary FROM documents LIMIT 1").fetchone()
    check("documents.is_salary column", True)
except Exception as e:
    check("documents.is_salary column", False, str(e))

from routes import notify as notify_routes
check("notify_bp blueprint", hasattr(notify_routes, "notify_bp"))

from services import slack_notify
check("slack_notify.send_urgent_payment callable", callable(slack_notify.send_urgent_payment))

from services import email_send
check("email_send.send callable", callable(email_send.send))

print()
print("=" * 70)
print("COMMIT b7e26b9 - Phase 3: Stream Budgets + X-alarm")
print("=" * 70)

for tbl in ("stream_budgets", "stream_budget_history", "xalarm_log"):
    try:
        conn.execute("SELECT COUNT(*) FROM " + tbl).fetchone()
        check(tbl + " table", True)
    except Exception as e:
        check(tbl + " table", False, str(e))

from routes import budgets as budgets_routes
check("budgets_bp blueprint", hasattr(budgets_routes, "budgets_bp"))

from services import xalarm
check("xalarm.fire_if_overrun callable", callable(xalarm.fire_if_overrun))

print()
print("=" * 70)
print("COMMIT f4133e1 - Slack dual-transport + secrets")
print("=" * 70)

check("slack transport detected", slack_notify.transport() in ("bot", "webhook", "none"))
check("slack transport = bot (live)", slack_notify.transport() == "bot")

for env_var in ("SLACK_BOT_TOKEN", "SLACK_CEO_CHANNEL",
                "SMTP_USER", "SMTP_PASS",
                "XALARM_CEO_EMAIL", "XALARM_OPS_EMAIL", "ASANA_PAT"):
    check(env_var + " set", bool(os.getenv(env_var)))

print()
print("=" * 70)
print("COMMIT 6119280 - BK decommission + M&G handoff doc")
print("=" * 70)

check("BK NOT in xalarm.PC_LABELS", "BK" not in xalarm.PC_LABELS)
check("AA still in PC_LABELS", "AA" in xalarm.PC_LABELS)

mng_doc = "/app/docs/meet-and-greet-dev-handoff.md"
exists = os.path.exists(mng_doc)
check("M&G handoff doc deployed", exists)
if exists:
    with open(mng_doc) as f:
        content = f.read()
    check("M&G doc has section 13 task list", "Your-side task list" in content)
    check("M&G doc has bilingual RU summary", "RU" in content)

print()
print("=" * 70)
print("COMMIT 183f3b0 - Tab visibility + What's New fixes")
print("=" * 70)

check("'policies' in roles.ALL_TABS", "policies" in roles.ALL_TABS)

with open("/app/data/whats_new.json") as f:
    wn = json.load(f)
top_versions = [e["version"] for e in wn.get("entries", [])[:4]]
expected = ["2026-06-16-phase3", "2026-06-16-phase2",
            "2026-06-16-phase1b", "2026-06-16-phase1"]
check("whats_new top entry = phase3",
      bool(top_versions) and top_versions[0] == expected[0])
missing = [v for v in expected if v not in top_versions]
check("whats_new contains all 4 phases", not missing,
      "missing: " + str(missing) if missing else "")

print()
print("=" * 70)
print("END-TO-END pipeline checks")
print("=" * 70)

import app
src = inspect.getsource(app.confirm_payment)
check("confirm_payment hooks xalarm.fire_if_overrun", "fire_if_overrun" in src)
check("confirm_payment hooks notifications", "ceo_approved_invoice" in src)

rows = conn.execute("SELECT role, profit_center, email FROM fio_users WHERE active=1").fetchall()
roles_seen = set(r[0] for r in rows)
pcs_seen = set(r[1] for r in rows if r[1])
check("fio_users has admin", "admin" in roles_seen)
check("fio_users has holding_ceo", "holding_ceo" in roles_seen)
check("fio_users has bookkeeper (Rita)", "bookkeeper" in roles_seen)
for pc in ("AA", "SR", "AH", "CF", "AL"):
    check("stream owner/admin for " + pc, pc in pcs_seen)
check("BK NOT in active fio_users", "BK" not in pcs_seen)

recips_aa = xalarm.recipients_for("AA")
check("xalarm.recipients_for(AA) >= 4 emails",
      len(recips_aa) >= 4,
      "got " + str(len(recips_aa)) + ": " + str(recips_aa))

recips_sr = xalarm.recipients_for("SR")
check("xalarm.recipients_for(SR) includes Serge",
      any("serge" in r.lower() for r in recips_sr),
      "got: " + str(recips_sr))

# Live Slack + SMTP connectivity (no-op pings already done earlier today)
check("Slack configured", slack_notify.is_configured())
check("SMTP configured", email_send.is_configured())

print()
print("=" * 70)
ok = sum(1 for tag, _ in results if tag == PASS)
fail = sum(1 for tag, _ in results if tag == FAIL)
print("FINAL: " + str(ok) + "/" + str(len(results)) + " passed" +
      ((" (" + str(fail) + " FAILED)") if fail else " - ALL GREEN"))
print("=" * 70)
if fail:
    print()
    print("FAILURES:")
    for tag, name in results:
        if tag == FAIL:
            print("  - " + name)
