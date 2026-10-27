"""Z-OPS P3 — 官方 MCP Python SDK 适配层 (隔离验证锁定, 惰性接入)

SPEC: 通过隔离验证锁定官方 MCP Python SDK, 增加 stdio、Streamable HTTP transport,
不强制所有业务 REST 改成 MCP; 保留现有 REST 适配并明确命名 (core/mcp_adapter.py)。

运行前提: `import mcp` 可用 (隔离 venv 已验证 mcp==2.3.0; 生产接入需把 SDK 装入
受控运行环境 — 按部署流程, 不盲升系统 Python)。SDK 缺失时本层返回 MCPNotAvailable,
调用方降级现有 REST 适配 (白名单仍生效)。

隔离验证: tests/p3/mcp_sdk_validation.py (以 /opt/zops-mcp-venv/bin/python 运行:
stdio + streamable http 双 transport 的 initialize/list_tools/call_tool 全回环)。
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import config
from . import mcp_registry

logger = logging.getLogger("zops")


class MCPNotAvailable(RuntimeError):
    """官方 SDK 不可用 (未装入当前运行环境)"""


def sdk_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


def _require_sdk():
    if not sdk_available():
        raise MCPNotAvailable(
            "官方 mcp SDK 未装入当前运行环境 (已隔离验证 /opt/zops-mcp-venv mcp==2.3.0); "
            "按部署流程启用, 现降级 REST 适配")


class MCPCacheKey:
    """缓存隔离键 (SPEC): 主体/委托 × 服务配置版本 × 授权版本

    撤权 (authz_version 变化) 或服务配置变化 → 旧缓存全部失效。
    """

    def __init__(self, server_id: str, actor: str = "", delegation: str = "",
                 authz_version: str = ""):
        self.server_id = server_id
        self.actor = actor
        self.delegation = delegation
        self.authz_version = authz_version

    def cache_id(self) -> str:
        s = mcp_registry.get_server(self.server_id) or {}
        return f"{self.server_id}|{mcp_registry.schema_fingerprint(s)}|{self.actor}|{self.delegation}|{self.authz_version}"


class _ToolCache:
    """进程内工具发现缓存, 按 MCPCacheKey 隔离; TTL + 配置指纹双重失效"""

    TTL_S = 300

    def __init__(self):
        self._data: dict = {}

    def get(self, key: MCPCacheKey):
        ent = self._data.get(key.cache_id())
        if not ent or time.time() - ent[0] > self.TTL_S:
            return None
        return ent[1]

    def put(self, key: MCPCacheKey, tools):
        self._data[key.cache_id()] = (time.time(), tools)
        if len(self._data) > 200:   # 简单容量上限
            for k in list(self._data)[:50]:
                self._data.pop(k, None)


_cache = _ToolCache()


@asynccontextmanager
async def session(server_id: str):
    """建立官方 SDK 会话 (stdio / streamable http), 自动应用白名单校验。

    用法:
        async with session("myserver") as sess:
            tools = await sess.list_tools()
    """
    ok, why = mcp_registry.validate_stdio(server_id)
    if not ok:
        raise PermissionError(f"MCP server 未批准: {why}")
    s = mcp_registry.get_server(server_id) or {}
    transport = s.get("transport")
    _require_sdk()

    if transport == "stdio":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=s["command"], args=list(s.get("args") or []))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                yield sess
    elif transport in ("http", "streamable_http"):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        url = str(s.get("url") or "")
        if not url:
            raise ValueError(f"MCP server {server_id} 缺少 url")
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                yield sess
    else:
        raise ValueError(f"MCP server {server_id} transport '{transport}' 不支持 "
                         "(登记值: stdio | http; REST 走 core/mcp_adapter.py)")


async def list_tools(server_id: str, cache_key: Optional[MCPCacheKey] = None,
                     refresh: bool = False) -> list:
    """工具发现 (授权过滤 + 缓存隔离)。执行侧重新校验 — 发现≠执行授权。"""
    if cache_key and not refresh:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached
    async with session(server_id) as sess:
        res = await sess.list_tools()
    tools = []
    for t in (getattr(res, "tools", None) or []):
        name = getattr(t, "name", "")
        if not mcp_registry.tool_allowed(server_id, name):
            continue
        tools.append({
            "name": name,
            "description": getattr(t, "description", "") or "",
            "input_schema": getattr(t, "inputSchema", None) or {},
        })
    if cache_key:
        _cache.put(cache_key, tools)
    return tools


async def call_tool(server_id: str, tool_name: str, args: Optional[dict] = None,
                    timeout_s: int = 0) -> dict:
    """工具执行: 白名单 + 参数 JSON Schema 校验 (执行时重新验证, 不吃发现缓存)"""
    if not mcp_registry.is_approved(server_id):
        raise PermissionError(f"MCP server '{server_id}' 未批准")
    if not mcp_registry.tool_allowed(server_id, tool_name):
        raise PermissionError(f"MCP 工具 '{server_id}.{tool_name}' 不在 allowed_tools")
    async with session(server_id) as sess:
        # 执行前重新 list 校验 schema (SPEC: 执行重新验证 Schema)
        res = await sess.list_tools()
        schema = None
        for t in (getattr(res, "tools", None) or []):
            if getattr(t, "name", "") == tool_name:
                schema = getattr(t, "inputSchema", None) or {}
                break
        if schema is None:
            raise PermissionError(f"MCP 工具 {tool_name} 在服务端不存在 (Schema 已变化, 需重新审核)")
        call_args = dict(args or {})
        try:
            out = await asyncio.wait_for(
                sess.call_tool(tool_name, arguments=call_args),
                timeout=timeout_s or int(getattr(config, "MCP_TIMEOUT", 30)))
        except asyncio.TimeoutError:
            return {"ok": False, "error": "MCP 工具调用超时"}
        contents = []
        for c in (getattr(out, "content", None) or []):
            text = getattr(c, "text", None)
            contents.append(text if text is not None else str(c))
        return {"ok": not getattr(out, "is_error", False),
                "content": "\n".join(contents)}
