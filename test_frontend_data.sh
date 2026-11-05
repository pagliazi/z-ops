#!/bin/bash
#
# Z-OPS 前端数据验证脚本
#

echo "=== Z-OPS 前端数据验证 ==="
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

# 1. 检查后端 API
echo "1. 检查后端 API 连通性..."
BACKEND_RESP=$(curl -s http://localhost:7789/api/auth/ldap-status 2>&1)
if echo "$BACKEND_RESP" | grep -q "ldap_enabled"; then
  check_pass "后端 API 响应正常"
else
  check_fail "后端 API 无响应"
  echo "   响应：$BACKEND_RESP"
fi

# 2. 检查前端代理
echo ""
echo "2. 检查前端 API 代理..."
FRONTEND_RESP=$(curl -s http://localhost:3000/api/auth/ldap-status 2>&1)
if echo "$FRONTEND_RESP" | grep -q "ldap_enabled"; then
  check_pass "前端 API 代理正常"
else
  check_fail "前端 API 代理失败"
  echo "   响应：$FRONTEND_RESP"
fi

# 3. 检查 Redis 数据
echo ""
echo "3. 检查 Redis 数据..."
SESSION_COUNT=$(redis-cli -h 127.0.0.1 -p 6379 ZCARD "zops:sessions:svc-opscore" 2>&1)
if [ "$SESSION_COUNT" -gt 0 ]; then
  check_pass "svc-opscore 用户有 $SESSION_COUNT 个会话"
else
  check_fail "svc-opscore 用户无会话数据"
fi

# 4. 测试会话 API
echo ""
echo "4. 测试会话列表 API..."
SESSIONS_RESP=$(curl -s -H "Authorization: Bearer test_token" http://localhost:7789/api/chat/sessions 2>&1)
if echo "$SESSIONS_RESP" | grep -q "sessions"; then
  check_pass "会话列表 API 正常"
  echo "$SESSIONS_RESP" | python3 -m json.tool | head -20
else
  check_fail "会话列表 API 异常"
  echo "   响应：$SESSIONS_RESP"
fi

# 5. 检查服务状态
echo ""
echo "5. 检查服务状态..."
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

echo ""
echo "=== 验证完成 ==="
echo ""
echo "提示："
echo "  - 访问前端：http://localhost:3000"
echo "  - 使用 svc-opscore 登录"
echo "  - 如果还是没数据，请清除浏览器缓存 (Ctrl+Shift+R 强制刷新)"
echo ""
