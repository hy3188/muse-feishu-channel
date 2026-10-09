#!/usr/bin/env python3
"""Local store for Feishu approval (authorization) requests.

Approval requests are created by send.py (which sends the interactive
card) and decided by the user tapping a button on that card; bridge.py
records the decision through :func:`decide_approval`.

State lives in approvals.json next to this file (chmod 600, git-ignored):

    {"items": {"ap_xxxxxxxx": {
        "id": "ap_xxxxxxxx",
        "title": "...", "detail": "...",
        "chat_id": "oc_..." ("" when addressed by open_id),
        "receive_id_type": "chat_id" | "open_id" | "",
        "receive_id": "...",
        "status": "pending" | "approved" | "rejected",
        "created_at": "<iso8601 Z>",
        "decided_at": "<iso8601 Z>" | None,
        "decided_by": "<operator open_id>" | None,
    }}}

Decisions are idempotent: the first decision wins and later taps return
``changed=False`` without overwriting anything. The store path is a
parameter on every function so tests can use a temporary directory.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APPROVALS_PATH = BASE_DIR / "approvals.json"

DECISIONS = ("approved", "rejected")

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": {}}


def _save(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def create_approval(
    title: str,
    detail: str,
    chat_id: str,
    receive_id_type: str = "",
    receive_id: str = "",
    path: Path = APPROVALS_PATH,
) -> dict:
    """Create a pending approval entry and return it."""
    entry = {
        "id": "ap_" + secrets.token_hex(4),
        "title": str(title),
        "detail": str(detail),
        "chat_id": str(chat_id or ""),
        "receive_id_type": str(receive_id_type or ""),
        "receive_id": str(receive_id or ""),
        "status": "pending",
        "created_at": _utc_now_iso(),
        "decided_at": None,
        "decided_by": None,
    }
    with _lock:
        data = _load(path)
        data["items"][entry["id"]] = entry
        _save(path, data)
    return entry


def get_approval(approval_id: str, path: Path = APPROVALS_PATH) -> dict | None:
    """Return the stored entry for approval_id, or None."""
    with _lock:
        entry = _load(path)["items"].get(approval_id)
    return dict(entry) if isinstance(entry, dict) else None


def decide_approval(
    approval_id: str,
    decision: str,
    operator_open_id: str,
    path: Path = APPROVALS_PATH,
) -> tuple[dict | None, bool]:
    """Record a decision. Returns (entry, changed).

    (None, False) when the approval does not exist. (entry, False) when
    it was already decided — the original decision is kept untouched,
    so repeated button taps are idempotent.
    """
    if decision not in DECISIONS:
        raise ValueError(f"invalid decision: {decision!r}")
    with _lock:
        data = _load(path)
        entry = data["items"].get(approval_id)
        if not isinstance(entry, dict):
            return None, False
        if entry.get("status") != "pending":
            return dict(entry), False
        entry["status"] = decision
        entry["decided_at"] = _utc_now_iso()
        entry["decided_by"] = str(operator_open_id or "")
        _save(path, data)
        return dict(entry), True
