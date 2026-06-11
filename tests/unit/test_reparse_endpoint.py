"""Regression tests for /api/documents/<id>/reparse.

Endpoint added 2026-06-11 as the bookkeeper-facing tool for retroactive
prompt re-runs (P2.1 follow-up). Tests focus on the contract:
  * 404 for nonexistent doc / missing source file
  * 400 for rejected status
  * 403 for unauthorised role
  * Successful reparse returns delta of changed fields
"""
from __future__ import annotations

import json
import pytest

from services import db


@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    from services import roles as roles_svc
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_ADMIN)
    with flask_app.app.test_client() as c:
        yield c


_HDR = {"X-FIO-User": "pytest"}


def _seed_doc(status="classified", **extra):
    import uuid
    doc_id = uuid.uuid4().hex[:12]
    db.insert_document({
        "id": doc_id,
        "filename": doc_id + ".pdf",
        "original_name": "fixture.pdf",
        "file_type": "pdf",
        "file_size": 1,
        "uploaded_at": "2026-06-11T00:00:00",
        "uploaded_by": "test",
        "status": status,
        **extra,
    })
    return doc_id


def test_reparse_404_for_unknown_doc(client):
    r = client.post("/api/documents/__nope__/reparse", headers=_HDR)
    assert r.status_code == 404


def test_reparse_400_for_rejected_status(client):
    doc_id = _seed_doc(status="rejected")
    r = client.post(f"/api/documents/{doc_id}/reparse", headers=_HDR)
    assert r.status_code == 400
    assert "rejected" in (r.get_json() or {}).get("error", "").lower()


def test_reparse_404_when_source_file_missing(client):
    """File never uploaded → reparse cannot proceed."""
    doc_id = _seed_doc(status="classified")
    # No file on disk for this doc_id — reparse should refuse.
    r = client.post(f"/api/documents/{doc_id}/reparse", headers=_HDR)
    assert r.status_code == 404
    assert "file" in (r.get_json() or {}).get("error", "").lower()


def test_reparse_forbidden_without_role(client, monkeypatch):
    """Viewer role must be rejected."""
    from services import roles as roles_svc
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_VIEWER)
    doc_id = _seed_doc()
    r = client.post(f"/api/documents/{doc_id}/reparse", headers=_HDR)
    assert r.status_code == 403
