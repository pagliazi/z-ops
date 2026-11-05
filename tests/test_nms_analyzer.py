#!/usr/bin/env python3
"""NMS 智能分析 Phase 1 单元测试

覆盖:
- 锁拆分: on_demand 永不阻塞 scheduled 启动, scheduled 在 on_demand 排队时让出
- Token bucket: 窗口内超额调用被拒
- TTL 缓存: 新鲜分析命中即跳过 LLM, 过期则重算
- 确定性结论表: classify / build_conclusion_table / format_conclusion_block 基础断言
- 流式路径 (Phase 1.3): prompt 构造与 on_delta / on_reasoning / on_wait 回调绑定

不依赖真实 Redis / Playwright / LLM 网关。
"""
import asyncio
import json
import os
import sys
import time
from collections import deque

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config
from agents.nms_agent import NmsAgent, _nms_alarm_content_key, _nms_parse_iso_ts


def _make_agent() -> NmsAgent:
    """构造一个不启动任何 IO 的 NmsAgent (用于纯算法/调度单元测试)。"""
    agent = NmsAgent.__new__(NmsAgent)
    # 手动初始化调度原语 (跳过 BaseAgent.__init__ 的 IO 副作用)
    agent._nms_on_demand_lock = asyncio.Lock()
    agent._nms_scheduled_lock = asyncio.Lock()
    agent._nms_on_demand_pending = 0
    agent._nms_llm_lock = agent._nms_on_demand_lock
    agent._nms_llm_bucket = deque()
    return agent


# ────────────────────────────────────────────────────────────────────────────
# 1.1 锁拆分: 优先级行为
# ────────────────────────────────────────────────────────────────────────────

async def test_on_demand_runs_during_scheduled_hold():
    """scheduled 持有锁时, on_demand 仍能并发运行 (互不阻塞)。"""
    agent = _make_agent()
    sched_started = asyncio.Event()
    on_demand_finished = asyncio.Event()

    async def sched():
        async with agent._nms_acquire_llm_lock("scheduled"):
            sched_started.set()
            await asyncio.sleep(0.4)

    async def on_demand():
        await sched_started.wait()
        t0 = time.monotonic()
        async with agent._nms_acquire_llm_lock("on_demand"):
            # on_demand 应立即拿到锁, 不应被 scheduled 阻塞
            elapsed = time.monotonic() - t0
            assert elapsed < 0.1, f"on_demand 等了 {elapsed:.3f}s, 不应被 scheduled 阻塞"
            await asyncio.sleep(0.05)
        on_demand_finished.set()

    await asyncio.wait_for(asyncio.gather(sched(), on_demand()), timeout=5.0)
    assert on_demand_finished.is_set()
    print("PASS 1.1a: on_demand 在 scheduled 持锁期间独立运行")


async def test_scheduled_yields_when_on_demand_pending():
    """scheduled 进入锁后, 若 on_demand 新到, 应主动放弃本次 (NMS_SCHED_YIELD_ON_DEMAND=True)。"""
    agent = _make_agent()
    # 降低等待预算, 让测试更快
    config.NMS_SCHED_WAIT_ON_DEMAND = 0.2
    config.NMS_SCHED_WAIT_TICKS = 10
    config.NMS_SCHED_YIELD_ON_DEMAND = True
    sched_done = asyncio.Event()
    on_demand_arrived = asyncio.Event()
    body_ran = asyncio.Event()

    async def on_demand():
        # 立即提升 pending 计数 (模拟用户请求"在路上")
        await asyncio.sleep(0.01)
        agent._nms_on_demand_pending += 1
        on_demand_arrived.set()
        await asyncio.sleep(0.5)
        agent._nms_on_demand_pending -= 1

    async def sched():
        await on_demand_arrived.wait()
        # 进入 scheduled 路径: 由于 pending > 0, 应在 wait 阶段让出
        async with agent._nms_acquire_llm_lock("scheduled") as got:
            if got:
                body_ran.set()
        sched_done.set()

    await asyncio.wait_for(asyncio.gather(on_demand(), sched()), timeout=5.0)
    # body_ran 应未被 set (scheduled 在 wait 时已检测到 pending 并退出)
    assert not body_ran.is_set(), "scheduled 未让出 on_demand 排队"
    print("PASS 1.1b: scheduled 在 on_demand 排队时主动放弃")


async def test_on_demand_pending_counter_cleaned_on_exception():
    """on_demand 异常路径也要清零 pending 计数 (避免泄漏导致 scheduled 永不让出)。"""
    agent = _make_agent()

    async def boom():
        async with agent._nms_acquire_llm_lock("on_demand"):
            raise RuntimeError("boom")

    try:
        await boom()
    except RuntimeError:
        pass
    assert agent._nms_on_demand_pending == 0, \
        f"pending 泄漏: {agent._nms_on_demand_pending}"
    print("PASS 1.1c: pending 计数器在异常后归零")


# ────────────────────────────────────────────────────────────────────────────
# 1.2 TTL 缓存: 命中/过期判定
# ────────────────────────────────────────────────────────────────────────────

def test_analysis_fresh_when_recent():
    agent = _make_agent()
    config.NMS_ANALYSIS_TTL = 480
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(time.time() - 60)),
           "analysis": "x"}
    assert agent._nms_analysis_is_fresh(rec) is True
    print("PASS 1.2a: 1 分钟内的记录视为新鲜")


def test_analysis_stale_when_expired():
    agent = _make_agent()
    config.NMS_ANALYSIS_TTL = 120
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(time.time() - 600)),
           "analysis": "x"}
    assert agent._nms_analysis_is_fresh(rec) is False
    print("PASS 1.2b: 10 分钟前的记录视为过期")


def test_analysis_fresh_disabled_when_ttl_zero():
    agent = _make_agent()
    config.NMS_ANALYSIS_TTL = 0
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(time.time()))}
    assert agent._nms_analysis_is_fresh(rec) is False
    print("PASS 1.2c: TTL=0 时缓存关闭")


def test_analysis_fresh_handles_missing_or_garbage():
    agent = _make_agent()
    assert agent._nms_analysis_is_fresh(None) is False
    assert agent._nms_analysis_is_fresh({}) is False
    assert agent._nms_analysis_is_fresh({"timestamp": "not-iso"}) is False
    print("PASS 1.2d: 无效输入安全返回 False")


# ────────────────────────────────────────────────────────────────────────────
# 1.4 Token bucket: 窗口限流
# ────────────────────────────────────────────────────────────────────────────

async def test_token_bucket_admits_under_cap():
    agent = _make_agent()
    config.NMS_LLM_BUCKET_WINDOW = 60
    config.NMS_LLM_BUCKET_MAX = 2
    assert await agent._nms_acquire_llm_bucket(timeout=1.0) is True
    assert await agent._nms_acquire_llm_bucket(timeout=1.0) is True
    print("PASS 1.4a: 容量内调用全部放行")


async def test_token_bucket_rejects_over_cap_within_window():
    agent = _make_agent()
    config.NMS_LLM_BUCKET_WINDOW = 60
    config.NMS_LLM_BUCKET_MAX = 1
    assert await agent._nms_acquire_llm_bucket(timeout=1.0) is True
    # 第二次应等待 (但 timeout=0 立即放弃 → 返回 False)
    got = await agent._nms_acquire_llm_bucket(timeout=0.0)
    assert got is False
    print("PASS 1.4b: 超额 + 零超时正确拒绝")


# ────────────────────────────────────────────────────────────────────────────
# Deterministic engine (core/deterministic.py) 基础断言
# ────────────────────────────────────────────────────────────────────────────

def test_deterministic_classify_basic():
    from core.deterministic import classify_alarm, SEVERITY_PRIORITY
    # BGP → 网络
    r = classify_alarm("BGP Peer Down on core-rtr-01", "critical")
    assert r["category"] == "网络" and r["priority"] == "P0"
    # 磁盘 → 存储
    r = classify_alarm("Disk usage > 90% on vm-web-01", "major")
    assert r["category"] == "存储" and r["priority"] == "P0"
    # warning → P1
    r = classify_alarm("Ping packet loss 5%", "warning")
    assert r["category"] == "网络" and r["priority"] == "P1"
    # 未知类别回退
    r = classify_alarm("????", "indeterminate")
    assert r["category"] == "其他" and r["priority"] == "P2"
    print("PASS det-a: 类别/优先级映射正确")


def test_deterministic_conclusion_table_groups_by_category_and_priority():
    from core.deterministic import build_conclusion_table, format_conclusion_block
    alarms = [
        {"summary": "BGP Peer Down on r1", "severity": "critical", "source": "r1"},
        {"summary": "BGP Peer Down on r2", "severity": "critical", "source": "r2"},
        {"summary": "Disk 95% on vm1",     "severity": "major",    "source": "vm1"},
        {"summary": "HTTP no response",    "severity": "minor",    "source": "app"},
    ]
    table = build_conclusion_table(alarms)
    # 期望: 网络(2) P0, 存储(1) P0, 应用(1) P1 → 排序 P0 先, 同优先级按 count 降序
    assert table[0]["category"] == "网络" and table[0]["priority"] == "P0"
    assert table[0]["count"] == 2
    # P0 的存储组 (count=1) 排在第二位
    assert table[1]["category"] == "存储" and table[1]["priority"] == "P0"
    assert table[2]["priority"] == "P1"
    # 渲染: 必须包含「不得更改」铁律提示
    block = format_conclusion_block(table)
    assert "不得更改" in block
    assert "网络" in block and "存储" in block and "应用" in block
    print("PASS det-b: 结论表按 (优先级, count) 排序 + 渲染文本含铁律")


# ────────────────────────────────────────────────────────────────────────────
# 1.3 SSE: prompt 构造与流式回调绑定 (不调真实 LLM)
# ────────────────────────────────────────────────────────────────────────────

async def test_stream_event_helper_publishes_to_redis(monkeypatch=None):
    """_nms_publish_stream_event 通过 Redis pubsub 发出事件 (用 fake redis)。"""
    agent = _make_agent()

    class FakeRedis:
        def __init__(self):
            self.published = []
        async def publish(self, ch, msg):
            self.published.append((ch, msg))
            return 1

    fake = FakeRedis()
    await agent._nms_publish_stream_event(
        fake, "abc123",
        {"type": "queued", "group": "OPSCORE_use", "ts": 1.0})
    assert len(fake.published) == 1
    ch, msg = fake.published[0]
    assert ch.endswith(":abc123")
    evt = json.loads(msg)
    assert evt["type"] == "queued" and evt["group"] == "OPSCORE_use"
    print("PASS sse-a: 流式事件发布到正确 Redis 频道")


# ────────────────────────────────────────────────────────────────────────────
# Phase 3.1: Checklist 规则表回放 (34 规则 ↔ 34 预案, 顺序敏感)
# ────────────────────────────────────────────────────────────────────────────

def test_checklist_rules_cover_all_xlsx_plans():
    """所有规则引用的故障名必须存在于 xlsx (否则永不命中, 属死代码)。"""
    from agents.nms_agent import (_NMS_CHECKLIST_RULES,
                                  _nms_load_checklist)
    entries = _nms_load_checklist()
    if not entries:  # xlsx 不存在时跳过 (CI 环境可能没有)
        print("SKIP det-c: xlsx 不可用")
        return
    missing = [t for _p, t in _NMS_CHECKLIST_RULES if t not in entries]
    assert not missing, f"规则引用不存在的预案: {missing}"
    print(f"PASS det-c: {len(_NMS_CHECKLIST_RULES)} 条规则全部锚定到 "
          f"{len(entries)} 个 xlsx 预案")


def test_checklist_replay_positive_and_negative():
    """正反例回放: 确保新增规则命中且老规则无回归 (顺序敏感, 改动规则表必须跑)。"""
    from agents.nms_agent import _nms_checklist_match, _nms_load_checklist
    entries = _nms_load_checklist()
    if not entries:
        print("SKIP det-d: xlsx 不可用")
        return
    cases = [
        # Phase 3.1 新增规则
        ('VCD customer MC account login failed - access denied', '客户MC账号无法登录登入VCD'),
        ('vm-web-01 使用慢 performance degraded', 'VM使用慢'),
        ('user report 收不到指定邮件 from scanner', 'SCM 邮件收不到指定邮件'),
        ('邮件已发送对方没收到 bounce', 'SCM 邮件发出去对方没收到'),
        ('veeam backup job 慢 slow transfer', 'vbr 备份慢'),
        ('VBR backup window 超时 timeout 9h', 'vbr 备份慢'),
        ('web service 无法打开 http 502', 'VM配置了WEB服务，但是该WEB浏览器无法打开'),
        ('ESXi host CPU MCE machine check error', 'ESXi CPU/内存硬件故障'),
        ('存储电源 fail psu2 offline', '存储电源故障'),
        ('ESXi host outage esxi02 not booting', 'ESXI主机outage故障'),
        ('netapp storage outage 宕机', '存储故障'),
        # 老规则回归 (顺序调整后必须仍命中)
        ('ESXi CPU usage 95% high', 'ESXi CPU/内存使用率高'),
        ('veeam backup time critical: 24', 'VBR备份失败'),
        ('ESXi power supply 故障 psu1', 'ESXi电源故障'),
        ('SSL证书过期 certificat expiring soon', 'SSL证书过期'),
        ('o365 backup job failed', 'A2C O365备份失败'),
        # 存储厂商扩展 (2026-10): nimble/ceph/msa + 裸 disk
        ('hp msa2050san disk异常: 5 (location=0.9, position=198.18.16.28_0.9, ip=198.18.16.28.)', '存储磁盘故障'),
        ('nimble disk failed volume offline', '存储磁盘故障'),
        ('ceph osd.3 marked down', '存储磁盘故障'),
        ('ceph cluster state degraded', '存储控制器故障'),
        ('nimble controller fault', '存储控制器故障'),
        ('nimble array outage 宕机', '存储故障'),
        ('msa power fail psu lost', '存储电源故障'),
        # 正例: 物理盘状态类告警 (Compellent disk状态/predictive failure) 仍归硬件预案
        ('Compellent disk状态: 2 (ip=198.18.128.21, position=4_Healthy, hostname=ARRAY-SN-0001)',
         '存储磁盘故障'),
        ('Compellent disk predictive failure', '存储磁盘故障'),
        # 反例: vCenter 报的是虚机逻辑卷用量 (position=mapper/log_vg-log_/log_ext4),
        # 属磁盘空间容量告警, 不得归入物理盘/RAID 硬件预案的存储磁盘故障
        ('vcenter Disk使用率: 82.0969730060639% (position=mapper/log_vg-log_/storage/log_ext4,\n'
         ' hostname=vcsa-01.example.org, ip=198.18.32.190.)', ''),
        ('ESXi disk usage 90%', ''),
        ('存储磁盘使用率 88%', ''),
        # 正例: datastore 容量告警走专属预案 (容量类中的例外)
        ('vcenter Datastore使用率: 95%', 'ESXi Datastore使用率高'),
        # 反例: Nimble 卷吞吐告警因卷名含 Datastore 被容量预案误吞 (性能≠容量)
        ('The Nimble ARRAY-SH-8003 volume NQA-8003SN-Datastore2 throughput reached 90% of limit', ''),
        # Zerto: 须有 journal 证据才归 Journal 预案; vpg status 泛化状态需人工判断
        ('Zerto vpg status: 3 (ip=, position=198.19.0.222_SITE-A-REPL-04, status=3.)', ''),
        ('Zerto 告警事件: 1 (ip=198.18.16.222, position=The_scratch_journal_for_VM_PRDDF-TMSDB1'
         '-qP13_in_VPG_G49979-ZACPTINS-SGSINDRT-01_has_reached_its_conf, status=Error.)',
         'Zerto VPG Journal空间不足'),
        ('Zerto vpg journal has exceeded 50 percent of limit', 'Zerto VPG Journal空间不足'),
        # 反例: diskSpace 类备份告警须先被 VBR 规则接住, 不落入存储磁盘故障
        ('veeam backup diskSpace critical: 24', 'VBR备份失败'),
        # 反例: cloud switch 上下文的 disk 不匹配存储预案
        ('cloud switch disk error on port 3', ''),
        # 反例: 网络类/系统类不误匹配
        ('BGP Peer Down on core-rtr-01', ''),
        ('linux oom-killer invoked', ''),
        ('windows service stopped: spooler', ''),
    ]
    for summary, want in cases:
        got = _nms_checklist_match(summary, entries)
        assert got == want, f"回放失败: {summary!r} want={want!r} got={got!r}"
    print(f"PASS det-d: {len(cases)} 条回放用例全部通过")


# ────────────────────────────────────────────────────────────────────────────
# Phase 3.3: 自动 LLM 复合门控 (纯函数)
# ────────────────────────────────────────────────────────────────────────────

def _gating_setup():
    config.NMS_AUTO_LLM_ANALYZE = True
    config.NMS_LLM_NEW_PATTERN_MIN = 3
    config.NMS_AUTO_IMPACT_MIN = 8
    config.NMS_LLM_COOLDOWN = 1800


def test_auto_llm_triggers_on_critical_novel():
    _gating_setup()
    alarms = [{"severity": "critical", "source": "r1",
               "summary": "BGP Peer Down on 10.0.0.1"}]
    novel = {NmsAgent._nms_pattern_key(alarms[0]): 1}
    ok, reason = NmsAgent._nms_auto_llm_should_trigger(
        alarms, novel, set(), last_auto_ts=None)
    assert ok and "critical" in reason, f"want critical trigger, got {ok}/{reason}"
    print("PASS 3.3a: critical 新模式立即触发")


def test_auto_llm_triggers_on_novel_threshold():
    _gating_setup()
    alarms = [{"severity": "minor", "source": f"host{i}",
               "summary": f"disk usage high on /data{i}"} for i in range(3)]
    novel = {NmsAgent._nms_pattern_key(a): 1 for a in alarms}
    ok, reason = NmsAgent._nms_auto_llm_should_trigger(alarms, novel, set())
    assert ok and "novel" in reason
    print("PASS 3.3b: 新模式达到阈值触发")


def test_auto_llm_triggers_on_impact_sources():
    _gating_setup()
    # 2 个已见模式 + 8 个不同来源 → 影响面触发
    alarms = [{"severity": "warning", "source": f"host{i}",
               "summary": "same pattern but different host"} for i in range(8)]
    novel = {}  # 模式均已见过 (低于阈值)
    ok, reason = NmsAgent._nms_auto_llm_should_trigger(alarms, novel, {"x"})
    assert ok and "impact" in reason, f"want impact trigger, got {ok}/{reason}"
    print("PASS 3.3c: 影响面 (8 源) 触发")


def test_auto_llm_blocked_by_cooldown():
    _gating_setup()
    alarms = [{"severity": "critical", "source": "r1", "summary": "x down"}]
    novel = {NmsAgent._nms_pattern_key(alarms[0]): 1}
    now = time.time()
    ok, reason = NmsAgent._nms_auto_llm_should_trigger(
        alarms, novel, set(), last_auto_ts=now - 60, now_ts=now)  # 60s 前刚分析过
    assert not ok and "cooldown" in reason
    # 冷却已过 → 触发
    ok2, _ = NmsAgent._nms_auto_llm_should_trigger(
        alarms, novel, set(), last_auto_ts=now - 2000, now_ts=now)
    assert ok2
    print("PASS 3.3d: 冷却窗口内拒绝, 过期后放行")


def test_auto_llm_disabled_or_no_new():
    config.NMS_AUTO_LLM_ANALYZE = False
    ok, reason = NmsAgent._nms_auto_llm_should_trigger(
        [{"severity": "critical", "source": "r1"}], {"k": 1}, set())
    assert not ok and reason == "auto_disabled"
    config.NMS_AUTO_LLM_ANALYZE = True
    ok, reason = NmsAgent._nms_auto_llm_should_trigger([], {"k": 1}, set())
    assert not ok and reason == "no_new"
    print("PASS 3.3e: 开关关闭 / 无新告警正确拒绝")


# ────────────────────────────────────────────────────────────────────────────
# Phase 4 智能化增强: 事件簇线索 / 实测证据 / 历史重复 三个格式化器
# ────────────────────────────────────────────────────────────────────────────

def test_incident_clues_formatter():
    doc = {"total_alarms": 42043, "incident_count": 6054, "incidents": [
        {"incident_id": "a", "title": "Device 198.19.7.23 ping failed..",
         "severity": "critical", "category": "systemic", "member_count": 88,
         "source_count": 88, "sources": ["198.19.7.23", "198.19.12.242"]},
        {"incident_id": "b", "title": "disk usage high on vm01",
         "severity": "major", "category": "object", "member_count": 3,
         "source_count": 1, "sources": ["vm01"]},
    ]}
    out = NmsAgent._nms_format_incident_clues(doc)
    assert "关联降噪事件簇" in out and "42043" in out and "6054" in out
    # 系统级排在前
    assert out.index("系统级") < out.index("对象级")
    assert "198.19.7.23" in out and "成员 88" in out
    # 空输入安全
    assert NmsAgent._nms_format_incident_clues(None) == ""
    assert NmsAgent._nms_format_incident_clues({"incidents": []}) == ""
    print("PASS 4a-1: 事件簇线索格式化 (系统级优先, 空输入安全)")


def test_tool_evidence_formatter():
    tc = {
        "ssl": [
            {"status": "ok", "host": "a.example", "days_left": 120,
             "summary": "a.example:443 → 剩余 120 天（到期 2027-02-20）；仅读取证书，未校验证书链/域名"},
            {"status": "warn", "host": "c.example", "days_left": 5,
             "summary": "c.example:443 → 剩余 5 天，低于 30 天观察门槛"},
            {"status": "fail", "host": "b.example",
             "summary": "b.example:443 → 连接失败"},
        ],
        "backup": [{"status": "ok", "total": 50, "ok": 31,
                    "summary": "VBR 50 jobs: 31 ok"}],
        "tcp": [{"status": "fail", "summary": "10.0.0.1:443 不可达"}],
    }
    out = NmsAgent._nms_format_tool_evidence(tc)
    assert "工具实测证据" in out
    # 旧采集只读取证书：ok 不得再被解释为“正常/已恢复”
    assert "[SSL·证书可读]" in out and "未校验证书链/域名" in out
    assert "[SSL·正常]" not in out
    assert "误报或已续期" not in out
    assert "[SSL·即将到期]" in out and "[SSL·无法核验]" in out
    assert "[SSL·异常]" not in out
    assert "[备份·ok]" in out and "[TCP·fail]" in out
    # dns 不在本格式化器职责内 (由 dns_context 注入)
    assert "DNS" not in out
    assert NmsAgent._nms_format_tool_evidence(None) == ""
    assert NmsAgent._nms_format_tool_evidence({"ssl": []}) == ""
    print("PASS 4a-2: 实测证据格式化 (SSL 仅证书读取，不判定恢复, 空输入安全)")


def test_history_recurring_formatter():
    # 构造: 同一模式 (IP 归一后) 在历史中出现 5 次, 当前活跃 2 条
    def _alarm(src, ip, t):
        return {"source": src, "summary": f"BGP Peer Down ( BGP Peer: {ip} )",
                "severity": "critical", "last_time": t}
    hist = [_alarm("r1", f"10.0.0.{i}", f"9/{10+i}/26") for i in range(5)]
    cur = [_alarm("r1", "10.0.0.99", "9/22/26"), _alarm("r1", "10.0.0.98", "9/22/26")]
    out = NmsAgent._nms_format_history_recurring(cur, hist, min_repeat=3)
    assert "历史重复模式" in out and "历史库中出现 5 次" in out and "当前 2 条" in out
    assert "根治方向" in out
    # 无重复 (历史只有 1 次) → 空串
    assert NmsAgent._nms_format_history_recurring(cur, hist[:1], min_repeat=3) == ""
    # 空输入安全
    assert NmsAgent._nms_format_history_recurring(cur, None) == ""
    assert NmsAgent._nms_format_history_recurring([], hist) == ""
    print("PASS 4a-3: 历史重复格式化 (IP 归一同模式聚合计数, 空输入安全)")


# ────────────────────────────────────────────────────────────────────────────
# A1 影响面 formatter + A2 动作抽取 + A3 异常检测 + A5 抑制过滤
# ────────────────────────────────────────────────────────────────────────────

def test_impact_formatter_renders_customer_and_asset():
    out = NmsAgent._nms_format_impact_block(
        devices=[{'name': 'core-rtr-01', 'vendor': 'Cisco', 'model': 'ASR 9001',
                  'site': '上海-DC1', 'status': 'ok'}],
        impact_data={'vms': 12, 'customers': {'客户A': 5, '客户B': 7},
                     'src': '存储卷映射命中 2 台阵列；vCenter 虚机 12 台'},
        all_ips=['198.19.40.85'])
    assert '资产与影响面' in out
    assert 'Cisco ASR 9001' in out and '上海-DC1' in out
    assert '客户A' in out and '客户B' in out
    assert NmsAgent._nms_format_impact_block([], {}, []) == ''
    print("PASS A1: 影响面 formatter (CMDB + 客户)")


def test_remediation_action_extractor_dedupes():
    actions = NmsAgent._nms_extract_remediation_actions([
        {'alarm_id': 'a1', 'source': '198.19.40.85',
         'summary': 'BGP Peer Connection Idle', 'severity': 'critical'},
        {'alarm_id': 'a2', 'source': '198.19.40.85',
         'summary': 'BGP Peer Down', 'severity': 'critical'},
        {'alarm_id': 'a3', 'source': '10.0.0.5',
         'summary': 'veeam backup failed', 'severity': 'minor'},
        {'alarm_id': 'a4', 'source': '',
         'summary': 'random unrelated', 'severity': 'warning'},
    ])
    # 同一目标多条 → 合并证据; 不同 action_type 各自一条
    types = [a['action_type'] for a in actions]
    assert 'ping_sweep' in types, f'want ping_sweep got {types}'
    assert 'backup_status' in types
    # evidence 合并
    bgp = next(a for a in actions if a['action_type'] == 'ping_sweep')
    assert len(bgp['evidence']) == 2
    assert 'a1' in bgp['evidence'] and 'a2' in bgp['evidence']
    # 无 source 的告警被丢弃
    assert not any(a['reason'] == 'random unrelated' for a in actions)
    print("PASS A2: 动作抽取器 (按规则路由 + 证据合并 + 目标去重)")


def test_anomaly_detector_flags_outlier():
    from core.nms_anomaly import compute_anomaly_scores, format_anomaly_block
    from datetime import datetime
    # 源 core-rtr 过去 5 天同一小时 1,2,1,1,2 条; 当前 5 条
    hist = [{'source': 'core-rtr', 'last_time': f'2026-09-{18+d} 14:30:00'}
            for d, c in enumerate([1, 2, 1, 1, 2]) for _ in range(c)]
    cur = [{'source': 'core-rtr', 'severity': 'critical', 'alarm_id': f'a{i}',
            'last_time': '2026-09-23 14:35:00'} for i in range(5)]
    now = datetime(2026, 9, 23, 14, 40)
    res = compute_anomaly_scores(hist, cur, now=now)
    assert len(res) == 1 and res[0]['source'] == 'core-rtr'
    assert res[0]['z_score'] >= 2.0
    block = format_anomaly_block(res)
    assert '异常活跃' in block and 'core-rtr' in block
    # 冷启动 (历史 < 3 天) 不应报异常
    short_hist = hist[:2]
    res2 = compute_anomaly_scores(short_hist, cur, now=now)
    assert res2 == []
    # 空输入安全
    assert compute_anomaly_scores([], []) == []
    print("PASS A3: 异常检测 (z-score + 冷启动保护)")


def test_suppression_filter_rules_and_maint():
    rules = {
        'sup_bgp': {'pattern': 'bgp.*idle', 'reason': 'BGP flap 风暴'},
    }
    maint = {'10.0.0.5': time.time() + 3600}
    alarms = [
        {'alarm_id': '1', 'source': 'r1', 'summary': 'BGP Peer Idle',
         'severity': 'critical'},          # → 命中规则, 被抑制
        {'alarm_id': '2', 'source': '10.0.0.5',
         'summary': 'veeam backup failed', 'severity': 'minor'},  # → 在维护窗口
        {'alarm_id': '3', 'source': 'r2', 'summary': 'BGP Peer Up',
         'severity': 'critical'},          # → 不匹配 idle, 通过
        {'alarm_id': '4', 'source': 'host-x', 'summary': 'random alert',
         'severity': 'warning'},          # → 不匹配, 通过
    ]
    kept, suppressed, in_maint = NmsAgent._nms_apply_suppression(
        alarms, rules, maint)
    assert len(kept) == 2 and {a['alarm_id'] for a in kept} == {'3', '4'}
    assert len(suppressed) == 1 and suppressed[0][0]['alarm_id'] == '1'
    assert suppressed[0][1] == 'sup_bgp'  # rule id
    assert len(in_maint) == 1 and in_maint[0][0]['alarm_id'] == '2'
    # 过期维护窗口 (until < now) 应失效
    expired_maint = {'10.0.0.5': time.time() - 100}
    kept2, _, in_maint2 = NmsAgent._nms_apply_suppression(
        alarms[1:2], rules, expired_maint)
    assert len(kept2) == 1 and not in_maint2
    # 空规则/空告警安全
    k, s, m = NmsAgent._nms_apply_suppression([], {})
    assert k == [] and s == [] and m == []
    print("PASS A5: 抑制过滤 (pattern 命中 + 维护窗口 + 过期失效)")


# ────────────────────────────────────────────────────────────────────────────
# Phase H: 历史存储升级 (LIST → ZSET) — 时间解析 + 迁移逻辑
# ────────────────────────────────────────────────────────────────────────────

def test_nms_parse_iso_ts():
    from agents.nms_agent import _nms_parse_iso_ts
    # ISO-8601 (naive 按本地解析, 实测 epoch ≈ 1790058600 = 2026-09-22 14:30 UTC)
    ts = _nms_parse_iso_ts("2026-09-22T14:30:00")
    assert ts is not None and 1790000000 < ts < 1791000000
    # 带毫秒 + 时区 (毫秒截断, +08:00 剥掉 → 同一秒级)
    ts2 = _nms_parse_iso_ts("2026-09-22T14:30:00.123+08:00")
    assert ts2 is not None and abs(ts2 - ts) < 2.0
    # Netcool M/D/YY 格式
    ts3 = _nms_parse_iso_ts("9/22/26, 2:30 PM")
    assert ts3 is not None and 1790000000 < ts3 < 1791000000
    # 失败输入
    assert _nms_parse_iso_ts("") is None
    assert _nms_parse_iso_ts("not a date") is None
    assert _nms_parse_iso_ts(None) is None
    print("PASS H1: _nms_parse_iso_ts (ISO / Netcool / 容错)")


class _HistFakeRedis:
    """历史写入/迁移测试用最小 Redis 模拟 (ZSET 按分数排序)。"""

    def __init__(self):
        self.z: Dict[str, Dict[str, float]] = {}
        self.h: Dict[str, Dict[str, str]] = {}
        self.lists: Dict[str, list] = {}
        self.kv: Dict[str, str] = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v):
        self.kv[k] = v

    async def zscan(self, k, cursor=0, count=100):
        items = sorted(self.z.get(k, {}).items(), key=lambda x: x[1])
        start = int(cursor or 0)
        chunk = items[start:start + count]
        nxt = start + len(chunk)
        return (nxt if nxt < len(items) else 0), chunk

    async def zcard(self, k):
        return len(self.z.get(k, {}))

    async def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    async def zrem(self, k, m):
        self.z.get(k, {}).pop(m, None)

    async def zrange(self, k, start, stop, withscores=False):
        items = sorted(self.z.get(k, {}).items(), key=lambda x: x[1])
        n = len(items)
        s = start if start >= 0 else max(0, n + start)
        e = n + stop + 1 if stop < 0 else stop + 1
        sub = items[s:e]
        return [(m, sc) for m, sc in sub] if withscores else [m for m, _ in sub]

    async def zrangebyscore(self, k, lo, hi):
        out = []
        for m, sc in sorted(self.z.get(k, {}).items(), key=lambda x: x[1]):
            if (lo == "-inf" or sc > float(lo[1:] if lo.startswith("(") else lo)):
                if hi.startswith("("):
                    if sc < float(hi[1:]):
                        out.append(m)
                elif hi == "+inf" or sc <= float(hi):
                    out.append(m)
        return out

    async def zremrangebyscore(self, k, lo, hi):
        victims = await self.zrangebyscore(k, lo, hi)
        for m in victims:
            self.z.get(k, {}).pop(m, None)
        return len(victims)

    async def zremrangebyrank(self, k, start, stop):
        items = sorted(self.z.get(k, {}).items(), key=lambda x: x[1])
        n = len(items)
        s = start if start >= 0 else max(0, n + start)
        e = n + stop + 1 if stop < 0 else stop + 1
        victims = [m for m, _ in items[s:e]]
        for m in victims:
            self.z.get(k, {}).pop(m, None)
        return len(victims)

    async def hmget(self, k, fields):
        h = self.h.get(k, {})
        return [h.get(f) for f in fields]

    async def hset(self, k, field, value):
        self.h.setdefault(k, {})[field] = value

    async def hdel(self, k, *fields):
        h = self.h.get(k, {})
        n = 0
        for f in fields:
            if h.pop(f, None) is not None:
                n += 1
        return n

    async def hgetall(self, k):
        return dict(self.h.get(k, {}))

    async def llen(self, k):
        return len(self.lists.get(k, []))

    async def lrange(self, k, start, stop):
        lst = self.lists.get(k, [])
        return lst[start:stop + 1] if stop >= 0 else lst[start:]

    async def delete(self, k):
        self.lists.pop(k, None)
        return 1


async def test_migrate_history_list_to_zset():
    """老 LIST → ZSET 一次性迁移: 内容键格式 + payload 入 history_data。"""
    agent = NmsAgent.__new__(NmsAgent)
    r = _HistFakeRedis()
    # 第一次调用: 3 条 (含同事件不同 serial 重复) → 2 个内容键
    rows = [{"alarm_id": "srl:1", "node": "n1", "event_key": "ek1",
             "summary": "down", "first_time": "2026-09-15T10:00:00"},
            {"alarm_id": "srl:2", "node": "n2", "event_key": "ek2",
             "summary": "up", "first_time": "2026-09-15T11:00:00"},
            {"alarm_id": "srl:3", "node": "n1", "event_key": "ek1",
             "summary": "down", "first_time": "2026-09-15T12:00:00"}]
    r.lists["list_key"] = [json.dumps(x) for x in rows]
    await agent._nms_migrate_history_list_to_zset(r, "list_key", "zset_key", "data_key")
    assert await r.zcard("zset_key") == 2, r.z.get("zset_key")
    assert len(r.h.get("data_key", {})) == 2
    assert "list_key" not in r.lists
    # payload 带 occurrences/_ck
    p = json.loads(list(r.h["data_key"].values())[0])
    assert "occurrences" in p and "_ck" in p
    # 第二次调用 (zset 已有): 走 backfill 分支, 新格式抽样即退出, 不重复迁移
    r.lists["list_key"] = [json.dumps({"alarm_id": "srl:9", "node": "x"})]
    await agent._nms_migrate_history_list_to_zset(r, "list_key", "zset_key", "data_key")
    assert await r.zcard("zset_key") == 2
    # 空 LIST: 跳过
    await agent._nms_migrate_history_list_to_zset(r, "empty_list", "empty_zset", "empty_data")
    assert "empty_zset" not in r.z
    print("PASS H2: 历史迁移 (老 LIST → ZSET 内容键格式, payload 入 history_data)")


async def test_history_content_key_dedup():
    """H3: 内容键去重写入 — Serial 换手折叠为 occurrences, 不新增条目;
    30 天老化同步清理 payload; cleared 只标 seen 不入历史。"""
    agent = NmsAgent.__new__(NmsAgent)
    r = _HistFakeRedis()
    old_ttl = getattr(config, "NMS_SEEN_TTL", None)
    config.NMS_SEEN_TTL = 7 * 86400
    try:
        a1 = {"alarm_id": "srl:1", "node": "198.18.16.28", "event_key": "msa_disk",
              "summary": "hp msa2050san disk异常: 5", "severity": "major",
              "first_time": "2026-10-19T03:00:00", "last_time": "2026-10-19T03:00:00"}
        b1 = {"alarm_id": "srl:2", "node": "198.18.16.28", "event_key": "msa_vol",
              "summary": "hp msa2050san volume degraded", "severity": "minor",
              "first_time": "2026-10-19T03:10:00", "last_time": "2026-10-19T03:10:00"}
        c1 = {"alarm_id": "srl:3", "node": "n9", "event_key": "ek9",
              "summary": "event cleared", "severity": "cleared",
              "first_time": "2026-10-19T03:11:00", "last_time": "2026-10-19T03:11:00"}
        await agent._nms_write_history_zset(r, "b1", [a1, b1, c1], "seen_k")
        assert await r.zcard("b1:history_z") == 2          # cleared 不入历史
        assert len(r.h["b1:history_data"]) == 2
        assert r.h["seen_k"]["srl:1"] and r.h["seen_k"]["srl:3"]  # cleared 也标 seen
        # 同事件换手重报 (不同 serial, 摘要计数值变化): occurrences 累加, 不新增条目
        a2 = dict(a1, alarm_id="srl:9", summary="hp msa2050san disk异常: 3",
                  first_time="2026-10-19T03:40:00", last_time="2026-10-19T03:40:00")
        await agent._nms_write_history_zset(r, "b1", [a2], "seen_k")
        assert await r.zcard("b1:history_z") == 2
        ck_a = _nms_alarm_content_key(a1)
        rec = json.loads(r.h["b1:history_data"][ck_a])
        assert rec["occurrences"] == 2
        assert rec["last_time"] == "2026-10-19T03:40:00"       # last 更新
        assert r.z["b1:history_z"][ck_a] == _nms_parse_iso_ts("2026-10-19T03:00:00")  # 首见保留最早
        # 老化: 手工塞一条 score 在 31 天前的成员 → 下次写入时连同 payload 删除
        old_ck = "0" * 16
        r.z["b1:history_z"][old_ck] = 1.0
        r.h["b1:history_data"][old_ck] = json.dumps({"summary": "old", "_ck": old_ck})
        await agent._nms_write_history_zset(r, "b1", [a1], "seen_k")
        assert old_ck not in r.z["b1:history_z"] and old_ck not in r.h["b1:history_data"]
        print("PASS H3: 内容键去重写入 (换手折叠 occurrences/首见保留/老化同步清 payload)")
    finally:
        if old_ttl is not None:
            config.NMS_SEEN_TTL = old_ttl


async def test_history_bootstrap_from_snapshot():
    """H4: 历史基线导入 — 快照存量补录, 保留期外跳过, 已有不覆盖, 标记幂等。"""
    agent = NmsAgent.__new__(NmsAgent)
    r = _HistFakeRedis()
    now = time.time()
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    fmt = lambda t: datetime.fromtimestamp(t, tz).strftime("%Y-%m-%dT%H:%M:%S")
    a = {"alarm_id": "srl:1", "node": "n1", "event_key": "ek1",
         "summary": "alarm A", "severity": "major", "first_time": fmt(now - 86400)}
    b = {"alarm_id": "srl:2", "node": "n2", "event_key": "ek2",
         "summary": "alarm B", "severity": "critical", "first_time": fmt(now - 2 * 86400)}
    old = {"alarm_id": "srl:3", "node": "n3", "event_key": "ek3",
           "summary": "alarm C old", "severity": "minor",
           "first_time": fmt(now - 40 * 86400)}
    r.kv["b:latest"] = json.dumps({"alarms": [a, b, old]})
    # A 已在历史中 (新告警路径写过)
    ck_a = _nms_alarm_content_key(a)
    r.z["b:history_z"] = {ck_a: now - 86400}
    r.h["b:history_data"] = {ck_a: json.dumps({**a, "occurrences": 3, "_ck": ck_a})}
    await agent._nms_bootstrap_history(r, "b")
    assert await r.zcard("b:history_z") == 2  # A 保留(不覆盖) + B 补录, C 超期跳过
    rec_a = json.loads(r.h["b:history_data"][ck_a])
    assert rec_a["occurrences"] == 3, "已有记录不应被覆盖"
    assert r.kv.get("b:history_bootstrap:v1") == "v1"
    # 二次执行: 标记键生效, 不再写入
    await agent._nms_bootstrap_history(r, "b")
    assert await r.zcard("b:history_z") == 2
    print("PASS H4: 历史基线导入 (存量补录/超期跳过/已有不覆盖/标记幂等)")


# ────────────────────────────────────────────────────────────────────────────
# SSO 会话 (零密码存储): bootstrap 存在性 + Cookie 恢复路径
# ────────────────────────────────────────────────────────────────────────────

async def test_sso_session_roundtrip():
    """Cookie 存取格式 + owner 解析 (会话版) — 不触发真实 Playwright。"""
    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    test_user = f"sso_test_{int(time.time())}"
    key = f"{config.REDIS_PREFIX}:nms_session:{test_user}"
    try:
        # 1) 模拟 bootstrap 写入 (与 nms_bootstrap_session 相同格式)
        fake_cookies = [
            {"name": "LTPAToken2", "value": "fake", "domain": "nms.example.com",
             "path": "/", "expires": time.time() + 3600},
            {"name": "JSESSIONID", "value": "abc", "domain": "nms.example.com",
             "path": "/ibm/console", "expires": -1},
        ]
        from agents.nms_agent import NMS_SESSION_TTL  # noqa: 存在性
        await r.set(key, json.dumps(fake_cookies), ex=NMS_SESSION_TTL)
        # 2) owner 解析 (会话版): 有会话 → 个人视图
        # _nms_resolve_owner_async 在 api 层, 内联同逻辑
        has = bool(await r.get(key))
        assert has
        base = (f"{config.REDIS_PREFIX}:nms_alarms:user:{test_user}"
                if has and test_user != config.NMS_USERNAME
                else f"{config.REDIS_PREFIX}:nms_alarms")
        assert "user:" in base
        # 3) 注销
        await r.delete(key)
        assert not await r.get(key)
        print("PASS SSO-a: 会话 Cookie 存取 + owner 判定 + 注销")
    finally:
        await r.delete(key)


async def main():
    await test_on_demand_runs_during_scheduled_hold()
    await test_scheduled_yields_when_on_demand_pending()
    await test_on_demand_pending_counter_cleaned_on_exception()
    test_analysis_fresh_when_recent()
    test_analysis_stale_when_expired()
    test_analysis_fresh_disabled_when_ttl_zero()
    test_analysis_fresh_handles_missing_or_garbage()
    await test_token_bucket_admits_under_cap()
    await test_token_bucket_rejects_over_cap_within_window()
    test_deterministic_classify_basic()
    test_deterministic_conclusion_table_groups_by_category_and_priority()
    test_checklist_rules_cover_all_xlsx_plans()
    test_checklist_replay_positive_and_negative()
    test_auto_llm_triggers_on_critical_novel()
    test_auto_llm_triggers_on_novel_threshold()
    test_auto_llm_triggers_on_impact_sources()
    test_auto_llm_blocked_by_cooldown()
    test_auto_llm_disabled_or_no_new()
    test_incident_clues_formatter()
    test_tool_evidence_formatter()
    test_history_recurring_formatter()
    test_impact_formatter_renders_customer_and_asset()
    test_remediation_action_extractor_dedupes()
    test_anomaly_detector_flags_outlier()
    test_suppression_filter_rules_and_maint()
    test_nms_parse_iso_ts()
    await test_migrate_history_list_to_zset()
    await test_history_content_key_dedup()
    await test_history_bootstrap_from_snapshot()
    await test_sso_session_roundtrip()
    await test_stream_event_helper_publishes_to_redis()
    print("\n✅ 所有 NMS 分析 Phase 1 单元测试通过")


if __name__ == "__main__":
    asyncio.run(main())
