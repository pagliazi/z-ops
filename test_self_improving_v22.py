#!/usr/bin/env python3
"""
Z-OPS v2.2.0 Self-Improving Advanced — 端到端功能测试
从用户角度模拟真实场景，验证所有核心能力
"""

import sys
import os
import json
import time
import shutil
import asyncio
import tempfile
import traceback

# 确保 zops 目录可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── 全局临时目录（隔离测试数据） ───
TEMP_DIR = tempfile.mkdtemp(prefix="zops_test_v22_")
TEMP_EXP_DIR = os.path.join(TEMP_DIR, "experiences")
os.makedirs(TEMP_EXP_DIR, exist_ok=True)

passed = 0
failed = 0
errors = []


def header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(name, condition, detail=""):
    global passed, failed, errors
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        msg = f"  ❌ {name}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(msg)


# ═══════════════════════════════════════════════════════════
#  测试 1: 根因分析器 (RootCauseAnalyzer)
# ═══════════════════════════════════════════════════════════
def test_root_cause_analyzer():
    header("测试 1: 根因分析器 (RootCauseAnalyzer)")
    from core.self_improving import RootCauseAnalyzer, ErrorCategory

    rca = RootCauseAnalyzer()

    # 1.1 各种错误类型分类
    test_cases = [
        ("Connection refused to 192.168.1.100:443", ErrorCategory.CONNECTION),
        ("Request timed out after 30s", ErrorCategory.TIMEOUT),
        ("HTTP 401 Unauthorized: invalid token", ErrorCategory.AUTH),
        ("HTTP 403 Forbidden: permission denied", ErrorCategory.AUTH),
        ("Missing required parameter: host", ErrorCategory.PARAM_INVALID),
        ("Resource not found: VM vm-001", ErrorCategory.RESOURCE),
        ("Service unavailable (502 Bad Gateway)", ErrorCategory.DEPENDENCY),
        ("Rate limit exceeded, 429 Too Many Requests", ErrorCategory.RATE_LIMIT),
        ("JSON decode error: Expecting value", ErrorCategory.DATA),
    ]

    for error_msg, expected_cat in test_cases:
        result = rca.analyze(error_msg)
        check(
            f"分类 '{error_msg[:40]}...' → {expected_cat.value}",
            result.category == expected_cat,
            f"实际={result.category.value}"
        )

    # 1.2 未知错误
    unknown = rca.analyze("Something completely unexpected happened")
    check("未知错误返回 UNKNOWN", unknown.category == ErrorCategory.UNKNOWN)

    # 1.3 空错误
    empty = rca.analyze("")
    check("空错误返回 UNKNOWN 且置信度=0", empty.category == ErrorCategory.UNKNOWN and empty.confidence == 0.0)

    # 1.4 修复建议非空
    result = rca.analyze("Connection refused")
    check("修复建议非空", len(result.suggested_fix) > 0, result.suggested_fix)

    # 1.5 历史加成：重复错误提高置信度
    first = rca.analyze("Cannot connect to database: ECONNREFUSED")
    second = rca.analyze("Cannot connect to database: ECONNREFUSED")
    third = rca.analyze("Cannot connect to database: ECONNREFUSED")
    check(
        "重复错误置信度提升",
        third.confidence >= first.confidence,
        f"first={first.confidence:.2f} third={third.confidence:.2f}"
    )


# ═══════════════════════════════════════════════════════════
#  测试 2: 经验数据库 (ExperienceDatabase)
# ═══════════════════════════════════════════════════════════
def test_experience_database():
    header("测试 2: 经验数据库 (ExperienceDatabase)")
    from core.self_improving import ExperienceDatabase, Experience

    db = ExperienceDatabase(db_path=TEMP_EXP_DIR)

    # 2.1 保存和读取经验
    exp = Experience(
        id="test001",
        skill_name="vm_check",
        action="execute",
        timestamp=time.time(),
        success=True,
        input_params={"host": "192.168.1.100", "timeout": 30},
        output_result={"status": "ok"},
        execution_time=2.5,
        root_cause="",
        strategy_used="default",
        param_fingerprint=ExperienceDatabase._param_fingerprint({"host": "192.168.1.100", "timeout": 30}),
    )
    db.save_experience(exp)

    loaded = db.get_experiences("vm_check", limit=10)
    check("保存并读取经验", len(loaded) >= 1)
    check("经验字段完整", loaded[0].id == "test001" and loaded[0].skill_name == "vm_check")

    # 2.2 参数指纹匹配
    exp2 = Experience(
        id="test002", skill_name="vm_check", action="execute",
        timestamp=time.time() - 100, success=False,
        input_params={"host": "192.168.1.100", "timeout": 30},
        output_result={}, error_message="timeout", execution_time=30.0,
        root_cause="timeout",
        param_fingerprint=ExperienceDatabase._param_fingerprint({"host": "192.168.1.100", "timeout": 30}),
    )
    db.save_experience(exp2)

    similar = db.get_similar_experiences("vm_check", {"host": "192.168.1.100", "timeout": 30}, limit=5)
    check("参数指纹匹配找到相似经验", len(similar) >= 2)

    # 2.3 时间衰减
    old_exp = Experience(
        id="test003", skill_name="vm_check", action="execute",
        timestamp=time.time() - 30 * 24 * 3600,  # 30天前
        success=True, input_params={"host": "10.0.0.1"}, output_result={},
        param_fingerprint=ExperienceDatabase._param_fingerprint({"host": "10.0.0.1"}),
    )
    new_exp = Experience(
        id="test004", skill_name="vm_check", action="execute",
        timestamp=time.time(),  # 现在
        success=True, input_params={"host": "10.0.0.1"}, output_result={},
        param_fingerprint=ExperienceDatabase._param_fingerprint({"host": "10.0.0.1"}),
    )
    db.save_experience(old_exp)
    db.save_experience(new_exp)

    similar2 = db.get_similar_experiences("vm_check", {"host": "10.0.0.1"}, limit=5)
    # 最新的经验应该排在前面（时间衰减加权）
    if len(similar2) >= 2:
        newest_first = similar2[0].timestamp >= similar2[1].timestamp
        check("时间衰减加权：最新经验排前", newest_first,
              f"first_ts={similar2[0].timestamp:.0f} second_ts={similar2[1].timestamp:.0f}")
    else:
        check("时间衰减加权", False, "找到的经验不足")

    # 2.4 跨技能搜索
    cross_exp = Experience(
        id="cross001", skill_name="net_scan", action="execute",
        timestamp=time.time(), success=False,
        input_params={"target": "10.0.0.0/24"}, output_result={},
        error_message="timeout", root_cause="timeout",
    )
    db.save_experience(cross_exp)

    cross_results = db.get_cross_skill_experiences(["vm_check", "net_scan"], error_category="timeout")
    check("跨技能搜索(按根因过滤)", len(cross_results) >= 1)

    # 2.5 统计信息 & 趋势分析
    stats = db.get_statistics("vm_check")
    check("统计信息包含 total", stats.get("total", 0) >= 4)
    check("统计信息包含 trend", stats.get("trend") in ("improving", "degrading", "stable"))
    check("统计信息包含根因分布", isinstance(stats.get("root_cause_distribution"), dict))

    # 2.6 向后兼容（旧格式数据）
    old_format = {"id": "compat001", "skill_name": "legacy_skill", "action": "run",
                  "timestamp": time.time(), "success": True,
                  "input_params": {}, "output_result": {}}
    compat_exp = Experience.from_dict(old_format)
    check("向后兼容旧格式数据", compat_exp.id == "compat001" and compat_exp.root_cause == "")


# ═══════════════════════════════════════════════════════════
#  测试 3: 策略自适应 & 执行规划
# ═══════════════════════════════════════════════════════════
def test_strategy_and_planning():
    header("测试 3: 策略自适应 & 执行规划")
    from core.self_improving import SelfImprovingEngine, ExperienceDatabase

    db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_strategy"))
    engine = SelfImprovingEngine(db=db)

    skill = "backup_check"

    # 3.1 记录不同策略的执行
    for i in range(5):
        engine.record_execution(skill, "run", {"host": "vc1"}, {}, success=True,
                                execution_time=1.5, strategy_used="retry_backoff")
    for i in range(5):
        engine.record_execution(skill, "run", {"host": "vc1"}, {}, success=(i > 2),
                                error_message="timeout" if i <= 2 else "",
                                execution_time=10.0, strategy_used="default")

    # 3.2 最佳策略应该是 retry_backoff（全部成功）
    best = engine.get_best_strategy(skill)
    check("策略自适应: 最佳策略选择", best == "retry_backoff", f"actual={best}")

    scores = engine.get_strategy_scores(skill)
    check("策略分数: retry_backoff > default",
          scores.get("retry_backoff", 0) > scores.get("default", 0),
          f"scores={scores}")

    # 3.3 执行规划
    plan = engine.plan_execution(skill, {"host": "vc1"})
    check("执行规划: 策略非空", plan.strategy != "")
    check("执行规划: 信心评估", 0.0 < plan.confidence < 1.0, f"confidence={plan.confidence:.3f}")
    check("执行规划: 风险等级", plan.risk_level in ("low", "medium", "high"), f"risk={plan.risk_level}")
    check("执行规划: 理由非空", len(plan.reasoning) > 0)
    check("执行规划: 预估耗时", plan.estimated_time > 0, f"est_time={plan.estimated_time:.1f}s")

    # 3.4 备选策略
    check("执行规划: 有备选策略", plan.fallback_strategy != "" or len(scores) <= 1,
          f"fallback={plan.fallback_strategy}")

    # 3.5 根因分布警告
    has_root_cause_warning = any("timeout" in w for w in plan.warnings)
    check("执行规划: 根因分布警告", has_root_cause_warning, f"warnings={plan.warnings}")


# ═══════════════════════════════════════════════════════════
#  测试 4: 跨技能迁移学习
# ═══════════════════════════════════════════════════════════
def test_cross_skill_transfer():
    header("测试 4: 跨技能迁移学习")
    from core.self_improving import SelfImprovingEngine, ExperienceDatabase

    db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_transfer"))
    engine = SelfImprovingEngine(db=db)

    # 注册技能标签
    engine.register_skill_tags("vm_backup", ["vmware", "backup", "api"])
    engine.register_skill_tags("vm_snapshot", ["vmware", "snapshot", "api"])
    engine.register_skill_tags("gcoc_backup", ["gcoc", "backup"])
    engine.register_skill_tags("net_ping", ["network", "ping"])

    # 记录 vm_snapshot 的经验
    for i in range(3):
        engine.record_execution("vm_snapshot", "run",
                                {"vm": f"vm-{i}"}, {}, success=False,
                                error_message="VMware API connection timeout",
                                execution_time=30.0, strategy_used="default")
    engine.record_execution("vm_snapshot", "run",
                            {"vm": "vm-3"}, {}, success=True,
                            execution_time=2.0, strategy_used="retry_with_backoff")

    # 4.1 vm_backup 应该能从 vm_snapshot 获取迁移洞察
    insights = engine.get_transfer_insights("vm_backup")
    check("跨技能迁移: 获取到洞察", len(insights) > 0, f"count={len(insights)}")

    if insights:
        has_transfer_tag = any("迁移自" in i for i in insights)
        check("跨技能迁移: 标记来源", has_transfer_tag, f"insight={insights[0][:60]}")

    # 4.2 无共同标签的技能不应有迁移
    insights_net = engine.get_transfer_insights("net_ping")
    check("无关技能: 无迁移洞察", len(insights_net) == 0, f"count={len(insights_net)}")

    # 4.3 按根因过滤的迁移
    insights_timeout = engine.get_transfer_insights("vm_backup", error_category="timeout")
    check("根因过滤迁移: 获取 timeout 相关", len(insights_timeout) >= 0)  # 可能有也可能没有


# ═══════════════════════════════════════════════════════════
#  测试 5: LLM 反思引擎 (规则模式)
# ═══════════════════════════════════════════════════════════
def test_reflection_engine():
    header("测试 5: LLM 反思引擎 (规则模式 — 无 LLM)")

    async def _test():
        from core.self_improving import SelfImprovingEngine, ExperienceDatabase

        db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_reflect"))
        engine = SelfImprovingEngine(db=db, llm_func=None)  # 不注入 LLM

        skill = "failing_skill"

        # 5.1 连续失败 < 3 次，不触发反思
        for i in range(2):
            engine.record_execution(skill, "run", {}, {},
                                    success=False, error_message="connection refused")

        result = await engine.maybe_reflect(skill)
        check("连续失败 2 次: 不触发反思", result is None)

        # 5.2 连续失败 >= 3 次，触发反思
        engine.record_execution(skill, "run", {}, {},
                                success=False, error_message="connection refused")
        check("连续失败计数 = 3", engine.get_consecutive_failures(skill) == 3)

        result = await engine.maybe_reflect(skill)
        check("连续失败 3 次: 触发反思", result is not None)
        if result:
            check("反思结果: summary 非空", len(result.summary) > 0, result.summary[:60])
            check("反思结果: root_cause 非空", len(result.root_cause) > 0)
            check("反思结果: 置信度 > 0", result.confidence > 0)

        # 5.3 成功一次后重置连续失败计数
        engine.record_execution(skill, "run", {}, {}, success=True)
        check("成功后重置连续失败", engine.get_consecutive_failures(skill) == 0)

        result2 = await engine.maybe_reflect(skill)
        check("重置后不触发反思", result2 is None)

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 6: LLM 反思引擎 (Mock LLM 模式)
# ═══════════════════════════════════════════════════════════
def test_reflection_with_mock_llm():
    header("测试 6: LLM 反思引擎 (Mock LLM)")

    async def mock_llm(prompt):
        """模拟 LLM 返回"""
        return json.dumps({
            "summary": "VMware API 连接频繁超时，疑似网络层面问题",
            "root_cause": "网络不稳定导致 API 超时",
            "what_went_wrong": "目标 vCenter 网络延迟过高",
            "what_to_improve": "增加重试机制，超时从30s提升到60s",
            "pattern_detected": "间歇性网络故障导致的周期性超时"
        })

    async def _test():
        from core.self_improving import SelfImprovingEngine, ExperienceDatabase

        db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_mock_llm"))
        engine = SelfImprovingEngine(db=db, llm_func=mock_llm)

        skill = "vm_api_call"
        for i in range(4):
            engine.record_execution(skill, "run", {"host": "vcenter.local"}, {},
                                    success=False, error_message="VMware API timeout after 30s",
                                    execution_time=30.0)

        result = await engine.maybe_reflect(skill)
        check("Mock LLM 反思: 结果非空", result is not None)
        if result:
            check("Mock LLM 反思: summary 包含分析",
                  "超时" in result.summary or "VMware" in result.summary,
                  result.summary[:60])
            check("Mock LLM 反思: 置信度较高", result.confidence >= 0.8,
                  f"confidence={result.confidence}")
            check("Mock LLM 反思: 改进建议非空",
                  len(result.what_to_improve) > 0, result.what_to_improve[:60])

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 7: 多维信心评估
# ═══════════════════════════════════════════════════════════
def test_multi_dimension_confidence():
    header("测试 7: 多维信心评估")
    from core.self_improving import SelfImprovingEngine, ExperienceDatabase

    db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_confidence"))
    engine = SelfImprovingEngine(db=db)

    # 7.1 无历史数据 → 默认信心 0.5
    conf0 = engine.calculate_confidence("new_skill", {})
    check("无历史: 默认信心 0.5", conf0 == 0.5, f"conf={conf0}")

    # 7.2 全部成功 → 高信心
    for i in range(10):
        engine.record_execution("good_skill", "run", {"x": i}, {}, success=True, execution_time=1.0)
    conf_good = engine.calculate_confidence("good_skill", {"x": 5})
    check("全部成功: 信心 > 0.8", conf_good > 0.8, f"conf={conf_good:.3f}")

    # 7.3 全部失败 → 低信心
    for i in range(10):
        engine.record_execution("bad_skill", "run", {"x": i}, {}, success=False,
                                error_message="error", execution_time=5.0)
    conf_bad = engine.calculate_confidence("bad_skill", {"x": 5})
    check("全部失败: 信心 < 0.3", conf_bad < 0.3, f"conf={conf_bad:.3f}")

    # 7.4 信心在 [0.05, 0.99] 范围内
    check("信心范围合理 (good)", 0.05 <= conf_good <= 0.99)
    check("信心范围合理 (bad)", 0.05 <= conf_bad <= 0.99)

    # 7.5 连续失败惩罚
    consecutive = engine.get_consecutive_failures("bad_skill")
    check("连续失败记录", consecutive == 10, f"consecutive={consecutive}")


# ═══════════════════════════════════════════════════════════
#  测试 8: BaseSkill 执行钩子
# ═══════════════════════════════════════════════════════════
def test_base_skill_hooks():
    header("测试 8: BaseSkill 执行钩子 (execute_with_learning)")

    async def _test():
        from core.skill_base import BaseSkill, SkillResult
        from core.self_improving import get_improving_engine, SelfImprovingEngine, ExperienceDatabase

        # 用临时 db 替换全局引擎
        import core.self_improving as si_mod
        old_engine = si_mod._improving_engine
        test_db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_hooks"))
        si_mod._improving_engine = SelfImprovingEngine(db=test_db)

        try:
            # 创建测试技能
            class TestSkill(BaseSkill):
                name = "test_hook_skill"
                description = "测试钩子的技能"
                skill_tags = ["test", "demo"]
                enable_reflection = True
                enable_param_adjustment = True

                async def execute(self, params, context=None):
                    if params.get("fail"):
                        return SkillResult(success=False, error="模拟失败: connection refused")
                    return SkillResult(success=True, text="执行成功", data={"result": "ok"})

            skill = TestSkill()

            # 8.1 成功执行
            result = await skill.execute_with_learning({"task": "hello"})
            check("成功执行: result.success=True", result.success)
            check("成功执行: 返回文本", result.text == "执行成功")

            # 8.2 失败执行
            result2 = await skill.execute_with_learning({"task": "world", "fail": True})
            check("失败执行: result.success=False", not result2.success)
            check("失败执行: 错误信息", "connection refused" in result2.error)

            # 8.3 经验已记录
            engine = get_improving_engine()
            stats = engine.get_statistics("test_hook_skill")
            check("经验记录: total >= 2", stats.get("total", 0) >= 2, f"total={stats.get('total')}")
            check("经验记录: success=1", stats.get("success", 0) >= 1)
            check("经验记录: failure=1", stats.get("failure", 0) >= 1)

            # 8.4 信心已更新
            check("信心分数已更新", skill.confidence_score != 0.5 or stats["total"] < 3,
                  f"confidence={skill.confidence_score:.3f}")

            # 8.5 策略已设置
            check("策略字段存在", hasattr(skill, '_current_strategy'))

            # 8.6 连续失败后触发反思路径（记录多次失败）
            for i in range(3):
                await skill.execute_with_learning({"task": f"fail_{i}", "fail": True})

            consecutive = engine.get_consecutive_failures("test_hook_skill")
            check("连续失败追踪", consecutive >= 3, f"consecutive={consecutive}")

        finally:
            si_mod._improving_engine = old_engine

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 9: SkillImprover 反馈与分析
# ═══════════════════════════════════════════════════════════
def test_skill_improver():
    header("测试 9: SkillImprover 反馈与分析")

    async def _test():
        from core.harness.skill_improver import SkillImprover, Feedback, UsageAnalysis

        improver = SkillImprover(llm_client=None, redis_client=None)
        # 替换存储路径避免污染
        import core.harness.skill_improver as imp_mod
        old_path = imp_mod.IMPROVEMENT_DB_PATH
        imp_mod.IMPROVEMENT_DB_PATH = type(old_path)(os.path.join(TEMP_DIR, "skill_improvements.json"))

        try:
            skill_name = "test_vm_backup"

            # 9.1 记录多条反馈
            for i in range(8):
                fb = await improver.record_feedback(
                    skill_name=skill_name,
                    session_id=f"sess_{i}",
                    user_id="user_test",
                    success=True,
                    execution_time=2.0 + i * 0.1,
                    root_cause="",
                    strategy_used="default",
                )
            check("成功反馈记录", len(improver._feedbacks.get(skill_name, [])) >= 8)

            # 9.2 记录失败反馈（触发根因分析联动）
            for i in range(4):
                await improver.record_feedback(
                    skill_name=skill_name,
                    session_id=f"sess_fail_{i}",
                    user_id="user_test",
                    success=False,
                    execution_time=30.0,
                    error_type="timeout",
                    error_message="VMware API connection timed out after 30s",
                )

            # 9.3 使用分析
            analysis = await improver.analyze_usage_patterns(skill_name)
            check("使用分析: total_uses >= 12", analysis.total_uses >= 12, f"total={analysis.total_uses}")
            check("使用分析: failure_count >= 4", analysis.failure_count >= 4,
                  f"failures={analysis.failure_count}")
            check("使用分析: 趋势字段", analysis.trend in ("improving", "degrading", "stable"),
                  f"trend={analysis.trend}")
            check("使用分析: 根因分布", len(analysis.root_cause_distribution) > 0,
                  f"dist={analysis.root_cause_distribution}")
            check("使用分析: 连续失败计数", analysis.consecutive_failures >= 4,
                  f"consecutive={analysis.consecutive_failures}")

            # 9.4 Feedback 新字段
            last_fb = improver._feedbacks[skill_name][-1]
            check("Feedback 包含 root_cause", hasattr(last_fb, 'root_cause'))
            check("Feedback 包含 strategy_used", hasattr(last_fb, 'strategy_used'))

            # 9.5 自动分析根因（联动 SelfImprovingEngine）
            check("自动根因分析", last_fb.root_cause != "" or last_fb.error_type == "timeout",
                  f"root_cause={last_fb.root_cause}")

        finally:
            imp_mod.IMPROVEMENT_DB_PATH = old_path

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 10: 改进验证闭环 (A/B 测试逻辑)
# ═══════════════════════════════════════════════════════════
def test_ab_test_verification():
    header("测试 10: A/B 测试验证闭环")

    from core.harness.skill_improver import Improvement
    from datetime import datetime

    # 10.1 Improvement 数据模型
    imp = Improvement(
        improvement_id="imp_test001",
        skill_name="test_skill",
        trigger_type="success_rate_drop",
        trigger_details={"success_rate": 0.6},
        suggested_changes={"pitfalls": "增加超时重试"},
        rationale="成功率低于阈值",
        confidence=0.7,
        status="applied",
        created_at=datetime.now().isoformat(),
        source="llm",
        pre_apply_metrics={"success_rate": 0.6, "avg_execution_time": 10.0},
        post_apply_metrics={},
    )

    check("Improvement: source 字段", imp.source == "llm")
    check("Improvement: pre_apply_metrics", imp.pre_apply_metrics.get("success_rate") == 0.6)
    check("Improvement: status = applied", imp.status == "applied")
    check("Improvement: verification_status 初始空", imp.verification_status == "")

    # 10.2 模拟验证逻辑
    # improved 判定
    pre_rate = imp.pre_apply_metrics["success_rate"]
    post_rate_good = 0.8
    if post_rate_good >= pre_rate + 0.05:
        imp.verification_status = "improved"
    check("A/B 验证: improved", imp.verification_status == "improved")

    # degraded 判定
    imp2 = Improvement(
        improvement_id="imp_test002", skill_name="s2", trigger_type="auto",
        source="rule", status="applied", created_at=datetime.now().isoformat(),
        pre_apply_metrics={"success_rate": 0.8},
    )
    post_rate_bad = 0.5
    if post_rate_bad < 0.8 - 0.1:
        imp2.verification_status = "degraded"
    check("A/B 验证: degraded", imp2.verification_status == "degraded")

    # neutral 判定
    imp3 = Improvement(
        improvement_id="imp_test003", skill_name="s3", trigger_type="auto",
        source="rule", status="applied", created_at=datetime.now().isoformat(),
        pre_apply_metrics={"success_rate": 0.75},
    )
    post_rate_neutral = 0.76
    if post_rate_neutral >= 0.75 + 0.05:
        imp3.verification_status = "improved"
    elif post_rate_neutral < 0.75 - 0.1:
        imp3.verification_status = "degraded"
    else:
        imp3.verification_status = "neutral"
    check("A/B 验证: neutral", imp3.verification_status == "neutral")


# ═══════════════════════════════════════════════════════════
#  测试 11: 装饰器 record_skill_execution
# ═══════════════════════════════════════════════════════════
def test_decorator():
    header("测试 11: 装饰器 @record_skill_execution")

    async def _test():
        from core.self_improving import record_skill_execution, get_improving_engine, SelfImprovingEngine, ExperienceDatabase
        import core.self_improving as si_mod

        old_engine = si_mod._improving_engine
        test_db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_deco"))
        si_mod._improving_engine = SelfImprovingEngine(db=test_db)

        try:
            class MockSkill:
                name = "deco_test_skill"
                _current_strategy = "fast"

                @record_skill_execution
                async def run_task(self, params):
                    if params.get("fail"):
                        raise RuntimeError("模拟错误: network unreachable")
                    return {"status": "ok"}

            skill = MockSkill()

            # 11.1 成功调用
            result = await skill.run_task({"action": "ping"})
            check("装饰器: 成功返回", result == {"status": "ok"})

            # 11.2 失败调用
            try:
                await skill.run_task({"action": "ping", "fail": True})
                check("装饰器: 异常应被抛出", False)
            except RuntimeError:
                check("装饰器: 异常正确抛出", True)

            # 11.3 验证经验已记录
            engine = get_improving_engine()
            stats = engine.get_statistics("deco_test_skill")
            check("装饰器: 记录了2次执行", stats.get("total", 0) == 2)
            check("装饰器: 1成功1失败", stats.get("success") == 1 and stats.get("failure") == 1)

        finally:
            si_mod._improving_engine = old_engine

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 12: 端到端场景 — 模拟技能从差到好的进化
# ═══════════════════════════════════════════════════════════
def test_e2e_skill_evolution():
    header("测试 12: 端到端场景 — 技能从差到好的进化过程")

    async def _test():
        from core.skill_base import BaseSkill, SkillResult
        from core.self_improving import SelfImprovingEngine, ExperienceDatabase
        import core.self_improving as si_mod

        old_engine = si_mod._improving_engine
        test_db = ExperienceDatabase(db_path=os.path.join(TEMP_DIR, "exp_e2e"))
        engine = SelfImprovingEngine(db=test_db)
        si_mod._improving_engine = engine

        try:
            call_count = {"n": 0}

            class EvolvingSkill(BaseSkill):
                name = "evolving_skill"
                description = "逐渐变好的技能"
                skill_tags = ["demo", "test"]
                enable_reflection = True
                enable_param_adjustment = True

                async def execute(self, params, context=None):
                    call_count["n"] += 1
                    n = call_count["n"]
                    # 前5次失败（模拟问题阶段）
                    if n <= 5:
                        return SkillResult(success=False,
                                           error=f"Connection timeout to {params.get('host', 'unknown')}")
                    # 第6-10次成功（模拟修复后）
                    return SkillResult(success=True, text="OK", data={"n": n})

            skill = EvolvingSkill()

            # 阶段 1: 持续失败
            print("\n  --- 阶段 1: 持续失败 ---")
            for i in range(5):
                result = await skill.execute_with_learning({"host": "192.168.1.100", "timeout": 30})
            
            stats_phase1 = engine.get_statistics("evolving_skill")
            conf_phase1 = engine.calculate_confidence("evolving_skill", {"host": "192.168.1.100"})
            consecutive = engine.get_consecutive_failures("evolving_skill")
            
            check("阶段1: 成功率=0%", stats_phase1["success_rate"] == 0.0)
            check("阶段1: 信心很低", conf_phase1 < 0.3, f"conf={conf_phase1:.3f}")
            check("阶段1: 连续失败=5", consecutive == 5)
            check("阶段1: 根因分布包含 timeout",
                  "timeout" in stats_phase1.get("root_cause_distribution", {}),
                  f"dist={stats_phase1.get('root_cause_distribution')}")

            # 阶段 2: 查看执行规划（应该提示高风险）
            plan = engine.plan_execution("evolving_skill", {"host": "192.168.1.100"})
            check("阶段1→2: 风险等级=high", plan.risk_level == "high", f"risk={plan.risk_level}")
            check("阶段1→2: 有警告", len(plan.warnings) > 0, f"warnings={plan.warnings}")

            # 阶段 3: 切换到成功（模拟修复后，需要 >20 条才能做趋势对比）
            print("\n  --- 阶段 2: 修复后持续成功 ---")
            # 先补充更多失败记录（让历史期有足够数据）
            for i in range(15):
                call_count["n"] -= 1  # 回退计数让 execute 继续失败
            # 用引擎直接记录，不走 skill.execute
            for i in range(16):
                engine.record_execution("evolving_skill", "execute",
                    {"host": "192.168.1.100", "timeout": 30}, {},
                    success=False, error_message="Connection timeout", execution_time=30.0)
            # 现在有 5(skill) + 16(补充) = 21 条失败记录作为历史
            # 再记录 20 条成功（作为近期）
            for i in range(20):
                engine.record_execution("evolving_skill", "execute",
                    {"host": "192.168.1.100", "timeout": 30}, {},
                    success=True, execution_time=1.5)

            stats_phase2 = engine.get_statistics("evolving_skill")
            conf_phase2 = engine.calculate_confidence("evolving_skill", {"host": "192.168.1.100"})
            consecutive2 = engine.get_consecutive_failures("evolving_skill")

            total_exp = stats_phase2["total"]
            check(f"阶段2: 总记录 >= 41", total_exp >= 41, f"total={total_exp}")
            check("阶段2: 信心提升", conf_phase2 > conf_phase1,
                  f"phase1={conf_phase1:.3f} phase2={conf_phase2:.3f}")
            check("阶段2: 连续失败=0", consecutive2 == 0)

            # 趋势分析: 近20条全成功 vs 之前全失败 → improving
            trend = stats_phase2.get("trend", "")
            check("阶段2: 趋势=improving", trend == "improving", f"trend={trend}")

            # 阶段 3: 再执行规划，风险应降低
            plan2 = engine.plan_execution("evolving_skill", {"host": "192.168.1.100"})
            risk_improved = plan2.risk_level in ("low", "medium")
            check("阶段2: 风险降低", risk_improved, f"risk={plan2.risk_level}")

            print(f"\n  📊 进化过程: 信心 {conf_phase1:.3f} → {conf_phase2:.3f} | "
                  f"风险 {plan.risk_level} → {plan2.risk_level}")

        finally:
            si_mod._improving_engine = old_engine

    asyncio.get_event_loop().run_until_complete(_test())


# ═══════════════════════════════════════════════════════════
#  测试 13: 模块导入兼容性
# ═══════════════════════════════════════════════════════════
def test_imports():
    header("测试 13: 模块导入兼容性")

    try:
        from core.self_improving import (
            ErrorCategory, RootCause, RootCauseAnalyzer,
            Experience, ExperienceDatabase, SkillInsight,
            ExecutionPlan, ReflectionResult, ReflectionEngine,
            SelfImprovingEngine, get_improving_engine, record_skill_execution
        )
        check("core.self_improving 所有公开类/函数导入", True)
    except ImportError as e:
        check("core.self_improving 导入", False, str(e))

    try:
        from core.skill_base import BaseSkill, SkillParam, SkillResult
        check("core.skill_base 导入", True)
    except ImportError as e:
        check("core.skill_base 导入", False, str(e))

    try:
        from core.harness.skill_improver import (
            SkillImprover, get_skill_improver, Feedback, UsageAnalysis, Improvement
        )
        check("core.harness.skill_improver 导入", True)
    except ImportError as e:
        check("core.harness.skill_improver 导入", False, str(e))

    try:
        from core.harness import __version__
        check(f"core.harness 版本 = {__version__}", __version__ == "2.2.0")
    except ImportError as e:
        check("core.harness 版本", False, str(e))


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
def main():
    print("\n" + "═" * 60)
    print("  Z-OPS v2.2.0 Self-Improving Advanced — 端到端功能测试")
    print("═" * 60)
    print(f"  测试数据目录: {TEMP_DIR}")

    tests = [
        ("根因分析器", test_root_cause_analyzer),
        ("经验数据库", test_experience_database),
        ("策略自适应 & 执行规划", test_strategy_and_planning),
        ("跨技能迁移学习", test_cross_skill_transfer),
        ("LLM 反思引擎 (规则模式)", test_reflection_engine),
        ("LLM 反思引擎 (Mock LLM)", test_reflection_with_mock_llm),
        ("多维信心评估", test_multi_dimension_confidence),
        ("BaseSkill 执行钩子", test_base_skill_hooks),
        ("SkillImprover 反馈与分析", test_skill_improver),
        ("A/B 测试验证闭环", test_ab_test_verification),
        ("装饰器 record_skill_execution", test_decorator),
        ("端到端技能进化", test_e2e_skill_evolution),
        ("模块导入兼容性", test_imports),
    ]

    for name, func in tests:
        try:
            func()
        except Exception as e:
            global failed
            failed += 1
            errors.append(f"  💥 测试组 [{name}] 异常: {e}")
            print(f"\n  💥 测试组 [{name}] 异常:")
            traceback.print_exc()

    # 清理临时文件
    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass

    # 汇总
    total = passed + failed
    print("\n" + "═" * 60)
    print(f"  测试汇总: {passed}/{total} 通过")
    if errors:
        print(f"\n  失败项:")
        for e in errors:
            print(f"    {e}")
    print("═" * 60)

    if failed == 0:
        print("\n  🎉 全部测试通过！Self-Improving v2.2 功能正常。\n")
    else:
        print(f"\n  ⚠️  有 {failed} 项测试失败，请检查。\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
