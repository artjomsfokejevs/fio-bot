"""Regression test for FB-B — Revolut Personal CSV parser.

Real-world team feedback: uploaded a Revolut Personal export and saw
"⚠ 0 rows imported — 42 skipped · Date column wasn't recognised in this
CSV. Format detected: Generic CSV." This test pins the format spec so
that headers from the team's actual export keep matching.
"""
from __future__ import annotations

import pytest

from services import db
from services.card_audit import detect_format, import_csv


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute(
            "DELETE FROM card_transactions WHERE imported_by = 'pytest_smoke'"
        )
        conn.commit()
    finally:
        conn.close()
    yield


# Headers verbatim from the team's failed upload (per docx feedback)
_TEAM_HEADERS = [
    "Completed date", "Time completed", "Status", "Transaction type",
    "Counterparty name", "Counterparty BIC", "Counterparty IBAN",
    "Amount", "Currency", "Reference",
]

_TEAM_CSV = (
    ",".join(_TEAM_HEADERS) + "\n"
    "2026-05-15,09:23:45,COMPLETED,TRANSFER,John Smith,REVOLT21XXX,"
    "GB29NWBK60161331926819,-45.50,EUR,Salary\n"
    "2026-05-16,12:01:25,COMPLETED,CARD_PAYMENT,Amazon.de,DEUTDEFFXXX,"
    "DE89370400440532013000,-129.99,EUR,Order #12345\n"
    "2026-05-17,10:00:05,COMPLETED,TOPUP,Acme Ltd,CITIIE2X,"
    "IE29AIBK93115212345678,2000.00,EUR,Invoice 2025-001\n"
    "2026-05-18,14:45:12,COMPLETED,CARD_PAYMENT,Pure Peaks,REVOLT21XXX,"
    "LT983250021131058300,-89.00,EUR,Conference\n"
).encode("utf-8")


def test_detect_format_picks_revolut_personal_for_team_headers():
    spec = detect_format(_TEAM_HEADERS)
    assert spec["id"] == "revolut_personal", (
        f"Expected revolut_personal, got {spec['id']}. "
        "Format detection regressed — check _FORMAT_SPECS in services/card_audit.py."
    )


def test_import_team_csv_parses_all_rows():
    result = import_csv(_TEAM_CSV, "revolut_team_smoke.csv",
                        imported_by="pytest_smoke")
    assert result["source"] == "revolut_personal"
    assert result["inserted"] == 4, (
        f"Expected 4 rows ingested, got {result['inserted']}. "
        f"Diagnosis: {result['diagnosis']}. Skip reasons: {result['skip_reasons']}"
    )
    assert result["skipped"] == 0
    assert result["diagnosis"] is None
    assert not result["errors"]


def test_import_team_csv_extracts_correct_amounts():
    result = import_csv(_TEAM_CSV, "revolut_team_smoke.csv",
                        imported_by="pytest_smoke")
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT amount, description, counterparty FROM card_transactions "
            "WHERE batch_id = ? ORDER BY posted_at",
            (result["batch_id"],),
        ).fetchall()
    finally:
        conn.close()
    amounts = sorted(r["amount"] for r in rows)
    assert amounts == [-129.99, -89.0, -45.5, 2000.0]
    # Counterparty names should land in the counterparty column
    names = {r["counterparty"] for r in rows}
    assert "John Smith" in names
    assert "Amazon.de" in names
    assert "Pure Peaks" in names
