# Z-OPS v4 架构优化 — 完整总结

> 创建时间: 2026-06-08  
> 基于: Claude Code / Claw Code / AutoResearch 六大核心抽象研究  
> 状态: Phase 0-5 完成，Phase 6 待实施

---

## 📊 代码统计

| 模块 | 文件 | 行数 | 状态 |
|------|------|------|------|
| Agent Loop | `core/agent_loop.py` | 209 | ✅ 完成 |
| Tool System | `core/tool_system.py` | 421 | ✅ 完成 |
| Memory System | `core/memory_system.py` | 406 | ✅ 完成 |
| Hooks System | `core/hooks.py` | 434 | ✅ 完成 |
| Context Compression | `core/context_compression.py` | 325 | ✅ 完成 |
| 迁移示例 | `agents/general_agent_v4.py` | 247 | ✅ 完成 |
| 架构文档 | `Z-OPS-v4-ARCHITECTURE-PLAN.md` | 245 | ✅ 完成 |
| 进度报告 | `Z-OPS-v4-PROGRESS.md` | 216 | ✅ 完成 |
| **总计** | **8 文件** | **2,503 行** | **Phase 0-5 完成** |

---

## ✅ 已完成 Phase (0-5)

### Phase 0: 基础设施准备
- ✅ Python 3.10.12, Redis 6.0.16 (370 keys)
- ✅ 11 Agents, 25 Core modules 验证
- ✅ 所有依赖检查通过

### Phase 1: Agent Loop 重构 (209 lines)
**文件**: `core/agent_loop.py`

**核心架构**:
```python
async for event in agent_loop.run(messages, tools, llm_fn, tool_executor):
    yield event  # progress / llm_response / tool_start / tool_result / terminal
```

**10 种终止状态** (Terminal discriminated union):
- `COMPLETED` / `MAX_TURNS` / `TOKEN_BUDGET_EXHAUSTED` / `PROMPT_TOO_LONG`
- `MODEL_ERROR` / `STREAM_ABORTED` / `TOOL_ERROR` / `TIMEOUT`
- `HOOK_BLOCKED` / `RECOVERY_FAILED`

**关键特性**:
- ✅ AsyncGenerator 作为唯一控制平面
- ✅ 内部方法通过 `event_cb` 回调发送事件 (避免 return+yield 冲突)
- ✅ 错误恢复阶梯: 重试 2 次，指数退避
- ✅ Token 预算监控 + 超时控制

### Phase 2: Tool System 标准化 (421 lines)
**文件**: `core/tool_system.py`

**核心接口**:
```python
@tool(name="query_kb", description="查询知识库")
class QueryKBTool(BaseTool):
    input_schema = {"query": str, "limit": int}
    is_concurrency_safe = True
    permission = PermissionLevel.READ_ONLY
    
    async def call(self, ctx, query, limit=5):
        ...
```

**14 步执行管线**:
1. 工具查找 → 2. 中断检查 → 3. Schema 验证 → 4. 语义验证
5. 推测性分类 → 6. 输入回填 → 7. PreToolUse Hooks
8. 权限解析 → 9. 权限拒绝 → 10. 执行 call()
11. 结果预算 → 12. PostToolUse Hooks → 13. 附加消息 → 14. 错误分类

**已注册工具**:
- ✅ `query_kb` — 知识库查询 (完整实现)
- ✅ `check_host` — 主机健康检查 (stub)

**权限级别**: READ_ONLY / WRITE / DESTRUCTIVE / ADMIN / BUBBLE

### Phase 3: Memory 文件化 (406 lines)
**文件**: `core/memory_system.py`

**文件结构**:
```
~/.zops/memory/{user}/
├── MEMORY.md          # 索引 (≤200 行, ≤25KB)
├── user/              # 用户偏好
├── feedback/          # 纠正记录
├── projects/          # 项目上下文
├── reference/         # 外部参考
└── team/              # 团队共享
```

**四层记忆分类**:
| 类型 | 说明 | 示例 |
|------|------|------|
| user | 角色、偏好 | "喜欢简洁回答" |
| feedback | 纠正+验证方法 | "不要用 rm -rf" |
| project | 活跃工作上下文 | "Q2 迁移计划" |
| reference | 外部系统指针 | "Linear Z-OPS-123" |

**核心功能**:
- ✅ 文件持久化 (增删改查)
- ✅ LLM 召回 (关键词匹配，后续接语义召回)
- ✅ 索引维护 (MEMORY.md 自动更新)
- ✅ 自动学习 (feedback 写入)
- ✅ System Context 构建 (注入 prompt)

### Phase 4: Hooks 系统 (434 lines)
**文件**: `core/hooks.py`

**27 个生命周期事件**:
| 分类 | 事件 | 数量 |
|------|------|------|
| 工具执行 | PreToolUse, PostToolUse, ToolCallStart/End, etc. | 8 |
| LLM 调用 | PreLLMCall, PostLLMCall, LLMStreamStart/Chunk/End, etc. | 6 |
| 会话生命周期 | SessionStart/End, MessageReceived, ReplySent, etc. | 6 |
| 系统级 | MemoryLoad/Save, ContextCompress, TokenBudget, etc. | 7 |

**5 种决策类型**: ALLOW / DENY / ASK / MODIFY / PASSTHROUGH

**内置 Hook**:
- ✅ `dangerous_command` — 危险命令拦截 (rm -rf /, chmod 777, etc.)
- ✅ `audit_log` — 审计日志记录
- ✅ `cost_tracking` — 成本追踪

**测试结果**:
```
✅ 危险命令拦截: deny - 禁止删除根目录
✅ 安全命令: passthrough
```

### Phase 5: Context Compression (325 lines)
**文件**: `core/context_compression.py`

**四层压缩管线**:
| 层级 | 操作 | 触发条件 | 成本 |
|------|------|---------|------|
| 0 | Tool result budget | 始终执行 | 极低 |
| 1 | Snip compact | 上下文 > 90% | 低 |
| 2 | Microcompact | 上下文 > 94% | 低 |
| 3 | Context collapse | 上下文 > 98% | 中 |
| 4 | Auto-compact | 接近硬阻塞 | 高 (需 LLM) |

**预算阈值**:
- Auto-compact: `128K - 13K = 115K tokens`
- Hard blocking: `128K - 3K = 125K tokens`
- Reactive gap: `10K tokens`

**Token 估算**:
- ✅ 英文: ~4 chars/token
- ✅ 中文: ~1.5 chars/token
- ⏳ 后续接入 tiktoken 精确计数

---

## 架构对比

| 维度 | Z-OPS v3 | Z-OPS v4 | 改进幅度 |
|------|--------|--------|---------|
| Agent Loop | 单次 request-response | AsyncGenerator 流式状态机 | ⭐⭐⭐⭐⭐ |
| 工具系统 | 硬编码 action 分发 | 自描述工具 + 14步管线 | ⭐⭐⭐⭐⭐ |
| 记忆系统 | Redis 缓存 | 文件持久化 + LLM 召回 | ⭐⭐⭐⭐ |
| 多轮推理 | 不支持 | 自动 tool-call 循环 | ⭐⭐⭐⭐⭐ |
| 错误恢复 | 简单 fallback | 阶梯式恢复 + 重试 | ⭐⭐⭐⭐ |
| 终止状态 | 无 | 10 种穷举类型 | ⭐⭐⭐⭐ |
| 生命周期 | 无 | 27 个 Hook 事件 | ⭐⭐⭐⭐⭐ |
| 上下文管理 | 无压缩 | 四层压缩管线 | ⭐⭐⭐⭐ |

---

## 验证记录

```bash
# Phase 1-2
✅ Agent Loop 导入成功
✅ Tool System 导入成功
✅ 已注册工具: 2 (query_kb, check_host)
✅ AgentLoopConfig: max_turns=5, timeout=120.0s
✅ TerminalKind: 10 种状态

# Phase 3
✅ MemorySystem 创建成功
✅ 添加记忆成功
✅ 添加反馈记录成功
✅ 召回 1 条记忆
✅ System Context 构建成功
✅ 统计: total=2, by_type={user:1, feedback:1}

# Phase 4
✅ Hooks 系统导入成功
✅ 内置 Hook 统计: {total: 3, by_event: {PreToolUse:1, PostToolUse:1, PostLLMCall:1}}
✅ 危险命令拦截: deny - 禁止删除根目录
✅ 安全命令: passthrough

# Phase 5
✅ Context Compression 导入成功
✅ 英文估算: "Hello world..." → 8 tokens
✅ 中文估算: "你好世界..." → 12 tokens
✅ 压缩: layer=0, freed=0 tokens, removed=0 msgs, 0ms
✅ 压缩后: 61 条消息
```

---

## 核心设计模式

### 1. AsyncGenerator 作为控制平面
```python
# run() 是唯一的 async generator，产生所有事件
# 内部方法通过 event_cb 回调发送事件，不直接 yield
# 避免 return+yield 的语法冲突
```

### 2. 失败安全默认值 (Fail-Closed)
```python
# 工具并发安全默认 False (串行)
# 工具验证失败 → 拒绝执行
# Hook 执行失败 → 不阻塞流程
```

### 3. 渐进式降级
```python
# LLM 失败 → 重试 2 次 (指数退避)
# 压缩失败 → 降级到上一层
# 记忆加载失败 → 返回空上下文
```

### 4. 信任通过透明
```python
# 记忆文件用户可直接查看/编辑
# Hook 决策日志可审计
# 压缩统计可追溯
```

---

## 下一步行动 (Phase 6)

### 立即可做
1. **集成现有 Agent** — 把 general_agent 迁移到 v4 (已有示例代码)
2. **注册运维工具** — 为 ops_agent 注册 5-10 个实际工具
3. **测试多轮流程** — 验证 tool-call 循环 + hooks + compression 联调

### 本周目标
1. 实际迁移 2-3 个 agent 到 v4
2. 性能基准测试 (v3 vs v4 响应时间对比)
3. 完整文档 + 迁移指南

### 本月目标
1. 全量迁移现有 agents
2. 性能优化 (目标: 响应时间降低 50%)
3. Memory 系统接入 LLM 语义召回 (目前仅关键词匹配)
4. Context Compression 接入 tiktoken 精确计数

---

## 文件清单

### 核心模块 (Phase 0-5)
```
core/
├── agent_loop.py              # Agent Loop AsyncGenerator (209 lines)
├── tool_system.py             # Tool System + 注册中心 (421 lines)
├── memory_system.py           # Memory 文件化系统 (406 lines)
├── hooks.py                   # Hooks 系统 27 事件 (434 lines)
├── context_compression.py     # Context Compression 四层管线 (325 lines)
└── ... (existing modules)
```

### Agent 示例
```
agents/
├── general_agent_v4.py        # v3 → v4 迁移示例 (247 lines)
└── ... (existing agents)
```

### 文档
```
Z-OPS-v4-ARCHITECTURE-PLAN.md    # 完整架构优化计划 (245 lines)
Z-OPS-v4-PROGRESS.md             # 实施进度报告 (216 lines)
Z-OPS-v4-COMPLETE.md             # 本文件 (完整总结)
```

---

## 技术债务

| 项目 | 当前状态 | 后续改进 |
|------|---------|---------|
| Token 估算 | 简单字符比例 | 接入 tiktoken 精确计数 |
| Memory 召回 | 关键词匹配 | 接入 LLM 语义召回 |
| Auto-Compact | 未实现 | 接入 LLM fork 摘要 |
| Tool 实现 | 2 个示例 | 注册 10+ 实际运维工具 |
| Agent 迁移 | 示例代码 | 全量迁移 11 个 agents |

---

*最后更新: 2026-06-08 08:00*  
*Phase 0-5 完成 (2,503 行代码/文档)*  
*Phase 6: 集成测试 + 性能优化 + 文档*
