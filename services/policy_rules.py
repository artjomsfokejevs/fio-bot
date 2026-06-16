"""Editable expense-policy rules — replaces hardcoded EXPENSE_POLICIES.

Each rule is one numeric threshold (e.g. "office_supplies > 500 EUR per item → RED").
Admin/CEO edit via the Policies & Limits tab; classifier.check_expense_policy()
reads the current effective rule set via `get_effective_policies()`.

Falls back to DEFAULTS (mirrors original hardcoded values) when the DB is empty
— so a fresh install still produces violations using the canonical thresholds.

Added 2026-06-16 for Phase 1 P1.2.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "list_rules",
    "get_rule",
    "create_rule",
    "update_rule",
    "delete_rule",
    "history_for",
    "get_effective_policies",
    "DEFAULTS_BY_POLICY",
    "seed_if_missing",
]

# Mirror of original EXPENSE_POLICIES constants — used as fallback only.
DEFAULTS_BY_POLICY: Dict[str, Dict[str, Any]] = {
    "business_dinner": {
        "max_per_person": 50.0,
        "max_total": 200.0,
        "requires": "attendee_list",
    },
    "business_travel": {
        "max_per_day": 150.0,
        "requires": "travel_order",
    },
    "office_supplies": {
        "max_per_item": 500.0,
    },
}


# ---------------------------------------------------------------------------
# Simple version-bumped cache to avoid hitting SQLite on every classify call.
# ---------------------------------------------------------------------------
_cache: Dict[str, Any] = {"version": 0, "loaded_at": 0.0, "rules": None}
_version = 0


def _bump_version() -> None:
    global _version
    _version += 1


def _all_active_rows() -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM policy_rules WHERE active = 1 ORDER BY policy_name, field"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_effective_policies() -> Dict[str, Dict[str, Any]]:
    """Return the EXPENSE_POLICIES-shaped dict the classifier expects.

    Reads from DB; falls back to DEFAULTS_BY_POLICY when DB has no rows for
    a given policy (graceful M81 pattern).
    """
    global _cache
    if _cache["version"] == _version and _cache["rules"] is not None:
        return _cache["rules"]

    out: Dict[str, Dict[str, Any]] = {
        name: dict(spec) for name, spec in DEFAULTS_BY_POLICY.items()
    }
    try:
        rows = _all_active_rows()
    except Exception:  # noqa: BLE001 — graceful, never block classify
        logger.exception("policy_rules: DB read failed, using DEFAULTS")
        rows = []

    # Preserve original keyword lists (they live only in classifier.py — we
    # don't store them in the DB rows).
    keywords = {
        "business_dinner": ["restaurant", "dinner", "lunch", "cafe", "bar",
                            "bistro", "grill", "pizza", "sushi", "food"],
        "business_travel": ["hotel", "flight", "taxi", "parking", "fuel",
                            "gas", "petrol", "toll", "train", "bus"],
        "office_supplies": ["office", "supplies", "stationery", "equipment",
                            "printer", "paper", "toner", "desk", "chair"],
    }

    for r in rows:
        pname = r["policy_name"]
        field = r["field"]
        if pname not in out:
            out[pname] = {}
        out[pname][field] = float(r["threshold_eur"])
        if r.get("requires"):
            out[pname]["requires"] = r["requires"]
        # Track the explicit level/code so the classifier can attach
        # rule_code + level back to each warning.
        out[pname].setdefault("_rules", {})[field] = {
            "code": r["code"],
            "level": r["level"],
            "description": r.get("description"),
        }

    for pname, kw in keywords.items():
        if pname in out:
            out[pname]["category_keywords"] = kw

    _cache = {
        "version": _version,
        "loaded_at": time.time(),
        "rules": out,
    }
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_rules(active_only: bool = False) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        sql = "SELECT * FROM policy_rules"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY active DESC, policy_name, field"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_rule(rule_id: int) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM policy_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _validate(payload: Dict[str, Any], *, partial: bool = False) -> None:
    required = ("code", "policy_name", "field", "level", "threshold_eur")
    if not partial:
        for k in required:
            if not payload.get(k) and payload.get(k) != 0:
                raise ValueError("Missing required field: %s" % k)
    if "level" in payload and payload["level"] not in ("red", "yellow", "green"):
        raise ValueError("level must be one of: red, yellow, green")
    if "threshold_eur" in payload:
        try:
            payload["threshold_eur"] = float(payload["threshold_eur"])
        except (TypeError, ValueError):
            raise ValueError("threshold_eur must be a number")
        if payload["threshold_eur"] < 0:
            raise ValueError("threshold_eur must be >= 0")


def _append_history(conn, rule_id: Optional[int], rule_code: str,
                    change_type: str, old: Any, new: Any, by: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO policy_rules_history "
        "(rule_id, rule_code, changed_at, changed_by, change_type, old_json, new_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            rule_id, rule_code, datetime.utcnow().isoformat(), by, change_type,
            json.dumps(old) if old is not None else None,
            json.dumps(new) if new is not None else None,
        ),
    )


def create_rule(payload: Dict[str, Any], *, by: Optional[str] = None) -> Dict[str, Any]:
    _validate(payload)
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO policy_rules "
            "(code, policy_name, field, description, level, threshold_eur, unit, "
            " requires, scope, owner, active, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                payload["code"], payload["policy_name"], payload["field"],
                payload.get("description"), payload["level"],
                float(payload["threshold_eur"]),
                payload.get("unit") or "per_invoice",
                payload.get("requires"), payload.get("scope"),
                payload.get("owner"), now, by,
            ),
        )
        rid = cur.lastrowid
        _append_history(conn, rid, payload["code"], "create", None, payload, by)
        conn.commit()
        _bump_version()
        return get_rule(rid)  # type: ignore[return-value]
    finally:
        conn.close()


def update_rule(rule_id: int, payload: Dict[str, Any],
                *, by: Optional[str] = None) -> Dict[str, Any]:
    existing = get_rule(rule_id)
    if not existing:
        raise ValueError("rule %d not found" % rule_id)
    _validate(payload, partial=True)
    now = datetime.utcnow().isoformat()
    fields = []
    values: List[Any] = []
    allowed = ("description", "level", "threshold_eur", "unit",
               "requires", "scope", "owner", "active")
    for k in allowed:
        if k in payload:
            fields.append("%s = ?" % k)
            values.append(payload[k])
    if not fields:
        return existing
    fields.append("updated_at = ?")
    values.append(now)
    fields.append("updated_by = ?")
    values.append(by)
    values.append(rule_id)
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE policy_rules SET %s WHERE id = ?" % ", ".join(fields),
            tuple(values),
        )
        _append_history(conn, rule_id, existing["code"], "update", existing, payload, by)
        conn.commit()
        _bump_version()
        return get_rule(rule_id)  # type: ignore[return-value]
    finally:
        conn.close()


def delete_rule(rule_id: int, *, by: Optional[str] = None) -> bool:
    """Soft-delete (active=0) so historic warnings retain rule provenance."""
    existing = get_rule(rule_id)
    if not existing:
        return False
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE policy_rules SET active = 0, updated_at = ?, updated_by = ? "
            "WHERE id = ?",
            (datetime.utcnow().isoformat(), by, rule_id),
        )
        _append_history(conn, rule_id, existing["code"], "delete", existing, None, by)
        conn.commit()
        _bump_version()
        return True
    finally:
        conn.close()


def history_for(rule_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        if rule_id is None:
            rows = conn.execute(
                "SELECT * FROM policy_rules_history ORDER BY changed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM policy_rules_history WHERE rule_id = ? "
                "ORDER BY changed_at DESC LIMIT ?",
                (rule_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            for k in ("old_json", "new_json"):
                if rec.get(k):
                    try:
                        rec[k.replace("_json", "")] = json.loads(rec[k])
                    except (json.JSONDecodeError, TypeError):
                        rec[k.replace("_json", "")] = rec[k]
            out.append(rec)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seed on first boot (idempotent)
# ---------------------------------------------------------------------------

def seed_if_missing() -> None:
    """If the policy_rules table is empty, populate from seed/policy_rules.json."""
    conn = db.get_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0]
        if cnt > 0:
            return
    finally:
        conn.close()
    seed_path = os.path.join(os.path.dirname(__file__), "..", "seed", "policy_rules.json")
    seed_path = os.path.abspath(seed_path)
    if not os.path.exists(seed_path):
        logger.warning("policy_rules.json seed missing — falling back to DEFAULTS_BY_POLICY")
        return
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load policy_rules.json seed")
        return
    for row in rows:
        try:
            create_rule(row, by="seed")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to seed rule %s", row.get("code"))
    logger.info("Seeded %d policy rules", len(rows))
