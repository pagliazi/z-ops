#!/usr/bin/env python3
"""Z-OPS v4 权限治理二期/三期 全量验证 (服务已加载新代码)
覆盖: meta 接口 / approvers 名单强约束 / SoD / 角色模板 / TTL 临时授权 / 权限度量 / VM scope 裁剪 / 核心回归
"""
import asyncio, hashlib, json, os, sys, time
import httpx

sys.path.insert(0, "/opt/zops")
os.chdir("/opt/zops")
import config

BASE = "http://127.0.0.1:7789"
PASS_HASH = hashlib.sha256(f"zops_salt_vpass_{config.JWT_SECRET}".encode()).hexdigest()
ADM, APPA, APPB, AP = "tmp_adm_v4", "tmp_appA_v4", "tmp_appB_v4", "tmp_ap_v4"

results = []
def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  [' + str(extra) + ']' if extra and not ok else ''}")

async def redis():
    import redis.asyncio as aioredis
    return aioredis.from_url(config.REDIS_URL, decode_responses=True)

def login_sync(client, username):
    r = client.post(f"{BASE}/api/auth/login", json={"username": username, "password": "vpass"})
    assert r.status_code == 200, f"login {username}: {r.status_code} {r.text[:200]}"
    return r.json()["token"]

async def main():
    r = await redis()
    # ═══ 准备: 临时账号 (redis 直写 admin, API 授权普通用户) ═══
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for u, dn in [(ADM, "v4验证管理员"), (APPA, "v4审批人A"), (APPB, "v4审批人B"), (AP, "v4申请人")]:
        role = "admin" if u == ADM else "user"
        await r.hset(config.KEY_USERS, u, json.dumps({"password_hash": PASS_HASH, "display_name": dn, "role": role, "group": "GCOC", "created_at": now}, ensure_ascii=False))
    # 度量原值备份 (测试后恢复, 不污染生产累计值)
    metrics_before = await r.hgetall(config.KEY_PERM_METRICS)

    c = httpx.Client(timeout=15)
    t_adm, t_appa, t_appb, t_ap = (login_sync(c, x) for x in (ADM, APPA, APPB, AP))
    H = lambda t: {"Authorization": f"Bearer {t}"}
    HJ = lambda t: {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}

    # 授权审批人 A/B
    for u in (APPA, APPB):
        resp = c.put(f"{BASE}/api/auth/users/{u}", headers=HJ(t_adm), json={"biz_perms": ["approval.decide"]})
        assert resp.status_code == 200, f"grant {u}: {resp.status_code} {resp.text[:150]}"

    print("== A. approvals meta (下拉数据源) ==")
    meta = c.get(f"{BASE}/api/approvals/meta", headers=H(t_ap))
    m = meta.json() if meta.status_code == 200 else {}
    check("meta HTTP 200", meta.status_code == 200, meta.text[:120])
    check("meta.apply_types 白名单 8 项", len(m.get("apply_types") or []) == 8, m.get("apply_types"))
    check("meta.service_types 非空", len(m.get("service_types") or []) > 0, m.get("service_types"))
    check("meta.sites 含预置站点东莞", "东莞" in (m.get("sites") or []), m.get("sites"))
    appr = m.get("approvers") or []
    check("meta.approvers 含持权审批人 A/B 且排除请求者(SoD)",
          {APPA, APPB} <= {a["username"] for a in appr} and AP not in {a["username"] for a in appr},
          [a.get("username") for a in appr])

    print("== B. approvers 名单强约束 + SoD ==")
    cr = c.post(f"{BASE}/api/approvals", headers=HJ(t_ap), json={
        "tool_name": "vm_power_off", "summary": "v4验证: 名单约束测试单",
        "params": {"vm": "TEST-VM-01"}, "site": "东莞", "service_type": "云主机",
        "case_no": "CS-v4", "customer": "V4Test", "approvers": f"{APPA},v4审批人A"})
    aid = cr.json().get("approval_id") if cr.status_code == 200 else None
    check("创建指定审批人单据", aid is not None, cr.text[:150])
    r1 = c.post(f"{BASE}/api/approvals/{aid}/decide", headers=HJ(t_ap), json={"decision": "approve"})
    check("SoD: 申请人不能批自己的单 (403)", r1.status_code == 403, r1.text[:150])
    r2 = c.post(f"{BASE}/api/approvals/{aid}/decide", headers=HJ(t_appb), json={"decision": "approve"})
    check("名单外审批人 B 被拦 (403)", r2.status_code == 403 and "指定审批人" in r2.text, r2.text[:150])
    r3 = c.post(f"{BASE}/api/approvals/{aid}/decide", headers=HJ(t_appa), json={"decision": "approve"})
    check("名单内审批人 A (username 匹配) 可批 (200)", r3.status_code == 200, r3.text[:150])

    # display_name 匹配路径: 新单指定 display_name
    cr2 = c.post(f"{BASE}/api/approvals", headers=HJ(t_ap), json={
        "tool_name": "vm_power_off", "summary": "v4验证: display_name 匹配",
        "params": {}, "site": "东莞", "case_no": "CS-v4b", "approvers": "v4审批人B"})
    aid2 = cr2.json().get("approval_id")
    r4 = c.post(f"{BASE}/api/approvals/{aid2}/decide", headers=HJ(t_appb), json={"decision": "reject"})
    check("名单内审批人 B (display_name 匹配) 可批 (200)", r4.status_code == 200, r4.text[:150])

    print("== C. 业务角色模板 ==")
    cat = c.get(f"{BASE}/api/auth/biz-perms", headers=H(t_adm))
    roles = cat.json().get("roles") if cat.status_code == 200 else None
    need_roles = {"值班工程师", "变更经理", "自动化管理员", "知识运营编辑", "全业务管理员"}
    check("biz-perms 下发 roles 5 模板", isinstance(roles, dict) and need_roles <= set(roles.keys()), list((roles or {}).keys()))
    full = (roles or {}).get("全业务管理员") or []
    check("全业务管理员 = 全部权限点", len(full) == len(cat.json().get("perms", [])), f"{len(full)} vs {len(cat.json().get('perms', []))}")

    print("== D. TTL 临时授权 ==")
    from core.bizperms import effective_perms
    fut = int(time.time()) + 3600
    resp = c.put(f"{BASE}/api/auth/users/{AP}", headers=HJ(t_adm),
                 json={"biz_perms": ["observability.view"], "biz_perms_ttl": {"observability.view": fut}})
    check("PUT biz_perms_ttl 落盘 (200)", resp.status_code == 200, resp.text[:150])
    eff = await effective_perms(AP)
    check("TTL 未过期 → effective 生效 + active_ttl", "observability.view" in eff["effective"] and "observability.view" in (eff.get("active_ttl") or {}), eff)
    ttl_loaded = await r.hget(config.KEY_USERS, AP)
    ttl_loaded = json.loads(ttl_loaded).get("biz_perms_ttl") or {}
    check("redis 落盘 expire_ts", abs(int(ttl_loaded.get("observability.view", 0)) - fut) < 5, ttl_loaded)
    past = int(time.time()) - 100
    c.put(f"{BASE}/api/auth/users/{AP}", headers=HJ(t_adm), json={"biz_perms_ttl": {"observability.view": past}})
    eff2 = await effective_perms(AP)
    check("TTL 过期 → effective 剔除 + expired 标记", "observability.view" not in eff2["effective"] and "observability.view" in eff2.get("expired", []), eff2)

    print("== E. 权限使用度量 ==")
    metrics_before = await r.hgetall(config.KEY_PERM_METRICS)
    d_before = int(metrics_before.get("denied:approval.decide") or 0)
    u_before = int(metrics_before.get("used:approval.decide") or 0)
    # denied: 无权限申请人 decide (新建一张无名单单)
    cr3 = c.post(f"{BASE}/api/approvals", headers=HJ(t_ap), json={
        "tool_name": "vm_snapshot", "summary": "v4验证: denied 计数", "params": {},
        "site": "香港", "case_no": "CS-v4c"})
    aid3 = cr3.json().get("approval_id")
    c.post(f"{BASE}/api/approvals/{aid3}/decide", headers=HJ(t_ap), json={"decision": "approve"})  # → 403 权限门
    # used: 审批人 A 决策
    c.post(f"{BASE}/api/approvals/{aid3}/decide", headers=HJ(t_appa), json={"decision": "approve"})
    await asyncio.sleep(0.3)
    d_after = int(await r.hget(config.KEY_PERM_METRICS, "denied:approval.decide") or 0)
    u_after = int(await r.hget(config.KEY_PERM_METRICS, "used:approval.decide") or 0)
    check("denied 计数: 无权限 decide → +1", d_after == d_before + 1, f"{d_before}→{d_after}")
    check("used 计数: 持权 decide → +1", u_after == u_before + 1, f"{u_before}→{u_after}")
    pm = c.get(f"{BASE}/api/admin/perm-metrics", headers=H(t_adm))
    rows = pm.json().get("metrics") if pm.status_code == 200 else []
    row = next((x for x in rows if x["key"] == "approval.decide"), {})
    check("perm-metrics 报表 (holders/used/denied/advice)",
          pm.status_code == 200 and row.get("used", 0) >= 1 and row.get("denied", 0) >= 1 and "holders" in row, row)
    check("perm-metrics 非管理员 403", c.get(f"{BASE}/api/admin/perm-metrics", headers=H(t_ap)).status_code == 403)

    print("== F. VM 查询 scope 裁剪 (单元级) ==")
    from agents.vm_agent import VMAgent
    agent = VMAgent.__new__(VMAgent)
    raw = json.dumps({"results": [
        {"name": "HKG-NIDEC-APP01", "vcenter_server": "vc-hkg01", "power": "on"},
        {"name": "YUD-FLEX-WEB02", "vcenter_server": "vc-yud01", "power": "off"},
        {"name": "SZXGC-TEST-DB1", "vcenter_server": "vc-szx01", "power": "on"},
    ], "count": 3})
    await r.hset(config.KEY_USERS, AP, json.dumps({"password_hash": PASS_HASH, "display_name": "v4申请人", "role": "user", "group": "GCOC",
        "created_at": now, "scopes": {"sites": ["香港"], "customers": []}}, ensure_ascii=False))
    # 返回体是 "JSON 主体 + 附注行" 的展示文本, 按文本断言
    out = await agent._filter_vms_by_scope(raw, AP)
    check("scope=香港 → 仅保留 HKG 记录", "HKG" in out and "YUD" not in out and "SZXGC" not in out, out[:200])
    check("裁剪结果附范围提示", "范围约束" in out)
    out2 = json.loads(await agent._filter_vms_by_scope(raw, "nonexist_user_zzz"))  # 无提示时是纯 JSON
    check("无 scope → fail-open 全保留", len(out2["results"]) == 3, out2.get("results"))

    print("== G. 核心回归 ==")
    ok_login = c.post(f"{BASE}/api/auth/login", json={"username": AP, "password": "vpass"}).status_code == 200
    check("R1 登录", ok_login)
    me = c.get(f"{BASE}/api/auth/me", headers=H(t_ap))
    check("R2 /api/auth/me", me.status_code == 200 and me.json().get("username") == AP, me.text[:120])
    ul = c.get(f"{BASE}/api/auth/users", headers=H(t_adm))
    check("R3 用户列表 (含 scopes/TTL 下发)", ul.status_code == 200 and "biz_perms_ttl" in json.dumps(ul.json().get("users", [])[:50]), ul.text[:120])
    gp = c.get(f"{BASE}/api/admin/group-permissions", headers=H(t_adm))
    check("R4 组权限配置", gp.status_code == 200 and "groups" in gp.json(), gp.text[:120])
    check("R5 审批列表", c.get(f"{BASE}/api/approvals", headers=H(t_appa)).status_code == 200)
    check("R6 我的申请", c.get(f"{BASE}/api/approvals/mine", headers=H(t_ap)).status_code == 200)

    # ═══ 清理: 恢复度量原值 / 删临时账号 / 删测试审批单 (审计日志按红线保留) ═══
    for k in ("used:approval.decide", "denied:approval.decide"):
        old = metrics_before.get(k)
        if old is None:
            await r.hdel(config.KEY_PERM_METRICS, k)
        else:
            await r.hset(config.KEY_PERM_METRICS, k, old)
    for a in (aid, aid2, aid3):
        await r.delete(f"{config.REDIS_PREFIX}:approval:{a}")
    await r.hdel(config.KEY_USERS, ADM, APPA, APPB, AP)
    print(f"\n清理完成: 4 临时账号已删, {aid}/{aid2}/{aid3} 测试单已删, 度量值已恢复")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*50}\n结果: {passed}/{len(results)} PASS")
    for name, ok, extra in results:
        if not ok:
            print(f"  FAIL 详情: {name} → {extra}")
    return 0 if passed == len(results) else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
