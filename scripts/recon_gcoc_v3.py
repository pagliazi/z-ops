"""侦察 v3：GCOC_use 重名/类型/多次抓取漂移 + identifierList 与 BGPStatus 交集。"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await agent._nms_login(page)

        filters = await agent._nms_fetch_filters_meta(page)
        print(f"共 {len(filters)} 个 filter")
        gcocs = [f for f in filters if f["name"] == "GCOC_use"]
        print(f"\n=== GCOC_use 定义条目数: {len(gcocs)} ===")
        for i, f in enumerate(gcocs):
            print(f"  [{i}] type={f['type']!r} view={f['view']!r} "
                  f"sql_len={len(f['sql'])}")
            print(f"      sql 开头: {f['sql'][:120]!r}")

        # 所有名字含 GCOC/BGP 的 filter
        print("\n=== 名称含 GCOC/BGP 的 filter ===")
        for f in filters:
            if "GCOC" in f["name"] or "BGP" in f["name"]:
                print(f"  {f['name']!r} type={f['type']!r} view={f['view']!r} "
                      f"sql_len={len(f['sql'])}")

        # BGPStatus 匹配集
        fmap = {f["name"]: f for f in filters}
        bgp_serials, bfb = await agent._nms_api_serials(page, fmap.get("BGPStatus"))
        bgp_set = set(bgp_serials)
        print(f"\nBGPStatus 匹配: {len(bgp_set)} (fallback={bfb})")

        # 主集（用于把 serial 映射到 summary）
        master = fmap.get("AllEvents")
        mf = dict(master)
        mf["view"] = "Default"
        fields, rows, serials = await agent._nms_api_get_full(page, mf)
        merged = {}
        n = min(len(rows), len(serials))
        for i in range(n):
            merged[serials[i]] = agent._nms_row_to_alarm(fields, rows[i], serials[i])
        print(f"主集 AllEvents: {len(merged)} 条")

        # GCOC_use 连续抓 3 次，看漂移
        for rnd in range(3):
            gcoc_serials, fell_back = await agent._nms_api_serials(
                page, fmap.get("GCOC_use"))
            gcoc_set = set(gcoc_serials)
            inter = gcoc_set & bgp_set
            print(f"\n[第{rnd+1}次] GCOC_use 匹配 {len(gcoc_set)} "
                  f"(fallback={fell_back}), 与 BGP 交集 {len(inter)}")
            if inter:
                for srl in list(inter)[:6]:
                    a = merged.get(srl, {})
                    print(f"    srl={srl} EventGroup={a.get('event_group')!r} "
                          f"Summary={str(a.get('summary'))[:60]!r}")

        # 原始响应结构：GCOC_use 用 global 类型请求，看 datasource 名
        root = config.NMS_CONSOLE_URL.split("/ibm/console")[0].rstrip("/")
        f = fmap.get("GCOC_use")
        url = agent._nms_api_url(f["name"], "global", f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        body = await resp.text()
        print(f"\nGCOC_use global 请求 status={resp.status}, body_len={len(body)}")
        m = re.search(r'"identifierList":"([^"]*)"', body)
        if m:
            ids = [x for x in m.group(1).split(",") if x.strip()]
            print(f"  首个 identifierList 长度: {len(ids)}")
        for ds in re.findall(r'"datasourceName":"([^"]+)"', body)[:5]:
            print(f"  datasourceName: {ds}")
        # datasource 块
        dss = re.findall(r'"datasource":\s*\[', body)
        print(f"  'datasource':[ 出现次数: {len(dss)}")
        idl = re.findall(r'"identifierList":"([^"]*)"', body)
        print(f"  identifierList 出现次数: {len(idl)}, 各长度: "
              f"{[len(x.split(',')) if x else 0 for x in idl]}")

        await browser.close()


asyncio.run(main())
