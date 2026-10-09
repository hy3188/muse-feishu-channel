"""Shared helpers for the Feishu/Lark bridge prototype.

Credentials live ONLY in config.json next to this file (chmod 600).
Nothing in this module ever prints app_secret.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
INBOX_PATH = BASE_DIR / "inbox.jsonl"
STATE_PATH = BASE_DIR / "state.json"
STATUS_PATH = BASE_DIR / "status.json"
MEDIA_DIR = BASE_DIR / "media"

DOMAIN_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}

# Sandbox egress: processes started by systemd (or any context without the
# interactive shell environment) need the egress proxy + CA bundle to reach
# Feishu at all. egress.env next to this file (chmod 600, git-ignored)
# carries those variables; values already present in the real environment
# always win, and only the allow-listed keys below are ever loaded.
_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


def load_local_env() -> None:
    env_file = BASE_DIR / "egress.env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _ENV_KEYS and key not in os.environ:
            os.environ[key] = value.strip()


load_local_env()


def mask(value: str | None, head: int = 6, tail: int = 2) -> str:
    """Mask an identifier for display: cli_a1***yz style."""
    if not value:
        return "(empty)"
    if len(value) <= head + tail:
        return value[:2] + "***"
    return f"{value[:head]}***{value[-tail:]}"


def load_config() -> dict | None:
    """Return config dict, or None when not onboarded / incomplete."""
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        return None
    cfg.setdefault("domain", "feishu")
    cfg.setdefault("owner_open_id", "")
    return cfg


def save_config(cfg: dict) -> Path:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(CONFIG_PATH, 0o600)
    return CONFIG_PATH


def domain_url(domain: str) -> str:
    return DOMAIN_URLS.get(domain, DOMAIN_URLS["feishu"])


def build_client(cfg: dict):
    """Build a lark_oapi REST client for the configured app/domain."""
    import lark_oapi as lark

    return (
        lark.Client.builder()
        .app_id(cfg["app_id"])
        .app_secret(cfg["app_secret"])
        .domain(domain_url(cfg.get("domain", "feishu")))
        .build()
    )


def get_tenant_access_token(cfg: dict) -> str:
    """Fetch a tenant access token for endpoints not wrapped by the SDK.

    The token is returned to the caller only and is never logged here.
    """
    import requests

    url = (
        domain_url(cfg.get("domain", "feishu"))
        + "/open-apis/auth/v3/tenant_access_token/internal"
    )
    response = requests.post(
        url,
        json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("tenant_access_token")
    if payload.get("code") != 0 or not token:
        raise RuntimeError(
            "tenant token request failed: "
            f"code={payload.get('code')} msg={payload.get('msg')}"
        )
    return str(token)


def get_bot_open_id(cfg: dict) -> str | None:
    """Return this app's bot open_id from /open-apis/bot/v3/info.

    Returns None instead of raising for API-level failures so the bridge
    can apply its explicitly logged degraded group-mention rule. Network
    exceptions are also converted to None by the caller-facing contract.
    """
    import requests

    try:
        token = get_tenant_access_token(cfg)
        response = requests.get(
            domain_url(cfg.get("domain", "feishu")) + "/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    if payload.get("code") not in (None, 0):
        return None
    bot = payload.get("bot")
    if not isinstance(bot, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            bot = data.get("bot") if isinstance(data.get("bot"), dict) else data
    if isinstance(bot, dict):
        open_id = bot.get("open_id")
        return str(open_id) if open_id else None
    return None


def download_message_resource(
    cfg: dict,
    message_id: str,
    file_key: str,
    kind: str,
) -> Path:
    """Download one image/file resource from a received message.

    Files are stored under media/ as <message_id>_<sequence>.<ext>.
    The sequence is derived from files already present for that message,
    making repeated calls for post messages collision-free.
    """
    if kind not in {"image", "file"}:
        raise ValueError(f"unsupported message resource kind: {kind}")
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    client = build_client(cfg)
    request = (
        GetMessageResourceRequest.builder()
        .type(kind)
        .message_id(message_id)
        .file_key(file_key)
        .build()
    )
    response = client.im.v1.message_resource.get(request)
    if not response.success():
        raise RuntimeError(
            "message resource download failed: "
            f"code={response.code} msg={response.msg} "
            f"log_id={response.get_log_id()}"
        )
    stream = getattr(response, "file", None)
    if stream is None:
        raise RuntimeError("message resource download returned no file stream")
    data = stream.read() if hasattr(stream, "read") else stream
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise RuntimeError("message resource download returned non-binary data")

    response_name = str(getattr(response, "file_name", "") or "")
    suffix = Path(response_name).suffix
    if not suffix:
        suffix = ".jpg" if kind == "image" else ".bin"

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", message_id)
    sequence = 1
    for existing in MEDIA_DIR.glob(f"{safe_id}_*"):
        match = re.match(rf"{re.escape(safe_id)}_(\d+)(?:\.|$)", existing.name)
        if match:
            sequence = max(sequence, int(match.group(1)) + 1)
    while True:
        target = MEDIA_DIR / f"{safe_id}_{sequence}{suffix}"
        if not target.exists():
            break
        sequence += 1
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(bytes(data))
    tmp.replace(target)
    return target


def chunk_text(text: str, limit: int = 3500) -> list[str]:
    """Split text into <=limit chunks without dropping any characters.

    Newline-delimited lines/paragraphs are packed greedily. A single line
    longer than the limit is hard-split; its final partial piece can be
    packed with following lines. Concatenating the result reproduces the
    input exactly.
    """
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
    if text == "":
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            pieces = [line[i:i + limit] for i in range(0, len(line), limit)]
            chunks.extend(pieces[:-1])
            current = pieces[-1]
            if len(current) == limit:
                chunks.append(current)
                current = ""
            continue
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
        if len(current) == limit:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def build_markdown_card_payload(markdown_text: str) -> dict:
    """Build the Feishu interactive-card payload for one markdown chunk."""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": markdown_text}],
    }


def _create_message(
    cfg: dict,
    receive_id_type: str,
    receive_id: str,
    msg_type: str,
    content: str,
) -> str:
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
    )

    client = build_client(cfg)
    request = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    if not response.success():
        raise RuntimeError(
            f"send failed: code={response.code} msg={response.msg} "
            f"log_id={response.get_log_id()}"
        )
    return response.data.message_id


def send_text(cfg: dict, receive_id_type: str, receive_id: str, text: str) -> str:
    """Send one text message via im.v1.message.create; return message_id."""
    return _create_message(
        cfg,
        receive_id_type,
        receive_id,
        "text",
        json.dumps({"text": text}, ensure_ascii=False),
    )


def send_interactive(
    cfg: dict,
    receive_id_type: str,
    receive_id: str,
    card: dict,
) -> str:
    """Send one interactive card message; return message_id."""
    return _create_message(
        cfg,
        receive_id_type,
        receive_id,
        "interactive",
        json.dumps(card, ensure_ascii=False),
    )


def send_markdown_card(
    cfg: dict,
    receive_id_type: str,
    receive_id: str,
    markdown_text: str,
) -> str:
    """Send one markdown interactive card; return message_id."""
    return send_interactive(
        cfg,
        receive_id_type,
        receive_id,
        build_markdown_card_payload(markdown_text),
    )


# ---------------------------------------------------------------------------
# Approval (authorization) cards
#
# An approval request is stored locally (see approvals.py) and sent as an
# interactive card with two buttons. The button values carry the
# approval_id and the decision; bridge.py receives the tap through the
# card.action.trigger callback, validates that the operator is the
# config owner, records the decision, and swaps the card for the
# decided-state card built by build_decision_card.
# ---------------------------------------------------------------------------
def build_approval_card(entry: dict) -> dict:
    """Build the pending-state approval card for a stored entry."""
    approval_id = entry.get("id", "")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"待授权：{entry.get('title', '')}",
            },
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": str(entry.get("detail", "") or "")},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "type": "primary",
                        "value": {
                            "approval_id": approval_id,
                            "decision": "approved",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "approval_id": approval_id,
                            "decision": "rejected",
                        },
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"申请时间：{entry.get('created_at', '')}"
                            " · 仅机主本人点击有效"
                        ),
                    }
                ],
            },
        ],
    }


def build_decision_card(entry: dict) -> dict:
    """Build the decided-state card (no buttons) for a stored entry."""
    approved = entry.get("status") == "approved"
    label = "已批准" if approved else "已拒绝"
    decision_text = "批准" if approved else "拒绝"
    detail = str(entry.get("detail", "") or "")
    markdown_lines = []
    if detail:
        markdown_lines.append(detail)
        markdown_lines.append("")
    markdown_lines.append(f"**决定：{decision_text}**")
    markdown_lines.append(f"决定时间：{entry.get('decided_at') or ''}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"{label}：{entry.get('title', '')}",
            },
            "template": "green" if approved else "red",
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(markdown_lines)},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "此申请已处理，按钮已失效。"}
                ],
            },
        ],
    }
