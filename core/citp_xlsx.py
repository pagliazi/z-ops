"""CITP xlsx 导出 — 严格复刻真实 CITP 附件模板版式。

模板基准: 01-CITP-20240205-CEPH-SH-8066-4723516.xlsx (主板更换, 2024/2/5)
五个固定页签及版式 (与模板逐项对齐):
  Plan:        B1:B6 标签 (宋体10粗) + C1:C6 值 (宋体10), A1:C6 细边框;
               A7:C7 合并 "Change Mamagement" 黄底 (FFFF00) 居中;
               第8行表头 step/time/{动作}步骤/操作人员/Verify/Test/Fallback,
               蓝底白字 (FF00B0F0, 等线11粗), 行高 22.5;
               数据区: 序号 Arial10 左对齐, 正文 宋体10 自动换行, Fallback 列
               底纹 FFFDFDFD 居中 (等线11); time 列同值纵向合并 (执行窗合并居中,
               同模板 B10:B16); 列宽 A5.875/B36/C63.625/D24.625/E24.625/F31.125/G11.375
               (模板 D/E 同宽); 数据行行高按换行内容估算写入
  Check_list:  表头 Step/Host/IP/URL/Sub Step/Test item/Expected result/Result(保留源数据或截图）,
               Arial16 无底纹; 数据 A/B/C/F/G Arial16、D 宋体16、E 微软雅黑16;
               Step 分组对 A/B/C 列纵向合并; 列宽 17.375/27.875/21.125/11.5/98/37.5/32.75
  Ping Snashot: 空表 (实施时粘贴 ping/测试截图, 页签名保留模板原拼写)
  Host List:   表头 设备名/位置/型号/HDM IP/IP, 蓝底宋体14粗; 数据 宋体11;
               设备行后接 Subnet/gateway/netmask; 列宽 18.25/14.875/22.625/16/15.75
  Rack Layout: 空表 (现场粘贴机柜视图, 模板此页仅含图片)

模板原样保留的拼写: "Preparedy by:" / "Change Mamagement" / "Ping Snashot" / "IP/URL"。
验收结果 (citp.acceptance, 网卡验收测试) 存在时在最后附加「验收结果」页
(模板外扩展页, 风格与模板页签统一: 蓝底表头 + 宋体正文)。
全表单元格自动换行 (wrap_text), 行高按各列换行内容计算写入, 内容放不下自动撑高不裁剪;
Host List 设备行后空 4 行接网段信息 (同模板 r9-r12)。
"""

import math

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ── 模板样式常量 (取自真实附件实测值) ──
_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BLUE = PatternFill("solid", fgColor="FF00B0F0")
_YELLOW = PatternFill("solid", fgColor="FFFFFF00")
_FALLBACK_FILL = PatternFill("solid", fgColor="FFFDFDFD")

_F_LABEL = Font(name="宋体", size=10, bold=True)          # Plan 标签
_F_VALUE = Font(name="宋体", size=10)                      # Plan 值/正文
_F_STEP = Font(name="Arial", size=10)                      # Plan 序号
_F_HEAD = Font(name="等线", size=11, bold=True, color="FFFFFFFF")  # Plan 表头
_F_FALLBACK = Font(name="等线", size=11, color="FF000000")  # Plan Fallback 列
_F_CL_HEAD = Font(name="Arial", size=16)                   # Check_list 表头
_F_CL_ABCG = Font(name="Arial", size=16)                   # Check_list A/B/C/G
_F_CL_D = Font(name="宋体", size=16)                       # Check_list Sub Step
_F_CL_E = Font(name="微软雅黑", size=16)                    # Check_list Test item
_F_HL_HEAD = Font(name="宋体", size=14, bold=True)          # Host List 表头
_F_HL_BODY = Font(name="宋体", size=11)                     # Host List 数据

_A_WRAP = Alignment(vertical="center", wrap_text=True)
_A_WRAP_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
_A_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_A_FALLBACK = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _est_lines(text: str, width: float) -> int:
    """估算单元格自动换行行数: 中文按 2 个宽度单位, 列宽单位≈1 个 ASCII 字符宽。"""
    if not text:
        return 1
    usable = max(width - 1.0, 4.0)
    total = 0
    for seg in str(text).split("\n"):
        units = sum(2 if ord(ch) > 127 else 1 for ch in seg)
        total += max(1, math.ceil(units / usable))
    return max(1, total)


def _row_height(pairs: list, line_pt: float = 12.75, min_h: float = 15.0) -> float:
    """按 (文本, 列宽) 列表计算行高; line_pt 为单行行距 (字号10→12.75, 字号16→21)。"""
    lines = max((_est_lines(t, w) for t, w in pairs), default=1)
    return max(min_h, lines * line_pt + 2.25)


def _merge_runs(ws, col: int, row_from: int, values: list):
    """同值连续单元格纵向合并 (模板 time 列 B10:B16 样式); 合并块水平居中。"""
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            if i - start > 1 and values[start]:
                r0, r1 = row_from + start, row_from + i - 1
                ws.merge_cells(start_row=r0, start_column=col, end_row=r1, end_column=col)
                ws.cell(row=r0, column=col).alignment = _A_CENTER
            start = i


def _set_widths(ws, widths: dict):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


# ═══ Plan ═══
def _build_plan(ws, citp: dict, approval: dict):
    ws.sheet_view.zoomScale = 115
    _set_widths(ws, {"A": 5.875, "B": 36.0, "C": 63.625, "D": 24.625, "E": 24.625,
                     "F": 31.125, "G": 11.375})
    ref = (citp.get("reference_number") or citp.get("citp_no")
           or (approval or {}).get("approval_id") or "N/A")
    dev = citp.get("device") or {}
    host = dev.get("name") or dev.get("hostname") or dev.get("target") or ""
    label = str(citp.get("fault_type") or "维护").strip()
    # Approved by 固定用模板人物 "Apollo" (不套用审批人账号): 旧快照中与
    # 审批记录 approver_ds 相同的自动填充值一并还原为模板人物
    approved_by = str(citp.get("approved_by") or "").strip()
    if not approved_by or approved_by == str((approval or {}).get("approver_ds") or ""):
        approved_by = "Apollo"
    header = [
        ("Reference number:", ref),
        ("Date: Time:", citp.get("window") or (approval or {}).get("change_window") or "N/A"),
        ("Purpose:", ref),
        ("Preparedy by:", citp.get("prepared_by") or "GCOC"),
        ("In-charge by:", citp.get("in_charge_by") or "GCOC"),
        ("Approved by:", approved_by),
    ]
    for i, (k, v) in enumerate(header, 1):
        # 标签值区行高随内容自适应 (放不下自动换行, 最矮 15 同模板)
        ws.row_dimensions[i].height = _row_height([(v, 63.625)], min_h=15.0)
        for col in (1, 2, 3):
            c = ws.cell(row=i, column=col)
            c.border = _BORDER
            c.alignment = _A_WRAP
        ws.cell(row=i, column=2, value=k).font = _F_LABEL
        ws.cell(row=i, column=3, value=v).font = _F_VALUE

    # A7:C7 黄底 "Change Mamagement" (模板原拼写)
    ws.merge_cells("A7:C7")
    ws.row_dimensions[7].height = 15.75
    for col in (1, 2, 3):
        c = ws.cell(row=7, column=col)
        c.border = _BORDER
        c.fill = _YELLOW
    c7 = ws.cell(row=7, column=1, value="Change Mamagement")
    c7.font = _F_VALUE
    c7.alignment = _A_CENTER

    # 第 8 行蓝底白字表头 (C 列标题 = {故障动作}步骤, 同模板 "更换故障磁盘升级步骤")
    ws.row_dimensions[8].height = 22.5
    cols = ["step", "time", f"{label}步骤", "操作人员", "Verify/Test", "Fallback"]
    for j, h in enumerate(cols, 1):
        c = ws.cell(row=8, column=j, value=h)
        c.font = _F_HEAD
        c.fill = _BLUE
        c.border = _BORDER
        c.alignment = _A_CENTER

    # 数据区 (同模板: 除「事先准备」外的执行步骤时间列统一为维护时间窗,
    # 合并成单一块, 对应模板 B10:B16)
    window = str(citp.get("window") or "").strip()
    has_window = bool(window) and "待选择" not in window and window != "-"
    times = []
    r = 9
    for st in (citp.get("plan") or []):
        desc = str(st.get("desc") or "")
        cmd = str(st.get("command") or "").strip()
        if cmd and cmd not in desc:
            desc = f"{desc}\n{cmd}" if desc else cmd
        tm = str(st.get("time") or "")
        if has_window and tm and tm != "事先准备":
            tm = window
        vals = [st.get("step"), tm, desc, st.get("operator") or "",
                st.get("verify") or "", st.get("fallback") or ""]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=j, value=v)
            c.border = _BORDER
            if j == 1:
                c.font = _F_STEP
                c.alignment = _A_WRAP_LEFT
            elif j == 6:
                c.font = _F_FALLBACK
                c.fill = _FALLBACK_FILL
                c.alignment = _A_FALLBACK
            else:
                c.font = _F_VALUE
                c.alignment = _A_WRAP
        # 行高按各换行列 (B/C/D/E/F) 内容计算, 放不下自动换行且不裁剪
        ws.row_dimensions[r].height = _row_height(
            [(vals[1], 36.0), (desc, 63.625), (vals[3], 24.625),
             (vals[4], 24.625), (vals[5], 31.125)])
        times.append(vals[1])
        r += 1
    if times:
        _merge_runs(ws, 2, 9, times)


# ═══ Check_list ═══
def _build_checklist(ws, citp: dict):
    ws.sheet_view.zoomScale = 70
    _set_widths(ws, {"A": 17.375, "B": 27.875, "C": 21.125, "D": 11.5,
                     "E": 98.0, "F": 37.5, "G": 32.75})
    headers = ["Step", "Host", "IP/URL", "Sub Step", "Test item",
               "Expected result", "Result(保留源数据或截图）"]
    ws.row_dimensions[1].height = 20.25
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = _F_CL_HEAD
        c.border = _BORDER
        c.alignment = _A_CENTER
    r, steps = 2, []
    for ck in (citp.get("checklist") or []):
        vals = [ck.get("step"), ck.get("host") or "", ck.get("ip") or "",
                ck.get("sub_step") or "", ck.get("item") or "",
                ck.get("expected") or "", ""]
        fonts = [_F_CL_ABCG, _F_CL_ABCG, _F_CL_ABCG, _F_CL_D, _F_CL_E,
                 _F_CL_ABCG, _F_CL_ABCG]
        aligns = [_A_CENTER, _A_CENTER, _A_WRAP_LEFT, _A_CENTER, _A_WRAP_LEFT,
                  _A_CENTER, _A_CENTER]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=j, value=v)
            c.font = fonts[j - 1]
            c.border = _BORDER
            c.alignment = aligns[j - 1]
        # 行高按各换行列内容计算 (字号16, 单行约 21pt), 放不下自动换行且不裁剪
        ws.row_dimensions[r].height = _row_height(
            [(vals[1], 27.875), (vals[2], 21.125), (vals[3], 11.5),
             (vals[4], 98.0), (vals[5], 37.5)],
            line_pt=21.0, min_h=20.25)
        steps.append(vals[0])
        r += 1
    # Step 分组对 A/B/C 列纵向合并 (同模板 A2:A5/B2:B5/C2:C5)
    start = 0
    for i in range(1, len(steps) + 1):
        if i == len(steps) or steps[i] != steps[start]:
            if i - start > 1:
                for col in (1, 2, 3):
                    ws.merge_cells(start_row=2 + start, start_column=col,
                                   end_row=2 + i - 1, end_column=col)
            start = i


# ═══ Host List ═══
_A_HL = Alignment(vertical="center", wrap_text=True)


def _build_host_list(ws, citp: dict):
    _set_widths(ws, {"A": 18.25, "B": 14.875, "C": 22.625, "D": 16.0, "E": 15.75})
    ws.row_dimensions[1].height = 18.75
    for j, h in enumerate(["设备名", "位置", "型号", "HDM IP", "IP"], 1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = _F_HL_HEAD
        c.fill = _BLUE
        c.border = _BORDER
        c.alignment = _A_HL
    devs, seen = [], set()
    for d in (citp.get("devices") or []):
        name = d.get("name") or d.get("hostname") or d.get("target") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        devs.append(d)
    r = 2
    for d in devs:
        vals = [d.get("name") or d.get("hostname") or d.get("target") or "",
                d.get("rack") or d.get("site") or "",
                d.get("model") or "", "", d.get("ip") or ""]  # HDM IP 现场补
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=j, value=v)
            c.font = _F_HL_BODY
            c.border = _BORDER
            c.alignment = _A_HL
        ws.row_dimensions[r].height = _row_height(
            [(vals[0], 18.25), (vals[1], 14.875), (vals[2], 22.625), (vals[4], 15.75)],
            line_pt=14.25, min_h=15.0)
        r += 1
    # 网段信息 (按管理 IP /24 推导, 同模板 Subnet/gateway/netmask 行; 设备行后空 4 行, 同模板 r9-r12)
    ip = str(((citp.get("device") or {}).get("ip")) or (devs[0].get("ip") if devs else "") or "")
    octs = ip.split(".") if ip.count(".") == 3 else []
    net_rows = [("Subnet", f"{'.'.join(octs[:3])}.0" if octs else ""),
                ("gateway", f"{'.'.join(octs[:3])}.1" if octs else ""),
                ("netmask", "255.255.255.0" if octs else "")]
    r += 4
    for k, v in net_rows:
        for j, val in enumerate((k, v), 1):
            c = ws.cell(row=r, column=j, value=val)
            c.font = _F_HL_BODY
            c.border = _BORDER
            c.alignment = _A_HL
        r += 1


# ═══ 验收结果 (模板外扩展页, 风格与模板页签一致: 蓝底表头 + 宋体正文 + 全表换行) ═══
def _build_acceptance(ws, citp: dict):
    acc = citp.get("acceptance") or {}
    dev = citp.get("device") or {}
    import time as _t
    head_a = [
        ("主机", dev.get("name") or dev.get("target") or "N/A"),
        ("测试时间", acc.get("tested_at") or "N/A"),
        ("结论", {"green": "✅ 通过 (green)", "warn": "⚠ 有告警项 (warn)",
                  "red": "❌ 失败 (red)"}.get(acc.get("overall_status"),
                                             acc.get("overall_status") or "N/A")),
        ("验收人 / 时间", f"{acc.get('by') or 'N/A'} / "
         f"{_t.strftime('%Y-%m-%d %H:%M', _t.localtime(float(acc.get('ts') or 0)))}"),
    ]
    for i, (k, v) in enumerate(head_a, 1):
        ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=2)
        c = ws.cell(row=i, column=1, value=k)
        c.font = _F_LABEL
        c.alignment = _A_WRAP
        ws.merge_cells(start_row=i, start_column=3, end_row=i, end_column=4)
        c2 = ws.cell(row=i, column=3, value=v)
        c2.font = _F_VALUE
        c2.alignment = _A_WRAP
        # 值区合并 C:D (16 宽, 不跨打印分页); 长值自动换行, 行高按两块内容计算
        ws.row_dimensions[i].height = _row_height([(k, 24.0), (v, 16.0)], min_h=15.0)
    for j, h in enumerate(["网卡", "vSwitch", "Link", "结论", "问题明细"], 1):
        c = ws.cell(row=6, column=j, value=h)
        c.font = _F_HEAD
        c.fill = _BLUE
        c.border = _BORDER
        c.alignment = _A_CENTER
    ws.row_dimensions[6].height = 22.5
    for i, n in enumerate(acc.get("nics") or []):
        vals = [n.get("name") or "N/A", n.get("vswitch") or "N/A",
                "Up" if n.get("link_up") else "Down", n.get("summary") or "N/A",
                "; ".join(n.get("issues") or [])]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=7 + i, column=j, value=v)
            c.font = _F_VALUE
            c.border = _BORDER
            c.alignment = _A_WRAP
        ws.row_dimensions[7 + i].height = _row_height(
            [(vals[0], 12.0), (vals[1], 12.0), (vals[3], 8.0), (vals[4], 70.0)],
            min_h=15.0)
    for col, w in zip("ABCDE", (12, 12, 8, 8, 70)):
        ws.column_dimensions[col].width = w


def build_citp_workbook(citp: dict, approval: dict = None):
    """CITP 快照 → 工作簿 (页签/版式与真实 CITP 模板一致)。

    页签顺序: Plan, Check_list, Ping Snashot, Host List, Rack Layout [, 验收结果]
    """
    import openpyxl

    wb = openpyxl.Workbook()
    _build_plan(wb.active, citp or {}, approval or {})
    wb.active.title = "Plan"
    _build_checklist(wb.create_sheet("Check_list"), citp or {})
    wb.create_sheet("Ping Snashot").sheet_view.zoomScale = 115
    _build_host_list(wb.create_sheet("Host List"), citp or {})
    wb.create_sheet("Rack Layout").sheet_view.zoomScale = 85
    if (citp or {}).get("acceptance"):
        _build_acceptance(wb.create_sheet("验收结果"), citp or {})
    wb.properties.creator = "Z.ai"
    return wb
