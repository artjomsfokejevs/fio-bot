"""Tests for POST /api/cashflow/weekly/import-file (2026-06-30).

Private-sheet fallback: operator downloads their Google Sheet as
CSV/TSV/XLSX and uploads. Backend converts XLSX → TSV and hands
off to the same import_tsv pipeline as the paste flow.
"""
from __future__ import annotations

import io

import pytest

from services import db, roles as roles_svc, cashflow_weekly as cw


@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    flask_app.app.testing = True
    flask_app.db.init_db()
    # Test client bypass auth — pretend caller is admin
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_ADMIN)
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM cashflow_weekly WHERE source LIKE 'import%'")
        conn.commit()
    finally:
        conn.close()


_HDR = {"X-FIO-User": "pytest"}


def test_file_import_accepts_tsv(client):
    tsv = ("Period\tEnd Date\tType\tB2C revenue plan\n"
            "W40\t10/5/2026\tForecast\t€40,000\n").encode("utf-8")
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (io.BytesIO(tsv), "planning.tsv"),
              "default_row_type": "forecast",
              "dry_run": "1"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows_imported"] == 1
    assert body["filename"] == "planning.tsv"
    assert body["dry_run"] is True


def test_file_import_skips_decorative_preamble_rows(client):
    """2026-07-01 — the operator's Amitours Cash Timeline CSV export has
    5 rows of decorative KPI preamble before the real header row (row 6).
    Importer must auto-detect the header instead of erroring out with
    'missing required header(s)' when the first row is empty."""
    csv = (
        "AMITOURS - UNIFIED CASH TIMELINE,,,Actuals from TxData,,,\n"
        ",,,AP,,AR,Overdue\n"
        ",,,b2c_liability,,ar_eur,overdue_eur\n"
        ",,,€388805,,€130764,€20641\n"
        ",,,,,,\n"
        "Period,End Date,Type,B2C revenue plan\n"
        "W40,10/12/2026,Forecast,\"€41,500\"\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (io.BytesIO(csv), "cash_timeline.csv"),
              "default_row_type": "forecast",
              "dry_run": "1"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["rows_imported"] == 1


def test_file_import_accepts_csv(client):
    csv = ("Period,End Date,Type,B2C revenue plan\n"
            "W41,10/12/2026,Forecast,\"€41,500\"\n").encode("utf-8")
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (io.BytesIO(csv), "planning.csv"),
              "default_row_type": "forecast",
              "dry_run": "1"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows_imported"] == 1


def test_file_import_accepts_xlsx(client):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Period", "End Date", "Type", "B2C revenue plan"])
    ws.append(["W42", "10/19/2026", "Forecast", 42000])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (buf, "planning.xlsx"),
              "default_row_type": "forecast",
              "dry_run": "1"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows_imported"] == 1


def test_file_import_rejects_unsupported_type(client):
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (io.BytesIO(b"junk"), "notes.pdf"),
              "default_row_type": "forecast"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "unsupported file type" in resp.get_json()["error"]


def test_file_import_rejects_missing_file(client):
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"default_row_type": "forecast"},
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "file is required" in resp.get_json()["error"]


def test_file_import_rejects_bad_row_type(client):
    resp = client.post(
        "/api/cashflow/weekly/import-file",
        data={"file": (io.BytesIO(b"x\ty\n"), "x.tsv"),
              "default_row_type": "actual"},   # derived, refused
        headers=_HDR,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "default_row_type must be one of" in resp.get_json()["error"]


def test_serve_docs_returns_markdown():
    """Docs endpoint used by the modal to link to the private-import guide."""
    import app as flask_app
    with flask_app.app.test_client() as c:
        resp = c.get("/docs/cashflow-private-import")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers.get("Content-Type", "")
    body = resp.get_data(as_text=True)
    assert "Cashflow: importing from a PRIVATE Google Sheet" in body


def test_serve_docs_404_for_missing_file():
    import app as flask_app
    with flask_app.app.test_client() as c:
        resp = c.get("/docs/definitely-not-a-real-doc")
    assert resp.status_code == 404


def test_serve_docs_rejects_path_traversal():
    import app as flask_app
    with flask_app.app.test_client() as c:
        resp = c.get("/docs/..%2Fapp")
    # Flask will URL-decode → '/docs/../app' which won't match the route
    # pattern (contains slash). Either way, no leak.
    assert resp.status_code in (400, 404)
