"""侦察 Build Quick Filter 对话框：Field 下拉选项 / Operator 选项 / Apply 按钮 / 表格 ACK 列值。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

# ① 打开 Build Quick Filter 对话框，枚举 Field/Operator 下拉选项 + 对话框按钮
PROBE = """async () => {
  const dijit = window.dijit;
  const out = {};
  const qf = document.querySelector("[id*='toolbar_quickfilter']");
  if (!qf) return {ok:false, err:'no quickfilter'};
  // 展开 quickfilter 菜单
  const combo = document.querySelector("[id*='quickfilter_button']");
  if (combo) { combo.click(); await new Promise(r => setTimeout(r, 1200)); }
  const build = document.querySelector("[id*='quickfilter_buildMenu']");
  if (build) { build.click(); await new Promise(r => setTimeout(r, 2000)); }
  else { qf.click(); await new Promise(r => setTimeout(r, 1500)); }
  // 对话框根 id
  const dlg = document.querySelector("[id*='filterDlg_dialog']");
  if (!dlg) return {ok:false, err:'no filterDlg'};
  out.dlgId = dlg.id;
  // 对话框内所有按钮
  out.buttons = [];
  document.querySelectorAll("[id*='filterDlg'] [id*='utton'], [id*='filterDlg'] [id*='_ok'], [id*='filterDlg'] [id*='_cancel'], [id*='filterDlg'] [id*='_apply']").forEach(el => {
    const w = dijit.getEnclosingWidget(el);
    out.buttons.push({id: el.id, widget: w ? w.declaredClass : null, text: (el.textContent || '').trim().slice(0, 20)});
  });
  // Field 下拉 widget
  const fldW = document.getElementById('widget_' + dlg.id.replace('_dialog','') + '_field')
    || document.querySelector("[id*='filterDlg_field']");
  if (fldW) {
    const w = dijit.getEnclosingWidget(fldW) || dijit.byId(fldW.id.replace('widget_',''));
    if (w && w.store && w.store.query) {
      const r = w.store.query({});
      let items;
      if (r && typeof r.then === 'function') items = await r;
      else if (r && r.forEach && !Array.isArray(r)) { items=[]; r.forEach(x=>items.push(x)); }
      else items = r;
      out.fieldOptions = (items || []).map(it => String(it.label || it.name || '')).filter(Boolean);
    }
  }
  // Operator 下拉 widget
  const opW = document.querySelector("[id*='filterDlg_operator']");
  if (opW) {
    const w = dijit.getEnclosingWidget(opW) || dijit.byId(opW.id.replace('widget_',''));
    if (w && w.store && w.store.query) {
      const r = w.store.query({});
      let items;
      if (r && typeof r.then === 'function') items = await r;
      else if (r && r.forEach && !Array.isArray(r)) { items=[]; r.forEach(x=>items.push(x)); }
      else items = r;
      out.operatorOptions = (items || []).map(it => String(it.label || it.name || '')).filter(Boolean);
    }
  }
  // 表格 ACK 列前几行值
  out.ackValues = [];
  const hdrs = document.querySelectorAll("table[id*='mainTable'] th, table[id*='mainTable'] td[class*='header']");
  let ackIdx = -1;
  hdrs.forEach((h, i) => { if ((h.id || '').indexOf('Acknowledged') >= 0) ackIdx = i; });
  if (ackIdx >= 0) {
    document.querySelectorAll("table[id*='mainTable'] tbody tr").forEach((tr, ri) => {
      if (ri > 6) return;
      const tds = tr.querySelectorAll('td');
      if (tds[ackIdx]) out.ackValues.push((tds[ackIdx].textContent || '').trim().slice(0, 20));
    });
  } else {
    out.ackIdxHint = 'not found in headers; hdrs=' + hdrs.length;
  }
  // 关闭对话框（点 Cancel）
  const cancel = document.querySelector("[id*='filterDlg'] [id*='ancel']");
  if (cancel) cancel.click();
  return {ok:true, ...out};
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
        res = await frame.evaluate(PROBE)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        await browser.close()


asyncio.run(main())
