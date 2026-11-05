"""侦察 v4：主集 rows/serials 对齐性 + OPSCORE_use 完整 rows 内容是否含 BGP。"""
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
        fmap = {f["name"]: f for f in filters}

        # 主集 AllEvents（Default 扁平视图）
        master = fmap.get("AllEvents")
        mf = dict(master)
        mf["view"] = "Default"
        fields, rows, serials = await agent._nms_api_get_full(page, mf)
        print(f"主集: rows={len(rows)}, serials={len(serials)}, fields={len(fields)}")
        print(f"字段名: {fields[:20]}")

        # 行内是否含 Serial 列可做对齐校验
        if "Serial" in fields:
            si = fields.index("Serial")
            mism = sum(1 for i in range(min(len(rows), len(serials)))
                       if str(rows[i][si]).strip() != str(serials[i]).strip())
            print(f"行内 Serial 列 vs identifierList 错位行数: {mism} / {min(len(rows), len(serials))}")

        # OPSCORE_use 完整响应（GroupByDevice 视图，与生产一致）
        f = fmap.get("OPSCORE_use")
        url = agent._nms_api_url(f["name"], "global", f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        print(f"\nOPSCORE_use status={resp.status}")
        obj = json.loads(await resp.text())
        ds0 = obj["header"]["datasource"][0]
        gfields = [x.get("name") for x in ds0.get("field", [])]
        grows = obj["rows"][0] if obj.get("rows") else []
        gserials = [x for x in str(ds0.get("identifierList") or "").split(",")
                    if x.strip()]
        print(f"OPSCORE_use: rows={len(grows)}, serials={len(gserials)}")
        print(f"OPSCORE_use 字段名: {gfields[:20]}")

        # OPSCORE_use 的 rows 内容（EventGroup/Summary 抽样）——是否含 BGP
        eg_idx = None
        sum_idx = None
        for i, nm in enumerate(gfields):
            if nm in ("EventGroup", "AlertGroup") and eg_idx is None:
                eg_idx = i
            if nm == "Summary" and sum_idx is None:
                sum_idx = i
        print("\n=== OPSCORE_use rows 抽样（前 15 行）===")
        for i, row in enumerate(grows[:15]):
            eg = row[eg_idx] if eg_idx is not None and eg_idx < len(row) else "?"
            sm = row[sum_idx] if sum_idx is not None and sum_idx < len(row) else "?"
            print(f"  [{i}] serial={gserials[i] if i < len(gserials) else '?'} "
                  f"eg={str(eg)[:40]!r} summary={str(sm)[:45]!r}")

        # OPSCORE_use rows 里含 BGP 的行数
        bgp_rows = 0
        for row in grows:
            blob = "|".join(str(x) for x in row)
            if "BGP" in blob:
                bgp_rows += 1
                if bgp_rows <= 5:
                    eg = row[eg_idx] if eg_idx is not None and eg_idx < len(row) else "?"
                    sm = row[sum_idx] if sum_idx is not None and sum_idx < len(row) else "?"
                    print(f"  BGP行: eg={str(eg)[:40]!r} summary={str(sm)[:50]!r}")
        print(f"OPSCORE_use rows 含 BGP 行数: {bgp_rows} / {len(grows)}")

        # 生产 cache 里那 4 条 BGP 串行是否在 OPSCORE_use 当前列表
        targets = {"1295398255", "1295022955", "1295511030", "1295022956"}
        gset = set(gserials)
        print(f"\n4 条污染串行当前在 OPSCORE_use 列表: {sorted(targets & gset)}")
        # 这些串行在 BGPStatus 列表吗
        bserials, bfb = await agent._nms_api_serials(page, fmap.get("BGPStatus"))
        bset = set(bserials)
        print(f"4 条污染串行在 BGPStatus 列表: {sorted(targets & bset)}")
        print(f"BGPStatus 大小: {len(bset)}")

        await browser.close()


asyncio.run(main())
