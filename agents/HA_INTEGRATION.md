# HA VM API 集成指南

## 概述

HA VM API 提供 vSphere HA（高可用）重启的 VM列表数据，通过缓存机制为 OpenCLAW 等系统提供快速查询。

**API 文档**: http://198.18.18.166:8088/docs

## 架构设计

- **读路径**: OpenCLAW 调用 `/api/v1/ha-vms` 只从缓存读取
- **写路径**: 告警触发调用 `/api/v1/alert-trigger` 更新缓存
- **数据来源**: 通过 SSH 连接 vCenter 服务器，查询 PostgreSQL 数据库获取 HA 事件

## 安全特性

- API Key 认证（X-API-Key header）
- IP 白名单
- 请求频率限制
- 站点白名单验证

## Z-OPS 集成步骤

### 1. 配置环境变量

编辑 `zops/zops.env`:

```bash
# ═══ HA VM 高可用信息 (HA Agent) ═══
HA_API_URL=http://198.18.18.166:8088
HA_API_TOKEN=your_ha_api_token_here
```

### 2. 使用方式

#### 方式一：直接使用 ha_api 模块

```python
import asyncio
from zops.agents.ha_api import query_ha_vms, search_ha_vms, get_all_sites

async def main():
    # 获取 SCC 站点的 HA VM列表
    result = await query_ha_vms("SCC")
    print(result)
    
    # 搜索特定 VM
    result = await search_ha_vms(vm_name="web-server", site="SCC")
    print(result)
    
    # 获取所有站点状态
    result = await get_all_sites()
    print(result)

asyncio.run(main())
```

#### 方式二：使用 gcoc_agent 的 HA 查询功能

在 gcoc_agent 中自动识别 HA 相关意图：

```python
# gcoc_agent 会自动路由到 HA API
用户问题："查看 SCC 站点因 HA 重启的虚拟机"
→ 自动调用 ha_vms 端点
```

### 3. API 端点说明

#### GET /api/v1/ha-vms

从缓存获取指定站点的 HA VM列表

**参数**:
- `site` (必需): 站点 ID（如 SCC）
- `vm_name` (可选): VM 名称过滤（模糊匹配）
- `include_expired` (可选): 是否包含过期数据，默认 false

**响应示例**:
```json
{
  "code": 200,
  "message": "success",
  "data": {
    "site": "SCC",
    "total": 5,
    "cache_updated_at": "2026-03-11T10:00:00",
    "vms": [
      {
        "vm_id": "vm-123",
        "vm_name": "web-server-01",
        "site": "SCC",
        "resource_pool": "Production",
        "restart_time": "2026-03-11T08:30:00",
        "vcenter_host": "198.18.138.151"
      }
    ]
  },
  "timestamp": "2026-03-11T10:00:00"
}
```

#### GET /api/v1/ha-vms/search

跨站点搜索HA VM

**参数**:
- `vm_name` (可选): VM 名称（模糊匹配）
- `site` (可选): 限定站点

#### GET /api/v1/sites

获取所有站点状态摘要

#### POST /api/v1/alert-trigger

告警触发刷新缓存

**请求体**:
```json
{
  "site": "SCC",
  "alert_type": "HA",
  "force_refresh": false
}
```

#### POST /api/v1/refresh/{site}

手动刷新站点缓存

**参数**:
- `site`: 站点 ID
- `force` (可选): 是否强制刷新，默认 false

#### DELETE /api/v1/cache/{site}

清除站点缓存

**参数**:
- `site`: 站点 ID

## 常见问题

### Q: 如何获取 API Token？

A: 联系 HA API服务管理员获取正确的 `HA_API_TOKEN`。

### Q: 缓存多久更新一次？

A: 默认情况下缓存不会自动更新，需要通过以下方式触发：
1. 调用 `/api/v1/alert-trigger`（告警触发）
2. 调用 `/api/v1/refresh/{site}`（手动刷新）

### Q: 支持哪些站点？

A: 支持的站点取决于 HA API服务的配置，可以通过 `/api/v1/sites` 查询所有已配置的站点。

## 故障排查

### 认证失败：API Key 无效

检查 `zops.env` 中的 `HA_API_TOKEN` 是否正确：

```bash
# 测试 API Key
curl -H "X-API-Key: your_token_here" http://198.18.18.166:8088/api/v1/health
```

### 连接超时

确认网络连通性：

```bash
ping 198.18.18.166
telnet 198.18.18.166 8088
```

### 无数据返回

可能原因：
1. 站点尚未有 HA 事件
2. 缓存未刷新（调用 refresh 接口）
3. 站点名称拼写错误

## 下一步计划

1. ✅ 创建 ha_api.py 客户端模块
2. ✅ 集成到 gcoc_api 端点目录
3. ⏳ 添加 gcoc_agent 的 HA 查询技能
4. ⏳ 前端增加 HA VM 展示面板
5. ⏳ 实现 HA 事件自动告警通知
