"""Z-OPS P1 验收测试 — 上线门诊断 (diagnostic readiness, SPEC 部署流程最后一项)

覆盖:
  1. 诊断结构完整 (Redis 前置/队列/租约/归档水位与 lag/事件补读/Outbox/服务主体);
  2. fail-closed: 任一异常进 blockers, ready 与 blockers 严格一致;
  3. 真实缺陷可检出: 投递 uncertain / 归档缺失 / 事件滞后 / 租约过期未回收;
  4. 凭据零外泄: 诊断体只报"是否配置", 不含任何密钥值;
  5. 端点需认证 (无 Authorization → 拒绝, 不暴露内部计数)。

隔离: Redis 6399 + 临时归档目录; 无真实外部系统。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

RUN = os.environ.get("ZOPS_TEST_RUN") or str(int(time.time() * 1000))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


async def seed_task(rt, key, status="succeeded", lease_expires=None, archive=True):
    tid, created, fenc, own = await rt.submit_for_chat(
        "diaguser", f"sess:{RUN}", f"{RUN}-{key}", "诊断样本", "diag", channel="webchat")
    if status == "succeeded":
        await rt.complete(tid, own, fenc, "succeeded", {"text": "ok"})
        if not archive:   # 模拟归档丢失 (Redis 有事实, JSONL 无)
            p = Path(os.environ["ZOPS_DIAG_ARCHIVE"]) / f"{tid}.jsonl"
            p.unlink(missing_ok=True)
    elif status == "running":
        r = await rt.ledger._redis()
        await r.hset(f"{config.REDIS_PREFIX}:tr:task:{tid}", "status", "running")
        if lease_expires is not None:
            await r.hset(f"{config.REDIS_PREFIX}:tr:task:{tid}", "lease_expires_ts", str(lease_expires))
    return tid


async def case_structure(rt, dg, r):
    rep = await dg.collect(r)
    for sec in ("redis", "queue", "outbox", "tasks", "archive", "authz", "feature_flags"):
        check(f"诊断: 分节完整 ({sec})", sec in rep, str(list(rep)))
    check("诊断: ready 与 blockers 严格一致 (fail-closed)",
          rep["ready"] is (not rep["blockers"]), f"{rep['ready']}/{rep['blockers']}")
    check("诊断: Redis ping 不单独作为验收 (核查版本/持久化/淘汰策略/拓扑/容量)",
          bool(rep["redis"].get("version")) and "blockers" in rep["redis"]
          and any("noeviction" in n or "AOF" in n or "appendfsync" in n
                  for n in (rep["redis"].get("notes", []) + rep["redis"].get("blockers", []))),
          str(rep["redis"])[:200])
    check("诊断: 入口开关可观测 (streams/飞书/n8n)",
          set(rep["feature_flags"]) == {"streams_consumer", "feishu_entry", "n8n_entry"})
    check("诊断: 任务分布按 channel 归因", isinstance(rep["tasks"].get("by_channel"), dict))


async def case_detect_defects(rt, dg, r):
    # 1) 投递 uncertain
    await seed_task(rt, "unc", status="succeeded")
    pop = await rt.ledger.pop_delivery()
    if pop:
        await rt.ledger.mark_delivery(pop["task_id"], pop["delivery_id"], "uncertain")
    rep = await dg.collect(r)
    check("检出: 投递 uncertain 进 blockers (不虚报已交付)",
          any("uncertain" in b for b in rep["blockers"]), str(rep["blockers"])[:200])
    check("检出: Outbox 状态计数可见", "uncertain" in (rep["outbox"].get("delivery_seen") or {}),
          str(rep["outbox"]))

    # 2) 租约过期未回收
    await seed_task(rt, "stale", status="running", lease_expires=time.time() - 3600)
    rep = await dg.collect(r)
    check("检出: 租约过期任务进 blockers (reclaim 停摆可见)",
          any("租约过期" in b for b in rep["blockers"]), str(rep["blockers"])[:200])

    # 3) 终态任务归档缺失 (永久事实只在 Redis)
    await seed_task(rt, "noarch", status="succeeded", archive=False)
    rep = await dg.collect(r)
    check("检出: 归档缺失/事件滞后进 blockers (上线门不允许盲区)",
          any("归档" in b for b in rep["blockers"]), str(rep["blockers"])[:200])
    check("检出: 归档水位含目录与抽样数",
          rep["archive"].get("dir") and rep["archive"].get("sampled", 0) >= 1, str(rep["archive"]))


async def case_no_credential_leak(dg, r):
    secret = "SECRET-NEVER-LEAK-" + RUN
    orig = (getattr(config, "N8N_WEBHOOK_KEY", ""), getattr(config, "N8N_CALLBACK_SECRET", ""))
    try:
        config.N8N_WEBHOOK_KEY = secret
        config.N8N_CALLBACK_SECRET = secret + "-cb"
        rep = await dg.collect(r)
        blob = json.dumps(rep, ensure_ascii=False)
        check("凭据: 诊断体不含密钥值 (仅布尔是否配置)",
              secret not in blob and rep["authz"]["n8n_webhook_key_configured"] is True, blob[:200])
        check("凭据: 诊断自证不返回值 (契约声明)",
              rep["authz"].get("credential_values_returned") is False)
    finally:
        config.N8N_WEBHOOK_KEY, config.N8N_CALLBACK_SECRET = orig


async def case_reverse_blindspot(dg, r, tmp):
    """归档有文件、账本无投影: 未标明 = 审计盲区 (blocker); 已标明 = 仅备注"""
    p = Path(tmp) / "tasks" / "tsk_stray_no_project.jsonl"
    p.write_text(json.dumps({"event_id": "evt_stray_1", "seq": 1, "type": "task_started",
                             "ts": time.time(), "data": {}}) + "\n", encoding="utf-8")
    rep = await dg.collect(r)
    check("反向: 无投影且未标明的归档 = 盲区 blocker",
          any("无账本投影且未标明" in b for b in rep["blockers"]), str(rep["blockers"])[:200])
    check("反向: archive_reverse 计数可见",
          (rep.get("archive_reverse") or {}).get("unexplained", 0) >= 1, str(rep.get("archive_reverse")))
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": "evt_missing_x", "seq": -1, "type": "archive_gap",
                            "ts": 0, "data": {"note": "[test_artifact] 已标明来源"}}) + "\n")
    rep2 = await dg.collect(r)
    check("反向: 标明后盲区解除 (不删历史也能可解释)",
          not any("无账本投影且未标明" in b for b in rep2["blockers"]), str(rep2["blockers"])[:200])
    check("反向: 已标明孤儿转为备注可追溯",
          any("已标明来源" in n for n in rep2["notes"]), str(rep2["notes"])[:200])


async def case_endpoint_auth():
    import webchat_api  # noqa: F401  (按生产顺序先导 app)
    from fastapi.testclient import TestClient
    client = TestClient(webchat_api.app)
    resp = client.get("/api/observability/task-runtime-readiness")
    check("端点: 未认证拒绝 (不暴露内部计数)", resp.status_code in (401, 403), str(resp.status_code))
    resp2 = client.get("/api/observability/task-runtime-readiness",
                       headers={"Authorization": "Bearer bogus-" + RUN})
    check("端点: 伪造令牌拒绝", resp2.status_code in (401, 403), str(resp2.status_code))


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_p1_diag_")
    os.environ["ZOPS_DIAG_ARCHIVE"] = str(Path(tmp) / "tasks")
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    from core.task_runtime import diagnostics as dg
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks"
    rt_mod._runtime = rt_mod.TaskRuntime(lease_s=60)
    rt = rt_mod._runtime
    r = await rt.ledger._redis()
    await r.flushdb()
    try:
        await case_structure(rt, dg, r)
        await case_detect_defects(rt, dg, r)
        await case_no_credential_leak(dg, r)
        await case_reverse_blindspot(dg, r, tmp)
        await case_endpoint_auth()
    finally:
        await r.aclose()
        rt_mod._runtime = None
        shutil.rmtree(tmp, ignore_errors=True)
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(main())
