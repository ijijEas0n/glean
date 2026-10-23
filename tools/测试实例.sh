#!/bin/bash
# 起一个「隔离的测试实例」：临时库 + 临时配置 + 另一个端口。
#
# 为什么要有这个脚本：我（agent）做验证时图省事直接打在主服务 8899 上，
# 结果把测试抓的内容写进了用户的真实库，用户打开「我拿过的」发现了不属于自己的东西。
# 光在 AGENTS.md 里写纪律不够——我自己就违反了。所以要有一条**顺手就能走对**的路。
#
# 用法：
#   ./tools/测试实例.sh start     # 起，打印 URL 和 env
#   ./tools/测试实例.sh stop      # 停并清理
#   ./tools/测试实例.sh status
#
# 测试脚本里请从环境变量拿地址，**不要写死 8899**：
#   const BASE = process.env.GLEAN_TEST_URL;
#   if (!BASE) throw new Error('没指向测试实例，拒绝运行');
# 这样就算忘了，脚本也会直接报错，而不是悄悄写进真实库。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${GLEAN_PYTHON:-$(command -v python3)}"
PORT=8901
LIB="/tmp/glean-test-lib"
CFG="/tmp/glean-test-cfg"
PIDFILE="/tmp/glean-test.pid"
LOG="/tmp/glean-test.log"

case "${1:-start}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "测试实例已经在跑（PID $(cat "$PIDFILE")）"
    else
      rm -rf "$LIB" "$CFG"
      mkdir -p "$LIB" "$CFG"
      GLEAN_LIBRARY_DIR="$LIB" GLEAN_CONFIG_DIR="$CFG" \
        nohup "$PYTHON" -u "$ROOT/server/main.py" --port "$PORT" >"$LOG" 2>&1 &
      echo $! >"$PIDFILE"
    fi
    for _ in $(seq 1 40); do
      if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
        echo "✓ 测试实例就绪"
        echo
        echo "  URL      http://127.0.0.1:$PORT"
        echo "  库       $LIB        （和真实库完全分开）"
        echo "  配置     $CFG        （改设置也不会影响真实配置）"
        echo "  日志     $LOG"
        echo
        echo "  测试脚本里这样拿地址，别写死端口："
        echo "    GLEAN_TEST_URL=http://127.0.0.1:$PORT node your_test.js"
        exit 0
      fi
      sleep 0.25
    done
    echo "✗ 没起来，看日志："
    tail -20 "$LOG" || true
    exit 1
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
    fi
    pkill -f "server/main.py --port $PORT" 2>/dev/null || true
    rm -rf "$LIB" "$CFG" "$LOG"
    echo "✓ 测试实例已停，临时库和配置都清了"
    ;;
  status)
    if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
      echo "测试实例在跑 → http://127.0.0.1:$PORT"
      curl -s "http://127.0.0.1:$PORT/api/status" | "$PYTHON" -c "
import json,sys
d=json.load(sys.stdin)
print('  库:', d['library_root'], '|', d['counts']['items'], '条')
"
    else
      echo "测试实例没在跑"
    fi
    ;;
  *)
    echo "用法：$0 {start|stop|status}"
    exit 2
    ;;
esac
