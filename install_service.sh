#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Z-OPS WebChat systemd 服务安装脚本
# ═══════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="${SCRIPT_DIR}/zops-webchat.service"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="zops-webchat"

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

# 检查 systemd 是否可用
if ! command -v systemctl &> /dev/null; then
    print_error "系统不支持 systemd"
    exit 1
fi

print_info "正在安装 ${SERVICE_NAME} 服务..."

# 确保日志目录存在
mkdir -p "${SCRIPT_DIR}/logs"
print_info "创建日志目录：${SCRIPT_DIR}/logs"

# 复制服务文件到 systemd 目录
cp "${SERVICE_FILE}" "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
print_success "服务文件已复制到 ${SYSTEMD_DIR}/${SERVICE_NAME}.service"

# 重新加载 systemd 配置
systemctl daemon-reload
print_success "systemd 配置已重新加载"

# 启用服务（开机自启）
systemctl enable "${SERVICE_NAME}"
print_success "服务已启用（开机自启）"

# 启动服务
systemctl start "${SERVICE_NAME}"
print_success "服务已启动"

# 显示状态
echo ""
print_info "服务状态："
systemctl status "${SERVICE_NAME}" --no-pager

echo ""
echo "════════════════════════════════════════════════"
echo ""
print_success "Z-OPS WebChat 服务已成功安装并启动！"
echo ""
echo "常用命令："
echo "  启动服务：     systemctl start ${SERVICE_NAME}"
echo "  停止服务：     systemctl stop ${SERVICE_NAME}"
echo "  重启服务：     systemctl restart ${SERVICE_NAME}"
echo "  查看状态：     systemctl status ${SERVICE_NAME}"
echo "  查看日志：     journalctl -u ${SERVICE_NAME} -f"
echo "  禁用自启：     systemctl disable ${SERVICE_NAME}"
echo ""
echo "服务文件位置："
echo "  ${SYSTEMD_DIR}/${SERVICE_NAME}.service"
echo ""
echo "访问地址："
echo "  http://localhost:7789"
echo ""
echo "════════════════════════════════════════════════"
