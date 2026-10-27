#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Z-OPS WebChat 服务守护脚本
# 功能：自动启动、异常重启、日志监控、优雅关闭
# ═══════════════════════════════════════════════════════════

set -e

# 配置项
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_CMD="${PYTHON_CMD:-python3}"
WEBCHAT_SCRIPT="${SCRIPT_DIR}/webchat_api.py"
LOG_DIR="${SCRIPT_DIR}/logs"
PID_FILE="${LOG_DIR}/webchat.pid"
LOG_FILE="${LOG_DIR}/webchat_daemon.log"
MAX_RESTART_COUNT=5
RESTART_DELAY=5
HEALTH_CHECK_PORT="${WEBCHAT_PORT:-7789}"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log() {
    local level="$1"
    local message="$2"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${timestamp} [${level}] ${message}" | tee -a "${LOG_FILE}"
}

log_info() {
    log "INFO" "${GREEN}$1${NC}"
}

log_warn() {
    log "WARN" "${YELLOW}$1${NC}"
}

log_error() {
    log "ERROR" "${RED}$1${NC}"
}

# 确保目录存在
mkdir -p "${LOG_DIR}"

# 检查文件是否存在
if [ ! -f "${WEBCHAT_SCRIPT}" ]; then
    log_error "脚本文件不存在：${WEBCHAT_SCRIPT}"
    exit 1
fi

# 检查 Python 是否可用
if ! command -v "${PYTHON_CMD}" &> /dev/null; then
    log_error "Python 命令不存在：${PYTHON_CMD}"
    exit 1
fi

# 清理函数
cleanup() {
    log_info "正在关闭服务..."
    if [ -f "${PID_FILE}" ]; then
        local pid=$(cat "${PID_FILE}")
        if kill -0 "${pid}" 2>/dev/null; then
            log_info "发送 SIGTERM 到进程 ${pid}"
            kill -TERM "${pid}" 2>/dev/null || true
            
            # 等待进程结束
            local count=0
            while kill -0 "${pid}" 2>/dev/null && [ $count -lt 10 ]; do
                sleep 1
                count=$((count + 1))
            done
            
            # 如果还在运行，强制终止
            if kill -0 "${pid}" 2>/dev/null; then
                log_warn "进程未响应，发送 SIGKILL"
                kill -9 "${pid}" 2>/dev/null || true
            fi
            
            log_info "服务已停止"
        else
            log_warn "进程 ${pid} 不存在"
        fi
        rm -f "${PID_FILE}"
    else
        log_info "PID 文件不存在，跳过清理"
    fi
    
    # 清理可能残留的进程
    local remaining_pids=$(pgrep -f "webchat_api.py" || true)
    if [ -n "${remaining_pids}" ]; then
        log_warn "发现残留进程，清理中..."
        echo "${remaining_pids}" | xargs kill -9 2>/dev/null || true
    fi
    
    exit 0
}

# 设置信号处理器
trap cleanup SIGTERM SIGINT SIGQUIT

# 健康检查函数
health_check() {
    local max_attempts=30
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        if curl -s "http://localhost:${HEALTH_CHECK_PORT}/" > /dev/null 2>&1; then
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    
    return 1
}

# 主循环
restart_count=0
last_start_time=0

while true; do
    current_time=$(date +%s)
    
    # 检查是否在短时间内频繁重启
    time_diff=$((current_time - last_start_time))
    if [ $time_diff -lt 10 ] && [ $restart_count -ge $MAX_RESTART_COUNT ]; then
        log_error "服务在 10 秒内重启超过 ${MAX_RESTART_COUNT} 次，停止自动重启"
        log_error "请检查日志文件：${LOG_DIR}/webchat.log"
        exit 1
    fi
    
    # 重置重启计数（如果距离上次启动已超过 1 分钟）
    if [ $time_diff -gt 60 ]; then
        restart_count=0
    fi
    
    # 启动服务
    log_info "启动 WebChat 服务 (第 $((restart_count + 1)) 次尝试)..."
    
    # 记录启动时间
    last_start_time=$(date +%s)
    
    # 启动进程（后台运行）
    cd "${SCRIPT_DIR}"
    nohup "${PYTHON_CMD}" "${WEBCHAT_SCRIPT}" > "${LOG_DIR}/webchat_stdout.log" 2>&1 &
    WEBCHAT_PID=$!
    
    # 保存 PID
    echo "${WEBCHAT_PID}" > "${PID_FILE}"
    
    log_info "服务已启动，PID: ${WEBCHAT_PID}"
    
    # 等待 5 秒检查是否启动成功
    sleep 5
    
    # 检查进程是否还在运行
    if ! kill -0 "${WEBCHAT_PID}" 2>/dev/null; then
        log_error "服务启动失败，查看日志：${LOG_DIR}/webchat_stdout.log"
        restart_count=$((restart_count + 1))
        sleep "${RESTART_DELAY}"
        continue
    fi
    
    # 健康检查
    if ! health_check; then
        log_warn "健康检查失败，但进程仍在运行"
    else
        log_info "✓ 健康检查通过"
    fi
    
    # 监控进程
    while kill -0 "${WEBCHAT_PID}" 2>/dev/null; do
        wait "${WEBCHAT_PID}" 2>/dev/null || true
    done
    
    # 如果到达这里，说明进程已退出
    exit_code=$?
    log_warn "服务已退出，退出码：${exit_code}"
    
    restart_count=$((restart_count + 1))
    
    if [ $exit_code -eq 0 ]; then
        log_info "服务正常退出"
        break
    elif [ $exit_code -eq 130 ]; then
        log_info "服务被用户中断 (Ctrl+C)"
        break
    elif [ $exit_code -eq 143 ]; then
        log_info "服务收到 SIGTERM 信号"
        break
    else
        log_warn "服务异常退出，${RESTART_DELAY} 秒后重启..."
        sleep "${RESTART_DELAY}"
    fi
done

log_info "守护进程已退出"
