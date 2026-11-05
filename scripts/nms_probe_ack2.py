"""侦察 NMS 工具栏完整结构 + quickfilter 快速过滤器下拉项 + 列头筛选菜单。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

# ① toolbar 根的所有子控件结构；② quickfilter 下拉展开后的选项
PROBE1 = """() => {
  const dijit = window.dijit;
  const out = {children: [], qf: null};
  const tb = document.querySelector("[id*='toolbar']:not([id*='_'])");
  if (!tb) return {ok:false, err:'no toolbar'};
  // toolbar 直接子元素（递归两层）
  const walk = (el, depth) => {
    if (depth > 2) return;
    el.children && Array.from(el.children).forEach(c => {
      const w = dijit.getEnclosingWidget(c);
      out.children.push({
        id: c.id || null, tag: c.tagName,
        cls: (c.className || '').toString().slice(0, 60),
        widget: w ? w.declaredClass : null,
        text: (c.textContent || '').trim().slice(0, 30),
      });
      walk(c, depth + 1);
    });
  };
  walk(tb, 0);
  // quickfilter
  const qf = document.querySelector("[id*='quickfilter']");
  if (qf) {
    const w = dijit.getEnclosingWidget(qf);
    out.qf = {id: qf.id, widget: w ? w.declaredClass : null};
    // quickfilter 输入区
    document.querySelectorAll("[id*='quickfilter'] [id*='input'], [id*='quickfilter'] input, [id*='quickfilter'] [id*='field']").forEach(el => {
      out.qf = Object.assign(out.qf || {}, {extra: el.id + ' / ' + (el.value || el.textContent || '').slice(0, 30)});
    });
  }
  return {ok:true, ...out};
}"""

# ③ 表格列头菜单：点击 Acknowledged 列头，枚举弹出菜单项
PROBE2 = """async () => {
  const dijit = window.dijit;
  const td = document.querySelector("[id*='mainTable-Acknowledged']");
  if (!td) return {ok:false, err:'no ack column header'};
  const before = document.querySelectorAll("[id*='_menu'], [id*='Menu'], [class*='menu']").length;
  // 触发列头点击（模拟右键/点击筛选图标）
  td.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  await new Promise(r => setTimeout(r, 1500));
  const menus = [];
  document.querySelectorAll("[id*='Menu'], [id*='menu']").forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length < 300) menus.push({id: el.id, cls: (el.className || '').toString().slice(0, 50), text: t.slice(0, 200)});
  });
  return {ok:true, menuCountBefore: before, menus: menus.slice(-10)};
}"""


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(ignore_https_errors=True)
        await agent._nms_login(page, config.NMS_USERNAME, config.NMS_PASSWORD)
        frame = await agent._nms_get_content_frame(page)
        if not frame:
            print("FAIL: 未找到 content frame")
            await browser.close()
            return
        await agent._nms_select_filter(frame, "OPSCORE_use")
        await asyncio.sleep(3)
        r1 = await frame.evaluate(PROBE1)
        print("═" * 70)
        print("toolbar 子控件结构:")
        for c in r1.get("children", []):
            print("  ", json.dumps(c, ensure_ascii=False))
        print("quickfilter:", json.dumps(r1.get("qf"), ensure_ascii=False))
        print("═" * 70)
        r2 = await frame.evaluate(PROBE2)
        print("列头菜单探测:", json.dumps(r2, ensure_ascii=False))
        await page.screenshot(path="/opt/zops/screenshots/probe_ack_menu.png")
        await browser.close()


asyncio.run(main())
