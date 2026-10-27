"""端到端验证：Build Quick Filter 构建 Acknowledged=0（未确认）快速过滤。

流程：切 GCOC_use → 记录表格指纹 → 展开 quickfilter 菜单 → Build Quick Filter
→ Field 选 Acknowledged → Operator 选 Equals → Term=0 → Apply → 对比指纹 → 截图。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

# 枚举弹出菜单项（dijit Menu 弹出后）
MENU_ITEMS_JS = """() => {
  const items = [];
  document.querySelectorAll("[id*='_popup'], [id*='Popup'], [id*='dijit_MenuItem']").forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length < 80) items.push({id: el.id, cls: (el.className || '').toString().slice(0, 40), text: t});
  });
  return items;
}"""


# 用 JS 直接点击弹出菜单中匹配文本的菜单项（避开 underlay/pointer 拦截）
CLICK_MENU_JS = """(label) => {
  const items = document.querySelectorAll("[id*='dijit_MenuItem'], .dijitMenuItem");
  for (const it of items) {
    const t = (it.textContent || '').trim();
    if (t === label) { it.click(); return {ok:true, clicked: t}; }
  }
  return {ok:false, err: 'not found: ' + label};
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
        before = await agent._nms_table_snapshot(frame)
        print(f"[1] 过滤前表格指纹长度: {len(before)}")

        # ── 展开 quickfilter 菜单并点 Build Quick Filter ──
        combo = frame.locator("css=[id*='quickfilter_button_arrow']")
        if await combo.count() == 0:
            combo = frame.locator("css=[id*='quickfilter_button']").last
        await combo.click()
        await asyncio.sleep(1.2)
        build = frame.get_by_role("menuitem", name="Build Quick Filter")
        if await build.count() == 0:
            build = frame.locator("css=[id*='quickfilter_buildMenu']").first
        await build.click()
        await asyncio.sleep(2)
        dlg = frame.locator("css=[id*='filterDlg_dialog']")
        print(f"[2] 对话框可见: {await dlg.count() > 0}")

        # ── Field 下拉：点击展开并枚举选项 ──
        field = frame.locator("css=[id*='filterDlg_field']").first
        await field.click()
        await asyncio.sleep(1.5)
        menus = await frame.evaluate(MENU_ITEMS_JS)
        print("[3] Field 下拉菜单项:")
        for m in menus:
            print("   ", json.dumps(m, ensure_ascii=False))
        # 找 Acknowledged 选项并点击（JS 直接点击避免 underlay 拦截）
        r_ack = await frame.evaluate(CLICK_MENU_JS, "Acknowledged")
        await asyncio.sleep(1)
        print("[3b] 选择 Acknowledged:", json.dumps(r_ack, ensure_ascii=False))
        if not r_ack.get("ok"):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

        # ── Operator 下拉 ──
        op = frame.locator("css=[id*='filterDlg_operator']").first
        await op.click()
        await asyncio.sleep(1.5)
        menus2 = await frame.evaluate(MENU_ITEMS_JS)
        print("[4] Operator 菜单项:")
        for m in menus2:
            print("   ", json.dumps(m, ensure_ascii=False))
        r_eq = await frame.evaluate(CLICK_MENU_JS, "Equals")
        await asyncio.sleep(1)
        print("[4b] 选择 Equals:", json.dumps(r_eq, ensure_ascii=False))
        if not r_eq.get("ok"):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

        # ── Term 输入 0 ──
        term = frame.locator("css=input[id*='filterDlg_term']").first
        if await term.count() > 0:
            await term.fill("0")
            await term.press("Enter")
            await asyncio.sleep(0.8)
            print("[5] Term 已输入 0")

        # ── Apply ──
        apply_btn = frame.locator("css=[id*='filterDlg_apply']").first
        await apply_btn.click()
        await asyncio.sleep(3)
        print("[6] 已点击 Apply")

        # ── 对比指纹 ──
        after = await agent._nms_table_snapshot(frame)
        print(f"[7] 过滤后表格指纹长度: {len(after)} (变化: {before != after})")
        await page.screenshot(path="/opt/zops/screenshots/probe_ack_filtered.png")
        print("[8] 已保存: screenshots/probe_ack_filtered.png")

        # ── 清理：Remove All Quick Filters ──
        try:
            combo2 = frame.locator("css=[id*='quickfilter_button_arrow']")
            if await combo2.count() == 0:
                combo2 = frame.locator("css=[id*='quickfilter_button']").last
            await combo2.click()
            await asyncio.sleep(1.2)
            remove = frame.get_by_role("menuitem", name="Remove All Quick Filters")
            if await remove.count() == 0:
                remove = frame.locator("css=[id*='quickfilter_removeMenu']").first
            if await remove.count() == 0:
                remove = frame.locator("text=Remove All Quick Filters").first
            await remove.click()
            await asyncio.sleep(2)
            print("[9] 已 Remove All Quick Filters")
        except Exception as e:
            print(f"[9] ⚠️ 清理失败: {e}")
        await browser.close()


asyncio.run(main())
