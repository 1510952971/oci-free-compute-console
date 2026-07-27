#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
LABEL="com.github.oci-free-compute-console"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "错误：尚未找到项目虚拟环境。请先双击 start.command 完成首次安装。"
  read "?按回车键退出..."
  exit 1
fi

mkdir -p "$PLIST_DIR"
mkdir -p "$LOG_DIR"

"$PYTHON" - "$PLIST_PATH" "$SCRIPT_DIR" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
root = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
payload = {
    "Label": "com.github.oci-free-compute-console",
    "ProgramArguments": [
        "/usr/bin/caffeinate",
        "-dimsu",
        str(root / ".venv" / "bin" / "python"),
        str(root / "grab_a1.py"),
    ],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "ThrottleInterval": 15,
    "StandardOutPath": str(log_dir / "oci-free-compute-console.log"),
    "StandardErrorPath": str(log_dir / "oci-free-compute-console-error.log"),
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
}
with plist_path.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

chmod 600 "$PLIST_PATH"
launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$PLIST_PATH"
launchctl enable "gui/$UID/$LABEL"
launchctl kickstart -k "gui/$UID/$LABEL"

echo
echo "后台服务已安装并启动。"
echo "控制台：http://127.0.0.1:8787"
echo "日志：$LOG_DIR/oci-free-compute-console.log"
echo
read "?按回车键关闭此窗口..."
