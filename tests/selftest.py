#!/usr/bin/env python3
"""Offline self-test for the bridge message pipeline.

Feeds sample im.message.receive_v1 events through
bridge.extract_record + MessageProcessor and asserts allowlist
filtering, dedupe, media extraction/download bookkeeping, group
mention gating, inbox JSONL writing and echo mode. It also checks
text chunking and markdown-card payload construction in common.py.

No network and no real credentials are used. Media downloads are
replaced by a fake downloader. Run with the project venv:
    .venv/bin/python tests/selftest.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import bridge  # noqa: E402
import common  # noqa: E402
import send as send_module  # noqa: E402

OWNER = "ou_owner123"
OTHER = "ou_intruder999"
BOT = "ou_bot456"

FAILURES = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def sample_event(
    message_id,
    sender=OWNER,
    chat_type="p2p",
    message_type="text",
    text="你好 Muse",
    content=None,
    mentions=None,
    sender_type="user",
) -> dict:
    if content is None:
        if message_type == "text":
            content = {"text": text}
        elif message_type == "image":
            content = {"image_key": "img_demo"}
        elif message_type == "file":
            content = {"file_key": "file_demo", "file_name": "报告.pdf"}
        elif message_type == "post":
            content = {
                "title": "季度报告",
                "content": [
                    [
                        {"tag": "text", "text": "第一段 "},
                        {"tag": "a", "text": "链接", "href": "https://example.invalid"},
                        {"tag": "at", "user_id": "@_user_1", "user_name": "张三"},
                    ],
                    [
                        {"tag": "text", "text": "第二段"},
                        {"tag": "img", "image_key": "img_post_1"},
                    ],
                    [{"tag": "img", "image_key": "img_post_2"}],
                ],
            }
        else:
            content = {"text": text}
    message = {
        "message_id": message_id,
        "create_time": "1700000000000",
        "chat_id": "oc_chat_demo",
        "chat_type": chat_type,
        "message_type": message_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "schema": "2.0",
        "header": {"event_id": f"ev-{message_id}",
                   "event_type": "im.message.receive_v1",
                   "tenant_key": "tenant-demo"},
        "event": {
            "sender": {
                "sender_id": {"open_id": sender, "user_id": "u-demo",
                              "union_id": "on_demo"},
                "sender_type": sender_type,
                "tenant_key": "tenant-demo",
            },
            "message": message,
        },
    }


def mention(open_id: str) -> list[dict]:
    return [{
        "key": "@_user_1",
        "id": {"open_id": open_id, "user_id": "u-demo", "union_id": "on_demo"},
        "mentioned_type": "bot" if open_id == BOT else "user",
        "name": "机器人" if open_id == BOT else "其他人",
        "tenant_key": "tenant-demo",
    }]


def read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="feishu-selftest-"))
    inbox = tmp / "inbox.jsonl"
    state = tmp / "state.json"
    echoes: list[tuple[str, str]] = []

    proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=inbox,
        state_path=state,
        echo_sender=lambda chat_id, text: echoes.append((chat_id, text)),
    )

    # 1) Owner p2p text (raw dict) -> accepted, echo fired
    r1 = proc.process(bridge.extract_record(sample_event("om_1")))
    check("owner p2p text accepted", r1 == "accepted", f"got {r1}")
    check("echo fired with prefix",
          echoes == [("oc_chat_demo", "已收到：你好 Muse")], f"echoes={echoes}")

    # 2) Same message again -> duplicate, inbox unchanged
    r2 = proc.process(bridge.extract_record(sample_event("om_1")))
    check("duplicate dropped", r2 == "duplicate", f"got {r2}")

    # 3) Typed SDK object path parses identically (different message id)
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.core.json import JSON as LarkJSON

    typed = LarkJSON.unmarshal(
        json.dumps(sample_event("om_2", text="第二条")), P2ImMessageReceiveV1
    )
    r3 = proc.process(bridge.extract_record(typed))
    check("typed SDK event accepted", r3 == "accepted", f"got {r3}")

    # 4) Non-owner sender -> ignored
    r4 = proc.process(bridge.extract_record(sample_event("om_3", sender=OTHER)))
    check("non-owner ignored", r4 == "ignored_sender", f"got {r4}")

    # 5) Group chat from owner -> ignored by default
    r5 = proc.process(bridge.extract_record(
        sample_event("om_4", chat_type="group")))
    check("group chat ignored by default", r5 == "ignored_chat_type", f"got {r5}")

    # 6) Unsupported type from owner -> ignored
    r6 = proc.process(bridge.extract_record(
        sample_event("om_5", message_type="audio", text="")))
    check("unsupported type ignored", r6 == "ignored_msg_type", f"got {r6}")

    # 7) Inbox contents: exactly the two accepted messages, correct fields
    lines = inbox.read_text(encoding="utf-8").strip().splitlines()
    check("inbox has exactly 2 lines", len(lines) == 2, f"lines={len(lines)}")
    first = json.loads(lines[0])
    expected_keys = {
        "ts", "message_id", "chat_id", "sender_open_id", "text",
        "kind", "media_paths", "file_name",
    }
    check("inbox record keys", set(first.keys()) == expected_keys,
          f"keys={sorted(first.keys())}")
    check("inbox record values",
          first["message_id"] == "om_1"
          and first["chat_id"] == "oc_chat_demo"
          and first["sender_open_id"] == OWNER
          and first["text"] == "你好 Muse"
          and first["kind"] == "text"
          and first["media_paths"] == []
          and first["file_name"] is None,
          json.dumps(first, ensure_ascii=False))
    check("last_event_at recorded", bool(proc.last_event_at))

    # 8) State file persists seen ids; new processor instance still dedupes
    proc2 = bridge.MessageProcessor(
        owner_open_id=OWNER, inbox_path=inbox, state_path=state)
    r8 = proc2.process(bridge.extract_record(sample_event("om_2")))
    check("dedupe survives restart", r8 == "duplicate", f"got {r8}")

    # 9) Media pipeline with a fake downloader (no network)
    media_tmp = tmp / "media-case"
    media_tmp.mkdir()
    media_inbox = media_tmp / "inbox.jsonl"
    media_state = media_tmp / "state.json"
    media_echoes: list[tuple[str, str]] = []
    download_calls: list[tuple[str, str, str]] = []

    def fake_downloader(message_id, file_key, kind):
        download_calls.append((message_id, file_key, kind))
        suffix = ".png" if kind == "image" else ".pdf"
        return str(media_tmp / f"{message_id}_{file_key}{suffix}")

    media_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=media_inbox,
        state_path=media_state,
        echo_sender=lambda chat_id, text: media_echoes.append((chat_id, text)),
        downloader=fake_downloader,
    )

    image_record = bridge.extract_record(sample_event(
        "om_img", message_type="image",
        content={"image_key": "img_123"},
    ))
    check("image key extracted", image_record["image_key"] == "img_123")
    r_img = media_proc.process(image_record)
    entries = read_entries(media_inbox)
    check("image accepted with downloaded path",
          r_img == "accepted"
          and entries[-1]["kind"] == "image"
          and entries[-1]["media_paths"] == [str(media_tmp / "om_img_img_123.png")],
          f"got {r_img}, entries={entries[-1:]}")

    file_record = bridge.extract_record(sample_event(
        "om_file", message_type="file",
        content={"file_key": "file_123", "file_name": "季度数据.xlsx"},
    ))
    check("file key and name extracted",
          file_record["file_key"] == "file_123"
          and file_record["file_name"] == "季度数据.xlsx")
    r_file = media_proc.process(file_record)
    entries = read_entries(media_inbox)
    check("file accepted with file_name",
          r_file == "accepted"
          and entries[-1]["kind"] == "file"
          and entries[-1]["file_name"] == "季度数据.xlsx"
          and len(entries[-1]["media_paths"]) == 1,
          f"got {r_file}, entries={entries[-1:]}")

    post_record = bridge.extract_record(sample_event("om_post", message_type="post"))
    check("post flattened to text",
          post_record["text"] == "季度报告\n第一段 链接张三\n第二段",
          post_record["text"])
    check("post image keys extracted",
          post_record["image_keys"] == ["img_post_1", "img_post_2"],
          str(post_record["image_keys"]))
    r_post = media_proc.process(post_record)
    entries = read_entries(media_inbox)
    check("post accepted with both media paths",
          r_post == "accepted"
          and entries[-1]["kind"] == "post"
          and len(entries[-1]["media_paths"]) == 2,
          f"got {r_post}, entries={entries[-1:]}")
    check("media echo placeholders",
          media_echoes == [
              ("oc_chat_demo", "已收到：[图片]"),
              ("oc_chat_demo", "已收到：[文件]"),
              ("oc_chat_demo", "已收到：[post]"),
          ], f"echoes={media_echoes}")
    check("fake downloader calls recorded",
          download_calls == [
              ("om_img", "img_123", "image"),
              ("om_file", "file_123", "file"),
              ("om_post", "img_post_1", "image"),
              ("om_post", "img_post_2", "image"),
          ], str(download_calls))

    # 10) Download failure keeps the message and records media_error
    fail_tmp = tmp / "media-fail"
    fail_tmp.mkdir()
    fail_inbox = fail_tmp / "inbox.jsonl"

    def failing_downloader(message_id, file_key, kind):
        raise RuntimeError("simulated download failure")

    fail_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=fail_inbox,
        state_path=fail_tmp / "state.json",
        downloader=failing_downloader,
    )
    r_fail = fail_proc.process(bridge.extract_record(sample_event(
        "om_img_fail", message_type="image", content={"image_key": "img_bad"})))
    fail_entries = read_entries(fail_inbox)
    check("download failure still accepts message",
          r_fail == "accepted"
          and fail_entries[-1]["media_paths"] == []
          and "simulated download failure" in fail_entries[-1].get("media_error", ""),
          f"got {r_fail}, entries={fail_entries[-1:]}")

    # 11) Group mention gate when explicitly enabled
    group_tmp = tmp / "group"
    group_tmp.mkdir()
    group_inbox = group_tmp / "inbox.jsonl"
    group_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=group_inbox,
        state_path=group_tmp / "state.json",
        allow_group_mentions=True,
        bot_open_id=BOT,
    )
    hit = group_proc.process(bridge.extract_record(sample_event(
        "om_group_hit", chat_type="group", mentions=mention(BOT))))
    check("group @bot accepted when enabled", hit == "accepted", f"got {hit}")
    miss = group_proc.process(bridge.extract_record(sample_event(
        "om_group_miss", chat_type="group", mentions=mention(OTHER))))
    check("group without @bot rejected",
          miss == "ignored_group_mention", f"got {miss}")
    none = group_proc.process(bridge.extract_record(sample_event(
        "om_group_none", chat_type="group", mentions=[])))
    check("group without mentions rejected",
          none == "ignored_group_mention", f"got {none}")

    degraded_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=group_tmp / "degraded-inbox.jsonl",
        state_path=group_tmp / "degraded-state.json",
        allow_group_mentions=True,
        bot_open_id=None,
    )
    degraded = degraded_proc.process(bridge.extract_record(sample_event(
        "om_group_degraded", chat_type="group", mentions=mention(OTHER))))
    check("degraded group rule accepts non-empty mentions",
          degraded == "accepted", f"got {degraded}")

    # 12) Chunking preserves content and honors the per-chunk limit
    paragraph_text = ("第一段内容。\n" * 300) + "\n" + ("第二段内容。\n" * 300)
    chunks = common.chunk_text(paragraph_text, limit=3500)
    check("chunk_text preserves paragraph text",
          "".join(chunks) == paragraph_text and all(len(c) <= 3500 for c in chunks),
          f"chunks={len(chunks)}, lengths={[len(c) for c in chunks]}")
    long_line = "x" * 10000
    long_chunks = common.chunk_text(long_line, limit=3500)
    check("chunk_text hard-splits a long line",
          [len(c) for c in long_chunks] == [3500, 3500, 3000]
          and "".join(long_chunks) == long_line,
          f"lengths={[len(c) for c in long_chunks]}")
    check("chunk_text short text", common.chunk_text("abc", limit=3500) == ["abc"])
    check("chunk_text empty text", common.chunk_text("", limit=3500) == [])

    # 13) Markdown card payload structure (offline construction only)
    payload = common.build_markdown_card_payload("**重点**")
    check("markdown card payload structure",
          payload == {
              "config": {"wide_screen_mode": True},
              "elements": [{"tag": "markdown", "content": "**重点**"}],
          }, json.dumps(payload, ensure_ascii=False))
    check("send auto format detection",
          send_module.choose_format("普通文本", "auto") == "text"
          and send_module.choose_format("**重点**", "auto") == "card"
          and send_module.choose_format("长" * 1201, "auto") == "card"
          and send_module.choose_format("**重点**", "text") == "text")

    # 14) Approval store, cards, card-action decisions, ack throttle
    import os
    import stat as stat_module
    import threading as threading_module

    import approvals as approvals_module

    appr_tmp = tmp / "approvals"
    appr_tmp.mkdir()
    appr_path = appr_tmp / "approvals.json"
    appr_inbox = appr_tmp / "inbox.jsonl"

    entry = approvals_module.create_approval(
        "发布周报", "把本周周报发布到团队群", "oc_chat_demo", path=appr_path,
    )
    check("approval id format",
          entry["id"].startswith("ap_") and len(entry["id"]) == 11,
          entry["id"])
    check("approval created pending",
          entry["status"] == "pending" and entry["decided_at"] is None,
          json.dumps(entry, ensure_ascii=False))
    check("approvals file mode 600",
          stat_module.S_IMODE(os.stat(appr_path).st_mode) == 0o600)
    fetched = approvals_module.get_approval(entry["id"], path=appr_path)
    check("approval get roundtrip",
          fetched is not None and fetched["title"] == "发布周报")
    check("approval get missing returns None",
          approvals_module.get_approval("ap_missing", path=appr_path) is None)

    card = common.build_approval_card(entry)
    check("approval card structure",
          card["header"]["template"] == "orange"
          and card["header"]["title"]["content"] == "待授权：发布周报"
          and card["elements"][0] == {
              "tag": "markdown", "content": "把本周周报发布到团队群"}
          and card["elements"][1]["tag"] == "action",
          json.dumps(card, ensure_ascii=False))
    actions = card["elements"][1]["actions"]
    check("approval card button values",
          actions[0]["type"] == "primary"
          and actions[0]["value"] == {
              "approval_id": entry["id"], "decision": "approved"}
          and actions[1]["type"] == "danger"
          and actions[1]["value"] == {
              "approval_id": entry["id"], "decision": "rejected"},
          json.dumps(actions, ensure_ascii=False))
    check("approval card note mentions owner-only",
          "仅机主本人点击有效" in card["elements"][2]["elements"][0]["content"])

    # Non-owner tap: rejected with toast, nothing stored or appended
    toast, upd_card, record = bridge.apply_card_action(
        OTHER, {"approval_id": entry["id"], "decision": "approved"},
        "oc_chat_demo",
        owner_open_id=OWNER, approvals_path=appr_path, inbox_path=appr_inbox,
    )
    check("non-owner card tap rejected",
          toast == "仅机主可操作" and upd_card is None and record is None
          and not appr_inbox.exists()
          and approvals_module.get_approval(
              entry["id"], path=appr_path)["status"] == "pending",
          f"toast={toast}")

    # Bad value / unknown approval: "not found" toast, still nothing stored
    toast_bad, _, _ = bridge.apply_card_action(
        OWNER, {"approval_id": "ap_missing", "decision": "approved"},
        "oc_chat_demo",
        owner_open_id=OWNER, approvals_path=appr_path, inbox_path=appr_inbox,
    )
    check("unknown approval tap rejected",
          toast_bad == "申请不存在或已失效" and not appr_inbox.exists(),
          f"toast={toast_bad}")

    # Owner approves: store decided, inbox decision record appended
    toast_ok, decided_card, record = bridge.apply_card_action(
        OWNER, {"approval_id": entry["id"], "decision": "approved"},
        "oc_fallback",
        owner_open_id=OWNER, approvals_path=appr_path, inbox_path=appr_inbox,
    )
    check("owner approval toast", toast_ok == "已记录：批准", f"toast={toast_ok}")
    stored = approvals_module.get_approval(entry["id"], path=appr_path)
    check("approval stored as approved",
          stored["status"] == "approved"
          and stored["decided_by"] == OWNER
          and bool(stored["decided_at"]),
          json.dumps(stored, ensure_ascii=False))
    check("decision inbox record fields",
          record is not None
          and record["message_id"] == f"decision_{entry['id']}"
          and record["kind"] == "decision"
          and record["chat_id"] == "oc_chat_demo"
          and record["sender_open_id"] == OWNER
          and record["text"] == "【授权决定】发布周报：已批准"
          and record["approval_id"] == entry["id"]
          and record["decision"] == "approved",
          json.dumps(record, ensure_ascii=False))
    decision_lines = read_entries(appr_inbox)
    check("decision record persisted to inbox file",
          len(decision_lines) == 1
          and decision_lines[0]["message_id"] == record["message_id"])
    check("decision card structure",
          decided_card is not None
          and decided_card["header"]["template"] == "green"
          and decided_card["header"]["title"]["content"] == "已批准：发布周报"
          and all(el.get("tag") != "action" for el in decided_card["elements"])
          and "批准" in decided_card["elements"][0]["content"],
          json.dumps(decided_card, ensure_ascii=False))

    # Second tap (even with the opposite decision) is idempotent
    toast_again, card_again, record_again = bridge.apply_card_action(
        OWNER, {"approval_id": entry["id"], "decision": "rejected"},
        "oc_chat_demo",
        owner_open_id=OWNER, approvals_path=appr_path, inbox_path=appr_inbox,
    )
    check("repeat tap idempotent",
          record_again is None
          and toast_again == "已记录：批准"
          and card_again["header"]["template"] == "green"
          and len(read_entries(appr_inbox)) == 1
          and approvals_module.get_approval(
              entry["id"], path=appr_path)["decided_at"] == stored["decided_at"],
          f"toast={toast_again}")

    # Rejected approval renders a red decision card
    entry2 = approvals_module.create_approval(
        "删除旧备份", "删除 2023 年的备份目录", "", path=appr_path,
    )
    toast_rej, card_rej, record_rej = bridge.apply_card_action(
        OWNER, {"approval_id": entry2["id"], "decision": "rejected"},
        "oc_from_context",
        owner_open_id=OWNER, approvals_path=appr_path, inbox_path=appr_inbox,
    )
    check("rejection flow with context chat fallback",
          toast_rej == "已记录：拒绝"
          and card_rej["header"]["template"] == "red"
          and record_rej["chat_id"] == "oc_from_context"
          and record_rej["decision"] == "rejected",
          f"toast={toast_rej}")

    # SDK surface: typed trigger object exposes the fields the bridge
    # handler reads, and the response marshals to the wire shape.
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    trigger = LarkJSON.unmarshal(
        json.dumps({
            "schema": "2.0",
            "header": {"event_id": "ev-card", "event_type": "card.action.trigger",
                       "tenant_key": "tenant-demo"},
            "event": {
                "operator": {"open_id": OWNER, "user_id": "u-demo",
                             "union_id": "on_demo"},
                "token": "demo-token",
                "action": {"value": {"approval_id": entry["id"],
                                     "decision": "approved"},
                           "tag": "button"},
                "host": "im_message",
                "context": {"open_message_id": "om_card",
                            "open_chat_id": "oc_chat_demo"},
            },
        }),
        P2CardActionTrigger,
    )
    check("typed card trigger fields",
          trigger.event.operator.open_id == OWNER
          and trigger.event.action.value["approval_id"] == entry["id"]
          and trigger.event.context.open_chat_id == "oc_chat_demo")
    response = bridge.build_card_trigger_response("已记录：批准", decided_card)
    wire = json.loads(LarkJSON.marshal(response))
    check("card trigger response wire shape",
          wire["toast"] == {"type": "info", "content": "已记录：批准"}
          and wire["card"]["type"] == "raw"
          and wire["card"]["data"]["header"]["template"] == "green",
          json.dumps(wire, ensure_ascii=False))

    # Ack throttler: one ack per chat per interval, async send
    fake_now = [1000.0]
    ack_calls: list[tuple[str, str]] = []
    ack_done = threading_module.Event()

    def fake_ack_sender(chat_id, text):
        ack_calls.append((chat_id, text))
        ack_done.set()

    throttler = bridge.AckThrottler(
        fake_ack_sender, interval=60.0, clock=lambda: fake_now[0])
    check("ack throttle decisions",
          throttler.should_send("oc_a") is True
          and throttler.should_send("oc_a") is False
          and throttler.should_send("oc_b") is True
          and throttler.should_send("") is False)
    fake_now[0] += 61.0
    check("ack throttle window expires",
          throttler.should_send("oc_a") is True)
    check("ack maybe_send fires once inside window",
          throttler.maybe_send("oc_c") is True
          and throttler.maybe_send("oc_c") is False
          and ack_done.wait(timeout=5)
          and ack_calls == [("oc_c", bridge.ACK_TEXT)],
          f"calls={ack_calls}")
    check("ack text uses configured agent_name, else neutral",
          bridge.ack_text_for({"agent_name": "小明"})
          == "收到，小明 正在处理，稍等。"
          and bridge.ack_text_for({}) == bridge.ACK_TEXT
          and bridge.ack_text_for({"agent_name": "  "}) == bridge.ACK_TEXT)

    # 15) Stale-backlog detection (offline / stuck-worker fallback)
    import inbox_hook as inbox_hook_module

    now_epoch = 1_700_000_000.0
    stale_entries = [
        {"message_id": "om_old_pending", "chat_id": "oc_chat_demo",
         "ts": "2023-11-14T20:00:00Z", "kind": "text"},
        {"message_id": "om_old_done", "chat_id": "oc_chat_demo",
         "ts": "2023-11-14T20:00:00Z", "kind": "text"},
        {"message_id": "om_fresh", "chat_id": "oc_chat_demo",
         "ts": "2023-11-14T22:13:19Z", "kind": "text"},
        {"message_id": "om_bad_ts", "chat_id": "oc_chat_demo",
         "ts": "not-a-date", "kind": "text"},
    ]
    stale_found = inbox_hook_module.find_stale(
        stale_entries, {"om_old_done"}, now_epoch, 600.0)
    check("stale detection finds only old unprocessed",
          [entry["message_id"] for entry, _age in stale_found] == ["om_old_pending"]
          and stale_found[0][1] > 600.0,
          str(stale_found))
    check("stale detection empty when all processed",
          inbox_hook_module.find_stale(
              stale_entries,
              {"om_old_pending", "om_old_done", "om_fresh", "om_bad_ts"},
              now_epoch, 600.0) == [])

    # 16) Onboard creation-time addons + bot-name resolution (offline)
    import onboard as onboard_module
    from lark_oapi.scene.registration import _normalize_addons

    normalized = _normalize_addons(onboard_module.REQUIRED_ADDONS)
    check("onboard addons validate against SDK shape",
          normalized == onboard_module.REQUIRED_ADDONS,
          json.dumps(normalized, ensure_ascii=False))
    check("onboard addons include chat-members scope",
          "im:chat:readonly"
          in onboard_module.REQUIRED_ADDONS["scopes"]["tenant"]
          and "im:message:send_as_bot"
          in onboard_module.REQUIRED_ADDONS["scopes"]["tenant"]
          and "im:resource"
          in onboard_module.REQUIRED_ADDONS["scopes"]["tenant"],
          json.dumps(onboard_module.REQUIRED_ADDONS, ensure_ascii=False))
    check("onboard addons include receive event and card callback",
          onboard_module.REQUIRED_ADDONS["events"]["items"]["tenant"]
          == ["im.message.receive_v1"]
          and onboard_module.REQUIRED_ADDONS["callbacks"]["items"]
          == ["card.action.trigger"])
    check("bot name resolution honours explicit name",
          onboard_module._resolve_bot_name("  我的机器人  ") == "我的机器人"
          and onboard_module._resolve_bot_name(None)
          == onboard_module.APP_PRESET["name"])

    # 17) Peer-bot restricted collaboration channel
    PEER = "ou_peerbot789"
    peer_tmp = tmp / "peer"
    peer_tmp.mkdir()
    peer_inbox = peer_tmp / "inbox.jsonl"
    peer_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=peer_inbox,
        state_path=peer_tmp / "state.json",
        allow_group_mentions=True,
        bot_open_id=BOT,
        peer_bots={PEER: "peer-demo-bot"},
        max_peer_handovers=2,
    )

    def peer_event(mid, **kw):
        kw.setdefault("sender", PEER)
        kw.setdefault("sender_type", "app")
        kw.setdefault("chat_type", "group")
        kw.setdefault("mentions", mention(BOT))
        return bridge.extract_record(sample_event(mid, **kw))

    r_peer_early = peer_proc.process(peer_event("om_peer_early"))
    check("peer without owner-started chain ignored",
          r_peer_early == "ignored_peer_chain", f"got {r_peer_early}")
    r_owner_group = peer_proc.process(bridge.extract_record(sample_event(
        "om_peer_owner", chat_type="group", mentions=mention(BOT))))
    check("owner group message opens peer chain",
          r_owner_group == "accepted", f"got {r_owner_group}")
    r_peer_user = peer_proc.process(peer_event(
        "om_peer_usertype", sender_type="user"))
    check("peer id with sender_type=user ignored",
          r_peer_user == "ignored_peer_type", f"got {r_peer_user}")
    r_peer_nom = peer_proc.process(peer_event(
        "om_peer_nomention", mentions=mention(OTHER)))
    check("peer without @bot ignored",
          r_peer_nom == "ignored_group_mention", f"got {r_peer_nom}")
    r_peer_1 = peer_proc.process(peer_event("om_peer_1", text="协作请求 1"))
    peer_entries = read_entries(peer_inbox)
    check("peer inside chain accepted as collaboration only",
          r_peer_1 == "accepted_peer"
          and peer_entries[-1]["sender_role"] == "peer_bot"
          and peer_entries[-1]["peer_bot_name"] == "peer-demo-bot"
          and peer_entries[-1]["authority"] == "collaboration_only"
          and peer_entries[-1]["sender_open_id"] == PEER,
          f"got {r_peer_1}, entries={peer_entries[-1:]}")
    r_peer_p2p = peer_proc.process(peer_event("om_peer_p2p", chat_type="p2p"))
    check("peer p2p ignored (group only)",
          r_peer_p2p == "ignored_peer_scope", f"got {r_peer_p2p}")
    r_peer_2 = peer_proc.process(peer_event("om_peer_2", text="协作请求 2"))
    check("peer second handover accepted",
          r_peer_2 == "accepted_peer", f"got {r_peer_2}")
    r_peer_3 = peer_proc.process(peer_event("om_peer_3", text="协作请求 3"))
    check("peer handover limit stops the chain",
          r_peer_3 == "ignored_peer_limit", f"got {r_peer_3}")
    # Owner can refresh the chain with a new group message.
    r_owner_group2 = peer_proc.process(bridge.extract_record(sample_event(
        "om_peer_owner2", chat_type="group", mentions=mention(BOT))))
    r_peer_4 = peer_proc.process(peer_event("om_peer_4", text="新链协作"))
    check("owner refresh reopens peer chain",
          r_owner_group2 == "accepted" and r_peer_4 == "accepted_peer",
          f"got {r_owner_group2}/{r_peer_4}")
    check("extract_record carries sender_type",
          peer_event("om_peer_typecheck")["sender_type"] == "app")
    # Real peer apps have been observed delivering with
    # sender_type="bot" (bridge.log 2026-10-07 08:02 / 09:03 UTC:
    # allowlisted peer dropped as ignored_peer_type). The allowlisted
    # open_id is the identity check; "bot" must pass like "app".
    r_peer_bot_type = peer_proc.process(peer_event(
        "om_peer_bottype", sender_type="bot", text="bot 型协作"))
    check("peer id with sender_type=bot accepted",
          r_peer_bot_type == "accepted_peer", f"got {r_peer_bot_type}")

    # 18) Peer discovery: allowlisted peers may introduce themselves
    # without an owner-started chain, rate-limited, discovery-marked.
    disc_tmp = tmp / "discovery"
    disc_tmp.mkdir()
    disc_inbox = disc_tmp / "inbox.jsonl"
    disc_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=disc_inbox,
        state_path=disc_tmp / "state.json",
        allow_group_mentions=True,
        bot_open_id=BOT,
        peer_bots={PEER: "peer-demo-bot"},
        peer_allow_discovery=True,
        peer_discovery_limit=2,
    )

    def disc_event(mid, **kw):
        kw.setdefault("sender", PEER)
        kw.setdefault("sender_type", "app")
        kw.setdefault("chat_type", "group")
        kw.setdefault("mentions", mention(BOT))
        return bridge.extract_record(sample_event(mid, **kw))

    r_disc_plain = disc_proc.process(disc_event(
        "om_disc_plain", text="普通协作请求"))
    check("discovery off-chain non-introduction still ignored",
          r_disc_plain == "ignored_peer_chain", f"got {r_disc_plain}")
    r_disc_1 = disc_proc.process(disc_event(
        "om_disc_1", text="【自我介绍】我是 peer-demo-bot，能力：测试"))
    disc_entries = read_entries(disc_inbox)
    check("peer introduction accepted without chain, marked discovery",
          r_disc_1 == "accepted_peer"
          and disc_entries[-1].get("peer_discovery") is True
          and disc_entries[-1]["authority"] == "collaboration_only",
          f"got {r_disc_1}, entries={disc_entries[-1:]}")
    r_disc_2 = disc_proc.process(disc_event(
        "om_disc_2", text="【自我介绍】补充介绍"))
    check("peer second introduction accepted",
          r_disc_2 == "accepted_peer", f"got {r_disc_2}")
    r_disc_3 = disc_proc.process(disc_event(
        "om_disc_3", text="【自我介绍】第三次介绍"))
    check("peer discovery rate limit stops introductions",
          r_disc_3 == "ignored_peer_discovery_limit",
          f"got {r_disc_3}")
    # Discovery disabled instance: introductions get no exception.
    nodisc_proc = bridge.MessageProcessor(
        owner_open_id=OWNER,
        inbox_path=disc_tmp / "inbox2.jsonl",
        state_path=disc_tmp / "state2.json",
        allow_group_mentions=True,
        bot_open_id=BOT,
        peer_bots={PEER: "peer-demo-bot"},
    )
    r_nodisc = nodisc_proc.process(disc_event(
        "om_nodisc_1", text="【自我介绍】你好"))
    check("discovery disabled: introduction still needs a chain",
          r_nodisc == "ignored_peer_chain", f"got {r_nodisc}")

    # 19) 0.3.7 hardening: unrendered-template overwrite protection.
    # Root cause from two user reports: a systemd unit with leftover
    # __INSTALL_DIR__ placeholders (template cp'd into /etc, or an
    # unrendered hook script whose self-heal sed became a no-op and kept
    # rewriting the raw template over /etc every poll) makes systemd
    # reject the service; the overwrite does not kill the running
    # process, so it stays hidden until the next restart.
    systemd_template = (PROJECT / "systemd" / "feishu-bridge.service.template").read_text(encoding="utf-8")
    check("systemd template carries do-not-cp warning",
          "不能直接复制" in systemd_template
          and systemd_template.startswith("#"),
          systemd_template.splitlines()[0] if systemd_template else "")
    hook_template = (PROJECT / "templates" / "feishu-inbox.sh.template").read_text(encoding="utf-8")
    check("hook self-heal refuses unrendered scripts",
          'case "$BRIDGE_DIR" in' in hook_template
          and "self-heal FAILED" in hook_template
          and "unit left untouched" in hook_template)
    check("hook self-heal renders only when missing or placeholder-tainted",
          "NEED_RENDER=1" in hook_template
          and 'grep -q "${PH_DIR}' in hook_template
          and "WorkingDirectory=$BRIDGE_DIR" in hook_template
          and "self-heal restored" in hook_template)
    # Regression for the root cause itself: install.sh renders this
    # template by plain string replacement, so the literal placeholder
    # tokens may appear ONLY in the BRIDGE_DIR assignment — every other
    # occurrence (sed/grep patterns) would be substituted at install
    # time and silently break self-heal (sed becomes a no-op). The
    # runtime-assembled PH_DIR/PH_PY variables exist for this reason.
    check("hook template is render-proof (literal tokens only in BRIDGE_DIR line)",
          hook_template.count("__INSTALL_DIR__") == 1
          and hook_template.count("__VENV_PYTHON__") == 0
          and 'PH_DIR="__INSTALL"' in hook_template
          and 's|${PH_DIR}|$BRIDGE_DIR|g' in hook_template,
          f"counts={hook_template.count('__INSTALL_DIR__')}/{hook_template.count('__VENV_PYTHON__')}")
    rendered_hook = hook_template.replace(
        "__INSTALL_DIR__", "/srv/feishu-bridge").replace(
        "__VENV_PYTHON__", "/srv/feishu-bridge/.venv/bin/python")
    check("rendered hook keeps working sed/grep placeholder patterns",
          'BRIDGE_DIR="/srv/feishu-bridge"' in rendered_hook
          and 's|${PH_DIR}|$BRIDGE_DIR|g' in rendered_hook
          and 'grep -q "${PH_DIR}' in rendered_hook)
    check("hook self-heal never writes unit with a bare redirection",
          "> /etc/systemd/system/feishu-bridge.service" not in hook_template
          and "> \"$UNIT_FILE\"" not in hook_template)
    install_src = (PROJECT / "install.sh").read_text(encoding="utf-8")
    check("install.sh refuses rendered output with leftover placeholders",
          "still contains" in install_src
          and "refusing to install" in install_src)
    feishu_src = (PROJECT / "feishu.sh").read_text(encoding="utf-8")
    check("doctor fails on placeholder-tainted unit and unrendered hook",
          "仍含未渲染占位符" in feishu_src
          and 'BRIDGE_DIR="__INSTALL_DIR__"' in feishu_src)
    # Rendering the systemd template (same replacements install.sh does)
    # must leave no placeholders behind.
    rendered_unit = systemd_template.replace(
        "__INSTALL_DIR__", "/srv/feishu-bridge").replace(
        "__VENV_PYTHON__", "/srv/feishu-bridge/.venv/bin/python")
    check("rendered systemd unit has no leftover placeholders",
          "__INSTALL_DIR__" not in rendered_unit
          and "__VENV_PYTHON__" not in rendered_unit
          and "WorkingDirectory=/srv/feishu-bridge" in rendered_unit)

    print("\n--- inbox.jsonl (temp) content ---")
    print(inbox.read_text(encoding="utf-8").strip())
    print("----------------------------------")
    if FAILURES:
        print(f"SELFTEST FAILED: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
