#!/bin/bash
# ═══ Z-OPS 运维智能体 — 部署脚本 ═══
# 使用方法: bash deploy/setup.sh

set -e

ZOPS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ZOPS_DIR/.venv"
LOG_DIR="$ZOPS_DIR/logs"
USER=$(whoami)

echo "╔══════════════════════════════════════╗"
echo "║     Z-OPS 运维智能体 — 部署脚本       ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "项目目录: $ZOPS_DIR"
echo "当前用户: $USER"
echo ""

# 1. Python 虚拟环境
echo ">>> [1/5] 创建 Python 虚拟环境..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "    虚拟环境已创建"
else
    echo "    虚拟环境已存在"
fi

source "$VENV_DIR/bin/activate"

# 2. 安装依赖
echo ">>> [2/5] 安装 Python 依赖..."
pip install -U pip -q
pip install -r "$ZOPS_DIR/requirements.txt" -q
echo "    依赖安装完成"

# 3. 创建日志目录
echo ">>> [3/5] 创建目录..."
mkdir -p "$LOG_DIR" "$ZOPS_DIR/screenshots"
echo "    目录已创建"

# 4. 检查 Redis
echo ">>> [4/5] 检查 Redis..."
if redis-cli ping > /dev/null 2>&1; then
    echo "    Redis 运行正常"
else
    echo "    ⚠️  Redis 未运行，请先启动: sudo systemctl start redis-server"
fi

# 5. 生成 systemd 服务
echo ">>> [5/5] 生成 systemd 服务文件..."

# Agent 通用模板
cat > "$ZOPS_DIR/deploy/zops@.service" << SVCEOF
[Unit]
Description=Z-OPS %i
After=redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$ZOPS_DIR
Environment=PYTHONPATH=$ZOPS_DIR
ExecStart=$VENV_DIR/bin/python -m agents.%i
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/%i.log
StandardError=append:$LOG_DIR/%i.err

[Install]
WantedBy=multi-user.target
SVCEOF

# Web Dashboard
cat > "$ZOPS_DIR/deploy/zops-webchat.service" << SVCEOF
[Unit]
Description=Z-OPS Web Dashboard
After=redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$ZOPS_DIR
Environment=PYTHONPATH=$ZOPS_DIR
ExecStart=$VENV_DIR/bin/python webchat_api.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/webchat.log
StandardError=append:$LOG_DIR/webchat.err

[Install]
WantedBy=multi-user.target
SVCEOF

# 飞书 Bot
cat > "$ZOPS_DIR/deploy/zops-feishu.service" << SVCEOF
[Unit]
Description=Z-OPS Feishu Bot
After=redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$ZOPS_DIR
Environment=PYTHONPATH=$ZOPS_DIR
ExecStart=$VENV_DIR/bin/python feishu_bot.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/feishu.log
StandardError=append:$LOG_DIR/feishu.err

[Install]
WantedBy=multi-user.target
SVCEOF

echo "    服务文件已生成到 $ZOPS_DIR/deploy/"
echo ""
echo "════════════════════════════════════════"
echo "部署完成！后续步骤:"
echo ""
echo "1. 编辑配置文件:"
echo "   vim $ZOPS_DIR/zops.env"
echo ""
echo "2. 安装 systemd 服务 (需要 sudo):"
echo "   sudo cp $ZOPS_DIR/deploy/zops@.service /etc/systemd/system/"
echo "   sudo cp $ZOPS_DIR/deploy/zops-webchat.service /etc/systemd/system/"
echo "   sudo cp $ZOPS_DIR/deploy/zops-feishu.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo ""
echo "3. 启动核心服务:"
echo "   for svc in orchestrator general_agent ops_agent network_agent vm_agent dev_agent browser_agent gcoc_agent; do"
echo "       sudo systemctl enable --now zops@\${svc}"
echo "   done"
echo "   sudo systemctl enable --now zops-webchat"
echo "   sudo systemctl enable --now zops-feishu"
echo ""
echo "4. 或者手动启动 (开发模式):"
echo "   cd $ZOPS_DIR && bash deploy/start-dev.sh"
echo ""
echo "5. Web Dashboard: http://localhost:7789"
echo "   用户: admin / 密码: zops_admin_2024"
echo "════════════════════════════════════════"
