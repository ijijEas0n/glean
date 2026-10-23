#!/bin/bash
# 停止拾取 Glean 本地服务。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$ROOT/.runtime/glean.pid"
PORT=8899

# 如果服务是 launchd 托管的，直接杀进程没用——KeepAlive 会立刻把它拉起来，
# 用户会以为"停不掉"。这种情况必须说清楚该怎么做。
LABEL="com.example.glean"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "这个服务现在由 launchd 托管（崩了会自动拉起，开机也会自启）。"
  echo "要停掉请执行：./卸载自启.sh"
  echo "（只是想重启的话：launchctl kickstart -k gui/$(id -u)/${LABEL}）"
  exit 0
fi

stopped=0
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
    # 变量必须用 ${} 界定：后面紧跟全角右括号时，bash 会把它当成变量名的一部分
    echo "✓ 已停止（PID ${pid}）"
    stopped=1
  fi
  rm -f "$PIDFILE"
fi

# 兜底：pid 文件丢了也能停掉占用端口的进程
if [ "$stopped" -eq 0 ]; then
  leftover="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -n "$leftover" ]; then
    kill $leftover 2>/dev/null || true
    echo "✓ 已停止占用 ${PORT} 的进程（PID ${leftover}）"
    stopped=1
  fi
fi

[ "$stopped" -eq 0 ] && echo "拾取没有在运行。"
exit 0
