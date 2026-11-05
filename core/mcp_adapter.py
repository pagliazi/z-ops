"""
Z-OPS v4 — MCP Tool Adapter (外部 MCP 服务集成)

将外部 MCP (Model Context Protocol) 服务暴露的工具注册到 Z-OPS Tool System

MCP 端点:
- SSE: https://opscore-mgt.example.com/mcp/sse (16 tools)
- REST API: https://opscore-mgt.example.com/api/snapshot/mcp/tools/<name>/

用法:
    from core.mcp_adapter import MCPToolAdapter
    
    # 自动发现并注册所有 MCP 工具
    adapter = MCPToolAdapter(tool_pool)
    await adapter.discover_and_register()
    
    # 或手动注册指定工具
    await adapter.register_tool("query_vm")
"""

import json
import time
import logging
import aiohttp
from typing import Any, Dict, List, Optional

from .tool_system import BaseTool, ToolContext, ToolResult, PermissionLevel, ToolPool
import config  # 修复:core 作为顶级包导入时相对导入失败

logger = logging.getLogger("zops")


# ═══ MCP 配置 ═══

MCP_CONFIG = {
    "sse_endpoint": config.MCP_SSE_ENDPOINT,
    "rest_base": config.MCP_REST_BASE,
    "timeout": config.MCP_TIMEOUT,
    "verify_ssl": config.MCP_VERIFY_SSL,
    "api_key": config.MCP_API_KEY,
}


def _mcp_headers() -> Dict[str, str]:
    """构造 MCP REST 调用鉴权头（X-API-Key）"""
    api_key = MCP_CONFIG.get("api_key", "")
    if api_key:
        return {"X-API-Key": api_key}
    return {}


def _ssl_param():
    """aiohttp 的 ssl 参数: True 走系统默认 CA 库 (与 httpx/certifi 不一致,
    对中间链不完整的网关会 CERTIFICATE_VERIFY_FAILED) → 显式用 certifi 证书库。
    验证关闭时返回 False (与旧行为一致)。"""
    if not MCP_CONFIG.get("verify_ssl", True):
        return False
    try:
        import ssl as _ssl
        import certifi
        ctx = _ssl.create_default_context(cafile=certifi.where())
        return ctx
    except Exception:
        return True   # certifi 不可用时保持旧行为 (宁可误拒, 不自默降级)


def normalize_tools(payload) -> List[Dict]:
    """工具清单归一化 (MCP REST 网关实测返回 {"tools": [...]} 包装 + params 名称列表)

    - 兼容 list / {"tools": list} / {"data": list} 三种形状;
    - 缺 input_schema 时由 params 名称列表合成宽容 schema (否则工具注册后 LLM 无法调用)。
    """
    if isinstance(payload, dict):
        tools = payload.get("tools")
        if tools is None:
            data = payload.get("data")
            tools = data if isinstance(data, list) else []
    elif isinstance(payload, list):
        tools = payload
    else:
        tools = []
    out: List[Dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        if not name:
            continue
        schema = t.get("input_schema") or t.get("inputSchema") or t.get("schema")
        if not schema:
            props = {p: {"type": "string"} for p in (t.get("params") or [])}
            schema = {"type": "object", "properties": props}
        out.append({
            "name": name,
            "description": t.get("description") or f"MCP 工具 ({t.get('module') or 'remote'})",
            "input_schema": schema,
            "module": t.get("module") or "",
        })
    return out


# ═══ MCP 工具包装器 ═══

class MCPToolWrapper(BaseTool):
    """
    MCP 远程工具包装器
    
    将 REST API 端的 MCP 工具包装为 Z-OPS BaseTool
    """
    
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict,
        rest_url: str,
        permission: PermissionLevel = PermissionLevel.READ_ONLY,
        is_concurrency_safe: bool = True,
    ):
        self._name = name
        self._description = description
        self._input_schema = input_schema
        self._rest_url = rest_url
        self._permission = permission
        self._is_concurrency_safe = is_concurrency_safe
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def description(self) -> str:
        return self._description
    
    @property
    def input_schema(self) -> Dict:
        return self._input_schema
    
    @property
    def is_concurrency_safe(self) -> bool:
        return self._is_concurrency_safe
    
    @property
    def permission(self) -> PermissionLevel:
        return self._permission
    
    async def call(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """调用远程 MCP 工具"""
        start_time = time.time()
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rest_url,
                    headers=_mcp_headers(),
                    json={
                        "tool_name": self._name,
                        "arguments": kwargs,
                        "session_id": ctx.session_id,
                        "trace_id": ctx.trace_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=MCP_CONFIG["timeout"]),
                    ssl=_ssl_param(),
                ) as response:
                    duration_ms = (time.time() - start_time) * 1000
                    
                    if response.status == 200:
                        result = await response.json()
                        return ToolResult(
                            tool_call_id="",  # 由调用方填充
                            tool_name=self._name,
                            success=True,
                            output=json.dumps(result, ensure_ascii=False, indent=2),
                            duration_ms=duration_ms,
                        )
                    else:
                        error_text = await response.text()
                        return ToolResult(
                            tool_call_id="",
                            tool_name=self._name,
                            success=False,
                            output=f"MCP 工具调用失败 (HTTP {response.status})",
                            error=error_text,
                            duration_ms=duration_ms,
                        )
        
        except aiohttp.ClientError as e:
            duration_ms = (time.time() - start_time) * 1000
            return ToolResult(
                tool_call_id="",
                tool_name=self._name,
                success=False,
                output=f"MCP 工具调用异常: {str(e)}",
                error=str(e),
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ToolResult(
                tool_call_id="",
                tool_name=self._name,
                success=False,
                output=f"MCP 工具调用未知错误: {str(e)}",
                error=str(e),
                duration_ms=duration_ms,
            )


# ═══ MCP 工具适配器 ═══

class MCPToolAdapter:
    """
    MCP 工具适配器 — 自动发现并注册外部 MCP 工具
    
    用法:
        adapter = MCPToolAdapter(tool_pool)
        count = await adapter.discover_and_register()
        print(f"注册了 {count} 个 MCP 工具")
    """
    
    def __init__(self, tool_pool: ToolPool, config: Optional[Dict] = None):
        self.tool_pool = tool_pool
        self.config = {**MCP_CONFIG, **(config or {})}
        self._discovered_tools: List[Dict] = []
    
    async def discover_tools(self) -> List[Dict]:
        """
        从 MCP SSE 端点发现所有可用工具
        
        返回: [{"name": "...", "description": "...", "input_schema": {...}}, ...]
        """
        logger.info(f"[mcp_adapter] 正在发现 MCP 工具: {self.config['sse_endpoint']}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.config["sse_endpoint"],
                    headers=_mcp_headers(),
                    timeout=aiohttp.ClientTimeout(total=self.config["timeout"]),
                    ssl=_ssl_param(),
                ) as response:
                    if response.status != 200:
                        # 旧行为直接返回空→SSE 不可用时 REST 白名单 server 永远休眠。
                        # 实际网关仅提供 REST 清单时, 必须降级到 REST 发现路径。
                        logger.warning(f"[mcp_adapter] SSE 端点返回 HTTP {response.status}, 降级 REST 发现")
                        return await self._discover_via_rest(session)
                    
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        self._discovered_tools = normalize_tools(await response.json())
                        logger.info(f"[mcp_adapter] 发现 {len(self._discovered_tools)} 个 MCP 工具")
                        return self._discovered_tools
                    else:
                        # SSE 流 - 需要特殊处理
                        logger.warning("[mcp_adapter] SSE 端点需要流式解析，使用 REST API 备选方案")
                        return await self._discover_via_rest(session)
        
        except Exception as e:
            # SSE 不可达 (连接失败/超时) 同样降级 REST, 不废弃整个发现流程
            logger.warning(f"[mcp_adapter] SSE 发现异常: {e} — 降级 REST 发现")
            return await self._discover_via_rest(None)
    
    async def _discover_via_rest(self, session=None) -> List[Dict]:
        """通过 REST 清单端点发现工具 (白名单已在 discover_and_register 处门控)"""
        tools_url = self.config["rest_base"].rstrip("/") + "/"
        try:
            # 统一自建 session (传入的 SSE session 与本请求无关, 复用反而受其超时影响)
            async with aiohttp.ClientSession() as own:
                async with own.get(
                    tools_url,
                    headers=_mcp_headers(),
                    timeout=aiohttp.ClientTimeout(total=self.config["timeout"]),
                    ssl=_ssl_param(),
                ) as response:
                    if response.status == 200:
                        self._discovered_tools = normalize_tools(await response.json())
                        logger.info(f"[mcp_adapter] 通过 REST 发现 {len(self._discovered_tools)} 个工具")
                        return self._discovered_tools
                    logger.error(f"[mcp_adapter] REST 端点返回 HTTP {response.status}")
                    return []
        except Exception as e:
            logger.error(f"[mcp_adapter] REST 发现失败: {e}")
            return []
    
    async def register_all(self) -> int:
        """注册所有发现的工具"""
        if not self._discovered_tools:
            await self.discover_tools()
        
        count = 0
        for tool_info in self._discovered_tools:
            try:
                await self._register_single_tool(tool_info)
                count += 1
            except Exception as e:
                logger.error(f"[mcp_adapter] 注册工具 {tool_info.get('name')} 失败: {e}")
        
        logger.info(f"[mcp_adapter] 成功注册 {count}/{len(self._discovered_tools)} 个 MCP 工具")
        return count
    
    async def _register_single_tool(self, tool_info: Dict):
        """注册单个 MCP 工具"""
        name = tool_info.get("name")
        description = tool_info.get("description", "")
        input_schema = tool_info.get("input_schema", tool_info.get("schema", {}))
        
        if not name:
            logger.warning("[mcp_adapter] 工具缺少 name 字段，跳过")
            return
        
        # 构建 REST URL
        rest_url = f"{self.config['rest_base']}/{name}/"
        
        # 判断权限级别
        permission = self._infer_permission(tool_info)
        
        # 创建包装器
        wrapper = MCPToolWrapper(
            name=name,
            description=description,
            input_schema=input_schema,
            rest_url=rest_url,
            permission=permission,
            is_concurrency_safe=True,  # MCP 工具默认并发安全
        )
        
        # 注册到 ToolPool
        self.tool_pool.register(wrapper)
        logger.info(f"[mcp_adapter] 注册 MCP 工具: {name} (权限={permission.value})")
    
    def _infer_permission(self, tool_info: Dict) -> PermissionLevel:
        """根据工具信息推断权限级别"""
        name = tool_info.get("name", "").lower()
        description = tool_info.get("description", "").lower()
        
        # 简单启发式规则
        destructive_keywords = ["delete", "remove", "destroy", "drop", "truncate"]
        write_keywords = ["create", "update", "modify", "write", "set", "add"]
        
        for keyword in destructive_keywords:
            if keyword in name or keyword in description:
                return PermissionLevel.DESTRUCTIVE
        
        for keyword in write_keywords:
            if keyword in name or keyword in description:
                return PermissionLevel.WRITE
        
        return PermissionLevel.READ_ONLY
    
    async def discover_and_register(self) -> int:
        """发现并注册所有 MCP 工具 (便捷方法)

        P3 (task-c17): REST 适配同样受已批准 server 白名单约束 (fail-closed) —
        仅当登记 transport=rest 且 url 与 MCP_REST_BASE 同源时放行; 未登记一律拒绝。
        """
        from core import mcp_registry
        approved = mcp_registry.approved_servers()
        rest_servers = [s for s in approved if s.get("transport") == "rest"]
        if not any(str(s.get("url") or "").rstrip("/") ==
                   str(MCP_CONFIG.get("rest_base") or "").rstrip("/")
                   or str(MCP_CONFIG.get("rest_base") or "").rstrip("/").startswith(
                       str(s.get("url") or "").rstrip("/"))
                   for s in rest_servers):
            logger.warning("[mcp_adapter] MCP REST 端点未在 MCP_APPROVED_SERVERS 白名单登记, "
                           "发现被拒绝 (fail-closed)")
            return 0
        await self.discover_tools()
        return await self.register_all()
    
    def get_discovered_tools(self) -> List[Dict]:
        """获取已发现的工具列表"""
        return self._discovered_tools.copy()


# ═══ 便捷函数 ═══

async def register_mcp_tools(
    tool_pool: ToolPool,
    config: Optional[Dict] = None,
) -> int:
    """
    便捷函数: 发现并注册所有 MCP 工具
    
    用法:
        from core.mcp_adapter import register_mcp_tools
        from core.tool_system import ToolPool
        
        pool = ToolPool()
        count = await register_mcp_tools(pool)
        print(f"注册了 {count} 个 MCP 工具")
    """
    adapter = MCPToolAdapter(tool_pool, config)
    return await adapter.discover_and_register()
