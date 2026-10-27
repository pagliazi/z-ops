#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Z-OPS 服务管理工具
# 功能：启动/停止/重启/状态查看
# ═══════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_SCRIPT="${SCRIPT_DIR}/start_webchat.sh"
PID_FILE="${SCRIPT_DIR}/logs/webchat.pid"
LOG_FILE="${SCRIPT_DIR}/logs/webchat.log"
PYTHON_CMD="${PYTHON_CMD:-python3}"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() {
    echo -e "${BLUE}════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}════════════════════════════════════════${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warn() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# 获取进程状态
get_pid() {
    if [ -f "${PID_FILE}" ]; then
        local pid=$(cat "${PID_FILE}")
        if kill -0 "${pid}" 2>/dev/null; then
            echo "${pid}"
            return 0
        fi
    fi
    
    # 尝试通过进程名查找
    local pgrep_pid=$(pgrep -f "webchat_api.py" | head -n1 || true)
    if [ -n "${pgrep_pid}" ]; then
        echo "${pgrep_pid}"
        return 0
    fi
    
    echo ""
    return 1
}

# 启动服务
start_service() {
    print_status "启动 Z-OPS WebChat 服务"
    
    local pid=$(get_pid)
    if [ -n "${pid}" ]; then
        print_warn "服务已在运行 (PID: ${pid})"
        echo ""
        echo "使用以下命令重启："
        echo "  $0 restart"
        return 1
    fi
    
    if [ ! -f "${DAEMON_SCRIPT}" ]; then
        print_error "守护脚本不存在：${DAEMON_SCRIPT}"
        return 1
    fi
    
    # 启动守护进程
    nohup bash "${DAEMON_SCRIPT}" > "${SCRIPT_DIR}/logs/daemon_stdout.log" 2>&1 &
    
    sleep 2
    
    pid=$(get_pid)
    if [ -n "${pid}" ]; then
        print_success "服务已启动 (PID: ${pid})"
        echo ""
        echo "查看日志："
        echo "  tail -f ${LOG_FILE}"
        echo ""
        echo "访问服务："
        echo "  http://localhost:7789"
        return 0
    else
        print_error "服务启动失败"
        print_error "查看日志：${SCRIPT_DIR}/logs/daemon_stdout.log"
        return 1
    fi
}

# 停止服务
stop_service() {
    print_status "停止 Z-OPS WebChat 服务"
    
    local pid=$(get_pid)
    if [ -z "${pid}" ]; then
        print_warn "服务未运行"
        return 0
    fi
    
    print_status "正在停止服务 (PID: ${pid})..."
    
    # 发送 SIGTERM
    kill -TERM "${pid}" 2>/dev/null || true
    
    # 等待进程结束
    local count=0
    while kill -0 "${pid}" 2>/dev/null && [ $count -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done
    
    # 如果还在运行，强制终止
    if kill -0 "${pid}" 2>/dev/null; then
        print_warn "进程未响应，强制终止..."
        kill -9 "${pid}" 2>/dev/null || true
    fi
    
    # 清理残留进程
    local remaining=$(pgrep -f "webchat_api.py" || true)
    if [ -n "${remaining}" ]; then
        print_warn "清理残留进程..."
        echo "${remaining}" | xargs kill -9 2>/dev/null || true
    fi
    
    # 清理 PID 文件
    rm -f "${PID_FILE}"
    
    print_success "服务已停止"
    return 0
}

# 重启服务
restart_service() {
    print_status "重启 Z-OPS WebChat 服务"
    
    stop_service
    sleep 2
    start_service
}

# 查看状态
status_service() {
    print_status "Z-OPS WebChat 服务状态"
    
    local pid=$(get_pid)
    
    if [ -n "${pid}" ]; then
        print_success "服务正在运行"
        echo ""
        echo "进程信息:"
        ps -p "${pid}" -o pid,ppid,user,%cpu,%mem,etime,command 2>/dev/null || true
        echo ""
        
        # 检查端口
        if command -v netstat &> /dev/null; then
            echo "端口监听:"
            netstat -tlnp 2>/dev/null | grep "${pid}" | grep -E "7789|8080" || echo "  未找到监听端口"
            echo ""
        fi
        
        # 最近日志
        echo "最近日志:"
        if [ -f "${LOG_FILE}" ]; then
            tail -5 "${LOG_FILE}"
        else
            echo "  日志文件不存在"
        fi
    else
        print_error "服务未运行"
        echo ""
        echo "启动命令:"
        echo "  $0 start"
    fi
}

# 查看日志
logs_service() {
    local lines="${1:-50}"
    
    if [ ! -f "${LOG_FILE}" ]; then
        print_error "日志文件不存在"
        return 1
    fi
    
    print_status "最近 ${lines} 行日志"
    echo ""
    tail -n "${lines}" "${LOG_FILE}"
}

# 清理进程
clean_service() {
    print_status "清理 Z-OPS WebChat 进程"
    
    local pids=$(pgrep -f "webchat_api.py" || true)
    
    if [ -z "${pids}" ]; then
        print_success "没有需要清理的进程"
        return 0
    fi
    
    print_warn "发现以下进程:"
    echo "${pids}"
    echo ""
    
    for pid in ${pids}; do
        echo -n "终止进程 ${pid}... "
        kill -9 "${pid}" 2>/dev/null && print_success "已终止" || print_error "失败"
    done
    
    rm -f "${PID_FILE}"
    
    print_success "清理完成"
}

# 显示帮助
show_help() {
    echo ""
    echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║          Z-OPS WebChat 服务管理工具                      ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "用法：$0 <命令> [选项]"
    echo ""
    echo "命令:"
    echo "  start       启动服务"
    echo "  stop        停止服务"
    echo "  restart     重启服务"
    echo "  status      查看状态"
    echo "  logs [n]    查看日志 (默认 50 行)"
    echo "  clean       清理进程"
    echo "  help        显示帮助"
    echo ""
    echo "示例:"
    echo "  $0 start          # 启动服务"
    echo "  $0 stop           # 停止服务"
    echo "  $0 restart        # 重启服务"
    echo "  $0 status         # 查看状态"
    echo "  $0 logs 100       # 查看最近 100 行日志"
    echo "  $0 clean          # 清理残留进程"
    echo ""
    echo "配置文件:"
    echo "  守护脚本：${DAEMON_SCRIPT}"
    echo "  PID 文件：${PID_FILE}"
    echo "  日志文件：${LOG_FILE}"
    echo ""
}

# 主函数
case "${1:-}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        status_service
        ;;
    logs)
        logs_service "${2:-50}"
        ;;
    clean)
        clean_service
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "未知命令：${1:-}"
        echo ""
        show_help
        exit 1
        ;;
esac
