#!/bin/bash
#
# Z-OPS 服务部署脚本
# 自动部署前后端 systemd 服务
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_SERVICE="$SCRIPT_DIR/zops-backend.service"
FRONTEND_SERVICE="$SCRIPT_DIR/zops-frontend.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Z-OPS 服务部署 ==="
echo ""

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
  echo "错误：请使用 sudo 运行此脚本"
  exit 1
fi

# 确保 systemd 目录存在
if [ ! -d "$SYSTEMD_DIR" ]; then
  echo "错误：systemd 目录不存在：$SYSTEMD_DIR"
  exit 1
fi

# 复制后端服务
if [ -f "$BACKEND_SERVICE" ]; then
  echo "部署后端服务..."
  cp "$BACKEND_SERVICE" "$SYSTEMD_DIR/zops-backend.service"
  chmod 644 "$SYSTEMD_DIR/zops-backend.service"
  echo "✓ 后端服务已部署"
else
  echo "警告：后端服务文件不存在：$BACKEND_SERVICE"
fi

# 复制前端服务
if [ -f "$FRONTEND_SERVICE" ]; then
  echo "部署前端服务..."
  cp "$FRONTEND_SERVICE" "$SYSTEMD_DIR/zops-frontend.service"
  chmod 644 "$SYSTEMD_DIR/zops-frontend.service"
  echo "✓ 前端服务已部署"
else
  echo "警告：前端服务文件不存在：$FRONTEND_SERVICE"
fi

# 重新加载 systemd
echo "重新加载 systemd 配置..."
systemctl daemon-reload
echo "✓ systemd 配置已重新加载"

# 显示状态
echo ""
echo "=== 服务状态 ==="
systemctl status zops-backend.service --no-pager -n 0 || true
systemctl status zops-frontend.service --no-pager -n 0 || true

echo ""
echo "=== 使用方法 ==="
echo ""
echo "启动所有服务:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh start"
echo ""
echo "停止所有服务:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh stop"
echo ""
echo "重启所有服务:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh restart"
echo ""
echo "查看状态:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh status"
echo ""
echo "查看日志:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh logs"
echo ""
echo "单独控制后端:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh backend start|stop|restart|status|logs"
echo ""
echo "单独控制前端:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh frontend start|stop|restart|status|logs"
echo ""
echo "设置开机自启:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh enable"
echo ""
echo "禁用开机自启:"
echo "  sudo $SCRIPT_DIR/manage_zops.sh disable"
echo ""
echo "✓ 部署完成！"
