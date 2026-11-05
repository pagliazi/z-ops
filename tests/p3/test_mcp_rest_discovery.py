"""Z-OPS P3 验收测试 — MCP REST 发现链路 (task-c17 收尾: 正式 server 可用化)

覆盖:
  1. normalize_tools 形状归一化 (list / {"tools":[]} / {"data":[]} / 脏数据) + schema 合成;
  2. SSE 非 JSON / SSE 连接异常 → 降级 REST 发现 (旧实现直接返回空 → 白名单 server 永远休眠);
  3. 降级请求携带 X-API-Key 且 URL 规范化 (尾斜杠避免 301);
  4. fail-closed 白名单门控 (未登记 transport=rest server 一律拒绝发现);
  5. SSL 参数语义 (verify=True 显式证书上下文; 关闭时 False, 不自默放宽)。

隔离: 无真实网络 — aiohttp 层打桩 (仅测试进程内)。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


class FakeResp:
    def __init__(self, status=200, payload=None, content_type="application/json"):
        self.status = status
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes, sink):
        self._routes = routes
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None, timeout=None, ssl=None, **kw):
        self._sink.append({"url": url, "headers": dict(headers or {}), "ssl": ssl})
        for frag, factory in self._routes.items():
            if frag in url:
                return factory()
        return FakeResp(status=404)


class BoomSession(FakeSession):
    def get(self, url, **kw):
        if "sse" in url:
            raise OSError("connection refused")
        return FakeResp(200, {"tools": [{"name": "vm_snapshots", "params": ["vm_name"]}]})


class AiohttpShim:
    """替换 core.mcp_adapter.aiohttp (测试进程内打桩, 不触网)"""

    def __init__(self, maker):
        self._maker = maker

    def ClientSession(self, *a, **k):
        return self._maker()

    class ClientTimeout:
        def __init__(self, *a, **k):
            pass

    ClientError = OSError


def patch_adapter(maker):
    import core.mcp_adapter as ma
    orig = ma.aiohttp
    ma.aiohttp = AiohttpShim(maker)
    return ma, orig


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def case_normalize():
    from core.mcp_adapter import normalize_tools
    a = normalize_tools({"tools": [{"name": "t1", "params": ["x", "y"], "module": "m"}]})
    check("归一: dict{tools} + params 合成 schema",
          len(a) == 1 and set(a[0]["input_schema"]["properties"]) == {"x", "y"}
          and a[0]["input_schema"]["type"] == "object")
    b = normalize_tools([{"name": "t2", "input_schema": {"type": "object"}}])
    check("归一: 裸 list 向后兼容", len(b) == 1 and b[0]["input_schema"] == {"type": "object"})
    c = normalize_tools({"data": [{"name": "t3"}]})
    check("归一: dict{data} 变体", len(c) == 1)
    d = normalize_tools({"tools": [{"no_name": 1}, "junk", None]})
    check("归一: 脏条目丢弃 (无 name/非 dict)", len(d) == 0)
    check("归一: 空输入不抛异常", normalize_tools(None) == [])
    f = normalize_tools({"tools": [{"name": "t4", "description": "已带描述"}]})
    check("归一: 保留服务端描述", f[0]["description"] == "已带描述")


def case_sse_fallback():
    sink = []
    payload = {"tools": [{"name": "snapshot_list", "params": ["limit"]}]}
    routes = {"/mcp/sse": lambda: FakeResp(200, None, content_type="text/event-stream"),
              "/mcp/tools/": lambda: FakeResp(200, payload)}
    maker = lambda: FakeSession(routes, sink)  # noqa: E731
    ma, orig = patch_adapter(maker)
    try:
        ma.MCP_CONFIG["api_key"] = "test-key"
        ad = ma.MCPToolAdapter(tool_pool=None)
        tools = run(ad.discover_tools())
        check("发现: SSE 流式不可解析 → 降级 REST (旧实现返回空)",
              len(tools) == 1 and tools[0]["name"] == "snapshot_list", str(tools))
        rest_calls = [c for c in sink if "tools/" in c["url"]]
        check("发现: 降级请求携带 X-API-Key", bool(rest_calls)
              and rest_calls[-1]["headers"].get("X-API-Key") == "test-key")
        check("发现: REST URL 尾斜杠规范化 (避免 301 丢体)",
              bool(rest_calls) and rest_calls[-1]["url"].endswith("/"), 
              rest_calls[-1]["url"] if rest_calls else "无请求")
    finally:
        ma.aiohttp = orig


def case_connection_error_fallback():
    ma, orig = patch_adapter(lambda: BoomSession({}, []))
    try:
        ad = ma.MCPToolAdapter(tool_pool=None)
        tools = run(ad.discover_tools())
        check("发现: SSE 连接异常仍降级 REST (不废弃流程)",
              len(tools) == 1 and tools[0]["name"] == "vm_snapshots", str(tools))
    finally:
        ma.aiohttp = orig


def case_failclosed_gate():
    import core.mcp_adapter as ma
    from core import mcp_registry
    orig = mcp_registry.approved_servers
    mcp_registry.approved_servers = lambda: [
        {"id": "other", "transport": "http", "url": "https://example.invalid/mcp"}]
    try:
        ad = ma.MCPToolAdapter(tool_pool=None)
        n = run(ad.discover_and_register())
        check("门控: rest server 未登记白名单 → 拒绝发现 (fail-closed)", n == 0, str(n))
    finally:
        mcp_registry.approved_servers = orig


def case_ssl_param():
    import core.mcp_adapter as ma
    orig = dict(ma.MCP_CONFIG)
    try:
        ma.MCP_CONFIG["verify_ssl"] = True
        import ssl as _ssl
        check("SSL: verify=True 用显式证书上下文 (非系统默认库)",
              isinstance(ma._ssl_param(), _ssl.SSLContext))
        ma.MCP_CONFIG["verify_ssl"] = False
        check("SSL: verify=False 显式关闭 (语义与旧行为一致)", ma._ssl_param() is False)
    finally:
        ma.MCP_CONFIG.clear()
        ma.MCP_CONFIG.update(orig)


def main():
    case_normalize()
    case_sse_fallback()
    case_connection_error_fallback()
    case_failclosed_gate()
    case_ssl_param()
    print("\n" + "═" * 46)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
