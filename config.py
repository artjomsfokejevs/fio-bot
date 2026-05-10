"""FIO configuration module."""
from __future__ import annotations

import os

_BASE = os.path.dirname(os.path.abspath(__file__))

# Load .env manually (dotenv has issues with override)
_env_path = os.path.join(_BASE, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _key, _val = _k.strip(), _v.strip()
                if _val and (not os.environ.get(_key)):
                    os.environ[_key] = _val

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "data", "intake")
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "fio.db")
RULES_FILE = os.path.join(os.path.dirname(__file__), "data", "accounting_rules.json")
ACTUALS_FILE = os.path.join(os.path.dirname(__file__), "data", "accounting_actuals.json")
LEDGER_FILE = os.path.join(os.path.dirname(__file__), "data", "ledger_schema.json")
MAX_FILE_SIZE_MB = 20
CONFIDENCE_AUTO_POST = 90
CONFIDENCE_REVIEW = 70
