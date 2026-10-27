#!/usr/bin/env python3
"""实验E: 紧凑格式 prompt (修复后生产格式) 的生成耗时与质量 — 对比基线A(JSON+截断)"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import warnings; warnings.filterwarnings("ignore")
import logging; logging.basicConfig(level=logging.WARNING)
import httpx
from agents.gcoc_api import query_vms_on_esxi_host, _get_mcp_gateway_client
from agents.gcoc_agent import GCOC_ANALYSIS_PROMPT

BASE = "https://aigw-noc.example.com/v1"
KEY = "8j4JI2X7W2yrG1vU0fD6A64c87404a39B3F5F154Bd296d4e"
KEYS = ['name', 'site', 'resource_pool', 'power_state', 'cpu_count', 'memory_mb',
        'guest_os', 'guest_ip', 'customer_gid', 'biz_type', 'cluster', 'vcenter_server']


def fmt_compact(vms):
    lines = []
    for i, v in enumerate(sorted(vms, key=lambda x: x.get('name', '')), 1):
        parts = [f"#{i}"]
        for k in KEYS:
            val = v.get(k)
            if val is not None and str(val).strip() and str(val) != 'None' and val != []:
                parts.append(f"{k}={str(val)[:50]}")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


async def call(client, prompt, label):
    body = {"model": "deepseek-v3-0324", "temperature": 0.7, "stream": False,
            "messages": [{"role": "system", "content": GCOC_ANALYSIS_PROMPT},
                         {"role": "user", "content": prompt}]}
    t0 = time.time()
    r = await client.post(f"{BASE}/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {KEY}"},
                          timeout=httpx.Timeout(300, connect=15))
    dt = time.time() - t0
    d = r.json()
    text = d["choices"][0]["message"]["content"]
    u = d.get("usage", {})
    return {"label": label, "dt": round(dt,1), "chars": len(text),
            "prompt_tok": u.get("prompt_tokens"), "comp_tok": u.get("completion_tokens"),
            "finish": d["choices"][0].get("finish_reason"), "text": text}


async def main():
    info = await query_vms_on_esxi_host("ESXI-02")
    vms, n = info["vms"], None
    n = len(vms)
    host_disp = ", ".join(info["esxi_hosts"][:2])
    head = (f"用户问题: 查询 ESXI-02 主机上的虚拟机列表\n\n"
            f"以下是主机 ESXI-02 ({host_disp}) 上的全部 {n} 台虚拟机 "
            f"(smartops_vms host=服务端过滤 + snapshot_vms 合并, 快照覆盖 {info['snapshot_matched']}/{n}):\n\n")
    compact = fmt_compact(vms)

    results = []
    async with httpx.AsyncClient(verify=False) as client:
        print(f">>> E1. 紧凑格式 ({len(head+compact)} 字符 prompt)")
        results.append(await call(client, head + compact[:12000], "E1.紧凑格式"))
        print(f"    {results[-1]['dt']}s, {results[-1]['chars']}字符, comp={results[-1]['comp_tok']}, finish={results[-1]['finish']}\n")
        print(">>> E2. 紧凑格式复测 (波动对照)")
        results.append(await call(client, head + compact[:12000], "E2.紧凑复测"))
        print(f"    {results[-1]['dt']}s, {results[-1]['chars']}字符, comp={results[-1]['comp_tok']}, finish={results[-1]['finish']}\n")

    # 质检: 18 台 VM 名是否全部出现在输出表格
    names = [v.get("name") for v in vms]
    print("="*70)
    print(f"质量检查 (全 {n} 台 VM 是否都在回复中):")
    for r in results:
        t = r["text"]
        missing = [nm for nm in names if nm not in t]
        print(f"  {r['label']}: {r['dt']}s | 缺失VM={len(missing)}台{(' ' + str(missing[:5])) if missing else ' ✓全部列出'} | 台数正确={str(n) in t} | prompt_tok={r['prompt_tok']}")
    Path(__file__).parent.joinpath("llm_compact_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1))
    print("\n完整回复(E1)已存 scripts/llm_compact_results.json")
    mc = await _get_mcp_gateway_client(); await mc.aclose()

asyncio.run(main())
