#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Z-OPS WebChat systemd 服务卸载脚本
# ═══════════════════════════════════════════════════════════

set -e

SERVICE_NAME="zops-webchat"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
    print_error "请以 root 用户运行此脚本 (sudo $0)"
    exit 1
fi

print_warn "即将卸载 ${SERVICE_NAME} 服务"
echo ""
read -p "是否继续？(y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_info "已取消"
    exit 0
fi

# 停止服务
print_info "正在停止服务..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || print_warn "服务未运行"
print_success "服务已停止"

# 禁用服务
print_info "正在禁用服务..."
systemctl disable "${SERVICE_NAME}" 2>/dev/null || print_warn "服务未启用"
print_success "服务已禁用"

# 删除服务文件
print_info "正在删除服务文件..."
rm -f "${SERVICE_FILE}"
print_success "服务文件已删除：${SERVICE_FILE}"

# 重新加载 systemd
print_info "正在重新加载 systemd 配置..."
systemctl daemon-reload
print_success "systemd 配置已重新加载"

# 清理日志（可选）
echo ""
print_warn "是否删除日志文件？"
read -p "删除所有日志？(y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    rm -rf "${SCRIPT_DIR}/logs"/*.log
    print_success "日志文件已删除"
fi

echo ""
print_success "${SERVICE_NAME} 服务已成功卸载！"
echo ""
print_info "如需重新安装，请运行："
echo "  sudo bash install_service.sh"
echo ""
