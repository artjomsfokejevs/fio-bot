"""Tests for services.parser — Claude vision + OCR vendor hints."""
from __future__ import annotations

import pytest


def test_parser_module_imports():
    """Catches missing imports — production bug shield."""
    from services import parser
    assert hasattr(parser, "parse_document")


def test_parse_document_missing_file_returns_empty_dict():
    """Error path: nonexistent file → empty result dict, not exception.

    FINDING: parser silently swallows FileNotFoundError and returns an
    all-None doc. This is a real bug (caller cannot distinguish missing
    file from bad OCR). Tracked for follow-up; test pins current behaviour
    to prevent silent regression.
    """
    from services import parser
    out = parser.parse_document("/tmp/does-not-exist-xyz-12345.pdf", "pdf")
    assert isinstance(out, dict)
    assert out.get("vendor") is None
    assert out.get("document_type") is None
