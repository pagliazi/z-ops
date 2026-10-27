"""Z-OPS P2 验收测试 — 技能生命周期 / 成功率口径 / 复盘隔离 / 外部参考标注

SPEC P2 验收: 技能从候选、回归、审核、调用到版本回退的完整路径;
验证越级发布、同版本覆盖、撤权失效、错误成功率和复盘越权。
隔离: 临时目录; 不触生产技能文件/生产 wiki。
"""
import asyncio
import os
import threading
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def _run(coro):
    """在独立线程事件循环中执行协程 (测试主循环内调用 asyncio.run 会报错)"""
    box = {}
    def _t():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:   # 线程内异常回传主线程
            box["e"] = e
    t = threading.Thread(target=_t)
    t.start(); t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


def test_lifecycle_full_path(tmp):
    from core.harness import skill_lifecycle as sl

    lc = sl.SkillLifecycle(
        base=Path(tmp) / "lc", live_dir=Path(tmp) / "live", aux_budget_daily=3)
    admin_yes = lambda: True
    admin_no = lambda: False

    skill = {"name": "vm_backup_v2", "description": "备份核对 v2",
             "version": "1.0.0", "procedure": "step1", "author": "retro"}

    # ── 复盘越权: 自动发布禁止 / 直写线上拒绝 ──
    try:
        lc.submit_candidate(dict(skill), author="retro", allow_auto_publish=True)
        check("复盘越权: allow_auto_publish 拒绝", False)
    except sl.LifecycleError as e:
        check("复盘越权: allow_auto_publish 拒绝", e.code == "auto_publish_forbidden")

    from core.harness.skill_registry import SkillRegistry
    reg = SkillRegistry()
    reg.BASE_DIR = lc.live_dir          # 测试隔离: 不写生产目录
    try:
        _run(reg.save_skill(dict(skill)))
        check("复盘越权: 直接 save_skill 写线上被拒", False)
    except PermissionError:
        check("复盘越权: 直接 save_skill 写线上被拒", True)

    # ── 完整路径: candidate → evaluated → approved → published → 调用 → 回退 ──
    v1 = lc.submit_candidate(dict(skill), author="retro",
                             source_execution="exec-001",
                             evidence=["failure:f1:timeout"])
    check("路径: 候选提交", v1 == "1.0.0")

    # 越级: candidate 直接 publish / approve 拒绝
    try:
        lc.publish("vm_backup_v2", v1, "admin", is_admin=admin_yes)
        check("越级: candidate→publish 拒绝", False)
    except sl.LifecycleError as e:
        check("越级: candidate→publish 拒绝", "illegal_transition" in e.code)
    try:
        lc.approve("vm_backup_v2", v1, "admin", is_admin=admin_yes)
        check("越级: candidate→approve 拒绝", False)
    except sl.LifecycleError as e:
        check("越级: candidate→approve 拒绝", "illegal_transition" in e.code)

    # 非管理员 approve 拒绝
    lc.evaluate("vm_backup_v2", v1, passed=True, tests=["t1 冒烟"])
    try:
        lc.approve("vm_backup_v2", v1, "mallory", is_admin=admin_no)
        check("越权: 非管理员 approve 拒绝", False)
    except sl.LifecycleError as e:
        check("越权: 非管理员 approve 拒绝", e.code == "not_authorized")

    lc.approve("vm_backup_v2", v1, "admin", is_admin=admin_yes)
    lc.publish("vm_backup_v2", v1, "admin", is_admin=admin_yes)
    pub = lc.get_published("vm_backup_v2")
    check("路径: 发布内容可读 (published 指针)", pub and pub["procedure"] == "step1")
    check("路径: 线上文件已写入", (lc.live_dir / "vm_backup_v2.yaml").exists())

    # ── 同版本不同内容: 不可覆盖, 自动新版本 ──
    skill_changed = dict(skill)
    skill_changed["procedure"] = "step1-改"
    v2 = lc.submit_candidate(skill_changed, author="retro")
    check("不可变版本: 同版本号不同内容 → 自动新版本",
          v2 != v1 and v2.startswith("1.0."), v2)
    v2b = lc.submit_candidate(dict(skill_changed), author="retro")
    check("不可变版本: 同内容重复提交幂等", v2b == v2)

    # ── 回退: v2 发布后指针回移 v1 (内容不可变) ──
    lc.evaluate("vm_backup_v2", v2, passed=True, tests=["t2"])
    lc.approve("vm_backup_v2", v2, "admin", is_admin=admin_yes)
    lc.publish("vm_backup_v2", v2, "admin", is_admin=admin_yes)
    check("回退: 新版本发布后指针=v2", lc._skill("vm_backup_v2")["published"] == v2)
    lc.rollback("vm_backup_v2", v1, "admin", is_admin=admin_yes)
    check("回退: 指针回移 v1 (发布指针机制)",
          lc._skill("vm_backup_v2")["published"] == v1
          and lc.get_published("vm_backup_v2")["procedure"] == "step1")
    # 未批准版本不可回退
    skill_v3 = dict(skill); skill_v3["procedure"] = "step3"
    v3 = lc.submit_candidate(skill_v3, author="retro")
    try:
        lc.rollback("vm_backup_v2", v3, "admin", is_admin=admin_yes)
        check("回退: 未批准版本为目标时拒绝", False)
    except sl.LifecycleError as e:
        check("回退: 未批准版本为目标时拒绝", "not_approved" in e.code)

    # ── 成功率口径: 未验证 null; 只统计 verified 证据 ──
    check("成功率: 未验证为 null (不默认成功)", lc.success_rate("vm_backup_v2") is None)
    lc.record_outcome("vm_backup_v2", success=False, verified=False)
    check("成功率: 未验证结果不计入 (仍 null)", lc.success_rate("vm_backup_v2") is None)
    lc.record_outcome("vm_backup_v2", success=True, verified=True)
    lc.record_outcome("vm_backup_v2", success=False, verified=True)
    check("成功率: 仅可核验证据计算 (1/2)", lc.success_rate("vm_backup_v2") == 0.5,
          str(lc.success_rate("vm_backup_v2")))

    # ── 复盘预算隔离 ──
    check("复盘预算: 初始有余额", lc.aux_budget_left() == 3)
    lc.aux_budget_consume(3)
    check("复盘预算: 耗尽返回 False (暂停复盘)", lc.aux_budget_consume(1) is False
          and lc.aux_budget_left() == 0)

    # ── 存量收编 ──
    live2 = Path(tmp) / "live2"
    live2.mkdir()
    import yaml
    (live2 / "legacy_skill.yaml").write_text(yaml.dump(
        {"name": "legacy_skill", "version": "2.1.0", "procedure": "old"}), encoding="utf-8")
    lc2 = sl.SkillLifecycle(base=Path(tmp) / "lc2", live_dir=live2)
    n = sl._bootstrap_existing(lc2)
    check("存量收编: 线上技能注册为 published", n == 1
          and lc2.get_published("legacy_skill")["procedure"] == "old"
          and lc2._skill("legacy_skill")["published"] == "2.1.0")
    try:
        _run(SkillRegistry().save_skill({"name": "legacy_skill"}))
        check("存量收编: 门控仍拦截直写", False)
    except PermissionError:
        check("存量收编: 门控仍拦截直写", True)


def test_reference_origin(tmp):
    """P2: 外部参考标注 — origin=reference 的页面检索带标记; 内部页不带"""
    from core.memory.wiki_store import WikiStore
    from core.memory.wiki_query import search_wiki
    from agents.base import _build_wiki_context

    store = WikiStore(root=Path(tmp) / "wiki2")
    store.get_or_create("concepts", "ext-1",
                        defaults_fm={"confidence": 0.9, "origin": "reference",
                                     "tags": ["prior_learning_reference"]},
                        default_body="# 外部经验\n备份巡检先查 Veeam 作业状态"
                        ).save()
    store.get_or_create("concepts", "int-1",
                        defaults_fm={"confidence": 0.9},
                        default_body="# 内部纠错\n备份巡检先查 Veeam 作业状态"
                        ).save()
    hits = search_wiki("备份巡检 Veeam", store=store)
    origins = {h.page_id: h.origin for h in hits}
    check("外部参考: reference 页命中带 origin=reference", origins.get("ext-1") == "reference")
    check("外部参考: 内部页 origin=internal (缺省)", origins.get("int-1") in ("internal", ""))
    ctx = _build_wiki_context("备份巡检 Veeam", store=None) if False else None
    # prompt 标注验证 (直接用检索结果模拟 build)
    marked = any("【外部参考·未验证】" in (getattr(h, "title", "") or "") or True
                 for h in hits)  # 标题标记在 _build_wiki_context 中加; 此处验证数据链路
    from agents.base import _build_wiki_context as _bwc
    # 临时替换 store 单例为测试 store
    import core.memory.wiki_query as wq
    orig_get = wq.get_wiki_store
    wq.get_wiki_store = lambda: store
    try:
        ctx2 = _bwc("备份巡检 Veeam")
        check("外部参考: prompt 上下文含【外部参考·未验证】标注",
              "【外部参考·未验证】" in ctx2, ctx2[:80] if ctx2 else "")
    finally:
        wq.get_wiki_store = orig_get


def test_revoke_immediate(tmp):
    """P2: 撤权立即失效 — 页面改 private 后立刻检索不到 (无索引/缓存残留)"""
    from core.memory.wiki_store import WikiStore
    from core.memory.wiki_query import search_wiki

    store = WikiStore(root=Path(tmp) / "wiki3")
    p = store.get_or_create("concepts", "rev-1",
                            defaults_fm={"visibility": "group",
                                         "audience": "feishu:group:g1"},
                            default_body="# 群知识\n值班电话 12345")
    store.save(p)
    hits = search_wiki("值班电话", store=store, actor="alice",
                       audience="feishu:group:g1")
    check("撤权前: 同群可检索", any(h.page_id == "rev-1" for h in hits))
    p.frontmatter["visibility"] = "private"
    p.frontmatter["owner"] = "bob"
    store.save(p)
    hits2 = search_wiki("值班电话", store=store, actor="alice",
                        audience="feishu:group:g1")
    check("撤权后: 立即不可检索 (查询时过滤, 无缓存残留)",
          not any(h.page_id == "rev-1" for h in hits2))


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_p2_")
    try:
        test_lifecycle_full_path(tmp)
        test_reference_origin(tmp)
        test_revoke_immediate(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
