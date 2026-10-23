#!/bin/bash
# 把拾取交给 launchd 托管：崩了自动拉起、开机自启、日志实时落盘。
#
# 装完之后日常就不需要再手动 ./启动.sh 了。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.example.glean"
SRC="$ROOT/launchd.plist.template"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8899

if [ ! -f "$SRC" ]; then
  echo "✗ 找不到 $SRC"
  exit 1
fi

mkdir -p "$ROOT/.runtime" "$HOME/Library/LaunchAgents"

# 先停掉手动起的那个，避免抢端口
if [ -f "$ROOT/.runtime/glean.pid" ]; then
  "$ROOT/停止.sh" >/dev/null 2>&1 || true
fi

# 已经装过就先卸掉，保证这次是干净的
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl unload "$DST" 2>/dev/null || true
rm -f "$DST"

sed -e "s#__ROOT__#$ROOT#g" -e "s#__LABEL__#$LABEL#g" "$SRC" > "$DST"
# 现代写法是 bootstrap；老系统只认 load，两个都试
if ! launchctl bootstrap "gui/$(id -u)" "$DST" 2>/dev/null; then
  launchctl load -w "$DST"
fi

# 等它真的起来再报成功，不谎报
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "✓ 已交给 launchd 托管 → http://127.0.0.1:$PORT"
    echo "  配置：$DST"
    echo "  日志：$ROOT/.runtime/glean.log"
    echo
    echo "  查看状态：launchctl print gui/$(id -u)/$LABEL | head -20"
    echo "  停止托管：./卸载自启.sh"
    exit 0
  fi
  sleep 0.25
done

echo "✗ 托管装上了但服务没起来，看日志："
tail -20 "$ROOT/.runtime/glean.err.log" 2>/dev/null || tail -20 "$ROOT/.runtime/glean.log" 2>/dev/null || true
exit 1
