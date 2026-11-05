"""P0 验收补充: /api/llm/config 密钥掩码与 PATCH 保留语义

隔离: 测试 Redis 6399 (env 覆盖), 铸造临时 token (config.key_token)。
覆盖 SPEC P0「通道、服务身份与凭据」:
  - 读取收归管理员;
  - 密钥仅返回掩码 (__KEEP__) 与 has_* 标志, 不回显原文;
  - POST 未提交密钥 (空/掩码) 保留原值; 清除必须显式 clear_keys。
"""
import json
import os
import sys

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio  # noqa: E402

import config  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


async def main():
    import webchat_api
    from fastapi.testclient import TestClient

    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await r.flushdb()
    # 铸造测试 token: admin 与普通用户
    await r.set(config.key_token("tok-admin"), "p0admin")
    await r.set(config.key_token("tok-user"), "p0user")
    await r.hset(config.KEY_USERS, "p0admin", json.dumps({"role": "admin"}))
    await r.hset(config.KEY_USERS, "p0user", json.dumps({"role": "user"}))
    # 存量密钥
    await r.set(config.KEY_LLM_PREF, json.dumps({
        "provider": "deepseek", "model": "m1",
        "api_key": "sk-SECRET-ORIGINAL", "oneapi_key": "",
    }))

    client = TestClient(webchat_api.app)
    with client:  # 单一 portal loop: 缓存的 async redis 连接跨请求复用
        # 1. 普通用户读取 → 403 (此前任意登录用户可读原始密钥)
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-user"})
        check("llm/config: 非管理员读取 403", resp.status_code == 403, str(resp.status_code))

        # 2. 管理员读取 → 掩码, 不泄漏原文
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-admin"})
        body = resp.json()
        check("llm/config: 管理员可读", resp.status_code == 200, str(resp.status_code))
        check("llm/config: api_key 不回显原文/哨兵 (空串, 状态由 has_* 表达)", body.get("api_key") == "", str(body.get("api_key")))
        check("llm/config: has_api_key 标志", body.get("has_api_key") is True)
        check("llm/config: 响应不含密钥原文", "sk-SECRET-ORIGINAL" not in resp.text)
        check("llm/config: 未配置密钥返回空串", body.get("oneapi_key") == "" and body.get("has_oneapi_key") is False)

        # 3. POST 掩码/空 → 保留原值
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m2", "api_key": "__KEEP__"})
        check("llm/config: 提交掩码保存成功", resp.status_code == 200)
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("llm/config: 掩码提交后密钥保留", pref.get("api_key") == "sk-SECRET-ORIGINAL", str(pref))
        check("llm/config: 非密钥字段已更新", pref.get("model") == "m2", str(pref.get("model")))

        # 4. POST 空密钥 → 同样保留
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m3", "api_key": ""})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("llm/config: 空密钥提交后保留", resp.status_code == 200 and pref.get("api_key") == "sk-SECRET-ORIGINAL")

        # 5. 新密钥 → 覆盖
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m3", "api_key": "sk-NEW"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("llm/config: 新密钥覆盖", pref.get("api_key") == "sk-NEW")

        # 6. 显式清除
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m3", "clear_keys": ["api_key"]})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("llm/config: clear_keys 显式清除", resp.status_code == 200 and "api_key" not in pref, str(pref))

        # 7. 普通用户 POST → 403
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-user"},
                           json={"provider": "x"})
        check("llm/config: 非管理员写入 403", resp.status_code == 403)

        # ── 2026-10-18: 切换 provider 不丢 URL/Key (provider_creds 记忆槽) ──
        await r.set(config.KEY_LLM_PREF, json.dumps({
            "provider": "deepseek", "model": "m1",
            "api_key": "sk-DS", "base_url": "https://api.deepseek.com/v1",
        }))
        await r.set(config.KEY_CUSTOM_PROVIDERS, json.dumps([
            {"id": "mygw", "name": "MyGW", "base_url": "https://gw.example.com/v1", "models": ["gwx"]},
        ]))

        # 8. 配置 A(deepseek) → 切到 B(mygw) 只提交 provider+model: 不得继承 A 的 key/URL
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m1", "api_key": "sk-DS",
                                 "base_url": "https://api.deepseek.com/v1"})
        check("切换前置: A 保存成功", resp.status_code == 200)
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("切到 B: 不继承 A 的 api_key", pref.get("api_key") == "", str(pref.get("api_key")))
        check("切到 B: base_url 用 provider 默认", pref.get("base_url") == "https://gw.example.com/v1",
              str(pref.get("base_url")))
        check("切到 B: A 的凭据进记忆槽", (pref.get("provider_creds", {}).get("deepseek", {}).get("api_key")) == "sk-DS",
              str(pref.get("provider_creds")))

        # 9. 切回 A: 不重新提交密钥 → 从记忆槽恢复
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m1"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("切回 A: api_key 从记忆恢复", pref.get("api_key") == "sk-DS", str(pref.get("api_key")))
        check("切回 A: base_url 从记忆恢复", pref.get("base_url") == "https://api.deepseek.com/v1",
              str(pref.get("base_url")))

        # 10. URL 规范化: 全端点 URL 剥 /chat/completions; 坏 URL 400
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx",
                                 "base_url": "https://gw2.example.com/v1/chat/completions"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("URL 规范化: 剥 /chat/completions", pref.get("base_url") == "https://gw2.example.com/v1",
              str(pref.get("base_url")))
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx", "base_url": "svc-opscore"})
        check("URL 规范化: 无 scheme 拒绝 400", resp.status_code == 400, str(resp.status_code))

        # 11. GET: provider_creds 摘要回显 (URL 明文/密钥只回 has_api_key)
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-admin"})
        body = resp.json()
        pc = body.get("provider_creds", {})
        check("GET: provider_creds 摘要含记忆槽", pc.get("mygw", {}).get("base_url") == "https://gw2.example.com/v1",
              str(pc))
        check("GET: 摘要不含密钥原文", "sk-DS" not in json.dumps(pc), str(pc))
        check("GET: 摘要 has_api_key 标志", pc.get("deepseek", {}).get("has_api_key") is True, str(pc))
        check("GET: 保存回显不泄漏记忆槽密钥", "sk-DS" not in resp.text or "provider_creds" not in resp.text, "")

        # 12. clear_keys 同时清记忆槽
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "deepseek", "model": "m1", "clear_keys": ["api_key"]})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("clear_keys: top-level 与记忆槽同清",
              "api_key" not in pref and "api_key" not in pref.get("provider_creds", {}).get("deepseek", {}),
              str(pref.get("provider_creds")))

        # ── 2026-10-18: 同一 provider 接入更多模型 (模型记忆 + 编辑) ──
        # 13. 保存即记住用过的模型 → 记忆槽 models + GET 合并进 provider 标签
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx-new"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        slot = pref.get("provider_creds", {}).get("mygw", {})
        check("保存记住模型: 记忆槽 models 含新模型", "gwx-new" in (slot.get("models") or []), str(slot))
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-admin"})
        mygw = next((p for p in resp.json().get("providers", []) if p["id"] == "mygw"), {})
        check("GET: 记忆模型并入 provider 标签列表", "gwx-new" in (mygw.get("models") or []), str(mygw))

        # 14. 模型列表端点: 预设+记忆合并, 无凭据时不发起网关请求 (source=preset)
        resp = client.get("/api/llm/models/mygw", headers={"Authorization": "Bearer tok-admin"})
        body = resp.json()
        check("models 端点: 预设+记忆合并", set(["gwx", "gwx-new"]) <= set(body.get("models") or []),
              str(body))
        check("models 端点: 无凭据 source=preset", body.get("source") == "preset", str(body.get("source")))

        # 15. PUT 编辑自定义 provider 模型列表
        resp = client.put("/api/llm/providers/mygw", headers={"Authorization": "Bearer tok-admin"},
                          json={"models": ["gwx", "gwx2", "gwx3"]})
        check("PUT provider: 编辑成功", resp.status_code == 200, str(resp.status_code))
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-admin"})
        mygw = next((p for p in resp.json().get("providers", []) if p["id"] == "mygw"), {})
        check("PUT provider: 模型列表已更新", set(["gwx", "gwx2", "gwx3"]) <= set(mygw.get("models") or []),
              str(mygw.get("models")))
        resp = client.put("/api/llm/providers/deepseek", headers={"Authorization": "Bearer tok-admin"},
                          json={"models": ["x"]})
        check("PUT provider: 内置 provider 拒绝 400", resp.status_code == 400, str(resp.status_code))

        # ── 2026-10-18 晚: Key 不回显 + llm_test 槽感知解析 + 验证模型名 ──
        # 16. llm_test: 被测 provider 无存量 key → 明确 400 (不误用其他 provider 的 key)
        resp = client.post("/api/llm/test", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx", "base_url": "https://gw.example.com/v1"})
        body = resp.json()
        check("llm/test: 无 key 400 且提示用已保存 Key", resp.status_code == 400 and "API Key" in body.get("error", ""),
              f"{resp.status_code} {body}")
        resp = client.post("/api/llm/test", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "custom", "model": "x"})
        check("llm/test: 无 Base URL 400", resp.status_code == 400, str(resp.status_code))

        # 17. 验证模型名映射: hash 优先于静态别名表, GET 暴露 model_names
        await r.hset(config.KEY_MODEL_NAMES, "gwx-new", "gwx-real-99")
        resp = client.get("/api/llm/config", headers={"Authorization": "Bearer tok-admin"})
        body = resp.json()
        check("模型名: GET 暴露 model_names 映射", body.get("model_names", {}).get("gwx-new") == "gwx-real-99",
              str(body.get("model_names")))
        check("模型名: real_model 用验证映射 (该是什么模型就是什么模型)",
              body.get("real_model") == "gwx-real-99", str(body.get("real_model")))

        # 18. llm_test 哨兵兼容: __KEEP__ 不再出现在 GET, 但 POST/llm_test 仍识别 (旧客户端)
        resp = client.post("/api/llm/config", headers={"Authorization": "Bearer tok-admin"},
                           json={"provider": "mygw", "model": "gwx-new", "api_key": "__KEEP__"})
        pref = json.loads(await r.get(config.KEY_LLM_PREF) or "{}")
        check("兼容: POST 仍识别 __KEEP__ 为保留", resp.status_code == 200 and not pref.get("api_key"),
              f"{resp.status_code} {pref.get('api_key')!r}")

    await r.aclose()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
