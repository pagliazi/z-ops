#!/bin/bash
# Z-OPS 前端美化优化验证脚本

echo "======================================"
echo "  Z-OPS 前端美化优化验证"
echo "======================================"
echo ""

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查文件备份
echo "📦 检查文件备份..."
if [ -f "webchat_frontend_backup.html" ]; then
    echo -e "${GREEN}✓${NC} 备份文件已创建"
else
    echo -e "${YELLOW}⚠${NC} 备份文件不存在，创建备份..."
    cp webchat_frontend.html webchat_frontend_backup_$(date +%Y%m%d_%H%M%S).html
    echo -e "${GREEN}✓${NC} 备份已创建"
fi
echo ""

# 检查 CSS 优化
echo "🎨 检查 CSS 优化..."
if grep -q "gradientShift" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} 渐变背景动画已添加"
else
    echo -e "${RED}✗${NC} 渐变背景动画未添加"
fi

if grep -q "glass-container" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} 玻璃态效果已添加"
else
    echo -e "${RED}✗${NC} 玻璃态效果未添加"
fi

if grep -q "login-logo" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} Logo 浮动动画已添加"
else
    echo -e "${RED}✗${NC} Logo 浮动动画未添加"
fi

if grep -q "message-bubble" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} 消息气泡动画已添加"
else
    echo -e "${RED}✗${NC} 消息气泡动画未添加"
fi

if grep -q "typing-indicator" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} 打字指示器已添加"
else
    echo -e "${RED}✗${NC} 打字指示器未添加"
fi

echo ""

# 检查登录界面优化
echo "🔐 检查登录界面优化..."
if grep -q "ri-brain-line" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} Brain 图标已更新"
else
    echo -e "${YELLOW}⚠${NC} Brain 图标未更新（可选）"
fi

if grep -q "input-field" webchat_frontend.html | grep -q "rounded-xl"; then
    echo -e "${GREEN}✓${NC} 输入框样式已优化"
else
    echo -e "${YELLOW}⚠${NC} 输入框样式可能需要手动调整"
fi

if grep -q "btn-gradient" webchat_frontend.html; then
    echo -e "${GREEN}✓${NC} 按钮渐变样式已添加"
else
    echo -e "${RED}✗${NC} 按钮渐变样式未添加"
fi

echo ""

# 统计优化数据
echo "📊 统计优化数据..."
ORIGINAL_LINES=$(wc -l < webchat_frontend_backup.html 2>/dev/null || echo "0")
CURRENT_LINES=$(wc -l < webchat_frontend.html)
ADDED_LINES=$((CURRENT_LINES - ORIGINAL_LINES))

echo "  原始文件行数：$ORIGINAL_LINES"
echo "  当前文件行数：$CURRENT_LINES"
echo -e "  ${GREEN}新增行数：+$ADDED_LINES${NC}"
echo ""

# 检查新增的 CSS 类
echo "📋 新增 CSS 类检查..."
CSS_CLASSES=(
    "gradientShift"
    "glass-container"
    "login-logo"
    "animate-float"
    "input-field-enhanced"
    "btn-gradient"
    "message-bubble"
    "messageSlide"
    "loading-spinner"
    "typing-indicator"
    "typing-dot"
    "card-enhanced"
    "table-enhanced"
    "status-indicator"
)

for class in "${CSS_CLASSES[@]}"; do
    if grep -q "$class" webchat_frontend.html; then
        echo -e "${GREEN}✓${NC} $class"
    else
        echo -e "${RED}✗${NC} $class"
    fi
done

echo ""

# 服务状态检查
echo "🚀 服务状态检查..."
if pgrep -f "python.*webchat_api.py" > /dev/null; then
    echo -e "${GREEN}✓${NC} WebChat API 服务正在运行"
    echo -e "  访问地址：${YELLOW}http://localhost:8000${NC}"
    echo -e "  ${YELLOW}提示：${NC} 刷新页面查看优化效果 (Ctrl+F5 强制刷新)"
else
    echo -e "${YELLOW}⚠${NC} WebChat API 服务未运行"
    echo -e "  启动命令：${YELLOW}python webchat_api.py${NC}"
fi

echo ""
echo "======================================"
echo "  验证完成"
echo "======================================"
echo ""
echo "📝 下一步操作："
echo "1. 启动/重启服务：python webchat_api.py"
echo "2. 浏览器访问：http://localhost:8000"
echo "3. 强制刷新页面：Ctrl+F5 或 Cmd+Shift+R"
echo "4. 验证优化效果："
echo "   - 紫色渐变背景（缓慢流动）"
echo "   - 登录框玻璃态效果"
echo "   - Logo 上下浮动动画"
echo "   - 按钮悬停上移效果"
echo "   - 输入框聚焦高亮"
echo "   - 美化滚动条"
echo ""
echo "📖 查看详细文档："
echo "   - frontend_optimization_summary.md"
echo "   - FRONTEND_ENHANCEMENT_GUIDE.md"
echo ""
