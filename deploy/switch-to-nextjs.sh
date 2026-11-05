#!/bin/bash
# ===========================================
# Z-OPS 前端切换到 Next.js 部署脚本
# ===========================================

set -e

echo "═══════════════════════════════════════════"
echo "  Z-OPS 前端切换到 Next.js"
echo "═══════════════════════════════════════════"

# 1. 复制Nginx配置
echo ""
echo ">>> 1. 更新 Nginx 配置..."
sudo cp /opt/zops/deploy/nginx-nextjs.conf /etc/nginx/sites-available/opscore-ai.example.com

# 2. 测试Nginx配置
echo ""
echo ">>> 2. 测试 Nginx 配置..."
sudo nginx -t

# 3. 重载Nginx
echo ""
echo ">>> 3. 重载 Nginx..."
sudo systemctl reload nginx

# 4. 清理冗余前端目录
echo ""
echo ">>> 4. 清理冗余前端目录..."
if [ -d "/opt/zops/frontend" ]; then
    rm -rf /opt/zops/frontend/
    echo "    ✓ 已删除 frontend/ 目录"
else
    echo "    - frontend/ 目录不存在，跳过"
fi

# 5. 确认Next.js服务运行中
echo ""
echo ">>> 5. 检查 Next.js 服务..."
if pgrep -f "next-server" > /dev/null; then
    echo "    ✓ Next.js 服务运行中 (端口 3000)"
else
    echo "    ⚠ Next.js 服务未运行，正在启动..."
    cd /opt/zops/web-next && npm run start &
    sleep 3
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ 部署完成！"
echo ""
echo "  访问地址: https://opscore-ai.example.com"
echo ""
echo "  架构说明:"
echo "    /      → Next.js 前端 (端口 3000)"
echo "    /api/  → FastAPI 后端 (端口 7789)"
echo "═══════════════════════════════════════════"