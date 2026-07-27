#!/bin/zsh

set -euo pipefail

LABEL="com.github.oci-free-compute-console"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "OCI 免费实例后台服务已停止并卸载。"
echo "项目、配置、日志和已创建的云实例均未删除。"
echo
read "?按回车键关闭此窗口..."
