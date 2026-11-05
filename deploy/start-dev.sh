#!/bin/bash
# ═══ Z-OPS 运维智能体 — 开发模式启动脚本 ═══
# 在前台启动所有组件，方便开发调试
# 使用: bash deploy/start-dev.sh
# 功能: PID 文件锁 + 启动前清理旧进程

set -e

ZOPS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ZOPS_DIR"

PID_DIR="$ZOPS_DIR/.pids"
mkdir -p "$PID_DIR"

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

export PYTHONPATH="$ZOPS_DIR"

echo "╔══════════════════════════════════════╗"
echo "║    Z-OPS 运维智能体 — 开发模式启动    ║"
echo "║    v2.0.0                            ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 清理旧进程 ──
cleanup_old_processes() {
    echo ">>> 检查并清理旧 Z-OPS 进程..."
    local cleaned=0

    # 从 PID 文件清理
    if [ -d "$PID_DIR" ]; then
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            local old_pid
            old_pid=$(cat "$pidfile" 2>/dev/null)
            if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
                echo "    停止旧进程: PID=$old_pid ($(basename "$pidfile" .pid))"
                kill "$old_pid" 2>/dev/null || true
                cleaned=$((cleaned + 1))
            fi
            rm -f "$pidfile"
        done
    fi

    # 按进程名清理残留的 Z-OPS 进程
    local zops_pids
    zops_pids=$(pgrep -f "python3.*agents\.(orchestrator|general_agent|ops_agent|network_agent|vm_agent|dev_agent|browser_agent|opscore_agent|kb_agent)" 2>/dev/null || true)
    if [ -n "$zops_pids" ]; then
        echo "    发现残留 Z-OPS Agent 进程，正在清理..."
        echo "$zops_pids" | xargs kill 2>/dev/null || true
        cleaned=$((cleaned + $(echo "$zops_pids" | wc -l)))
    fi

    # 清理残留的 webchat_api / feishu_bot
    local channel_pids
    channel_pids=$(pgrep -f "python3.*(webchat_api|feishu_bot)" 2>/dev/null || true)
    if [ -n "$channel_pids" ]; then
        echo "    发现残留通道进程，正在清理..."
        echo "$channel_pids" | xargs kill 2>/dev/null || true
        cleaned=$((cleaned + $(echo "$channel_pids" | wc -l)))
    fi

    if [ "$cleaned" -gt 0 ]; then
        echo "    ✓ 已清理 $cleaned 个旧进程"
        sleep 1  # 等待进程退出
    else
        echo "    ✓ 无残留进程"
    fi
}

cleanup_old_processes

# 检查 Redis
if ! redis-cli ping > /dev/null 2>&1; then
    echo "⚠️  Redis 未运行，请先启动 Redis"
    exit 1
fi
echo "✓ Redis 连接正常"

# 存储所有后台进程 PID
PIDS=()

save_pid() {
    local name="$1"
    local pid="$2"
    echo "$pid" > "$PID_DIR/${name}.pid"
    PIDS+=($pid)
}

cleanup() {
    echo ""
    echo ">>> 停止所有组件..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    # 清理 PID 文件
    rm -f "$PID_DIR"/*.pid
    echo ">>> 已停止"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 启动 Orchestrator
echo ">>> 启动 Orchestrator..."
python3 -m agents.orchestrator &
save_pid "orchestrator" $!
sleep 1

# 启动 Agents
for agent in general_agent ops_agent network_agent vm_agent dev_agent browser_agent opscore_agent kb_agent; do
    echo ">>> 启动 ${agent}..."
    python3 -m agents.${agent} &
    save_pid "$agent" $!
    sleep 0.5
done

# 启动 Web Dashboard
echo ">>> 启动 Web Dashboard (:7789)..."
python3 webchat_api.py &
save_pid "webchat_api" $!

# 启动飞书 Bot
echo ">>> 启动飞书 Bot..."
python3 feishu_bot.py &
save_pid "feishu_bot" $!

echo ""
echo "════════════════════════════════════════"
echo "✓ 所有组件已启动 (PID 文件: $PID_DIR/)"
echo "  Web Dashboard: http://localhost:7789"
echo ""
echo "  按 Ctrl+C 停止所有服务"
echo "════════════════════════════════════════"

# 等待所有子进程
wait
