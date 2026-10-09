#!/usr/bin/env python3
"""Onboard the Feishu/Lark app for the Muse bridge.

Default mode (QR / device flow):
    Uses lark_oapi.register_app (OAuth device flow, archetype=PersonalAgent).
    The terminal prints a verification URL plus an ASCII QR code, and saves
    the QR as qr.png in this directory. Scan it with the Feishu/Lark mobile
    app and approve; the app is created in YOUR tenant and its credentials
    are written to config.json (chmod 600).

    Creation-time configuration (do NOT defer these to the developer
    console — post-hoc changes need a manual version publish and are a
    known pain point):
      * ``addons`` pre-fills every scope/event/callback the bridge needs
        into the scan-confirm page, including ``im:chat:readonly`` (list
        chat members / bots). They take effect when the user confirms.
      * ``app_preset.name`` pre-fills the bot name on the same page. A
        PersonalAgent name cannot be changed through this flow afterwards,
        so the name is chosen BEFORE the QR is shown and the user is told
        to verify it on the confirm page.

Update mode (--update-permissions):
    Re-runs the QR flow against the EXISTING app (register_app with
    app_id + addons) so an already-created bot can gain the scopes above
    by scanning once, instead of hand-editing the developer console and
    publishing a new version. Credentials and extra config keys are
    preserved. This flow cannot rename the bot.

Manual fallback mode (--manual):
    Reads FEISHU_APP_ID / FEISHU_APP_SECRET (and optionally FEISHU_DOMAIN,
    FEISHU_OWNER_OPEN_ID) from the environment and writes config.json.
    Use this when you created the app by hand in the developer console,
    or when the QR flow does not respond (seen with some CN Feishu setups).

No credential value is ever printed; the success summary is masked.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import CONFIG_PATH, mask, save_config  # noqa: E402

APP_PRESET = {
    "name": "{user} 的 Muse",
    "desc": "Muse personal agent bridge (Feishu/Lark channel)",
}

# Everything the bridge needs, requested at creation time via addons.
# Scopes verified against the live API: listing chat members/bots fails
# with 99991672 unless one of im:chat:readonly / im:chat /
# im:chat.group_info:readonly / im:chat.members:read is granted, and the
# current pre-0.3.4 apps have none of them.
REQUIRED_ADDONS = {
    "scopes": {
        "tenant": [
            "im:message",
            "im:message:send_as_bot",
            "im:resource",
            "im:chat:readonly",
        ],
    },
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
    "callbacks": {"items": ["card.action.trigger"]},
}


def _resolve_bot_name(explicit: str | None) -> str:
    """Pick the bot name BEFORE the QR exists; there is no rename later.

    Priority: --name flag > FEISHU_BOT_NAME env > interactive prompt
    (TTY only) > the historical default.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env_name = os.environ.get("FEISHU_BOT_NAME", "").strip()
    if env_name:
        return env_name
    default = APP_PRESET["name"]
    if sys.stdin.isatty():
        print("[onboard] 机器人名称在创建后无法通过扫码流程修改，"
              "请在扫码前定好（确认页上还可以最后核对一次）。")
        try:
            entered = input(
                f"[onboard] 机器人名称 [{default}]: ").strip()
        except EOFError:
            entered = ""
        if entered:
            return entered
    return default


def _render_qr(url: str) -> None:
    """Save qr.png and print a terminal QR code for the verification URL."""
    import qrcode

    img = qrcode.make(url)
    qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr.png")
    img.save(qr_path)
    print(f"[onboard] QR code saved to: {qr_path}")

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def onboard_qr(domain_override: str | None, bot_name: str | None = None,
               update_app_id: str | None = None) -> int:
    try:
        import lark_oapi as lark
    except ImportError:
        print(
            "[onboard] lark-oapi is not installed. Run the venv install first "
            "(see README), or use --manual mode.",
            file=sys.stderr,
        )
        return 2
    if not hasattr(lark, "register_app"):
        print(
            "[onboard] this lark-oapi version has no register_app; "
            "upgrade (pip install -U 'lark-oapi>=1.6.8') or use --manual mode.",
            file=sys.stderr,
        )
        return 2

    def on_qr_code(info: dict) -> None:
        url = info["url"]
        print("[onboard] Scan this URL with the Feishu/Lark app to create the app:")
        print(f"[onboard] {url}")
        print(f"[onboard] (link expires in {info.get('expire_in', '?')}s)")
        _render_qr(url)
        print("[onboard] Waiting for you to scan and approve in the app ...")

    def on_status_change(info: dict) -> None:
        print(f"[onboard] status: {info.get('status')}")

    existing_cfg: dict = {}
    if update_app_id:
        from common import load_config

        existing_cfg = load_config() or {}
        if existing_cfg.get("app_id") != update_app_id:
            print("[onboard] --update-permissions: config.json 的 app_id "
                  "与待更新应用不一致，已中止，未改任何配置。",
                  file=sys.stderr)
            return 2
        print("[onboard] 更新模式：对已有应用增量补权限（扫码确认后生效）；"
              "此流程不能改机器人名称。")
        result = lark.register_app(
            on_qr_code=on_qr_code,
            on_status_change=on_status_change,
            app_id=update_app_id,
            addons=REQUIRED_ADDONS,
        )
    else:
        preset = dict(APP_PRESET)
        preset["name"] = _resolve_bot_name(bot_name)
        print(f"[onboard] 机器人名称将预填为「{preset['name']}」；"
              "所需权限（含 im:chat:readonly）会一并预填到扫码确认页，"
              "请在确认页核对名称与权限后再确认——创建后改名/补权限都很麻烦。")
        result = lark.register_app(
            on_qr_code=on_qr_code,
            on_status_change=on_status_change,
            app_preset=preset,
            addons=REQUIRED_ADDONS,
        )

    user_info = result.get("user_info") or {}
    if domain_override:
        domain = domain_override
    elif user_info.get("tenant_brand") == "lark":
        domain = "lark"
    elif existing_cfg.get("domain"):
        domain = str(existing_cfg["domain"])
    else:
        domain = "feishu"

    if update_app_id and result["client_id"] != update_app_id:
        print("[onboard] 更新流程返回的 app_id 与原应用不一致，"
              "已中止，未覆盖 config.json。", file=sys.stderr)
        return 2

    cfg = dict(existing_cfg)
    cfg.update({
        "app_id": result["client_id"],
        "app_secret": result["client_secret"],
        "domain": domain,
        "owner_open_id": user_info.get("open_id", "")
        or existing_cfg.get("owner_open_id", ""),
    })
    save_config(cfg)
    print("[onboard] SUCCESS — config written to", CONFIG_PATH, "(chmod 600)")
    print(f"[onboard]   app_id:        {mask(cfg['app_id'])}")
    print("[onboard]   app_secret:    *** (stored, never printed)")
    print(f"[onboard]   domain:        {cfg['domain']}")
    print(f"[onboard]   owner_open_id: {mask(cfg['owner_open_id'])}")
    if not cfg["owner_open_id"]:
        print(
            "[onboard] NOTE: no owner open_id returned; the bridge will log "
            "the sender of the first DM so you can fill owner_open_id in "
            "config.json manually."
        )
    return 0


def onboard_manual(domain_override: str | None) -> int:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print(
            "[onboard] --manual needs FEISHU_APP_ID and FEISHU_APP_SECRET "
            "in the environment. Example:\n"
            "  FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx "
            "./feishu.sh onboard --manual",
            file=sys.stderr,
        )
        return 2
    domain = (
        domain_override
        or os.environ.get("FEISHU_DOMAIN", "").strip()
        or "feishu"
    )
    if domain not in ("feishu", "lark"):
        print(f"[onboard] domain must be 'feishu' or 'lark', got: {domain}",
              file=sys.stderr)
        return 2
    cfg = {
        "app_id": app_id,
        "app_secret": app_secret,
        "domain": domain,
        "owner_open_id": os.environ.get("FEISHU_OWNER_OPEN_ID", "").strip(),
    }
    save_config(cfg)
    print("[onboard] SUCCESS (manual) — config written to", CONFIG_PATH,
          "(chmod 600)")
    print(f"[onboard]   app_id:        {mask(cfg['app_id'])}")
    print("[onboard]   app_secret:    *** (stored, never printed)")
    print(f"[onboard]   domain:        {cfg['domain']}")
    print(f"[onboard]   owner_open_id: {mask(cfg['owner_open_id'])}")
    if not cfg["owner_open_id"]:
        print(
            "[onboard] NOTE: owner_open_id is empty. The bridge allowlist "
            "will accept nobody until you set it; send the bot a DM after "
            "starting the bridge and copy the logged sender open_id here."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu/Lark onboarding")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="read credentials from FEISHU_APP_ID/FEISHU_APP_SECRET env vars",
    )
    parser.add_argument(
        "--domain",
        choices=["feishu", "lark"],
        default=None,
        help="force domain instead of auto-detecting from the tenant",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="bot name pre-filled on the creation confirm page "
             "(default: prompt on a TTY, else '{user} 的 Muse'); "
             "choose it now — it cannot be changed via this flow later",
    )
    parser.add_argument(
        "--update-permissions",
        action="store_true",
        help="scan once to add REQUIRED_ADDONS to the EXISTING app from "
             "config.json, instead of creating a new app",
    )
    args = parser.parse_args()
    if args.manual:
        if args.update_permissions or args.name:
            print("[onboard] --manual cannot be combined with "
                  "--update-permissions/--name", file=sys.stderr)
            return 2
        return onboard_manual(args.domain)
    if args.update_permissions:
        from common import load_config

        existing = load_config()
        if not existing:
            print("[onboard] --update-permissions needs an existing "
                  "config.json; run a normal onboard first.",
                  file=sys.stderr)
            return 2
        return onboard_qr(args.domain, update_app_id=existing["app_id"])
    return onboard_qr(args.domain, bot_name=args.name)


if __name__ == "__main__":
    raise SystemExit(main())
