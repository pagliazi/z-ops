#!/usr/bin/env python3
"""LLM 生成段基准测试: 生产同款 prompt, 对比 模型/max_tokens 策略的耗时与质量
回答问题: "压缩prompt/限制输出/换模型" 是否影响回答效果"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import warnings; warnings.filterwarnings("ignore")
import logging; logging.basicConfig(level=logging.WARNING)

import httpx
from agents.opscore_api import query_vms_on_esxi_host, _get_mcp_gateway_client
from agents.opscore_agent import OPSCORE_ANALYSIS_PROMPT

BASE = "https://aigw-noc.example.com/v1"
KEY = "8j4JI2X7W2yrG1vU0fD6A64c87404a39B3F5F154Bd296d4e"


async def call_llm(client, model, prompt, system, max_tokens=None, label=""):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.7, "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    t0 = time.time()
    try:
        r = await client.post(f"{BASE}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {KEY}"},
                              timeout=httpx.Timeout(300, connect=15))
        dt = time.time() - t0
        d = r.json()
        if "error" in d:
            return {"label": label, "model": model, "dt": dt, "error": str(d["error"])[:120]}
        choice = d["choices"][0]
        text = choice["message"].get("content") or ""
        usage = d.get("usage", {})
        finish = choice.get("finish_reason")
        return {"label": label, "model": d.get("model", model), "dt": round(dt, 1),
                "out_chars": len(text), "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish": finish, "text": text}
    except Exception as e:
        return {"label": label, "model": model, "dt": round(time.time()-t0, 1),
                "error": f"{type(e).__name__}: {e}"}


async def main():
    # 1) 生产同款数据
    info = await query_vms_on_esxi_host("ESXI-02")
    vms = info["vms"]
    raw = json.dumps(vms, ensure_ascii=False, indent=1)
    prompt = (f"用户问题: 查询 ESXI-02 主机上的虚拟机列表\n\n"
              f"以下是主机 ESXI-02 ({', '.join(info['esxi_hosts'][:2])}) 上的全部 {len(vms)} 台虚拟机 "
              f"(smartops_vms host=服务端过滤 + snapshot_vms 合并, 快照覆盖 {info['snapshot_matched']}/{len(vms)}):\n\n{raw[:9000]}")
    print(f"数据段: {info['elapsed']}s, {len(vms)} 台 VM, prompt {len(prompt)} 字符\n")

    quality_keys = ["分析数据时", "🔴", "🟡", "🟢", "表格"]
    results = []
    async with httpx.AsyncClient(verify=False) as client:
        # A. 生产基线: deepseek-v3-0324 无限制
        print(">>> A. 基线 deepseek-v3-0324 (生产现状)")
        a = await call_llm(client, "deepseek-v3-0324", prompt, OPSCORE_ANALYSIS_PROMPT, label="A.基线")
        results.append(a); print(f"    {a['dt']}s, {a.get('out_chars')} 字符, completion={a.get('completion_tokens')}, finish={a.get('finish')}\n")

        # B. 限制输出: max_tokens=1024
        print(">>> B. deepseek-v3-0324 + max_tokens=1024")
        b = await call_llm(client, "deepseek-v3-0324", prompt, OPSCORE_ANALYSIS_PROMPT, max_tokens=1024, label="B.限1024")
        results.append(b); print(f"    {b['dt']}s, {b.get('out_chars')} 字符, completion={b.get('completion_tokens')}, finish={b.get('finish')}\n")

        # C. 备用模型 openclaw:svc-opscore
        print(">>> C. openclaw:svc-opscore (备用)")
        c = await call_llm(client, "openclaw:svc-opscore", prompt, OPSCORE_ANALYSIS_PROMPT, label="C.svc-opscore")
        results.append(c)
        cmsg = c.get("error") or f"{c.get('dt')}s, {c.get('out_chars')} 字符, completion={c.get('completion_tokens')}"
        print(f"    {cmsg}\n")

        # D. 快模型? deepseek 别名再来一次 (网络波动对照)
        print(">>> D. 基线复测 deepseek-v3-0324 (波动对照)")
        d = await call_llm(client, "deepseek-v3-0324", prompt, OPSCORE_ANALYSIS_PROMPT, label="D.基线复测")
        results.append(d); print(f"    {d['dt']}s, {d.get('out_chars')} 字符, completion={d.get('completion_tokens')}, finish={d.get('finish')}\n")

    # 质检: VM 台数是否说对 + 表格行数
    print("="*70)
    print("质量检查 (每项: 输出中是否含正确台数/表格结构):")
    for r in results:
        if "error" in r:
            print(f"  {r['label']}: ERROR {r['error']}")
            continue
        t = r["text"]
        n_correct = str(len(vms)) in t
        has_table = ("|" in t and "---" in t)
        rows = t.count("\n|") if has_table else 0
        truncated = r.get("finish") == "length"
        print(f"  {r['label']}: {r['dt']}s | 台数正确={n_correct} | 表格={has_table}(~{rows}行) | 截断={truncated} | {r['out_chars']}字符")

    out = Path(__file__).parent / "llm_bench_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n详细输出已存: {out}")
    mc = await _get_mcp_gateway_client(); await mc.aclose()


if __name__ == "__main__":
    asyncio.run(main())
