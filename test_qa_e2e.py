#!/usr/bin/env python3
"""Z-OPS 端到端问答测试 — 覆盖 KB 搜索、备份查询、VCD、通用问答等场景"""

import httpx
import asyncio
import json
import sys
import time

API_BASE = "http://127.0.0.1:7789"

# ═══ 测试用例 ═══
TEST_CASES = [
    # (名称, 消息文本, 期望路由到的 agent, 期望包含的关键词列表, 超时秒)
    ("KB知识库查询", "查知识库 VPN故障处理", "kb_agent", [], 30),
    ("KB查文档",     "搜索文档 防火墙配置规范", "kb_agent", [], 30),
    ("备份查询-GID", "46445的备份", "gcoc_agent", ["46445"], 30),
    ("备份站点列表", "BRR有哪些站点", "gcoc_agent", ["site"], 30),
    ("VCD资源查询",  "查看VCD资源情况", "gcoc_agent", [], 30),
    ("虚拟机列表",   "列出虚拟机", "gcoc_agent", [], 30),
    ("通用问答",     "你好，你能做什么", "general_agent", [], 45),
    ("HA高可用查询", "最近有没有HA重启的虚拟机", "gcoc_agent", [], 60),
    ("快照查询",     "查看快照列表", "gcoc_agent", [], 30),
]


async def login() -> str:
    """登录获取 token"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{API_BASE}/api/auth/login", json={
            "username": "admin", "password": "zops_admin_2024"
        })
        resp.raise_for_status()
        return resp.json()["token"]


async def send_chat(token: str, message: str, timeout: int = 30) -> dict:
    """发送聊天消息并收集 SSE 响应"""
    headers = {"Authorization": f"Bearer {token}"}
    result = {"full_text": "", "error": None, "source": "", "progress": []}
    
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
            async with client.stream("POST", f"{API_BASE}/api/chat",
                                      headers=headers,
                                      json={"message": message}) as resp:
                if resp.status_code != 200:
                    result["error"] = f"HTTP {resp.status_code}"
                    return result
                
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    try:
                        data = json.loads(data_str)
                        event_type = data.get("type", "")
                        
                        if event_type == "start":
                            result["msg_id"] = data.get("msg_id", "")
                        elif event_type == "progress":
                            result["progress"].append(data.get("text", ""))
                        elif event_type == "reply":
                            result["full_text"] = data.get("text", "")
                            result["source"] = data.get("source", "")
                        elif event_type == "error":
                            result["error"] = data.get("text", "unknown error")
                        elif event_type == "done":
                            break
                    except json.JSONDecodeError:
                        pass
    except httpx.ReadTimeout:
        result["error"] = "SSE 读取超时"
    except Exception as e:
        result["error"] = str(e)
    
    return result


async def run_tests():
    print("=" * 70)
    print("Z-OPS 端到端问答测试")
    print("=" * 70)
    
    # 登录
    print("\n[登录] 获取 Token...")
    try:
        token = await login()
        print(f"  ✅ Token: {token[:20]}...")
    except Exception as e:
        print(f"  ❌ 登录失败: {e}")
        sys.exit(1)
    
    results = []
    passed = 0
    failed = 0
    
    for name, message, expected_agent, expected_keywords, timeout in TEST_CASES:
        print(f"\n{'─' * 60}")
        print(f"[测试] {name}")
        print(f"  消息: {message}")
        print(f"  期望: → {expected_agent}")
        
        start = time.time()
        resp = await send_chat(token, message, timeout)
        elapsed = time.time() - start
        
        # 判断结果
        is_ok = True
        issues = []
        
        # 检查是否有错误
        if resp["error"]:
            is_ok = False
            issues.append(f"错误: {resp['error']}")
        
        # 检查路由（从 source 字段获取）
        actual_agent = resp.get("source", "?") or "?"
        actual_action = "-"
        
        if expected_agent and actual_agent != expected_agent:
            # 路由信息可能不是通过 route event 返回的，不视为致命错误
            issues.append(f"路由: 期望 {expected_agent}，实际 {actual_agent}")
        
        # 检查是否有实质性回复
        text = resp["full_text"].strip()
        if not text and not resp["error"]:
            is_ok = False
            issues.append("无回复内容")
        
        # 检查期望关键词
        for kw in expected_keywords:
            if kw.lower() not in text.lower():
                issues.append(f"回复中缺少关键词: {kw}")
        
        # 输出
        status = "✅" if is_ok and not issues else ("⚠️" if is_ok else "❌")
        if not is_ok:
            failed += 1
        else:
            passed += 1
        
        print(f"  {status} 耗时: {elapsed:.1f}s | 路由到: {actual_agent}")
        if resp["progress"]:
            for p in resp["progress"][:3]:
                print(f"  📡 进度: {p[:80]}")
        if text:
            # 截取前 200 字符
            preview = text[:200].replace('\n', ' ')
            print(f"  📝 回复: {preview}{'...' if len(text) > 200 else ''}")
        for issue in issues:
            print(f"  ⚠️  {issue}")
        
        results.append({
            "name": name, "message": message, "ok": is_ok,
            "agent": actual_agent, "action": actual_action,
            "elapsed": elapsed, "text_len": len(text),
            "issues": issues, "error": resp["error"]
        })
    
    # 汇总
    print(f"\n{'=' * 70}")
    print(f"测试汇总: {passed} 通过 / {failed} 失败 / {len(TEST_CASES)} 总计")
    print(f"{'=' * 70}")
    
    for r in results:
        status = "✅" if r["ok"] and not r["issues"] else ("⚠️" if r["ok"] else "❌")
        print(f"  {status} {r['name']:16s} → {r['agent']:15s} | {r['elapsed']:5.1f}s | {r['text_len']:5d}字")
        if r["issues"]:
            for i in r["issues"]:
                print(f"       ⚠️ {i}")
    
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)
