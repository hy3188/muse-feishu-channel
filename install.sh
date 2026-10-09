#!/usr/bin/env bash
# One-command installer for feishu-bridge on Linux + systemd hosts.
# Safe to re-run: it reuses .venv, does not overwrite config.json,
# egress.env, or an existing rendered hook script's directory setup.
set -euo pipefail

cd "$(dirname "$0")"
INSTALL_DIR="$(pwd)"
VENV_DIR="$INSTALL_DIR/.venv"
PY="$VENV_DIR/bin/python"
SYSTEMD_TEMPLATE="$INSTALL_DIR/systemd/feishu-bridge.service.template"
SYSTEMD_DEST="${FEISHU_SYSTEMD_UNIT_PATH:-/etc/systemd/system/feishu-bridge.service}"
HOOK_TEMPLATE="$INSTALL_DIR/templates/feishu-inbox.sh.template"
HOOK_DIR="$HOME/hooks/scripts"
HOOK_DEST="$HOOK_DIR/feishu-inbox.sh"

info() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }

render_template() {
  local src="$1" dst="$2"
  INSTALL_DIR="$INSTALL_DIR" VENV_PYTHON="$PY" TEMPLATE_SRC="$src" TEMPLATE_DST="$dst" python3 - <<'PY'
import os
import sys
from pathlib import Path

src = Path(os.environ["TEMPLATE_SRC"])
dst = Path(os.environ["TEMPLATE_DST"])
content = src.read_text(encoding="utf-8")
content = content.replace("__INSTALL_DIR__", os.environ["INSTALL_DIR"])
content = content.replace("__VENV_PYTHON__", os.environ["VENV_PYTHON"])
# Hard validation (0.3.7): a rendered file that still contains
# placeholders must NEVER be installed. An unrendered systemd unit makes
# systemd refuse to start the bridge ("Unit configuration has fatal
# error") — and because replacing the unit does not kill the running
# process, the breakage stays hidden until the next restart. Refuse the
# whole install instead of writing a time bomb.
leftover = [p for p in ("__INSTALL_DIR__", "__VENV_PYTHON__") if p in content]
if leftover:
    print(
        f"[install] ERROR: rendered output for {dst} still contains "
        f"placeholder(s) {leftover}; refusing to install an unrendered file",
        file=sys.stderr,
    )
    raise SystemExit(1)
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(content, encoding="utf-8")
PY
}

# 1) Python >= 3.11
if ! command -v python3 >/dev/null 2>&1; then
  echo "[install] ERROR: python3 not found" >&2
  exit 1
fi
if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "[install] ERROR: Python 3.11 or newer is required" >&2
  exit 1
fi
info "Python 检查通过：$(python3 --version 2>&1)"

# 2) Virtual environment + dependencies
if [ ! -x "$PY" ]; then
  info "创建虚拟环境 .venv"
  python3 -m venv "$VENV_DIR"
fi
info "安装/更新 Python 依赖"
"$PY" -m pip install -r "$INSTALL_DIR/requirements.txt"

# 3) Snapshot proxy / CA environment for systemd-launched processes
if [ -f "$INSTALL_DIR/egress.env" ]; then
  info "egress.env 已存在，跳过环境快照"
else
  EGRESS_TMP="$(mktemp)"
  env | sort | while IFS='=' read -r key value; do
    upper="$(printf '%s' "$key" | tr '[:lower:]' '[:upper:]')"
    case "$upper" in
      *PROXY*|SSL_CERT_FILE|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE)
        printf '%s=%s\n' "$key" "$value"
        ;;
    esac
  done > "$EGRESS_TMP"
  if [ -s "$EGRESS_TMP" ]; then
    mv "$EGRESS_TMP" "$INSTALL_DIR/egress.env"
    chmod 600 "$INSTALL_DIR/egress.env"
    info "已把当前代理/CA 环境快照写入 egress.env（权限 600）"
  else
    rm -f "$EGRESS_TMP"
    info "当前环境没有代理/CA 变量，跳过 egress.env 快照"
  fi
fi

# 4) Onboarding hint (never create or overwrite credentials here)
if [ -f "$INSTALL_DIR/config.json" ]; then
  info "发现已有 config.json，保留现有接入配置"
else
  warn "尚未接入：安装完成后运行 ./feishu.sh onboard 并用飞书 App 扫码"
fi

# 5) systemd service from the template
if command -v systemctl >/dev/null 2>&1; then
  RENDERED_UNIT="$(mktemp)"
  render_template "$SYSTEMD_TEMPLATE" "$RENDERED_UNIT"
  # Defense in depth (0.3.7): never copy a unit with leftover placeholders
  # into /etc, even if render_template's own validation is bypassed.
  if grep -q '__INSTALL_DIR__\|__VENV_PYTHON__' "$RENDERED_UNIT"; then
    echo "[install] ERROR: rendered systemd unit still contains placeholders; refusing to install it (an unrendered unit makes systemd reject the service)" >&2
    rm -f "$RENDERED_UNIT"
    exit 1
  fi
  INSTALLED_UNIT=0
  if [ "$(id -u)" -eq 0 ]; then
    install -m 644 "$RENDERED_UNIT" "$SYSTEMD_DEST" && INSTALLED_UNIT=1
  elif command -v sudo >/dev/null 2>&1; then
    if sudo install -m 644 "$RENDERED_UNIT" "$SYSTEMD_DEST"; then
      INSTALLED_UNIT=1
    fi
  else
    warn "无 root/sudo 权限，未安装 systemd unit：$SYSTEMD_DEST"
  fi
  rm -f "$RENDERED_UNIT"
  if [ "$INSTALLED_UNIT" -eq 1 ]; then
    info "systemd unit 已渲染到 $SYSTEMD_DEST"
    if systemctl daemon-reload; then
      if systemctl enable feishu-bridge; then
        info "systemd 服务 feishu-bridge 已 enable"
        if systemctl is-enabled --quiet feishu-bridge; then
          info "复核通过：feishu-bridge 处于 enabled 状态（重启/换机后会自启）"
        else
          warn "复核失败：feishu-bridge 并未处于 enabled 状态——这不算常驻安装，重启或会话关闭后会失联；请勿宣布安装完成，先修复 enable"
        fi
      else
        warn "systemctl enable 失败，请检查 systemd 环境"
      fi
      if [ -f "$INSTALL_DIR/config.json" ]; then
        # Updates must RESTART, not start: `systemctl start` on an already
        # active service is a no-op, so an upgraded install would silently
        # keep running the old bridge code (fixed in 0.3.3).
        if systemctl is-active --quiet feishu-bridge; then
          if systemctl restart feishu-bridge; then
            info "systemd 服务 feishu-bridge 已 restart（更新后旧进程已替换，新代码生效）"
          else
            warn "systemctl restart 失败，请用 systemctl status feishu-bridge 查看"
          fi
        elif systemctl start feishu-bridge; then
          info "systemd 服务 feishu-bridge 已 start"
        else
          warn "systemctl start 失败，请用 systemctl status feishu-bridge 查看"
        fi
      else
        info "没有 config.json，暂不启动服务；先完成 onboard"
      fi
    else
      warn "systemctl daemon-reload 失败，跳过 enable/start"
    fi
  fi
else
  warn "未找到 systemctl，跳过 systemd 安装"
  info "手动启动方式：完成 onboard 后运行 ./feishu.sh start"
fi

# 6) Render the Muse hook polling script (the hook itself is created by Muse)
mkdir -p "$HOOK_DIR"
render_template "$HOOK_TEMPLATE" "$HOOK_DEST"
chmod +x "$HOOK_DEST"
info "hook 轮询脚本已渲染到 $HOOK_DEST"
# The script alone wakes nobody: verify the hook definition itself.
# On a fresh install it normally does not exist yet (Muse creates it
# next); on an update it MUST already exist and be enabled — a missing
# or disabled definition is the known "installed but silent" failure.
HOOK_DEF="$HOME/hooks/definitions/feishu-inbox.json"
if [ -f "$HOOK_DEF" ]; then
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("id")=="feishu-inbox" and d.get("enabled") is True else 1)' "$HOOK_DEF" 2>/dev/null; then
    info "复核通过：hook feishu-inbox 定义存在且已 enabled"
  else
    warn "复核失败：hook 定义 $HOOK_DEF 存在但未 enabled（或 id 不对）——消息不会唤醒 Muse；请让 Muse 启用该 hook，勿宣布安装完成"
  fi
elif [ -f "$INSTALL_DIR/config.json" ]; then
  warn "未检测到 hook 定义 $HOOK_DEF：若这是更新且此前已建过 hook，说明 hook 已丢失；若是首次安装，请按下一步创建。没有 enabled 的 hook 就不算装好"
else
  info "下一步：把 templates/hook-setup.md 交给你的 Muse，让它创建并启用 feishu-inbox hook"
fi

# 7) Offline self-test
SELFTEST_LOG="$(mktemp)"
info "运行离线自测"
if "$PY" "$INSTALL_DIR/tests/selftest.py" >"$SELFTEST_LOG" 2>&1; then
  grep -E '^\[PASS\]|SELFTEST OK' "$SELFTEST_LOG" | tail -n 8
  info "自测摘要：SELFTEST OK（完整输出已通过）"
  rm -f "$SELFTEST_LOG"
else
  cat "$SELFTEST_LOG" >&2
  rm -f "$SELFTEST_LOG"
  echo "[install] ERROR: self-test failed" >&2
  exit 1
fi

info "安装完成"
if [ ! -f "$INSTALL_DIR/config.json" ]; then
  info "接下来运行：./feishu.sh onboard"
fi
info "健康检查：./feishu.sh doctor"
