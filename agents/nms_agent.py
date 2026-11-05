"""
Z-OPS — nms_agent: IBM Netcool NMS 告警监控 Agent
职责: NMS 告警定时监控/抓取/分析/飞书通知 + 告警驱动的自动化检查工作流编排
"""

import asyncio
import json
import logging
import base64
import os
import re
import socket
import jieba  # 中文分词: 告警查询关键词提取
import time
import hashlib
from pathlib import Path as FilePath
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from contextlib import asynccontextmanager
from collections import deque

import redis.asyncio as aioredis
from agents.base import BaseAgent, AgentMessage, run_agent

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

SCREENSHOT_DIR = config.BASE_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# NMS Redis 键名
NMS_KEY_LATEST   = f"{config.REDIS_PREFIX}:nms_alarms:latest"
NMS_KEY_HISTORY  = f"{config.REDIS_PREFIX}:nms_alarms:history"
NMS_KEY_HISTORY_Z = f"{config.REDIS_PREFIX}:nms_alarms:history_z"  # Phase H: 长期保留 ZSET (score=首见时间)
NMS_KEY_SEEN     = f"{config.REDIS_PREFIX}:nms_alarms:seen"


def _nms_parse_iso_ts(ts_str: str) -> Optional[float]:
    """Netcool 告警时间字符串 → epoch 秒。解析失败返回 None。

    支持: ISO-8601 / 'M/D/YY, h:MM AM/PM' 等 NMS 常见格式; 容忍 .SSS 毫秒与 +TZ 后缀。
    """
    if not ts_str:
        return None
    import datetime as _dt
    s = ts_str.strip().split('.')[0].split('+')[0].rstrip('Z').rstrip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%m/%d/%y, %I:%M %p', '%m/%d/%Y, %I:%M %p'):
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None

# NMS 告警时间为东八区（API 请求带 timezone=Asia/Chungking）；服务器运行在 UTC，
# naive isoformat 会被前端 new Date() 当浏览器本地时间解析导致 -8h 偏差，
# 所有落库/展示时间戳统一输出带偏移的东八区时间
TZ_CN = timezone(timedelta(hours=8))


def _nms_now_iso() -> str:
    return datetime.now(TZ_CN).isoformat()
NMS_KEY_ANALYSIS = f"{config.REDIS_PREFIX}:nms_alarms:analysis"
NMS_KEY_SEEN     = f"{config.REDIS_PREFIX}:nms_alarms:seen"
_NMS_LOCAL_KB_HEADER = chr(10)+chr(10)+'### 🔧 修复方案核查（知识库参考）'+chr(10)+chr(10)+'> 以下为知识库检索到的相关修复知识，供高优告警处置参考：'+chr(10)+chr(10)
_NMS_KB_CONTEXT_HEADER = chr(10) + chr(10) + '【知识库检索结果（供修复方案核查参考）】' + chr(10)
_NMS_KB_CHECK_INSTRUCTION = chr(10) + chr(10) + '请在上述输出基础上，额外增加一节【修复方案核查】：结合上方【知识库检索结果】逐条核查高优告警的处置/修复步骤，命中知识库的请标注来源链接，未命中的标注“KB无匹配，以下为通用建议”。' + chr(10)

# ── OPS-CORE 故障处置 Checklist（xlsx）：告警 → 处置步骤的确定性映射 ────────────────
# 规则表：(告警摘要正则, checklist 故障名称)，按序匹配，一条告警只取首个命中。
# 原则：宁缺勿滥——禁止单一宽词（如裸 vcenter/mailbox/电源），特征词在前、泛词在后
_NMS_CHECKLIST_RULES = [
    (r"ssl证书|certificat.{0,12}expir", "SSL证书过期"),
    # datastore 容量类；排除性能类上下文：Nimble "volume ...-Datastore2 throughput reached 90%"
    # 是卷吞吐/IOPS 告警，卷名里带 Datastore 不等于 datastore 容量告警（容量/性能处置完全不同）
    (r"^(?!.*(?:throughput|iops|latency|吞吐|带宽|时延))(?!.*cloud switch)(?=.*datastore)",
     "ESXi Datastore使用率高"),
    (r"drs.{0,12}(conflict|冲突)|_drs_conflict", "vCenter DRS冲突"),
    (r"vsphere ha|ha.{0,8}(restart|重启)", "VM HA重启"),
    (r"consolidat|快照(整合|合并)", "VM快照整合异常"),
    # 硬件故障 (MCE/ECC) 必须先于使用率规则: 否则 "esxi ... cpu" 宽模式会吞掉硬件特征
    (r"(mce|machine check|\becc\b.{0,12}(error|错)|memory.{0,15}(ecc|correctable|uncorrectable)|esxi.{0,25}(硬件|hardware).{0,20}(cpu|mem|内存|fault))", "ESXi CPU/内存硬件故障"),
    (r"esxi.{0,15}(cpu|mem|内存)", "ESXi CPU/内存使用率高"),
    (r"^(?!.*cloud switch)(?=.*vmnic)(?=.*(中断|down|fail|link))", "ESXi网卡故障"),
    (r"not respond|vpxa|无法连接host|host.{0,8}(disconnect|down)|esxi.{0,10}(失联|不可达)", "vCenter连接Host失败"),
    (r"o365", "A2C O365备份失败"),
    (r"\ba2c\b|vspc", "A2C VSPC备份失败"),
    # Zerto Journal 空间不足：必须有 journal 证据（scratch_journal / has reached its configured /
    # exceeded N percent）。"zerto vpg status: 3" 只是 VPG 状态异常（RPO 未达/复制中断等），
    # 按 Journal 预案去扩容 journal 会误导；而真实 journal 告警（简报摘要较长）易被距离阈值漏掉
    (r"scratch[\s_-]*journal|journal.{0,25}(空间|不足|full|reached|exceeded|阈值|超限)"
     r"|(?:zerto|vpg).{0,40}journal", "Zerto VPG Journal空间不足"),
    # 备份慢 (慢/超时特征) 必须先于 VBR备份失败: 否则 ^veeam backup / \bvbr\b 宽模式会吞掉慢场景
    (r"(vbr|veeam).{0,25}(慢|slow|超时|timeout|时长.{0,8}(高|超))", "vbr 备份慢"),
    # 真实 VBR 告警以 "veeam backup" 开头（如 "veeam backup time critical: 24"）；
    # 路径含 Veeam 的 diskSpace、交换机端口流量的 "Veeam Backup" 链路名均不算
    (r"^\s*veeam\s+backup\b|\bvbr\b|备份失败", "VBR备份失败"),
    # 存储厂商白名单: compellent/netapp/nimble/ceph/msa + 中文"存储"。
    # \bdisk(?!space) 裸 disk 匹配（disk异常/disk状态/disks 命中，diskSpace 排除）；
    # 排序保证 Veeam/o365/zerto/datastore 类 diskSpace 告警先被上方规则接住。
    # 容量/使用率语义一律排除："vcenter Disk使用率: 82% (position=mapper/log_vg-log_)"
    # 是虚机逻辑卷用量（磁盘空间告警），不是物理盘/RAID 硬件故障；误归会引导按预案去
    # 核对设备 SN、登录存储管理页看 RAID，处置方向完全错位。
    (r"^(?!.*cloud switch)(?!.*(?:使用率|使用量|利用率|占用率|\busage|\butili[sz]))"
     r"(?=.*((compellent|netapp|nimble|ceph|msa\d*|存储).{0,10}disk|(physical|hard).{0,4}disk|\braid\b|磁盘故障|ceph.{0,15}osd|osd.{0,10}(down|fail|error|异常)|\bdisk(?!\s*(?:space|使用|容量|空间|占用|free))))",
     "存储磁盘故障"),
    (r"(?:netapp|nimble|ceph).{0,20}(cluster|ha|port|node).{0,15}(state|状态|异常)|存储控制器|controller.{0,10}(fail|fault|异常)", "存储控制器故障"),
    (r"mailq|mail queue|邮件队列堆积", "SCM邮件队列堆积"),
    (r"spam|发送大量邮件", "SCM邮箱异常外发"),
    (r"proxymta.{0,15}(down|outage|不可达|异常)", "Proxymta outage故障"),
    (r"mailbox.{0,15}(down|outage|不可达|异常)", "mailbox outage故障"),
    (r"avas.{0,15}(down|outage|不可达|异常)", "AVAS outage故障"),
    (r"gslb", "TAB故障"),
    (r"vidyo", "vidyo outage故障"),
    (r"pdns", "PDNS outage故障"),
    (r"dns.{0,12}(解析|resolve|fail)|无法解析", "DNS解析异常"),
    # 存储电源 (更具体) 必须先于 ESXi 电源宽规则: 避免存储侧电源告警被 ESXi 规则吞掉
    (r"(compellent|netapp|nimble|ceph|msa\d*|存储|storage|阵列).{0,25}(电源|power).{0,15}(故障|异常|fail|fault|丢失|lost)", "存储电源故障"),
    (r"(?=.*esxi)(?=.*(power|电源))|电源(故障|异常)", "ESXi电源故障"),
    # ── Phase 3.1: 补齐 xlsx 中已有预案但缺规则的故障 (宁缺勿滥, 特征词在前) ──
    # 客户 MC 账号登录 VCD 失败: 必须同时含 VCD 与登录特征, 避免吞掉其它登录类告警
    (r"(?=.*(vcd|vmware cloud director))(?=.*(无法登录|登录失败|登陆失败|login.{0,10}(fail|denied)|账号.{0,10}(锁|lock|异常)))", "客户MC账号无法登录登入VCD"),
    # VM 使用慢: VM/虚拟机 + 性能关键词 (CPU/内存使用率类已有专属规则在前, 不会误吞)
    (r"(vm|虚拟机).{0,25}(使用慢|运行慢|很慢|卡顿|slow)", "VM使用慢"),
    # 邮件收不到 (入向): 收件方视角
    (r"(收不到|接收不到|未收到|没有收到).{0,12}(指定)?(邮件|mail|email)", "SCM 邮件收不到指定邮件"),
    # 邮件发出去对方没收到 (出向): 发件方视角
    (r"(发出去|已发出|已发送|发送成功).{0,20}(对方|对端).{0,12}(没|未|没有)收到", "SCM 邮件发出去对方没收到"),
    # VM Web 服务无法打开: web/http + 打不开/无法访问
    (r"(web|http).{0,40}(无法打开|打不开|无法访问|不可访问|browse.{0,10}fail)", "VM配置了WEB服务，但是该WEB浏览器无法打开"),
    # ESXi 主机 outage: 主机整机不可用 (连接类/使用率类规则在前, 仅剩整机 outage)
    (r"esxi.{0,20}outage|esxi.{0,15}host.{0,12}(宕机|down|isolat)|host.{0,15}(isolated|isolation)", "ESXI主机outage故障"),
    # 存储整机故障: 存储 + outage/宕机 (磁盘/控制器/电源均有专属规则在前)
    (r"(compellent|netapp|nimble|ceph|msa\d*|存储|storage).{0,25}(outage|宕机|整体不可用|完全不可用)", "存储故障"),
]
_nms_checklist_cache = {"mtime": 0.0, "entries": {}}

# DNS 专项告警识别 (需要解析测试的告警摘要特征；排除仅 app=SDNSv3 的内存/CPU 类告警)
# 注: resolve(?!d) 排除 resolved/unresolved (如 NSX 的 alarmBeingResolved)
_DNS_ALARM_TEST_RE = re.compile(
    r"解析(异常|失败)|无法解析|resolve(?!d)|dns[_\s]?query", re.IGNORECASE
)

# 历史重复模式展示时清理摘要尾部的波动量 (次数/时长等), 让展示更稳定
_NMS_HISTORY_SUMMARY_RE = re.compile(
    r"[::]\s*\d+([.,]\d+)?\s*(%|s|min|次|天|小时)?\s*$|\s+\d+([.,]\d+)?(ms|%)?\s*$")

# 活跃级别集合 (入历史/参与聚类的告警级别)
_NMS_ACTIVE_SEV = {"critical", "major", "minor", "warning", "indeterminate"}


def _nms_alarm_content_key(alarm: Dict) -> str:
    """内容键: 同一逻辑事件的稳定指纹 (Node|EventKey|规范化摘要) → 16hex。

    Netcool Serial 会因事件重插而换手, 按 alarm_id(serial) 记历史会把同一
    事件反复占位 (换手高峰一天可产生数万"新" Serial); 内容键跨 Serial 稳定,
    是历史去重的正确粒度。摘要剥离尾部波动量, 使 "disk异常: 5" 与 "...: 3"
    视为同一事件 (occurrences 累加而非新增条目)。"""
    node = (alarm.get("node") or alarm.get("node_alias")
            or alarm.get("source") or "").strip().lower()
    ekey = (alarm.get("event_key") or alarm.get("alert_key")
            or "").strip().lower()
    summ = _NMS_HISTORY_SUMMARY_RE.sub("", alarm.get("summary") or "")
    summ = re.sub(r"\s+", " ", summ).strip().lower()
    return hashlib.md5(
        f"{node}|{ekey}|{summ}".encode("utf-8", "ignore")).hexdigest()[:16]


def _nms_history_bound_epoch(ts):
    """时间筛选边界 → epoch (与 history_z score 同一口径)。

    score 写入用 _nms_parse_iso_ts: naive 时间按服务器时区(UTC)解释, 即
    Netcool CST 墙钟按 UTC 折算。前端传 "+08:00" 带偏移的墙钟字符串,
    这里剥掉时区取同一墙钟按 UTC 解释, 保证边界与 score 可比。"""
    if not ts:
        return None
    import datetime as _dt
    try:
        dt = _dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        return dt.replace(tzinfo=None).timestamp()
    except Exception:
        return _nms_parse_iso_ts(ts)


async def nms_history_read(r, base: str, limit: int = 200, page: int = 1,
                           groups=None, from_ts: str = '', to_ts: str = '',
                           scan_cap: int = 60000, q: str = ''):
    """读历史告警 (时间倒序)。返回 (alarms, source, has_more)。

    主路径: history_z (member=内容键) + history_data (payload 哈希);
      - score = 最早首见时间, from_ts/to_ts 直接作分数区间 (zrevrangebyscore);
      - 无 groups 且无 q: 按分数区间 rank 分页, 快路径;
      - 有 groups 或 q: 分批扫描 (zrevrangebyscore + hmget) 过滤, 收满 page*limit 即停,
        扫描上限 scan_cap 条防御全库扫（命中稀疏时仍能在 ~1s 内返回）。
    q: 关键字搜索（服务端全库口径，不再只在前端已加载的本页 200 条里筛）。
      多个词 AND；在非 UTF-8 安全的原始字节上先子串匹配（避免先解码上百 MB），
      只有命中项才 json.loads；为与前端旧口径一致，haystack 含"序列化后的整条
      记录"（因此字段名也可能被命中，与旧行为相同不会提词）。
    降级: 老 LIST (兼容未迁移环境, 忽略 groups/page, 时间按 payload 首见过滤)。"""
    limit = max(1, min(int(limit or 200), 1000))
    page = max(1, int(page or 1))
    gs = set(groups or [])
    terms = [t.lower() for t in str(q or '').split() if t.strip()]
    if not terms and q and str(q).strip():
        terms = [str(q).strip().lower()]
    lo = _nms_history_bound_epoch(from_ts)
    hi = _nms_history_bound_epoch(to_ts)
    lo_s = '-inf' if lo is None else str(lo)
    hi_s = '+inf' if hi is None else str(hi)
    k_z = f"{base}:history_z"
    k_data = f"{base}:history_data"

    def _match(obj) -> bool:
        if gs:
            _g = obj.get("groups") or ([obj["group"]] if obj.get("group") else [])
            if not (_g and any(g in gs for g in _g)):
                return False
        if terms:
            hay = json.dumps(obj, ensure_ascii=False).lower()
            if not all(t in hay for t in terms):
                return False
        return True

    try:
        if await r.zcard(k_z) > 0:
            need = page * limit
            out: List = []
            if not gs and not terms:
                # 快路径: 分数区间 + rank 分页, 一次定位
                cks = await r.zrevrangebyscore(
                    k_z, hi_s, lo_s, start=(page - 1) * limit, num=limit)
                vals = await r.hmget(k_data, cks) if cks else []
                for v in vals:
                    if not v:
                        continue
                    try:
                        out.append(json.loads(v))
                    except Exception:
                        continue
                alarms = out[:limit]
                if alarms:
                    return alarms, 'zset', len(out) >= limit
            else:
                start = 0
                step = 500
                scanned = 0
                while len(out) < need and scanned < scan_cap:
                    cks = await r.zrevrangebyscore(
                        k_z, hi_s, lo_s, start=start, num=step)
                    if not cks:
                        break
                    start += step
                    scanned += len(cks)
                    vals = await r.hmget(k_data, cks)
                    for v in vals:
                        if not v:
                            continue
                        b = v if isinstance(v, bytes) else str(v).encode("utf-8")
                        if terms:
                            # 先字节级筛选：命中才解码+解析（全库解码要上百 MB）
                            low = b.lower()
                            if not all(t.encode("utf-8", "ignore") in low
                                       for t in terms):
                                continue
                        try:
                            obj = json.loads(b)
                        except Exception:
                            continue
                        if not _match(obj):
                            continue
                        out.append(obj)
                        if len(out) >= need:
                            break
                alarms = out[(page - 1) * limit: page * limit]
                if alarms:
                    return alarms, 'zset', len(out) >= need
    except Exception:
        pass
    raw = await r.lrange(f"{base}:history", -limit, -1)
    out = []
    for it in reversed(raw):
        try:
            obj = json.loads(it)
        except Exception:
            continue
        if lo is not None or hi is not None:
            t = _nms_parse_iso_ts(obj.get("first_time") or obj.get("time") or "")
            if t is None:
                continue
            if lo is not None and t < lo:
                continue
            if hi is not None and t > hi:
                continue
        if not _match(obj):
            continue
        out.append(obj)
    return out, 'list', False


def _nms_load_checklist() -> Dict[str, Dict]:
    """加载 OPS-CORE checklist xlsx → {故障名称: {steps, sheet, variants}}（mtime 缓存，可热更新）

    variants 按 sheet（单客户/平台）各保留一份处理建议；steps/sheet 为步骤最详细的主份（兼容旧引用）。
    """
    path = config.NMS_CHECKLIST_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _nms_checklist_cache["entries"] and _nms_checklist_cache["mtime"] == mtime:
        return _nms_checklist_cache["entries"]
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        entries: Dict[str, Dict] = {}
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=2, max_col=2, values_only=True):
                name = (str(row[0]) if row and row[0] else "").strip()
                steps = (str(row[1]) if row and len(row) > 1 and row[1] else "").strip()
                if not name or not steps or name == "故障名称":
                    continue
                ent = entries.setdefault(name, {"steps": "", "sheet": "", "variants": []})
                # 跨 sheet（单客户/平台）同名各保留一份 variant，同 sheet 重复取步骤更详细的
                v = next((x for x in ent["variants"] if x["sheet"] == ws.title), None)
                if v is None:
                    ent["variants"].append({"sheet": ws.title, "steps": steps})
                elif len(steps) > len(v["steps"]):
                    v["steps"] = steps
                # 主份取步骤更详细的一份（兼容旧引用）
                if len(steps) > len(ent["steps"]):
                    ent["steps"], ent["sheet"] = steps, ws.title
        wb.close()
        # 跨 sheet 同名且步骤完全相同的 variant 归并为一份（sheet 名合并，如"单客户 / 平台"），
        # 避免前端并排两张重复卡片；内容有差异时才保留多份
        for ent in entries.values():
            merged: List[Dict] = []
            for v in ent["variants"]:
                dup = next((x for x in merged if x["steps"] == v["steps"]), None)
                if dup:
                    dup["sheet"] = f"{dup['sheet']} / {v['sheet']}"
                else:
                    merged.append(dict(v))
            ent["variants"] = merged
        _nms_checklist_cache["mtime"] = mtime
        _nms_checklist_cache["entries"] = entries
        logger.info(f"[NMS] Checklist 已加载: {len(entries)} 条故障处置预案 ({path})")
        return entries
    except Exception as e:
        logger.warning(f"[NMS] Checklist 加载失败 ({path}): {e}")
        return _nms_checklist_cache["entries"] or {}


async def _nms_merged_checklist(r=None):
    """xlsx 预案 + Redis 覆盖层 合并视图（前端「预案清单」页可编辑的最终生效数据源）：
    xlsx 打底（mtime 缓存）→ 移除 deleted Set 标记的 → Hash 中自定义/编辑预案同名覆盖。
    匹配/分析/清单页统一走此函数，编辑保存后立即生效。"""
    entries = dict(_nms_load_checklist())
    own_conn = False
    try:
        if r is None:
            # P0 修复: 使用共享连接池 (不再每次调用创建新连接 → "Too many connections")
            if _nms_shared_redis is not None:
                r = _nms_shared_redis
            else:
                r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
                own_conn = True
        deleted = {x for x in (await r.smembers(config.KEY_NMS_CHECKLIST_DELETED) or []) if x}
        raw = await r.hgetall(config.KEY_NMS_CHECKLIST_PLANS) or {}
        custom = {}
        for title, v in raw.items():
            try:
                ent = json.loads(v)
                custom[title] = {"steps": ent.get("steps", ""),
                                 "sheet": ent.get("sheet", ""),
                                 "variants": ent.get("variants") or []}
            except Exception:
                continue
    except Exception:
        deleted, custom = set(), {}
    finally:
        if own_conn:
            try:
                await r.aclose()
            except Exception:
                pass
    out = {t: e for t, e in entries.items() if t not in deleted}
    out.update(custom)
    return out


def _nms_checklist_match(summary: str, entries: Dict[str, Dict]) -> str:
    """单条告警摘要 → checklist 故障名称（未命中返回空串）"""
    if not summary or not entries:
        return ""
    s = summary.lower()
    for pat, title in _NMS_CHECKLIST_RULES:
        if title in entries and re.search(pat, s, re.I):
            return title
    return ""


def _nms_checklist_context(alarms: List[Dict], max_entries: int = 8,
                            entries: Dict[str, Dict] = None) -> str:
    """把活跃告警按关键词规则匹配到 checklist 条目，返回注入 LLM 的处置参考文本
    （entries 可传入合并后视图；缺省回落 xlsx 原始清单）"""
    if entries is None:
        entries = _nms_load_checklist()
    if not entries:
        return ""
    hit_count: Dict[str, int] = {}
    for a in alarms:
        title = _nms_checklist_match(a.get("summary") or "", entries)
        if title:
            hit_count[title] = hit_count.get(title, 0) + 1
    if not hit_count:
        return ""
    top = sorted(hit_count.items(), key=lambda x: -x[1])[:max_entries]
    blocks = []
    for title, cnt in top:
        # 同名预案在单客户/平台两个 sheet 各有一份时，两侧处理建议都注入
        variants = entries[title].get("variants") or [
            {"sheet": entries[title]["sheet"], "steps": entries[title]["steps"]}]
        parts = []
        for v in variants:
            steps = v["steps"]
            if len(steps) > 500:
                steps = steps[:500] + "…"
            parts.append(f"【{v['sheet']}】{steps}")
        blocks.append(f"【{title}】（匹配 {cnt} 条告警）\n" + "\n".join(parts))
    return "\n\n".join(blocks)


# 切换告警视图分组的 JS（IBM Tivoli CustomFilteringSelect + dojo/store）
# 通过 store.query 枚举选项，按 label 精确匹配，再用 set('item') 触发 onChange
_NMS_SELECT_FILTER_JS = """async (name) => {
    const inp = document.querySelector("input[id*='filterDropDown']");
    if (!inp) return {ok:false, err:'no input'};
    const dijit = window.dijit;
    if (!dijit) return {ok:false, err:'no dijit'};
    const w = dijit.getEnclosingWidget(inp);
    if (!w) return {ok:false, err:'no widget'};
    const store = w.store || w._store;
    if (!store || !store.query) return {ok:false, err:'no store.query'};
    const searchAttr = w.searchAttr || 'label';
    let items;
    const r = store.query({});
    if (r && typeof r.then === 'function') items = await r;
    else if (r && r.forEach && !Array.isArray(r)) { items=[]; r.forEach(x=>items.push(x)); }
    else items = r;
    let target = null;
    for (const it of (items||[])) {
        if (it[searchAttr] === name || it.label === name || it.name === name) { target = it; break; }
    }
    if (!target) return {ok:false, err:'name not in store'};
    const identity = store.getIdentity ? store.getIdentity(target) : (target.id||null);
    try { w.set('item', target); }
    catch(e) { try { w.set('value', identity); } catch(e2) { return {ok:false, err:'set failed: '+e2}; } }
    try { if (w.onChange) w.onChange(w.get('value')); } catch(e) {}
    return {ok:true, identity: identity, valueAfter: w.get('value'), display: target[searchAttr]};
}"""


# 标准级别 → NMS SeverityButtons 控件标题
_NMS_SEV_TITLES = {"critical": "Critical", "major": "Major", "minor": "Minor",
                   "warning": "Warning", "indeterminate": "Indeterminate"}
# 标准级别 → 中文名（通知/卡片展示用）
_NMS_SEV_CN = {"critical": "严重", "major": "主要", "minor": "轻微",
               "warning": "警告", "indeterminate": "未知"}


_NMS_SEVERITY_JS = """async (severities) => {
    // 精确设置级别筛选（SeverityButtons ToggleButton），severities 之外的级别全部取消勾选
    const dijit = window.dijit;
    if (!dijit) return {ok:false, err:'no dijit'};
    const root = document.querySelector("[id*='toolbar_severityButtons']");
    if (!root) return {ok:false, err:'no severityButtons root'};
    const titles = ['Critical','Major','Minor','Warning','Indeterminate'];
    const states = [];
    for (let i = 0; i < titles.length; i++) {
        const el = document.getElementById(root.id + '_severityButton' + i);
        if (!el) continue;
        const b = dijit.getEnclosingWidget(el) || dijit.byId(root.id + '_severityButton' + i);
        if (!b) continue;
        const want = severities.indexOf(titles[i]) >= 0;
        if (b.get('checked') !== want) b.set('checked', want);
        states.push({t: titles[i], checked: b.get('checked')});
    }
    return {ok:true, states: states};
}"""


# ─── 确认状态(ACK)快速过滤：Build Quick Filter 对话框（Acknowledged Equals 0 = 仅未确认） ───
_NMS_QF_ARROW_CLICK_JS = """() => {
    const el = document.querySelector("[id*='quickfilter_button_arrow']")
        || document.querySelector("[id*='quickfilter_button']");
    if (!el) return {ok:false, err:'no quickfilter button'};
    el.click();
    return {ok:true};
}"""
_NMS_QF_FIELD_CLICK_JS = """() => {
    const el = document.querySelector("[id*='filterDlg_field']");
    if (!el) return {ok:false, err:'no filterDlg field'};
    el.click();
    return {ok:true};
}"""
_NMS_QF_OPERATOR_CLICK_JS = """() => {
    const el = document.querySelector("[id*='filterDlg_operator']");
    if (!el) return {ok:false, err:'no filterDlg operator'};
    el.click();
    return {ok:true};
}"""
_NMS_QF_PICK_JS = """(label) => {
    // 点击弹出菜单中文本精确匹配的菜单项（避开 underlay 拦截）
    const items = document.querySelectorAll("[id*='dijit_MenuItem'], .dijitMenuItem");
    for (const it of items) {
        const t = (it.textContent || '').trim();
        if (t === label) { it.click(); return {ok:true, clicked: t}; }
    }
    return {ok:false, err: 'menu item not found: ' + label};
}"""
_NMS_QF_TERM_JS = """(term) => {
    const inp = document.querySelector("input[id*='filterDlg_term']");
    if (!inp) return {ok:false, err:'no filterDlg term input'};
    inp.focus();
    inp.value = term;
    inp.dispatchEvent(new Event('input', {bubbles:true}));
    inp.dispatchEvent(new Event('change', {bubbles:true}));
    return {ok:true, value: inp.value};
}"""
_NMS_QF_APPLY_JS = """() => {
    const el = document.querySelector("[id*='filterDlg_apply']");
    if (!el) return {ok:false, err:'no filterDlg apply button'};
    el.click();
    return {ok:true};
}"""


_NMS_SCROLL_EXTRACT_JS = r"""async () => {
  const hdrKws = ["FirsOccurrence","LastOccurrence","Summary","Severity"];
  const TAB = String.fromCharCode(9);
  let header = null;
  const seen = new Set();
  const rows = [];
  const scan = () => {
    for (const t of document.querySelectorAll("table")) {
      const trs = t.querySelectorAll("tr");
      if (trs.length > 2) continue;
      const txt = t.innerText || "";
      if (txt.length < 5) continue;
      const cells = txt.split(TAB).map(c => c.trim());
      if (cells.length < 5) continue;
      if (txt.includes("Total:") || txt.includes("Selected:") || txt.includes("Refresh in:") || txt.includes("Apply") || txt.includes("Build Quick Filter")) continue;
      if (cells.some(c => hdrKws.includes(c))) { if (!header) header = cells; continue; }
      if (cells.length < 10) continue;
      const sig = cells.join(String.fromCharCode(1));
      if (seen.has(sig)) continue;
      seen.add(sig);
      const td = t.querySelector("td"); const tr = t.querySelector("tr");
      let bg = "";
      if (td) { bg = td.style.backgroundColor || td.getAttribute("bgcolor") || ""; if (!bg) bg = window.getComputedStyle(td).backgroundColor || ""; }
      if (!bg && tr) { bg = tr.style.backgroundColor || tr.getAttribute("bgcolor") || ""; if (!bg) bg = window.getComputedStyle(tr).backgroundColor || ""; }
      const cls = ((td || tr || t).className || "").toString();
      rows.push({cells: cells, bg: bg, cls: cls});
    }
  };
  scan();
  const firstScreen = rows.length;
  let best = document.querySelector(".gridxVScroller");
  if (!best || best.scrollHeight - best.clientHeight <= 50) {
    best = null; let bestDelta = 0;
    for (const el of document.querySelectorAll("*")) {
      const d = el.scrollHeight - el.clientHeight;
      if (d > bestDelta && el.clientHeight > 80) { bestDelta = d; best = el; }
    }
  }
  if (best) {
    const t0 = Date.now();
    const step = Math.max(50, best.clientHeight - 60);
    let pos = 0;
    for (let k = 0; k < 800; k++) {
      best.scrollTop = pos;
      await new Promise(r => setTimeout(r, 150));
      scan();
      if (rows.length >= 2000) break;
      if (Date.now() - t0 > 45000) break;
      if (pos >= best.scrollHeight) break;
      pos += step;
    }
    best.scrollTop = best.scrollHeight;
    await new Promise(r => setTimeout(r, 250));
    scan();
    best.scrollTop = 0;
  }
  let total = "";
  const mt = (document.body.innerText || "").match(/Total:\s*([0-9]+)/);
  if (mt) total = mt[1];
  return {header: header, rows: rows, firstScreen: firstScreen, total: total};
}"""



# 连续认证失败熔断 (2026-10-09 锁号事故): 陈旧密码反复失败登录会把 AD 账号锁死。
# 达阈值后拒绝一切抓取尝试; 登录成功清零, 进程重启归零 → 修好凭据重启即恢复。
_NMS_AUTH_FAILS = 0
_NMS_AUTH_FAIL_LIMIT = 5


async def _nms_do_login(page, username: str, password: str) -> bool:
    """登录 IBM Netcool 控制台 (模块级, 供 nms_agent 与 API 进程 SSO 引导复用)。

    成功返回 True; 失败 (含 Netcool 回显 error=wrongPassword 等) 返回 False。
    """
    url = config.NMS_CONSOLE_URL
    logger.info(f"[NMS] 正在访问: {url}")
    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
    # 等待页面稳定
    await asyncio.sleep(5)

    # 检查是否已登录（无需登录的场景）
    current_url = page.url
    logger.info(f"[NMS] 当前 URL: {current_url}")

    # Cookie 会话有效时直接落在 /ibm/console 控制台; 未认证才被重定向到 logon.jsp。
    # 先按 URL 判定, 避免控制台页含 "login" 字样被误判为登录页而多余提交表单
    if "logon.jsp" not in current_url.lower() and "/ibm/console" in current_url.lower():
        logger.info("[NMS] 已是登录态 (URL 非 logon.jsp), 跳过表单登录")
        return True

    # 检查页面是否有登录表单特征
    page_content = await page.content()
    is_login_page = any(keyword in page_content.lower() for keyword in [
        'user id', 'userid', 'username', 'login', 'sign in', '登录'
    ])

    if not is_login_page:
        logger.info("[NMS] 页面不含登录表单，可能已登录")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
            return True
        except Exception:
            pass

    logger.info("[NMS] 检测到登录页面，开始填写表单...")

    # IBM Netcool 常见登录表单选择器
    login_selectors = [
        ("input#userId, input[name='userId']", "userId"),
        ("input#username, input[name='username']", "username"),
        ("input[name='user']", "user"),
        ("input[type='text']:first-of-type", "first_text"),
    ]

    user_filled = False
    for selector, desc in login_selectors:
        try:
            await page.fill(selector, username, timeout=5000)
            user_filled = True
            logger.info(f"[NMS] 用户名已填写 (selector: {desc})")
            break
        except Exception as e:
            logger.debug(f"[NMS] 选择器 {desc} 失败: {e}")
            continue

    if not user_filled:
        try:
            await page.wait_for_selector("input[type='text']", timeout=10000)
            inputs = await page.query_selector_all("input[type='text']")
            if inputs:
                await inputs[0].fill(username)
                user_filled = True
                logger.info("[NMS] 用户名已填写 (fallback: first text input)")
        except Exception as e:
            logger.warning(f"[NMS] 未找到用户名输入框: {e}")

    # 填写密码
    pwd_filled = False
    pwd_selectors = [
        ("input#password, input[name='password']", "password"),
        ("input[type='password']", "password_type"),
    ]
    for selector, desc in pwd_selectors:
        try:
            await page.fill(selector, password, timeout=5000)
            pwd_filled = True
            logger.info(f"[NMS] 密码已填写 (selector: {desc})")
            break
        except Exception as e:
            logger.debug(f"[NMS] 密码选择器 {desc} 失败: {e}")
            continue

    if not user_filled or not pwd_filled:
        logger.error(f"[NMS] 表单填写失败: user={user_filled}, pwd={pwd_filled}")
        return False

    # 点击登录按钮
    submit_clicked = False
    submit_selectors = [
        ("button[type='submit']", "submit_button"),
        ("input[type='submit']", "submit_input"),
        ("button#loginBtn, button#login", "login_id"),
        ("button:has-text('Log in')", "log_in_text"),
        ("button:has-text('Login')", "login_text"),
        ("button:has-text('登录')", "login_cn"),
        ("button:has-text('Sign in')", "sign_in"),
        ("input[value='Login'], input[value='Log in']", "value_login"),
    ]
    for selector, desc in submit_selectors:
        try:
            await page.click(selector, timeout=5000)
            submit_clicked = True
            logger.info(f"[NMS] 登录按钮已点击 (selector: {desc})")
            break
        except Exception as e:
            logger.debug(f"[NMS] 登录按钮选择器 {desc} 失败: {e}")
            continue

    if not submit_clicked:
        # 尝试按 Enter 键提交
        logger.warning("[NMS] 未找到登录按钮，尝试按 Enter 提交")
        await page.keyboard.press("Enter")

    # 等待登录后页面加载
    logger.info("[NMS] 等待登录完成...")
    await asyncio.sleep(8)
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # 验证登录是否成功
    await asyncio.sleep(2)
    new_url = page.url
    logger.info(f"[NMS] 登录后 URL: {new_url}")

    # Netcool 登录失败会以 query 参数回显错误码 (error=wrongPassword / expired 等),
    # 必须显式检查 — 否则误报"登录成功"再在后续找 frame 时莫名失败, 排障成本高
    if "error=" in new_url:
        from urllib.parse import urlparse, parse_qs
        try:
            _err = (parse_qs(urlparse(new_url).query).get("error") or [""])[0]
        except Exception:
            _err = "unknown"
        if _err:
            logger.error(f"[NMS] 认证失败 (NMS 返回 error={_err})。"
                         f"若 AD 密码已变更, 请更新 zops.env 的 NMS_PASSWORD 并重启 nms_agent")
            global _NMS_AUTH_FAILS
            _NMS_AUTH_FAILS += 1
            return False

    # 检查是否还在登录页
    new_content = await page.content()
    still_login = any(keyword in new_content.lower() for keyword in [
        'user id', 'userid', 'username', 'password'
    ]) and 'alarm' not in new_content.lower() and 'event' not in new_content.lower()

    if still_login:
        logger.error("[NMS] 登录可能失败，仍在登录页面")
        return False

    logger.info("[NMS] 登录成功")
    _NMS_AUTH_FAILS = 0
    return True


# ────────────────────────────────────────────────────────────────────────────
# SSO 会话引导 (零密码存储): 用户 AD 登录 Z-OPS 的瞬间, 用内存中密码一次性登录
# Netcool, 抓取会话 Cookie 存 Redis (密码即弃, 不落盘不持久化)。
# 后续该用户的 NMS 抓取复用 Cookie; 会话过期 → 下次 Z-OPS 登录自动重建。
# ────────────────────────────────────────────────────────────────────────────
NMS_SESSION_TTL = getattr(config, "NMS_SESSION_TTL", 8 * 3600)


async def nms_bootstrap_session(zops_user: str, nms_username: str,
                                 nms_password: str) -> dict:
    """一次性 Playwright 登录 → 捕获会话 Cookie → 存 Redis → 销毁浏览器。

    密码仅存在于本次调用内存, 不写日志/不落盘/不进 Redis。
    Cookie 键: {prefix}:nms_session:{zops_user}, 值: JSON cookies 数组。
    """
    if not nms_username or not nms_password:
        return {"success": False, "error": "缺少凭据"}
    from playwright.async_api import async_playwright
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        ok = await _nms_do_login(page, nms_username, nms_password)
        if not ok:
            await browser.close()
            return {"success": False, "error": "NMS 登录失败 (凭据被 Netcool 拒绝)"}
        cookies = await context.cookies()
        await browser.close()
        if not cookies:
            return {"success": False, "error": "未捕获到会话 Cookie"}
        import redis.asyncio as aioredis
        r = _get_nms_shared_redis()  # 共享连接, 不泄漏
        await r.set(f"{config.REDIS_PREFIX}:nms_session:{zops_user}",
                    json.dumps(cookies, ensure_ascii=False),
                    ex=NMS_SESSION_TTL)
        logger.info(f"[NMS-SSO] 用户 {zops_user} 的 NMS 会话已建立 "
                    f"(cookies={len(cookies)}, TTL={NMS_SESSION_TTL}s, 密码未存储)")
        return {"success": True, "cookies": len(cookies)}
    except Exception as e:
        logger.error(f"[NMS-SSO] 会话引导失败: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass

# NMS 共享 Redis 连接 (避免 checklist 每次调用创建新连接 → 连接池耗尽)
_nms_shared_redis = None


def _get_nms_shared_redis():
    global _nms_shared_redis
    import redis.asyncio as _aioredis
    if _nms_shared_redis is None:
        _nms_shared_redis = _aioredis.from_url(config.REDIS_URL,
                                                 decode_responses=True, max_connections=5)
    return _nms_shared_redis


class NmsAgent(BaseAgent):
    name = "nms_agent"
    description = "IBM Netcool NMS 告警监控与分析"

    def __init__(self):
        super().__init__()
        self._browser = None
        self._playwright = None
        self._nms_last_alarm_hash: dict = {}   # 上次告警快照 hash（用于去重）
        self._nms_round_no = 0                  # 定时抓取轮次计数（分组分析降频用）
        self._dyn_cfg_cache: dict = None        # 前端动态配置缓存（KEY_NMS_DYN_CFG）
        self._dyn_cfg_ts = 0.0
        # 最近一轮 API 采集质量（内部诊断：status/error/row_count/identity_unreliable/fallback_source）
        self._nms_collect_quality: dict = {}
        # ── Phase 1: LLM 调度原语 ──
        # 双锁拆分：用户请求(on_demand)与后台批量(scheduled)互不阻塞；scheduled
        # 在进入前会先让出 pending 的 on_demand。详细设计见 wiki/opt-nms-latency-p95.md
        self._nms_on_demand_lock: Optional[asyncio.Lock] = None
        self._nms_scheduled_lock: Optional[asyncio.Lock] = None
        self._nms_on_demand_pending = 0          # 当前等待/持有 on_demand_lock 的调用计数
        self._nms_llm_bucket: deque = deque()    # LLM 调用时间戳队列(token bucket)
        # 兼容旧引用 (webchat_api.py / 其它模块仍可能拿到旧的单锁名字)
        self._nms_llm_lock: Optional[asyncio.Lock] = None

    # ────────────────────────────────────────────────────────────────────────
    # Phase 1.1 + 1.4: LLM 调度原语 — 优先级锁 + Token Bucket
    # ────────────────────────────────────────────────────────────────────────
    @asynccontextmanager
    async def _nms_acquire_llm_lock(self, kind: str = "on_demand"):
        """按优先级获取 NMS LLM 调用锁。

        - ``on_demand``: 用户/手动触发的分析。串行化在 ``_nms_on_demand_lock`` 上，
          永远不会阻塞等待 ``scheduled`` 任务完成；同时维护 ``_nms_on_demand_pending``
          计数，scheduled 进入前会先让出正在排队的 on_demand。
        - ``scheduled``: 定时抓取轮次内的批量分析。串行化在 ``_nms_scheduled_lock`` 上；
          进入前先 drain 已 pending 的 on_demand（最大 ``NMS_SCHED_WAIT_ON_DEMAND`` 秒），
          拿到锁后再校验一次，若仍有 on_demand 排队则让出本次。

        旧 ``_nms_llm_lock`` 字段作为向后兼容保留，调用时退化为 ``on_demand`` 行为。
        """
        # 惰性初始化 (允许 __new__ 直构造的测试实例安全使用)
        if not hasattr(self, "_nms_on_demand_lock") or self._nms_on_demand_lock is None:
            self._nms_on_demand_lock = asyncio.Lock()
        if not hasattr(self, "_nms_scheduled_lock") or self._nms_scheduled_lock is None:
            self._nms_scheduled_lock = asyncio.Lock()
        if not hasattr(self, "_nms_llm_lock") or self._nms_llm_lock is None:
            self._nms_llm_lock = self._nms_on_demand_lock
        if not hasattr(self, "_nms_on_demand_pending"):
            self._nms_on_demand_pending = 0

        if kind == "scheduled":
            # 先让出 pending 的 on_demand (最多 NMS_SCHED_WAIT_ON_DEMAND 秒)
            ticks = max(1, getattr(config, "NMS_SCHED_WAIT_TICKS", 100))
            wait_total = max(0.1, getattr(config, "NMS_SCHED_WAIT_ON_DEMAND", 10))
            should_skip = False
            for _ in range(ticks):
                if self._nms_on_demand_pending == 0:
                    break
                await asyncio.sleep(wait_total / ticks)
            else:
                if self._nms_on_demand_pending > 0:
                    logger.info("[nms_agent] scheduled 等待 on_demand 超时，放弃本次")
                    should_skip = True
            await self._nms_scheduled_lock.acquire()
            try:
                # 进入锁后再次校验：新到的 on_demand 应当抢占
                if (not should_skip
                        and self._nms_on_demand_pending > 0
                        and getattr(config, "NMS_SCHED_YIELD_ON_DEMAND", True)):
                    logger.info("[nms_agent] scheduled 检测到 on_demand 排队，主动让出")
                    should_skip = True
                if should_skip:
                    # asynccontextmanager 必须 yield 一次; 用一个空 yield + 立即退出
                    yield False
                    return
                yield True
            finally:
                if self._nms_scheduled_lock.locked():
                    self._nms_scheduled_lock.release()
        else:
            self._nms_on_demand_pending += 1
            try:
                async with self._nms_on_demand_lock:
                    yield True
            finally:
                self._nms_on_demand_pending -= 1

    async def _nms_acquire_llm_bucket(self, timeout: float = 30.0) -> bool:
        """Token Bucket 限流：窗口内最多 NMS_LLM_BUCKET_MAX 次调用。

        Returns True 表示获准通过，False 表示超时未拿到（调用方应回退到本地摘要）。
        """
        window = max(1, getattr(config, "NMS_LLM_BUCKET_WINDOW", 60))
        cap = max(1, getattr(config, "NMS_LLM_BUCKET_MAX", 2))
        now = time.monotonic()
        # 清理窗口外的旧时间戳
        while self._nms_llm_bucket and (now - self._nms_llm_bucket[0]) > window:
            self._nms_llm_bucket.popleft()
        if len(self._nms_llm_bucket) < cap:
            self._nms_llm_bucket.append(now)
            return True
        # 等到最早的 token 出窗 (最多 timeout 秒)
        wait = max(0.1, (self._nms_llm_bucket[0] + window) - now)
        if wait > timeout:
            return False
        await asyncio.sleep(wait)
        # 重新检测 (期间可能又过了一个)
        now = time.monotonic()
        while self._nms_llm_bucket and (now - self._nms_llm_bucket[0]) > window:
            self._nms_llm_bucket.popleft()
        if len(self._nms_llm_bucket) < cap:
            self._nms_llm_bucket.append(now)
            return True
        return False

    @staticmethod
    def _nms_auto_llm_should_trigger(new_alarms: List[Dict],
                                     novel_patterns: Dict[str, int],
                                     seen_patterns: set,
                                     last_auto_ts: Optional[float] = None,
                                     now_ts: Optional[float] = None) -> tuple:
        """Phase 3.3: 自动 LLM 分析复合门控 (纯函数, 便于单测)。

        触发条件 (任一命中, 且冷却通过):
          1. 任一 critical 告警属于新模式
          2. 新模式告警累计 >= NMS_LLM_NEW_PATTERN_MIN
          3. 影响面: 新增告警涉及来源数 >= NMS_AUTO_IMPACT_MIN
        冷却: 距上次自动分析 (last_auto_ts) 不足 NMS_LLM_COOLDOWN 秒 → 拒绝。

        Returns:
            (should_trigger: bool, reason: str)  reason 用于日志与「已自动分析」标记。
        """
        if not bool(getattr(config, "NMS_AUTO_LLM_ANALYZE", False)):
            return False, "auto_disabled"
        if not new_alarms:
            return False, "no_new"
        # 冷却检查
        cooldown = max(0, getattr(config, "NMS_LLM_COOLDOWN", 1800))
        now = now_ts if now_ts is not None else time.time()
        if cooldown > 0 and last_auto_ts is not None and (now - last_auto_ts) < cooldown:
            return False, f"cooldown({int(now - last_auto_ts)}s<{cooldown}s)"
        # ① critical 新模式
        novel_keys = set(novel_patterns or {})
        for a in new_alarms:
            if (a.get("severity") == "critical"
                    and NmsAgent._nms_pattern_key(a) in novel_keys):
                return True, "critical_novel"
        # ② 新模式数阈值
        novel_count = sum((novel_patterns or {}).values())
        if novel_count >= getattr(config, "NMS_LLM_NEW_PATTERN_MIN", 3):
            return True, f"novel_patterns({novel_count})"
        # ③ 影响面
        sources = {((a.get("source") or a.get("node") or "")).strip().lower()
                   for a in new_alarms}
        sources.discard("")
        if len(sources) >= getattr(config, "NMS_AUTO_IMPACT_MIN", 8):
            return True, f"impact({len(sources)}_sources)"
        return False, "below_threshold"

    @staticmethod
    def _nms_analysis_is_fresh(record: Optional[dict], now_ts: Optional[float] = None) -> bool:
        """判定既有分析是否在 TTL 内且仍可复用 (Phase 1.2)。"""
        if not record or not isinstance(record, dict):
            return False
        ttl = getattr(config, "NMS_ANALYSIS_TTL", 480)
        if ttl <= 0:
            return False
        ts_str = record.get("timestamp")
        if not ts_str:
            return False
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            return False
        now_ts = now_ts if now_ts is not None else time.time()
        return (now_ts - ts) <= ttl

    async def start(self):
        """覆写 start：启动消息订阅 + NMS 监控协程"""
        # 启动 NMS 定时监控后台协程
        if config.NMS_USERNAME and config.NMS_PASSWORD:
            asyncio.create_task(self._nms_monitor_loop())
            logger.info("[nms_agent] NMS 告警监控已启用 "
                        f"(interval={config.NMS_CHECK_INTERVAL}s)")
        else:
            logger.warning("[nms_agent] NMS 凭据未配置，跳过定时监控")
        # 继续原有启动流程
        await super().start()

    async def _ensure_browser(self):
        """确保浏览器已启动"""
        if self._browser is None:
            import os as _os, tempfile as _tf
            _pwtmp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), chr(46) + chr(112) + chr(119) + chr(95) + chr(116) + chr(109) + chr(112))
            try:
                _os.makedirs(_pwtmp, exist_ok=True)
                _os.environ[chr(84)+chr(77)+chr(80)+chr(68)+chr(73)+chr(82)] = _pwtmp
                _tf.tempdir = _pwtmp
            except Exception:
                pass
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                logger.info("[nms_agent] Chromium 已启动")
            except ImportError:
                raise RuntimeError(
                    "playwright 未安装，请运行: pip install playwright && playwright install chromium"
                )
            except Exception as e:
                raise RuntimeError(f"浏览器启动失败: {e}")
        return self._browser

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"
        handlers = {
            "nms_alarm_query": self._nms_alarm_query,
            "nms_alarm_now":   self._nms_alarm_now,
            "nms_analyze_group": self._nms_analyze_group,
            "nms_analyze_stream": self._nms_analyze_stream,
            "chat":            self._nms_alarm_query,
        }
        handler = handlers.get(action, self._nms_alarm_query)
        await handler(msg)

    async def _nms_monitor_loop(self):
        """定时抓取 IBM NMS 告警页面并分析（后台协程，永不终止）"""
        # 启动后等待 30 秒，让浏览器完全就绪（分段等待：期间收到退出信号立即结束，
        # 不必让重启白等半分钟）
        for _ in range(30):
            if not self._running:
                return
            await asyncio.sleep(1)
        logger.info("[nms_agent] NMS 监控循环启动")

        # 订阅 bot 命令唤醒频道：feishu_bot 入队截图/分析命令后 publish，
        # 可立即开启下一轮，无需等满周期（默认 10 分钟）
        _wake_ps = None
        try:
            _wake_ps = (await self.get_redis()).pubsub()
            await _wake_ps.subscribe(config.NMS_WAKE_CHANNEL)
        except Exception as e:
            logger.warning(f"[nms_agent] 唤醒频道订阅失败: {e}")

        while self._running:
            _t0 = time.time()
            try:
                # P0: 周期任务只用显式配置的服务主体 — 服务账号会话 → zops.env 服务凭据;
                # 已移除借用其他用户个人 SSO 会话的回退。
                _r = await self.get_redis()
                _session_user = await self._nms_pick_global_session(_r)
                if _session_user is None and not config.NMS_PASSWORD:
                    # 服务主体未授权 (无会话且未配置服务凭据) → 暂停本轮并展示原因 (4h 去重)
                    _pause_key = f"{config.REDIS_PREFIX}:nms_session:pause_alert"
                    if await _r.set(_pause_key, "1", ex=4 * 3600, nx=True):
                        await self.notify(
                            "⏸️ **NMS 周期采集已暂停**\n"
                            f"服务账号 {config.NMS_USERNAME} 无可用 NMS 会话, 且 zops.env 未配置 "
                            "NMS_PASSWORD 服务凭据。\n"
                            "按最小权限策略, 周期任务不再借用个人 SSO 会话。\n"
                            "**恢复方式**: 用服务账号登录一次 Z-OPS 重建会话, 或在 zops.env 配置 "
                            "服务账号凭据后重启 nms_agent。",
                            topic="nms_snapshot")
                        logger.warning("[nms_agent] 周期采集暂停: 服务主体无会话且未配置服务凭据")
                    await self._record_sched_exec(
                        "nms_scheduled_round", _t0, False,
                        err="paused: 服务主体无会话且未配置 NMS_PASSWORD (P0 移除个人会话借用)")
                else:
                    await self._nms_run_once(session_user=_session_user)
                    await self._record_sched_exec("nms_scheduled_round", _t0, True)
            except Exception as e:
                logger.error(f"[nms_agent] NMS 监控执行异常: {e}")
                await self._record_sched_exec("nms_scheduled_round", _t0, False, err=str(e))
            # 等待下一轮（间隔支持前端管理页动态配置；bot 命令唤醒可提前触发）
            await self._nms_wait_next_round(_wake_ps)

    async def _nms_effective_interval(self) -> int:
        """当前生效的采集间隔（秒）：管理页动态配置优先，静态默认兜底。"""
        _interval = config.NMS_CHECK_INTERVAL
        try:
            _dyn = await self._nms_dyn_cfg(await self.get_redis())
            if isinstance(_dyn.get("check_interval"), (int, float)) and _dyn["check_interval"] >= 60:
                _interval = int(_dyn["check_interval"])
        except Exception:
            pass
        return _interval

    async def _nms_wait_next_round(self, wake_ps) -> None:
        """等待下一轮采集。

        分段等待（≤1s）：管理页把采集间隔调短/调长后，无需等满旧周期即可热生效
        （无需重启进程）；bot 命令唤醒可提前结束等待、立即开下一轮；
        `_running=False`（SIGTERM）后最多 1 秒退出，不等满旧分段——这是把
        agent 重启窗口从 45s 压到秒级、消除「重启期间告警凭空消失」的关键。
        """
        _round_end = time.time()
        _interval = await self._nms_effective_interval()
        while self._running:
            _remain = _round_end + _interval - time.time()
            if _remain <= 0:
                return
            _step = min(_remain, 1.0)
            if wake_ps is not None:
                try:
                    _msg = await wake_ps.get_message(
                        ignore_subscribe_messages=True, timeout=_step)
                    if _msg is not None and _msg.get("type") == "message":
                        logger.info("[nms_agent] 收到 bot 命令唤醒，立即开启下一轮")
                        return
                except Exception:
                    await asyncio.sleep(_step)
            else:
                await asyncio.sleep(_step)
            # 每段等待后重读动态配置：调短则提前触发，调长则顺延
            _new_interval = await self._nms_effective_interval()
            if _new_interval != _interval:
                logger.info(f"[nms_agent] 采集间隔热更新 {_interval}s -> "
                            f"{_new_interval}s (管理页动态配置, 无需重启)")
                _interval = _new_interval

    async def _record_sched_exec(self, task: str, t0: float, success: bool,
                                 err: str = "", summary: str = "") -> None:
        """定时后台任务入账 (口径: 真实任务执行, 与用户消息/按需分析区分。
        监控循环不经消息总线, 不埋则 nms_agent 负载对可观测大盘完全不可见)"""
        try:
            from core.execution_registry import get_execution_registry, ExecutionType
            cost = time.time() - t0
            await get_execution_registry().record_tool_call(
                agent_id=self.name, tool_name=task,
                input_summary="NMS 定时后台任务",
                output_summary=(summary or f"耗时 {round(cost)}s")
                               + (f"，异常: {err[:200]}" if err else ""),
                success=success, latency_ms=round(cost * 1000, 1),
                exec_type=ExecutionType.TOOL_CALL,
            )
        except Exception as e:
            logger.debug(f"[nms_agent] 定时任务入账失败: {e}")

    async def _nms_dyn_cfg(self, r) -> Dict:
        """读取前端管理页动态配置（Redis KEY_NMS_DYN_CFG），带短缓存；无则返回 {}。
        优先级：Redis 动态配置 > config.py 环境变量静态默认。"""
        now = time.time()
        if self._dyn_cfg_cache is not None and now - self._dyn_cfg_ts < 30:
            return self._dyn_cfg_cache
        try:
            raw = await r.get(config.KEY_NMS_DYN_CFG)
            cfg = json.loads(raw) if raw else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        self._dyn_cfg_cache = cfg
        self._dyn_cfg_ts = now
        return cfg

    @staticmethod
    def _dyn_list(cfg: Dict, key: str, default: str) -> List[str]:
        """动态配置中逗号分隔列表项：显式空字符串=关闭（返回 []），未设置=静态默认。"""
        if key in cfg and cfg[key] is not None:
            return [x.strip() for x in str(cfg[key]).split(",") if x.strip()]
        return [x.strip() for x in (default or "").split(",") if x.strip()]

    @staticmethod
    async def _nms_pick_global_session(r) -> Optional[str]:
        """为全局采集挑选服务账号 SSO 会话 (P0: 只用显式配置的服务主体)

        优先级:
          1. 全局监控服务账号 (config.NMS_USERNAME) 自己的会话
          2. 无 → 返回 None, 调用方回退 zops.env 显式配置的服务账号密码登录。

        P0 移除: 借用任意其他登录用户的个人 SSO 会话 (跨用户回退)。
        周期任务只能使用显式配置且授权的服务主体; 个人会话属于个人,
        借用即以他人身份执行且其私有过滤视图可能污染全局数据。
        """
        prefix = f"{config.REDIS_PREFIX}:nms_session"
        try:
            if await r.get(f"{prefix}:{config.NMS_USERNAME}"):
                return config.NMS_USERNAME
        except Exception:
            pass
        return None

    async def _nms_ssl_observe(self, r, base: str, alarms: List[Dict]) -> None:
        """把本轮原始行交给 SSL 调查观察器（在空数据/无变化早退之前调用）。

        采集计数同时写 Redis，使 backend 进程的指标汇总/接口能看到本进程的
        采集异常率（两进程隔离，无法共享进程内指标）。
        """
        try:
            from core.nms_ssl_workflow import (
                observe_snapshot as _ssl_observe,
                record_collection_remote as _ssl_record_collection,
            )
            logger.info("[nms_ssl_metrics] collection ok "
                        + await _ssl_record_collection(r, base, True, empty=not alarms))
            touched = await _ssl_observe(base, alarms, _nms_now_iso())
            if touched:
                logger.info(f"[nms_agent] SSL 调查观测: {touched} 条记录已更新")
        except Exception as e:
            logger.warning(f"[nms_agent] SSL 调查观测失败 (不阻断采集): {e}")

    async def _nms_ssl_observe_error(self, r, base: str, error: str) -> None:
        """采集失败/身份不可靠：只记录采集异常，不推动恢复。"""
        try:
            from core.nms_ssl_workflow import (
                observe_collection_error as _ssl_observe_error,
                record_collection_remote as _ssl_record_collection,
            )
            logger.warning("[nms_ssl_metrics] collection error "
                           + await _ssl_record_collection(r, base, False, error=error))
            touched = await _ssl_observe_error(base, error, _nms_now_iso())
            if touched:
                logger.info(f"[nms_agent] SSL 采集异常已记录: {touched} 条调查")
        except Exception as e:
            logger.warning(f"[nms_agent] SSL 采集异常记录失败 (不阻断采集): {e}")

    async def _nms_run_once(self, nms_username: str = None, nms_password: str = None,
                            force_analyze: bool = False,
                            session_user: str = None):
        """执行一次 NMS 告警抓取 + 分析 + 通知

        session_user: SSO 会话用户 (Z-OPS 登录名) — 有则从 Redis 恢复其 NMS 会话
        Cookie 免密抓取 (SSO 引导建立); 会话过期则本次终止并提示重新登录 Z-OPS。
        """
        # 熔断: 连续认证失败达阈值后停止一切抓取 (含定时/唤醒/聊天触发),
        # 防止持续失败登录锁死 AD 账号。更新 zops.env 凭据后重启即恢复。
        global _NMS_AUTH_FAILS
        if _NMS_AUTH_FAILS >= _NMS_AUTH_FAIL_LIMIT:
            logger.warning(f"[nms_agent] 已熔断: 连续认证失败 {_NMS_AUTH_FAILS} 次 "
                           f"(≥{_NMS_AUTH_FAIL_LIMIT}), 本轮跳过。"
                           "请更新 zops.env 的 NMS_PASSWORD 后 "
                           "systemctl restart zops-agent@nms_agent")
            return False
        # 认证失败退避 (Redis 持久化, 进程重启不清零): 账号被 AD 锁定期间若仍按采集周期
        # 反复密码登录, 每次失败都可能把锁续上。失败 1/2/3/4 次 → 退避 30 分钟/1/2/4 小时,
        # 登录成功即清零。
        try:
            _r0 = await self.get_redis()
            _bu = await _r0.get(f"{config.REDIS_PREFIX}:nms_auth_backoff_until")
            if _bu and float(_bu) > time.time():
                _left = int((float(_bu) - time.time()) // 60) + 1
                logger.warning(f"[nms_agent] 认证失败退避中: 约 {_left} 分钟后重试 "
                               f"(账号可能被 AD 锁定, 避免持续试密顶锁)")
                return False
        except Exception:
            pass
        logger.info("[nms_agent] NMS 开始抓取...")
        r = await self.get_redis()

        # 按 owner 计算键前缀：普通用户 -> per-user 独立快照；svc-opscore/后台 -> 共享全局键
        if nms_username and nms_username != config.NMS_USERNAME:
            base = f"{config.REDIS_PREFIX}:nms_alarms:user:{nms_username}"
            is_personal = True
        else:
            base = f"{config.REDIS_PREFIX}:nms_alarms"
            is_personal = False
        k_latest = f"{base}:latest"
        k_history = f"{base}:history"
        k_analysis = f"{base}:analysis"
        k_seen = f"{base}:seen"
        logger.info(
            f"[nms_agent] NMS owner={'个人:'+nms_username if is_personal else '全局(svc-opscore)'}"
        )

        browser = await self._ensure_browser()
        page = await browser.new_page(ignore_https_errors=True)
        try:
            await page.set_viewport_size({"width": 1920, "height": 1080})

            # 1. 登录 — SSO Cookie 恢复优先, 无会话回退表单登录
            if session_user:
                cookies_raw = await r.get(
                    f"{config.REDIS_PREFIX}:nms_session:{session_user}")
                if not cookies_raw:
                    logger.warning(f"[nms_agent] 用户 {session_user} 无 NMS 会话 "
                                   f"(未 Z-OPS-AD 登录过或已过期), 终止本次个人抓取")
                    await page.close()
                    return
                try:
                    cookies = json.loads(cookies_raw)
                    await page.context.add_cookies(cookies)
                    logger.info(f"[nms_agent] 已恢复 {session_user} 的 NMS 会话 "
                                f"({len(cookies)} cookies)")
                except Exception as e:
                    logger.error(f"[nms_agent] 会话 Cookie 恢复失败: {e}")
                    await page.close()
                    return
            login_ok = await self._nms_login(page, nms_username, nms_password)
            if not login_ok:
                logger.error("[nms_agent] NMS 登录失败，终止本次抓取")
                # 仅密码登录失败计入退避 (Cookie 失效属正常轮换, 不视为密码错误)
                if not session_user:
                    try:
                        _fk = f"{config.REDIS_PREFIX}:nms_auth_fails"
                        _fails = int(await r.incr(_fk))
                        await r.expire(_fk, 24 * 3600)
                        _delay = min(1800 * (2 ** (_fails - 1)), 4 * 3600)
                        await r.set(f"{config.REDIS_PREFIX}:nms_auth_backoff_until",
                                    str(time.time() + _delay), ex=6 * 3600)
                        logger.warning(f"[nms_agent] 连续认证失败 {_fails} 次 → "
                                       f"退避 {_delay // 60} 分钟后再试 (避免持续顶锁 AD)")
                    except Exception as _e:
                        logger.debug(f"[nms_agent] 认证失败退避记录失败: {_e}")
                if session_user:
                    # Cookie 已失效 (落在登录表单) → 清掉, 下次 Z-OPS 登录自动重建
                    try:
                        await r.delete(
                            f"{config.REDIS_PREFIX}:nms_session:{session_user}")
                        logger.warning(f"[nms_agent] 用户 {session_user} 的 NMS 会话"
                                       f"已失效并清除, 重新登录 Z-OPS 可自动重建")
                    except Exception:
                        pass
                    # 全局语境 (全局账号会话失效 / 监控循环无会话可用) 且登录失败
                    # → 告警 (4h 去重)。这是"监控静默中断"的唯一信号源, 必须可感知
                    if session_user == config.NMS_USERNAME or session_user is None:
                        try:
                            _alert_key = f"{config.REDIS_PREFIX}:nms_session:alert_sent"
                            if not await r.set(_alert_key, "1", ex=4 * 3600, nx=True):
                                pass  # 4h 内已告警过
                            else:
                                await self.notify(
                                    "🔴 **NMS 全局采集会话已失效**\n"
                                    f"服务账号 {config.NMS_USERNAME} 的 NMS 会话过期且 "
                                    "zops.env 中的密码登录被 Netcool 拒绝 "
                                    "(error=wrongPassword)。\n"
                                    "**恢复方式**: 用服务账号 " + config.NMS_USERNAME + " "
                                    "登录 Z-OPS 重建 NMS 会话 (P0 起周期任务只认服务主体, "
                                    "其他个人账号登录不再可用于全局采集), "
                                    "或更新 zops.env 密码后重启 nms_agent。下一轮采集 (≤30 分钟) 自动恢复。",
                                    topic="nms_snapshot")
                                logger.warning("[nms_agent] 已发送全局会话失效告警")
                        except Exception as _e:
                            logger.debug(f"[nms_agent] 会话失效告警失败: {_e}")
                await page.close()
                return

            # 认证成功 → 清除失败计数/退避, 并缓存服务账号会话 Cookie 供后续轮次免密复用
            # (每轮密码登录都是一次 AD 认证, 缓存后 8h 内只需用密码登录 1 次)
            try:
                await r.delete(f"{config.REDIS_PREFIX}:nms_auth_fails",
                               f"{config.REDIS_PREFIX}:nms_auth_backoff_until")
                if (nms_username or config.NMS_USERNAME) == config.NMS_USERNAME:
                    _ck = await page.context.cookies()
                    if _ck:
                        await r.set(
                            f"{config.REDIS_PREFIX}:nms_session:{config.NMS_USERNAME}",
                            json.dumps(_ck, ensure_ascii=False), ex=NMS_SESSION_TTL)
                        logger.info(f"[nms_agent] 服务账号 NMS 会话已缓存 "
                                    f"({len(_ck)} cookies, TTL={NMS_SESSION_TTL}s), 后续轮次免密复用")
            except Exception as _e:
                logger.debug(f"[nms_agent] 认证成功后清理/缓存失败: {_e}")

            # 2. 定位告警内容 frame，逐个监控分组切换并提取
            content_frame = await self._nms_get_content_frame(page)
            if not content_frame:
                logger.error("[NMS] 未找到告警内容 frame，终止本次抓取")
                await page.close()
                return

            # 3. 通过 EventDataService JSON API 采集（无 cap、Serial 精确去重、全字段）
            try:
                alarms, group_counts, raw_count, columns = await self._nms_collect_via_api(page)
            except Exception as e:
                await self._nms_ssl_observe_error(r, base, f"采集失败: {e}")
                raise
            # 3.1 SSL 调查观察：在空数据/无变化早退之前，把本轮原始行交给调查观察器，
            # 只采信明确的 cleared 行；采集失败/身份不可靠只记录异常，不推动恢复
            if (self._nms_collect_quality or {}).get("status") == "error":
                await self._nms_ssl_observe_error(
                    r, base, (self._nms_collect_quality or {}).get("error") or "采集失败")
            else:
                await self._nms_ssl_observe(r, base, alarms)
            if not alarms:
                logger.warning("[nms_agent] NMS API 采集为空，终止本次抓取")
                await page.close()
                return
            logger.info(f"[nms_agent] NMS 提取: API 全量 {raw_count} 行 -> {len(alarms)} 条唯一告警 ({len(group_counts)} 视图)")

            # 2.5 以告警为核心的 Checklist 预案标注：逐条活跃告警确定性匹配处置预案，
            # 写入 alarm.checklist 供前端徽标/分析聚合直接使用（cleared 无处置意义不标注）
            # 合并视图（xlsx + 前端可编辑覆盖层），编辑保存后下一轮抓取即生效
            _cl_entries = await _nms_merged_checklist(r)
            _active_sev = {"critical", "major", "minor", "warning", "indeterminate"}
            checklist_steps = {}
            cl_hit = 0
            if _cl_entries:
                for a in alarms:
                    if a.get("severity") not in _active_sev:
                        continue
                    title = _nms_checklist_match(a.get("summary") or "", _cl_entries)
                    if not title:
                        continue
                    a["checklist"] = title
                    cl_hit += 1
                    if title not in checklist_steps:
                        checklist_steps[title] = {
                            "steps": _cl_entries[title]["steps"],
                            "sheet": _cl_entries[title]["sheet"],
                            "variants": _cl_entries[title].get("variants") or [],
                        }
                logger.info(f"[nms_agent] Checklist 标注: {cl_hit} 条活跃告警命中 {len(checklist_steps)} 个预案")

            # 3. 去重：与上次对比（按 owner 隔离哈希，避免个人/全局互相污染）
            # P1 (task-c17): 命令消费与"告警内容是否变化"解耦 — 队列有命令
            # (分析/截图) 或显式 force_analyze 时不得因无变化跳过本轮,
            # 否则飞书「nms 分析/截图」在告警静默期永远得不到执行。
            alarm_hash = self._nms_hash_alarms(alarms)
            if not isinstance(self._nms_last_alarm_hash, dict):
                self._nms_last_alarm_hash = {}
            _pending_cmds = False
            try:
                _pending_cmds = (not is_personal) and bool(await r.llen(config.NMS_CMD_KEY))
            except Exception:
                pass
            if (alarm_hash == self._nms_last_alarm_hash.get(base)
                    and not _pending_cmds and not force_analyze):
                logger.info("[nms_agent] NMS 告警无变化，跳过分析 (无待处理命令)")
                await page.close()
                return
            if alarm_hash == self._nms_last_alarm_hash.get(base):
                logger.info("[nms_agent] NMS 告警无变化, 但存在待处理命令/强制分析, 继续执行")
            self._nms_last_alarm_hash[base] = alarm_hash

            # 4. 找出新增告警
            seen_raw = await r.hgetall(k_seen)
            new_alarms = [a for a in alarms if a.get("alarm_id") not in seen_raw]

            # 4.5 工具实测：对命中预案的告警用现有工具做初步分析判断，
            # 结果随快照落库（tool_checks），供处理分析处置核对引用
            tool_checks = {}
            if config.NMS_DNS_RESOLVE_ENABLED:
                _dns_targets = [a for a in alarms
                                if a.get("checklist") == "DNS解析异常"
                                and a.get("severity") in _active_sev]
                if _dns_targets:
                    _dns_tests = await self._nms_dns_probe(_dns_targets)
                    if _dns_tests:
                        tool_checks["dns"] = _dns_tests
                        logger.info(f"[nms_agent] 工具实测(DNS): {len(_dns_tests)} 项")

            # SSL 证书实测：对 SSL 证书告警的网页地址做 TLS 握手核验剩余天数
            _ssl_targets = [a for a in alarms
                            if a.get("checklist") == "SSL证书过期"
                            and a.get("severity") in _active_sev]
            if _ssl_targets:
                _ssl_tests = await self._nms_ssl_probe(_ssl_targets)
                if _ssl_tests:
                    tool_checks["ssl"] = _ssl_tests
                    logger.info(f"[nms_agent] 工具实测(SSL): {len(_ssl_tests)} 项")

            # TCP 端口探测：网络服务类预案（PDNS outage 等）探测服务端口可达性
            _tcp_targets = [a for a in alarms
                            if a.get("checklist") == "PDNS outage故障"
                            and a.get("severity") in _active_sev]
            if _tcp_targets:
                _tcp_tests = await self._nms_tcp_probe(_tcp_targets)
                if _tcp_tests:
                    tool_checks["tcp"] = _tcp_tests
                    logger.info(f"[nms_agent] 工具实测(TCP): {len(_tcp_tests)} 项")

            # 备份作业实测：备份失败类预案用 BRR/M365 API 实测作业状态与最近还原点
            _bk_titles = {"VBR备份失败", "A2C VSPC备份失败", "A2C O365备份失败"}
            _bk_targets = [a for a in alarms
                           if a.get("checklist") in _bk_titles
                           and a.get("severity") in _active_sev]
            if _bk_targets:
                _bk_tests = await self._nms_backup_probe(_bk_targets)
                if _bk_tests:
                    tool_checks["backup"] = _bk_tests
                    logger.info(f"[nms_agent] 工具实测(备份): {len(_bk_tests)} 项")

            # 5. 存储最新快照
            snapshot = {
                "timestamp": _nms_now_iso(),
                "total": len(alarms),
                "unique_total": len(alarms),
                "raw_total": raw_count,
                "group_counts": group_counts,
                "columns": columns,
                "checklist_steps": checklist_steps,
                "tool_checks": tool_checks,
                "alarms": alarms
            }
            await r.set(k_latest, json.dumps(snapshot, ensure_ascii=False))
            # 旁路时间戳小键：API 比较快照新鲜度靠它，避免为取时间戳 GET 70+MB 本体
            await r.set(f"{k_latest}:ts", snapshot["timestamp"])

            # 5.5 关联降噪：把原始告警归并为「事件簇」，写入 {base}:incidents
            try:
                from agents.nms_correlation import correlate_alarms, analyze_scope, analyze_scope_by_group
                new_ids = {a.get("alarm_id") for a in new_alarms}
                incidents = correlate_alarms(
                    alarms,
                    systemic_min_sources=config.NMS_SYSTEMIC_MIN_SOURCES,
                    new_ids=new_ids,
                )
                # 站点/服务关联分析（网段 × 类型），供事件视图定性展示；
                # 同时按分组各算一份，避免分组过滤时口径不一致
                scope_analysis = analyze_scope(alarms)
                scope_by_group = analyze_scope_by_group(alarms)
                await r.set(f"{base}:incidents", json.dumps({
                    "timestamp": _nms_now_iso(),
                    "total_alarms": len(alarms),
                    "incident_count": len(incidents),
                    "incidents": incidents,
                    "scope_analysis": scope_analysis,
                    "scope_analysis_by_group": scope_by_group,
                }, ensure_ascii=False))
                logger.info(f"[nms_agent] NMS 关联降噪: {len(alarms)} 告警 -> "
                            f"{len(incidents)} 事件簇; 站点/服务评分 "
                            f"{scope_analysis.get('judgement', {}).get('site_score')}/"
                            f"{scope_analysis.get('judgement', {}).get('service_score')}; "
                            f"分组件 {len(scope_by_group)}")
            except Exception as e:
                logger.error(f"[nms_agent] NMS 关联降噪失败: {e}")
                incidents = []

            # 6. 记录新增告警到历史 ZSET (Phase H2: 内容键去重 — Serial 换手不重复占位)
            # 结构: {base}:history_z      member=内容键(16hex), score=最早首见时间
            #       {base}:history_data   field=内容键 → JSON(alarm + occurrences)
            if new_alarms:
                await self._nms_write_history_zset(r, base, new_alarms, k_seen)
                # 兼容期: 一次性迁移老 list / 修复存量 JSON 成员格式
                await self._nms_migrate_history_list_to_zset(
                    r, k_history, f"{base}:history_z", f"{base}:history_data")
                # 兼容旧 list (短期滚动窗口, 读取已优选 ZSET)
                for alarm in new_alarms:
                    if alarm.get("severity") in _NMS_ACTIVE_SEV:
                        await r.rpush(k_history,
                                      json.dumps(alarm, ensure_ascii=False))
                await r.ltrim(k_history, -config.NMS_HISTORY_MAX, -1)

            # 6.5 历史覆盖修复 (每轮幂等): 历史过去只由"本轮新增"写入, 长期活跃事件
            # (上线前的存量 / 摘要每日倒数而换内容键的事件) 永远补不进来, 老键又被
            # 按首见时间老化删除 → 正在告警的事件在"历史告警"里凭空消失。这里以当前
            # 快照为准补录缺失并做老化 (活跃豁免)。一次性基线导入 (marker 门禁) 在
            # 首次执行后即失效, 无法跟进后续漂移, 故被本步骤取代。
            await self._nms_repair_history(r, base, alarms)

            # 7.8 分组告警截图（browser tool）：重点分组 + 飞书 bot 命令请求的分组
            # ⚠️ 必须在 page.close() 前执行（需浏览器会话内切换分组视图并截图）
            # 每个分组截整组图 + 按级别（严重/主要/轻微…）筛选的告警图
            group_shots = []
            cmd_shots = []
            _force_analyze = False
            if not is_personal:
                _dyn = await self._nms_dyn_cfg(r)
                _shot_groups = self._dyn_list(
                    _dyn, "snapshot_groups", config.NMS_SNAPSHOT_GROUPS)
                _shot_sevs = [s.lower() for s in self._dyn_list(
                    _dyn, "snapshot_severities", config.NMS_SNAPSHOT_SEVERITIES)]
                # 确认状态筛选：默认仅截未确认告警（动态配置可关）
                _shot_unack = config.NMS_SNAPSHOT_UNACK_ONLY
                if "snapshot_unack_only" in _dyn and isinstance(_dyn["snapshot_unack_only"], bool):
                    _shot_unack = _dyn["snapshot_unack_only"]
                _cmds = []
                while len(_cmds) < 5:
                    _raw_cmd = await r.lpop(config.NMS_CMD_KEY)
                    if not _raw_cmd:
                        break
                    try:
                        _c = json.loads(_raw_cmd)
                    except Exception:
                        continue
                    if _c.get("cmd") == "snapshot" and _c.get("group"):
                        if _c["group"] not in _shot_groups:
                            _shot_groups.append(_c["group"])
                        _cmds.append(_c)
                    elif _c.get("cmd") == "analyze":
                        # 飞书 bot 指令: nms 分析 → 即使无新增告警也强制执行 LLM 分析
                        _force_analyze = True
                    elif _c.get("cmd") == "diag_filters":
                        # 运维诊断: dump filter 元数据 + 实测各 filtertype 途径，写 Redis 供排查
                        # 诊断仅需登录态，无需等采集完成，立即执行
                        try:
                            await self._nms_diag_filters(page)
                        except Exception as e:
                            logger.error(f"[NMS-API] filter 诊断失败: {e}")
                if _shot_groups:
                    group_shots = await self._nms_capture_group_shots(
                        page, content_frame, _shot_groups[:4],
                        severities=_shot_sevs, unack_only=_shot_unack)
                    # bot 命令请求的分组单独推送（bot 交互回复）
                    if _cmds:
                        _wanted = {c.get("group") for c in _cmds}
                        cmd_shots = [s for s in group_shots if s.get("group") in _wanted]
                        if cmd_shots:
                            _shot_lines = "\n".join(
                                f"　[{s['group']}{' · ' + _NMS_SEV_CN.get(s.get('severity'), s.get('severity')) + '告警' if s.get('severity') else ''}视图截图{'（未确认）' if s.get('unack') else ''}]({s['url']})" for s in cmd_shots)
                            await self.notify(
                                f"📸 **NMS 分组告警截图完成**（bot 命令触发）\n"
                                f"**时间**: {datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"{_shot_lines}",
                                topic="nms_snapshot")
                        else:
                            _fail_groups = "、".join(sorted(_wanted))
                            await self.notify(
                                f"⚠️ **NMS 分组告警截图失败**（bot 命令触发）\n"
                                f"分组「{_fail_groups}」截图未成功（浏览器会话异常或分组名不匹配），"
                                f"请检查分组名或稍后重试。",
                                topic="nms_snapshot")

            # SSO keep-alive: 会话身份抓取成功后回写最新 Cookie (延长 Redis 侧
            # 会话有效期; Netcool 服务端若为空闲超时模型, 30 分钟一轮的访问
            # 即可持续续命, 全局监控不再依赖任何持久密码)
            if session_user:
                try:
                    fresh = await page.context.cookies()
                    if fresh:
                        await r.set(
                            f"{config.REDIS_PREFIX}:nms_session:{session_user}",
                            json.dumps(fresh, ensure_ascii=False),
                            ex=NMS_SESSION_TTL)
                        logger.debug(f"[nms_agent] 会话 Cookie 已刷新 "
                                     f"({session_user}, {len(fresh)} 条)")
                except Exception as e:
                    logger.debug(f"[nms_agent] Cookie 刷新失败 (忽略): {e}")

            await page.close()

            # 7. LLM 分析（手动触发优先；NMS 采集本身不调用 LLM）
            # 定时采集轮次默认只写本地规则摘要（零 LLM），LLM 仅在以下情况执行：
            #   ① bot 命令「nms 分析」强制分析 ② 用户主动「立即抓取」③ NMS_AUTO_LLM_ANALYZE 开启且新模式达阈值
            analysis_text = ""
            _manual = _force_analyze or force_analyze
            _new_patterns: Dict[str, int] = {}
            for a in new_alarms:
                _pk = self._nms_pattern_key(a)
                _new_patterns[_pk] = _new_patterns.get(_pk, 0) + 1
            _seen_patterns = set()
            try:
                _sp_raw = await r.get(f"{base}:analysis:seen_patterns")
                if _sp_raw:
                    _seen_patterns = set(json.loads(_sp_raw))
            except Exception:
                pass
            _novel = {k: v for k, v in _new_patterns.items() if k not in _seen_patterns}
            _novel_count = sum(_novel.values())
            _novel_crit = [a for a in new_alarms
                           if self._nms_pattern_key(a) in _novel
                           and a.get("severity") == "critical"]
            _auto_on = bool(config.NMS_AUTO_LLM_ANALYZE)
            # Phase 3.3: 复合门控 (critical 新模式 / 新模式阈值 / 影响面) + 冷却
            _last_auto_ts = None
            try:
                _lat_raw = await r.get(f"{base}:analysis:auto_ts")
                if _lat_raw:
                    _last_auto_ts = float(_lat_raw)
            except Exception:
                pass
            _auto_ok, _auto_reason = self._nms_auto_llm_should_trigger(
                new_alarms, _novel, _seen_patterns, last_auto_ts=_last_auto_ts)
            _auto_do = _auto_ok
            _do_llm = _manual or _auto_do
            if _auto_do:
                logger.info(f"[nms_agent] 自动 LLM 分析触发: {_auto_reason}")
            if not _do_llm and new_alarms:
                logger.info(f"[nms_agent] 定时采集轮次不调用 LLM（手动触发优先，"
                            f"auto={_auto_on}, reason={_auto_reason}），写本地规则摘要")
            if _do_llm:
                _analysis_new, _analysis_all = new_alarms, alarms
                try:
                    _dyn2 = await self._nms_dyn_cfg(r)
                    _push_groups2 = [g.strip() for g in
                                     str(_dyn2.get("push_groups") or "").split(",") if g.strip()]
                    if _push_groups2:
                        _keep2 = set(_push_groups2)
                        _analysis_new = [a for a in new_alarms
                                         if _keep2 & set(self._nms_alarm_groups(a))]
                        _analysis_all = [a for a in alarms
                                         if _keep2 & set(self._nms_alarm_groups(a))]
                        logger.info(f"[nms_agent] LLM 主分析按推送分组过滤: "
                                    f"new {len(_analysis_new)}/{len(new_alarms)}, all {len(_analysis_all)}/{len(alarms)}")
                except Exception:
                    pass
                analysis_text = await self._nms_analyze_alarms(
                    _analysis_new, _analysis_all, dns_tests=tool_checks.get("dns"),
                    base=base, tool_checks=tool_checks)
                # 清理 reasoning 模型输出（deepseek 等会在 content 中包含推理过程）
                analysis_text = self._nms_strip_reasoning(analysis_text)
                # 分析完成后把本轮新模式并入指纹库（下次同模式直接复用）
                try:
                    _merged = _seen_patterns | set(_new_patterns)
                    await r.set(f"{base}:analysis:seen_patterns",
                                json.dumps(sorted(_merged), ensure_ascii=False),
                                ex=config.NMS_LLM_PATTERN_TTL)
                    logger.info(f"[nms_agent] 模式指纹库更新: 新增 {_novel_count} 条新模式，"
                                f"库内共 {len(_merged)} 种")
                except Exception:
                    pass
            elif _auto_on and new_alarms:
                analysis_text = await self._nms_reuse_analysis(
                    new_alarms, _new_patterns, _novel, k_analysis)
            elif new_alarms:
                analysis_text = self._nms_local_summary(new_alarms, alarms)
            if analysis_text:
                analysis_record = {
                    "timestamp": _nms_now_iso(),
                    "new_count": len(new_alarms),
                    "total_count": len(alarms),
                    # Phase 3.3: 标记触发方式, 飞书卡片/前端可区分 (manual=用户主动, auto=门槛触发)
                    "trigger": "manual" if _manual else ("auto" if _auto_do else "local"),
                    "analysis": analysis_text
                }
                await r.set(k_analysis,
                            json.dumps(analysis_record, ensure_ascii=False))
                # Phase 3.3: 自动分析时间戳 (冷却计算用; 手动分析也刷新, 避免手动后紧跟一次冗余自动)
                if _do_llm:
                    try:
                        await r.set(f"{base}:analysis:auto_ts", str(time.time()))
                    except Exception:
                        pass

            # 7.5 分组维度分析（仅手动触发或自动分析开启时执行；采集轮次不调 LLM）
            _do_group = _manual or (_auto_on and new_alarms)
            if _do_group and new_alarms:
                self._nms_round_no += 1
                do_group = _manual or (self._nms_round_no % config.NMS_GROUP_ANALYZE_EVERY == 0) or \
                           (len(new_alarms) >= config.NMS_GROUP_ANALYZE_MIN_NEW)
                if not do_group:
                    logger.info(f"[nms_agent] 分组分析降频跳过 "
                                f"(round={self._nms_round_no}, new={len(new_alarms)}, "
                                f"阈值={config.NMS_GROUP_ANALYZE_MIN_NEW})")
                else:
                    target_groups = await self._nms_analysis_target_groups(r)
                    for g in target_groups:
                        grp_new = [a for a in new_alarms if g in self._nms_alarm_groups(a)]
                        if not grp_new:
                            continue
                        # 同质化门槛（仅自动模式）：组内新增均属已分析模式时跳过 LLM；
                        # 手动触发时直接分析（用户主动要求，不做门槛）
                        if not _manual:
                            _grp_novel = [a for a in grp_new
                                          if self._nms_pattern_key(a) not in _seen_patterns]
                            if len(_grp_novel) < config.NMS_GROUP_ANALYZE_MIN_NEW:
                                logger.info(f"[nms_agent] 分组 {g} 分析同质化跳过 "
                                            f"(组内新增 {len(grp_new)} 条，新模式 {len(_grp_novel)} "
                                            f"< {config.NMS_GROUP_ANALYZE_MIN_NEW})")
                                continue
                        try:
                            _g0 = time.time()
                            # Phase 1.1: scheduled 路径走 scheduled 锁,
                            # 进入前会自动让出 pending 的 on_demand (用户请求优先)
                            await self._nms_compute_group_analysis(
                                r, base, g, alarms, new_alarms, kind="scheduled")
                            await self._record_sched_exec(
                                "nms_group_analysis", _g0, True,
                                summary=f"分组 {g} 分析完成 (new={len(grp_new)})")
                            # 组内新模式并入指纹库（避免主分析未触发时同模式重复分组分析）
                            try:
                                _merged_g = _seen_patterns | {
                                    self._nms_pattern_key(a) for a in grp_new}
                                await r.set(f"{base}:analysis:seen_patterns",
                                            json.dumps(sorted(_merged_g), ensure_ascii=False),
                                            ex=config.NMS_LLM_PATTERN_TTL)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"[nms_agent] 分组 {g} 分析预算失败: {e}")
                            await self._record_sched_exec(
                                "nms_group_analysis", _g0, False, err=str(e),
                                summary=f"分组 {g}")

            # 8. 推送飞书通知（仅全局 svc-opscore 抓取推送，per-user 不推，避免刷屏）
            if not is_personal:
                await self._nms_notify_feishu(alarms, new_alarms, analysis_text,
                                              incidents=incidents,
                                              group_shots=group_shots,
                                              force=_force_analyze)

            # 9. 告警驱动的自动化检查工作流（仅全局监控 + 开关开启 + 有新告警）
            if not is_personal and new_alarms and config.NMS_AUTO_CHECK_ENABLED:
                await self._nms_trigger_workflow(new_alarms)
                # A2: 同时确定性分派结构化动作 (ping / backup / vm) 给 orchestrator
                try:
                    await self._nms_auto_remediate(new_alarms, base=base)
                except Exception as e:
                    logger.error(f"[nms_agent] 自动处置分派失败: {e}")

            logger.info(f"[nms_agent] NMS 完成: total={len(alarms)}, "
                        f"new={len(new_alarms)}")

        except Exception as e:
            logger.error(f"[nms_agent] NMS 抓取失败: {e}")
            try:
                await page.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _nms_alarm_groups(a):
        """单条告警的分组归属列表（groups[] 优先，回退到单 group）"""
        gs = a.get("groups")
        if gs:
            return gs
        single = a.get("group")
        return [single] if single else ["未分组"]

    @staticmethod
    def _nms_pattern_key(alarm) -> str:
        """告警模式指纹：来源 + 规范化摘要（IP/接口/BGP Peer 等变量归一为占位符）。
        同质化告警（同一模式反复出现，仅变量不同）指纹相同，用于分析结论复用，
        避免同样模式每轮重复调 LLM。"""
        src = (alarm.get("source") or "").strip()
        summ = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]", "",
                      alarm.get("summary") or "")
        # IP 地址 → 占位符
        summ = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}", "<ip>", summ)
        # 括号内变量（BGP Peer / interface / host 等）→ 占位符
        summ = re.sub(r"\(\s*BGP Peer:[^)]*\)", "(BGP Peer: <var>)", summ)
        summ = re.sub(r"\(\s*[^)]*(?:peer|interface|host|port|device|ifname|node)[^)]*\)",
                      "(<var>)", summ, flags=re.I)
        return f"{src}|{summ[:80]}"

    async def _nms_reuse_analysis(self, new_alarms, new_patterns, novel,
                                  k_analysis: str) -> str:
        """同质化告警复用上次分析结论（不调 LLM）。
        输出头部说明复用原因与本轮增量，正文附上次完整分析报告。"""
        _prev_text = ""
        try:
            _r2 = await self.get_redis()
            _prev_raw = await _r2.get(k_analysis)
            if _prev_raw:
                _prev = json.loads(_prev_raw)
                _prev_text = (_prev.get("analysis") or "").strip()
        except Exception:
            pass
        _kind_names = sorted(new_patterns, key=lambda k: -new_patterns[k])[:5]
        _kind_lines = "、".join(
            f"「{k.split('|')[-1][:48]}」×{new_patterns[k]}" for k in _kind_names)
        _head = (f"> **同质化复用**：本轮新增 {len(new_alarms)} 条告警（{len(new_patterns)} 种模式），"
                 f"与上次分析模式一致，直接复用上次分析结论（未重复调用 LLM）。\n"
                 f"> 本轮模式分布：{_kind_lines}\n")
        if novel:
            _head += (f"> 另发现 {sum(novel.values())} 条新模式告警（未达分析阈值 "
                      f"{config.NMS_LLM_NEW_PATTERN_MIN}，待累计后触发分析）。\n")
        if _prev_text:
            return _head + "\n---\n\n" + _prev_text
        return _head + "\n（暂无历史分析报告，下一轮新模式达到阈值时将生成完整分析。）"

    async def _nms_analysis_target_groups(self, r):
        """后台预算分析的目标分组：各用户组 NMS 分组映射(KEY_GROUP_NMS)之并集，
        都为空时回退到 config.NMS_DEFAULT_GROUPS；最多取 NMS_ANALYSIS_MAX_GROUPS 个。"""
        groups = set()
        try:
            mapping = await r.hgetall(config.KEY_GROUP_NMS)
            for _grp, raw in (mapping or {}).items():
                try:
                    for g in json.loads(raw):
                        if g:
                            groups.add(g)
                except Exception:
                    continue
        except Exception:
            pass
        if not groups:
            groups = set(config.NMS_DEFAULT_GROUPS)
        return list(groups)[:config.NMS_ANALYSIS_MAX_GROUPS]

    async def _nms_compute_group_analysis(self, r, base, group,
                                          all_alarms, new_alarms=None,
                                          force: bool = False, kind: str = "on_demand"):
        """计算并存储单个分组的 LLM 分析到 {base}:analysis:group:{group}。

        Phase 1.2 — TTL 缓存:
          - 默认检查既有分析是否在 ``NMS_ANALYSIS_TTL`` 内; 命中则跳过 LLM 重算。
          - ``force=True`` 时强制重算 (用户点击"生成分析"或传 ?fresh=1)。
        Phase 1.1 — 锁拆分:
          - ``kind="on_demand"`` 走用户优先锁;
          - ``kind="scheduled"`` 走后台批量锁, 进入前会先让出 pending 的 on_demand。
        """
        grp_alarms = [a for a in all_alarms if group in self._nms_alarm_groups(a)]
        if not grp_alarms:
            logger.info(f"[nms_agent] 分组分析跳过（无告警）: {group}")
            return
        # TTL 命中检查: 不强制且既有分析仍新鲜则直接复用
        if not force:
            try:
                existing_raw = await r.get(f"{base}:analysis:group:{group}")
                if existing_raw:
                    existing = json.loads(existing_raw)
                    if self._nms_analysis_is_fresh(existing):
                        logger.info(f"[nms_agent] 分组 {group} 分析命中 TTL 缓存 "
                                    f"(age<={getattr(config, 'NMS_ANALYSIS_TTL', 480)}s)，跳过 LLM")
                        return
            except Exception:
                pass
        grp_new = [a for a in (new_alarms or []) if group in self._nms_alarm_groups(a)]
        # 无新增时以活跃告警作为分析上下文，保证输出聚焦当前问题
        # Phase 4: 传 base 注入事件簇/历史原料 (分组分析无本轮 tool_checks, 略)
        text = await self._nms_analyze_alarms(grp_new or grp_alarms, grp_alarms,
                                              kind=kind, base=base)
        text = self._nms_strip_reasoning(text)
        _active = {"critical", "major", "minor", "warning", "indeterminate"}
        rec = {
            "timestamp": _nms_now_iso(),
            "group": group,
            "new_count": len(grp_new),
            "total_count": len(grp_alarms),
            "active_count": sum(1 for a in grp_alarms if a.get("severity") in _active),
            "analysis": text,
        }
        await r.set(f"{base}:analysis:group:{group}",
                    json.dumps(rec, ensure_ascii=False))
        logger.info(f"[nms_agent] 分组分析已更新: {group} "
                    f"(total={len(grp_alarms)}, new={len(grp_new)}, kind={kind})")

    async def _nms_analyze_group(self, msg: AgentMessage):
        """按需计算某分组分析（不重新抓取，直接基于当前快照）。

        Phase 1.2: 接受 ``params.force`` / ``params.fresh`` 强制跳过 TTL 缓存。
        """
        params = msg.params or {}
        group = (params.get("group") or "").strip()
        if not group:
            return
        force = bool(params.get("force") or params.get("fresh"))
        nms_username = params.get("nms_username")
        r = await self.get_redis()
        if nms_username and nms_username != config.NMS_USERNAME:
            base = f"{config.REDIS_PREFIX}:nms_alarms:user:{nms_username}"
        else:
            base = f"{config.REDIS_PREFIX}:nms_alarms"
        latest_raw = await r.get(f"{base}:latest")
        if not latest_raw and base != f"{config.REDIS_PREFIX}:nms_alarms":
            base = f"{config.REDIS_PREFIX}:nms_alarms"
            latest_raw = await r.get(f"{base}:latest")
        if not latest_raw:
            logger.warning(f"[nms_agent] 按需分组分析：无快照 group={group}")
            return
        snapshot = json.loads(latest_raw)
        alarms = snapshot.get("alarms", [])
        logger.info(f"[nms_agent] 按需触发分组分析: {group} (force={force})")
        await self._nms_compute_group_analysis(r, base, group, alarms, None,
                                               force=force, kind="on_demand")

    # ────────────────────────────────────────────────────────────────────────
    # Phase 1.3: SSE 流式分组分析
    # 通过 Redis pubsub (`zops:nms:analyze:stream:{stream_id}`) 把 LLM 增量
    # chunk 推给 /api/nms/analyze/stream 端点,再由端点以 SSE 形态回给前端。
    # ────────────────────────────────────────────────────────────────────────
    NMS_STREAM_CHANNEL = f"{config.REDIS_PREFIX}:nms:analyze:stream"

    @staticmethod
    def _nms_stream_channel(stream_id: str) -> str:
        return f"{NmsAgent.NMS_STREAM_CHANNEL}:{stream_id}"

    async def _nms_publish_stream_event(self, r, stream_id: str, event: dict):
        """发布一个流式分析事件 (queued/running/token/done/error)。"""
        if not stream_id:
            return
        try:
            await r.publish(self._nms_stream_channel(stream_id),
                            json.dumps(event, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[nms_agent] 发布 stream 事件失败: {e}")

    async def _nms_analyze_stream(self, msg: AgentMessage):
        """SSE 流式分组分析: 边生成边把增量 token 通过 Redis 推给 API 端点。

        与 ``_nms_compute_group_analysis`` 的差异:
          - 总是 force=True (用户主动连接,期望看到新结果)
          - LLM 调用使用 ``on_delta`` 回调,把每个 token chunk 即时广播
          - 完成后写 Redis 分析快照,与非流式路径兼容
        """
        params = msg.params or {}
        group = (params.get("group") or "").strip()
        stream_id = (params.get("stream_id") or "").strip()
        if not group or not stream_id:
            return
        r = await self.get_redis()
        nms_username = params.get("nms_username")
        if nms_username and nms_username != config.NMS_USERNAME:
            base = f"{config.REDIS_PREFIX}:nms_alarms:user:{nms_username}"
        else:
            base = f"{config.REDIS_PREFIX}:nms_alarms"
        await self._nms_publish_stream_event(
            r, stream_id,
            {"type": "queued", "group": group, "ts": time.time()})

        latest_raw = await r.get(f"{base}:latest")
        if not latest_raw and base != f"{config.REDIS_PREFIX}:nms_alarms":
            base = f"{config.REDIS_PREFIX}:nms_alarms"
            latest_raw = await r.get(f"{base}:latest")
        if not latest_raw:
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "error", "group": group,
                 "message": "暂无告警快照,请先点击「立即抓取」"})
            return
        snapshot = json.loads(latest_raw)
        all_alarms = snapshot.get("alarms", [])
        grp_alarms = [a for a in all_alarms if group in self._nms_alarm_groups(a)]
        if not grp_alarms:
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "error", "group": group,
                 "message": f"分组「{group}」暂无告警"})
            return
        new_alarms = [a for a in all_alarms if a.get("_is_new")]
        grp_new = [a for a in (new_alarms or []) if group in self._nms_alarm_groups(a)]

        await self._nms_publish_stream_event(
            r, stream_id,
            {"type": "running", "group": group,
             "total": len(grp_alarms), "new": len(grp_new),
             "ts": time.time()})

        # 构造与 _nms_analyze_alarms 等价的 prompt
        # 复用其私有清洗 / checklist / 确定性 / KB / DNS 富化逻辑
        # Phase 4: 传 base 注入事件簇/历史原料; 快照里带 tool_checks 时一并注入
        _tc = None
        try:
            _tc = snapshot.get("tool_checks") or None
        except Exception:
            pass
        prompt, _nms_sys, _need_kb = await self._nms_build_analyze_prompt(
            grp_new or grp_alarms, grp_alarms, kind="on_demand",
            base=base, tool_checks=_tc)

        if not await self._nms_acquire_llm_bucket(timeout=20):
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "error", "group": group,
                 "message": "LLM 调用限流中,请稍后再试"})
            return

        result_chunks: list = []
        reasoning_chunks: list = []

        async def _on_delta(seg: str):
            result_chunks.append(seg)
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "token", "delta": seg, "ts": time.time()})

        async def _on_reasoning(seg: str):
            reasoning_chunks.append(seg)
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "reasoning", "delta": seg, "ts": time.time()})

        async def _on_wait(elapsed: int):
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "wait", "elapsed": elapsed, "ts": time.time()})

        result = ""
        try:
            async with self._nms_acquire_llm_lock("on_demand") as got_lock:
                if not got_lock:
                    await self._nms_publish_stream_event(
                        r, stream_id,
                        {"type": "error", "group": group,
                         "message": "scheduled 占用锁, 请重试"})
                    return
                result = await self.llm_chat(
                    prompt=prompt, system=_nms_sys,
                    max_tokens=config.NMS_LLM_MAX_TOKENS,
                    on_delta=_on_delta, on_reasoning=_on_reasoning,
                    on_wait=_on_wait)
        except Exception as e:
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "error", "group": group,
                 "message": f"LLM 调用失败: {type(e).__name__}: {e}"})
            return

        # 与非流式路径一致: 不可用或缺核心章节 → 本地摘要
        result = self._nms_strip_reasoning(result or "")
        if not result or self._nms_llm_unusable(result) or not self._nms_has_core_sections(result):
            local = self._nms_local_summary(grp_new or grp_alarms, grp_alarms)
            result = local
            await self._nms_publish_stream_event(
                r, stream_id,
                {"type": "fallback", "message": "改用本地摘要",
                 "ts": time.time()})

        # 写快照 (与非流式一致, 供前端 /api/nms/analysis 后续读取)
        _active = {"critical", "major", "minor", "warning", "indeterminate"}
        rec = {
            "timestamp": _nms_now_iso(),
            "group": group,
            "new_count": len(grp_new),
            "total_count": len(grp_alarms),
            "active_count": sum(1 for a in grp_alarms if a.get("severity") in _active),
            "analysis": result,
        }
        await r.set(f"{base}:analysis:group:{group}",
                    json.dumps(rec, ensure_ascii=False))
        await self._nms_publish_stream_event(
            r, stream_id,
            {"type": "done", "group": group,
             "analysis": result, "timestamp": rec["timestamp"],
             "ts": time.time()})
        logger.info(f"[nms_agent] 流式分组分析完成: {group} "
                    f"(chunks={len(result_chunks)}, "
                    f"reasoning={len(reasoning_chunks)})")

    async def _nms_login(self, page, nms_username: str = None, nms_password: str = None):
        """登录 NMS 控制台 — 委托模块级 _nms_do_login (与 SSO 引导共用同一实现)。"""
        username = nms_username or config.NMS_USERNAME
        password = nms_password or config.NMS_PASSWORD
        return await _nms_do_login(page, username, password)


    async def _nms_get_content_frame(self, page):
        """等待并返回包含告警表格 + 分组过滤下拉框的 contentRender iframe"""
        for _ in range(12):
            await asyncio.sleep(5)
            for frame in page.frames:
                try:
                    if "contentRender" in frame.url and \
                            await frame.query_selector("input[id*='filterDropDown']"):
                        return frame
                except Exception:
                    continue
        return None

    # ==============================================================
    # 通过 IBM Netcool WebGUI EventDataService JSON API 采集告警
    # 替代虚拟表格滚动：无 2000 行 cap、Serial 精确去重、全字段、快数十倍
    # ==============================================================
    def _nms_api_url(self, name, ftype, view, grouping="false"):
        # 构造 EventDataService 请求 URL
        from urllib.parse import quote
        root = config.NMS_CONSOLE_URL.split("/ibm/console")[0].rstrip("/")
        return (root + "/ibm/console/webtop/EventDataService?format=jsonstream&filtername=" + quote(str(name)) +
                "&filtertype=" + quote(str(ftype)) + "&datasources=NCOMS_V&viewname=" + quote(str(view or "Default")) +
                "&viewtype=global&timestamp=&top-nCompatible=true&rowidconsumer=true&commands=false" +
                "&applyrelationships=false&applygroupings=" + grouping + "&timezone=Asia%2FChungking")

    async def _nms_fetch_filters_meta(self, page):
        # 从 OMNIbusWebGUI REST 数据集读取全部 filter 的 NAME/TYPE/VIEWNAME/SQL
        root = config.NMS_CONSOLE_URL.split("/ibm/console")[0].rstrip("/")
        url = root + "/ibm/tivoli/rest/providers/OMNIbusWebGUI/datasources/any/datasets/filters/items?properties=all&optimize=true"
        try:
            resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
            obj = json.loads(await resp.text())
        except Exception as e:
            logger.error(f"[NMS-API] 读取 filter 列表失败: {e}")
            return []
        out = []
        for it in (obj.get("items") or []):
            d = {}
            for p in (it.get("properties") or []):
                if isinstance(p, dict) and "id" in p:
                    d[p["id"]] = p.get("value")
            name = d.get("NAME")
            if not name:
                continue
            out.append({
                "name": name,
                "type": d.get("TYPE") or "global",
                "view": d.get("VIEWNAME") or "Default",
                "sql": (d.get("SQL") or "").strip(),
            })
        logger.info(f"[NMS-API] 读取到 {len(out)} 个 filter")
        return out

    async def _nms_diag_filters(self, page):
        """filter 诊断：全量元数据 dump + group 型 filter 各 filtertype 实测 + OMNIbus REST 探测。
        结果写 Redis <prefix>:nms:filter_diag（TTL 2h），供排查分组缺失问题
        （如 GNOC_use 是否存在于平台、CN_only 等 group 型 EventDataService 500）。"""
        import base64 as _b64
        from urllib.parse import quote as _q
        root = config.NMS_CONSOLE_URL.split("/ibm/console")[0].rstrip("/")
        url = root + "/ibm/tivoli/rest/providers/OMNIbusWebGUI/datasources/any/datasets/filters/items?properties=all&optimize=true"
        diag = {"time": datetime.now(TZ_CN).strftime("%F %T"), "filters": [],
                "probe": {}, "omnibus_rest": {}}
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        if resp.status != 200:
            diag["error"] = f"filters API status {resp.status}"
            await (await self.get_redis()).set(
                f"{config.REDIS_PREFIX}:nms:filter_diag",
                json.dumps(diag, ensure_ascii=False), ex=7200)
            return
        obj = json.loads(await resp.text())
        all_names = []
        for it in (obj.get("items") or []):
            d = {}
            for p in (it.get("properties") or []):
                if isinstance(p, dict) and "id" in p:
                    d[p["id"]] = p.get("value")
            name = d.get("NAME")
            if not name:
                continue
            all_names.append(name)
            diag["filters"].append({
                "name": name, "type": d.get("TYPE") or "global",
                "view": d.get("VIEWNAME") or "Default",
                "sql": (d.get("SQL") or "").strip()[:400],
                "extra": {k: str(v)[:80] for k, v in d.items()
                          if k not in ("NAME", "TYPE", "VIEWNAME", "SQL")
                          and v not in (None, "")},
            })
        diag["total"] = len(all_names)
        diag["gnoc_use_exists"] = "GNOC_use" in all_names
        # 事件服务 probe：group 型 + 采集缺失名单 逐个试 filtertype 变体
        targets = [f for f in diag["filters"] if (f["type"] or "global") != "global"]
        extra_missing = ("CN_only", "Trunk_Event_Alarm", "trunk_Event", "Phase2",
                         "GNOC_need_confirm_Switch", "Cloud_Mon", "TEMP_BY_LUKE_GROUP")
        targets += [f for f in diag["filters"] if f["name"] in extra_missing]
        seen_t = set()
        for f in targets:
            if f["name"] in seen_t:
                continue
            seen_t.add(f["name"])
            res = {}
            for ft in ("group", "global", "user", "system"):
                u = self._nms_api_url(f["name"], ft, f["view"] or "Default", "false")
                try:
                    rp = await page.request.get(u, headers={"Accept-Encoding": "identity"})
                    m = re.search(r'"identifierList":"([^"]*)"', (await rp.text())[:60000])
                    res[ft] = {"status": rp.status,
                               "serials": len([x for x in m.group(1).split(",") if x.strip()]) if m else 0}
                except Exception as e:
                    res[ft] = {"error": str(e)[:120]}
            diag["probe"][f["name"]] = {"type": f["type"], "attempts": res}
        # OMNIbus REST 探测（group 型 filter 的候选通道：直接执行 filter SQL）
        try:
            cred = _b64.b64encode(
                f"{config.NMS_USERNAME}:{config.NMS_PASSWORD}".encode()).decode()
            rest_root = root + "/objectserver/restapi/alerts/status"
            rp = await page.request.get(rest_root + "?collist=Serial&limit=3", headers={
                "Accept-Encoding": "identity", "Authorization": "Basic " + cred})
            diag["omnibus_rest"]["list_status"] = rp.status
            diag["omnibus_rest"]["body"] = (await rp.text())[:400]
            if rp.status == 200:
                for f in list(seen_t)[:2]:
                    _sql = ""
                    for tf in diag["filters"]:
                        if tf["name"] == f:
                            _sql = (tf["sql"] or "").replace('"', "'").replace("\n", " ")
                            break
                    q = rest_root + "?collist=Serial&limit=5" + ("&filter=" + _q(_sql) if _sql else "")
                    rp2 = await page.request.get(q, headers={
                        "Accept-Encoding": "identity", "Authorization": "Basic " + cred})
                    diag["omnibus_rest"][f] = {"status": rp2.status,
                                               "body": (await rp2.text())[:300]}
        except Exception as e:
            diag["omnibus_rest"]["error"] = str(e)[:200]
        try:
            r = await self.get_redis()
            await r.set(f"{config.REDIS_PREFIX}:nms:filter_diag",
                        json.dumps(diag, ensure_ascii=False), ex=7200)
        except Exception:
            pass
        logger.info(f"[NMS-API] filter 诊断完成: total={diag.get('total')}, "
                    f"GNOC_use 存在={diag.get('gnoc_use_exists')}, "
                    f"probe={len(diag.get('probe', {}))} 个")

    def _nms_row_to_alarm(self, fields, row, serial):
        # 按列名把行数组转为告警 dict（列集因 view 而异，必须按名映射）
        d = {}
        for i, nm in enumerate(fields):
            if i < len(row):
                d[nm] = row[i]

        def gv(*names):
            # 多别名取第一个非空列值
            for nm in names:
                v = d.get(nm)
                if v is not None and str(v) != "":
                    return str(v)
            return ""

        summary = self._nms_clean_summary(str(d.get("Summary") or ""))[:300]
        source = gv("Node", "NodeAlias")
        sev_raw = d.get("Severity")
        sev = self._nms_map_severity(str(sev_raw) if sev_raw is not None else "")
        t_last = gv("LastOccurrence")
        t_first = gv("FirstOccurrence")
        t = t_last or t_first
        # 全量非空字段：供前端「全部字段」展示，保证不遗漏任何列（含 caseid/eventkey/eventgroup 等自定义列）
        all_fields = {k: (str(v) if v is not None else "") for k, v in d.items()
                      if v is not None and str(v) != ""}
        return {
            "alarm_id": "srl:" + str(serial),
            "event_id": str(serial),
            "serial": str(serial),
            "group": "",
            "view_group": "",
            "groups": [],
            "severity": sev,
            "summary": summary,
            "source": source,
            "node": gv("Node"),
            "node_alias": gv("NodeAlias"),
            "time": t,
            "first_time": t_first,
            "last_time": t_last,
            "status": gv("Acknowledged"),
            "acknowledged": gv("Acknowledged"),
            "alarm_type": gv("AlertGroup"),
            "alert_key": gv("AlertKey"),
            "event_key": gv("EventKey", "AlertKey", "Identifier"),
            "event_group": gv("EventGroup", "AlertGroup"),
            "case_id": gv("Hpdcaseid", "HpdCaseId", "HPDCASEID", "CaseId", "CaseID", "CASE_ID", "Case_Id", "TicketId", "IncidentId", "Case"),
            "state_change": gv("StateChange"),
            "create_time": gv("AlarmCreateTime"),
            "flash": gv("Flash"),
            "owner_gid": gv("OwnerGID"),
            "manager": gv("Manager"),
            "agent": gv("Agent"),
            "identifier": gv("Identifier"),
            "aclass": gv("Class"),
            "atype": gv("Type"),
            "grade": gv("Grade"),
            "location": gv("Location"),
            "owner": gv("OwnerUID"),
            "count": gv("Tally", "Count"),
            "expire_time": gv("ExpireTime"),
            "server_name": gv("ServerName"),
            "server_serial": gv("ServerSerial"),
            "raw": summary,
            "fields": all_fields,
        }

    async def _nms_api_get_full(self, page, f):
        # 抓一个 filter 的全量行 + 列定义 + Serial 列表（用于主集）
        url = self._nms_api_url(f["name"], f["type"], f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        if resp.status != 200:
            raise RuntimeError(f"status {resp.status}")
        obj = json.loads(await resp.text())
        ds0 = obj["header"]["datasource"][0]
        fields = [x.get("name") for x in ds0.get("field", [])]
        rows = obj["rows"][0] if obj.get("rows") else []
        serials = [x for x in str(ds0.get("identifierList") or "").split(",") if x.strip()]
        return fields, rows, serials

    async def _nms_api_filter_rows(self, page, f, expect_total: int = 0):
        # 抓 filter 的完整行 + 列定义，返回 (fields, rows, fell_back)
        # scope 策略与 _nms_api_serials 一致：遍历回退链，剔除 ≥99% 主集的假全量，
        # 取行数最多的真实结果（部分 group 型 filter 仅 system scope 提供真实成员集）
        hdr = {"Accept-Encoding": "identity"}
        orig_type = f.get("type") or "global"
        types = [orig_type]
        for _ft in ("global", "system", "user"):
            if _ft not in types:
                types.append(_ft)
        last = None
        best = None
        saw_full = False
        for ft in types:
            url = self._nms_api_url(f["name"], ft, f["view"], "false")
            try:
                resp = await page.request.get(url, headers=hdr)
            except Exception as e:
                last = e
                continue
            if resp.status != 200:
                last = RuntimeError(f"status {resp.status}")
                continue
            obj = json.loads(await resp.text())
            ds0 = obj["header"]["datasource"][0]
            fields = [x.get("name") for x in ds0.get("field", [])]
            rows = obj["rows"][0] if obj.get("rows") else []
            if expect_total and len(rows) >= expect_total * 0.99:
                saw_full = True
                continue
            if best is None or len(rows) > len(best[1]):
                best = (fields, rows, ft != orig_type)
        if best is not None:
            return best
        if saw_full:
            # 全部 scope 都返全量：真实成员集不可得
            return [], [], True
        raise last or RuntimeError("all attempts failed")

    @staticmethod
    def _nms_row_content_key(fields, row):
        # 行内容键 (Node|NodeAlias, EventKey|AlertKey|Identifier, Summary)：跨视图稳定
        # NMS 服务端 Serial 会换手（事件重插重新分配）且 identifierList 与 rows 可能错位，
        # 归属匹配必须用内容键而非串行，否则串行换手窗口期会把别家事件打上本 filter 标签
        d = {}
        for i, nm in enumerate(fields):
            if i < len(row):
                d[nm] = row[i]

        def gv(*names):
            for nm in names:
                v = d.get(nm)
                if v is not None and str(v) != "":
                    return str(v)
            return ""

        return (gv("Node", "NodeAlias"), gv("EventKey", "AlertKey", "Identifier"),
                gv("Summary"))

    async def _nms_api_serials(self, page, f, expect_total: int = 0):
        # 只取 identifierList（Serial 集合）用于成员归属，正则抽取避免解析巨大行数组
        # group 型 filter 的 scope 行为不稳定：group scope 常 500 或静默忽略过滤返全量，
        # 部分仅 system scope 提供真实成员集（诊断实测 TEMP_BY_LUKE_GROUP=125 条）。
        # 策略：遍历 scope 回退链，剔除 ≥99% 主集的假全量，取成员数最多的真实结果
        import re as _re
        hdr = {"Accept-Encoding": "identity"}
        orig_type = f.get("type") or "global"
        types = [orig_type]
        for _ft in ("global", "system", "user"):
            if _ft not in types:
                types.append(_ft)
        last = None
        best = None  # (serials, fell_back)
        saw_full = False
        for ft in types:
            url = self._nms_api_url(f["name"], ft, f["view"], "false")
            try:
                resp = await page.request.get(url, headers=hdr)
            except Exception as e:
                last = e
                continue
            if resp.status != 200:
                last = RuntimeError(f"status {resp.status}")
                continue
            body = await resp.text()
            m = _re.search(r'"identifierList":"([^"]*)"', body)
            if not m:
                return [], ft != orig_type
            serials = [x for x in m.group(1).split(",") if x.strip()]
            # 近全量 = 服务端忽略了过滤条件，记录后换下一 scope 找真实成员集
            if expect_total and len(serials) >= expect_total * 0.99:
                saw_full = True
                continue
            if best is None or len(serials) > len(best[0]):
                best = (serials, ft != orig_type)
        if best is not None:
            return best
        if saw_full:
            return [], True
        raise last or RuntimeError("all attempts failed")

    async def _nms_collect_via_api(self, page):
        # 主流程：AllEvents 全量为主集 + 各 filter identifierList 累积 groups[]
        # 采集质量（内部诊断，供日志/排障）：区分成功读取、错误、行身份不可靠与回退来源
        quality = {"status": "ok", "error": "", "row_count": 0, "unique": 0,
                   "identity_unreliable": 0, "fallback_source": "",
                   "observed_at": _nms_now_iso()}
        self._nms_collect_quality = quality
        filters = await self._nms_fetch_filters_meta(page)
        if not filters:
            quality.update(status="error", error="filter 元数据为空")
            return [], {}, 0, []
        # 补充 system scope filter：TYPE=system 的 filter 不在默认元数据列表返回
        # （如 GNOC_use，owner=system），按配置清单补入，归属流程按 filtertype=system 查询
        _have = {f["name"] for f in filters}
        for _extra in config.NMS_EXTRA_SYSTEM_FILTERS:
            if _extra not in _have:
                filters.append({"name": _extra, "type": "system",
                                "view": "Default", "sql": ""})
        empty_sql = [f for f in filters if not f["sql"].strip('"').strip()]
        master_f = None
        for f in filters:
            if f["name"] == "AllEvents":
                master_f = f
                break
        if master_f is None and empty_sql:
            master_f = empty_sql[0]
        if master_f is None:
            master_f = filters[0]
        # 主集用 Default 扁平视图抓全量事件
        mf = dict(master_f)
        mf["view"] = "Default"
        try:
            fields, rows, serials = await self._nms_api_get_full(page, mf)
        except Exception as e:
            logger.error(f"[NMS-API] 主集 {mf['name']} 抓取失败: {e}")
            quality.update(status="error", error=f"主集抓取失败: {e}"[:200])
            return [], {}, 0, []
        merged = {}
        master_idx = {}
        # 优先用行内 Serial 列配对（identifierList 与 rows 可能错位）；缺失时退回 identifierList
        serial_idx = fields.index("Serial") if "Serial" in fields else -1
        n = min(len(rows), len(serials))
        quality["row_count"] = len(rows)
        quality["fallback_source"] = "row_serial" if serial_idx >= 0 else "identifier_list"
        for i in range(n):
            if serial_idx >= 0 and serial_idx < len(rows[i]) and str(rows[i][serial_idx]) != "":
                srl = str(rows[i][serial_idx])
                serial_source = "row"
            else:
                # 行内 Serial 缺失 → 退回 identifierList：身份不可靠计数，清除证据不采用
                srl = serials[i]
                serial_source = "identifier_list"
                quality["identity_unreliable"] += 1
            if srl in merged:
                continue
            alarm = self._nms_row_to_alarm(fields, rows[i], srl)
            alarm["serial_source"] = serial_source  # 供清除证据校验：行内 Serial 才可信
            merged[srl] = alarm
            key = self._nms_row_content_key(fields, rows[i])
            if key[0] or key[1] or key[2]:
                master_idx.setdefault(key, []).append(srl)
        logger.info(f"[NMS-API] 主集 {mf['name']}: {len(merged)} 条唯一告警 (rows={len(rows)}, serials={len(serials)}, content_keys={len(master_idx)})")
        empty_names = set(f["name"] for f in empty_sql)
        group_counts = {}
        raw_count = len(merged)
        done = 0
        for f in filters:
            name = f["name"]
            if name in empty_names and name == master_f["name"]:
                # 主集（AllEvents 类 catch-all）SQL 为空：匹配全部，直接归属
                for a in merged.values():
                    if name not in a["groups"]:
                        a["groups"].append(name)
                group_counts[name] = len(merged)
                continue
            # 其余 filter（含空 SQL 的 group 容器型，如 TEMP_BY_LUKE_GROUP/Cloud_Mon）
            # 均走下方正常成员归属流程：scope 回退链 global/system/user 尝试取真实成员集
            # （诊断实测部分空 SQL group 型仅 system scope 可查），假全量剔除 + 99% 守卫兜底
            try:
                serials, fell_back = await self._nms_api_serials(page, f, expect_total=len(merged))
            except Exception as e:
                logger.warning(f"[NMS-API] filter {name} 抓取失败: {e}")
                continue
            if not serials:
                # 所有 scope 均假全量或无数据：真实成员集不可得
                logger.info(f"[NMS-API] filter {name} 无可用成员集（假全量或空），跳过归属")
                continue
            matched = [srl for srl in serials if srl in merged]
            # 服务端对部分 filter（如 group 型、或 SQL 引用失效字段）会静默忽略过滤条件直接返回全量事件；
            # 主集与 filter 抓取有时差，用命中率≥99%（而非严格相等）判定假归属并丢弃
            # （如 GNOC_need_confirm_Switch/Cloud_Mon 全量假分组；实际最宽的真 filter Unacknowledged 约 94%）
            if len(matched) >= len(merged) * 0.99:
                logger.warning(f"[NMS-API] filter {name} 命中近全量({len(matched)}/{len(merged)}, fallback={fell_back})，判定过滤条件未生效，跳过归属")
                continue
            # 通过 99% 守卫的 filter 抓完整行，用内容键归属：串行换手/identifierList 错位会产生假归属，
            # 内容键 (Node,EventKey,Summary) 跨视图稳定，杜绝 BGP 串行被打 OPSCORE_use 之类的污染
            try:
                f_fields, f_rows, f_fell_back = await self._nms_api_filter_rows(page, f, expect_total=len(merged))
            except Exception as e:
                logger.warning(f"[NMS-API] filter {name} 行抓取失败: {e}")
                continue
            f_keys = set()
            for r in f_rows:
                k = self._nms_row_content_key(f_fields, r)
                if k[0] or k[1] or k[2]:
                    f_keys.add(k)
            matched_srls = []
            for k in f_keys:
                matched_srls.extend(master_idx.get(k, []))
            if f_fell_back and len(matched_srls) >= len(merged) * 0.99:
                # 回退请求可能被服务端静默忽略过滤条件直接返全量，二次守卫丢弃
                logger.warning(f"[NMS-API] filter {name} 回退请求内容命中近全量({len(matched_srls)}/{len(merged)})，判定过滤条件未生效，跳过归属")
                continue
            cnt = 0
            for srl in matched_srls:
                a = merged.get(srl)
                if a is None:
                    continue
                if name not in a["groups"]:
                    a["groups"].append(name)
                cnt += 1
            group_counts[name] = cnt
            raw_count += len(f_rows)
            done += 1
        logger.info(f"[NMS-API] 成员归属完成: {done} 个 filter, 累积 raw={raw_count}")
        return list(merged.values()), group_counts, raw_count, fields
    async def _nms_select_filter(self, frame, filter_name: str) -> Dict:
        """切换告警视图分组（IBM CustomFilteringSelect，通过 dojo/store 精确匹配）"""
        try:
            return await frame.evaluate(_NMS_SELECT_FILTER_JS, filter_name)
        except Exception as e:
            return {"ok": False, "err": str(e)}

    async def _nms_capture_group_shots(self, page, content_frame, groups: List[str],
                                      severities: List[str] = None,
                                      unack_only: bool = False) -> List[Dict]:
        """对指定分组切换告警视图并截图（browser tool）。
        severities: 分级筛选列表（如 ['critical','major','minor']），每个分组除整组图外
        对每个级别单独勾选筛选后截图；为空仅截整组图。
        unack_only: 仅截未确认告警（Build Quick Filter 叠加 Acknowledged Equals 0 快速过滤），
        所有截图（整组+分级）均生效。
        写入 webchat_frontend/nms_shots/ 供 /static 访问，返回 [{group, severity, unack, file, url}]（severity 为空=整组图）。"""
        shots: List[Dict] = []
        sevs = [s for s in (severities or []) if s in _NMS_SEV_TITLES]
        shot_dir = config.BASE_DIR / "webchat_frontend" / "nms_shots"
        try:
            shot_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        for g in groups:
            _ack_ok = False
            # ① 整组视图：按配置级别勾选（排除 warning/indeterminate 低级别，
            # 避免蓝色警告告警混入推送截图）；未配置级别时才恢复全级别
            _group_titles = ([_NMS_SEV_TITLES[s] for s in sevs]
                             if sevs else list(_NMS_SEV_TITLES.values()))
            try:
                await content_frame.evaluate(_NMS_SEVERITY_JS, _group_titles)
            except Exception:
                pass
            try:
                before = await self._nms_table_snapshot(content_frame)
                _sel = await self._nms_select_filter(content_frame, g)
                if _sel and not _sel.get("ok"):
                    logger.warning(f"[nms_agent] 分组视图切换失败 ({g}): {_sel.get('err')}")
                    continue
                await self._nms_wait_table_changed(content_frame, before, max_wait=12)
                # 切换分组后叠加"仅未确认"快速过滤（Acknowledged Equals 0）
                if unack_only:
                    _ack_ok = await self._nms_apply_ack_quickfilter(content_frame)
                ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
                fname = f"nms_{re.sub(r'[^A-Za-z0-9_-]', '_', g)}_{ts}.png"
                fpath = shot_dir / fname
                await page.screenshot(path=str(fpath))
                url = f"{config.ZOPS_PUBLIC_BASE_URL}/static/nms_shots/{fname}"
                shots.append({"group": g, "severity": "", "unack": _ack_ok,
                              "file": fname, "url": url})
                logger.info(f"[nms_agent] 分组告警截图完成: {g} -> {fname}"
                            + (" (仅未确认)" if _ack_ok else ""))
            except Exception as e:
                logger.warning(f"[nms_agent] 分组告警截图失败 ({g}): {e}")
            # ② 分级视图：只勾选指定级别（严重/主要/轻微…）后截图，ACK 快速过滤保持生效
            for sev in sevs:
                try:
                    before = await self._nms_table_snapshot(content_frame)
                    r = await content_frame.evaluate(
                        _NMS_SEVERITY_JS, [_NMS_SEV_TITLES[sev]])
                    if r and not r.get("ok"):
                        logger.warning(f"[nms_agent] 级别筛选失败 ({g}/{sev}): {r.get('err')}")
                        continue
                    await self._nms_wait_table_changed(content_frame, before, max_wait=10)
                    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
                    fname = f"nms_{re.sub(r'[^A-Za-z0-9_-]', '_', g)}_{sev}_{ts}.png"
                    fpath = shot_dir / fname
                    await page.screenshot(path=str(fpath))
                    url = f"{config.ZOPS_PUBLIC_BASE_URL}/static/nms_shots/{fname}"
                    shots.append({"group": g, "severity": sev, "unack": _ack_ok,
                                  "file": fname, "url": url})
                    logger.info(f"[nms_agent] 分级告警截图完成: {g}/{sev} -> {fname}"
                                + (" (仅未确认)" if _ack_ok else ""))
                except Exception as e:
                    logger.warning(f"[nms_agent] 分级告警截图失败 ({g}/{sev}): {e}")
            # ③ 恢复全级别勾选 + 清除快速过滤，避免影响后续视图
            try:
                await content_frame.evaluate(
                    _NMS_SEVERITY_JS, list(_NMS_SEV_TITLES.values()))
            except Exception:
                pass
            if _ack_ok:
                await self._nms_clear_quickfilters(content_frame)
        return shots

    async def _nms_apply_ack_quickfilter(self, content_frame, only_unack: bool = True) -> bool:
        """叠加"仅未确认"快速过滤：Build Quick Filter → Field=Acknowledged / Operator=Equals / Term=0 → Apply。
        返回是否成功（失败则截图降级为不过滤）。
        菜单项点击用 get_by_role（侦察验证可行，JS 遍历兜底）。"""
        if not only_unack:
            return False
        try:
            # 1) 展开 quickfilter 菜单
            _combo = content_frame.locator("css=[id*='quickfilter_button_arrow']")
            if await _combo.count() == 0:
                _combo = content_frame.locator("css=[id*='quickfilter_button']").last
            await _combo.click()
            await asyncio.sleep(1.2)
            # 2) Build Quick Filter → 打开对话框（get_by_role 优先）
            _build = content_frame.get_by_role("menuitem", name="Build Quick Filter")
            if await _build.count() == 0:
                _build = content_frame.locator("css=[id*='quickfilter_buildMenu']").first
            if await _build.count() == 0:
                r = await content_frame.evaluate(_NMS_QF_PICK_JS, "Build Quick Filter")
                if not r.get("ok"):
                    logger.warning(f"[nms_agent] 打开 Build Quick Filter 失败: {r.get('err')}")
                    return False
            else:
                await _build.click()
            await asyncio.sleep(2)
            # 3) Field → Acknowledged
            await content_frame.locator("css=[id*='filterDlg_field']").first.click()
            await asyncio.sleep(1.5)
            r = await content_frame.evaluate(_NMS_QF_PICK_JS, "Acknowledged")
            if not r.get("ok"):
                logger.warning(f"[nms_agent] 选择 Acknowledged 字段失败: {r.get('err')}")
                return False
            await asyncio.sleep(0.8)
            # 4) Operator → Equals
            await content_frame.locator("css=[id*='filterDlg_operator']").first.click()
            await asyncio.sleep(1.5)
            r = await content_frame.evaluate(_NMS_QF_PICK_JS, "Equals")
            if not r.get("ok"):
                logger.warning(f"[nms_agent] 选择 Equals 操作符失败: {r.get('err')}")
                return False
            await asyncio.sleep(0.8)
            # 5) Term = 0（未确认）
            _term = content_frame.locator("css=input[id*='filterDlg_term']").first
            if await _term.count() == 0:
                r = await content_frame.evaluate(_NMS_QF_TERM_JS, "0")
                if not r.get("ok"):
                    logger.warning(f"[nms_agent] 输入过滤值失败: {r.get('err')}")
                    return False
            else:
                await _term.fill("0")
                await _term.press("Enter")
            await asyncio.sleep(0.5)
            # 6) Apply
            await content_frame.locator("css=[id*='filterDlg_apply']").first.click()
            await asyncio.sleep(2.5)
            return True
        except Exception as e:
            logger.warning(f"[nms_agent] 未确认快速过滤应用异常: {e}")
            return False

    async def _nms_clear_quickfilters(self, content_frame) -> bool:
        """清除全部快速过滤（Remove All Quick Filters），恢复分组默认视图。"""
        try:
            _combo = content_frame.locator("css=[id*='quickfilter_button_arrow']")
            if await _combo.count() == 0:
                _combo = content_frame.locator("css=[id*='quickfilter_button']").last
            await _combo.click()
            await asyncio.sleep(1.2)
            _remove = content_frame.get_by_role("menuitem", name="Remove All Quick Filters")
            if await _remove.count() == 0:
                _remove = content_frame.locator("css=[id*='quickfilter_removeMenu']").first
            if await _remove.count() == 0:
                r = await content_frame.evaluate(_NMS_QF_PICK_JS, "Remove All Quick Filters")
                return bool(r.get("ok"))
            await _remove.click()
            await asyncio.sleep(1.5)
            return True
        except Exception as e:
            logger.warning(f"[nms_agent] 清除快速过滤异常: {e}")
            return False

    async def _nms_table_snapshot(self, frame) -> str:
        """抓 frame 内"tab 最多"那张表格的 inner_text 作为内容指纹"""
        js = """() => {
            const tables = document.querySelectorAll('table');
            let best = null, bestScore = -1;
            tables.forEach(t => {
                const rows = t.querySelectorAll('tr');
                if (rows.length < 5) return;
                const txt = t.innerText || '';
                const score = (txt.match(/\t/g) || []).length;
                if (score > bestScore) { bestScore = score; best = txt; }
            });
            return best || '';
        }"""
        try:
            return await frame.evaluate(js)
        except Exception:
            return ""

    async def _nms_wait_table_changed(self, frame, before: str, max_wait: int = 30) -> Dict:
        """循环等待表格 inner_text 相比 before 有明显变化；返回 {changed, waited, snap_len, after_len, after}"""
        import time as _t
        start = _t.monotonic()
        before_len = len(before)
        # 最少等 3s 给 NMS 一点渲染时间
        await asyncio.sleep(3)
        deadline = start + max(10, max_wait)
        poll = 0
        while _t.monotonic() < deadline and poll < 40:
            snap = await self._nms_table_snapshot(frame)
            snap_len = len(snap)
            # 判定"明显变化"：长度相对变化 > 8% 或长度本身显著差异
            if before_len == 0:
                if snap_len > 50:
                    return {"changed": True, "waited": _t.monotonic() - start,
                            "snap_len": snap_len, "after_len": snap_len, "after": snap}
            else:
                diff = abs(snap_len - before_len) / max(before_len, 1)
                # 内容不同且长度差异 > 5% 视为变化；或者长度差距 > 500 字符
                if diff > 0.05 or abs(snap_len - before_len) > 500:
                    return {"changed": True, "waited": _t.monotonic() - start,
                            "snap_len": snap_len, "after_len": snap_len, "after": snap}
            await asyncio.sleep(1)
            poll += 1
        # 超时：表格没变化，仍返回最新快照供下次对比
        snap = await self._nms_table_snapshot(frame)
        return {"changed": False, "waited": _t.monotonic() - start,
                "snap_len": len(snap), "after_len": len(snap), "after": snap}

    async def _nms_enum_filters(self, frame) -> List[str]:
        """枚举 NMS Filter 下拉框中全部可用分组标签（来自 dojo/store）"""
        js = """async () => {
            const inp = document.querySelector("input[id*='filterDropDown']");
            if (!inp) return {ok:false, err:'no input'};
            const dijit = window.dijit;
            if (!dijit) return {ok:false, err:'no dijit'};
            const w = dijit.getEnclosingWidget(inp);
            if (!w) return {ok:false, err:'no widget'};
            const store = w.store || w._store;
            if (!store || !store.query) return {ok:false, err:'no store.query'};
            const searchAttr = w.searchAttr || 'label';
            const r = store.query({});
            let items;
            if (r && typeof r.then === 'function') items = await r;
            else if (r && r.forEach && !Array.isArray(r)) { items=[]; r.forEach(x=>items.push(x)); }
            else items = r;
            return {ok:true, labels: (items||[]).map(it => String(it[searchAttr] || it.label || it.name || '')).filter(Boolean)};
        }"""
        try:
            res = await frame.evaluate(js)
        except Exception as e:
            logger.warning(f"[NMS] 枚举 filter 失败: {e}")
            return []
        if not res.get("ok"):
            logger.warning(f"[NMS] 枚举 filter 失败: {res.get('err')}")
            return []
        labels = [l for l in res.get("labels", []) if l.strip()]
        logger.info(f"[NMS] store 枚举到 {len(labels)} 个可用 filter")
        return labels

    async def _nms_resolve_groups(self, configured: List[str], frame) -> List[str]:
        """将 NMS_FILTERS 解析为最终要抓取的 group 列表；`*` / `ALL` 表示枚举全部可用分组"""
        if not configured:
            return []
        if len(configured) == 1 and configured[0].strip().lower() in ("*", "all"):
            labels = await self._nms_enum_filters(frame)
            return labels or ["OPSCORE_use"]
        return configured


    async def _nms_extract_alarms(self, frame, group_name: str = "") -> List[Dict]:
        # 从 NMS frame 提取告警：滚动 gridxVScroller 虚拟表格，逐屏累积去重全部行
        alarms = []
        try:
            payload = await frame.evaluate(_NMS_SCROLL_EXTRACT_JS)
        except Exception as e:
            logger.warning(f"[NMS] 滚动提取失败: {e}")
            return alarms
        header = payload.get("header") or []
        rows = payload.get("rows") or []
        col_map = {name: idx for idx, name in enumerate(header)} if header else {}
        if col_map:
            logger.info(f"[NMS] 表头列映射 ({len(header)}列): {col_map}")
        fs = payload.get("firstScreen")
        tot = payload.get("total")
        logger.info(f"[NMS] 分组 {group_name} 滚动累积 {len(rows)} 行 (首屏 {fs}, 页面Total {tot})")
        for item in rows:
            cells = item.get("cells") or []
            if len(cells) < 10:
                continue
            alarm = self._nms_parse_row(cells, len(alarms), group_name, col_map)
            if alarm:
                if alarm.get("severity") == "unknown":
                    css_sev = self._nms_css_to_severity({"bg": item.get("bg", ""), "cls": item.get("cls", "")})
                    if css_sev not in ("unknown", ""):
                        alarm["severity"] = css_sev
                alarms.append(alarm)
        logger.info(f"[NMS] 分组 {group_name} 解析完成: {len(alarms)} 条告警")
        return alarms


    def _nms_parse_row(self, cells: List[str], index: int, group_name: str = "", col_map: Dict = None) -> Optional[Dict]:
        """解析一行告警数据，使用列映射提取字段"""
        alarm = {
            "alarm_id": "",
            "group": group_name,
            "view_group": group_name,
            "severity": "unknown",
            "summary": "",
            "source": "",
            "time": "",
            "status": "",
            "alarm_type": "",
            "raw": " | ".join(cells),
        }

        if len(cells) < 3:
            return None

        col_map = col_map or {}

        # ── 1. 按列映射提取字段 ──
        def get_col(name, default=""):
            idx = col_map.get(name)
            if idx is not None and idx < len(cells):
                return cells[idx].strip()
            return default

        # 时间
        alarm["time"] = get_col("LastOccurrence") or get_col("FirstOccurrence") or cells[0].strip()

        # 摘要
        raw_summary = get_col("Summary") or cells[1].strip() if len(cells) > 1 else ""
        alarm["summary"] = self._nms_clean_summary(raw_summary)[:300]

        # 来源设备
        alarm["source"] = get_col("Node") or cells[2].strip() if len(cells) > 2 else ""

        # 告警类型
        alarm["alarm_type"] = get_col("AlertGroup") or get_col("Identifier")

        # 状态
        alarm["status"] = get_col("State") or get_col("Acknowledged")

        # ── 2. 严重级别：直接从 NMS Severity 列提取 ──
        nms_severity = get_col("Severity")
        alarm["severity"] = self._nms_map_severity(nms_severity)

        # ── 3. EventId（ObjectServer 稳定序号，跨视图去重键）──
        alarm["event_id"] = get_col("EventId") or get_col("Identifier")

        # ── 5. 稳定 ID（与视图无关，跨视图合并去重用）──
        if alarm["event_id"]:
            id_src = "eid:" + alarm["event_id"]
        else:
            id_src = (alarm["summary"] or alarm["raw"]) + "|" + alarm["source"] + "|" + alarm["time"]
        alarm["alarm_id"] = hashlib.md5(id_src.encode()).hexdigest()[:12]

        # 该告警所属视图（跨视图合并时累积）
        alarm["groups"] = [group_name] if group_name else []

        return alarm if (alarm["summary"] or alarm["source"]) else None

    @staticmethod
    def _nms_clean_summary(s: str) -> str:
        """清理 NMS summary：剥离 'key=' 前缀，提取可读描述。
        例如 'Linux_CPU利用率=87.4%,hostname=X,ip=1.2.3.4'
           -> 'Linux CPU利用率: 87.4% (hostname=X, ip=1.2.3.4)'
        """
        if not s:
            return ""
        # 如果包含 '='，按 key=value 拆分
        if "=" in s:
            parts = [p.strip() for p in s.split(",") if p.strip()]
            first = ""
            extras = []
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if not first:
                        # 首项: "key=value" -> "key 可读化: value"
                        pretty_key = k.replace("_", " ")
                        first = f"{pretty_key}: {v}" if v else pretty_key
                    else:
                        extras.append(f"{k}={v}")
                else:
                    if not first:
                        first = p
                    else:
                        extras.append(p)
            if extras:
                return f"{first} ({', '.join(extras[:3])})"
            return first
        # 纯文本（如 'BFD Session Up ( bfdSessEntry.4805 ) | ...'）
        # 去除多余的 ' | ' 分隔（来自 raw）
        return s.split(" | ")[0].strip()

    @staticmethod
    def _nms_map_severity(nms_severity: str) -> str:
        """将 NMS 原始 Severity 值映射为标准级别"""
        if not nms_severity:
            return "unknown"
        s = nms_severity.strip().lower()
        # 数字映射（IBM Netcool 标准）
        if s.isdigit():
            mapping = {"5": "critical", "4": "major", "3": "minor", "2": "warning", "1": "clear", "0": "cleared"}
            return mapping.get(s, "unknown")
        # 文本映射
        mapping = {
            "critical": "critical", "严重": "critical",
            "major": "major", "重要": "major",
            "minor": "minor", "次要": "minor",
            "warning": "warning", "警告": "warning",
            "indeterminate": "indeterminate", "未知": "indeterminate",
            "clear": "clear", "cleared": "cleared", "清除": "cleared",
        }
        return mapping.get(s, "unknown")

    @staticmethod
    def _nms_css_to_severity(color_info: dict) -> str:
        """从 CSS 背景色/类名推断告警严重级别（NMS Severity 列为空时的 fallback）"""
        bg = (color_info.get("bg") or "").lower().strip()
        cls = (color_info.get("cls") or "").lower().strip()

        # 从 CSS class 名推断
        for kw in ("critical", "严重"):
            if kw in cls:
                return "critical"
        for kw in ("major", "重要"):
            if kw in cls:
                return "major"
        for kw in ("minor", "次要"):
            if kw in cls:
                return "minor"
        for kw in ("warning", "警告"):
            if kw in cls:
                return "warning"

        # 从背景色推断 (IBM Netcool 标准配色)
        if not bg or bg == "transparent" or bg == "rgba(0, 0, 0, 0)":
            return "unknown"

        # 解析 rgb/rgba/hex 颜色
        import re
        rgb_match = re.search(r'rgba?\((\d+),\s*(\d+),\s*(\d+)', bg)
        if rgb_match:
            r, g, b = int(rgb_match.group(1)), int(rgb_match.group(2)), int(rgb_match.group(3))
            # Critical: 红色 (R>180, G<80, B<80)
            if r > 180 and g < 80 and b < 80:
                return "critical"
            # Major: 橙色 (R>200, G 100-180, B<80)
            if r > 200 and 80 <= g <= 180 and b < 80:
                return "major"
            # Minor: 黄色 (R>200, G>200, B<80)
            if r > 200 and g > 200 and b < 80:
                return "minor"
            # Warning: 青色/浅蓝 (R<100, G>180, B>180)
            if r < 100 and g > 180 and b > 180:
                return "warning"
            # Indeterminate: 紫色/品红 (R>150, G<80, B>150)
            if r > 150 and g < 80 and b > 150:
                return "indeterminate"
            # Clear: 绿色 (R<80, G>150, B<80)
            if r < 80 and g > 150 and b < 80:
                return "clear"

        # hex 颜色
        hex_match = re.search(r'#([0-9a-f]{6})', bg)
        if hex_match:
            h = hex_match.group(1)
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            if r > 180 and g < 80 and b < 80:
                return "critical"
            if r > 200 and 80 <= g <= 180 and b < 80:
                return "major"
            if r > 200 and g > 200 and b < 80:
                return "minor"
            if r < 100 and g > 180 and b > 180:
                return "warning"

        return "unknown"

    def _nms_parse_text(self, text: str) -> List[Dict]:
        """从纯文本中提取告警（fallback）"""
        alarms = []
        lines = text.split("\n")
        for line in lines:
            line = line.strip()
            if not line or len(line) < 10:
                continue
            # 过滤明显非告警行
            if any(kw in line.lower() for kw in [
                "copyright", "version", "menu", "file", "edit"
            ]):
                continue
            alarm = {
                "alarm_id": hashlib.md5(line.encode()).hexdigest()[:12],
                "severity": "unknown",
                "summary": line[:200],
                "source": "",
                "time": "",
                "status": "",
                "raw": line,
            }
            # 检测严重级别
            for kw in ["critical", "严重", "major", "重要"]:
                if kw in line.lower():
                    alarm["severity"] = (
                        "critical" if kw in ["critical", "严重"] else "major"
                    )
                    break
            alarms.append(alarm)
            if len(alarms) >= 50:
                break
        return alarms

    @staticmethod
    def _nms_hash_alarms(alarms: List[Dict]) -> str:
        """对告警列表生成快照 hash，用于快速判断是否有变化。

        checklist 标注纳入 hash：规则表调整后同一批告警的预案归属会变，
        若只看 alarm_id 则判定"无变化"而早退，旧快照会持续展示按旧规则
        标注的错误预案（如把虚机逻辑卷使用率归到存储磁盘故障）。"""
        ids = sorted(f"{a.get('alarm_id', '')}:{a.get('checklist') or ''}"
                     for a in alarms)
        raw = "|".join(ids)
        return hashlib.md5(raw.encode()).hexdigest()

    async def _nms_clean_seen(self, r, seen_key: str = NMS_KEY_SEEN):
        """清理超过 TTL 的 seen 记录 (NMS_SEEN_TTL, 默认 7 天)。

        TTL 必须 ≥ 告警典型存活时长: seen 时间戳只在首见时写入不刷新,
        若 TTL 短于告警存活期, 仍活跃的告警会被清出 seen 后整批重报为"新增"
        (曾用 1h 的相似度窗口导致每 ~2h 一次 new≈44000 的全量脉冲)。
        """
        try:
            ttl = max(3600, getattr(config, "NMS_SEEN_TTL", 7 * 86400))
            seen = await r.hgetall(seen_key)
            now = time.time()
            for alarm_id, ts_str in seen.items():
                try:
                    if now - float(ts_str) > ttl:
                        await r.hdel(seen_key, alarm_id)
                except (ValueError, TypeError):
                    await r.hdel(seen_key, alarm_id)
        except Exception as e:
            logger.debug(f"[NMS] seen 清理失败: {e}")

    async def _nms_write_history_zset(self, r, base: str, new_alarms, k_seen: str):
        """Phase H2: 历史写入 (内容键去重)。

        结构: {base}:history_z     member=内容键(16hex), score=最早首见时间
              {base}:history_data  field=内容键 → JSON(alarm + occurrences + _ck)
        同一逻辑事件 (Node|EventKey|规范化摘要 相同) 无论 Serial 换手多少次,
        历史里只占一条: occurrences 累加、first_time 取最早、last_time 取最新。
        历史条数 = 真实事件数, 30 天保留不再被换手流量冲爆。"""
        now_ts = time.time()
        active = [a for a in new_alarms if a.get("severity") in _NMS_ACTIVE_SEV]
        if active:
            ck_cnt: Dict[str, int] = {}
            ck_first: Dict[str, float] = {}
            ck_alarm: Dict[str, Dict] = {}
            for a in active:
                ck = _nms_alarm_content_key(a)
                ts_str = (a.get("first_time") or a.get("time")
                          or a.get("last_time") or "")
                score = _nms_parse_iso_ts(ts_str) or now_ts
                ck_cnt[ck] = ck_cnt.get(ck, 0) + 1
                ck_alarm[ck] = a
                if ck not in ck_first or score < ck_first[ck]:
                    ck_first[ck] = score
            cks = list(ck_alarm)
            try:
                prevs = await r.hmget(f"{base}:history_data", cks)
            except Exception:
                prevs = [None] * len(cks)
            for ck, prev in zip(cks, prevs):
                p_occ, p_first = 0, None
                if prev:
                    try:
                        p = json.loads(prev)
                        p_occ = int(p.get("occurrences", 1))
                        p_first = _nms_parse_iso_ts(
                            p.get("first_time") or p.get("time") or "")
                    except Exception:
                        p_occ, p_first = 1, None
                score = ck_first[ck]
                if p_first is not None and p_first > 0:
                    score = min(score, p_first)
                rec = dict(ck_alarm[ck])
                rec["occurrences"] = p_occ + ck_cnt[ck]
                rec["_ck"] = ck
                try:
                    await r.hset(f"{base}:history_data", ck,
                                 json.dumps(rec, ensure_ascii=False))
                    await r.zadd(f"{base}:history_z", {ck: float(score)})
                except Exception as e:
                    logger.warning(f"[NMS] 历史写入失败 (下轮覆盖修复会补回): {e}")
            # 老化与条数保险丝不在写入路径做: 改由 _nms_repair_history 每轮统一
            # 执行, 并豁免当前仍活跃的事件 (否则存活超保留期的告警会被当成
            # "过期数据"删掉, 表现为正在响的告警在历史库里凭空消失)。
        # seen 标记 (全部 new, 含 cleared; 语义不变)
        for alarm in new_alarms:
            aid = alarm.get("alarm_id", "")
            if aid:
                try:
                    await r.hset(k_seen, aid, str(now_ts))
                except Exception:
                    pass
        await self._nms_clean_seen(r, k_seen)

    async def _nms_migrate_history_list_to_zset(self, r, list_key: str,
                                                zset_key: str, data_key: str,
                                                batch: int = 500):
        """Phase H 兼容: 一次性把老 LIST 历史迁入新 ZSET (内容键格式)。

        - ZSET 非空: 仅检查存量格式, 老 JSON 成员 → 内容键格式一次性转换。
        - ZSET 为空且 LIST 非空: 全量迁移, 完成后清空 LIST。"""
        try:
            existing = await r.zcard(zset_key)
            if existing > 0:
                await self._nms_backfill_history_data(r, zset_key, data_key)
                return
            old_count = await r.llen(list_key)
            if old_count == 0:
                return  # 老数据本来就是空
            logger.info(f"[nms_agent] 启动历史迁移: list {old_count} 条 → zset")
            migrated = 0
            now_ts = time.time()
            start = 0
            while True:
                chunk = await r.lrange(list_key, start, start + batch - 1)
                if not chunk:
                    break
                mapping: Dict[str, float] = {}
                payloads: Dict[str, str] = {}
                for h in chunk:
                    try:
                        obj = json.loads(h)
                        ts_str = (obj.get("first_time") or obj.get("time")
                                  or obj.get("last_time") or "")
                        score = _nms_parse_iso_ts(ts_str) or (now_ts - 86400 * 30)
                        ck = _nms_alarm_content_key(obj)
                        obj.setdefault("_ck", ck)
                        obj.setdefault("occurrences", 1)
                        if ck not in mapping or score < mapping[ck]:
                            mapping[ck] = float(score)
                        payloads[ck] = json.dumps(obj, ensure_ascii=False)
                    except Exception:
                        continue
                if mapping:
                    await r.zadd(zset_key, mapping)
                    for ck, payload in payloads.items():
                        await r.hset(data_key, ck, payload)
                    migrated += len(mapping)
                start += batch
            # 清空老 LIST (保留键名 = 标记已迁移)
            await r.delete(list_key)
            logger.info(f"[nms_agent] 历史迁移完成: {migrated} 条")
        except Exception as e:
            logger.debug(f"[nms_agent] 历史迁移失败 (忽略): {e}")

    async def _nms_backfill_history_data(self, r, zset_key: str, data_key: str,
                                         batch: int = 500):
        """存量格式修复: history_z 里 Phase H1 的 JSON 成员 → 内容键格式。

        用 zscan 游标遍历 (转换会增删成员, 基于位置的分批读取会漏项);
        同内容键的多条旧成员 occurrences 合并累加。完成后写 fmt 标记键,
        之后周期直接跳过。幂等。"""
        try:
            fmt_key = f"{zset_key}:fmt"
            if await r.get(fmt_key):
                return  # 已完成过转换
            logger.info("[nms_agent] 历史格式修复: 扫描存量成员")
            # zscan 全量收集 (游标遍历不受成员增删影响); 无老成员则仅落标记
            pending: Dict[str, list] = {}  # old_member -> (score, obj|None)
            cursor = 0
            while True:
                cursor, chunk = await r.zscan(
                    zset_key, cursor=cursor, count=batch)
                for m, score in chunk:
                    try:
                        pending[m] = (float(score), json.loads(m))
                    except Exception:
                        pending[m] = (float(score), None)
                if cursor == 0:
                    break
            json_cnt = sum(1 for _, obj in pending.values()
                           if isinstance(obj, dict))
            if not pending or json_cnt == 0:
                await r.set(fmt_key, "v2")
                return
            logger.info(f"[nms_agent] 历史格式修复: 发现 {json_cnt} 条老格式成员")
            # 2) 本地按内容键归并: 同 ck 多条旧成员 → occurrences 累加, score 取最早
            merged: Dict[str, Dict] = {}
            ck_score: Dict[str, float] = {}
            old_members = []
            for old_member, (score, obj) in pending.items():
                old_members.append(old_member)
                if not isinstance(obj, dict):
                    continue  # 无法解析/标量(裸数字等): 仅删除
                ck = _nms_alarm_content_key(obj)
                obj.setdefault("_ck", ck)
                if ck in merged:
                    merged[ck]["occurrences"] = (merged[ck].get("occurrences", 1)) + 1
                    lt = (obj.get("last_time") or obj.get("time") or "")
                    if lt and lt > (merged[ck].get("last_time") or ""):
                        merged[ck]["last_time"] = lt
                else:
                    obj.setdefault("occurrences", 1)
                    merged[ck] = obj
                if ck not in ck_score or score < ck_score[ck]:
                    ck_score[ck] = score
            # 3) 批量落库: payload + 新成员, 删除全部老成员 (保留新内容键成员本身)
            for ck, obj in merged.items():
                await r.hset(data_key, ck, json.dumps(obj, ensure_ascii=False))
                await r.zadd(zset_key, {ck: ck_score[ck]})
            ck_set = set(ck_score.keys())
            for m in old_members:
                if m in ck_set:
                    continue  # 新内容键成员, 不删
                await r.zrem(zset_key, m)
            await r.set(fmt_key, "v2")
            logger.info(f"[nms_agent] 历史格式修复完成: "
                        f"{len(pending)} 老成员 → {len(merged)} 事件")
        except Exception as e:
            logger.debug(f"[nms_agent] 历史格式修复失败 (忽略): {e}")

    async def _nms_trim_history(self, r, base: str, active_cks=None,
                                now_ts: float = None) -> int:
        """历史老化: 删除超出保留期且当前不活跃的事件 (payload 同步清理)。

        活跃豁免是关键: score = 首见时间, 一个持续数十天的事件 (证书倒数天数、
        长期磁盘劣化) 若只按首见老化, 正在告警的事件也会被从历史库删掉 ——
        前端"历史告警"就再也找不到它, 这不是数据自然过期, 是活跃数据被误删。"""
        now_ts = now_ts or time.time()
        protected = set(active_cks or ())
        k_z, k_data = f"{base}:history_z", f"{base}:history_data"
        removed = 0
        retention_days = max(0, getattr(config, "NMS_HISTORY_RETENTION_DAYS", 30))
        try:
            if retention_days > 0:
                cutoff = now_ts - retention_days * 86400
                stale = [ck for ck in await r.zrangebyscore(
                    k_z, "-inf", f"({cutoff}") if ck not in protected]
                if stale:
                    await r.zrem(k_z, *stale)
                    await r.hdel(k_data, *stale)
                    removed += len(stale)
                    logger.info(f"[nms_agent] 历史老化清理: 删除 {len(stale)} 条"
                                f" (> {retention_days} 天, 活跃事件豁免)")
        except Exception as e:
            logger.debug(f"[NMS] 历史老化失败 (忽略): {e}")
        # 条数保险丝: 只防内存失控 (保留期由 retention_days 决定), 同样豁免活跃
        max_n = getattr(config, "NMS_HISTORY_ZSET_MAX", 100000)
        try:
            if max_n > 0:
                over = await r.zcard(k_z) - max_n
                if over > 0:
                    excess = [ck for ck in await r.zrange(k_z, 0, over - 1)
                              if ck not in protected]
                    if excess:
                        await r.zrem(k_z, *excess)
                        await r.hdel(k_data, *excess)
                        removed += len(excess)
                        logger.info(f"[nms_agent] 历史条数裁剪: 删除 {len(excess)} 条"
                                    f" (> NMS_HISTORY_ZSET_MAX={max_n})")
        except Exception as e:
            logger.debug(f"[NMS] 历史条数裁剪失败 (忽略): {e}")
        return removed

    async def _nms_repair_history(self, r, base: str, alarms,
                                  batch: int = 1000) -> int:
        """历史覆盖修复 (每轮幂等执行, 取代一次性基线导入)。

        背景: 历史过去只由"本轮新增告警"写入, 而长期活跃事件两条路径都覆盖不到 ——
        1) 监控上线/历史库重建之前就存在的存量; 2) 摘要随时间漂移换了内容键的存量
        (如 "ssl证书可用天数: 27" 每日倒数 → 内容键天天变, 老键被老化删除、新键
        因 Serial 未变而不算"新增", 于是永远不入历史)。实测 4.2 万条现存告警中
        3.1 万条不在历史库, 前端"历史告警"因此看不到正在响的告警。
        做法: 以当前快照活跃事件为准补录缺失记录 (occurrences=1, 首见取告警
        FirstTime), 已存在的不覆盖; 补录后做保留期老化 (活跃事件豁免)。
        每轮最多补写 NMS_HISTORY_REPAIR_MAX 条, 防首次空库时大批量写入阻塞采集。"""
        added = missing = 0
        try:
            now_ts = time.time()
            ck_first: Dict[str, float] = {}
            ck_alarm: Dict[str, Dict] = {}
            for a in (alarms or []):
                if a.get("severity") not in _NMS_ACTIVE_SEV:
                    continue
                ck = _nms_alarm_content_key(a)
                score = _nms_parse_iso_ts(a.get("first_time") or a.get("time")
                                          or a.get("last_time") or "") or now_ts
                ck_alarm.setdefault(ck, a)
                if ck not in ck_first or score < ck_first[ck]:
                    ck_first[ck] = score
            cks = list(ck_alarm)
            cap = max(0, int(getattr(config, "NMS_HISTORY_REPAIR_MAX", 8000)))
            k_data, k_z = f"{base}:history_data", f"{base}:history_z"
            for i in range(0, len(cks), batch):
                chunk = cks[i:i + batch]
                try:
                    prevs = await r.hmget(k_data, chunk)
                except Exception:
                    prevs = [None] * len(chunk)
                for ck, prev in zip(chunk, prevs):
                    if prev:
                        continue
                    missing += 1
                    if cap and added >= cap:
                        continue  # 单轮上限, 剩余留给下一轮
                    rec = dict(ck_alarm[ck])
                    rec["occurrences"] = 1
                    rec["_ck"] = ck
                    try:
                        await r.hset(k_data, ck,
                                     json.dumps(rec, ensure_ascii=False))
                        await r.zadd(k_z, {ck: float(ck_first[ck])})
                        added += 1
                    except Exception as e:
                        logger.warning(f"[NMS] 历史补录失败 (下轮重试): {e}")
            if cks:
                await self._nms_trim_history(r, base, set(cks), now_ts)
            if added or missing != added:
                logger.info(f"[nms_agent] 历史覆盖修复: 快照活跃 {len(cks)} 个事件 → "
                            f"补录 {added} 个 (缺失 {missing}"
                            f"{'，单轮上限内剩余留待下轮' if missing > added else ''})")
        except Exception as e:
            logger.debug(f"[nms_agent] 历史覆盖修复失败 (忽略): {e}")
        return added


    async def _nms_kb_remediation_context(self, new_alarms, all_alarms):
        """KB 修复知识检索 (Phase 3.2 增强)。

        增强点:
        1. 查询词带上 Checklist 命中标题 — 预案名 (如 "SSL证书过期") 是比原始摘要
           (含 IP/变量噪声) 更优的检索键, 命中率显著高于裸摘要;
        2. 模式指纹缓存 — 同质化告警 (同模式不同变量) 不重复检索,
           结果缓存 1h (`{prefix}:nms:kb_cache:{fp}`);
        3. 检索失败/无结果静默降级为空串, 绝不阻塞主分析。
        """
        try:
            from agents.kb_agent import kb_multi_recall, format_kb_results
        except Exception as e:
            logger.debug(f'[NMS] KB import failed: {e}')
            return ''
        prio = {'critical': 0, 'major': 1, 'minor': 2}
        cand = [a for a in all_alarms if a.get('severity') in prio]
        cand.sort(key=lambda a: prio.get(a.get('severity'), 9))
        _cl_entries = await _nms_merged_checklist()
        seen = set()
        queries = []
        cache_keys = []
        for a in cand:
            summ = (a.get('summary') or '').strip()
            if not summ:
                continue
            key = summ[:40]
            if key in seen:
                continue
            seen.add(key)
            # Phase 3.2: Checklist 命中标题优先作检索键 (预案名是知识库里的人工标题,
            # 比含 IP/接口号的原始摘要更接近 KB 文档命名)
            cl_title = (a.get('checklist')
                        or _nms_checklist_match(summ, _cl_entries))
            q = f"{cl_title} {summ[:40]}".strip() if cl_title else summ[:60]
            queries.append(q)
            cache_keys.append(self._nms_pattern_key(a))
            if len(queries) >= 5:
                break
        if not queries:
            return ''

        # Phase 3.2: 模式指纹缓存 — 全部查询都命中缓存时直接复用, 不再检索
        cache_hits: Dict[str, str] = {}
        missed_keys: List[str] = []
        try:
            r = await self.get_redis()
            for fp in cache_keys:
                ck = f"{config.REDIS_PREFIX}:nms:kb_cache:{fp}"
                cached = await r.get(ck)
                if cached:
                    cache_hits[fp] = cached
                else:
                    missed_keys.append(fp)
        except Exception:
            missed_keys = list(cache_keys)  # Redis 不可用时退化为每次检索
        if len(cache_hits) == len(set(cache_keys)) and cache_hits:
            # 全命中 → 拼接缓存文本
            parts = [v for _k, v in sorted(cache_hits.items()) if v]
            if parts:
                logger.info(f'[NMS] KB 检索全命中模式缓存 ({len(cache_hits)} 键), 复用')
                return "\n".join(parts)
        if not missed_keys and not cache_hits:
            return ''

        try:
            results = await kb_multi_recall(queries, limit=config.KB_SEARCH_LIMIT)
        except Exception as e:
            logger.warning(f'[NMS] KB search failed: {e}')
            return ''
        if not results:
            # 负缓存: 无结果也写缓存 (空串), 避免同模式每轮都白查
            try:
                r2 = await self.get_redis()
                for fp in set(cache_keys):
                    await r2.set(f"{config.REDIS_PREFIX}:nms:kb_cache:{fp}",
                                 "", ex=3600)
            except Exception:
                pass
            return ''
        logger.info(f'[NMS] KB remediation check hit {len(results)} docs')
        formatted = format_kb_results(results)
        # 写缓存 (整个查询集合的联合结果按每个指纹存一份; 命中任一指纹即复用全集)
        try:
            r3 = await self.get_redis()
            for fp in set(cache_keys):
                await r3.set(f"{config.REDIS_PREFIX}:nms:kb_cache:{fp}",
                             formatted, ex=3600)
        except Exception:
            pass
        return formatted

    async def _nms_dns_probe(self, alarms: List[Dict], max_targets: int = 8) -> List[Dict]:
        """对 DNS 类告警做解析测试（OPS-CORE DNS API 实测），返回结构化结果列表
        [{domain, server, record_type, status, label, summary}]，供快照持久化与处理分析引用。
        status: ok=解析出结果 / fail=解析失败 / no_record=无记录；失败/超时静默降级为空列表。"""
        if not alarms:
            return []
        try:
            from agents.dns_api import (
                extract_dns_test_targets, get_dns_client, restore_encoded_domain,
            )
            client = get_dns_client()

            async def _test_one(domain: str, rt: str, server: str) -> dict:
                try:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(client.query, domain, rt, server),
                        timeout=15)
                    # 编码域名 (无点) 且无记录时, 尝试还原常见 TLD 后缀再测 (Zabbix 监控项编码)
                    if res.get("success") and "." not in domain and not any(
                            r.get("result") for r in (res.get("successful_results") or [])):
                        for cand in restore_encoded_domain(domain)[1:]:
                            res2 = await asyncio.wait_for(
                                asyncio.to_thread(client.query, cand, rt, server),
                                timeout=15)
                            if res2.get("success") and any(
                                    r.get("result") for r in (res2.get("successful_results") or [])):
                                res2["_restored_from"] = domain
                                return res2
                    return res
                except Exception as e:
                    return {"success": False, "error": f"解析测试异常: {e}"}

            out: List[Dict] = []
            tested_keys: set = set()   # domain|server|rt 去重: 相同告警批量出现时只测一次
            for a in alarms:
                if len(out) >= max_targets:
                    break
                for t in extract_dns_test_targets(a.get("summary") or "")[:3]:
                    key = f"{t['domain'].lower()}|{t['server']}|{t['record_type']}"
                    if key in tested_keys:
                        continue
                    tested_keys.add(key)
                    res = await _test_one(t["domain"], t["record_type"], t["server"])
                    sres = [r.get("result") for r in (res.get("successful_results") or []) if r.get("result")]
                    eres = [r.get("result") for r in (res.get("error_results") or []) if r.get("result")]
                    if res.get("success") is False:
                        status, detail = "fail", (res.get("error") or "未知错误")
                    elif sres:
                        status, detail = "ok", "; ".join(sres[:3])
                    elif eres:
                        status, detail = "fail", eres[0]
                    else:
                        status, detail = "no_record", "无记录"
                    restored = res.get("_restored_from")
                    dom = f"{t['domain']}(还原自监控项编码 {restored})" if restored else t["domain"]
                    srv = f"server={t['server']}" if t["server"] else "默认server"
                    out.append({
                        "domain": t["domain"], "server": t["server"],
                        "record_type": t["record_type"], "status": status,
                        "label": f"[{a.get('severity', '?')}/{a.get('source', '?')}]",
                        "summary": f"{dom} [{t['record_type']}, {srv}] → {detail}",
                    })
                    if len(out) >= max_targets:
                        break
            return out
        except Exception as e:
            logger.debug(f"[nms_agent] DNS 工具实测失败 (忽略): {e}")
            return []

    async def _nms_ssl_probe(self, alarms: List[Dict], max_targets: int = 8) -> List[Dict]:
        """对 SSL 证书类告警的网页地址做证书实测（TLS 握手提取剩余天数），
        返回结构化结果列表 [{host, port, status, days_left, expires, label, summary}]，
        供快照持久化与处理分析引用。

        status 语义（仅证书读取，不校验证书链/域名，不能作为健康证明）：
        - ok：剩余天数 ≥ NMS_SSL_MIN_DAYS
        - warn：剩余天数低于观察门槛
        - expired：已过期
        - fail：连接/解析失败，无法读取证书
        """
        if not alarms:
            return []
        try:
            from agents.ssl_check import check_cert, extract_cert_targets
            out: List[Dict] = []
            tested: set = set()   # host|port 去重
            for a in alarms:
                if len(out) >= max_targets:
                    break
                for t in extract_cert_targets(a.get("summary") or "")[:2]:
                    key = f"{t['host'].lower()}|{t['port']}"
                    if key in tested:
                        continue
                    tested.add(key)
                    res = await asyncio.wait_for(
                        asyncio.to_thread(check_cert, t["host"], t["port"]),
                        timeout=15)
                    if res.get("success"):
                        days = res["days_left"]
                        _min_days = int(getattr(config, "NMS_SSL_MIN_DAYS", 30))
                        if days < 0:
                            status = "expired"
                            detail = f"已到期（{abs(days)} 天前）"
                        elif days < _min_days:
                            status = "warn"
                            detail = f"剩余 {days} 天，低于 {_min_days} 天观察门槛（到期 {res['expires']}）"
                        else:
                            status = "ok"
                            detail = f"剩余 {days} 天（到期 {res['expires']}）"
                        detail += "；仅读取证书，未校验证书链/域名"
                    else:
                        status, detail = "fail", (res.get("error") or "未知错误")
                    out.append({
                        "host": t["host"], "port": t["port"], "status": status,
                        "days_left": res.get("days_left"),
                        "expires": res.get("expires"),
                        "verified": False,
                        "note": "旧采集实测仅读取证书，不作为证书健康证明",
                        "label": f"[{a.get('severity', '?')}/{a.get('source', '?')}]",
                        "summary": f"{t['host']}:{t['port']} → {detail}",
                    })
                    if len(out) >= max_targets:
                        break
            return out
        except Exception as e:
            logger.debug(f"[nms_agent] SSL 证书实测失败 (忽略): {e}")
            return []

    async def _nms_tcp_probe(self, alarms: List[Dict], ports: List[int] = (53,),
                             max_targets: int = 8, timeout: int = 5) -> List[Dict]:
        """对网络服务类告警做 TCP 端口探测（如 PDNS 53 端口），返回结构化结果
        [{ip, port, status, label, summary}]。status: ok=端口可达 / fail=不可达或拒绝。"""
        if not alarms:
            return []
        try:
            out: List[Dict] = []
            tested: set = set()   # ip|port 去重

            def _tcp_connect(ip: str, port: int) -> dict:
                try:
                    with socket.create_connection((ip, port), timeout=timeout):
                        return {"success": True}
                except socket.timeout:
                    return {"success": False, "error": "连接超时"}
                except ConnectionRefusedError:
                    return {"success": False, "error": "连接被拒绝"}
                except Exception as e:
                    return {"success": False, "error": str(e)[:60]}

            for a in alarms:
                if len(out) >= max_targets:
                    break
                ip = (a.get("node") or a.get("source") or "").strip()
                if not ip:
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", a.get("summary") or "")
                    ip = m.group(1) if m else ""
                if not ip:
                    continue
                for port in ports:
                    key = f"{ip}|{port}"
                    if key in tested:
                        continue
                    tested.add(key)
                    res = await asyncio.wait_for(
                        asyncio.to_thread(_tcp_connect, ip, port), timeout=timeout + 5)
                    if res.get("success"):
                        status, detail = "ok", "端口可达"
                    else:
                        status, detail = "fail", (res.get("error") or "不可达")
                    out.append({
                        "ip": ip, "port": port, "status": status,
                        "label": f"[{a.get('severity', '?')}/{ip}]",
                        "summary": f"{ip}:{port} → {detail}",
                    })
                    if len(out) >= max_targets:
                        break
            return out
        except Exception as e:
            logger.debug(f"[nms_agent] TCP 端口探测失败 (忽略): {e}")
            return []

    async def _nms_backup_probe(self, alarms: List[Dict], max_targets: int = 6) -> List[Dict]:
        """对备份失败类告警用 OPS-CORE-MGT BRR/M365 API 实测备份作业状态，
        返回结构化结果 [{ip, total, ok, failed, warning, latest_rp, servers,
        status, label, summary}]。status: ok=最近结果正常 / attention=存在失败作业 / fail=查询失败。"""
        if not alarms:
            return []
        try:
            from agents.opscore_api import get_mgt_client
            client = get_mgt_client()
            out: List[Dict] = []
            tested: set = set()   # ip 去重
            for a in alarms:
                if len(out) >= max_targets:
                    break
                ip = (a.get("node") or a.get("source") or "").strip()
                if not ip:
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", a.get("summary") or "")
                    ip = m.group(1) if m else ""
                if not ip or ip in tested:
                    continue
                tested.add(ip)
                is_o365 = (a.get("checklist") == "A2C O365备份失败")
                ep = "/api/veeam-m365/backup-jobs/" if is_o365 else "/api/brr/backups/"
                try:
                    data = await asyncio.wait_for(
                        client.request("GET", ep, params={"search": ip, "page_size": 50}),
                        timeout=30)
                except Exception as e:
                    logger.debug(f"[nms_agent] 备份实测查询失败 ({ip}): {e}")
                    continue
                if not isinstance(data, dict) or data.get("error"):
                    continue
                rs = data.get("results") or []
                if not rs:
                    continue
                from collections import Counter
                res_key = ("last_result" if is_o365 else "job_last_result")
                rp_key = ("latest_restore_point" if is_o365 else "latest_restore_point_time")
                results = Counter(str(r.get(res_key) or "").strip() for r in rs)
                ok = results.get("Success", 0)
                failed = results.get("Failed", 0)
                warning = results.get("Warning", 0)
                rps = sorted((r.get(rp_key) for r in rs if r.get(rp_key)), reverse=True)
                servers = sorted({(r.get("server_url") or r.get("server") or "").strip()
                                  for r in rs if (r.get("server_url") or r.get("server"))})
                status = "attention" if failed else "ok"
                latest = rps[0][:16].replace("T", " ") if rps else ""
                detail_bits = [f"最近结果 Success {ok}" + (f"、Failed {failed}" if failed else "") + (f"、Warning {warning}" if warning else "")]
                if latest:
                    detail_bits.append(f"最近还原点 {latest}")
                if servers:
                    detail_bits.append(f"服务器 {servers[0]}")
                summary = f"{ip} → BRR 备份作业 {len(rs)} 个，{'；'.join(detail_bits)}" \
                    if not is_o365 else \
                    f"{ip} → M365 备份作业 {len(rs)} 个，{'；'.join(detail_bits)}"
                out.append({
                    "ip": ip, "total": len(rs), "ok": ok, "failed": failed,
                    "warning": warning, "latest_rp": latest,
                    "servers": servers[:3], "status": status,
                    "is_o365": is_o365,
                    "label": f"[{a.get('severity', '?')}/{a.get('checklist', '?')}]",
                    "summary": summary,
                })
            return out
        except Exception as e:
            logger.debug(f"[nms_agent] 备份作业实测失败 (忽略): {e}")
            return []

    async def _nms_dns_resolve_context(self, alarms: List[Dict], timeout_s: int = 30,
                                       cached_tests: Optional[List[Dict]] = None) -> str:
        """DNS 解析富化上下文，供告警分析注入。

        两段式:
        1. DNS 专项告警 → 用告警中指定的 server 对告警域名做解析测试 (实测结果)
        2. 全部告警 → 提取 IP/域名做 PTR/A 通用解析 (定位告警对象主机名/IP 归属)

        cached_tests: 本轮抓取已测的结构化结果（快照 tool_checks.dns），传入时跳过重复测试。
        失败/超时静默降级为空串，绝不阻塞告警分析主流程。
        """
        if not config.NMS_DNS_RESOLVE_ENABLED:
            return ""
        try:
            from agents.dns_api import (
                extract_targets, resolve_targets, format_resolve_report,
                extract_dns_test_targets, format_dns_test_result, get_dns_client,
            )
            alarms = alarms or []

            # ── 1. DNS 专项告警解析测试 (指定告警中的 server) ──
            # cached_tests: 本轮抓取已测的结构化结果，复用避免重复调用 DNS API
            if cached_tests is not None:
                test_lines = [f"- {t.get('label', '')} {t['summary']}".strip()
                              for t in cached_tests if t.get('summary')]
                tested_domains = {t['domain'].lower() for t in cached_tests}
                tested_keys: set = set()
                dns_alarms = []
            else:
                dns_alarms = [a for a in alarms
                              if _DNS_ALARM_TEST_RE.search(a.get("summary") or "")]
                test_lines: List[str] = []
                tested_domains: set = set()
                tested_keys: set = set()   # domain|server|rt 去重: 相同告警批量出现时只测一次
            if dns_alarms:
                client = get_dns_client()

                async def _test_one(domain: str, rt: str, server: str) -> dict:
                    try:
                        res = await asyncio.wait_for(
                            asyncio.to_thread(client.query, domain, rt, server),
                            timeout=15)
                        # 编码域名 (无点) 且无记录时, 尝试还原常见 TLD 后缀再测 (Zabbix 监控项编码)
                        if res.get("success") and "." not in domain and not any(
                                r.get("result") for r in (res.get("successful_results") or [])):
                            from agents.dns_api import restore_encoded_domain
                            for cand in restore_encoded_domain(domain)[1:]:
                                res2 = await asyncio.wait_for(
                                    asyncio.to_thread(client.query, cand, rt, server),
                                    timeout=15)
                                if res2.get("success") and any(
                                        r.get("result") for r in (res2.get("successful_results") or [])):
                                    res2["_restored_from"] = domain
                                    return res2
                        return res
                    except Exception as e:
                        return {"success": False, "error": f"解析测试异常: {e}"}

                # 遍历全部 DNS 告警，按去重目标数限量 (避免同质重复告警占满名额
                # 而漏掉排在后面的不同告警类型，如 RecursiveDNS 指定 server 用例)
                _MAX_TEST_TARGETS = 8
                for a in dns_alarms:
                    label = f"[{a.get('severity', '?')}/{a.get('source', '?')}]"
                    for t in extract_dns_test_targets(a.get("summary") or "")[:3]:
                        key = f"{t['domain'].lower()}|{t['server']}|{t['record_type']}"
                        if key in tested_keys:
                            continue
                        tested_keys.add(key)
                        tested_domains.add(t["domain"].lower())
                        res = await _test_one(t["domain"], t["record_type"], t["server"])
                        test_lines.append(format_dns_test_result(
                            t["domain"], t["record_type"], t["server"], res, label))
                        if len(tested_keys) >= _MAX_TEST_TARGETS:
                            break
                    if len(tested_keys) >= _MAX_TEST_TARGETS:
                        break
            dns_block = ""
            if test_lines:
                logger.info(f"[nms_agent] DNS 专项告警解析测试: {len(test_lines)} 项")
                dns_block = (
                    "【DNS 告警解析测试（OPS-CORE DNS API 实测）】\n"
                    + "\n".join(test_lines[:20]) + "\n"
                    + "说明：以上为告警涉及域名在指定 DNS 服务器上的实测解析结果，"
                      "用于判断解析异常是否仍存在及影响范围。\n"
                )

            # ── 2. 通用 IP/域名富化 (排除专项已测域名，避免重复) ──
            text_parts = []
            for a in alarms[:200]:
                for k in ("summary", "node", "node_alias"):
                    v = a.get(k)
                    if v:
                        text_parts.append(str(v))
            if not text_parts:
                return dns_block
            ips, domains = extract_targets("\n".join(text_parts),
                                           max_items=config.NMS_DNS_RESOLVE_MAX)
            domains = [d for d in domains if d.lower() not in tested_domains]
            if not ips and not domains:
                return dns_block
            result = await asyncio.wait_for(resolve_targets(ips, domains), timeout=timeout_s)
            report = format_resolve_report(result)
            if not report:
                return dns_block
            logger.info(f"[nms_agent] DNS 解析富化: {len(result.get('resolved', {}))} 个目标解析成功 "
                        f"(ips={len(ips)}, domains={len(domains)})")
            return (
                dns_block +
                f"【DNS 解析信息（自动富化，供告警对象定位参考）】\n{report}\n"
                f"说明：解析结果来自 OPS-CORE DNS 平台，用于辅助判断告警对象的主机名/IP 归属；"
                f"无记录/解析失败属正常现象，不要据此推断故障。\n"
            )
        except asyncio.TimeoutError:
            logger.debug(f"[nms_agent] DNS 解析富化超时 (>{timeout_s}s)，跳过 (不影响分析)")
            return ""
        except Exception as e:
            logger.debug(f"[nms_agent] DNS 解析富化失败 (忽略): {e}")
            return ""

    # ────────────────────────────────────────────────────────────────────────
    # Phase 4 (智能化增强): 三类"智能原料"格式化器 — 纯函数, 便于单测
    # 原料: ① 关联降噪事件簇 ② SSL/备份/TCP 实测证据 ③ 历史重复模式
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _nms_format_incident_clues(incidents_doc: Optional[dict], top_n: int = 10) -> str:
        """事件簇文档 → 注入 prompt 的「关联线索」块。

        LLM 只能看到去重后的 ~45 条样本, 无法自行发现 6054 个簇的归并结构;
        把 top 簇 (按成员数) 以线索形式给出, 供根因假设引用。
        """
        if not incidents_doc or not isinstance(incidents_doc, dict):
            return ""
        incidents = incidents_doc.get("incidents") or []
        if not incidents:
            return ""
        # 排序: 系统级优先(跨源=共性根因信号最强), 再按成员数
        ranked = sorted(
            incidents,
            key=lambda i: (i.get("category") != "systemic",
                           -(i.get("member_count") or 0)))
        lines = []
        for inc in ranked[:top_n]:
            cat = "系统级" if inc.get("category") == "systemic" else "对象级"
            srcs = ", ".join((inc.get("sources") or [])[:5])
            lines.append(
                f"- [{cat}|{inc.get('severity', '?')}] {inc.get('title', '')[:80]} "
                f"| 成员 {inc.get('member_count', 0)} 条 / 跨源 {inc.get('source_count', 0)} "
                f"| 代表来源: {srcs or '—'}")
        total = incidents_doc.get("total_alarms") or 0
        cnt = incidents_doc.get("incident_count") or len(incidents)
        return ("【关联降噪事件簇（规则引擎已归并，根因推断的强线索）】\n"
                f"原始 {total} 条告警已归并为 {cnt} 个事件簇，以下为 TOP 簇"
                f"（系统级=同一摘要跨多源出现，指向共性根因；对象级=同一对象反复告警）：\n"
                + "\n".join(lines) + "\n")

    @staticmethod
    def _nms_format_tool_evidence(tool_checks: Optional[dict], max_items: int = 12) -> str:
        """快照 tool_checks (ssl/backup/tcp) → 注入 prompt 的「实测证据」块。

        实测结果让 LLM 能区分「告警仍存在」与「已自愈」——例如 SSL 证书告警
        但实测剩余天数充足 → 误报/已续期; 备份告警但实测 ok → 已恢复。
        dns 由专用 dns_context 注入, 此处只处理 ssl/backup/tcp。
        """
        if not tool_checks or not isinstance(tool_checks, dict):
            return ""
        lines = []
        ssl = tool_checks.get("ssl") or []
        for t in ssl[:max_items]:
            st = t.get("status")
            if st == "ok":
                lines.append(f"- [SSL·证书可读] {t.get('summary', '')} → 旧采集仅读取证书、未校验证书链/域名，不等于证书健康，不能据此判定恢复")
            elif st == "warn":
                lines.append(f"- [SSL·即将到期] {t.get('summary', '')}")
            elif st == "expired":
                lines.append(f"- [SSL·已过期] {t.get('summary', '')}")
            else:
                lines.append(f"- [SSL·无法核验] {t.get('summary', '')}")
        backup = tool_checks.get("backup") or []
        for t in backup[:max_items]:
            lines.append(f"- [备份·{t.get('status', '?')}] {t.get('summary', '') or t.get('label', '')}")
        tcp = tool_checks.get("tcp") or []
        for t in tcp[:max_items]:
            lines.append(f"- [TCP·{t.get('status', '?')}] {t.get('summary', '')}")
        if not lines:
            return ""
        return ("【工具实测证据（本轮抓取时的真实探测结果，优先于告警文本本身）】\n"
                + "\n".join(lines)
                + "\n说明：实测为抓取时刻的真实状态。若告警活跃但实测正常，需考虑已自愈/误报；"
                  "若实测异常，告警可信度高，应直接进入处置。\n")

    # ────────────────────────────────────────────────────────────────────────
    # A5 告警抑制: pattern 命中阈值 → 静默 (飞书不推送)
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _nms_apply_suppression(alarms: List[Dict],
                               rules: Dict[str, Dict],
                               maint_windows: Optional[Dict[str, float]] = None
                               ) -> tuple:
        """纯函数: 返回 (pass_through, suppressed, maintenance)。

        Args:
            alarms: 待发送的告警列表
            rules: {rule_id: {pattern, max_hits, window_min, until_epoch, ...}}
                   + 计数器 _hits: {rule_id: 当前窗口命中数}
            maint_windows: {source: until_epoch} 维护窗口静音

        Returns:
            (kept, suppressed, in_maint)
            kept: 通过 (须推送)
            suppressed: 被 pattern 抑制的告警 (附 reason)
            in_maint: 在维护窗口内的告警 (附 until)
        """
        import time as _time
        now = _time.time()
        kept, suppressed, in_maint = [], [], []
        # 索引告警到 rule 命中
        for a in alarms or []:
            summary = (a.get("summary") or "").lower()
            source = (a.get("source") or "").strip()
            # 维护窗口静音
            if maint_windows and source in maint_windows:
                until = float(maint_windows[source])
                if now < until:
                    in_maint.append((a, until))
                    continue
            # 规则抑制
            matched = False
            for rid, rule in (rules or {}).items():
                pat = rule.get("pattern", "")
                if pat and re.search(pat, summary, re.IGNORECASE):
                    suppressed.append((a, rid, rule.get("reason", "") or rid))
                    matched = True
                    break
            if not matched:
                kept.append(a)
        return kept, suppressed, in_maint

    @staticmethod
    def _nms_format_impact_block(devices, impact_data, all_ips) -> str:
        """CMDB 设备 + 客户影响 → 注入 prompt 的【资产与影响面】块。

        同一段代码也在 Checklist 端点用过 (api/routers/agents.py), 这里精简版
        只输出对根因分析最有用的几个字段: 厂商/型号/站点/客户/虚机数。
        """
        if not devices and not impact_data:
            return ""
        lines = []
        for d in devices or []:
            if not d:
                continue
            if d.get("status") in ("not_found", "OPS-CORE-MGT 资产查询失败"):
                continue
            vendor = d.get("vendor") or ""
            model = d.get("model") or ""
            site = d.get("site") or d.get("location") or "—"
            name = d.get("name") or d.get("hostname") or d.get("key", "?")
            lines.append(f"- {name} | 厂商/型号: {vendor} {model} | 站点: {site}")
        customers = (impact_data or {}).get("customers") or {}
        if customers:
            sorted_c = sorted(customers.items(), key=lambda kv: -kv[1])[:6]
            lines.append("- 影响客户 TOP: " + " | ".join(
                f"{n}×{c}台" for n, c in sorted_c))
        if impact_data.get("vms"):
            lines.append(f"- 关联客户虚机: {impact_data['vms']} 台"
                         f"（{impact_data.get('src', '') or 'MCP 资源平台查询'}）")
        if not lines:
            return ""
        return ("【资产与影响面（CMDB 实查，决定告警的业务影响范围）】\n"
                + "\n".join(lines) + "\n")

    @staticmethod
    def _nms_format_history_recurring(new_alarms: List[Dict],
                                      history_items: Optional[list],
                                      min_repeat: int = 3,
                                      max_patterns: int = 8) -> str:
        """历史库 → 「重复故障模式」块: 当前活跃模式在过去历史中出现 ≥ min_repeat 次
        即视为重复故障, 提示 LLM 升级为长期治理项而非重复救火。
        """
        if not history_items or not new_alarms:
            return ""
        # 统计历史库中每个模式指纹的出现次数与最近时间
        hist_count: Dict[str, int] = {}
        hist_last: Dict[str, str] = {}
        for h in history_items:
            fp = NmsAgent._nms_pattern_key(h)
            # 内容键去重后, Serial 换手折叠进 occurrences — 按真实发生次数加权
            try:
                w = max(1, int(h.get("occurrences", 1)))
            except (ValueError, TypeError):
                w = 1
            hist_count[fp] = hist_count.get(fp, 0) + w
            lt = h.get("last_time") or h.get("time") or ""
            if lt and (not hist_last.get(fp) or lt > hist_last[fp]):
                hist_last[fp] = lt
        # 当前活跃告警的模式 → 命中历史的报重复
        cur_patterns: Dict[str, int] = {}
        for a in new_alarms:
            fp = NmsAgent._nms_pattern_key(a)
            cur_patterns[fp] = cur_patterns.get(fp, 0) + 1
        lines = []
        for fp, cur_n in sorted(cur_patterns.items(),
                                key=lambda kv: -hist_count.get(kv[0], 0)):
            n = hist_count.get(fp, 0)
            if n < min_repeat:
                continue
            sample = next((a for a in new_alarms
                           if NmsAgent._nms_pattern_key(a) == fp), None)
            summ = _NMS_HISTORY_SUMMARY_RE.sub("", (sample or {}).get("summary") or "")[:60] \
                   or fp[:60]
            lines.append(
                f"- 「{summ}」当前 {cur_n} 条 | 历史库中出现 {n} 次"
                f"（最近 {hist_last.get(fp, '—')}）→ 疑似重复/顽固故障")
            if len(lines) >= max_patterns:
                break
        if not lines:
            return ""
        return ("【历史重复模式（来自告警历史库，识别顽固/反复故障）】\n"
                + "\n".join(lines)
                + "\n说明：上列模式在历史中反复出现，单纯重复处理无法收敛；"
                  "应在优先处理建议中给出根治方向（而非再次临时处置）。\n")

    async def _nms_build_analyze_prompt(self, new_alarms: List[Dict],
                                        all_alarms: List[Dict],
                                        dns_tests: Optional[List[Dict]] = None,
                                        kind: str = "on_demand",
                                        base: str = "",
                                        tool_checks: Optional[Dict] = None) -> tuple:
        """构造 LLM 分析 prompt + system (Phase 1.3 抽出供流式/非流式共用)。

        Phase 4 智能化增强: ``base`` 提供时自动注入
          ① 关联降噪事件簇线索 ② 历史重复模式 ③ SSL/备份/TCP 实测证据。

        Returns:
            (prompt, system, need_kb) 三元组。need_kb=True 时调用方应在
            末尾追加 KB 章节;prompt 自身已包含 KB 主体。
        """
        # 清洗摘要中的二进制/控制字符乱码（如 BGP Peer 字段夹带的乱码），避免污染分析
        def _clean(txt: str) -> str:
            if not txt:
                return ""
            t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]", "", txt)
            t = re.sub(r"\(\s*BGP Peer:[^)]*\)", "(BGP Peer: <乱码>)", t)
            t = re.sub(r"__+", "_", t)
            return re.sub(r"\s{2,}", " ", t).strip()

        _cl_entries = await _nms_merged_checklist()

        def _slim(alarm):
            d = {
                "级别": alarm.get("severity", "unknown"),
                "来源": alarm.get("source", ""),
                "摘要": _clean(alarm.get("summary") or "")[:120],
                "时间": alarm.get("time", ""),
                "状态": "已确认" if str(alarm.get("status") or alarm.get("acknowledged") or "") == "1" else "未确认",
            }
            title = alarm.get("checklist") or _nms_checklist_match(alarm.get("summary") or "", _cl_entries)
            if title:
                d["预案"] = title
            return d

        kb_context = await self._nms_kb_remediation_context(new_alarms, all_alarms)
        dns_context = await self._nms_dns_resolve_context(
            new_alarms or all_alarms, cached_tests=dns_tests)

        _active_sev = {"critical", "major", "minor", "warning", "indeterminate"}
        _sev_order = ["critical", "major", "minor", "warning",
                      "indeterminate", "clear", "cleared", "unknown"]
        active_alarms = [a for a in all_alarms if a.get("severity") in _active_sev]
        cleared_count = len(all_alarms) - len(active_alarms)
        new_active = [a for a in new_alarms if a.get("severity") in _active_sev]

        sev_stats = {}
        for a in all_alarms:
            s = a.get("severity", "unknown")
            sev_stats[s] = sev_stats.get(s, 0) + 1
        src_stats = {}
        for a in active_alarms:
            src = (a.get("source") or "").strip()
            if not src:
                continue
            src_stats[src] = src_stats.get(src, 0) + 1
        top_src = sorted(src_stats.items(), key=lambda x: -x[1])[:8]
        acked_total = sum(1 for a in all_alarms
                          if str(a.get("acknowledged") or a.get("status") or "") == "1")

        def _representative(items, per_sev=10, total=40):
            order = {s: i for i, s in enumerate(_sev_order)}
            ordered = sorted(items, key=lambda a: order.get(a.get("severity"), 99))
            picked, seen_summ, per = [], set(), {}
            for a in ordered:
                sv = a.get("severity", "unknown")
                key = (a.get("summary") or "")[:40]
                if key and key in seen_summ:
                    continue
                if per.get(sv, 0) >= per_sev:
                    continue
                seen_summ.add(key)
                per[sv] = per.get(sv, 0) + 1
                picked.append(a)
                if len(picked) >= total:
                    break
            return picked

        new_brief = [_slim(a) for a in _representative(new_active or new_alarms, per_sev=12, total=30)]
        all_brief = [_slim(a) for a in _representative(active_alarms, per_sev=10, total=45)]

        checklist_ctx = _nms_checklist_context(
            active_alarms, entries=await _nms_merged_checklist())
        checklist_block = (
            f"【OPS-CORE Checklist 处置预案（命中当前告警的标准检查步骤）】\n{checklist_ctx}\n\n"
            if checklist_ctx else ""
        )
        det_block = ""
        if getattr(config, "DETERMINISTIC_FIRST", True):
            try:
                from core.deterministic import build_conclusion_table, format_conclusion_block
                det_table = build_conclusion_table(
                    active_alarms,
                    match_fn=lambda s: _nms_checklist_match(s, _cl_entries))
                det_block = format_conclusion_block(det_table)
                if det_block:
                    det_block += "\n\n"
                    logger.info(f"[NMS] 确定性结论表: {len(det_table)} 组 "
                                f"(P0={sum(1 for g in det_table if g['priority']=='P0')})")
            except Exception as e:
                logger.debug(f"[NMS] 确定性结论生成失败, 回退 LLM 自主分析: {e}")
        dns_block = f"{dns_context}\n\n" if dns_context else ""
        det_instruction = (
            "本报告的分类/预案归属/优先级/分组已由【规则引擎确定性结论】给定："
            "请严格按结论表组织“## 二、根因假设与验证”与“## 三、优先处理建议”的分组与优先级"
            "（对象、数量、预案归属与结论表一一对应）；你只负责解释措辞与补充分析叙述，"
            "不得把告警移到其他类别或预案、不得改动 P0/P1 定级。\n\n"
        ) if det_block else ""

        # ── Phase 4 智能化增强: 拉取三类"智能原料" (best-effort, 失败静默跳过) ──
        incident_block = ""
        history_block = ""
        evidence_block = ""
        impact_block = ""
        anomaly_block = ""
        if base:
            try:
                r_int = await self.get_redis()
                inc_raw = await r_int.get(f"{base}:incidents")
                if inc_raw:
                    incident_block = self._nms_format_incident_clues(json.loads(inc_raw))
                    if incident_block:
                        incident_block += "\n"
                # 读历史 (Phase H: 优选 ZSET 时间窗口, 降级老 LIST)
                # 按 NMS_ANOMALY_HISTORY_WINDOW_DAYS 取最近窗口,
                # 既给 A3 异常检测足够 baseline, 又控制数据量
                window_days = max(1, getattr(config,
                                            "NMS_ANOMALY_HISTORY_WINDOW_DAYS", 14))
                now_ts = time.time()
                cutoff_ts = now_ts - window_days * 86400
                hist_parsed: List = []
                # 主路径: ZSET 时间范围读取 (member=内容键 → history_data 取 payload)
                try:
                    raw_z = await r_int.zrangebyscore(
                        f"{base}:history_z", cutoff_ts, "+inf",
                        withscores=False)
                    if raw_z:
                        vals = await r_int.hmget(f"{base}:history_data", raw_z)
                        for v in vals:
                            if not v:
                                continue
                            try:
                                hist_parsed.append(json.loads(v))
                            except Exception:
                                pass
                except Exception:
                    pass
                # 降级: 旧 list 兼容 (如果 ZSET 还没数据)
                if not hist_parsed:
                    hist_items = await r_int.lrange(f"{base}:history", 0, -1)
                    for h in hist_items or []:
                        try:
                            hist_parsed.append(json.loads(h))
                        except Exception:
                            pass
                if hist_parsed:
                    history_block = self._nms_format_history_recurring(
                        new_active or new_alarms or active_alarms, hist_parsed)
                    if history_block:
                        history_block += "\n"
                    # A3: 异常活跃检测 (复用已解析的 history)
                    try:
                        from core.nms_anomaly import (
                            compute_anomaly_scores, format_anomaly_block)
                        anomalies = compute_anomaly_scores(
                            hist_parsed, active_alarms)
                        anomaly_block = format_anomaly_block(anomalies)
                        if anomaly_block:
                            anomaly_block += "\n"
                    except Exception as e:
                        logger.debug(f"[NMS] 异常活跃检测失败 (忽略): {e}")
                # ── A1 影响面 (CMDB 资产 + 客户影响) ──
                # 抽到 agents/nms_cmdb.py 避开 api.routers.agents 的循环导入
                if active_alarms:
                    try:
                        from agents.nms_cmdb import (
                            _nms_lookup_cmdb_devices,
                            _nms_mcp_customer_impact,
                        )
                        ips = list(dict.fromkeys(
                            (a.get("source") or a.get("node") or "").strip()
                            for a in active_alarms
                            if (a.get("source") or a.get("node") or "").strip()
                        ))[:12]
                        devices, impact_data = [], {"vms": 0, "customers": {}}
                        if ips:
                            devices = await _nms_lookup_cmdb_devices(r_int, ips[:8])
                            impact_data = await _nms_mcp_customer_impact(r_int, ips[:8])
                        impact_block = self._nms_format_impact_block(
                            devices, impact_data, ips)
                        if impact_block:
                            impact_block += "\n"
                    except Exception as e:
                        logger.debug(f"[NMS] 影响面查询失败 (忽略): {e}")
            except Exception as e:
                logger.debug(f"[NMS] 智能原料拉取失败 (忽略): {e}")
        evidence_block = self._nms_format_tool_evidence(tool_checks)
        if evidence_block:
            evidence_block += "\n"

        # 异常活跃摘要 (简短一句, 给章节一作为可选内容, 避免 LLM 自创)
        auto_anomalies_line = ""
        if anomaly_block:
            try:
                from core.nms_anomaly import compute_anomaly_scores
                _ = compute_anomaly_scores  # 已用过, 这里只是确认能 import
                # 从已有 anomaly_block 文本中提取 source 名 (取首行 "  - src: ..." 即可)
                for ln in anomaly_block.splitlines():
                    if ln.startswith("- ") and ":" in ln:
                        first = ln[2:].split(":")[0].strip()
                        auto_anomalies_line = f"异常活跃源 {first}"
                        break
            except Exception:
                pass

        prompt = (
            f"请对以下 IBM Netcool 网管系统的告警数据做运维分析，直接输出最终报告。\n\n"
            f"【统计概览】\n"
            f"- 告警总数 {len(all_alarms)} 条（活跃 {len(active_alarms)} / 已清除 {cleared_count}），本次新增 {len(new_alarms)} 条（其中活跃 {len(new_active)}）\n"
            f"- 按级别分布: {json.dumps(sev_stats, ensure_ascii=False)}\n"
            f"- 确认状态: 已确认 {acked_total} 条 / 未确认 {len(all_alarms) - acked_total} 条（未确认告警需优先关注）\n"
            f"- 活跃告警来源TOP: {json.dumps(dict(top_src), ensure_ascii=False)}\n\n"
            f"【新增告警样本(去重·覆盖各级别)】\n{json.dumps(new_brief, ensure_ascii=False, indent=1)}\n\n"
            f"【活跃告警代表样本(去重·覆盖各级别)】\n{json.dumps(all_brief, ensure_ascii=False, indent=1)}\n\n"
            f"{incident_block}"
            f"{impact_block}"
            f"{evidence_block}"
            f"{anomaly_block}"
            f"{history_block}"
            f"{checklist_block}"
            f"{det_block}"
            f"{dns_block}"
            f"注意：上方样本为去重后的代表，请结合统计数字判断规模；分析聚焦活跃告警，已清除告警无需处理。\n"
            f"分析原则：事件簇线索 > 工具实测 > 告警文本 — 三者矛盾时按此优先级采信并在报告中说明矛盾。\n\n"
            f"{det_instruction}"
            f"严格按以下 Markdown 结构输出，第一行必须是“## 一、告警概览”，禁止在其之前输出任何文字：\n"
            f"## 一、告警概览（≤4 行, 只谈数字, 不引根因/事件簇/异常源/具体设备）\n"
            f"模板（直接套用, 数字从统计概览替换, 不要扩写）:\n"
            f"  共 N 条告警（活跃 X / 已清除 Y），新增 Z 条；"
            f"级别 critical X / major Y / minor Z / warning W；未确认 K 条。"
            f"{'此外 ' + auto_anomalies_line + '。' if auto_anomalies_line else ''}\n"
            f"禁止事项（章节一只准谈数字, 严禁）:\n"
            f"- 列出具体事件簇标题/源/设备名（属章节二）\n"
            f"- 复述告警摘要模板（如“路由/标签交换邻接类”、“电源硬件类”）\n"
            f"- 用“本次...共...其中...整体以...构成...核心...”这种话术把数字扩写成段落\n"
            f"- 重复章节二/三/四会展开的内容\n"
            f"## 二、根因假设与验证\n"
            f"（这是报告的核心章节。基于事件簇/实测/样本给出根因推断：\n"
            f"  - 主假设：最可能的根因（1-2 个），说明因果链「根因 → 直接受影响对象 → 级联告警」；\n"
            f"  - 次假设：备选解释（如计划内维护/割接窗口），并给出区分主/次假设的判据；\n"
            f"  - 验证步骤：每个假设给出 2-3 条可立即执行的验证动作（登录哪台设备看什么、跑什么命令）；\n"
            f"  - 无充分证据不得编造因果；证据不足时明确写「证据不足，需先执行验证步骤」。\n）\n"
            f"## 三、优先处理建议（分析方向 + 修复检查方向）\n"
            f"（按告警类别列出需处理项，每项以 **P0** / **P1** 开头，标明对象，并分三行给出：\n"
            f"  - 分析方向：先排查什么、如何定位根因（如区分计划内维护/硬件/网络/应用层）；\n"
            f"  - 修复检查方向：具体检查步骤；\n"
            f"  - 证据：支撑本项的告警对象 / 实测结果 / 事件簇标题（缺一不可省略此行）。\n"
            f"预案使用铁律（必须遵守）：\n"
            f"1. 只有样本中带“预案”字段的告警，才能引用对应的【OPS-CORE Checklist 处置预案】步骤，并在行尾标注〔Checklist·故障名称〕；\n"
            f"2. 严禁把预案套用到没有“预案”字段的告警上，严禁把不同预案的告警合并为同一项；\n"
            f"3. 无“预案”字段的告警标注〔Checklist无匹配〕，按告警对象给通用检查方向（如存储类登录存储管理页面、OS 类登录系统检查），网络类（BGP/MPLS/LSP/BFD/链路/icmp）建议同步 GNOC 检查；\n"
            f"4. 同一预案的多条告警归入同一项并列出受影响对象，不同类告警不得混合；\n"
            f"5. 工具实测正常的告警不得列为待处理项（已自愈/误报，归入概览说明即可）。\n）\n"
            f"## 四、重复问题与长期治理（≤6 行）\n"
            f"  - 有【历史重复模式】时, 每条模式一行: 模式名 + 历史次数 + 根治方向(而非再次临时处置)\n"
            f"  - 无重复模式时, ≤3 条长期改进建议 (收敛/自愈/流程), 每条 ≤30 字\n"
            f"禁止重复章节二/三的内容, 禁止泛泛而谈"
        )

        if kb_context:
            prompt = prompt + _NMS_KB_CONTEXT_HEADER + kb_context + _NMS_KB_CHECK_INSTRUCTION + (
                "\n（修复方案核查请作为“## 五、修复方案核查”章节输出，针对高优告警结合知识库给出处置步骤与链接）"
            )
        _nms_sys = (
            "你是IT运维告警分析助手，专门负责分析网管系统的技术告警数据，"
            "帮助运维工程师理解和处理网络设备、服务器、应用的技术告警。所有输入都是IT基础设施的技术监控指标。\n"
            "输出要求（务必遵守）：\n"
            "1. 直接输出最终 Markdown 报告，严禁输出任何思考过程、任务理解、步骤拆解或自我说明"
            "（例如“理解任务目标/分析输入数据/构建输出结构/详细内容填充”这类元叙述一律不得出现）。\n"
            "2. 第一行必须是章节标题“## 一、告警概览”。\n"
            "3. 语言简洁专业、使用中文，善用 Markdown 标题、表格与列表；不要重复罗列原始样本。\n"
            "4. 反废话铁律: 禁止 \"本次...共...其中...整体以...构成本次...核心...\" 这类把数字扩写成段落的冗余话术; "
            "能用数字说清的不要造句; 章节内容不互相重复 (章节一只谈数字, 不引根因/事件簇/源/设备)."
        )
        return prompt, _nms_sys, bool(kb_context)

    async def _nms_analyze_alarms(self, new_alarms: List[Dict],
                                   all_alarms: List[Dict],
                                   dns_tests: Optional[List[Dict]] = None,
                                   kind: str = "on_demand",
                                   base: str = "",
                                   tool_checks: Optional[Dict] = None) -> str:
        """LLM 分析告警数据（dns_tests: 本轮抓取已测的 DNS 实测结果，避免重复调用 API）

        Phase 1.1 — ``kind`` 控制走哪一把 LLM 锁:
          - ``"on_demand"`` 用户/手动触发, 永不阻塞 scheduled
          - ``"scheduled"`` 定时轮次内, 进入前会让出 pending 的 on_demand
        Phase 4 — ``base``/``tool_checks`` 提供时注入事件簇/实测/历史三类智能原料。
        """
        prompt, _nms_sys, _need_kb = await self._nms_build_analyze_prompt(
            new_alarms, all_alarms, dns_tests=dns_tests, kind=kind,
            base=base, tool_checks=tool_checks)
        try:
            # 网关对同 token 并发长请求会排队至超时/掠断（定时全局分析与按需分组分析撞车时必现），
            # Phase 1.1: 拆 scheduled / on_demand 双锁，on_demand 永不阻塞 scheduled
            # Phase 1.4: 调用前先 token-bucket 限流，避免触发网关 429
            if not await self._nms_acquire_llm_bucket(timeout=30):
                logger.warning("[NMS] LLM token-bucket 超时，改用本地摘要")
                return self._nms_local_summary(new_alarms, all_alarms)
            async with self._nms_acquire_llm_lock(kind) as got_lock:
                if not got_lock:
                    # scheduled 让位 on_demand, 静默回退到本地摘要
                    logger.info("[NMS] scheduled 让位于 on_demand, 改用本地摘要")
                    return self._nms_local_summary(new_alarms, all_alarms)
                result = await self.llm_chat(prompt=prompt, system=_nms_sys,
                                              max_tokens=config.NMS_LLM_MAX_TOKENS)
        except Exception as e:
            logger.error(f"[NMS] LLM 分析失败: type={type(e).__name__}, err={e}")
            result = ""

        # 该网关 deepseek-v3-0324 为推理模型，会消耗 ~1.3k reasoning tokens，叠加 KB 章节后
        # 输出易触顶 max_tokens 导致截断；另外网关偶发 ~90s 读超时直接抛错。
        # 两种情况都降温重试一次，避免因瞬时抖动落入纯统计摘要回退；
        # 但网关 429 限流 (分钟级窗口) 时重试只会加重网关压力，直接走本地摘要
        # (_need_kb 由 _nms_build_analyze_prompt 返回, kb 上下文已并入 prompt)
        if self._nms_llm_unusable(result) or self._nms_llm_incomplete(result, _need_kb):
            if self._nms_llm_rate_limited(result):
                logger.warning(f"[NMS] LLM 分析遇网关限流 (429)，跳过降温重试，改用本地摘要")
            else:
                logger.warning(f"[NMS] LLM 分析失败/疑似截断 (len={len(str(result))})，退避后降温重试一次")
                try:
                    await asyncio.sleep(10)  # 给网关/前一请求残留连接一点释放时间
                    if not await self._nms_acquire_llm_bucket(timeout=15):
                        logger.warning("[NMS] 重试前 token-bucket 超时，跳过重试")
                        raise RuntimeError("llm_bucket_timeout")
                    async with self._nms_acquire_llm_lock(kind) as got_lock:
                        if not got_lock:
                            logger.info("[NMS] 重试让位于 on_demand, 跳过")
                            raise RuntimeError("lock_yield")
                        retry = await self.llm_chat(prompt=prompt, system=_nms_sys,
                                                    temperature=0.3,
                                                    max_tokens=config.NMS_LLM_MAX_TOKENS)
                    if not self._nms_llm_unusable(retry) and (
                            self._nms_llm_unusable(result) or len(str(retry)) > len(str(result))):
                        result = retry
                except Exception as e:
                    logger.error(f"[NMS] LLM 分析重试失败: {e}")

        # 最终回退判定：仅当结果不可用、或连核心分析章节都未覆盖时才回退本地摘要；
        # 若已含丰富分析（即使结尾略被截断），保留 LLM 结果，价值远高于纯统计摘要。
        if self._nms_llm_unusable(result) or not self._nms_has_core_sections(result):
            preview = str(result)[:60]
            logger.warning(f"[NMS] LLM 分析不可用/缺核心章节，改用本地摘要 (len={len(str(result))}, 返回: {preview})")
            return self._nms_local_summary(new_alarms, all_alarms)
        return result

    @staticmethod
    def _nms_llm_unusable(result) -> bool:
        """判断 LLM 返回是否不可用（空/调用失败/被网关内容安全策略拒绝）"""
        if not result or not str(result).strip():
            return True
        markers = (
            "LLM 调用失败", "LLM 分析失败", "无法回答", "不安全内容",
            "抱歉，我无法", "content_filter", "内容安全",
        )
        return any(m in result for m in markers)

    @staticmethod
    def _nms_llm_rate_limited(result) -> bool:
        """判断 LLM 返回是否禁止重试：网关限流 (429) 或内容安全拒绝。
        两类情况重试必然同样失败，纯浪费 token 与时间。"""
        if not result:
            return False
        return any(m in str(result) for m in (
            "429", "Too Many Requests", "限流",
            "不安全内容", "内容安全", "提示词注入", "content_filter"))

    @staticmethod
    def _nms_has_core_sections(result) -> bool:
        """判断 LLM 结果是否已覆盖核心分析章节（有实际分析价值）。
        只要抵达根因/优先处理/治理章节中的任一章节，即使结尾被截断也保留（优于纯统计回退）。"""
        if not result:
            return False
        txt = str(result).strip()
        if len(txt) < 400:
            return False
        return any(m in txt for m in (
            "根因关联", "根因假设", "优先处理", "运维改进", "运维建议", "长期治理",
            "## 二", "## 三", "## 四",
        ))

    @staticmethod
    def _nms_llm_incomplete(result, require_kb: bool = False) -> bool:
        """判断 LLM 返回是否被截断/不完整（网关偶发中断流式输出，仅返回开头段落）。
        提示词要求固定章节结构：一、告警概览 → … → 四、重复问题与长期治理（含 KB 时额外要到五、修复方案核查）。"""
        if not result:
            return True
        txt = str(result).strip()
        if len(txt) < 280:
            return True
        # 含 KB 时，完整报告必然抵达“修复方案核查”（第五节）
        if require_kb:
            return not any(m in txt for m in ("修复方案核查", "## 五", "五、修复"))
        # 无 KB 时，完整报告以第四节收尾 (重复问题与长期治理 / 旧版运维改进)
        tail_markers = ("长期治理", "运维改进建议", "运维建议", "## 四", "四、运维", "四、重复")
        return not any(m in txt for m in tail_markers)

    def _nms_local_summary(self, new_alarms: List[Dict],
                            all_alarms: List[Dict]) -> str:
        """本地规则化告警摘要（LLM 不可用时的回退，保证分析标签页始终有内容）"""
        from datetime import datetime
        sev_order = ["critical", "major", "minor", "warning",
                     "indeterminate", "clear", "cleared", "unknown"]
        sev_label = {
            "critical": "🔴 严重", "major": "🟠 重要", "minor": "🟡 次要",
            "warning": "⚠️ 警告", "indeterminate": "⚪ 未知", "clear": "✅ 清除",
            "cleared": "✅ 已清除", "unknown": "⚪ 未分类",
        }
        sev_count = {}
        for a in all_alarms:
            s = a.get("severity", "unknown")
            sev_count[s] = sev_count.get(s, 0) + 1

        total = len(all_alarms) or 1
        _active_sev = {"critical", "major", "minor", "warning", "indeterminate"}
        active_n = sum(v for k, v in sev_count.items() if k in _active_sev)
        cleared_n = len(all_alarms) - active_n
        lines = ["## 📊 告警统计摘要", "",
                 f"> 自动生成于 {datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M')} | 告警总数 {len(all_alarms)} 条（活跃 {active_n} / 已清除 {cleared_n}），本次新增 {len(new_alarms)} 条", "",
                 "### 严重级别分布", "",
                 "| 级别 | 数量 | 占比 |",
                 "| --- | ---: | ---: |"]
        for s in sev_order:
            if s in sev_count:
                pct = f"{sev_count[s]/total*100:.0f}%"
                lines.append(f"| {sev_label.get(s, s)} | {sev_count[s]} | {pct} |")
        lines.append(f"| **合计** | **{len(all_alarms)}** | **100%** |")

        # 告警来源分析（含最高严重级别）
        src_count = {}
        src_sev = {}
        sev_rank = {s: i for i, s in enumerate(sev_order)}
        for a in all_alarms:
            src = a.get("source") or "未知来源"
            src_count[src] = src_count.get(src, 0) + 1
            cur_sev = a.get("severity", "unknown")
            if src not in src_sev or sev_rank.get(cur_sev, 99) < sev_rank.get(src_sev[src], 99):
                src_sev[src] = cur_sev
        top_src = sorted(src_count.items(), key=lambda x: -x[1])[:8]
        if top_src:
            lines += ["", "### 告警来源 TOP 8", "",
                      "| 来源 | 告警数 | 最高级别 |",
                      "| --- | ---: | --- |"]
            for src, cnt in top_src:
                lines.append(f"| {src} | {cnt} | {sev_label.get(src_sev.get(src, 'unknown'), '?')} |")

        # 确认状态分布（是否已 ACK 确认）
        acked_n = sum(1 for a in all_alarms
                      if str(a.get("acknowledged") or a.get("status") or "") == "1")
        unacked_n = len(all_alarms) - acked_n
        lines += ["", "### 确认状态分布", "",
                  "| 确认状态 | 告警数 | 占比 |",
                  "| --- | ---: | ---: |",
                  f"| 未确认 | {unacked_n} | {unacked_n/total*100:.0f}% |",
                  f"| 已确认 | {acked_n} | {acked_n/total*100:.0f}% |"]

        # 新增告警明细
        if new_alarms:
            lines += ["", f"### 🆕 新增告警（{len(new_alarms)} 条）", ""]
            for a in new_alarms[:15]:
                sv = sev_label.get(a.get("severity", "unknown"), "?")
                summ = a.get("summary") or (a.get("raw", "")[:100])
                src = a.get("source", "")
                lines.append(f"- {sv} **{src}**: {summ}")

        # 智能建议
        crit = sev_count.get("critical", 0)
        major = sev_count.get("major", 0)
        minor = sev_count.get("minor", 0)
        lines += ["", "### 💡 处理建议", ""]
        if crit:
            lines.append(f"1. **紧急**：存在 {crit} 条严重告警，需立即排查处理")
        if major:
            lines.append(f"2. **重要**：{major} 条重要告警，建议优先安排处理")
        if minor:
            lines.append(f"3. **关注**：{minor} 条次要告警，可纳入日常巡检")
        # 同源多告警提示
        high_src = [(s, c) for s, c in src_count.items() if c >= 3]
        if high_src:
            names = "、".join([s for s, _ in sorted(high_src, key=lambda x: -x[1])[:3]])
            lines.append(f"4. **关联分析**：{names} 存在多条告警，可能存在共同根因")
        if not crit and not major:
            lines.append("- 当前无严重/重要级别告警，系统整体运行正常")
        return "\n".join(lines)


    @staticmethod
    def _nms_strip_reasoning(text: str) -> str:
        """清理推理模型的思考过程输出，只保留最终分析结果。
        报告要求以“## 一、告警概览”开头，因此优先裁掉该锤点之前的任何
        思考/步骤/元叙述内容（如 deepseek 偶尔输出的 '1. 理解任务目标…'）。
        """
        if not text or len(text) < 100:
            return text
        # 首选：报告应以“告警概览”章节开头，裁掉此前的思考/步骤内容
        for anchor in ("## 一、告警概览", "一、告警概览", "告警概览", "## 一"):
            idx = text.find(anchor)
            if idx == 0:
                return text
            if idx > 0:
                ls = text.rfind("\n", 0, idx)
                cut = text[(ls + 1) if ls != -1 else 0:].strip()
                if len(cut) > 120:
                    return cut
        # 回退（旧启发式）：检测到典型 reasoning 标记后跳到最终 markdown 输出
        reasoning_markers = [
            "理解任务目标", "理解目标", "分析输入数据", "构建输出结构",
            "详细内容填充", "**理解目标", "1. **理解", "让我来分析", "我需要",
        ]
        has_reasoning = any(m in text[:600] for m in reasoning_markers)
        if not has_reasoning:
            return text
        # 尝试找到实际输出的开始位置（reasoning 结束后通常会有 markdown 标题或表格）
        output_markers = ["## ", "### ", "| ", "---\n"]
        # 从后面往前找最后一个 markdown 结构开始
        best_idx = -1
        for marker in output_markers:
            idx = text.rfind(marker)
            if idx > len(text) * 0.3 and idx > best_idx:
                # 回退到该行的开头
                line_start = text.rfind("\n", 0, idx)
                if line_start == -1:
                    line_start = 0
                else:
                    line_start += 1
                best_idx = max(best_idx, line_start)
        if best_idx > 0 and len(text) - best_idx > 100:
            cleaned = text[best_idx:].strip()
            # 如果清理后太短，返回原文
            if len(cleaned) > 100:
                return cleaned
        # Fallback: 查找 "运维建议" 等关键字后的内容
        for kw in ["运维建议", "处理建议", "建议\n"]:
            idx = text.find(kw)
            if idx > 0:
                # 找到该段结束后的下一个标题
                next_h = text.find("\n## ", idx)
                if next_h > 0:
                    return text[next_h:].strip()
        return text

    async def _nms_notify_channel(self, ch: Dict, f_all: List[Dict],
                                  f_new: List[Dict], base_extra: Dict,
                                  keep_groups: set):
        """向单个推送通道发送按其分组/级别过滤后的告警卡片。
        ch: {id,name,type:webhook|app,webhook_url,chat_id,...}；base_extra: 全量卡片数据。"""
        _sub_extra = dict(base_extra)
        _sub_extra["total"] = len(f_all)
        _sub_extra["new"] = len(f_new)
        # 级别计数按过滤后告警重算，保证卡片头部级别行与明细一致
        _sev_cnt = {}
        for a in f_all:
            s = a.get("severity", "unknown")
            if s in (base_extra.get("severity") or {}):
                _sev_cnt[s] = _sev_cnt.get(s, 0) + 1
        _sub_extra["severity"] = _sev_cnt
        _sub_extra["push_groups"] = sorted(keep_groups)
        _sub_extra["target"] = ch.get("chat_id") or ""  # 兼容旧 bot target 分发
        _sub_extra["channel"] = {
            "id": ch.get("id") or "", "name": ch.get("name") or "",
            "type": ch.get("type") or "app",
            "webhook_url": ch.get("webhook_url") or "",
            "chat_id": ch.get("chat_id") or "",
        }
        # 卡片事件/明细/截图区块也按订阅组过滤，避免订阅群看到其他组告警
        _sub_extra["incidents"] = [
            i for i in (base_extra.get("incidents") or [])
            if not keep_groups or keep_groups & set(i.get("groups") or [])]
        _sub_extra["new_alarms"] = [
            a for a in (base_extra.get("new_alarms") or [])
            if not keep_groups or a.get("group") in keep_groups]
        _sub_extra["shots"] = [
            s for s in (base_extra.get("shots") or [])
            if not keep_groups or s.get("group") in keep_groups]
        _gs_label = "、".join(sorted(keep_groups)) if keep_groups else "全部"
        await self.notify(
            f"NMS 通道推送「{ch.get('name')}」({_gs_label}): "
            f"活跃 {len(f_all)}, 新增 {len(f_new)}",
            topic="nms_alarm", extra=_sub_extra)
        # 推送锚定群会话（app 型）：写入 feishu:{chat_id} 会话历史，
        # 群内追问“刚才那条告警怎么回事”时 orchestrator/agent 可见本轮推送上下文
        try:
            if ch.get("type") != "webhook" and ch.get("chat_id"):
                r2 = await self.get_redis()
                _sid = f"feishu:{ch['chat_id']}"
                _top = [f"[{a.get('severity', '-')}] {str(a.get('summary', ''))[:60]}"
                        f"（{a.get('group', '-')}）"
                        for a in (f_new or f_all)[:3]]
                _anchor = (f"【NMS 推送锚点】通道「{ch.get('name')}」({_gs_label}): "
                          f"活跃 {len(f_all)}, 新增 {len(f_new)}。"
                          + ("Top: " + "；".join(_top) if _top else "无新告警"))
                _skey = config.key_session_msgs(_sid)
                await r2.rpush(_skey, json.dumps({
                    "role": "assistant", "content": _anchor[:600],
                    "timestamp": time.time(), "session_id": _sid,
                    "source": "nms_push", "channel": ch.get("name", ""),
                }, ensure_ascii=False))
                if config.SESSION_MSG_MAX > 0:
                    await r2.ltrim(_skey, -config.SESSION_MSG_MAX, -1)
                if config.SESSION_TTL > 0:
                    await r2.expire(_skey, config.SESSION_TTL)
        except Exception as e:
            logger.debug(f"[NMS] 会话锚点写入失败: {e}")

    async def _nms_notify_feishu(self, all_alarms: List[Dict],
                                  new_alarms: List[Dict],
                                  analysis: str,
                                  incidents: List[Dict] = None,
                                  group_shots: List[Dict] = None,
                                  force: bool = False):
        """推送 NMS 告警监控报告到飞书（事件驱动：默认仅有新增告警时推送）。
        NMS_NOTIFY_BY_INCIDENT 开启时按事件簇汇总推送（降噪），否则逐条新增告警。
        group_shots: 分组视图截图 [{group, severity, url}]，附卡片尾部供点击查看。
        force: True 时无视 push_only_on_new 闸门（bot 指令 nms 分析 强制推送）。"""
        # 动态配置（前端管理页可写，优先级高于 config 静态值）
        try:
            _dyn = await self._nms_dyn_cfg(await self.get_redis())
        except Exception:
            _dyn = {}
        push_enabled = _dyn.get("push_enabled", config.NMS_FEISHU_PUSH_ENABLED)
        push_only_new = _dyn.get("push_only_on_new", config.NMS_PUSH_ONLY_ON_NEW)
        if not push_enabled:
            logger.info("[NMS] 飞书推送已关闭 (push_enabled=False)，跳过")
            return
        if push_only_new and not new_alarms and not force:
            logger.debug("[NMS] 无新增告警，跳过飞书推送 (push_only_on_new)")
            return
        # 临时静默（飞书群 `nms 静默 <小时>` 设置 push_silent_until）：到期前不推
        _silent_until = _dyn.get("push_silent_until")
        if isinstance(_silent_until, (int, float)) and time.time() < _silent_until:
            _left_min = max(1, int((_silent_until - time.time()) // 60))
            logger.info(f"[NMS] 飞书推送静默中（约剩 {_left_min} 分钟），跳过")
            return
        # 最小推送间隔（飞书群 `nms 推送间隔 <分钟>` 设置 push_min_interval）：距上次推送不足则跳过
        _min_interval = _dyn.get("push_min_interval")
        if isinstance(_min_interval, (int, float)) and _min_interval > 0:
            try:
                _r2 = await self.get_redis()
                _last_raw = await _r2.get(config.KEY_NMS_LAST_PUSH_TS)
                if _last_raw and (time.time() - float(_last_raw)) < _min_interval * 60:
                    logger.info(f"[NMS] 距上次推送不足 {int(_min_interval)} 分钟，跳过飞书推送")
                    return
            except Exception:
                pass

        # A5 告警抑制 (pattern 重复 + 维护窗口) — 在推送前过滤
        try:
            _r3 = await self.get_redis()
            # 加载抑制规则 + 维护窗口
            rule_raw_list = await _r3.hvals(config.KEY_NMS_SUPPRESS_RULES)
            rules = {}
            for raw in rule_raw_list or []:
                try:
                    rec = json.loads(raw)
                    rid = rec.get("id") or rec.get("rule_id")
                    if not rid:
                        continue
                    # 过期规则跳过
                    if rec.get("until_epoch") and time.time() > float(rec["until_epoch"]):
                        continue
                    rules[rid] = rec
                except Exception:
                    pass
            maint_raw = await _r3.hgetall(config.KEY_NMS_MAINT_WINDOWS) or {}
            maint_windows = {k: float(v) for k, v in maint_raw.items()
                              if float(v) > time.time()}
            # 仅对新告警应用抑制 (历史告警不影响)
            kept, suppressed, in_maint = self._nms_apply_suppression(
                new_alarms, rules, maint_windows)
            if suppressed or in_maint:
                logger.info(f"[NMS] 抑制推送: {len(suppressed)} 条 pattern 命中"
                            f", {len(in_maint)} 条维护窗口内"
                            f" (新告警 {len(new_alarms)} → 通过 {len(kept)})")
            new_alarms = kept
            if not new_alarms and not force:
                # 全被抑制, 跳过推送 (但 group_shots + analysis 仍可能需要, 暂直接返回)
                logger.info("[NMS] 新增告警全部被抑制, 跳过推送")
                return
        except Exception as e:
            logger.debug(f"[NMS] 抑制应用失败 (忽略): {e}")

        now_str = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        _SEV_EMOJI = {"critical": "🔴", "major": "🟠", "minor": "🟡",
                      "warning": "🔵", "indeterminate": "⚪"}
        _SEV_CN = {"critical": "严重", "major": "重要", "minor": "次要",
                   "warning": "警告", "indeterminate": "未知"}
        _ACTIVE = ("critical", "major", "minor", "warning", "indeterminate")

        # 原始全量副本：指定了分组的推送通道从原始告警过滤，不受全局 push_groups 挤压
        # （push_groups 仅约束默认兜底推送的关注面）
        all_alarms_raw, new_alarms_raw = list(all_alarms), list(new_alarms)

        # ── 推送分组过滤：只推送配置选择的组（飞书 `nms 推送分组 <组,组>` / 前端管理页可配；空=全部） ──
        _push_groups = [g.strip() for g in
                        str(_dyn.get("push_groups") or "").split(",") if g.strip()]
        if _push_groups:
            _keep = set(_push_groups)
            all_alarms = [a for a in all_alarms
                          if _keep & set(self._nms_alarm_groups(a))]
            new_alarms = [a for a in new_alarms
                          if _keep & set(self._nms_alarm_groups(a))]
            if not all_alarms and not new_alarms:
                logger.info(f"[NMS] 推送分组过滤后无告警 ({','.join(_push_groups)})，跳过推送")
                return
            # 按过滤后的告警重新归并事件簇（保证明细只含关注组）
            try:
                from agents.nms_correlation import correlate_alarms
                _new_ids = {a.get("alarm_id") for a in new_alarms}
                incidents = correlate_alarms(
                    all_alarms,
                    systemic_min_sources=config.NMS_SYSTEMIC_MIN_SOURCES,
                    new_ids=_new_ids)
            except Exception as e:
                logger.warning(f"[nms_agent] 推送分组过滤后归并失败: {e}")
                incidents = []
            logger.info(f"[NMS] 推送分组过滤: {','.join(_push_groups)} "
                        f"-> active={len(all_alarms)}, new={len(new_alarms)}")
            # 降耗治理：分析摘要已在主分析阶段按 push_groups 过滤生成（见 7. 主分析），
            # 此处直接复用传入的 analysis，不再二次调用 LLM

        # 活跃告警严重级别分布
        severity_count = {}
        for a in all_alarms:
            s = a.get("severity", "unknown")
            if s in _ACTIVE:
                severity_count[s] = severity_count.get(s, 0) + 1
        sev_lines = "　".join(
            f"{_SEV_EMOJI.get(s, '⚪')} {_SEV_CN.get(s, s)} {severity_count[s]}"
            for s in _ACTIVE if severity_count.get(s)
        ) or "无"

        # 分组统计（top 5）与 Checklist 预案命中摘要
        from collections import Counter
        gcount = Counter()
        for a in all_alarms:
            for g in (a.get("groups") or ([a.get("group")] if a.get("group") else ["未分组"])):
                gcount[g] += 1
        extra_sections = ""
        if gcount:
            top_g = "、".join(f"{g} {n}" for g, n in gcount.most_common(5))
            extra_sections += f"\n**重点分组**: {top_g}"
        clcount = Counter(a.get("checklist") for a in all_alarms if a.get("checklist"))
        if clcount:
            top_cl = "、".join(f"{t} {n}" for t, n in clcount.most_common(6))
            extra_sections += f"\n**预案命中**: {top_cl}"
        if group_shots:
            def _shot_label(s):
                _unack_tag = "（未确认）" if s.get("unack") else ""
                if s.get("severity"):
                    return f"{s['group']} · {_NMS_SEV_CN.get(s['severity'], s['severity'])}告警截图{_unack_tag}"
                return f"{s['group']} 视图截图{_unack_tag}"
            shot_lines = "\n".join(f"　[{_shot_label(s)}]({s['url']})" for s in group_shots)
            extra_sections += f"\n**分组告警截图**:\n{shot_lines}"

        # 明细段：按事件簇汇总（降噪）或逐条新增告警
        detail_section = ""
        card_incidents = []   # 结构化事件列表（供飞书卡片渲染）
        card_new_alarms = []  # 结构化新增告警列表（非 incident 模式）
        use_incident = (config.NMS_NOTIFY_BY_INCIDENT and incidents)
        if use_incident:
            _CAT_CN = {"systemic": "系统级", "object": "对象级"}
            # 仅推有本轮新增成员的事件簇，按 priority 已降序
            new_incidents = [i for i in incidents if i.get("new_count", 0) > 0]
            top_n = config.NMS_INCIDENT_TOP
            inc_lines = []
            for i in new_incidents[:top_n]:
                s = i.get("severity", "unknown")
                cat = _CAT_CN.get(i.get("category"), "")
                line = (f"{_SEV_EMOJI.get(s, '⚪')} **[{_SEV_CN.get(s, s)}·{cat}]** "
                        f"{(i.get('title') or '')[:60]}\n"
                        f"　成员 {i.get('member_count', 0)} 条 / 来源 {i.get('source_count', 0)} 个 "
                        f"/ 未确认 {i.get('unacked_count', 0)} / 本轮新增 {i.get('new_count', 0)}")
                lt = i.get("last_time")
                if lt:
                    line += f" | 最后: {lt}"
                inc_lines.append(line)
                card_incidents.append({
                    "severity": s, "category": cat,
                    "title": (i.get("title") or "")[:60],
                    "members": i.get("member_count", 0),
                    "sources": i.get("source_count", 0),
                    "unacked": i.get("unacked_count", 0),
                    "new": i.get("new_count", 0),
                    "last_time": i.get("last_time") or "",
                    # 成员告警的分组集合（订阅分发按组过滤用）
                    "groups": sorted(i.get("groups") or []),
                })
            if inc_lines:
                detail_section = (
                    f"\n\n**本次新增 {len(new_alarms)} 条告警，归并为 {len(new_incidents)} 个事件:**\n"
                    + "\n".join(inc_lines)
                )
                if len(new_incidents) > top_n:
                    detail_section += f"\n　… 另有 {len(new_incidents) - top_n} 个事件"
        else:
            # 逐条新增告警明细（最多 10 条，带分组/来源/首末时间/工单号）
            new_lines = []
            for a in new_alarms[:10]:
                s = a.get("severity", "unknown")
                gs = a.get("groups") or ([a.get("group")] if a.get("group") else [])
                g = gs[0] if gs else "-"
                src = a.get("source") or a.get("node") or "-"
                line = (f"{_SEV_EMOJI.get(s, '⚪')} **[{_SEV_CN.get(s, s)}]** [{g}] "
                        f"{(a.get('summary') or '')[:60]}\n"
                        f"　来源: {src}")
                ft = a.get("first_time")
                lt = a.get("last_time") or a.get("time")
                if ft or lt:
                    line += f" | 首次: {ft or '-'} / 最后: {lt or '-'}"
                if a.get("case_id"):
                    line += f" | Case: {a['case_id']}"
                new_lines.append(line)
                card_new_alarms.append({
                    "severity": s, "group": g,
                    "summary": (a.get("summary") or "")[:60],
                    "source": src,
                    "first_time": ft or "", "last_time": lt or "",
                    "case_id": a.get("case_id") or "",
                })
            if new_alarms:
                detail_section = (f"\n\n**新增告警（{len(new_alarms)} 条）:**\n"
                                  + "\n".join(new_lines))
                if len(new_alarms) > 10:
                    detail_section += f"\n　… 另有 {len(new_alarms) - 10} 条"

        analysis_section = f"\n\n**分析摘要:**\n{analysis[:600]}" if analysis else ""

        text = (
            f"**时间**: {now_str}\n"
            f"**活跃告警**: {len(all_alarms)} 条　{sev_lines}\n"
            f"**本次新增**: {len(new_alarms)} 条"
            f"{extra_sections}"
            f"{detail_section}"
            f"{analysis_section}\n\n"
            f"[查看 NMS 控制台]({config.NMS_CONSOLE_URL})"
        )

        # 结构化卡片数据（飞书通道据此渲染富卡片）
        _extra = {
            "time": now_str,
            "total": len(all_alarms),
            "new": len(new_alarms),
            "severity": {k: severity_count.get(k, 0) for k in _ACTIVE},
            "top_groups": [{"name": g, "count": n}
                           for g, n in gcount.most_common(5)],
            "checklists": [{"name": t, "count": n}
                           for t, n in clcount.most_common(6)],
            "push_groups": _push_groups,
            "console_url": config.NMS_CONSOLE_URL,
            "analysis": (analysis or "")[:600],
            "incidents": card_incidents,
            "new_alarms": card_new_alarms,
            "shots": [
                {"group": s.get("group"),
                 "severity": s.get("severity") or "",
                 "unack": bool(s.get("unack")),
                 "file": s.get("file") or "",
                 "url": s.get("url") or ""}
                for s in (group_shots or [])
            ],
        }

        # ── 多源通道分发：每通道按 分组∩级别 独立过滤推送（KEY_NMS_CHANNELS）──
        # 无通道配置时回退旧订阅表(KEY_NMS_SUBS)按 chat 分发，保持兼容
        try:
            _r3 = await self.get_redis()
            _ch_raw = await _r3.hgetall(config.KEY_NMS_CHANNELS)
            _legacy_subs = ({} if _ch_raw
                            else await _r3.hgetall(config.KEY_NMS_SUBS))
        except Exception:
            _ch_raw, _legacy_subs = {}, {}
        _channels = []
        for _craw in (_ch_raw or {}).values():
            try:
                _c = json.loads(_craw)
            except Exception:
                continue
            if not (isinstance(_c, dict) and _c.get("enabled", True)):
                continue
            # 卡片按钮「静默」写入的 silent_until 生效期内跳过该通道（告警降噪）
            if float(_c.get("silent_until") or 0) > time.time():
                _silent_h = datetime.fromtimestamp(
                    float(_c["silent_until"])).strftime("%m-%d %H:%M")
                logger.info(f"[NMS] 通道「{_c.get('name')}」静默中"
                            f"（至 {_silent_h}），本轮跳过")
                continue
            _channels.append(_c)
        for _chat_id, _raw in (_legacy_subs or {}).items():
            try:
                _gs = [g.strip() for g in json.loads(_raw or "[]")
                       if isinstance(g, str) and g.strip()]
            except Exception:
                _gs = []
            if _gs:
                _channels.append({"id": f"sub_{_chat_id}", "name": _chat_id[:16],
                                  "type": "app", "chat_id": _chat_id,
                                  "groups": _gs, "severities": [],
                                  "enabled": True, "is_default": False})

        _default_ch = next((_c for _c in _channels if _c.get("is_default")), None)
        _sent_ch = 0
        for _ch in _channels:
            if _ch.get("is_default"):
                continue  # 兜底通道仅在所有常规通道均无匹配时推送
            _keep2 = set(_ch.get("groups") or [])
            _sev2 = set(_ch.get("severities") or [])
            _base_all, _base_new = all_alarms_raw, new_alarms_raw
            if _keep2:
                _base_all = [a for a in _base_all
                             if _keep2 & set(self._nms_alarm_groups(a))]
                _base_new = [a for a in _base_new
                             if _keep2 & set(self._nms_alarm_groups(a))]
            if _sev2:
                _base_all = [a for a in _base_all if a.get("severity") in _sev2]
                _base_new = [a for a in _base_new if a.get("severity") in _sev2]
            if not _base_all and not _base_new:
                logger.info(f"[NMS] 通道「{_ch.get('name')}」无匹配告警，跳过")
                continue
            await self._nms_notify_channel(_ch, _base_all, _base_new,
                                           _extra, _keep2)
            _sent_ch += 1
        if _sent_ch:
            logger.info(f"[NMS] 多源通道分发完成: {_sent_ch}/{len(_channels)} 通道命中")
        elif _default_ch is not None:
            # 全部启用通道无匹配 → 推默认兜底通道（全局 push_groups 过滤后的全量卡片）
            logger.info(f"[NMS] 常规通道无匹配，推默认兜底通道「{_default_ch.get('name')}」")
            await self._nms_notify_channel(_default_ch, all_alarms, new_alarms,
                                           _extra, set())
        else:
            # 无通道配置 → 旧默认 webhook 行为
            await self.notify(text, topic="nms_alarm", extra=_extra)
        # 记录推送时间戳（供最小推送间隔判断）
        try:
            await (await self.get_redis()).set(
                config.KEY_NMS_LAST_PUSH_TS, time.time())
        except Exception:
            pass
        logger.info(
            f"[NMS] 飞书通知已发送 (total={len(all_alarms)}, new={len(new_alarms)}, "
            f"mode={'incident' if use_incident else 'per-alarm'})"
        )

    async def _nms_alarm_query(self, msg: AgentMessage):
        """对话查询：按用户问题关键词过滤当前告警，命中返回明细，未命中返回概览

        (此前无论问什么都返回同一份全量统计+缓存分析报告，答非所问 — 实测
         "硬件层面告警"与"开单跟进"两个不同问题拿到一字不差的同一回复)
        """
        r = await self.get_redis()

        analysis_raw = await r.get(NMS_KEY_ANALYSIS)
        latest_raw = await r.get(NMS_KEY_LATEST)

        if not analysis_raw and not latest_raw:
            await self.reply(msg, result={
                "text": "暂无 NMS 告警数据，监控任务可能尚未执行首次抓取。"
            })
            return

        question = (msg.text or "").strip()

        # ── 概览文本 (抓取时间 + 级别分布 + 缓存分析报告) ──
        overview_parts = []
        latest_ts, latest_total, latest_alarms = "", 0, []
        if latest_raw:
            try:
                latest = json.loads(latest_raw)
                latest_ts = latest.get("timestamp", "")
                latest_total = latest.get("total", 0)
                latest_alarms = latest.get("alarms", [])
                overview_parts.append(
                    f"**最近抓取时间**: {latest_ts}\n**当前活跃告警**: {latest_total} 条"
                )
                sev_count = {}
                for a in latest_alarms:
                    sev = a.get("severity", "unknown")
                    sev_count[sev] = sev_count.get(sev, 0) + 1
                if sev_count:
                    sev_str = " / ".join(f"{k}: {v}" for k, v in sev_count.items())
                    overview_parts.append(f"**级别分布**: {sev_str}")
            except Exception:
                pass
        if analysis_raw:
            try:
                analysis = json.loads(analysis_raw)
                overview_parts.append(
                    f"\n**分析报告** (新增 {analysis.get('new_count', 0)} 条):\n"
                    f"{analysis.get('analysis', '')}"
                )
            except Exception:
                pass
        overview = "\n".join(overview_parts)

        # ── 针对性过滤: 问题关键词在告警 summary/source/node/groups 中匹配 ──
        kws = []
        if question and latest_alarms:
            try:
                # jieba 分词 (整串正则会把连续中文当成一个关键词, 子串匹配必失败)
                stop = {'目前', '哪些', '有没有', '请问', '一下', '现在', '什么', '怎么', '如何',
                        '云主机', '告警', '状态', '查询', '方面', '层面', '设置', '供监控',
                        '跟进', '处理', '开单', '服务器', '主机', '当前', '最近', '告警的',
                        'netcool', '监控', '所有', '全部', '以及', '还有', '这个', '哪些是',
                        '设置在', '是有', '没有', '哪些有'}
                raw_kws = [w for w in jieba.cut(question)
                           if len(w) >= 2 and not w.isspace()]
                kws = [k.lower() for k in raw_kws if k.lower() not in stop]
                # 领域同义词扩展: 用户抽象词 (硬件/电源...) → 告警文本常见英文词
                # (告警 summary 多为具体设备词, 抽象词直接匹配必为 0)
                _syn = {'硬件': ['power', 'fan', 'disk', 'temperature', 'voltage',
                                'hardware', 'psu', 'raid', 'cpu', 'memory'],
                        '电源': ['power', 'psu'], '风扇': ['fan'],
                        '磁盘': ['disk', 'storage'], '温度': ['temperature', 'temp'],
                        '内存': ['memory'], '网络': ['network', 'interface', 'link', 'bgp'],
                        '备份': ['backup', 'veeam', 'vbr']}
                _expanded = []
                for _k in kws:
                    _expanded.append(_k)
                    _expanded.extend(_syn.get(_k, []))
                kws = _expanded
            except Exception:
                kws = []

        matched = []
        if kws:
            for a in latest_alarms:
                hay = f"{a.get('summary', '')} {a.get('source', '')} {a.get('node', '')} {a.get('groups', '')}".lower()
                if any(k in hay for k in kws):
                    matched.append(a)

        if matched:
            # 命中: 匹配明细 (按严重级排序, 最多 20 条)
            sev_order = {'critical': 0, 'major': 1, 'minor': 2, 'warning': 3}
            matched.sort(key=lambda a: sev_order.get(str(a.get('severity', '')).lower(), 9))
            lines = [f"在当前 {latest_total} 条告警中，与「{'、'.join(kws[:5])}」匹配到 "
                     f"**{len(matched)} 条**（最近抓取: {latest_ts}）：", ""]
            # 问"开单跟进"类: 按 case_id 有无分组统计
            if re.search(r'开单|工单|单号|case|跟进', question, re.I):
                with_case = [a for a in matched if str(a.get('case_id') or '').strip()]
                without_case = [a for a in matched if not str(a.get('case_id') or '').strip()]
                lines.append(f"**开单情况**: 已开单 {len(with_case)} 条 / 未开单 {len(without_case)} 条")
                lines.append("")
                for a in matched[:20]:
                    case = str(a.get('case_id') or '').strip()
                    lines.append(f"- [{a.get('severity', '?')}] {str(a.get('summary', ''))[:80]} "
                                 f"(节点: {a.get('node', '?')} | {'工单: ' + case if case else '未开单'})")
            else:
                for a in matched[:20]:
                    case = str(a.get('case_id') or '').strip()
                    case_tag = f" | 工单: {case}" if case else ""
                    lines.append(f"- [{a.get('severity', '?')}] {str(a.get('summary', ''))[:80]} "
                                 f"(节点: {a.get('node', '?')}{case_tag})")
            if len(matched) > 20:
                lines.append(f"... 及另外 {len(matched) - 20} 条（可提供更具体的关键词缩小范围）")
            await self.reply(msg, result={"text": "\n".join(lines)})
            await self.log_daily(
                f"NMS 告警查询: {question[:50]} → 命中 {len(matched)} 条",
                level="success", category="chat", session_id=msg.session_id,
                username=(msg.username or "")
            )
            return

        # 开单跟进类问题且关键词未命中: 对全量告警做开单统计 (而非落回通用概览)
        wants_case = bool(re.search(r'开单|工单|单号|case|跟进', question, re.I))
        if wants_case and latest_alarms:
            with_case = [a for a in latest_alarms if str(a.get('case_id') or '').strip()]
            without_case = [a for a in latest_alarms if not str(a.get('case_id') or '').strip()]
            sev_order = {'critical': 0, 'major': 1, 'minor': 2, 'warning': 3}
            with_case.sort(key=lambda a: sev_order.get(str(a.get('severity', '')).lower(), 9))
            lines = [f"**当前 {latest_total} 条告警开单情况**（最近抓取: {latest_ts}）：",
                     "",
                     f"- 已开单（有 case_id）: **{len(with_case)} 条**",
                     f"- 未开单: **{len(without_case)} 条**",
                     "",
                     "**已开单告警明细**（按严重级排序, 最多 20 条）:",
                     ""]
            for a in with_case[:20]:
                lines.append(f"- [{a.get('severity', '?')}] {str(a.get('summary', ''))[:80]} "
                             f"(节点: {a.get('node', '?')} | 工单: {str(a.get('case_id')).strip()})")
            if len(with_case) > 20:
                lines.append(f"... 及另外 {len(with_case) - 20} 条已开单告警")
            await self.reply(msg, result={"text": "\n".join(lines)})
            await self.log_daily(
                f"NMS 告警查询: {question[:50]} → 开单统计 已开单{len(with_case)}/未开单{len(without_case)}",
                level="success", category="chat", session_id=msg.session_id,
                username=(msg.username or "")
            )
            return

        # 未命中: 明确说明 + 概览兜底
        _nms_answer = overview
        if kws:
            _nms_answer = (f"未在当前 {latest_total} 条告警中找到与「{'、'.join(kws[:5])}」直接匹配的告警。\n\n"
                           f"以下为当前告警概览（如需精确过滤请提供具体主机名 / IP / 告警关键词）：\n\n" + overview)
        await self.reply(msg, result={"text": _nms_answer})
        # 完整问答全文由 BaseAgent.reply() 兜底收录，这里只记摘要
        await self.log_daily(
            f"NMS 告警查询: {question[:50]} → 已回复",
            level="success", category="chat", session_id=msg.session_id,
            username=(msg.username or "")
        )

    async def _nms_alarm_now(self, msg: AgentMessage):
        """立即执行一次 NMS 告警抓取"""
        await self.send_progress(msg, "正在抓取 NMS 告警页面...")
        try:
            # SSO 会话用户 (零密码存储) 优先; 兼容旧的显式凭据参数
            session_user = msg.params.get("session_user") if msg.params else None
            nms_username = msg.params.get("nms_username") if msg.params else None
            nms_password = msg.params.get("nms_password") if msg.params else None
            await self._nms_run_once(nms_username, nms_password,
                                     force_analyze=True,
                                     session_user=session_user)
            r = await self.get_redis()
            # 按 owner 读回对应快照，保证回执与实际写入一致
            _owner = session_user or nms_username
            if _owner and _owner != config.NMS_USERNAME:
                _base = f"{config.REDIS_PREFIX}:nms_alarms:user:{_owner}"
            else:
                _base = f"{config.REDIS_PREFIX}:nms_alarms"
            analysis_raw = await r.get(f"{_base}:analysis")
            latest_raw = await r.get(f"{_base}:latest")
            parts = ["NMS 告警抓取完成。"]
            if latest_raw:
                latest = json.loads(latest_raw)
                parts.append(f"当前活跃告警: {latest.get('total', 0)} 条")
            if analysis_raw:
                analysis = json.loads(analysis_raw)
                parts.append(analysis.get("analysis", ""))
            await self.reply(msg, result={"text": "\n".join(parts)})
        except Exception as e:
            await self.reply(msg, error=f"NMS 抓取失败: {str(e)}")

    async def _nms_trigger_workflow(self, new_alarms: List[Dict]):
        """告警驱动的自动化检查工作流编排钩子。

        将本轮新增告警汇总为运维检查请求 publish 给 orchestrator，由其 LLM 路由层
        分派到 network/vm/opscore 等 agent 做自动化检查（ping、查 VM、查备份）。
        触发记录写入 zops:nms_alarms:workflow。仅全局监控账号触发，per-user 不触发。
        """
        if not new_alarms:
            return
        try:
            r = await self.get_redis()
            briefs = []
            for a in new_alarms[:10]:
                sev = a.get("severity", "unknown")
                src_ = a.get("source", "未知")
                summ = (a.get("summary") or "")[:80]
                briefs.append(f"[{sev}] {src_}: {summ}")
            brief_text = "\n".join(briefs)
            prompt = (
                "NMS 监控检测到以下新增告警，请对涉及的主机/服务做自动化检查"
                "（如网络连通性 ping、虚拟机状态、备份作业状态），并给出处理建议：\n"
                + brief_text
            )
            wf_msg = AgentMessage(
                source="nms_agent",
                target="orchestrator",
                action="chat",
                text=prompt,
                params={"origin": "nms_auto_check", "alarm_count": len(new_alarms)},
            )
            await r.publish(config.channel("orchestrator"), wf_msg.to_json())
            record = {
                "timestamp": _nms_now_iso(),
                "alarm_count": len(new_alarms),
                "briefs": briefs,
            }
            await r.set(f"{config.REDIS_PREFIX}:nms_alarms:workflow",
                        json.dumps(record, ensure_ascii=False))
            logger.info(f"[nms_agent] 已触发自动化检查工作流: new={len(new_alarms)}")
        except Exception as e:
            logger.error(f"[nms_agent] 触发工作流失败: {e}")

    # ────────────────────────────────────────────────────────────────────────
    # A2: 结构化处置闭环 — 从告警内容确定性抽取动作 → orchestrator 精准路由
    # ────────────────────────────────────────────────────────────────────────
    _NMS_REMEDIATION_RULES: List = [
        # (summary 正则, action_type, action params 提取器)
        # VBR / 备份失败 / O365 / Zerto 等 → 查备份状态
        (r"vbr|veeam|备份失败|o365|a2c|zerto.{0,30}journal|brr",
         "backup_status",
         lambda a: {"ip": a.get("source") or "", "is_o365": "o365" in (a.get("summary") or "").lower()}),
        # DNS 解析异常 → DNS 实测
        (r"dns.{0,12}(解析|resolve|fail)|无法解析",
         "dns_query",
         lambda a: {"source": a.get("summary") or ""}),
        # ESXi / VM / vCenter / datastore / DRS → 查主机/虚机
        (r"esxi|vsphere|datastore|drs|host.{0,8}(down|disconnect)|not respond|vpxa|vcenter",
         "vm_list",
         lambda a: {"source": a.get("source") or a.get("node") or ""}),
        # BGP / 网络 / 链路 → ping
        (r"bgp|ospf|bfd|interface|链路|link.{0,8}(down|err)|lsp|icmp|ping|unreachable",
         "ping_sweep",
         lambda a: {"target": a.get("source") or a.get("node") or ""}),
    ]

    @staticmethod
    def _nms_extract_remediation_actions(alarms: List[Dict], max_actions: int = 8) -> List[Dict]:
        """从告警内容确定性抽取处置动作(纯函数, 便于单测)。

        Returns:
            [{"action_id", action_type, params, reason, evidence: [alarm_id...]}]
        """
        out: Dict[str, Dict] = {}  # key=action_type+target → action dict (去重)
        for a in alarms:
            summary = (a.get("summary") or "").lower()
            source = (a.get("source") or a.get("node") or "").strip()
            alarm_id = a.get("alarm_id") or a.get("event_id") or a.get("serial") or ""
            for pat, atype, param_fn in NmsAgent._NMS_REMEDIATION_RULES:
                if re.search(pat, summary, re.IGNORECASE):
                    params = param_fn(a)
                    target = (params.get("ip") or params.get("target")
                              or params.get("source") or "").strip()
                    if not target:
                        continue
                    key = f"{atype}|{target}"
                    if key in out:
                        # 合并证据 (同一动作多条告警支撑)
                        out[key]["evidence"].append(alarm_id)
                        continue
                    out[key] = {
                        "action_id": f"act_{len(out)+1}_{atype}_{hashlib.md5(target.encode()).hexdigest()[:6]}",
                        "action_type": atype,
                        "params": params,
                        "reason": (a.get("summary") or "")[:100],
                        "severity": a.get("severity", ""),
                        "evidence": [alarm_id] if alarm_id else [],
                        "status": "pending",
                        "created_at": _nms_now_iso(),
                    }
                    if len(out) >= max_actions:
                        return list(out.values())
        return list(out.values())

    async def _nms_auto_remediate(self, new_alarms: List[Dict], base: str = ""):
        """A2: 触发结构化处置动作分派 (orchestrator 路由)。

        动作类型 → orchestrator COMMAND_ROUTES:
          ping_sweep → network_agent,  backup_status → opscore_agent,
          vm_list → vm_agent, dns_query → opscore_agent.
        每个动作发一条独立 AgentMessage, 收集回复用 {base}:remediations:{action_id}.
        """
        actions = self._nms_extract_remediation_actions(new_alarms)
        if not actions:
            return
        r = await self.get_redis()
        from agents.base import AgentMessage
        dispatched = 0
        for act in actions:
            try:
                # 每个动作发独立消息 (orchestrator 走 COMMAND_ROUTES 精确路由)
                msg = AgentMessage(
                    source="nms_agent",
                    target="orchestrator",
                    action=act["action_type"],
                    params=act["params"],
                    text=f"NMS 自动处置: {act['action_type']} on {act['params']}",
                    username="svc-opscore",
                )
                # 确定性 msg_id → 固定 reply 频道 (config.reply_channel(msg_id))
                # 后续可由 nms_agent 订阅回填结果, 当前为 dispatch-only
                msg.msg_id = f"nms_remediate_{act['action_id']}"
                await r.publish(config.channel("orchestrator"), msg.to_json())
                # 写持久化记录
                await r.set(
                    f"{config.REDIS_PREFIX}:nms_remediations:{act['action_id']}",
                    json.dumps(act, ensure_ascii=False),
                    ex=24 * 3600,
                )
                # append 到 group 索引 (trim 保留最近 500 条)
                await r.rpush(
                    f"{config.REDIS_PREFIX}:nms_remediations:index",
                    act["action_id"],
                )
                await r.ltrim(
                    f"{config.REDIS_PREFIX}:nms_remediations:index",
                    -500, -1,
                )
                dispatched += 1
            except Exception as e:
                logger.error(f"[nms_agent] 分派动作失败 {act.get('action_id')}: {e}")
        # 更新本次自动处置的摘要
        try:
            summary_rec = {
                "timestamp": _nms_now_iso(),
                "total_actions": dispatched,
                "action_types": sorted({a["action_type"] for a in actions[:dispatched]}),
                "by_type": {t: sum(1 for a in actions if a["action_type"] == t)
                             for t in {a["action_type"] for a in actions}},
            }
            await r.set(
                f"{config.REDIS_PREFIX}:nms_alarms:remediation_summary",
                json.dumps(summary_rec, ensure_ascii=False),
            )
        except Exception:
            pass
        logger.info(f"[nms_agent] A2 自动处置分派: dispatched={dispatched} "
                    f"types={[a['action_type'] for a in actions[:dispatched]]}")

    async def _cleanup(self):
        """清理浏览器资源（退出路径必须有界：playwright 与 node 子进程通信卡住时
        不能拖到 systemd SIGKILL，否则遗留 node/chromium 孤儿进程）"""
        if self._browser:
            try:
                await asyncio.wait_for(self._browser.close(), timeout=3)
            except Exception as e:
                logger.debug(f"[nms_agent] 浏览器关闭异常(忽略): {e}")
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=3)
            except Exception as e:
                logger.debug(f"[nms_agent] playwright 停止异常(忽略): {e}")
        await super()._cleanup()


if __name__ == "__main__":
    run_agent(NmsAgent())
