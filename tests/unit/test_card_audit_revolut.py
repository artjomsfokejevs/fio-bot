"""Regression tests for Top-10 P1.1: Revolut Business CSV detection.

The 2026-06-11 Top-10 self-review caught that the Revolut signature was
based on imagined column names (`currency`, `started date`) instead of
the actual export (`Payment currency`, `Date started (UTC)`). One real
Revolut export now lives in the test as a fixture string so a future
detector refactor can't silently regress.
"""
from __future__ import annotations

from services import card_audit


REVOLUT_HEADER = (
    "Date started (UTC),Date completed (UTC),ID,Type,State,Description,"
    "Reference,Payer,Card number,Card label,Card state,Orig currency,"
    "Orig amount,Payment currency,Amount,Total amount,Exchange rate,Fee,"
    "Fee currency,Balance,Account,Beneficiary account number,"
    "Beneficiary sort code or routing number,Beneficiary IBAN,"
    "Beneficiary BIC,MCC,Related transaction id,Spend program,"
    "Card references"
)


def _headers_list() -> list:
    return [h.strip() for h in REVOLUT_HEADER.split(",")]


def test_revolut_detector_matches_real_export_header():
    """The actual Revolut Business CSV header must match the spec, not
    the docs/imagined version."""
    spec = card_audit.detect_format(_headers_list())
    assert spec["id"] == "revolut", (
        f"Detector picked '{spec['id']}' — Revolut signature drifted "
        f"from the real export. See retro 2026-06-11 Q93."
    )


def test_revolut_detector_does_not_fall_through_to_generic():
    """Safety net: if 'revolut' detection breaks, the row would fall to
    generic and lose column mapping. Pin the contract."""
    spec = card_audit.detect_format(_headers_list())
    assert spec["id"] != "generic"


def test_revolut_field_mappings_resolve_real_columns():
    """Each field in the spec must point to at least one column that
    actually exists in the real export (case-insensitive)."""
    spec = card_audit.detect_format(_headers_list())
    norm_headers = {h.strip().lower() for h in _headers_list()}
    for field_name, candidates in spec["fields"].items():
        if not isinstance(candidates, list):
            continue
        # At least one candidate must exist in the real headers
        assert any(c.lower() in norm_headers for c in candidates), (
            f"Field `{field_name}` maps to {candidates} — none of them "
            f"appear in the real Revolut export headers."
        )


# ────────────────────────────────────────────────────────────────────────
# 2026-06-30 — Finom Business CSV format (operator hit "0 rows imported"
# because the Revolut Personal detector required a bare 'amount' column
# but Finom uses 'Payment amount').
# ────────────────────────────────────────────────────────────────────────

def test_finom_csv_detected_and_imported():
    """Real Finom export headers + a couple of representative rows."""
    from services.card_audit import detect_format, import_statement
    headers = ["Completed date","Time completed","Status","Transaction type",
               "Counterparty name","Counterparty BIC","Counterparty IBAN",
               "Reference","Tags","Transaction payer","Card number",
               "Original currency","Original amount","Payment currency",
               "Payment amount","Wallet balance after transaction",
               "Wallet name","Wallet IBAN","Supporting documents","Transaction Id"]
    spec = detect_format(headers)
    assert spec["id"] == "finom", f"Expected finom, got {spec['id']}"

    csv_text = (
        "Completed date,Time completed,Status,Transaction type,Counterparty name,"
        "Counterparty BIC,Counterparty IBAN,Reference,Tags,Transaction payer,"
        "Card number,Original currency,Original amount,Payment currency,Payment amount,"
        "Wallet balance after transaction,Wallet name,Wallet IBAN,"
        "Supporting documents,Transaction Id\n"
        "27.06.2026,09:53,Completed,Card,FACEBK *5MDQ5VMQG2,,,N/A,SERVICES,Holder,"
        "***0265,EUR,-298.68,EUR,-298.68,20509.17,Mountly (RSA),NL24FNOM0542742838,N/A,T1\n"
        "22.06.2026,10:08,Completed,International,Caisse AVS,POFICHBE,CH3730000001100070006,"
        "Ref 39,N/A,,,CHF,-3984.45,EUR,-4334.22,109.74,Main,NL70FNOM0542742883,N/A,T2\n"
    )
    res = import_statement(csv_text.encode("utf-8"), "Finom_pytest.csv",
                            imported_by="pytest_finom")
    assert res.get("total_rows") == 2
    assert res.get("inserted") == 2

    # Cleanup
    from services import db
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM card_transactions WHERE batch_id = ?",
                     (res["batch_id"],))
        conn.commit()
    finally:
        conn.close()
