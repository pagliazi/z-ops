# Z-OPS v4 - MCP 工具集成指南

> Model Context Protocol (MCP) 外部工具集成  
> 创建时间: 2026-06-25  
> 状态: ✅ 已完成

---

## 📋 概述

Z-OPS v4 现在支持通过 MCP (Model Context Protocol) 集成外部工具服务。

**MCP 端点:**
- **SSE 端点**: `https://opscore-mgt.example.com/mcp/sse` (16 tools)
- **REST API**: `https://opscore-mgt.example.com/api/snapshot/mcp/tools/<name>/` (全部可用)

---

## 🚀 快速开始

### 1. 配置环境变量

在 `zops.env` 中添加以下配置:

```bash
# MCP 工具集成配置
MCP_ENABLED=true
MCP_SSE_ENDPOINT=https://opscore-mgt.example.com/mcp/sse
MCP_REST_BASE=https://opscore-mgt.example.com/api/snapshot/mcp/tools
MCP_TIMEOUT=30
MCP_VERIFY_SSL=true
```

### 2. 在代码中集成

```python
from core.tool_system import ToolPool
from core.mcp_adapter import register_mcp_tools

# 创建工具池
tool_pool = ToolPool()

# 自动发现并注册所有 MCP 工具
mcp_count = await register_mcp_tools(tool_pool)
print(f"✅ 注册了 {mcp_count} 个 MCP 工具")
```

---

## 📦 核心组件

### MCPToolAdapter

自动发现并注册外部 MCP 工具的适配器。

**用法:**

```python
from core.mcp_adapter import MCPToolAdapter

adapter = MCPToolAdapter(tool_pool)

# 发现工具
tools = await adapter.discover_tools()
print(f"发现 {len(tools)} 个工具")

# 注册所有工具
count = await adapter.register_all()
print(f"注册了 {count} 个工具")

# 或一步完成
count = await adapter.discover_and_register()
```

### MCPToolWrapper

将远程 MCP 工具包装为 Z-OPS `BaseTool` 的包装器。

**特性:**
- ✅ 自动处理 HTTP 请求
- ✅ 超时控制
- ✅ 错误重试
- ✅ 权限推断
- ✅ 并发安全标记

---

## 🔧 配置选项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `MCP_ENABLED` | bool | `true` | 是否启用 MCP 集成 |
| `MCP_SSE_ENDPOINT` | string | `https://opscore-mgt.example.com/mcp/sse` | SSE 端点 URL |
| `MCP_REST_BASE` | string | `https://opscore-mgt.example.com/api/snapshot/mcp/tools` | REST API 基础 URL |
| `MCP_TIMEOUT` | int | `30` | 工具调用超时 (秒) |
| `MCP_VERIFY_SSL` | bool | `true` | 是否验证 SSL 证书 |

---

## 📊 工具发现流程

```
1. 连接 SSE 端点
   ↓
2. 获取工具列表 (JSON 格式)
   ↓
3. 解析工具定义 (name, description, input_schema)
   ↓
4. 为每个工具创建 MCPToolWrapper
   ↓
5. 推断权限级别 (READ_ONLY / WRITE / DESTRUCTIVE)
   ↓
6. 注册到 ToolPool
   ↓
7. 可用 LLM 调用
```

---

## 🔐 权限推断规则

MCP 工具适配器会根据工具名和描述自动推断权限级别:

| 关键词 | 权限级别 |
|--------|---------|
| `delete`, `remove`, `destroy`, `drop` | `DESTRUCTIVE` |
| `create`, `update`, `modify`, `write` | `WRITE` |
| 其他 | `READ_ONLY` (默认) |

---

## 📝 示例脚本

运行示例脚本查看集成效果:

```bash
cd /opt/zops
python scripts/mcp_integration_example.py
```

**输出示例:**

```
============================================================
Z-OPS v4 - MCP 工具集成示例
============================================================

[方式1] 使用便捷函数注册 MCP 工具...
✅ 成功注册 16 个 MCP 工具

[方式2] 使用 MCPToolAdapter...
📋 发现 16 个 MCP 工具:
  - query_vm: 查询虚拟机信息
  - check_host: 检查主机健康状态
  - list_snapshots: 列出虚拟机快照
  ... 还有 13 个工具
✅ 通过适配器注册 16 个工具

============================================================
📊 当前 ToolPool 状态:
============================================================
总计: 18 个工具
  - 内置工具: 2 个
  - MCP 工具: 16 个

📝 工具列表:
   1. check_host                     - 检查主机健康状态
   2. query_kb                        - 知识库查询
   3. query_vm                        - 查询虚拟机信息
   ...

============================================================
✅ MCP 工具集成示例完成
============================================================
```

---

## 🔄 在 Agent 中使用

MCP 工具注册后，可以像内置工具一样在 Agent 中使用:

```python
# Agent 代码无需修改
# LLM 会自动发现并使用 MCP 工具

async def handle_tool_call(self, tool_call):
    tool_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    
    # ToolPool 会自动路由到 MCP 工具
    result = await self.tool_pool.execute(
        tool_call_id=tool_call.id,
        tool_name=tool_name,
        arguments=arguments,
        ctx=tool_context
    )
    
    return result
```

---

## 🐛 故障排查

### 问题: MCP 工具未注册

**检查清单:**
1. ✅ `MCP_ENABLED=true` 是否设置
2. ✅ MCP 端点是否可访问: `curl https://opscore-mgt.example.com/mcp/sse`
3. ✅ 网络连接是否正常
4. ✅ SSL 证书是否有效 (或设置 `MCP_VERIFY_SSL=false`)

### 问题: 工具调用超时

**解决方案:**
- 增加超时时间: `MCP_TIMEOUT=60`
- 检查 MCP 服务响应时间
- 查看日志中的错误信息

### 问题: 权限推断不正确

**解决方案:**
手动指定工具权限 (需要修改 MCPToolWrapper 初始化):

```python
wrapper = MCPToolWrapper(
    name="custom_tool",
    description="...",
    input_schema={...},
    rest_url="...",
    permission=PermissionLevel.ADMIN,  # 手动指定
)
```

---

## 📚 相关文件

| 文件 | 说明 |
|------|------|
| `core/mcp_adapter.py` | MCP 工具适配器实现 (321 lines) |
| `core/tool_system.py` | Z-OPS 工具系统核心 |
| `config.py` | MCP 配置项 |
| `scripts/mcp_integration_example.py` | 集成示例脚本 |
| `MCP_INTEGRATION.md` | 本文档 |

---

## 🎯 下一步

1. ✅ **基础集成** - 已完成
2. ⏳ **SSE 流式支持** - 待实现完整 SSE 事件流解析
3. ⏳ **工具缓存** - 缓存工具定义，减少启动时间
4. ⏳ **健康检查** - 定期检查 MCP 服务可用性
5. ⏳ **性能监控** - 记录工具调用耗时和成功率

---

*最后更新: 2026-06-25*  
*MCP 端点: https://opscore-mgt.example.com/mcp/sse (16 tools)*  
*状态: ✅ 生产就绪*
