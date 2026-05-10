"""Document parsing service using Claude Vision, pypdf, and tabular parsers."""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

__all__ = ["parse_document"]


def parse_document(file_path: str, file_type: str) -> Dict[str, Any]:
    """Parse a financial document and extract structured data.

    Args:
        file_path: Absolute path to the uploaded file.
        file_type: File extension (pdf, jpg, png, csv, xlsx).

    Returns:
        Extracted data dictionary with keys: document_type, vendor, dates,
        money, line_items, ledger_codes, warnings.
    """
    logger.info("Parsing %s (type=%s)", file_path, file_type)

    if file_type in ("csv",):
        return _parse_csv(file_path)
    if file_type in ("xlsx",):
        return _parse_xlsx(file_path)
    if file_type in ("pdf",):
        return _parse_pdf(file_path)
    if file_type in ("jpg", "jpeg", "png"):
        return _parse_image(file_path)

    return _mock_response(file_path, "Unsupported file type: %s" % file_type)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _parse_pdf(file_path: str) -> Dict[str, Any]:
    """Parse a PDF -- try text extraction first, fall back to Vision."""
    text = _extract_pdf_text(file_path)
    if text and len(text.strip()) > 50:
        result = _parse_text_with_llm(text, os.path.basename(file_path))
        # Enrich vendor with regex-extracted details (VAT, Reg.Nr, IBAN, address)
        # This works even if LLM failed and returned mock data
        result = _enrich_with_regex_details(result, text)
        return result
    # Fall back to Vision (treat each page as an image)
    return _parse_image(file_path)


# ---------------------------------------------------------------------------
# Regex-based VAT / Reg.Nr / IBAN / Address extraction
# Works without LLM, enriches LLM output, salvages failed LLM calls.
# ---------------------------------------------------------------------------

# EU VAT formats per https://en.wikipedia.org/wiki/VAT_identification_number
_VAT_PATTERNS = [
    (r"\b(LV)\s*[-]?\s*(\d{11})\b", "LV"),
    (r"\b(EE)\s*[-]?\s*(\d{9})\b", "EE"),
    (r"\b(LT)\s*[-]?\s*(\d{9}|\d{12})\b", "LT"),
    (r"\b(PL)\s*[-]?\s*(\d{10})\b", "PL"),
    (r"\b(DE)\s*[-]?\s*(\d{9})\b", "DE"),
    (r"\b(FR)\s*[-]?\s*([A-HJ-NP-Z0-9]{2}\d{9})\b", "FR"),
    (r"\b(GB)\s*[-]?\s*(\d{9}|\d{12})\b", "GB"),
    (r"\b(IE)\s*[-]?\s*(\d{7}[A-Z]{1,2}|\d[A-Z\d]\d{5}[A-Z])\b", "IE"),
    (r"\b(NL)\s*[-]?\s*(\d{9}B\d{2})\b", "NL"),
    (r"\b(BE)\s*[-]?\s*(0?\d{9})\b", "BE"),
    (r"\b(IT)\s*[-]?\s*(\d{11})\b", "IT"),
    (r"\b(ES)\s*[-]?\s*([A-Z]\d{7}[A-Z0-9])\b", "ES"),
    (r"\b(SE)\s*[-]?\s*(\d{12})\b", "SE"),
    (r"\b(FI)\s*[-]?\s*(\d{8})\b", "FI"),
    (r"\b(DK)\s*[-]?\s*(\d{8})\b", "DK"),
    (r"\b(AT)\s*[-]?\s*(U\d{8})\b", "AT"),
    (r"\b(CZ)\s*[-]?\s*(\d{8,10})\b", "CZ"),
    (r"\b(SK)\s*[-]?\s*(\d{10})\b", "SK"),
    (r"\b(HU)\s*[-]?\s*(\d{8})\b", "HU"),
    (r"\b(BG)\s*[-]?\s*(\d{9,10})\b", "BG"),
    (r"\b(RO)\s*[-]?\s*(\d{2,10})\b", "RO"),
    (r"\b(HR)\s*[-]?\s*(\d{11})\b", "HR"),
    (r"\b(SI)\s*[-]?\s*(\d{8})\b", "SI"),
    (r"\b(LU)\s*[-]?\s*(\d{8})\b", "LU"),
    (r"\b(MT)\s*[-]?\s*(\d{8})\b", "MT"),
    (r"\b(CY)\s*[-]?\s*(\d{8}[A-Z])\b", "CY"),
    (r"\b(PT)\s*[-]?\s*(\d{9})\b", "PT"),
    (r"\b(GR|EL)\s*[-]?\s*(\d{9})\b", "EL"),
]

# Latvian "Reg. Nr." patterns (without LV prefix) -- often appears separately
_LV_REG_PATTERNS = [
    r"(?:PVN\s*)?[Rr]eg\.?\s*[Nn]r\.?[:\s]*(\d{11})",
    r"[Nn]odoklu\s+maks\.?\s*reg\.?\s*nr\.?[:\s]*(\d{11})",
    r"[Vv][AaĀā][Tt]\s*(?:[Nn]o\.?|[Nn]umber)?[:\s]*(LV\d{11})",
    r"[Tt]ax\s*ID[:\s]*(LV\d{11})",
]

_IBAN_PATTERN = r"\b([A-Z]{2}\d{2}\s?(?:[A-Z0-9]{4}\s?){3,7}[A-Z0-9]{1,4})\b"
_LV_POSTAL = r"\bLV\s*[-]?\s*(\d{4})\b"
_ADDRESS_HINTS = (
    r"(?:iela|street|str\.?|prospekts|aleja|laukums|bulvaris|"
    r"strasse|straße|ulica|avenue|ave\.?|road|rd\.?)"
)


def _extract_vat_details(text: str) -> Dict[str, Any]:
    """Extract VAT, registration number, IBAN, postal code, address hints from raw text.

    Returns a dict with optional keys: vat_number, registration_number, iban,
    postal_code, address_hint. Missing fields are omitted.
    """
    out: Dict[str, Any] = {}

    # 1. EU VAT
    for pattern, country in _VAT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            out["vat_number"] = (country + m.group(2)).upper().replace(" ", "").replace("-", "")
            out["vat_country"] = country
            break

    # 2. Latvian Reg.Nr. specifically
    for pattern in _LV_REG_PATTERNS:
        m = re.search(pattern, text)
        if m:
            digits = m.group(1)
            if digits.startswith("LV"):
                out.setdefault("vat_number", digits)
                out["registration_number"] = digits[2:]
            else:
                out["registration_number"] = digits
                out.setdefault("vat_number", "LV" + digits)
            break

    # 3. IBAN
    m = re.search(_IBAN_PATTERN, text)
    if m:
        out["iban"] = re.sub(r"\s+", "", m.group(1))

    # 4. Latvian postal code
    m = re.search(_LV_POSTAL, text)
    if m:
        out["postal_code"] = "LV-" + m.group(1)

    # 5. Address hint (line containing street keyword)
    for line in text.split("\n"):
        if re.search(_ADDRESS_HINTS, line, re.IGNORECASE) and 5 < len(line.strip()) < 200:
            out["address_hint"] = line.strip()
            break

    return out


def _enrich_with_regex_details(result: Any, raw_text: str) -> Any:
    """Merge regex-extracted vendor details into the parsed result, then call VIES.

    Pipeline:
      1. Regex pulls VAT/IBAN/postal/address from raw text.
      2. Merge into vendor dict (LLM values win where present).
      3. If a VAT was found, hit VIES for the official name + legal address.
    """
    details = _extract_vat_details(raw_text)

    def _merge_into_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
        vendor = doc.get("vendor")
        if isinstance(vendor, str):
            vendor = {"name": vendor}
        elif not isinstance(vendor, dict):
            vendor = {}
        for key, value in details.items():
            if not vendor.get(key):
                vendor[key] = value
        # VIES enrichment runs even without regex hits, in case LLM extracted VAT
        try:
            from services.vies import vies_enrich_vendor
            vendor = vies_enrich_vendor(vendor)
        except Exception as exc:
            logger.warning("VIES enrichment failed: %s", exc)
        doc["vendor"] = vendor
        return doc

    if isinstance(result, list):
        return [_merge_into_doc(d) if isinstance(d, dict) else d for d in result]
    if isinstance(result, dict):
        return _merge_into_doc(result)
    return result


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from a PDF using pypdf."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        pages: List[str] = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        return "\n\n".join(pages)
    except Exception as exc:
        logger.warning("pypdf extraction failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Image (Claude Vision)
# ---------------------------------------------------------------------------

def _parse_image(file_path: str) -> Dict[str, Any]:
    """Parse an image or PDF via Claude Vision API."""
    if not config.ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY set -- returning mock data")
        return _mock_response(file_path, "No API key configured (mock mode)")

    try:
        import anthropic

        with open(file_path, "rb") as f:
            raw = f.read()

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        media_map = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "pdf": "application/pdf",
        }
        media_type = media_map.get(ext, "application/octet-stream")
        b64 = base64.b64encode(raw).decode("utf-8")

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        content_block: List[Dict[str, Any]]
        if ext == "pdf":
            content_block = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                },
                {"type": "text", "text": _extraction_prompt()},
            ]
        else:
            content_block = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {"type": "text", "text": _extraction_prompt()},
            ]

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": content_block}],
        )

        text = response.content[0].text
        result = _parse_llm_json(text, file_path)
        # Run VIES on whatever VAT the LLM extracted from the image
        result = _enrich_with_regex_details(result, "")
        return result

    except Exception as exc:
        logger.exception("Vision parsing failed: %s", exc)
        return _mock_response(file_path, "Vision parsing error: %s" % str(exc))


# ---------------------------------------------------------------------------
# Text-based LLM extraction
# ---------------------------------------------------------------------------

def _parse_text_with_llm(text: str, filename: str) -> Dict[str, Any]:
    """Send extracted text to Claude for structured extraction."""
    if not config.ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY set -- returning mock data")
        return _mock_response(filename, "No API key configured (mock mode)")

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = _extraction_prompt() + "\n\nDocument text:\n" + text[:8000]

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        return _parse_llm_json(response.content[0].text, filename)

    except Exception as exc:
        logger.exception("Text LLM parsing failed: %s", exc)
        return _mock_response(filename, "LLM parsing error: %s" % str(exc))


# ---------------------------------------------------------------------------
# CSV / XLSX
# ---------------------------------------------------------------------------

def _parse_csv(file_path: str) -> Dict[str, Any]:
    """Parse a CSV bank or invoice export."""
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        line_items: List[Dict[str, Any]] = []
        total = 0.0
        vendor = ""
        for row in rows[:50]:
            amount = _extract_amount_from_row(row)
            total += amount
            desc = " ".join(str(v) for v in row.values() if v)
            if not vendor:
                vendor = _guess_vendor_from_text(desc)
            line_items.append({
                "description": desc[:200],
                "amount": amount,
                "raw": {k: v for k, v in row.items()},
            })

        return {
            "document_type": "bank_statement" if len(rows) > 3 else "invoice",
            "vendor": vendor,
            "dates": {"document_date": datetime.utcnow().strftime("%Y-%m-%d")},
            "money": {
                "total_amount": round(total, 2),
                "currency": "EUR",
                "line_count": len(line_items),
            },
            "line_items": line_items,
            "ledger_codes": [],
            "warnings": [],
        }
    except Exception as exc:
        logger.exception("CSV parsing failed: %s", exc)
        return _mock_response(file_path, "CSV parse error: %s" % str(exc))


def _parse_xlsx(file_path: str) -> Dict[str, Any]:
    """Parse an XLSX file."""
    try:
        from openpyxl import load_workbook

        wb = load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            return _mock_response(file_path, "Empty workbook")

        headers: List[str] = []
        line_items: List[Dict[str, Any]] = []
        total = 0.0
        vendor = ""

        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(c) if c else f"col_{j}" for j, c in enumerate(row)]
                continue
            if i > 50:
                break
            row_dict = {
                headers[j]: (cell if cell is not None else "")
                for j, cell in enumerate(row)
                if j < len(headers)
            }
            amount = _extract_amount_from_row(row_dict)
            total += amount
            desc = " ".join(str(v) for v in row_dict.values() if v)
            if not vendor:
                vendor = _guess_vendor_from_text(desc)
            line_items.append({
                "description": desc[:200],
                "amount": amount,
                "raw": row_dict,
            })

        wb.close()

        return {
            "document_type": "spreadsheet",
            "vendor": vendor,
            "dates": {"document_date": datetime.utcnow().strftime("%Y-%m-%d")},
            "money": {
                "total_amount": round(total, 2),
                "currency": "EUR",
                "line_count": len(line_items),
            },
            "line_items": line_items,
            "ledger_codes": [],
            "warnings": [],
        }
    except Exception as exc:
        logger.exception("XLSX parsing failed: %s", exc)
        return _mock_response(file_path, "XLSX parse error: %s" % str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extraction_prompt() -> str:
    """Return the system prompt for financial document extraction."""
    return (
        "You are a financial document parser for Amitours Holding (Latvia). "
        "This image may contain MULTIPLE receipts, invoices, or documents. "
        "Analyze the image carefully and extract EACH separate document individually.\n\n"
        "CRITICAL: If you see multiple receipts/invoices in one image, return a JSON ARRAY "
        "with one object per document. If there is only one document, still return an array with one element.\n\n"
        "Return ONLY valid JSON (no markdown fences). Format:\n\n"
        "[\n"
        "  {\n"
        '    "document_type": "invoice" | "receipt" | "bank_tx" | "credit_note" | "terminal_receipt" | "other",\n'
        '    "vendor": {\n'
        '      "name": "SIA Example Company",\n'
        '      "vat_number": "LV40203241255",\n'
        '      "address": "Street 1, Riga, Latvia"\n'
        "    },\n"
        '    "dates": {"document_date": "YYYY-MM-DD", "due_date": null},\n'
        '    "money": {\n'
        '      "total_amount": 0.00,\n'
        '      "tax_amount": 0.00,\n'
        '      "net_amount": 0.00,\n'
        '      "currency": "EUR"\n'
        "    },\n"
        '    "line_items": [\n'
        "      {\n"
        '        "description_original": "Imeretiesu hacapuri",\n'
        '        "description_en": "Imereti khachapuri (Georgian cheese bread)",\n'
        '        "description": "Imereti khachapuri (Georgian cheese bread)",\n'
        '        "amount": 9.50,\n'
        '        "quantity": 1,\n'
        '        "language": "lv"\n'
        "      }\n"
        "    ],\n"
        '    "payment_method": "card" | "bank" | "cash" | "unknown",\n'
        '    "is_terminal_receipt": false,\n'
        '    "linked_vendor": null,\n'
        '    "our_entity": null,\n'
        '    "ledger_codes": [],\n'
        '    "warnings": []\n'
        "  }\n"
        "]\n\n"
        "IMPORTANT RULES:\n\n"
        "VAT NUMBER EXTRACTION (CRITICAL):\n"
        "- ALWAYS look for the vendor's VAT/tax registration number\n"
        "- Latvian patterns: 'PVN Reg. Nr.', 'Reg. Nr.', 'PVN reg. nr.', 'PVN Nr.', 'Nodoklu maks. reg. nr.'\n"
        "- Format is usually 'LV' followed by 11 digits, e.g. LV40203241255\n"
        "- Also look for generic 'VAT', 'Tax ID', 'TIN' patterns\n"
        "- If a registration number is found without 'LV' prefix but is 11 digits, add 'LV' prefix\n\n"
        "LANGUAGE DETECTION & TRANSLATION:\n"
        "- Detect the language of each line item description\n"
        "- Translate ALL line items to English in 'description_en'\n"
        "- Keep the original text in 'description_original'\n"
        "- Set 'description' to the English translation\n"
        "- Set 'language' to ISO 639-1 code (lv, ru, ka, en, etc.)\n"
        "- Common Latvian food: 'hacapuri'=khachapuri, 'pelmenji'=dumplings, "
        "'kotlete'=cutlet, 'zupa'=soup, 'salati'=salad\n\n"
        "TERMINAL RECEIPT LINKING:\n"
        "- Card terminal receipts (kvitanciis/kvits) are separate from purchase receipts\n"
        "- Set 'is_terminal_receipt': true for card terminal receipts\n"
        "- Set 'document_type': 'terminal_receipt' for these\n"
        "- If a terminal receipt (e.g., 'ZUNDA TOWERS' terminal) appears alongside a purchase receipt "
        "for the SAME amount, link them:\n"
        '  - On the terminal receipt: set "linked_vendor": {"name": "SIA Snabb", "vat_number": "LV..."}\n'
        '  - Add warning: "duplicate_payment_confirmation - this is a card terminal receipt for the same payment"\n\n'
        "OTHER RULES:\n"
        "- Latvian receipts: look for 'PVN' (VAT), 'Kopa EUR' (total), 'Bez PVN' (net), 'Summa PVN' (tax)\n"
        "- Extract the actual company name from 'SIA ...' or 'AS ...' headers\n"
        "- Amounts should be positive numbers\n"
        "- Currency is usually EUR for Latvian documents\n"
        "- Date format in Latvia: DD.MM.YYYY or YYYY-MM-DD\n"
        "- For payment_method: detect from context (e.g., 'Bankas karte'='card', 'Skaidra nauda'='cash')"
    )


def _parse_llm_json(text: str, filename: str) -> Any:
    """Extract JSON from LLM response text. May return list (multi-receipt) or dict."""
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip())
    try:
        result = json.loads(cleaned)
        # If it's a list of documents, return as-is (but never empty)
        if isinstance(result, list):
            if not result:
                logger.warning("LLM returned empty list for %s -- treating as unparseable", filename)
                return _mock_response(filename, "LLM returned empty list (likely blank/scanned/unreadable document)")
            logger.info("Multi-document parse: %d documents found in %s", len(result), filename)
            return result
        return result
    except json.JSONDecodeError:
        # Try to find JSON array first, then object
        match_arr = re.search(r"\[[\s\S]*\]", cleaned)
        if match_arr:
            try:
                result = json.loads(match_arr.group())
                if isinstance(result, list) and result:
                    return result
            except json.JSONDecodeError:
                pass
        match_obj = re.search(r"\{[\s\S]*\}", cleaned)
        if match_obj:
            try:
                return json.loads(match_obj.group())
            except json.JSONDecodeError:
                pass
        logger.warning("Could not parse LLM JSON for %s", filename)
        return _mock_response(filename, "Failed to parse LLM response")


def _extract_amount_from_row(row: Dict[str, Any]) -> float:
    """Try to find an amount value in a row dictionary."""
    amount_keys = ["amount", "total", "sum", "debit", "credit", "value", "price"]
    for key in row:
        if any(ak in str(key).lower() for ak in amount_keys):
            try:
                val = str(row[key]).replace(",", ".").replace(" ", "")
                return abs(float(val))
            except (ValueError, TypeError):
                continue
    return 0.0


def _guess_vendor_from_text(text: str) -> str:
    """Try to extract a vendor name from free text."""
    known = [
        "Google", "Facebook", "LinkedIn", "AWS", "Hetzner",
        "Stripe", "Revolut", "Sebra Auto", "PeopleForce",
    ]
    for v in known:
        if v.lower() in text.lower():
            return v
    return ""


def _mock_response(filename: str, warning: str) -> Dict[str, Any]:
    """Return a mock parsed response for testing without API key."""
    return {
        "document_type": "invoice",
        "vendor": "Mock Vendor Ltd",
        "dates": {
            "document_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "due_date": None,
        },
        "money": {
            "total_amount": 1250.00,
            "tax_amount": 250.00,
            "net_amount": 1000.00,
            "currency": "EUR",
        },
        "line_items": [
            {
                "description": "Sample service (mock data)",
                "amount": 1000.00,
                "quantity": 1,
            }
        ],
        "ledger_codes": [],
        "warnings": [warning],
    }
