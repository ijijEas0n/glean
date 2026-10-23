#!/bin/bash
# 取消 launchd 托管，回到手动 ./启动.sh 的方式。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.example.glean"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl unload "$DST" 2>/dev/null || true
rm -f "$DST"

# 确认真的停了
for _ in $(seq 1 20); do
  if ! curl -fsS "http://127.0.0.1:8899/api/status" >/dev/null 2>&1; then
    echo "✓ 已取消托管，服务已停止"
    echo "  之后可以用 ./启动.sh 手动启动"
    exit 0
  fi
  sleep 0.25
done

echo "⚠ 托管配置已删，但服务还在跑（可能是 KeepAlive 正在拉起）。"
echo "  再等一下，或手动执行：launchctl bootout gui/$(id -u)/$LABEL"
exit 1
