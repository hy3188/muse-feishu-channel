#!/usr/bin/env python3
"""Send a Feishu/Lark message from the command line.

Usage:
    send.py --chat-id oc_xxx --text "hello"
    send.py --to-open-id ou_xxx --text "# 标题" --format auto
    send.py --chat-id oc_xxx --approval-title "发布文章" \
        --approval-detail "把《…》发布到公众号"

`--format auto` (default) uses an interactive markdown card when the
text looks like markdown or is longer than 1200 characters; otherwise
it sends a plain text message. Text and card content are chunked at
3500 characters and sent sequentially.

Approval mode stores a pending request in approvals.json and sends an
interactive card with 批准/拒绝 buttons; the bridge records the owner's
tap as a formal decision. Approval mode is mutually exclusive with
--text. It prints the approval id and the card's message_id.

Uses the app credentials from config.json (written by onboard.py).
Prints the last created message_id on success and, for multi-part
sends, a second line stating the total part count. Never prints the
app secret.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import approvals  # noqa: E402
import common  # noqa: E402

AUTO_CARD_LENGTH = 1200
_MARKDOWN_PATTERNS = (
    re.compile(r"```"),
    re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE),
    re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE),
    re.compile(r"\*\*[^*]+\*\*"),
    re.compile(r"^\s*[-*+]\s+\S", re.MULTILINE),
    re.compile(r"\[[^\]]+\]\([^)]+\)"),
)


def looks_like_markdown(text: str) -> bool:
    """Conservative markdown detection used by --format auto."""
    return any(pattern.search(text) for pattern in _MARKDOWN_PATTERNS)


def choose_format(text: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if len(text) > AUTO_CARD_LENGTH or looks_like_markdown(text):
        return "card"
    return "text"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Feishu/Lark message")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", help="target chat_id (oc_...)")
    target.add_argument("--to-open-id", help="target user open_id (ou_...)")
    parser.add_argument("--text", help="message text (text mode)")
    parser.add_argument(
        "--format",
        choices=["auto", "text", "card"],
        default="auto",
        help="send format; auto picks card for markdown or >1200 chars",
    )
    parser.add_argument(
        "--approval-title",
        help="approval mode: short title of the request (needs --approval-detail)",
    )
    parser.add_argument(
        "--approval-detail",
        help="approval mode: markdown detail shown on the approval card",
    )
    args = parser.parse_args()

    approval_mode = bool(args.approval_title or args.approval_detail)
    if approval_mode:
        if not args.approval_title or args.approval_detail is None:
            print(
                "[send] approval mode needs both --approval-title and "
                "--approval-detail",
                file=sys.stderr,
            )
            return 2
        if args.text is not None:
            print(
                "[send] --text and --approval-* are mutually exclusive",
                file=sys.stderr,
            )
            return 2
    elif args.text is None:
        print(
            "[send] either --text or --approval-title/--approval-detail "
            "is required",
            file=sys.stderr,
        )
        return 2

    cfg = common.load_config()
    if cfg is None:
        print(
            "[send] Not onboarded: config.json is missing or incomplete. "
            "Run `./feishu.sh onboard` first.",
            file=sys.stderr,
        )
        return 2

    if args.chat_id:
        receive_id_type, receive_id = "chat_id", args.chat_id
    else:
        receive_id_type, receive_id = "open_id", args.to_open_id

    if approval_mode:
        entry = approvals.create_approval(
            title=args.approval_title,
            detail=args.approval_detail,
            chat_id=args.chat_id or "",
            receive_id_type=receive_id_type,
            receive_id=receive_id,
        )
        try:
            message_id = common.send_interactive(
                cfg,
                receive_id_type,
                receive_id,
                common.build_approval_card(entry),
            )
        except RuntimeError as exc:
            print(
                f"[send] {exc} (approval {entry['id']} stays pending "
                "in approvals.json)",
                file=sys.stderr,
            )
            return 1
        print(f"approval_id: {entry['id']}")
        print(message_id)
        return 0

    selected = choose_format(args.text, args.format)
    chunks = common.chunk_text(args.text, limit=3500)
    if not chunks:
        print("[send] refusing to send an empty message", file=sys.stderr)
        return 2

    sender = common.send_markdown_card if selected == "card" else common.send_text
    last_message_id = ""
    try:
        for chunk in chunks:
            last_message_id = sender(cfg, receive_id_type, receive_id, chunk)
    except RuntimeError as exc:
        print(f"[send] {exc}", file=sys.stderr)
        return 1
    print(last_message_id)
    if len(chunks) > 1:
        print(f"共发送 {len(chunks)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
