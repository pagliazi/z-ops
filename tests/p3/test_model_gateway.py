"""Z-OPS P3 验收 — 统一模型调用层 (model_gateway) 限流/退避/熔断 + 解析链

隔离: httpx MockTransport (无真实 LLM 调用) + mock redis。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


class MockRedis:
    def __init__(self, data=None):
        self.data = data or {}

    async def get(self, k):
        return self.data.get(k)


async def test_resolver_priority():
    import config as _cfg
    from core.model_gateway import resolve_model_config

    # 层级3: env 默认
    cfg = await resolve_model_config(MockRedis(), agent_id="a1")
    check("解析: env 兜底 (provider/model 来自 env)", cfg["provider"] == _cfg.LLM_PROVIDER)

    # 层级2: 全局 Redis pref + agent yaml model 同层合并 (model 可来自 yaml)
    r2 = MockRedis({"zops:llm_pref": '{"provider":"oneapi","model":"gpt-x","oneapi_key":"sk-oapi"}'})
    cfg2 = await resolve_model_config(r2, agent_id="a1", agent_yaml_model="deepseek-chat")
    check("解析: provider=全局层, model=yaml 层可合并", cfg2["provider"] == "oneapi" and cfg2["model"] == "deepseek-chat")
    check("解析: OneAPI 专用密钥优先 (不跨 provider 混用)", cfg2["api_key"] == "sk-oapi")

    # 层级1: per-agent provider/model 必须同层
    r3 = MockRedis({"zops:agent_model:a1": "anthropic/claude-x",
                    "zops:llm_pref": '{"provider":"oneapi","oneapi_key":"k"}'})
    cfg3 = await resolve_model_config(r3, agent_id="a1")
    check("解析: per-agent 覆盖 provider+model 同层生效",
          cfg3["provider"] == "anthropic" and cfg3["model"] == "claude-x")

    # anthropic 凭据绑定
    check("解析: anthropic 凭据不落到 oneapi key", cfg3["api_key"] == _cfg.ANTHROPIC_API_KEY)


async def test_gateway_backoff_breaker():
    import httpx
    import core.model_gateway as gw_mod
    from core.model_gateway import ModelGateway, _BreakerOpen

    calls = {"n": 0}

    def handler_429_then_ok(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0.01"},
                                  json={"error": "rate"})
        return httpx.Response(200, json={"ok": True, "choices": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_429_then_ok))
    gw = ModelGateway()
    cfg = {"provider": "deepseek", "api_key": "k", "base_url": "https://gw.test/v1"}
    resp = await gw.request_chat(cfg, {"model": "m"}, client)
    check("退避: 429×2 后按 Retry-After 重试成功 (200)", resp.status_code == 200 and calls["n"] == 3)

    # 429 耗尽 → 如实返回最终 429 (不虚构成功)
    calls["n"] = 0
    def handler_always_429(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0.01"})
    client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler_always_429))
    gw2 = ModelGateway()
    resp2 = await gw2.request_chat(cfg, {"model": "m"}, client2)
    check("退避: 耗尽重试后如实返回 429", resp2.status_code == 429
          and calls["n"] == gw_mod.BACKOFF_MAX_RETRY + 1, str(calls))

    # 熔断: 连续 5xx 达阈值 → 开闸快速失败
    def handler_500(request):
        return httpx.Response(500, text="boom")
    client3 = httpx.AsyncClient(transport=httpx.MockTransport(handler_500))
    gw3 = ModelGateway()
    blocked = False
    for i in range(8):
        try:
            await gw3.request_chat(cfg, {"model": "m"}, client3)
        except _BreakerOpen:
            blocked = True
            break
    check("熔断: 连续失败后开闸 (_BreakerOpen 快速失败)", blocked)
    st = gw3.stats()
    open_endpoints = [u for u, s in st.items() if s["breaker_open"]]
    check("熔断: endpoint 状态可见", len(open_endpoints) == 1, str(st))

    # 熔断恢复窗口后可半开重试 (时间快进: 直接改 breaker_open_until)
    s3 = list(gw3._endpoints.values())[0]
    s3.breaker_open_until = 0.0
    def handler_ok(request):
        return httpx.Response(200, json={"ok": 1})
    client4 = httpx.AsyncClient(transport=httpx.MockTransport(handler_ok))
    resp4 = await gw3.request_chat(cfg, {"model": "m"}, client4)
    check("熔断: 开闸窗口过后恢复请求", resp4.status_code == 200)

    await client.aclose(); await client2.aclose(); await client3.aclose(); await client4.aclose()


async def main():
    await test_resolver_priority()
    await test_gateway_backoff_breaker()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
