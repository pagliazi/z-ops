# Z-OPS v4 架构优化计划

> 基于 Claude Code / Claw Code / AutoResearch 六大核心抽象的系统性重构

---

## 已完成 (Phase 0-2)

### ✅ Phase 0: 基础设施准备
- Python 3.10.12, Redis 6.0.16 (370 keys)
- 11 Agents, 25 Core modules
- 所有依赖验证通过

### ✅ Phase 1: Agent Loop 重构
**文件**: `core/agent_loop.py` (209 lines)

**核心设计**:
```
async for event in agent_loop.run(messages, tools, llm_fn, tool_executor):
    yield event  # progress / llm_response / tool_start / tool_result / terminal
```

**10 种终止状态** (Terminal discriminated union):
| 状态 | 说明 |
|------|------|
| `COMPLETED` | 正常完成，无更多 tool_call |
| `MAX_TURNS` | 达到最大轮数限制 |
| `TOKEN_BUDGET_EXHAUSTED` | Token 预算耗尽 |
| `PROMPT_TOO_LONG` | 提示词超过上下文窗口 |
| `MODEL_ERROR` | LLM 调用失败 (不可恢复) |
| `STREAM_ABORTED` | 用户主动中断 |
| `TOOL_ERROR` | 工具执行致命错误 |
| `TIMEOUT` | 总执行超时 |
| `HOOK_BLOCKED` | Hook 阻止继续执行 |
| `RECOVERY_FAILED` | 错误恢复尝试失败 |

**架构要点**:
- `run()` 是唯一的 async generator，产生所有事件
- 内部方法通过 `event_cb` 回调发送事件，不直接 yield (避免 return+yield 冲突)
- 错误恢复阶梯: 重试 2 次，指数退避
- Token 预算监控 + 超时控制

### ✅ Phase 2: Tool System 标准化
**文件**: `core/tool_system.py` (421 lines)

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

**14 步执行管线** (参考 Claude Code checkPermissionsAndCallTool):
1. 工具查找 (含别名映射)
2. 中断检查
3. Schema 验证 (JSON Schema)
4. 语义验证 (业务规则)
5. 推测性分类器启动
6. 输入回填 (路径展开)
7. PreToolUse Hooks
8. 权限解析 (7 种模式)
9. 权限拒绝路径
10. 执行 call()
11. 结果预算控制 (30K chars)
12. PostToolUse Hooks
13. 附加新消息 (子 agent 转录)
14. 错误分类 (遥测)

**权限级别**:
- `READ_ONLY` — 只读，自动允许
- `WRITE` — 写操作，可能需要确认
- `DESTRUCTIVE` — 破坏性操作，需要确认
- `ADMIN` — 管理员权限
- `BUBBLE` — 子 agent 上抛给父 agent

**已注册工具**:
- `query_kb` — 知识库查询 (已实现)
- `check_host` — 主机健康检查 (stub)

### 示例: general_agent_v4
**文件**: `agents/general_agent_v4.py` (247 lines)

演示如何将现有 agent 迁移到 v4 架构:
- 旧: `handle() → 单次 LLM 调用 → reply()`
- 新: `handle() → agent_loop.run() → 多轮 tool-call → 流式 reply()`

---

## 待实施 (Phase 3-6)

### Phase 3: Memory 文件化
**目标**: 从 Redis 缓存迁移到文件持久化 + LLM 召回

**文件结构**:
```
~/.zops/memory/
├── MEMORY.md          # 索引 (≤200 行，≤25KB)
├── user_prefs.md      # 用户偏好
├── feedback.md        # 纠正记录
├── projects/          # 项目上下文
└── team/              # 团队共享 (symlink)
```

**四层记忆分类**:
| 类型 | 说明 | 示例 |
|------|------|------|
| user | 角色、偏好 | "用户喜欢简洁回答" |
| feedback | 纠正+验证方法 | "不要用 rm -rf，改用 mv 备份" |
| project | 活跃工作上下文 | "Q2 迁移计划，截止 2026-06-30" |
| reference | 外部系统指针 | "Linear 项目 Z-OPS-123" |

**召回机制**:
- Session 启动时用小模型 (Haiku) 挑选相关记忆
- 注入 system prompt: "以下是用户相关的记忆..."
- 描述字段 (description) 是索引关键

**关键原则**:
- 透明度: 用户可直接查看/编辑 .md 文件
- 可衍生性: 如果 git log / code 能回答，不记忆
- 时间戳: 修改时间是认识论信号

**预计工时**: 2-3 天

### Phase 4: Hooks 系统
**目标**: 27 个生命周期事件拦截器

**核心接口**:
```python
@hook("PreToolUse", tool="bash")
async def check_bash_safety(tool_input, ctx):
    if "rm -rf /" in tool_input.command:
        return HookDecision.DENY("危险操作")
    return HookDecision.PASSTHROUGH
```

**关键生命周期事件**:
| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `PreToolUse` | 工具执行前 | 权限检查、安全验证 |
| `PostToolUse` | 工具执行后 | 结果过滤、日志记录 |
| `PreLLMCall` | LLM 调用前 | Prompt 注入、缓存检查 |
| `PostLLMCall` | LLM 调用后 | 响应过滤、成本追踪 |
| `SessionStart` | 会话开始 | 记忆加载、上下文初始化 |
| `SessionEnd` | 会话结束 | 记忆保存、成本持久化 |
| `MessageReceived` | 收到消息 | 审计日志、速率限制 |
| `ReplySent` | 发送回复 | 审计日志、关键词索引 |

**Hook 决策类型**:
- `ALLOW` — 允许继续
- `DENY` — 阻止并返回错误
- `ASK` — 需要用户确认
- `MODIFY` — 修改输入后继续
- `PASSTHROUGH` — 不干预

**预计工时**: 2-3 天

### Phase 5: Context Compression
**目标**: 四层压缩管线，每次 LLM 调用前执行

**压缩层级**:
| 层级 | 操作 | 成本 | 触发条件 |
|------|------|------|---------|
| 0 | Tool result budget | 极低 | 始终执行 |
| 1 | Snip compact | 低 | 上下文 > 80% |
| 2 | Microcompact | 低 | 工具结果不再需要 |
| 3 | Context collapse | 中 | 上下文 > 90% |
| 4 | Auto-compact | 高 | 上下文 > 95% |

**预算阈值** (参考 Claude Code):
- Auto-compact: `effective_context_window - 13,000 tokens`
- Hard blocking: `effective_context_window - 3,000 tokens`
- 10K token gap: reactive compact 运行窗口

**Token 计数策略**:
- 权威: API usage 数字
- 估算: 新加入消息的 rough estimate
- 保守偏差: compaction 略微提前触发

**预计工时**: 2 天

### Phase 6: 集成测试 + 性能优化 + 文档
- 单元测试: Agent Loop (10 种终止状态)
- 集成测试: Tool System (14 步管线)
- 性能测试: 对比 v3 vs v4 响应时间
- 文档: 架构文档 + 迁移指南

**预计工时**: 3-5 天

---

## 架构对比

| 维度 | Z-OPS v3 | Z-OPS v4 |
|------|--------|--------|
| Agent Loop | 单次 request-response | AsyncGenerator 流式状态机 |
| 工具系统 | 硬编码 action 分发 | 自描述工具 + 14 步管线 |
| 记忆系统 | Redis 缓存 | 文件持久化 + LLM 召回 |
| 生命周期 | 无 | 27 个 Hook 事件 |
| 上下文管理 | 无压缩 | 四层压缩管线 |
| 多轮推理 | 不支持 | 自动 tool-call 循环 |
| 错误恢复 | 简单 fallback | 阶梯式恢复 + 重试 |
| 终止状态 | 无 | 10 种穷举类型 |

---

## 下一步行动

### 立即可做 (今天)
1. 在 general_agent 中集成 Agent Loop (已有 general_agent_v4.py 示例)
2. 为 ops_agent 注册 5-10 个实际工具 (host_check, log_query, etc.)
3. 测试多轮 tool-call 流程

### 本周目标
1. 完成 Memory 文件化 (Phase 3)
2. 实现基础 Hooks 系统 (Phase 4 关键事件)
3. Context Compression 基础版 (Phase 5 层 0-2)

### 本月目标
1. 全量迁移现有 agents 到 v4 架构
2. 性能优化 (目标: 响应时间降低 50%)
3. 完整文档 + 迁移指南

---

## 核心文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `core/agent_loop.py` | ✅ 完成 | Agent Loop AsyncGenerator |
| `core/tool_system.py` | ✅ 完成 | Tool System + 注册中心 |
| `agents/general_agent_v4.py` | ✅ 完成 | 迁移示例 |
| `core/memory/` | ⏳ 待创建 | Memory 文件化系统 |
| `core/hooks.py` | ⏳ 待创建 | Hooks 系统 |
| `core/context_compression.py` | ⏳ 待创建 | Context Compression |

---

*创建时间: 2026-06-08*
*基于: Claude Code / Claw Code / AutoResearch 架构研究*
