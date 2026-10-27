#!/bin/bash
#
# Z-OPS 前端页面完整性验证脚本
#

echo "=== Z-OPS 前端页面完整性验证 ==="
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

# 1. 检查服务状态
echo "1. 检查服务状态..."
if systemctl is-active --quiet zops-backend; then
  check_pass "后端服务运行中"
else
  check_fail "后端服务未运行"
fi

if systemctl is-active --quiet zops-frontend; then
  check_pass "前端服务运行中"
else
  check_fail "前端服务未运行"
fi

# 2. 检查端口监听
echo ""
echo "2. 检查端口监听..."
netstat -tlnp 2>/dev/null | grep -q ":7789"
if [ $? -eq 0 ]; then
  check_pass "后端端口 7789 正在监听"
else
  check_fail "后端端口 7789 未监听"
fi

netstat -tlnp 2>/dev/null | grep -q ":3000"
if [ $? -eq 0 ]; then
  check_pass "前端端口 3000 正在监听"
else
  check_fail "前端端口 3000 未监听"
fi

# 3. 检查 API 响应
echo ""
echo "3. 检查 API 响应..."
BACKEND_RESP=$(curl -s http://localhost:7789/api/auth/ldap-status 2>&1)
if echo "$BACKEND_RESP" | grep -q "ldap_enabled"; then
  check_pass "后端 API 响应正常"
else
  check_fail "后端 API 无响应"
  echo "   响应：$BACKEND_RESP"
fi

FRONTEND_RESP=$(curl -s http://localhost:3000/api/auth/ldap-status 2>&1)
if echo "$FRONTEND_RESP" | grep -q "ldap_enabled"; then
  check_pass "前端 API 代理正常"
else
  check_fail "前端 API 代理失败"
fi

# 4. 检查页面内容
echo ""
echo "4. 检查页面内容..."
PAGE_HTML=$(curl -s http://localhost:3000/ 2>&1)

# 检查登录页面
if echo "$PAGE_HTML" | grep -q "AD 域登录"; then
  check_pass "登录页面显示正常"
else
  check_fail "登录页面显示异常"
fi

# 检查页面标题
if echo "$PAGE_HTML" | grep -q "智 OC - Operation Claw"; then
  check_pass "页面标题正确"
else
  check_fail "页面标题错误"
fi

# 检查 CSS 加载
if echo "$PAGE_HTML" | grep -q "_next/static/chunks/.*.css"; then
  check_pass "CSS 文件引用正常"
else
  check_fail "CSS 文件引用缺失"
fi

# 检查 JS 加载
if echo "$PAGE_HTML" | grep -q "_next/static/chunks/.*.js"; then
  check_pass "JS 文件引用正常"
else
  check_fail "JS 文件引用缺失"
fi

# 5. 检查 Redis 数据
echo ""
echo "5. 检查 Redis 数据..."
SESSION_COUNT=$(redis-cli -h 127.0.0.1 -p 6379 ZCARD "zops:sessions:svc-gcoc" 2>&1)
if [ "$SESSION_COUNT" -gt 0 ]; then
  check_pass "svc-gcoc 用户有 $SESSION_COUNT 个会话"
else
  check_fail "svc-gcoc 用户无会话数据"
fi

# 6. 检查静态资源
echo ""
echo "6. 检查静态资源..."
CSS_CHECK=$(curl -s http://localhost:3000/_next/static 2>&1)
if echo "$CSS_CHECK" | grep -q "404\|This page could not be found"; then
  check_fail "静态资源目录返回 404"
else
  check_pass "静态资源目录访问正常"
fi

echo ""
echo "=== 验证完成 ==="
echo ""
echo "访问地址："
echo "  - 前端页面：http://localhost:3000"
echo "  - 后端 API: http://localhost:7789"
echo "  - JupyterLab: http://localhost:8888"
echo ""
echo "如果前端还是显示不完整，请:"
echo "  1. 清除浏览器缓存 (Ctrl+Shift+R)"
echo "  2. 检查浏览器窗口大小是否正常"
echo "  3. 按 F12 检查是否有 JavaScript 错误"
echo "  4. 尝试使用无痕模式打开"
echo ""
