#!/usr/bin/env python3
"""Inbox claim/processed state machine for the Feishu auto-reply hook.

The hook's polling script calls ``check``: it returns (as JSON on stdout)
inbox entries that are neither processed nor currently claimed, and
claims them so a later poll does not wake a second worker for the same
message while the first is still answering. Claims expire after
CLAIM_TTL seconds (a crashed worker's messages become pending again,
unless already processed).

The woken worker calls ``processed <message_id>...`` after it has sent
its reply, moving those ids from claimed to processed permanently.

``stale [threshold_seconds]`` (default 600) is the fallback detector for
the "user got the ack but never got a reply" case: it lists accepted
messages that are still not processed past the threshold, oldest first,
as JSON. ``feishu.sh doctor`` surfaces the same signal as a warning.

When HATCH_HOOK_DRY_RUN=1, ``check`` only peeks (no state mutation), so
hook dry runs never consume detections.

State lives in handled.json next to this file (git-ignored):
    {"processed": ["om_...", ...],
     "claimed":   {"om_...": <epoch_seconds>}}
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INBOX_PATH = BASE_DIR / "inbox.jsonl"
HANDLED_PATH = BASE_DIR / "handled.json"

CLAIM_TTL = 1800  # seconds
MAX_PER_WAKE = 10
MAX_TEXT_CHARS = 2000


def load_state() -> dict:
    try:
        data = json.loads(HANDLED_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("processed", [])
            data.setdefault("claimed", {})
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"processed": [], "claimed": {}}


def save_state(state: dict) -> None:
    tmp = HANDLED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(HANDLED_PATH)


def read_inbox() -> list[dict]:
    entries = []
    try:
        lines = INBOX_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("message_id"):
            entries.append(entry)
    return entries


def cmd_check() -> int:
    dry = os.environ.get("HATCH_HOOK_DRY_RUN") == "1"
    state = load_state()
    now = time.time()
    processed = set(state["processed"])
    claimed = {
        mid: ts
        for mid, ts in state["claimed"].items()
        if mid not in processed and now - float(ts) < CLAIM_TTL
    }

    new = []
    for entry in read_inbox():
        mid = entry["message_id"]
        if mid in processed or mid in claimed:
            continue
        item = {
            "message_id": mid,
            "chat_id": entry.get("chat_id", ""),
            "ts": entry.get("ts", ""),
            "kind": entry.get("kind", "text"),
            "text": str(entry.get("text", ""))[:MAX_TEXT_CHARS],
        }
        if entry.get("media_paths"):
            item["media_paths"] = list(entry["media_paths"])
        if entry.get("file_name"):
            item["file_name"] = entry["file_name"]
        if entry.get("media_error"):
            item["media_error"] = entry["media_error"]
        if entry.get("approval_id"):
            item["approval_id"] = entry["approval_id"]
        if entry.get("decision"):
            item["decision"] = entry["decision"]
        if entry.get("sender_role"):
            # Peer-bot collaboration entries: the woken worker must be
            # able to tell these apart from owner messages — they are
            # never owner instructions or authorizations.
            item["sender_role"] = entry["sender_role"]
            item["sender_open_id"] = entry.get("sender_open_id", "")
            if entry.get("peer_bot_name"):
                item["peer_bot_name"] = entry["peer_bot_name"]
            if entry.get("authority"):
                item["authority"] = entry["authority"]
            if entry.get("peer_discovery"):
                item["peer_discovery"] = True
        new.append(item)
        claimed[mid] = now
        if len(new) >= MAX_PER_WAKE:
            break

    if not dry:
        state["processed"] = sorted(processed)
        state["claimed"] = claimed
        save_state(state)
    print(json.dumps({"new": new}, ensure_ascii=False))
    return 0


def parse_ts_epoch(raw: object) -> float | None:
    """Parse an inbox entry's ISO-8601 ``ts`` into epoch seconds."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def find_stale(
    entries: list[dict],
    processed: set[str],
    now_epoch: float,
    threshold_seconds: float,
) -> list[tuple[dict, float]]:
    """Unprocessed entries older than the threshold, oldest first.

    This is the offline / stuck-worker fallback signal: a message the
    bridge accepted (and acked) but no worker ever marked processed.
    Claimed-but-crashed workers are covered too, because a claim is not
    a completion — the entry stays unprocessed until ``processed`` runs.
    """
    stale = []
    for entry in entries:
        mid = entry.get("message_id")
        if not mid or mid in processed:
            continue
        ts_epoch = parse_ts_epoch(entry.get("ts"))
        if ts_epoch is None:
            continue
        age = now_epoch - ts_epoch
        if age > threshold_seconds:
            stale.append((entry, age))
    stale.sort(key=lambda item: item[1], reverse=True)
    return stale


def cmd_stale(threshold_seconds: float) -> int:
    state = load_state()
    stale = find_stale(
        read_inbox(), set(state["processed"]), time.time(), threshold_seconds
    )
    payload = {
        "stale": [
            {
                "message_id": entry["message_id"],
                "chat_id": entry.get("chat_id", ""),
                "ts": entry.get("ts", ""),
                "kind": entry.get("kind", "text"),
                "age_seconds": int(age),
            }
            for entry, age in stale
        ]
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_processed(ids: list[str], *, seed: bool = False) -> int:
    state = load_state()
    processed = set(state["processed"])
    claimed = dict(state["claimed"])
    for mid in ids:
        processed.add(mid)
        claimed.pop(mid, None)
    state["processed"] = sorted(processed)
    state["claimed"] = claimed
    save_state(state)
    print(f"ok: {len(ids)} id(s) marked processed")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "check":
        return cmd_check()
    if len(argv) >= 2 and argv[1] == "stale":
        threshold = 600.0
        if len(argv) >= 3:
            try:
                threshold = float(argv[2])
            except ValueError:
                print("stale threshold must be seconds", file=sys.stderr)
                return 2
        return cmd_stale(threshold)
    if len(argv) >= 3 and argv[1] == "processed":
        return cmd_processed(argv[2:])
    if len(argv) >= 3 and argv[1] == "seed-processed":
        return cmd_processed(argv[2:], seed=True)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
