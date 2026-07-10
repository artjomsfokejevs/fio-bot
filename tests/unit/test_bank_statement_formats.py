"""Regression tests for the multi-bank statement importer (2026-07-10).

The holding uploads statements from six providers: Narvi, Revolut Business,
Finom, PayPal, Mercury, Airwallex. Two of them (Narvi + PayPal) silently
imported 0 rows because no format spec existed — their money column is
"Transaction amount" / "Gross", never bare "amount", so every row was
dropped as `no_amount` and month-close stalled. These tests pin the real
export headers (as fixtures on disk) so a future refactor can't regress the
detection or the account→legal-entity attribution.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services import card_audit as ca
from services import db

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bank"


def _cleanup(batch_id: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM card_transactions WHERE batch_id = ?", (batch_id,))
        conn.commit()
    finally:
        conn.close()


# ── detection ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fname,expected", [
    ("narvi_business.csv", "narvi"),
    ("paypal_business.csv", "paypal"),
    ("revolut_business.csv", "revolut"),
    ("finom_business.csv", "finom"),
])
def test_detect_format_from_real_header(fname, expected):
    import csv, io
    text = (FIX / fname).read_bytes().decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    spec = ca.detect_format(reader.fieldnames or [])
    assert spec["id"] == expected, f"{fname} detected as {spec['id']}, want {expected}"


# ── full import (the actual breakage: Narvi/PayPal were 0 rows) ───────────
@pytest.mark.parametrize("fname,source,min_rows", [
    ("narvi_business.csv", "narvi", 8),
    ("paypal_business.csv", "paypal", 12),
    ("revolut_business.csv", "revolut", 5),
    ("finom_business.csv", "finom", 5),
])
def test_import_real_csv_parses_rows(fname, source, min_rows):
    res = ca.import_statement((FIX / fname).read_bytes(), fname, imported_by="pytest")
    try:
        assert res["source"] == source
        assert res["inserted"] >= min_rows, (
            f"{fname}: only {res['inserted']} rows imported "
            f"(diagnosis={res.get('diagnosis')})"
        )
        assert res["diagnosis"] is None
    finally:
        _cleanup(res["batch_id"])


# ── account → legal-entity attribution ────────────────────────────────────
def test_detect_account_narvi_holding():
    acct = ca.detect_account("holder IBAN FI6579600107718890 BIC NARYFIH2")
    assert acct and acct["legal_entity"] == "AMITOURS_HOLDING"


def test_detect_account_revolut_london():
    acct = ca.detect_account("Account GB51REVO00996977416879")
    assert acct and acct["legal_entity"] == "AMITOURS_LONDON"


def test_detect_account_airwallex_group_sa_sets_pc():
    acct = ca.detect_account("IBAN: NL13AINH7433463759")
    assert acct and acct["legal_entity"] == "AMITOURS_GROUP_SA"
    assert acct["profit_center"] == "AG"   # single-stream → PC assignable


def test_detect_account_iban_with_spaces_still_matches():
    # IBANs are often printed grouped ("GB51 REVO 0099 ...") — must still match.
    acct = ca.detect_account("Account GB51 REVO 0099 6977 4168 79")
    assert acct and acct["legal_entity"] == "AMITOURS_LONDON"


def test_detect_account_unknown_returns_none():
    assert ca.detect_account("no known token here") is None


def test_narvi_import_stamps_legal_entity_on_rows():
    res = ca.import_statement((FIX / "narvi_business.csv").read_bytes(),
                              "narvi_business.csv", imported_by="pytest")
    try:
        assert res["legal_entity"] == "AMITOURS_HOLDING"
        conn = db.get_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM card_transactions "
                "WHERE batch_id=? AND legal_entity='AMITOURS_HOLDING'",
                (res["batch_id"],)).fetchone()["c"]
        finally:
            conn.close()
        assert n == res["inserted"]
    finally:
        _cleanup(res["batch_id"])


# ── date robustness ───────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,iso", [
    ("2026-06-30", "2026-06-30"),
    ("2026-06-27T09:53:00Z", "2026-06-27"),
    ("29.06.2026", "2026-06-29"),
    ("01/05/2026", "2026-05-01"),
    ("30 Jun 2026", "2026-06-30"),
    ("30 Jun 2026 EEST", "2026-06-30"),
    ("Jun 16 2025", "2025-06-16"),
    ("30-Jun-2026", "2026-06-30"),
    ("27/06/26", "2026-06-27"),
])
def test_parse_date_formats(raw, iso):
    assert ca._parse_date(raw) == iso


@pytest.mark.parametrize("raw", ["", "N/A", "13/25/2026", "not a date"])
def test_parse_date_rejects_garbage(raw):
    assert ca._parse_date(raw) is None


# ── Narvi sign-aware counterparty (inflow=sender, outflow=recipient) ──────
def test_narvi_credit_row_counterparty_is_sender():
    """The first Narvi row is a +13,700 credit FROM Alps2Alps OU. The
    counterparty must be the sender (Alps2Alps), not our own holder name."""
    res = ca.import_statement((FIX / "narvi_business.csv").read_bytes(),
                              "narvi_business.csv", imported_by="pytest")
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT counterparty FROM card_transactions "
                "WHERE batch_id=? AND amount > 0 ORDER BY posted_at DESC LIMIT 1",
                (res["batch_id"],)).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "alps2alps" in (row["counterparty"] or "").lower()
    finally:
        _cleanup(res["batch_id"])
