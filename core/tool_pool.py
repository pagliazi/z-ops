"""
Z-OPS ToolPool — 动态工具发现 + 跨 Agent 工具注册
参考 claude-code ToolSearchTool + AgentTool + claw-code tool_pool.py

核心能力:
1. ToolPool: 全局工具池，跨 Agent 工具注册/发现/搜索
2. AgentTool: 跨 Agent 子任务委派 (参考 claude-code AgentTool)
3. ToolSearchTool: 延迟工具发现 (参考 claude-code ToolSearchTool)
4. VibeCoding 集成: 自然语言 → 工具选择 → 执行
"""

import json
import time
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("zops")


@dataclass
class AgentCapability:
    """Agent 能力描述 (用于 ToolPool 跨 Agent 发现)"""
    agent_id: str = ""
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    status: str = "online"      # online / offline
    last_seen: float = 0.0

    def matches(self, query: str) -> float:
        """计算与查询的匹配分数 (0-1)"""
        q = query.lower()
        score = 0.0
        if q in self.description.lower():
            score += 0.5
        for cap in self.capabilities:
            if q in cap.lower():
                score += 0.3
        for tn in self.tool_names:
            if q in tn.lower():
                score += 0.2
        return min(score, 1.0)


class ToolPool:
    """
    全局工具池 — 跨 Agent 工具注册/发现
    
    参考:
    - claude-code tools.ts: 全局工具注册
    - claw-code tool_pool.py: 工具发现/过滤
    - claude-code AgentTool: 子 Agent 生成
    - claude-code ToolSearchTool: 延迟工具发现
    
    使用方式:
        pool = get_tool_pool()
        pool.register_agent("gcoc_agent", tools=[...], capabilities=[...])
        results = pool.search_tools("备份查询")
        agent = pool.find_best_agent("查询客户资源池")
    """

    def __init__(self):
        self._agents: Dict[str, AgentCapability] = {}
        self._all_tools: Dict[str, dict] = {}  # tool_name -> {agent_id, tool_def}
        self._initialized = False

    def register_agent(self, agent_id: str, description: str = "",
                        capabilities: List[str] = None,
                        tools: list = None):
        """注册 Agent 及其工具到全局池 (同时持久化到 Redis)"""
        tool_names = []
        tool_summaries = []  # 用于 Redis 持久化
        if tools:
            for t in tools:
                name = t.name if hasattr(t, 'name') else t.get('name', '')
                desc = t.description if hasattr(t, 'description') else str(t)[:100]
                if name:
                    tool_names.append(name)
                    self._all_tools[name] = {
                        "agent_id": agent_id,
                        "tool_def": t,
                        "registered_at": time.time(),
                    }
                    tool_summaries.append({"name": name, "description": desc[:120]})
        
        self._agents[agent_id] = AgentCapability(
            agent_id=agent_id,
            description=description,
            capabilities=capabilities or [],
            tool_names=tool_names,
            status="online",
            last_seen=time.time(),
        )
        logger.info(f"[tool_pool] 注册 {agent_id}: {len(tool_names)} 个工具, {len(capabilities or [])} 项能力")
        # 持久化到 Redis (供可观测性 API 跨进程读取)
        self._persist_to_redis(agent_id, description, capabilities or [], tool_summaries)

    def _persist_to_redis(self, agent_id: str, description: str,
                          capabilities: List[str], tool_summaries: list):
        """将注册信息持久化到 Redis (同步, 用于启动时)"""
        try:
            import redis as _redis
            import json as _json
            r = _redis.Redis(decode_responses=True)
            entry = _json.dumps({
                "agent_id": agent_id,
                "description": description,
                "capabilities": capabilities,
                "tools": tool_summaries,
                "tool_count": len(tool_summaries),
                "status": "online",
                "registered_at": time.time(),
            }, ensure_ascii=False)
            r.hset("zops:toolpool:agents", agent_id, entry)
            r.expire("zops:toolpool:agents", 86400 * 7)  # 7天 TTL
            r.close()
        except Exception as e:
            logger.debug(f"[tool_pool] Redis 持久化失败: {e}")

    def heartbeat(self):
        """定期续期 Redis TTL + 更新 last_seen (由 agent 定时调用)"""
        try:
            import redis as _redis
            import json as _json
            r = _redis.Redis(decode_responses=True)
            for agent_id, cap in self._agents.items():
                cap.last_seen = time.time()
                raw = r.hget("zops:toolpool:agents", agent_id)
                if raw:
                    entry = _json.loads(raw)
                    entry["status"] = "online"
                    entry["last_seen"] = time.time()
                    r.hset("zops:toolpool:agents", agent_id, _json.dumps(entry, ensure_ascii=False))
            r.expire("zops:toolpool:agents", 86400 * 7)
            r.close()
        except Exception as e:
            logger.debug(f"[tool_pool] heartbeat 失败: {e}")

    def ensure_agent_registered(self, agent_id: str, description: str = "",
                                 capabilities: List[str] = None) -> bool:
        """若 Redis 中无该 agent 的注册则补注册 (已有注册不覆盖，保护 gcoc 等自带工具清单的条目)

        BaseAgent 心跳统一调用，保证全部 agent (含 nms/ops/network 等)
        都有 ToolPool 生产者，而不是只有显式 register_agent 的 3 个。
        返回 True 表示本次新写入。
        """
        try:
            import redis as _redis
            r = _redis.Redis(decode_responses=True)
            if r.hexists("zops:toolpool:agents", agent_id):
                r.close()
                return False
            r.close()
            self._persist_to_redis(agent_id, description, capabilities or [], [])
            return True
        except Exception as e:
            logger.debug(f"[tool_pool] ensure_agent_registered 失败: {e}")
            return False

    def touch_agent(self, agent_id: str):
        """刷新单个 agent 的 status/last_seen (不动工具清单，未注册则忽略)"""
        try:
            import redis as _redis
            import json as _json
            r = _redis.Redis(decode_responses=True)
            raw = r.hget("zops:toolpool:agents", agent_id)
            if raw:
                entry = _json.loads(raw)
                entry["status"] = "online"
                entry["last_seen"] = time.time()
                r.hset("zops:toolpool:agents", agent_id, _json.dumps(entry, ensure_ascii=False))
            r.expire("zops:toolpool:agents", 86400 * 7)
            r.close()
        except Exception as e:
            logger.debug(f"[tool_pool] touch_agent 失败: {e}")

    def unregister_agent(self, agent_id: str):
        """取消注册 Agent"""
        if agent_id in self._agents:
            # 移除该 Agent 的工具
            for tn in self._agents[agent_id].tool_names:
                self._all_tools.pop(tn, None)
            del self._agents[agent_id]

    def search_tools(self, query: str, limit: int = 10) -> List[dict]:
        """搜索全局工具 (参考 claude-code ToolSearchTool)"""
        q = query.lower()
        matches = []
        for name, info in self._all_tools.items():
            td = info["tool_def"]
            desc = td.description if hasattr(td, 'description') else str(td)
            if q in name.lower() or q in desc.lower():
                matches.append({
                    "name": name,
                    "agent_id": info["agent_id"],
                    "description": desc[:200],
                })
        return matches[:limit]

    def find_best_agent(self, query: str) -> Optional[str]:
        """找到最适合处理查询的 Agent (参考 claude-code AgentTool 选择逻辑)"""
        best_agent = None
        best_score = 0.0
        for agent_id, cap in self._agents.items():
            if cap.status != "online":
                continue
            score = cap.matches(query)
            if score > best_score:
                best_score = score
                best_agent = agent_id
        return best_agent if best_score > 0.1 else None

    def get_agent_tools(self, agent_id: str) -> List[dict]:
        """获取指定 Agent 的所有工具"""
        return [
            info for name, info in self._all_tools.items()
            if info["agent_id"] == agent_id
        ]

    def get_all_agents(self) -> List[dict]:
        """获取所有已注册 Agent 的概览"""
        return [
            {
                "agent_id": cap.agent_id,
                "description": cap.description,
                "tool_count": len(cap.tool_names),
                "capabilities": cap.capabilities[:5],
                "status": cap.status,
            }
            for cap in self._agents.values()
        ]

    @property
    def total_tools(self) -> int:
        return len(self._all_tools)

    @property
    def total_agents(self) -> int:
        return len(self._agents)


# ═══ AgentTool — 跨 Agent 子任务委派 ═══

def create_agent_tool(base_agent) -> "ToolDef":
    """
    创建 AgentTool (参考 claude-code AgentTool)
    
    允许当前 Agent 将子任务委派给其他 Agent。
    LLM 可通过 function calling 调用此工具，指定目标 agent 和任务描述。
    """
    from core.tool_schema import ToolDef, ToolInputSchema, ToolParam, ParamType, ToolExecution

    async def _execute_agent_tool(params=None, context=None):
        """执行跨 Agent 委派"""
        p = params or {}
        target_agent = p.get("target_agent", "")
        task = p.get("task", "")
        
        if not target_agent or not task:
            return ToolExecution(
                tool_name="delegate_to_agent",
                success=False,
                error="需要指定 target_agent 和 task"
            )
        
        pool = get_tool_pool()
        agent_cap = pool._agents.get(target_agent)
        if not agent_cap or agent_cap.status != "online":
            return ToolExecution(
                tool_name="delegate_to_agent",
                success=False,
                error=f"Agent {target_agent} 不在线或未注册"
            )
        
        try:
            from agents.base import AgentReply
            reply = await base_agent.send_and_wait(
                target_agent, action="chat", text=task, timeout=60
            )
            if reply.error:
                return ToolExecution(
                    tool_name="delegate_to_agent",
                    success=False,
                    error=f"{target_agent} 回复错误: {reply.error}"
                )
            result_text = reply.result.get("text", "") if reply.result else "无回复内容"
            return ToolExecution(
                tool_name="delegate_to_agent",
                success=True,
                output=f"[{target_agent}] {result_text[:3000]}",
                data={"agent": target_agent, "result": reply.result}
            )
        except Exception as e:
            return ToolExecution(
                tool_name="delegate_to_agent",
                success=False,
                error=f"委派失败: {e}"
            )

    # 获取可用 Agent 列表作为描述
    pool = get_tool_pool()
    agents_desc = ", ".join(
        f"{a['agent_id']}({a['description'][:20]})" 
        for a in pool.get_all_agents()
        if a['agent_id'] != base_agent.name
    )

    return ToolDef(
        name="delegate_to_agent",
        description=f"将子任务委派给其他 Agent 处理。可用 Agent: {agents_desc or '暂无'}",
        category="system",
        input_schema=ToolInputSchema(params=[
            ToolParam(name="target_agent", type=ParamType.STRING,
                      description="目标 Agent ID (如 kb_agent, ops_agent, general_agent)",
                      required=True),
            ToolParam(name="task", type=ParamType.STRING,
                      description="要委派的任务描述 (自然语言)",
                      required=True),
        ]),
        execute_fn=_execute_agent_tool,
        source_hint="agent_tool",
    )


def create_tool_search_tool() -> "ToolDef":
    """
    创建 ToolSearchTool (参考 claude-code ToolSearchTool)
    
    允许 LLM 搜索全局可用工具，实现延迟工具发现。
    """
    from core.tool_schema import ToolDef, ToolInputSchema, ToolParam, ParamType, ToolExecution

    async def _search_tools(params=None, context=None):
        p = params or {}
        query = p.get("query", "")
        if not query:
            return ToolExecution(
                tool_name="search_available_tools",
                success=False,
                error="需要搜索关键词"
            )
        pool = get_tool_pool()
        results = pool.search_tools(query, limit=10)
        if not results:
            return ToolExecution(
                tool_name="search_available_tools",
                success=True,
                output=f"未找到与 '{query}' 匹配的工具"
            )
        lines = [f"找到 {len(results)} 个匹配工具:"]
        for r in results:
            lines.append(f"  - {r['name']} ({r['agent_id']}): {r['description']}")
        return ToolExecution(
            tool_name="search_available_tools",
            success=True,
            output="\n".join(lines),
            data={"results": results}
        )

    return ToolDef(
        name="search_available_tools",
        description="搜索全局可用工具 — 输入关键词，发现跨 Agent 的可用工具",
        category="system",
        input_schema=ToolInputSchema(params=[
            ToolParam(name="query", type=ParamType.STRING,
                      description="搜索关键词 (如 '备份', '虚拟机', 'DNS')",
                      required=True),
        ]),
        execute_fn=_search_tools,
        source_hint="tool_search",
    )


# ═══ VibeCoding 集成 ═══

async def vibecoding_resolve(query: str, llm_chat_fn, pool: "ToolPool" = None) -> dict:
    """
    VibeCoding 自然语言解析 — 将用户自然语言意图解析为工具调用计划
    
    参考 claw-code 的 vibecoding 模式:
    用户说 "帮我查一下47141的备份和VM信息" →
    解析为: [
        {"tool": "query_crm_customer", "args": {"gid_or_name": "47141"}},
        {"tool": "query_backup_jobs", "args": {"search": "47141"}},
        {"tool": "query_smartops_vms", "args": {"search": "47141"}}
    ]
    """
    pool = pool or get_tool_pool()
    
    # 构建工具清单
    tool_list = []
    for name, info in pool._all_tools.items():
        td = info["tool_def"]
        desc = td.description if hasattr(td, 'description') else ""
        tool_list.append(f"  {name}: {desc[:80]}")
    
    if not tool_list:
        return {"plan": [], "reasoning": "无可用工具"}
    
    prompt = f"""用户意图: {query}

可用工具:
{chr(10).join(tool_list[:30])}

请分析用户意图，返回工具调用计划 (JSON 格式):
{{"plan": [{{"tool": "工具名", "args": {{"参数名": "参数值"}}}}], "reasoning": "分析理由"}}

只返回 JSON，不要其他内容。"""
    
    try:
        result = await llm_chat_fn(
            prompt=prompt,
            system="你是工具调用规划器。根据用户意图，选择最合适的工具组合。返回 JSON 格式的调用计划。",
            temperature=0.1,
        )
        # 解析 JSON
        import re
        text = result.strip()
        # 去掉 markdown fence
        fence = re.compile(r'```(?:json)?\s*\n?(.*?)\n?```', re.DOTALL)
        m = fence.search(text)
        if m:
            text = m.group(1).strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[tool_pool] vibecoding 解析失败: {e}")
        return {"plan": [], "reasoning": f"解析失败: {e}"}


# ═══ 全局单例 ═══

_tool_pool: Optional[ToolPool] = None

def get_tool_pool() -> ToolPool:
    global _tool_pool
    if _tool_pool is None:
        _tool_pool = ToolPool()
    return _tool_pool
