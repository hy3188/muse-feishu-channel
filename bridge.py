#!/usr/bin/env python3
"""Feishu/Lark -> Muse inbox bridge (WebSocket long connection).

Connects with lark_oapi.ws.Client (no public port / webhook needed),
subscribes to im.message.receive_v1, and for every *accepted* message
appends one JSON line to inbox.jsonl:

    {"ts": ..., "message_id": ..., "chat_id": ...,
     "sender_open_id": ..., "text": ...}

Acceptance rules (allowlist):
  * the sender open_id must equal config.owner_open_id
  * private (p2p) text, image, file and post messages are accepted;
    supported media resources are downloaded into media/ when possible
  * group messages are ignored unless config.allow_group_mentions=true,
    in which case the owner's message must mention this bot (with an
    explicitly logged degraded rule if the bot id cannot be fetched)
  * peer bots (config.peer_bots, a restricted collaboration allowlist
    strictly separate from the owner): a peer bot's message is accepted
    ONLY in a group chat, ONLY when it mentions this bot, ONLY with
    sender_type in {"app", "bot"} (real peer apps have been observed
    delivering as "bot" — the allowlisted open_id is the identity
    check, sender_type only screens out human "user" senders), and
    ONLY inside an owner-started task chain for
    that chat (an accepted owner group message opens/refreshes the
    chain; config.peer_chain_ttl_seconds bounds its lifetime, default
    24h). Each accepted peer message consumes one handover; at
    config.peer_max_handovers (default 6) the chain stops accepting
    peer messages (loop prevention). Peer entries are written with
    sender_role="peer_bot" and authority="collaboration_only" and are
    NEVER owner instructions or authorizations.
  * discovery exception (config.peer_allow_discovery=true): a peer
    bot may introduce itself WITHOUT an owner-started chain, but only
    with a group text message whose text starts with the discovery
    prefix "【自我介绍】". Discovery entries are rate-limited per
    peer+chat (config.peer_discovery_limit, default 3, per rolling
    config.peer_discovery_window_seconds, default 24h), are marked
    peer_discovery=true, and stay collaboration_only — introducing
    is not instructing.
  * duplicate message_id values are dropped (state.json)

In production mode an accepted message also triggers a short instant
acknowledgement ("收到，正在处理，稍等。", per-chat 60s throttle,
config ack_on_receive=false disables it) so the user is not left
staring at silence while Muse wakes up.

Approval cards: card.action.trigger callbacks are handled here too.
Only the owner may decide; the first decision is stored in
approvals.json and mirrored into inbox.jsonl as a kind="decision"
record (message_id "decision_<approval_id>") so the hook pipeline
treats it like any other user instruction.

Everything else is ignored and logged. With --echo-test, an accepted
message is answered with "已收到：<original text>" or a media placeholder
(integration testing only; off by default).

Requires config.json from onboard.py; exits gracefully with a clear
message when not onboarded.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import approvals  # noqa: E402
import common  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("feishu-bridge")

MAX_SEEN_IDS = 2000
_AT_TAG = re.compile(r"<at[^>]*>.*?</at>", re.DOTALL)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Event extraction: supports the typed SDK event object AND a plain dict
# with the raw event JSON shape (used by tests / future transports).
# ---------------------------------------------------------------------------
_USER_PLACEHOLDER = re.compile(r"@_user_\d+\b")
_SUPPORTED_KINDS = {"text", "image", "file", "post"}
_ECHO_LABELS = {"image": "图片", "file": "文件", "post": "post"}


def _clean_text(value: object) -> str:
    text = _AT_TAG.sub("", str(value or ""))
    return _USER_PLACEHOLDER.sub("", text).strip()


def _flatten_post_node(node, image_keys: list[str]) -> str:
    """Flatten one post content node/paragraph into plain text."""
    if isinstance(node, str):
        return _USER_PLACEHOLDER.sub("", node)
    if isinstance(node, list):
        parts = [_flatten_post_node(child, image_keys) for child in node]
        # A list of dicts is one paragraph; a list of lists is the
        # top-level paragraph collection and gets newline separators.
        if parts and all(isinstance(child, dict) for child in node):
            return "".join(parts)
        return "\n".join(part for part in parts if part)
    if not isinstance(node, dict):
        return ""

    tag = node.get("tag", "")
    if tag == "img":
        image_key = node.get("image_key") or node.get("imageKey") or ""
        if image_key:
            image_keys.append(str(image_key))
        return ""
    if tag in {"text", "a", "at"}:
        value = (
            node.get("text")
            or node.get("user_name")
            or node.get("name")
            or node.get("user_id")
            or ""
        )
        return _USER_PLACEHOLDER.sub("", str(value))
    # Unknown inline nodes: retain any obvious textual payload.
    for key in ("text", "title", "content"):
        if key in node:
            return _flatten_post_node(node[key], image_keys)
    return ""


def _parse_content(message_type: str, raw_content: str) -> dict:
    """Parse a message content JSON string by message type."""
    parsed: dict = {}
    if raw_content:
        try:
            candidate = json.loads(raw_content)
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, TypeError):
            parsed = {}

    result = {
        "text": "",
        "image_key": "",
        "file_key": "",
        "file_name": "",
        "image_keys": [],
        "media_items": [],
    }
    if message_type == "text":
        result["text"] = _clean_text(parsed.get("text", raw_content))
    elif message_type == "image":
        image_key = str(parsed.get("image_key", "") or "")
        result["image_key"] = image_key
        if image_key:
            result["image_keys"] = [image_key]
            result["media_items"] = [{"file_key": image_key, "kind": "image"}]
    elif message_type == "file":
        file_key = str(parsed.get("file_key", "") or "")
        result["file_key"] = file_key
        result["file_name"] = str(parsed.get("file_name", "") or "")
        if file_key:
            result["media_items"] = [{"file_key": file_key, "kind": "file"}]
    elif message_type == "post":
        image_keys: list[str] = []
        paragraphs: list[str] = []
        title = _flatten_post_node(parsed.get("title", ""), image_keys).strip()
        if title:
            paragraphs.append(title)
        content = parsed.get("content", [])
        if isinstance(content, list):
            for paragraph in content:
                flattened = _flatten_post_node(paragraph, image_keys).strip()
                if flattened:
                    paragraphs.append(flattened)
        elif content:
            flattened = _flatten_post_node(content, image_keys).strip()
            if flattened:
                paragraphs.append(flattened)
        result["text"] = "\n".join(paragraphs).strip()
        result["image_keys"] = image_keys
        result["media_items"] = [
            {"file_key": key, "kind": "image"} for key in image_keys
        ]
    else:
        result["text"] = _clean_text(parsed.get("text", ""))
    return result


def _mention_open_ids(raw_mentions) -> list[str]:
    open_ids: list[str] = []
    for mention in raw_mentions or []:
        if isinstance(mention, dict):
            identity = mention.get("id") or {}
            if isinstance(identity, dict):
                open_id = identity.get("open_id", "")
            else:
                open_id = getattr(identity, "open_id", "") or ""
        else:
            identity = getattr(mention, "id", None)
            open_id = getattr(identity, "open_id", "") if identity else ""
        if open_id:
            open_ids.append(str(open_id))
    return open_ids


def extract_record(event) -> dict:
    if isinstance(event, dict):
        data = event.get("event") or {}
        sender = data.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        message = data.get("message") or {}
        raw_content = message.get("content") or ""
        sender_open_id = sender_id.get("open_id", "")
        sender_type = sender.get("sender_type", "") or ""
        chat_type = message.get("chat_type", "")
        message_type = message.get("message_type", "")
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        raw_mentions = message.get("mentions") or []
    else:
        data = event.event
        message = data.message
        sender_open_id = data.sender.sender_id.open_id or ""
        sender_type = getattr(data.sender, "sender_type", "") or ""
        chat_type = message.chat_type or ""
        message_type = message.message_type or ""
        message_id = message.message_id or ""
        chat_id = message.chat_id or ""
        raw_content = message.content or ""
        raw_mentions = message.mentions or []

    parsed = _parse_content(message_type, raw_content)
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "kind": message_type,
        "sender_open_id": sender_open_id,
        "sender_type": sender_type,
        "text": parsed["text"],
        "image_key": parsed["image_key"],
        "file_key": parsed["file_key"],
        "file_name": parsed["file_name"],
        "image_keys": parsed["image_keys"],
        "media_items": parsed["media_items"],
        "mention_open_ids": _mention_open_ids(raw_mentions),
    }


# ---------------------------------------------------------------------------
# Core processor (transport-independent; unit-tested by tests/selftest.py)
# ---------------------------------------------------------------------------
class MessageProcessor:
    def __init__(
        self,
        owner_open_id: str,
        inbox_path: Path = common.INBOX_PATH,
        state_path: Path = common.STATE_PATH,
        echo_sender=None,  # callable(chat_id, text) or None
        downloader=None,  # callable(message_id, file_key, kind) -> path|None
        allow_group_mentions: bool = False,
        bot_open_id: str | None = None,
        peer_bots: dict | None = None,  # {open_id: display name}
        max_peer_handovers: int = 6,
        peer_chain_ttl_seconds: float = 86400.0,
        peer_allow_discovery: bool = False,
        peer_discovery_limit: int = 3,
        peer_discovery_window_seconds: float = 86400.0,
        clock=time.time,
    ):
        self.owner_open_id = owner_open_id or ""
        self.inbox_path = Path(inbox_path)
        self.state_path = Path(state_path)
        self.echo_sender = echo_sender
        self.downloader = downloader
        self.allow_group_mentions = bool(allow_group_mentions)
        self.bot_open_id = bot_open_id or None
        self.peer_bots = {
            str(k): str(v or k) for k, v in (peer_bots or {}).items() if k
        }
        self.max_peer_handovers = max(0, int(max_peer_handovers))
        self.peer_chain_ttl_seconds = float(peer_chain_ttl_seconds)
        self.peer_allow_discovery = bool(peer_allow_discovery)
        self.peer_discovery_limit = max(0, int(peer_discovery_limit))
        self.peer_discovery_window_seconds = float(
            peer_discovery_window_seconds)
        self._clock = clock
        self.last_event_at: str | None = None
        self._lock = threading.Lock()
        self._seen: list[str] = []
        self._peer_chains: dict = {}
        self._peer_discovery: dict = {}
        self._load_state()

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._seen = list(data.get("seen_message_ids", []))
            chains = data.get("peer_chains", {})
            self._peer_chains = chains if isinstance(chains, dict) else {}
            discovery = data.get("peer_discovery", {})
            self._peer_discovery = (
                discovery if isinstance(discovery, dict) else {})
        except (OSError, json.JSONDecodeError, AttributeError):
            self._seen = []
            self._peer_chains = {}
            self._peer_discovery = {}

    def _save_state(self) -> None:
        payload = {
            "seen_message_ids": self._seen[-MAX_SEEN_IDS:],
            "peer_chains": self._peer_chains,
            "peer_discovery": self._peer_discovery,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- peer-bot task chains -------------------------------------------------
    def _open_peer_chain(self, chat_id: str, root_message_id: str) -> None:
        """(Re)open a peer task chain for chat_id, anchored on an
        accepted owner group message. Caller must hold self._lock."""
        if not chat_id or not self.peer_bots:
            return
        self._peer_chains[chat_id] = {
            "root_message_id": root_message_id,
            "started_at": float(self._clock()),
            "handover_count": 0,
        }
        self._save_state()

    def _peer_chain_gate(self, chat_id: str) -> str | None:
        """Consume one handover for chat_id. Caller must hold self._lock.

        Returns None when the peer message may pass, else the ignore
        status string (no active / expired chain, or limit reached)."""
        chain = self._peer_chains.get(chat_id or "")
        if not isinstance(chain, dict):
            log.info("ignored (peer bot message without an owner-started task chain)")
            return "ignored_peer_chain"
        started_at = chain.get("started_at")
        if (
            not isinstance(started_at, (int, float))
            or self._clock() - float(started_at) > self.peer_chain_ttl_seconds
        ):
            log.info("ignored (peer task chain expired for this chat)")
            return "ignored_peer_chain"
        count = chain.get("handover_count")
        count = int(count) if isinstance(count, (int, float)) else 0
        if count >= self.max_peer_handovers:
            log.info(
                "ignored (peer handover limit %s reached for this chat)",
                self.max_peer_handovers,
            )
            return "ignored_peer_limit"
        chain["handover_count"] = count + 1
        self._save_state()
        return None

    # -- peer-bot discovery (mutual introductions) ---------------------------
    DISCOVERY_PREFIXES = (
        "【自我介绍】", "[自我介绍]", "【机器人自我介绍】",
    )

    @classmethod
    def _is_discovery_text(cls, text: object) -> bool:
        stripped = str(text or "").strip()
        return any(stripped.startswith(p) for p in cls.DISCOVERY_PREFIXES)

    def _peer_discovery_gate(self, chat_id: str, sender: str) -> str | None:
        """Consume one discovery slot for (chat_id, sender).
        Caller must hold self._lock.

        Returns None when the introduction may pass, else
        "ignored_peer_discovery_limit"."""
        if not self.peer_allow_discovery or self.peer_discovery_limit <= 0:
            return "ignored_peer_discovery_limit"
        key = f"{chat_id or ''}:{sender or ''}"
        now = float(self._clock())
        stamps = [
            float(t) for t in self._peer_discovery.get(key, [])
            if isinstance(t, (int, float))
            and now - float(t) <= self.peer_discovery_window_seconds
        ]
        if len(stamps) >= self.peer_discovery_limit:
            log.info(
                "ignored (peer discovery limit %s reached for this chat)",
                self.peer_discovery_limit,
            )
            self._peer_discovery[key] = stamps
            return "ignored_peer_discovery_limit"
        stamps.append(now)
        self._peer_discovery[key] = stamps
        self._save_state()
        return None

    def _group_mention_allowed(self, record: dict) -> bool:
        mentions = [str(v) for v in record.get("mention_open_ids", []) if v]
        if self.bot_open_id:
            return self.bot_open_id in mentions
        # Degraded mode is deliberately visible in logs: without the
        # bot's own id we can only require that *someone* was mentioned.
        if mentions:
            log.warning(
                "group mention fallback: bot open_id unavailable; "
                "accepting owner group message with non-empty mentions"
            )
            return True
        return False

    def process(self, record: dict) -> str:
        """Apply allowlist + dedupe. Returns a status string:
        accepted | accepted_peer | duplicate | ignored_sender |
        ignored_chat_type | ignored_group_mention | ignored_msg_type |
        ignored_peer_scope | ignored_peer_type | ignored_peer_chain |
        ignored_peer_limit | ignored_peer_discovery_limit.
        """
        message_id = record.get("message_id", "")
        sender = record.get("sender_open_id", "")
        is_owner = bool(self.owner_open_id) and sender == self.owner_open_id
        is_peer = sender in self.peer_bots
        peer_discovery = False

        with self._lock:
            if message_id and message_id in self._seen:
                log.info("duplicate dropped: %s", message_id)
                return "duplicate"
            if message_id:
                self._seen.append(message_id)
                self._save_state()

        chat_type = record.get("chat_type")
        if chat_type == "group":
            if not self.allow_group_mentions:
                log.info(
                    "ignored (chat_type=group, group mentions disabled) from %s",
                    common.mask(sender),
                )
                return "ignored_chat_type"
            if not self._group_mention_allowed(record):
                log.info("ignored (group message did not mention this bot)")
                return "ignored_group_mention"
        elif chat_type != "p2p":
            log.info(
                "ignored (chat_type=%s) from %s",
                chat_type, common.mask(sender),
            )
            return "ignored_chat_type"

        kind = str(record.get("kind") or record.get("message_type") or "")
        if kind not in _SUPPORTED_KINDS:
            log.info("ignored (message_type=%s)", record.get("message_type"))
            return "ignored_msg_type"

        if is_peer and not is_owner:
            # Restricted collaboration channel: group-only, app/bot
            # senders only, and only inside an owner-started task chain whose
            # handover budget is not exhausted. Peer messages are data /
            # collaboration requests — never owner instructions.
            if chat_type != "group":
                log.info("ignored (peer bot message outside a group chat)")
                return "ignored_peer_scope"
            sender_type = str(record.get("sender_type") or "")
            if sender_type and sender_type not in ("app", "bot"):
                log.info(
                    "ignored (peer bot id with sender_type=%s, "
                    "not app/bot)",
                    sender_type,
                )
                return "ignored_peer_type"
            with self._lock:
                gate = self._peer_chain_gate(record.get("chat_id", ""))
            if gate is not None:
                # Discovery exception: a "【自我介绍】" text may pass
                # without a chain so allowlisted peers can find and
                # introduce themselves to each other. It consumes a
                # rate-limited discovery slot instead of a handover,
                # and the entry stays collaboration_only.
                if kind == "text" and self._is_discovery_text(
                        record.get("text", "")):
                    if not self.peer_allow_discovery:
                        return gate
                    with self._lock:
                        dgate = self._peer_discovery_gate(
                            record.get("chat_id", ""), sender)
                    if dgate is None:
                        peer_discovery = True
                    else:
                        return dgate
                else:
                    return gate
        elif not is_owner:
            log.info(
                "ignored (sender %s not in allowlist)", common.mask(sender)
            )
            return "ignored_sender"

        if is_owner and chat_type == "group":
            # An accepted owner group message opens / refreshes the
            # peer task chain for this chat (done only after the kind
            # check so unsupported owner messages do not open chains).
            with self._lock:
                self._open_peer_chain(record.get("chat_id", ""), message_id)

        media_paths: list[str] = []
        media_errors: list[str] = []
        for item in record.get("media_items", []):
            file_key = str(item.get("file_key", "") or "")
            item_kind = str(item.get("kind", "") or "")
            if not file_key or not item_kind:
                continue
            if self.downloader is None:
                media_errors.append(
                    f"downloader not configured for {item_kind} resource {file_key}"
                )
                continue
            try:
                downloaded = self.downloader(message_id, file_key, item_kind)
                if downloaded:
                    media_paths.append(str(downloaded))
                else:
                    media_errors.append(
                        f"download returned no path for {item_kind} resource {file_key}"
                    )
            except Exception as exc:  # never drop the message for media failure
                media_errors.append(
                    f"download failed for {item_kind} resource {file_key}: {exc}"
                )

        entry = {
            "ts": utc_now_iso(),
            "message_id": message_id,
            "chat_id": record.get("chat_id", ""),
            "sender_open_id": sender,
            "text": record.get("text", ""),
            "kind": kind,
            "media_paths": media_paths,
            "file_name": record.get("file_name", "") or None,
        }
        if is_peer and not is_owner:
            entry["sender_role"] = "peer_bot"
            entry["peer_bot_name"] = self.peer_bots.get(sender, "")
            entry["authority"] = "collaboration_only"
            if peer_discovery:
                entry["peer_discovery"] = True
        if media_errors:
            entry["media_error"] = "; ".join(media_errors)
        with self._lock:
            self.last_event_at = entry["ts"]
            with self.inbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if is_peer and not is_owner:
            log.info(
                "accepted peer bot message %s (%s) from %s -> inbox "
                "(collaboration only, not an owner instruction)",
                message_id, kind, common.mask(sender),
            )
            # No echo for peer messages: echoing a bot risks a bot<->bot
            # ping-pong loop even with the handover budget.
            return "accepted_peer"
        log.info("accepted message %s (%s) -> inbox", message_id, kind)

        if self.echo_sender is not None:
            try:
                if kind == "text":
                    echo_text = f"已收到：{entry['text']}"
                else:
                    echo_text = f"已收到：[{_ECHO_LABELS[kind]}]"
                self.echo_sender(entry["chat_id"], echo_text)
            except Exception as exc:  # echo is best-effort for integration tests
                log.warning("echo failed: %s", exc)
        return "accepted"


# ---------------------------------------------------------------------------
# Instant acknowledgement ("收到，正在处理") with per-chat throttling
# ---------------------------------------------------------------------------
ACK_TEXT = "收到，正在处理，稍等。"
ACK_INTERVAL_SECONDS = 60.0


def ack_text_for(cfg: dict) -> str:
    """Instant-ack text; uses the instance's own agent name when the
    user configured one (config key ``agent_name``), else a neutral
    text. The name is per-instance configuration — never hardcode a
    particular assistant's name into the bridge."""
    name = str(cfg.get("agent_name") or "").strip()
    if name:
        return f"收到，{name} 正在处理，稍等。"
    return ACK_TEXT


class AckThrottler:
    """Decide whether an instant-ack may be sent to a chat right now.

    At most one ack per chat per interval. The send itself runs on a
    daemon thread so event handling never blocks on the network; send
    failures are logged and swallowed (the ack is best-effort).
    """

    def __init__(
        self,
        sender,  # callable(chat_id, text) -> object
        interval: float = ACK_INTERVAL_SECONDS,
        clock=time.monotonic,
    ):
        self._sender = sender
        self._interval = float(interval)
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_send(self, chat_id: str) -> bool:
        """Claim the ack slot for chat_id; True when an ack may go out."""
        if not chat_id:
            return False
        now = self._clock()
        with self._lock:
            last = self._last_sent.get(chat_id)
            if last is not None and now - last < self._interval:
                return False
            self._last_sent[chat_id] = now
            return True

    def maybe_send(self, chat_id: str, text: str = ACK_TEXT) -> bool:
        """Send the ack asynchronously if the throttle allows it."""
        if not self.should_send(chat_id):
            return False

        def _send() -> None:
            try:
                self._sender(chat_id, text)
            except Exception:
                log.warning("ack send failed for chat %s", chat_id, exc_info=True)

        threading.Thread(target=_send, name="feishu-ack", daemon=True).start()
        return True


# ---------------------------------------------------------------------------
# Approval card actions (card.action.trigger)
# ---------------------------------------------------------------------------
_inbox_append_lock = threading.Lock()


def apply_card_action(
    operator_open_id: str,
    value: dict | None,
    context_chat_id: str,
    *,
    owner_open_id: str,
    approvals_path: Path = approvals.APPROVALS_PATH,
    inbox_path: Path = common.INBOX_PATH,
) -> tuple[str, dict | None, dict | None]:
    """Apply one approval-card tap. Transport-independent core.

    Returns (toast_text, decision_card_or_None, appended_record_or_None).

    Only the config owner may decide. A first valid decision is stored
    (approvals.decide_approval), mirrored into inbox.jsonl as a
    kind="decision" record so the normal hook pipeline wakes Muse with
    it, and answered with the decided-state card. Repeated taps are
    idempotent: no second inbox record, original decision card shown.
    """
    if not owner_open_id or operator_open_id != owner_open_id:
        log.warning(
            "card action from non-owner %s rejected",
            common.mask(operator_open_id),
        )
        return "仅机主可操作", None, None

    value = value if isinstance(value, dict) else {}
    approval_id = str(value.get("approval_id", "") or "")
    decision = str(value.get("decision", "") or "")
    if not approval_id or decision not in approvals.DECISIONS:
        return "申请不存在或已失效", None, None

    entry = approvals.get_approval(approval_id, path=approvals_path)
    if entry is None:
        return "申请不存在或已失效", None, None

    entry, changed = approvals.decide_approval(
        approval_id, decision, operator_open_id, path=approvals_path
    )
    decision_label = "批准" if entry["status"] == "approved" else "拒绝"
    record = None
    if changed:
        record = {
            "ts": utc_now_iso(),
            "message_id": f"decision_{approval_id}",
            "chat_id": entry.get("chat_id") or context_chat_id or "",
            "sender_open_id": operator_open_id,
            "text": f"【授权决定】{entry['title']}：已{decision_label}",
            "kind": "decision",
            "media_paths": [],
            "file_name": None,
            "approval_id": approval_id,
            "decision": entry["status"],
        }
        with _inbox_append_lock:
            with Path(inbox_path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(
            "approval %s decided: %s by owner", approval_id, entry["status"]
        )
    else:
        log.info(
            "approval %s already decided (%s); tap ignored",
            approval_id, entry["status"],
        )
    toast = f"已记录：{decision_label}"
    return toast, common.build_decision_card(entry), record


def build_card_trigger_response(toast_text: str, card_dict: dict | None):
    """Build a P2CardActionTriggerResponse for the SDK card handler.

    The SDK models (lark_oapi.event.callback.model.p2_card_action_trigger)
    carry ``toast`` (CallBackToast: type/content) and ``card``
    (CallBackCard: type/data); card updates use type="raw" with data
    being the full replacement card JSON — verified by marshaling a
    sample response through lark_oapi.core.json.JSON in the self-test.
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    response = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "info"
    toast.content = toast_text
    response.toast = toast
    if card_dict is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = card_dict
        response.card = card
    return response


# ---------------------------------------------------------------------------
# WS service
# ---------------------------------------------------------------------------
def write_status(
    state: str,
    cfg: dict,
    echo_test: bool,
    last_event_at: str | None = None,
) -> None:
    payload = {
        "state": state,
        "pid": os.getpid(),
        "updated_at": utc_now_iso(),
        "app_id": common.mask(cfg.get("app_id")),
        "domain": cfg.get("domain", "feishu"),
        "echo_test": echo_test,
        "last_event_at": last_event_at,
    }
    common.STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def enable_env_proxy_for_ws() -> None:
    """Route the Lark WebSocket through this sandbox's egress proxy.

    Sandbox quirk (found in integration, 2026-10-06): direct outbound
    TCP is intercepted here, so a direct wss:// connection dies with
    ``SSL: WRONG_VERSION_NUMBER``. lark-oapi's ws client deliberately
    passes ``proxy=None`` to websockets (preserving its historical
    direct-connect behaviour), while websockets >= 15 would otherwise
    discover the proxy from the environment (HTTPS_PROXY — a CONNECT
    proxy that also handles the sandbox's TLS interception). Restore
    environment proxy discovery for this process only.
    """
    if not (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    ):
        return
    try:
        import lark_oapi.ws.client as ws_client_module

        ws_client_module._ws_connect_kwargs = lambda: {"proxy": True}
        log.info("websocket proxy: using environment proxy discovery")
    except Exception:
        log.exception("failed to enable websocket env proxy")


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu/Lark inbox bridge")
    parser.add_argument(
        "--echo-test",
        action="store_true",
        default=os.environ.get("FEISHU_ECHO_TEST") == "1",
        help="reply '已收到：<text>' to accepted messages (integration only)",
    )
    args = parser.parse_args()

    cfg = common.load_config()
    if cfg is None:
        print(
            "[bridge] Not onboarded: config.json is missing or incomplete.\n"
            "[bridge] Run `./feishu.sh onboard` first (QR scan), or "
            "`./feishu.sh onboard --manual` with FEISHU_APP_ID/"
            "FEISHU_APP_SECRET set.",
            file=sys.stderr,
        )
        return 2

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401  (typed handler)

    echo_sender = None
    if args.echo_test:
        def echo_sender(chat_id: str, text: str) -> str:  # noqa: F811
            return common.send_text(cfg, "chat_id", chat_id, text)

    allow_group_mentions = cfg.get("allow_group_mentions") is True
    bot_open_id = None
    if allow_group_mentions:
        bot_open_id = common.get_bot_open_id(cfg)
        if bot_open_id:
            log.info("group mentions enabled for bot %s", common.mask(bot_open_id))
        else:
            log.warning(
                "group mentions enabled, but bot open_id could not be "
                "fetched; degraded rule will accept owner group messages "
                "with any non-empty mentions list"
            )

    def downloader(message_id: str, file_key: str, kind: str) -> Path:
        return common.download_message_resource(cfg, message_id, file_key, kind)

    # Peer bots: restricted collaboration allowlist, config shape is a
    # list of {"open_id": ..., "name": ...} (a bare open_id string or a
    # {open_id: name} mapping is also accepted). Owner remains the only
    # principal; peers are never instructions or authorizations.
    peer_bots: dict = {}
    raw_peers = cfg.get("peer_bots") or []
    if isinstance(raw_peers, dict):
        peer_bots = {str(k): str(v or k) for k, v in raw_peers.items() if k}
    elif isinstance(raw_peers, list):
        for item in raw_peers:
            if isinstance(item, dict) and item.get("open_id"):
                peer_bots[str(item["open_id"])] = str(
                    item.get("name") or item["open_id"])
            elif isinstance(item, str) and item:
                peer_bots[item] = item
    try:
        max_peer_handovers = int(cfg.get("peer_max_handovers") or 6)
    except (TypeError, ValueError):
        max_peer_handovers = 6
    try:
        peer_chain_ttl = float(cfg.get("peer_chain_ttl_seconds") or 86400)
    except (TypeError, ValueError):
        peer_chain_ttl = 86400.0
    peer_allow_discovery = cfg.get("peer_allow_discovery") is True
    try:
        peer_discovery_limit = int(cfg.get("peer_discovery_limit") or 3)
    except (TypeError, ValueError):
        peer_discovery_limit = 3
    try:
        peer_discovery_window = float(
            cfg.get("peer_discovery_window_seconds") or 86400)
    except (TypeError, ValueError):
        peer_discovery_window = 86400.0
    if peer_bots:
        log.info(
            "peer bots enabled (collaboration only): %s",
            ", ".join(
                f"{name}({common.mask(oid)})"
                for oid, name in peer_bots.items()
            ),
        )
        if peer_allow_discovery:
            log.info(
                "peer discovery enabled: allowlisted peers may send "
                "【自我介绍】 introductions without an owner-started "
                "chain (limit %s per chat per %ss)",
                peer_discovery_limit, int(peer_discovery_window),
            )

    processor = MessageProcessor(
        owner_open_id=cfg.get("owner_open_id", ""),
        echo_sender=echo_sender,
        downloader=downloader,
        allow_group_mentions=allow_group_mentions,
        bot_open_id=bot_open_id,
        peer_bots=peer_bots,
        max_peer_handovers=max_peer_handovers,
        peer_chain_ttl_seconds=peer_chain_ttl,
        peer_allow_discovery=peer_allow_discovery,
        peer_discovery_limit=peer_discovery_limit,
        peer_discovery_window_seconds=peer_discovery_window,
    )
    if not cfg.get("owner_open_id"):
        log.warning(
            "owner_open_id is empty in config.json: allowlist accepts "
            "nobody; watch the log for your sender open_id and fill it in."
        )

    ack_enabled = cfg.get("ack_on_receive") is not False
    ack_throttler = None
    if ack_enabled and not args.echo_test:
        def ack_sender(chat_id: str, text: str) -> str:
            return common.send_text(cfg, "chat_id", chat_id, text)

        ack_throttler = AckThrottler(ack_sender)
        log.info("instant ack on receive: enabled (60s per-chat throttle)")
    elif args.echo_test:
        log.info("instant ack on receive: disabled in echo-test mode")
    else:
        log.info("instant ack on receive: disabled by config ack_on_receive")

    def on_message(data) -> None:
        try:
            record = extract_record(data)
            status = processor.process(record)
            if status == "accepted" and ack_throttler is not None:
                ack_throttler.maybe_send(
                    record.get("chat_id", ""), text=ack_text_for(cfg))
        except Exception:
            log.exception("failed to process incoming event")

    def on_card_action(data):
        try:
            event = getattr(data, "event", None)
            operator = getattr(event, "operator", None)
            operator_open_id = getattr(operator, "open_id", "") or ""
            action = getattr(event, "action", None)
            value = getattr(action, "value", None)
            context = getattr(event, "context", None)
            context_chat_id = getattr(context, "open_chat_id", "") or ""
            toast_text, card_dict, record = apply_card_action(
                operator_open_id,
                value if isinstance(value, dict) else {},
                context_chat_id,
                owner_open_id=cfg.get("owner_open_id", ""),
            )
            if record is not None:
                # Surface the decision in the bridge heartbeat too.
                processor.last_event_at = record["ts"]
        except Exception:
            log.exception("card action handling failed")
            toast_text, card_dict = "处理失败，请稍后再试", None
        try:
            return build_card_trigger_response(toast_text, card_dict)
        except Exception:
            log.exception("failed to build card action response")
            return build_card_trigger_response(toast_text, None)

    # WS mode: verification token / encrypt key are empty strings.
    enable_env_proxy_for_ws()
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        cfg["app_id"],
        cfg["app_secret"],
        event_handler=handler,
        domain=common.domain_url(cfg.get("domain", "feishu")),
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    def _run_ws() -> None:
        try:
            ws_client.start()
        except Exception:
            log.exception("websocket client stopped with an error")
            stop_event.set()

    write_status("running", cfg, args.echo_test, processor.last_event_at)
    log.info(
        "bridge starting (app %s, domain %s, echo_test=%s)",
        common.mask(cfg["app_id"]), cfg.get("domain", "feishu"), args.echo_test,
    )
    ws_thread = threading.Thread(target=_run_ws, name="feishu-ws", daemon=True)
    ws_thread.start()
    last_heartbeat = time.monotonic()
    try:
        while not stop_event.wait(timeout=1.0):
            if not ws_thread.is_alive():
                log.error("websocket thread exited unexpectedly")
                break
            if time.monotonic() - last_heartbeat >= 30.0:
                write_status(
                    "running", cfg, args.echo_test, processor.last_event_at
                )
                last_heartbeat = time.monotonic()
    finally:
        try:
            stop = getattr(ws_client, "stop", None)
            if callable(stop):
                stop()
        except Exception:
            pass
        write_status("stopped", cfg, args.echo_test, processor.last_event_at)
        log.info("bridge stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
