#!/bin/bash
#
# Z-OPS 服务配置验证脚本
#

set -e

echo "=== Z-OPS 服务配置验证 ==="
echo ""

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

check_pass() {
  echo -e "${GREEN}✓ $1${NC}"
}

check_fail() {
  echo -e "${RED}✗ $1${NC}"
}

check_warn() {
  echo -e "${YELLOW}⚠ $1${NC}"
}

# 1. 检查服务文件
echo "1. 检查服务文件..."
if [ -f "/etc/systemd/system/zops-backend.service" ]; then
  check_pass "后端服务文件存在"
else
  check_fail "后端服务文件不存在"
fi

if [ -f "/etc/systemd/system/zops-frontend.service" ]; then
  check_pass "前端服务文件存在"
else
  check_fail "前端服务文件不存在"
fi

# 2. 检查管理脚本
echo ""
echo "2. 检查管理脚本..."
if [ -x "/opt/zops/manage_zops.sh" ]; then
  check_pass "管理脚本存在且可执行"
else
  check_fail "管理脚本不存在或不可执行"
fi

if [ -x "/opt/zops/deploy_services.sh" ]; then
  check_pass "部署脚本存在且可执行"
else
  check_fail "部署脚本不存在或不可执行"
fi

# 3. 检查 systemd 状态
echo ""
echo "3. 检查 systemd 配置..."
if systemctl cat zops-backend &>/dev/null; then
  check_pass "后端服务已注册到 systemd"
else
  check_fail "后端服务未注册"
fi

if systemctl cat zops-frontend &>/dev/null; then
  check_pass "前端服务已注册到 systemd"
else
  check_fail "前端服务未注册"
fi

# 4. 检查服务状态
echo ""
echo "4. 检查服务运行状态..."
if systemctl is-active --quiet zops-backend; then
  check_pass "后端服务运行中"
else
  check_warn "后端服务未运行"
fi

if systemctl is-active --quiet zops-frontend; then
  check_pass "前端服务运行中"
else
  check_warn "前端服务未运行"
fi

# 5. 检查开机自启
echo ""
echo "5. 检查开机自启状态..."
if systemctl is-enabled --quiet zops-backend 2>/dev/null; then
  check_pass "后端服务已启用开机自启"
else
  check_warn "后端服务未启用开机自启"
fi

if systemctl is-enabled --quiet zops-frontend 2>/dev/null; then
  check_pass "前端服务已启用开机自启"
else
  check_warn "前端服务未启用开机自启"
fi

# 6. 检查依赖服务
echo ""
echo "6. 检查依赖服务..."
if systemctl is-active --quiet redis; then
  check_pass "Redis 服务运行中"
else
  check_warn "Redis 服务未运行（后端依赖 Redis）"
fi

# 7. 检查日志文件
echo ""
echo "7. 检查日志文件..."
LOGS_DIR="/opt/zops/logs"
if [ -d "$LOGS_DIR" ]; then
  check_pass "日志目录存在"
  ls -la "$LOGS_DIR"/*.log 2>/dev/null | wc -l | xargs -I {} check_pass "发现 {} 个日志文件"
else
  check_warn "日志目录不存在"
fi

# 8. 检查端口
echo ""
echo "8. 检查端口监听..."
if command -v netstat &>/dev/null; then
  if netstat -tlnp 2>/dev/null | grep -q ':7789'; then
    check_pass "后端端口 7789 正在监听"
  else
    check_warn "后端端口 7789 未监听"
  fi
  
  if netstat -tlnp 2>/dev/null | grep -q ':3000'; then
    check_pass "前端端口 3000 正在监听"
  else
    check_warn "前端端口 3000 未监听"
  fi
else
  check_warn "netstat 命令不可用，跳过端口检查"
fi

# 9. 显示服务信息
echo ""
echo "9. 服务详细信息..."
echo ""
echo "=== 后端服务 ==="
systemctl status zops-backend --no-pager -n 3 || true
echo ""
echo "=== 前端服务 ==="
systemctl status zops-frontend --no-pager -n 3 || true

echo ""
echo "=== 验证完成 ==="
echo ""
echo "提示:"
echo "  - 部署服务：sudo /opt/zops/deploy_services.sh"
echo "  - 启动服务：sudo /opt/zops/manage_zops.sh start"
echo "  - 查看状态：sudo /opt/zops/manage_zops.sh status"
echo "  - 查看日志：sudo /opt/zops/manage_zops.sh logs"
echo ""
