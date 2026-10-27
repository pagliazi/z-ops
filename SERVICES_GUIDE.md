# Z-OPS 服务管理指南

## 服务部署

首次部署或更新服务配置后，运行：

```bash
sudo /opt/zops/deploy_services.sh
```

这将：
- 复制服务文件到 `/etc/systemd/system/`
- 重新加载 systemd 配置
- 显示服务状态

## 服务管理

### 方式一：使用管理脚本（推荐）

```bash
# 启动所有服务（先后端，后前端）
sudo /opt/zops/manage_zops.sh start

# 停止所有服务（先前端，后后端）
sudo /opt/zops/manage_zops.sh stop

# 重启所有服务
sudo /opt/zops/manage_zops.sh restart

# 查看所有服务状态
sudo /opt/zops/manage_zops.sh status

# 查看最近日志
sudo /opt/zops/manage_zops.sh logs

# 设置开机自启
sudo /opt/zops/manage_zops.sh enable

# 禁用开机自启
sudo /opt/zops/manage_zops.sh disable
```

### 方式二：单独控制

```bash
# 后端服务
sudo /opt/zops/manage_zops.sh backend start
sudo /opt/zops/manage_zops.sh backend stop
sudo /opt/zops/manage_zops.sh backend restart
sudo /opt/zops/manage_zops.sh backend status
sudo /opt/zops/manage_zops.sh backend logs

# 前端服务
sudo /opt/zops/manage_zops.sh frontend start
sudo /opt/zops/manage_zops.sh frontend stop
sudo /opt/zops/manage_zops.sh frontend restart
sudo /opt/zops/manage_zops.sh frontend status
sudo /opt/zops/manage_zops.sh frontend logs
```

### 方式三：直接使用 systemctl

```bash
# 启动
sudo systemctl start zops-backend
sudo systemctl start zops-frontend

# 停止
sudo systemctl stop zops-backend
sudo systemctl stop zops-frontend

# 重启
sudo systemctl restart zops-backend
sudo systemctl restart zops-frontend

# 查看状态
sudo systemctl status zops-backend
sudo systemctl status zops-frontend

# 查看日志
sudo journalctl -u zops-backend -n 50
sudo journalctl -u zops-frontend -n 50

# 实时日志
sudo journalctl -u zops-backend -f
sudo journalctl -u zops-frontend -f

# 开机自启
sudo systemctl enable zops-backend
sudo systemctl enable zops-frontend

# 禁用自启
sudo systemctl disable zops-backend
sudo systemctl disable zops-frontend
```

## 服务说明

### zops-backend (后端 API 服务)
- **端口**: 7789
- **工作目录**: `/opt/zops`
- **启动命令**: `python3 webchat_api.py`
- **日志文件**: 
  - 标准输出：`/opt/zops/logs/webchat_stdout.log`
  - 错误输出：`/opt/zops/logs/webchat_stderr.log`
- **依赖**: Redis
- **功能**: 
  - WebChat API 服务
  - 用户认证
  - 会话管理
  - AI 对话处理

### zops-frontend (前端 Next.js 服务)
- **端口**: 3000
- **工作目录**: `/opt/zops/web-next`
- **启动命令**: `npm run start`
- **日志文件**:
  - 标准输出：`/opt/zops/logs/frontend_stdout.log`
  - 错误输出：`/opt/zops/logs/frontend_stderr.log`
- **依赖**: zops-backend
- **功能**:
  - Next.js 前端应用
  - 用户界面
  - 对话界面
  - 管理后台

## 启动顺序

服务配置了依赖关系，确保正确的启动顺序：

1. **Redis** → 必须首先启动
2. **zops-backend** → 依赖 Redis
3. **zops-frontend** → 依赖 zops-backend

使用 `manage_zops.sh start` 会自动按正确顺序启动。

## 日志查看

### 使用 journalctl

```bash
# 后端最近 50 行日志
sudo journalctl -u zops-backend -n 50

# 前端最近 50 行日志
sudo journalctl -u zops-frontend -n 50

# 实时查看后端日志
sudo journalctl -u zops-backend -f

# 实时查看前端日志
sudo journalctl -u zops-frontend -f

# 查看今天的日志
sudo journalctl -u zops-backend --since today

# 查看特定时间的日志
sudo journalctl -u zops-backend --since "2024-01-01 00:00:00" --until "2024-01-01 23:59:59"
```

### 查看日志文件

```bash
# 后端日志
tail -f /opt/zops/logs/webchat_stdout.log
tail -f /opt/zops/logs/webchat_stderr.log

# 前端日志
tail -f /opt/zops/logs/frontend_stdout.log
tail -f /opt/zops/logs/frontend_stderr.log
```

## 故障排查

### 服务启动失败

```bash
# 检查服务状态
sudo systemctl status zops-backend
sudo systemctl status zops-frontend

# 查看详细错误
sudo journalctl -u zops-backend -n 100 --no-pager
sudo journalctl -u zops-frontend -n 100 --no-pager

# 检查端口占用
sudo netstat -tlnp | grep -E '7789|3000'

# 检查 Redis 状态
sudo systemctl status redis
```

### 后端无法启动

常见原因：
1. Redis 未运行 → `sudo systemctl start redis`
2. 端口被占用 → 检查并停止占用 7789 端口的进程
3. 配置文件错误 → 检查 `zops.env` 和 `webchat.env`
4. Python 依赖缺失 → `pip3 install -r requirements.txt`

### 前端无法启动

常见原因：
1. 后端未运行 → 先启动后端
2. 端口被占用 → 检查并停止占用 3000 端口的进程
3. Node.js 版本不兼容 → 确保使用 Node.js 18+
4. 依赖缺失 → `cd web-next && npm install`

### 重新部署服务

如果服务文件损坏或需要更新：

```bash
# 停止服务
sudo /opt/zops/manage_zops.sh stop

# 禁用服务
sudo /opt/zops/manage_zops.sh disable

# 重新部署
sudo /opt/zops/deploy_services.sh

# 启用并启动
sudo /opt/zops/manage_zops.sh enable
sudo /opt/zops/manage_zops.sh start
```

## 性能优化

### 调整资源限制

编辑服务文件 `/etc/systemd/system/zops-backend.service`：

```ini
[Service]
# 增加文件描述符限制
LimitNOFILE=65535

# 增加进程数限制
LimitNPROC=4096

# 内存限制（可选）
MemoryLimit=2G
```

### 调整重启策略

```ini
[Service]
# 失败后 5 秒重启
Restart=always
RestartSec=5

# 失败后重启，但限制频率
Restart=on-failure
RestartSec=5
StartLimitBurst=3
StartLimitInterval=60s
```

修改后运行：
```bash
sudo /opt/zops/deploy_services.sh
```

## 监控建议

### 添加监控脚本

创建 `/opt/zops/monitor_services.sh`：

```bash
#!/bin/bash
# 检查服务状态
systemctl is-active zops-backend || echo "后端服务异常！"
systemctl is-active zops-frontend || echo "前端服务异常！"

# 检查端口
netstat -tlnp | grep -q ':7789' || echo "后端端口未监听！"
netstat -tlnp | grep -q ':3000' || echo "前端端口未监听！"

# 检查 Redis
redis-cli ping || echo "Redis 异常！"
```

### 设置定时检查

```bash
# 添加到 crontab
*/5 * * * * /opt/zops/monitor_services.sh >> /var/log/zops_monitor.log 2>&1
```

## 常见问题

**Q: 服务开机不启动？**
A: 运行 `sudo /opt/zops/manage_zops.sh enable` 启用开机自启

**Q: 如何查看服务是否开机自启？**
A: `systemctl is-enabled zops-backend` 和 `systemctl is-enabled zops-frontend`

**Q: 日志文件太大怎么办？**
A: 使用 `logrotate` 轮转日志，或定期清理：
```bash
sudo truncate -s 0 /opt/zops/logs/*.log
```

**Q: 如何平滑重启（不中断服务）？**
A: 使用 `systemctl reload`（如果应用支持）或 `systemctl restart`

**Q: 服务占用多少资源？**
A: 使用 `systemctl status` 或 `top` 查看实时资源占用

---

**最后更新**: 2024-01-XX  
**维护者**: Z-OPS 运维团队
