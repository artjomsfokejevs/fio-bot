"""Phase 2 «Money integrity» regression tests (2026-07-08).

  H1  — genuine duplicate transactions both survive import
  H2  — apply_match refuses double-apply
  H3  — date dialect inferred per import (dd/mm vs mm/dd)
  H4  — upsert_row only updates supplied keys
  H6  — budget actuals include split allocations + legacy aliases
  H10 — canonical status sets are the single source of truth
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from services import db


@pytest.fixture(autouse=True)
def _init():
    db.init_db()
    yield


# ── H10 ─────────────────────────────────────────────────────────────────
def test_canonical_status_sets_shared():
    from services import stream_budgets, intercompany, cashflow_weekly
    assert stream_budgets._COMMITTED_STATUSES is db.COMMITTED_STATUSES
    assert intercompany._PAID_STATUSES is db.POSTED_STATUSES
    assert cashflow_weekly._PAID_EXPENSE_STATUSES is db.POSTED_STATUSES
    # phantom statuses gone
    assert "archived" not in db.COMMITTED_STATUSES
    assert "payment_executed" not in db.COMMITTED_STATUSES


# ── H3 ──────────────────────────────────────────────────────────────────
def test_date_dialect_inference():
    from services.cashflow_weekly import _infer_dayfirst, _coerce_end_date_to_monday
    # A column containing "25/12/2025" (25 > 12) must be day-first.
    assert _infer_dayfirst(["4/5/2025", "25/12/2025"]) is True
    # A column containing "12/25/2025" (2nd comp > 12) must be month-first.
    assert _infer_dayfirst(["4/5/2025", "12/25/2025"]) is False
    # 4/5/2025 day-first → 4 May → Monday of that week is 2025-04-28
    assert _coerce_end_date_to_monday("4/5/2025", dayfirst=True) == "2025-04-28"
    # 4/5/2025 month-first → 5 April (Sat) → Monday 2025-03-31
    assert _coerce_end_date_to_monday("4/5/2025", dayfirst=False) == "2025-03-31"


# ── H4 ──────────────────────────────────────────────────────────────────
def test_upsert_row_preserves_unsupplied_fields():
    from services import cashflow_weekly as cw
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM cashflow_weekly WHERE source = 'test:h4'")
        conn.commit()
    finally:
        conn.close()
    # Seed with two numeric fields.
    cw.upsert_row(week_start="2026-07-06", row_type="forecast",
                  fields={"b2c_revenue_plan": 100.0, "a2a_burn_plan": -40.0},
                  source="test:h4", by="pytest")
    # Update ONLY one — the other must survive.
    cw.upsert_row(week_start="2026-07-06", row_type="forecast",
                  fields={"b2c_revenue_plan": 200.0},
                  source="test:h4", by="pytest")
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT b2c_revenue_plan, a2a_burn_plan FROM cashflow_weekly "
            "WHERE week_start='2026-07-06' AND row_type='forecast'").fetchone()
    finally:
        conn.execute("DELETE FROM cashflow_weekly WHERE source = 'test:h4'")
        conn.commit()
        conn.close()
    assert row["b2c_revenue_plan"] == 200.0
    assert row["a2a_burn_plan"] == -40.0   # NOT nulled


# ── H1 ──────────────────────────────────────────────────────────────────
def test_make_id_distinguishes_genuine_duplicates():
    from services.card_audit import _make_id
    base = {"posted_at": "2026-07-01", "amount": -4.50, "description": "STARBUCKS RIGA"}
    id1 = _make_id("revolut", "b1", {**base, "row_seq": 2})
    id2 = _make_id("revolut", "b1", {**base, "row_seq": 3})
    assert id1 != id2   # two identical charges, different file rows → distinct ids
    # Re-import same file (same row_seq) → same id → idempotent
    id1b = _make_id("revolut", "b_other", {**base, "row_seq": 2})
    assert id1 == id1b


# ── H2 ──────────────────────────────────────────────────────────────────
def test_apply_match_refuses_double_apply():
    from services import revenue_bank_match as rbm
    from services import revenue as rev
    conn = db.get_connection()
    now = datetime.utcnow().isoformat()
    try:
        conn.execute("DELETE FROM card_transactions WHERE id = 'test_h2_tx'")
        conn.execute("DELETE FROM revenue_documents WHERE id = 'test_h2_doc'")
        conn.execute(
            "INSERT INTO card_transactions (id, source, batch_id, imported_at, "
            " posted_at, period, amount, currency, amount_eur, match_status) "
            "VALUES ('test_h2_tx','mercury','b',?, '2026-07-01','2026-07',500,'EUR',500,'unmatched')",
            (now,))
        conn.execute(
            "INSERT INTO revenue_documents (id, kind, status, amount, currency, created_at) "
            "VALUES ('test_h2_doc','invoice','sent',500,'EUR',?)", (now,))
        conn.commit()
    finally:
        conn.close()
    # First apply succeeds.
    rbm.apply_match("test_h2_tx", "test_h2_doc", by="pytest")
    # Second apply must raise (already matched) — no phantom second receipt.
    with pytest.raises(ValueError):
        rbm.apply_match("test_h2_tx", "test_h2_doc", by="pytest")
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM revenue_receipts "
                         "WHERE bank_statement_tx_id = 'test_h2_tx'").fetchone()["c"]
    finally:
        conn.execute("DELETE FROM card_transactions WHERE id = 'test_h2_tx'")
        conn.execute("DELETE FROM revenue_documents WHERE id = 'test_h2_doc'")
        conn.execute("DELETE FROM revenue_receipts WHERE bank_statement_tx_id = 'test_h2_tx'")
        conn.commit()
        conn.close()
    assert n == 1   # exactly one receipt, not two


# ── H6 ──────────────────────────────────────────────────────────────────
def test_budget_actuals_include_split_allocations():
    from services import stream_budgets as sb
    conn = db.get_connection()
    now = datetime.utcnow().isoformat()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'test_h6_%'")
        # A split doc: primary PC = AA, allocates 60% to AA, 40% to SP.
        allocs = json.dumps([
            {"profit_center": "AA", "percentage": 60},
            {"profit_center": "SP", "percentage": 40},
        ])
        conn.execute(
            "INSERT INTO documents (id, filename, file_size, uploaded_at, status, "
            " amount, currency, profit_center, period, allocations_json) "
            "VALUES ('test_h6_split','x.pdf',0,?, 'paid', 1000.0,'EUR','AA','2026-07',?)",
            (now, allocs))
        conn.commit()
    finally:
        conn.close()
    try:
        # SP's actuals must include its 40% = 400 share.
        sp_actuals = sb.actuals_for("SP", "2026-07")
        assert sp_actuals == pytest.approx(400.0)
    finally:
        conn = db.get_connection()
        conn.execute("DELETE FROM documents WHERE id LIKE 'test_h6_%'")
        conn.commit()
        conn.close()
