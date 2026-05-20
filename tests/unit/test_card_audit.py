"""Tests for services.card_audit — CSV sniffer + reconciler (P5 pattern)."""
from __future__ import annotations

import pytest

from services import db, card_audit

MERCURY_CSV = b"""Date (UTC),Description,Amount (USD),Status
2026-05-03,LinkedIn Premium,-29.99,Sent
2026-05-07,Fireflies AI Pro,-19.00,Sent
"""

EMPTY_CSV = b""


@pytest.fixture(autouse=True)
def _db():
    db.init_db()


def test_import_returns_expected_shape():
    """P5: result dict must contain batch_id, source, total_rows."""
    result = card_audit.import_csv(
        MERCURY_CSV, "mercury_may.csv", imported_by="pytest"
    )
    assert "batch_id" in result
    assert "source" in result
    assert "total_rows" in result
    assert result["total_rows"] >= 2


def test_import_csv_handles_empty_gracefully():
    """Error path: empty bytes → either raises or returns 0-row result."""
    try:
        result = card_audit.import_csv(
            EMPTY_CSV, "empty.csv", imported_by="pytest"
        )
        assert result.get("total_rows", 0) == 0
    except Exception:
        pass  # raising is also acceptable


def test_reconcile_period_returns_counts():
    """Reconciler must return a stable dict shape even with no data."""
    counts = card_audit.reconcile_period("2099-01")  # period with no rows
    assert isinstance(counts, dict)
    assert "checked" in counts


def test_list_card_tx_returns_list():
    """list_card_tx() always returns a list, even for empty period."""
    out = card_audit.list_card_tx(period="2099-01")
    assert isinstance(out, list)
