#!/usr/bin/env bash
# Feishu/Lark bridge control for Muse.
#   ./feishu.sh status           show onboarding + bridge status
#   ./feishu.sh doctor           local, read-only health checks (masked)
#   ./feishu.sh start [--echo-test]   start bridge.py in the background
#   ./feishu.sh stop             stop the background bridge
#   ./feishu.sh onboard [--manual|--domain feishu|lark]   QR / manual setup
#   ./feishu.sh send --chat-id oc_... --text "..."         send a message
set -u
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/.venv/bin/python"
PIDFILE="$DIR/bridge.pid"
LOGFILE="$DIR/bridge.log"

need_venv() {
  if [ ! -x "$PY" ]; then
    echo "[feishu] venv not found. Install first:" >&2
    echo "  cd $DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
}

bridge_pid() {
  if [ -f "$PIDFILE" ]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  fi
  return 1
}

cmd_status() {
  if [ ! -f "$DIR/config.json" ]; then
    echo "未接入：还没有 config.json。"
    echo "先运行 ./feishu.sh onboard 扫码接入（或 ./feishu.sh onboard --manual 手动填凭证）。"
    return 0
  fi
  "$PY" - <<'PYEOF'
import json, sys
sys.path.insert(0, ".")
import common
cfg = common.load_config()
if cfg is None:
    print("config.json 存在但不完整（缺 app_id / app_secret），请重新 onboard。")
    sys.exit(0)
print("已接入：")
print(f"  app_id:        {common.mask(cfg['app_id'])}")
print(f"  domain:        {cfg.get('domain', 'feishu')}")
print(f"  owner_open_id: {common.mask(cfg.get('owner_open_id'))}")
PYEOF
  if systemctl is-active --quiet feishu-bridge 2>/dev/null; then
    local spid
    spid="$(systemctl show -p MainPID --value feishu-bridge 2>/dev/null || true)"
    echo "桥接状态：运行中（systemd 服务 feishu-bridge, pid ${spid:-?}，回声关闭），日志：$LOGFILE"
  elif pid="$(bridge_pid)"; then
    echo "桥接状态：运行中 (pid $pid，手动模式)，日志：$LOGFILE"
  else
    echo "桥接状态：未运行。生产模式用 systemctl start feishu-bridge；手动模式用 ./feishu.sh start。"
  fi
  if [ -f "$DIR/inbox.jsonl" ]; then
    echo "收件箱：inbox.jsonl 共 $(wc -l < "$DIR/inbox.jsonl" | tr -d ' ') 条"
  else
    echo "收件箱：inbox.jsonl 尚不存在（还没有收到消息）"
  fi
}

cmd_doctor() {
  DOCTOR_DIR="$DIR" "${PYTHON3:-python3}" - <<'PYEOF'
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

base = Path(os.environ["DOCTOR_DIR"])
failures = 0
warnings = 0

def emit(level, message):
    global failures, warnings
    if level == "FAIL":
        failures += 1
    elif level == "WARN":
        warnings += 1
    print(f"[{level}] {message}")

# Python / venv
venv_python = base / ".venv" / "bin" / "python"
if venv_python.is_file() and os.access(venv_python, os.X_OK):
    try:
        out = subprocess.run(
            [str(venv_python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        version = out.stdout.strip() or "unknown"
        emit("OK", f"Python/venv 就绪：{venv_python} (Python {version})")
    except Exception as exc:
        emit("FAIL", f"Python/venv 无法执行：{exc}")
else:
    emit("FAIL", "Python/venv 未就绪：缺少 .venv/bin/python，请先运行 ./install.sh")

# Config (existence and permissions only; values are never printed)
config = base / "config.json"
if not config.is_file():
    emit("FAIL", "config.json 不存在：尚未完成 onboard")
else:
    mode = stat.S_IMODE(config.stat().st_mode)
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
        shape = "JSON 有效" if isinstance(payload, dict) else "JSON 不是对象"
    except Exception:
        shape = "JSON 无效"
    if shape != "JSON 有效":
        emit("FAIL", f"config.json 存在，但{shape}")
    elif mode == 0o600:
        emit("OK", "config.json 存在且权限为 600")
    else:
        emit("FAIL", f"config.json 权限为 {mode:03o}，应为 600")

# systemd service
systemctl = shutil.which("systemctl")
if not systemctl:
    emit("WARN", "systemd 不可用：未找到 systemctl（可使用手动模式）")
else:
    try:
        proc = subprocess.run(
            [systemctl, "is-active", "feishu-bridge"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        state = (proc.stdout or proc.stderr).strip() or "unknown"
        if state == "active":
            emit("OK", "systemd 服务 feishu-bridge：active")
        else:
            emit("FAIL", f"systemd 服务 feishu-bridge：{state}")
    except Exception as exc:
        emit("WARN", f"systemd 服务状态无法读取：{exc}")
    # Enabled check: active alone is not persistence. A bridge that was
    # started manually (or enabled never completed) dies with the session
    # and does not come back after reboot / VM replacement — the exact
    # "works until I close the web page" failure from the first friend
    # install. Treat not-enabled as FAIL whenever systemctl exists.
    try:
        proc = subprocess.run(
            [systemctl, "is-enabled", "feishu-bridge"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        estate = (proc.stdout or proc.stderr).strip() or "unknown"
        if estate == "enabled":
            emit("OK", "systemd 服务 feishu-bridge：enabled（重启/换机后会自启）")
        else:
            emit("FAIL", f"systemd 服务 feishu-bridge 未 enabled（当前：{estate}）：现在能用也不算常驻，重启或会话关闭后就会失联；请运行 ./install.sh 或 systemctl enable feishu-bridge 后复查")
    except Exception as exc:
        emit("WARN", f"systemd enabled 状态无法读取：{exc}")

# Installed systemd unit content (0.3.7): an unrendered unit file —
# template copied into /etc with __INSTALL_DIR__ / __VENV_PYTHON__
# placeholders intact — makes systemd reject the service outright
# ("Unit configuration has fatal error"), and the breakage stays hidden
# until the next restart because replacing the file does not kill the
# running process. Check the actual file content, not just the state.
unit_path = Path("/etc/systemd/system/feishu-bridge.service")
if systemctl and unit_path.is_file():
    try:
        unit_text = unit_path.read_text(encoding="utf-8", errors="replace")
        if "__INSTALL_DIR__" in unit_text or "__VENV_PYTHON__" in unit_text:
            emit("FAIL", "systemd unit 文件仍含未渲染占位符（模板被直接复制到了 /etc）：systemd 会拒绝启动桥；请在项目根目录运行 ./install.sh 由它渲染安装，切勿手工 cp 模板")
        elif "WorkingDirectory=" in unit_text:
            emit("OK", "systemd unit 文件已正确渲染（无占位符残留）")
        else:
            emit("WARN", "systemd unit 文件内容不像是本项目的 unit（缺 WorkingDirectory）")
    except Exception as exc:
        emit("WARN", f"systemd unit 文件无法读取：{exc}")

# status.json freshness
status_path = base / "status.json"
if not status_path.is_file():
    emit("WARN", "status.json 不存在：桥接可能尚未启动过")
else:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        raw_updated = str(status.get("updated_at", ""))
        updated = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - updated).total_seconds()))
        state = status.get("state", "unknown")
        if age <= 90:
            emit("OK", f"status.json 新鲜度：{age} 秒（state={state}）")
        else:
            emit("WARN", f"status.json 已 {age} 秒未更新（state={state}）")
    except Exception as exc:
        emit("WARN", f"status.json 无法解析或缺少 updated_at：{exc}")

# Hook script
hook = Path.home() / "hooks" / "scripts" / "feishu-inbox.sh"
if hook.is_file():
    # An unrendered copy of the template (BRIDGE_DIR still a placeholder)
    # silently disables / corrupts self-heal; doctor must not pass it.
    try:
        hook_text = hook.read_text(encoding="utf-8", errors="replace")
    except Exception:
        hook_text = ""
    if 'BRIDGE_DIR="__INSTALL_DIR__"' in hook_text:
        emit("FAIL", f"hook 脚本是未渲染的模板副本：{hook}：BRIDGE_DIR 仍是占位符，自愈会拒绝工作甚至覆写坏 systemd unit；请运行 ./install.sh 重新渲染")
    else:
        emit("OK", f"hook 脚本存在：{hook}")
else:
    emit("FAIL", f"hook 脚本不存在：{hook}")

# Hook definition: the script file alone does not wake anyone — the hook
# itself must exist in ~/hooks/definitions/ and be enabled. Up to 0.3.2
# doctor could not see this, so a never-created / disabled hook passed
# every check while messages piled up unanswered (the "works until I
# close the web page" friend install had exactly this blind spot).
hook_def = Path.home() / "hooks" / "definitions" / "feishu-inbox.json"
if not hook_def.is_file():
    emit("FAIL", f"hook 定义不存在：{hook_def}：只有轮询脚本不算完成，请让 Muse 按 templates/hook-setup.md 创建并启用 feishu-inbox hook")
else:
    try:
        hook_payload = json.loads(hook_def.read_text(encoding="utf-8"))
        if hook_payload.get("id") != "feishu-inbox":
            emit("FAIL", f"hook 定义 id 不是 feishu-inbox：{hook_def}")
        elif hook_payload.get("enabled") is True:
            emit("OK", "hook feishu-inbox：定义存在且 enabled=true")
        else:
            emit("FAIL", "hook feishu-inbox 定义存在但未 enabled：消息只会躺在 inbox 里无人唤醒；请让 Muse 启用该 hook 后复查")
    except Exception as exc:
        emit("FAIL", f"hook 定义无法解析：{hook_def}：{exc}")

# Inbox / handled state
inbox = base / "inbox.jsonl"
if not inbox.is_file():
    emit("WARN", "inbox.jsonl 不存在：尚无已接受消息或桥接未运行")
else:
    valid = 0
    invalid = 0
    for line in inbox.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
            valid += 1
        except Exception:
            invalid += 1
    if invalid:
        emit("FAIL", f"inbox.jsonl：{valid} 条有效、{invalid} 条无效")
    else:
        emit("OK", f"inbox.jsonl：{valid} 条有效记录")

handled = base / "handled.json"
if not handled.is_file():
    emit("WARN", "handled.json 不存在：hook 尚未处理或尚未初始化")
else:
    try:
        state = json.loads(handled.read_text(encoding="utf-8"))
        processed = len(state.get("processed", []))
        claimed = len(state.get("claimed", {}))
        emit("OK", f"handled.json 有效：processed={processed}，claimed={claimed}")
    except Exception as exc:
        emit("FAIL", f"handled.json 无法解析：{exc}")

# Stale backlog (offline / stuck-worker fallback signal): messages the
# bridge accepted — and acked — but no worker ever marked processed.
# The bridge itself cannot send this warning while it is down, so the
# local doctor is where it must surface; 10 minutes is well past the
# normal wake+reply latency.
try:
    sys.path.insert(0, str(base))
    import inbox_hook as inbox_hook_module

    _entries = inbox_hook_module.read_inbox()
    _state = inbox_hook_module.load_state()
    _stale = inbox_hook_module.find_stale(
        _entries, set(_state.get("processed", [])), datetime.now(timezone.utc).timestamp(), 600.0
    )
    if _stale:
        _oldest_min = int(_stale[0][1] // 60)
        emit("WARN", f"有 {len(_stale)} 条消息超过 10 分钟仍未处理（最早一条已 {_oldest_min} 分钟）：用户可能只收到了即时确认却没等到回复；先确认 hook 已 enabled，再运行 .venv/bin/python inbox_hook.py stale 查看详情，必要时重启服务并让用户重发")
    else:
        emit("OK", "无积压：没有超过 10 分钟未处理的消息")
except Exception as exc:
    emit("WARN", f"积压检查无法执行：{exc}")

if failures:
    print(f"结论：FAIL（{failures} 项失败，{warnings} 项警告）")
    raise SystemExit(1)
if warnings:
    print(f"结论：WARN（0 项失败，{warnings} 项警告）")
else:
    print("结论：OK（全部检查通过）")
PYEOF
}

cmd_start() {
  need_venv
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet feishu-bridge 2>/dev/null; then
    echo "[feishu] systemd 服务 feishu-bridge 正在运行，拒绝手动启动第二个桥接。" >&2
    echo "[feishu] 请使用 systemctl 管理：systemctl status feishu-bridge" >&2
    exit 1
  fi
  if [ ! -f "$DIR/config.json" ]; then
    echo "[feishu] 还没有 config.json，先运行 ./feishu.sh onboard。" >&2
    exit 1
  fi
  if pid="$(bridge_pid)"; then
    echo "[feishu] bridge already running (pid $pid)"
    exit 0
  fi
  nohup "$PY" "$DIR/bridge.py" "$@" >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 1
  if pid="$(bridge_pid)"; then
    echo "[feishu] bridge started (pid $pid), log: $LOGFILE"
  else
    echo "[feishu] bridge failed to start; last log lines:" >&2
    tail -n 20 "$LOGFILE" >&2
    exit 1
  fi
}

cmd_stop() {
  if pid="$(bridge_pid)"; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "[feishu] bridge stopped (pid $pid)"
  else
    echo "[feishu] bridge is not running"
  fi
}

case "${1:-}" in
  status)
    [ -x "$PY" ] || { echo "未接入：venv 尚未安装（先按 README 安装依赖）。"; exit 0; }
    cmd_status
    ;;
  doctor)
    cmd_doctor
    ;;
  start)
    shift
    cmd_start "$@"
    ;;
  stop)
    cmd_stop
    ;;
  onboard)
    shift
    need_venv
    exec "$PY" "$DIR/onboard.py" "$@"
    ;;
  send)
    shift
    need_venv
    exec "$PY" "$DIR/send.py" "$@"
    ;;
  *)
    echo "Usage: $0 {status|doctor|start|stop|onboard|send} [args...]" >&2
    exit 64
    ;;
esac
