"""侦察 NMS Build Quick Filter 对话框 + 列头右键菜单 + toolbar 完整结构（修正选择器）。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

# ① toolbar 根完整结构（id 以 _toolbar 结尾）
PROBE_TB = """() => {
  const dijit = window.dijit;
  const out = [];
  const tb = document.querySelector("[id$='_toolbar']");
  if (!tb) return {ok:false, err:'no toolbar root'};
  const walk = (el, depth) => {
    if (depth > 3) return;
    Array.from(el.children || []).forEach(c => {
      const w = dijit.getEnclosingWidget(c);
      out.push({
        id: c.id || null, tag: c.tagName,
        widget: w ? w.declaredClass : null,
        text: (c.textContent || '').trim().slice(0, 40),
      });
      walk(c, depth + 1);
    });
  };
  walk(tb, 0);
  return {ok:true, items: out};
}"""

# ② 打开 Build Quick Filter 对话框并枚举内容
PROBE_BUILD = """async () => {
  const dijit = window.dijit;
  const btn = document.querySelector("[id*='quickfilter']");
  if (!btn) return {ok:false, err:'no quickfilter button'};
  btn.click();
  await new Promise(r => setTimeout(r, 1200));
  const build = document.getElementById(btn.id + '_buildMenu') || document.querySelector("[id*='quickfilter_buildMenu']");
  if (build) { build.click(); }
  await new Promise(r => setTimeout(r, 2000));
  // 枚举所有 dialog
  const dialogs = [];
  document.querySelectorAll("[class*='dialog'], [id*='Dialog'], [id*='dialog']").forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length < 500) dialogs.push({id: el.id, cls: (el.className || '').toString().slice(0, 60), text: t.slice(0, 300)});
  });
  // 枚举 dialog 内控件
  const controls = [];
  document.querySelectorAll("[class*='dijitDialog'] input, [class*='dijitDialog'] select, [class*='dijitDialog'] [class*='Select']").forEach(el => {
    controls.push({id: el.id, tag: el.tagName, value: (el.value || '').slice(0, 40), cls: (el.className || '').toString().slice(0, 50)});
  });
  return {ok:true, dialogs: dialogs.slice(-8), controls: controls.slice(-30)};
}"""

# ③ Acknowledged 列头右键菜单
PROBE_CTX = """async () => {
  const td = document.querySelector("[id*='mainTable-Acknowledged']");
  if (!td) return {ok:false, err:'no ack column header'};
  td.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
  await new Promise(r => setTimeout(r, 1500));
  const menus = [];
  document.querySelectorAll("[id*='Menu']").forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length < 200 && el.offsetParent !== null) menus.push({id: el.id, text: t.slice(0, 150)});
  });
  return {ok:true, menus: menus.slice(-12)};
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
        await agent._nms_select_filter(frame, "GCOC_use")
        await asyncio.sleep(3)
        r1 = await frame.evaluate(PROBE_TB)
        print("═" * 70)
        print("toolbar 完整结构:")
        for c in (r1.get("items") or []):
            print("  ", json.dumps(c, ensure_ascii=False))
        print("═" * 70)
        r3 = await frame.evaluate(PROBE_CTX)
        print("列头右键菜单:", json.dumps(r3, ensure_ascii=False))
        await page.screenshot(path="/opt/zops/screenshots/probe_ack_ctx.png")
        print("═" * 70)
        r2 = await frame.evaluate(PROBE_BUILD)
        print("Build Quick Filter 对话框:", json.dumps(r2, ensure_ascii=False)[:2000])
        await page.screenshot(path="/opt/zops/screenshots/probe_ack_build.png")
        await browser.close()


asyncio.run(main())
