# ═══════════════════════════════════════════════════════════
# Z-OPS WebChat 快速启动指南
# ═══════════════════════════════════════════════════════════

## 📋 目录

1. [快速启动](#快速启动)
2. [服务管理](#服务管理)
3. [日志查看](#日志查看)
4. [桌面接管验收](#-桌面接管验收linux--x11)
5. [故障排查](#故障排查)

---

## 🚀 快速启动

### 方式 1：使用管理脚本（推荐）

```bash
cd /opt/zops

# 启动服务
bash manage_webchat.sh start

# 查看状态
bash manage_webchat.sh status

# 访问服务
# 浏览器打开：http://localhost:7789
```

### 方式 2：直接运行（开发调试）

```bash
cd /opt/zops
python webchat_api.py
```

### 方式 3：使用守护脚本（生产环境）

```bash
cd /opt/zops
bash start_webchat.sh
```

---

## 🛠️ 服务管理

### 启动服务
```bash
bash manage_webchat.sh start
```

### 停止服务
```bash
bash manage_webchat.sh stop
```

### 重启服务
```bash
bash manage_webchat.sh restart
```

### 查看状态
```bash
bash manage_webchat.sh status
```

输出示例：
```
╔════════════════════════════════════════════════════════╗
║          Z-OPS WebChat 服务状态                          ║
╚════════════════════════════════════════════════════════╝

✓ 服务正在运行

进程信息:
    PID    PPID USER     %CPU %MEM     ELAPSED COMMAND
  12345   12344 root      2.5  1.2  00:15:32 python3 webchat_api.py

端口监听:
  tcp   0.0.0.0:7789  LISTEN  12345/python3

最近日志:
  2026-03-18 20:30:00 [INFO] Z-OPS WebChat 启动
  2026-03-18 20:30:05 [INFO] 健康检查通过
```

### 清理残留进程
```bash
bash manage_webchat.sh clean
```

---

## 📂 日志查看

### 查看最近 50 行日志
```bash
bash manage_webchat.sh logs
```

### 查看最近 100 行日志
```bash
bash manage_webchat.sh logs 100
```

### 实时查看日志
```bash
tail -f logs/webchat.log
```

### 查看守护进程日志
```bash
tail -f logs/daemon_stdout.log
```

---

## 🖥️ 桌面接管验收（Linux / X11）

桌面接管默认全关，且必须先在本机做一遍人工验收。验收记录只写在本机，**不**等于开放远程控制：
每次接管仍要你在桌面上点一次“允许”，租约 60 秒，对话框 30 秒无人操作自动拒绝（原因码
`desktop_approval_timeout`）。

> Wayland 会话（含 XWayland）不支持，`--doctor` 会报 `wayland_portal_not_implemented`；
> X11 认不出密码框，所以 Linux 不声明 `desktop.screen`、不做窗口采集。

### 1. 配对并授权要操作的应用

```bash
# companion 要求 Python 3.11+；源码用仓库内 venv，交付用打包好的可执行文件
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json \
  --server https://gcoc-ai.example.com --pair <一次性配对码> --name "$(hostname)"

# 只授权该进程的窗口；未列出的应用与安全桌面一律不可操作
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json --allow-desktop-app firefox

# 看实际能力与不可用原因
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json --doctor
```

### 2. 本机验收（必须有人坐在机器前）

```bash
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json --accept-desktop
```

预检全过才会弹原生对话框；对话框 90 秒无人回应按 `local_user_timeout` 结束，有人点“不写入”
才是 `local_user_declined`。两种都不写记录。

### 3. 逐步跑 6 项人工步骤并留下证据

```bash
companion/.venv/bin/python scripts/linux_desktop_attended.py --config ~/.config/zops/config.json \
  --output ~/zops-linux-attended.json
```

默认顺序：approve → reject → timeout → typing_interference → lock_screen → revoke。每一步都给出
动作与判定标准，脚本观察真实状态（租约时长、原因码、焦点、外部输入、热键、锁屏）后写证据；
观察不到就判失败。`--step approve` 可只跑一项，`--stop-on-fail` 遇错即停。

- 对话框在按回车**之后**才弹出，别提前去找按钮；
- `Ctrl+Alt+Pause` 是紧急停止：租约立即作废，再接管要重新批准；
- `lock_screen` 需要本机真有锁屏器（GNOME/KDE/XScreenSaver），没有锁屏器时该项只能是未通过。

### 4. 交给清单判定

```bash
companion/.venv/bin/python scripts/linux_desktop_check.py --config ~/.config/zops/config.json \
  --attended ~/zops-linux-attended.json
```

清单同时实测 8 项 auto（doctor 自洽、X11 会话、窗口枚举与 `_NET_WM_PID`、屏保查询、XTest 与
键盘映射、全局热键、控件树/采集必须被拒绝、托盘如实上报）。缺项、未做、未通过，结论都是
“未通过”；结果默认写入 `~/zops-linux-acceptance.json`。清单默认调用
`companion/.venv/bin/python -m companion`；没有该 venv，或要验打包产物时，用
`--companion <可执行文件路径>` 指定。

> 用 XTest 脚本扮演“机旁的人”只能证明链路能跑通，**不能**作为验收结论。

---

## 🔧 故障排查

### 问题 1：服务启动失败

**症状**: 启动后立即退出

**解决步骤**:
```bash
# 1. 查看详细错误
bash manage_webchat.sh logs

# 2. 查看守护进程日志
tail -f logs/daemon_stdout.log

# 3. 清理残留进程
bash manage_webchat.sh clean

# 4. 检查端口占用
netstat -tlnp | grep 7789

# 5. 重新启动
bash manage_webchat.sh restart
```

### 问题 2：服务无法访问

**症状**: 浏览器无法访问 http://localhost:7789

**解决步骤**:
```bash
# 1. 检查服务状态
bash manage_webchat.sh status

# 2. 检查端口监听
netstat -tlnp | grep 7789

# 3. 检查防火墙
sudo ufw status
sudo ufw allow 7789/tcp

# 4. 查看错误日志
grep "ERROR" logs/webchat.log | tail -20
```

### 问题 3：服务频繁重启

**症状**: 服务启动后不断重启

**解决步骤**:
```bash
# 1. 查看重启原因
grep -i "restart\|error\|exception" logs/webchat.log | tail -30

# 2. 检查系统资源
free -h
df -h

# 3. 检查 Redis 连接
redis-cli ping

# 4. 手动运行查看详细错误
python webchat_api.py 2>&1 | head -50
```

### 问题 4：内存占用过高

**症状**: 服务占用大量内存

**解决步骤**:
```bash
# 1. 查看进程内存
ps aux | grep webchat

# 2. 重启服务释放内存
bash manage_webchat.sh restart

# 3. 添加内存监控（可选）
# 在 webchat_api.py 中添加内存检查代码
```

---

## 📊 配置文件

### 环境变量配置

编辑 `.env` 文件：
```bash
# 服务端口
WEBCHAT_PORT=7789
WEBCHAT_HOST=0.0.0.0

# Redis 配置
REDIS_URL=redis://localhost:6379/0

# 日志级别
LOG_LEVEL=INFO
```

### 守护脚本配置

编辑 `start_webchat.sh`：
```bash
# 最大重启次数
MAX_RESTART_COUNT=5

# 重启延迟（秒）
RESTART_DELAY=5

# 健康检查端口
HEALTH_CHECK_PORT=7789
```

---

## 🔍 健康检查

### 手动检查

```bash
# 检查服务是否响应
curl http://localhost:7789/

# 检查 API 状态
curl http://localhost:7789/api/overview

# 检查端口
netstat -tlnp | grep 7789
```

### 自动监控

创建监控脚本 `/opt/zops/health_check.sh`：
```bash
#!/bin/bash
while true; do
    if ! curl -s "http://localhost:7789/" > /dev/null; then
        echo "[$(date)] 服务异常，尝试重启..." >> logs/health.log
        bash manage_webchat.sh restart
    fi
    sleep 60
done
```

---

## 📈 性能优化

### 1. 调整进程数
```bash
# 在 webchat_api.py 中调整 uvicorn workers
# 建议：CPU 核心数 * 2 + 1
```

### 2. 优化日志级别
```bash
# 生产环境使用 WARNING
LOG_LEVEL=WARNING

# 开发环境使用 INFO 或 DEBUG
LOG_LEVEL=INFO
```

### 3. 定期清理日志
```bash
# 添加定时任务（crontab）
0 0 * * * find /opt/zops/logs -name "*.log" -mtime +7 -delete
```

---

## 🆘 获取帮助

### 查看所有命令
```bash
bash manage_webchat.sh help
```

### 查看进程信息
```bash
ps aux | grep webchat
```

### 查看网络监听
```bash
netstat -tlnp | grep python
```

### 查看系统资源
```bash
top -p $(pgrep -f webchat_api.py)
```

---

## 📞 技术支持

如遇到其他问题，请：

1. 查看详细日志：`bash manage_webchat.sh logs 200`
2. 检查系统配置：`cat .env`
3. 查看进程状态：`bash manage_webchat.sh status`
4. 联系系统管理员

---

**最后更新**: 2026-03-18  
**版本**: v2.0.0
