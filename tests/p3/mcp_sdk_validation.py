"""Z-OPS P3 — 官方 MCP SDK 隔离验证 (以 /opt/zops-mcp-venv/bin/python 运行)

SPEC: 通过隔离验证锁定官方 MCP Python SDK, 增加 stdio、Streamable HTTP transport。

验证内容 (双 transport 全回环):
  1. stdio: 子进程启动内嵌 mock server → initialize → list_tools → call_tool;
  2. streamable HTTP: 本机 FastMCP (streamable-http) → 同一回环;
  3. 白名单/stdio 命令登记校验 (core/mcp_registry, 与 SDK 无关但同链路);
  4. 缓存隔离键 (配置指纹/主体/授权版本)。

运行: /opt/zops-mcp-venv/bin/python tests/p3/mcp_sdk_validation.py
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


# mock server 脚本 (stdio transport 子进程运行)
_MOCK_SERVER = '''
import sys
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="mock-ops")

@mcp.tool()
def query_backup(vm: str) -> str:
    """查询 VM 备份状态"""
    return f"backup-ok:{vm}"

@mcp.tool()
def dangerous_wipe(vm: str) -> str:
    """危险操作 (应在白名单外被过滤)"""
    return f"wiped:{vm}"

if __name__ == "__main__":
    mcp.run()
'''


async def validate_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    tmp = tempfile.mkdtemp(prefix="zops_mcp_")
    script = os.path.join(tmp, "mock_server.py")
    with open(script, "w") as f:
        f.write(_MOCK_SERVER)

    params = StdioServerParameters(
        command=sys.executable,        # venv python (登记的命令)
        args=[script],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await sess.list_tools()
            names = sorted(t.name for t in res.tools)
            check("stdio: initialize + list_tools", "query_backup" in names, str(names))
            out = await sess.call_tool("query_backup", arguments={"vm": "VM001"})
            text = out.content[0].text if out.content else ""
            check("stdio: call_tool 回环", "backup-ok:VM001" in text, text)
            check("stdio: 无错误", not out.is_error)


async def validate_http():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.mcpserver import MCPServer
    import uvicorn

    mcp = MCPServer(name="mock-ops-http")

    @mcp.tool()
    def query_backup(vm: str) -> str:
        """查询 VM 备份状态"""
        return f"http-backup-ok:{vm}"

    app = mcp.streamable_http_app()
    config_uv = uvicorn.Config(app, host="127.0.0.1", port=8931, log_level="error")
    server = uvicorn.Server(config_uv)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)

    try:
        async with streamable_http_client("http://127.0.0.1:8931/mcp") as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                res = await sess.list_tools()
                names = [t.name for t in res.tools]
                check("http: initialize + list_tools", "query_backup" in names, str(names))
                out = await sess.call_tool("query_backup", arguments={"vm": "VM002"})
                text = out.content[0].text if out.content else ""
                check("http: call_tool 回环", "http-backup-ok:VM002" in text, text)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def validate_registry_and_cache():
    from core import mcp_registry
    from core.mcp_official import MCPCacheKey

    import config
    config.MCP_APPROVED_SERVERS = json.dumps([
        {"id": "mock", "transport": "stdio", "command": "/opt/zops-mcp-venv/bin/python",
         "args": ["/tmp/mock.py"], "allowed_tools": ["query_backup"]},
        {"id": "http-mock", "transport": "http", "url": "http://127.0.0.1:8931/mcp",
         "allowed_tools": ["*"]},
        {"id": "bad-rel", "transport": "stdio", "command": "nc -e /bin/sh"},
    ])
    check("registry: 未登记 server 拒绝", not mcp_registry.is_approved("unknown"))
    ok, why = mcp_registry.validate_stdio("bad-rel")
    check("registry: 相对路径/元字符 stdio 命令拒绝", not ok and "绝对路径" in why, why)
    ok2, _ = mcp_registry.validate_stdio("mock")
    check("registry: 登记的绝对路径 stdio 通过", ok2)
    check("registry: allowed_tools 过滤 (dangerous_wipe 不放行)",
          not mcp_registry.tool_allowed("mock", "dangerous_wipe")
          and mcp_registry.tool_allowed("mock", "query_backup"))
    k1 = MCPCacheKey("mock", actor="alice", authz_version="v1")
    k2 = MCPCacheKey("mock", actor="bob", authz_version="v1")
    k3 = MCPCacheKey("mock", actor="alice", authz_version="v2")
    check("cache: 主体/授权版本隔离键不同",
          len({k1.cache_id(), k2.cache_id(), k3.cache_id()}) == 3)
    # 配置指纹: 改 allowed_tools → 缓存键变化 (先固化旧键再改配置)
    k1_id = k1.cache_id()
    config.MCP_APPROVED_SERVERS = json.dumps([
        {"id": "mock", "transport": "stdio", "command": "/opt/zops-mcp-venv/bin/python",
         "args": ["/tmp/mock.py"], "allowed_tools": ["*"]}])
    k4 = MCPCacheKey("mock", actor="alice", authz_version="v1")
    check("cache: 服务配置变化 → 指纹失效", k4.cache_id() != k1_id)


async def validate_official_layer_uses_sdk():
    """core/mcp_official 在有 SDK 的环境下: 白名单 server 可经官方 SDK 双 transport 执行"""
    from core import mcp_official
    check("official: SDK 可用性探测", mcp_official.sdk_available())

    import config
    tmp = tempfile.mkdtemp(prefix="zops_mcp2_")
    script = os.path.join(tmp, "mock_server.py")
    with open(script, "w") as f:
        f.write(_MOCK_SERVER)
    config.MCP_APPROVED_SERVERS = json.dumps([
        {"id": "mock2", "transport": "stdio", "command": sys.executable,
         "args": [script], "allowed_tools": ["query_backup"]}])

    tools = await mcp_official.list_tools("mock2",
                                          cache_key=mcp_official.MCPCacheKey("mock2", actor="alice"))
    check("official: list_tools (白名单过滤掉 dangerous_wipe)",
          [t["name"] for t in tools] == ["query_backup"], str(tools))
    out = await mcp_official.call_tool("mock2", "query_backup", {"vm": "VM003"})
    check("official: call_tool 经官方 SDK 回环", out.get("ok") and "backup-ok:VM003" in out.get("content", ""), str(out))
    try:
        await mcp_official.call_tool("mock2", "dangerous_wipe", {})
        check("official: 未授权工具拒绝", False)
    except PermissionError:
        check("official: 未授权工具拒绝", True)


async def main():
    await validate_stdio()
    await validate_http()
    validate_registry_and_cache()
    await validate_official_layer_uses_sdk()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
