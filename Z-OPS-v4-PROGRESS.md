# Z-OPS v4 架构优化 — 进度报告

> 创建时间: 2026-06-08
> 基于: Claude Code / Claw Code / AutoResearch 架构研究

---

## 已完成 Phase (0-3)

### ✅ Phase 0: 基础设施准备
- Python 3.10.12, Redis 6.0.16 (370 keys)
- 11 Agents, 25 Core modules
- 所有依赖验证通过

### ✅ Phase 1: Agent Loop 重构 (209 lines)
**文件**: `core/agent_loop.py`

**核心设计**:
```python
async for event in agent_loop.run(messages, tools, llm_fn, tool_executor):
    # event.type: progress / llm_response / tool_start / tool_result / terminal
    yield event
```

**10 种终止状态** (Terminal discriminated union):
- `COMPLETED` / `MAX_TURNS` / `TOKEN_BUDGET_EXHAUSTED` / `PROMPT_TOO_LONG`
- `MODEL_ERROR` / `STREAM_ABORTED` / `TOOL_ERROR` / `TIMEOUT`
- `HOOK_BLOCKED` / `RECOVERY_FAILED`

**关键特性**:
- AsyncGenerator 作为唯一控制平面
- 内部方法通过 `event_cb` 回调发送事件 (避免 return+yield 冲突)
- 错误恢复阶梯: 重试 2 次，指数退避
- Token 预算监控 + 超时控制

### ✅ Phase 2: Tool System 标准化 (421 lines)
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

**14 步执行管线** (参考 Claude Code):
1. 工具查找 → 2. 中断检查 → 3. Schema 验证 → 4. 语义验证
5. 推测性分类 → 6. 输入回填 → 7. PreToolUse Hooks
8. 权限解析 → 9. 权限拒绝 → 10. 执行 call()
11. 结果预算 → 12. PostToolUse Hooks → 13. 附加消息 → 14. 错误分类

**已注册工具**:
- `query_kb` — 知识库查询 (完整实现)
- `check_host` — 主机健康检查 (stub)

**权限级别**: READ_ONLY / WRITE / DESTRUCTIVE / ADMIN / BUBBLE

### ✅ Phase 3: Memory 文件化 (406 lines)
**文件**: `core/memory_system.py`

**文件结构**:
```
~/.zops/memory/{user}/
├── MEMORY.md          # 索引 (≤200 行, ≤25KB)
├── user/
│   └── 回答风格.md    # 用户偏好
├── feedback/
│   └── powershell教训.md  # 纠正记录
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

**测试验证**:
```
✅ MemorySystem 创建成功
✅ 添加记忆成功
✅ 添加反馈记录成功  
✅ 召回 1 条记忆
✅ System Context 构建成功
✅ 统计: total=2, by_type={user:1, feedback:1}
```

---

## 待实施 Phase (4-6)

### Phase 4: Hooks 系统
**目标**: 27 个生命周期事件拦截器

**关键事件**:
- `PreToolUse` / `PostToolUse` — 工具执行前后
- `PreLLMCall` / `PostLLMCall` — LLM 调用前后  
- `SessionStart` / `SessionEnd` — 会话生命周期
- `MessageReceived` / `ReplySent` — 消息流

**预计工时**: 2-3 天

### Phase 5: Context Compression
**目标**: 四层压缩管线

**层级**:
0. Tool result budget (30K chars)
1. Snip compact (物理移除旧消息)
2. Microcompact (丢弃不需要工具结果)
3. Context collapse (对话片段摘要)
4. Auto-compact (fork 整个对话摘要)

**预计工时**: 2 天

### Phase 6: 集成测试 + 性能优化 + 文档
- 单元测试: Agent Loop (10 种终止状态)
- 集成测试: Tool System (14 步管线)
- 性能测试: v3 vs v4 响应时间对比
- 文档: 架构文档 + 迁移指南

**预计工时**: 3-5 天

---

## 代码统计

| 模块 | 文件 | 行数 | 状态 |
|------|------|------|------|
| Agent Loop | `core/agent_loop.py` | 209 | ✅ 完成 |
| Tool System | `core/tool_system.py` | 421 | ✅ 完成 |
| Memory System | `core/memory_system.py` | 406 | ✅ 完成 |
| 迁移示例 | `agents/general_agent_v4.py` | 247 | ✅ 完成 |
| 架构文档 | `Z-OPS-v4-ARCHITECTURE-PLAN.md` | 245 | ✅ 完成 |
| **总计** | **5 文件** | **1,528 行** | **Phase 0-3 完成** |

---

## 架构对比

| 维度 | Z-OPS v3 | Z-OPS v4 | 改进 |
|------|--------|--------|------|
| Agent Loop | 单次 request-response | AsyncGenerator 流式状态机 | ⭐⭐⭐⭐⭐ |
| 工具系统 | 硬编码 action 分发 | 自描述工具 + 14步管线 | ⭐⭐⭐⭐⭐ |
| 记忆系统 | Redis 缓存 | 文件持久化 + LLM 召回 | ⭐⭐⭐⭐ |
| 多轮推理 | 不支持 | 自动 tool-call 循环 | ⭐⭐⭐⭐⭐ |
| 错误恢复 | 简单 fallback | 阶梯式恢复 + 重试 | ⭐⭐⭐⭐ |
| 终止状态 | 无 | 10 种穷举类型 | ⭐⭐⭐⭐ |
| 生命周期 | 无 | 27 个 Hook 事件 | ⏳ Phase 4 |
| 上下文管理 | 无压缩 | 四层压缩管线 | ⏳ Phase 5 |

---

## 下一步行动

### 立即可做
1. **集成现有 Agent** — 把 general_agent 迁移到 v4 (已有示例)
2. **注册运维工具** — 为 ops_agent 注册 5-10 个实际工具
3. **测试多轮流程** — 验证 tool-call 循环正常工作

### 本周目标
1. 完成 Hooks 系统 (Phase 4)
2. Context Compression 基础版 (Phase 5 层 0-2)
3. 实际迁移 2-3 个 agent 到 v4

### 本月目标
1. 全量迁移现有 agents
2. 性能优化 (响应时间降低 50%)
3. 完整文档 + 迁移指南

---

## 验证记录

```bash
# Phase 1-2 验证
✅ Agent Loop 导入成功
✅ Tool System 导入成功
✅ 已注册工具: 2 (query_kb, check_host)
✅ AgentLoopConfig: max_turns=5, timeout=120.0s
✅ TerminalKind: 10 种状态

# Phase 3 验证  
✅ MemorySystem 创建成功
✅ 添加记忆成功
✅ 添加反馈记录成功
✅ 召回 1 条记忆
✅ System Context 构建成功
✅ 统计: total=2, by_type={user:1, feedback:1}

# 文件系统验证
✅ /root/.zops/memory/test/ 目录结构正确
✅ MEMORY.md 索引自动生成
✅ .md 文件 frontmatter 格式正确
```

---

*最后更新: 2026-06-08 07:50*
*Phase 0-3 完成，Phase 4-6 待实施*
