"""Smoke tests for MCP-1, MCP-3, MCP-5 modules — 2026-06-26."""
from __future__ import annotations

import pytest

from services import db, travel, erp_export


def _purge():
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'tr_%' OR id LIKE 'pytest_mcp_%'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    _purge()
    yield
    _purge()  # also clean AFTER so leak doesn't poison test_intercompany


# ── MCP-3 travel ─────────────────────────────────────────────────────────

def test_default_per_diem_known_country():
    assert travel.default_per_diem_for("EE") == 50.0
    assert travel.default_per_diem_for("LV") == 6.0
    # Unknown country falls back to 60
    assert travel.default_per_diem_for("XX") == 60.0


def test_create_travel_report_calculates_total():
    r = travel.create_travel_report({
        "destination_country": "EE",
        "destination_city": "Tallinn",
        "departure_date": "2026-07-01",
        "return_date": "2026-07-04",
        "purpose": "Q3 review",
        "profit_center": "AA",
    }, by="pytest")
    assert r["days"] == 4
    assert r["per_diem_eur"] == 50.0
    assert r["total_eur"] == 200.0


def test_create_travel_report_rejects_bad_dates():
    with pytest.raises(ValueError):
        travel.create_travel_report({
            "destination_country": "EE",
            "departure_date": "2026-07-10",
            "return_date": "2026-07-01",  # before departure
            "profit_center": "AA",
        }, by="pytest")


def test_update_travel_report_recalculates():
    r = travel.create_travel_report({
        "destination_country": "DE",
        "departure_date": "2026-07-01",
        "return_date": "2026-07-02",
        "profit_center": "AA",
    }, by="pytest")
    upd = travel.update_travel_report(r["id"], {"per_diem_eur": 100.0}, by="pytest")
    assert upd["per_diem_eur"] == 100.0
    assert upd["total_eur"] == 200.0  # 2 days × 100


# ── MCP-5 erp_export ─────────────────────────────────────────────────────

def _seed(doc_id, amount, period, pc="AA", status="approved",
          ledger="OP00", vendor="Test Vendor"):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, file_size, uploaded_at, status, amount, "
            "currency, profit_center, ledger_code, period, vendor) "
            "VALUES (?, 'x.pdf', 0, '2026-06-20T10:00:00', ?, ?, 'EUR', ?, ?, ?, ?)",
            (doc_id, status, amount, pc, ledger, period, vendor),
        )
        conn.commit()
    finally:
        conn.close()


def test_select_documents_filters_by_period_and_pc():
    _seed("pytest_mcp_1", 100, "2026-06", pc="AA")
    _seed("pytest_mcp_2", 200, "2026-06", pc="SP")
    _seed("pytest_mcp_3", 300, "2026-05", pc="AA")
    rows = erp_export.select_documents("2026-06")
    ids = [r["id"] for r in rows]
    assert "pytest_mcp_1" in ids and "pytest_mcp_2" in ids
    assert "pytest_mcp_3" not in ids
    rows_aa = erp_export.select_documents("2026-06", pc="AA")
    assert [r["id"] for r in rows_aa] == ["pytest_mcp_1"]


def test_build_export_standard_books_format():
    _seed("pytest_mcp_4", 123.45, "2026-06")
    name, data, count = erp_export.build_export("standard_books", "2026-06")
    text = data.decode("utf-8")
    assert name.endswith(".csv") and count == 1
    assert "Kuupäev;Number;Hankija" in text.splitlines()[0]
    assert "pytest_mcp_4" in text
    assert "123.45" in text


def test_build_export_jumis_pro_format_has_bom():
    _seed("pytest_mcp_5", 55.0, "2026-06")
    name, data, count = erp_export.build_export("jumis_pro", "2026-06")
    # BOM check
    assert data.startswith("﻿".encode("utf-8"))
    text = data.decode("utf-8-sig")
    assert "Datums,DokumentaNumurs" in text.splitlines()[0]
    assert "pytest_mcp_5" in text


def test_build_export_raises_when_empty():
    with pytest.raises(ValueError):
        erp_export.build_export("generic_csv", "1999-01")


def test_build_export_rejects_unknown_format():
    _seed("pytest_mcp_6", 1.0, "2026-06")
    with pytest.raises(ValueError):
        erp_export.build_export("xero", "2026-06")
