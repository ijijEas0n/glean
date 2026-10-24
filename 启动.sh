#!/bin/bash
# 启动拾取 Glean 本地服务。
# 默认后台常驻，日志写到 .runtime/glean.log；加 --foreground 可以前台跑（调试用）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 可用 GLEAN_PYTHON 指定解释器（需 Python 3.11+）；.env 若存在会自动加载
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }
PYTHON="${GLEAN_PYTHON:-$(command -v python3 || true)}"
PORT=8899
HOST="127.0.0.1"
RUNTIME="$ROOT/.runtime"
LOG="$RUNTIME/glean.log"
PIDFILE="$RUNTIME/glean.pid"

if [ ! -x "$PYTHON" ]; then
  echo "✗ 找不到 $PYTHON"
  echo "  请安装 Python 3.11+，或设置 GLEAN_PYTHON 指向解释器。"
  exit 1
fi

mkdir -p "$RUNTIME"

# 已经在跑就不重复启动
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "拾取已经在运行（PID $(cat "$PIDFILE")）→ http://$HOST:$PORT"
  exit 0
fi

# 端口检查用 lsof，确认服务起来用 curl。缺了就直接失败，
# 避免把「命令不存在」误看成「端口空闲」或「服务没起来」。
missing=""
command -v curl >/dev/null 2>&1 || missing="${missing} curl"
command -v lsof >/dev/null 2>&1 || missing="${missing} lsof"
if [ -n "$missing" ]; then
  echo "✗ 缺少命令:${missing}"
  echo "  curl：确认 http://$HOST:$PORT/api/status 已经可访问。"
  echo "  lsof：判断端口 $PORT 是否已被占用。"
  echo "  Debian/Ubuntu：sudo apt-get install -y curl lsof"
  echo "  macOS：通常已随系统提供；缺失时 brew install curl lsof"
  exit 1
fi

# 端口被占：先看是不是我们自己
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ 端口 $PORT 已被占用："
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2
  echo "  先停掉占用进程，或改 server/main.py 里的 BIND_PORT。"
  exit 1
fi

# -u 关掉输出缓冲：不然日志重定向到文件后一直留在缓冲区，
# 出问题时日志是空的，根本没法查（踩过）。
if [ "${1:-}" = "--foreground" ]; then
  echo "前台启动 → http://$HOST:$PORT"
  exec "$PYTHON" -u "$ROOT/server/main.py" --host "$HOST" --port "$PORT"
fi

# 日志超过 5MB 就轮转一次。用 wc -c 读字节数：
# macOS 的 stat -f%z 在 Linux 上不可用，失败后轮转会静默失效。
if [ -f "$LOG" ]; then
  log_bytes=$(wc -c < "$LOG")
  log_bytes=${log_bytes//[[:space:]]/}
  if [ "$log_bytes" -gt 5242880 ]; then
    mv "$LOG" "$LOG.1"
  fi
fi

nohup "$PYTHON" -u "$ROOT/server/main.py" --host "$HOST" --port "$PORT" >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

# 等它真的起来再报成功，不谎报
for _ in $(seq 1 40); do
  if curl -fsS "http://$HOST:$PORT/api/status" >/dev/null 2>&1; then
    echo "✓ 拾取已启动 → http://$HOST:$PORT"
    echo "  日志：$LOG"
    exit 0
  fi
  sleep 0.25
done

echo "✗ 启动失败，日志尾部："
tail -20 "$LOG" || true
rm -f "$PIDFILE"
exit 1
