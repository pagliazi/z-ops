#!/bin/bash
#
# Z-OPS 服务管理脚本
# 用法：./manage_zops.sh {start|stop|restart|status|logs}
#

set -e

BACKEND_SERVICE="zops-backend"
FRONTEND_SERVICE="zops-frontend"
LOGS_DIR="/opt/zops/logs"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}请使用 sudo 运行此脚本${NC}"
  exit 1
fi

# 确保日志目录存在
mkdir -p "$LOGS_DIR"

show_usage() {
  echo "用法：$0 {start|stop|restart|status|logs|enable|disable}"
  echo ""
  echo "命令说明:"
  echo "  start    - 启动所有服务 (先后端，后前端)"
  echo "  stop     - 停止所有服务 (先前端，后后端)"
  echo "  restart  - 重启所有服务"
  echo "  status   - 查看所有服务状态"
  echo "  logs     - 查看最近日志"
  echo "  enable   - 设置开机自启"
  echo "  disable  - 禁用开机自启"
  echo ""
  echo "单独控制:"
  echo "  $0 backend start|stop|restart|status|logs"
  echo "  $0 frontend start|stop|restart|status|logs"
}

service_exists() {
  systemctl cat "$1" &>/dev/null
}

start_backend() {
  if service_exists "$BACKEND_SERVICE"; then
    echo -e "${GREEN}启动后端服务...${NC}"
    systemctl start "$BACKEND_SERVICE"
    sleep 2
    status_backend
  else
    echo -e "${RED}后端服务文件不存在${NC}"
    exit 1
  fi
}

start_frontend() {
  if service_exists "$FRONTEND_SERVICE"; then
    echo -e "${GREEN}启动前端服务...${NC}"
    systemctl start "$FRONTEND_SERVICE"
    sleep 2
    status_frontend
  else
    echo -e "${RED}前端服务文件不存在${NC}"
    exit 1
  fi
}

stop_backend() {
  if service_exists "$BACKEND_SERVICE"; then
    echo -e "${YELLOW}停止后端服务...${NC}"
    systemctl stop "$BACKEND_SERVICE"
  fi
}

stop_frontend() {
  if service_exists "$FRONTEND_SERVICE"; then
    echo -e "${YELLOW}停止前端服务...${NC}"
    systemctl stop "$FRONTEND_SERVICE"
  fi
}

status_backend() {
  if service_exists "$BACKEND_SERVICE"; then
    systemctl is-active --quiet "$BACKEND_SERVICE" && \
      echo -e "${GREEN}✓ 后端服务运行中${NC}" || \
      echo -e "${RED}✗ 后端服务未运行${NC}"
  else
    echo -e "${RED}后端服务未安装${NC}"
  fi
}

status_frontend() {
  if service_exists "$FRONTEND_SERVICE"; then
    systemctl is-active --quiet "$FRONTEND_SERVICE" && \
      echo -e "${GREEN}✓ 前端服务运行中${NC}" || \
      echo -e "${RED}✗ 前端服务未运行${NC}"
  else
    echo -e "${RED}前端服务未安装${NC}"
  fi
}

logs_backend() {
  echo -e "${GREEN}=== 后端最近日志 ===${NC}"
  journalctl -u "$BACKEND_SERVICE" --no-pager -n 30
  echo ""
  echo -e "${GREEN}=== 后端错误日志 ===${NC}"
  tail -30 "$LOGS_DIR/webchat_stderr.log" 2>/dev/null || echo "无错误日志"
}

logs_frontend() {
  echo -e "${GREEN}=== 前端最近日志 ===${NC}"
  journalctl -u "$FRONTEND_SERVICE" --no-pager -n 30
  echo ""
  echo -e "${GREEN}=== 前端错误日志 ===${NC}"
  tail -30 "$LOGS_DIR/frontend_stderr.log" 2>/dev/null || echo "无错误日志"
}

start_all() {
  echo -e "${GREEN}启动 Z-OPS 服务...${NC}"
  start_backend
  echo -e "${YELLOW}等待后端启动完成...${NC}"
  sleep 3
  start_frontend
  echo -e "${GREEN}✓ 所有服务已启动${NC}"
}

stop_all() {
  echo -e "${YELLOW}停止 Z-OPS 服务...${NC}"
  stop_frontend
  stop_backend
  echo -e "${GREEN}✓ 所有服务已停止${NC}"
}

restart_all() {
  echo -e "${YELLOW}重启 Z-OPS 服务...${NC}"
  stop_all
  sleep 2
  start_all
}

status_all() {
  echo "=== Z-OPS 服务状态 ==="
  echo ""
  status_backend
  status_frontend
  echo ""
  
  # 显示详细状态
  echo "=== 详细状态 ==="
  if service_exists "$BACKEND_SERVICE"; then
    systemctl status "$BACKEND_SERVICE" --no-pager -n 5
  fi
  echo ""
  if service_exists "$FRONTEND_SERVICE"; then
    systemctl status "$FRONTEND_SERVICE" --no-pager -n 5
  fi
}

logs_all() {
  logs_backend
  echo ""
  logs_frontend
}

enable_all() {
  echo -e "${GREEN}设置开机自启...${NC}"
  if service_exists "$BACKEND_SERVICE"; then
    systemctl enable "$BACKEND_SERVICE"
    echo "✓ 后端服务已启用开机自启"
  fi
  if service_exists "$FRONTEND_SERVICE"; then
    systemctl enable "$FRONTEND_SERVICE"
    echo "✓ 前端服务已启用开机自启"
  fi
}

disable_all() {
  echo -e "${YELLOW}禁用开机自启...${NC}"
  if service_exists "$BACKEND_SERVICE"; then
    systemctl disable "$BACKEND_SERVICE"
    echo "✓ 后端服务已禁用开机自启"
  fi
  if service_exists "$FRONTEND_SERVICE"; then
    systemctl disable "$FRONTEND_SERVICE"
    echo "✓ 前端服务已禁用开机自启"
  fi
}

# 主逻辑
case "${1:-}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    restart_all
    ;;
  status)
    status_all
    ;;
  logs)
    logs_all
    ;;
  enable)
    enable_all
    ;;
  disable)
    disable_all
    ;;
  backend)
    case "${2:-}" in
      start) start_backend ;;
      stop) stop_backend ;;
      restart) systemctl restart "$BACKEND_SERVICE" ;;
      status) status_backend ;;
      logs) logs_backend ;;
      *) echo "用法：$0 backend {start|stop|restart|status|logs}" ;;
    esac
    ;;
  frontend)
    case "${2:-}" in
      start) start_frontend ;;
      stop) stop_frontend ;;
      restart) systemctl restart "$FRONTEND_SERVICE" ;;
      status) status_frontend ;;
      logs) logs_frontend ;;
      *) echo "用法：$0 frontend {start|stop|restart|status|logs}" ;;
    esac
    ;;
  *)
    show_usage
    ;;
esac

exit 0
