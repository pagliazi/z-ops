"""Z-OPS 业务权限体系 (v3: 域×动作级 + 范围约束)

模型四层:
  L1 原子权限点  perm = 域.动作级  (如 approval.decide / vmops.operate / observability.detail)
  L2 业务角色    = L1 的命名集合 (预置打包, 非独立机制)
  L3 授权主体    个人 biz_perms ∪ 组 biz_perms (组级继承)
  L4 范围约束    scope (site/customer; 空=不限; 审批决策与申请侧校验)

横切机制:
  - 旧 key 别名归一 (BIZ_PERM_ALIASES): 存量授权零迁移, 读取时自动映射到新命名空间
  - SoD 四眼原则 (decide 端点校验, 见 api/routers/admin.py)
  - write 级动作权限门 (WRITE_PERM_MAP, 见 core/approval.py gate_action_message)
  - 权限变更双通道审计 (user-ops + daily-log, 见 api/routers/admin.py)

本模块自包含 (不依赖 webchat_api), 任何 router/core 模块均可安全 import。
"""
import json
import time

import redis.asyncio as aioredis
from fastapi import HTTPException

import config

# ═══ L1: 权限点目录 (域.动作级) ═══
# 扩展方式: 新增业务域/动作级时在此追加条目并挂门; 授权界面经 /api/auth/biz-perms 自动渲染。
BIZ_PERMS: dict = {
    # ── 变更治理 ──
    "approval.decide":      {"label": "审批决策",       "desc": "批准/拒绝审批单 (四眼原则: 不可审批自己发起的单)", "group": "变更治理"},
    "approval.ds_review":   {"label": "DS 配置复核",    "desc": "分级审批流第一级: DS 配置复核级决策 (需同时在单据 DS 名单内)", "group": "变更治理"},
    "vmops.operate":        {"label": "VM 变更操作",    "desc": "VM 快照创建等 write 级操作 (对话触发时校验)", "group": "变更治理"},
    # ── 自动化 ──
    "n8n.operate":          {"label": "工作流触发",     "desc": "触发 n8n 工作流 (巡检/备份检查等自动化任务)", "group": "自动化"},
    "n8n.manage":           {"label": "工作流管理",     "desc": "启用/停用 n8n 工作流定义", "group": "自动化"},
    "notify.manage":        {"label": "通知渠道管理",   "desc": "管理通知群组、通讯录同步与通知渠道配置", "group": "自动化"},
    "devops.operate":       {"label": "脚本与配置执行", "desc": "script_exec / ansible_run 等 write 级执行", "group": "自动化"},
    # ── 知识运营 ──
    "knowledge.manage":     {"label": "LLM-Wiki 运营",  "desc": "LLM-Wiki 页面检索、统计与索引维护", "group": "知识运营"},
    # ── 可观测 ──
    "observability.view":   {"label": "业务监控查看",   "desc": "监控大盘聚合统计", "group": "可观测"},
    "observability.detail": {"label": "调用明细查看",   "desc": "执行明细/调用链/用户级用量 (含对话内容, 敏感)", "group": "可观测"},
    # 预留扩展点 (业务域就绪后移入上方即生效):
    # "kb.manage":          {"label": "知识库运营",     "desc": "知识库文档管理与索引重建", "group": "知识运营"},
    # "nms.ack":            {"label": "告警确认处理",   "desc": "NMS 告警 ACK 与处理动作", "group": "可观测"},
}

# 旧域级 key → 新命名空间 (v3 兼容层; 存量授权零迁移)
BIZ_PERM_ALIASES: dict = {
    "approval": "approval.decide",
    "n8n": "n8n.operate",
    "notify": "notify.manage",
    "llm_wiki": "knowledge.manage",
    "obs": "observability.view",
}

# 权限蕴含 (高级动作级自然包含低级): 明细持有者可看聚合; 工作流管理者可触发
PERM_IMPLIES: dict = {
    "observability.detail": ["observability.view"],
    "n8n.manage": ["n8n.operate"],
}


def _expand_perms(perms: list) -> list:
    """按蕴含表展开 (去重保序)"""
    return list(dict.fromkeys([q for p in perms for q in [p, *PERM_IMPLIES.get(p, [])]]))

# ═══ L4: 范围约束 (scope) ═══
# 维度: site (站点) / customer (客户); 空列表 = 不限 (全量)。
# 生效: 个人 scope ∪ 组 scope; 校验点 = 审批决策 (decide) + 用户申请 (create_approval)。
SCOPE_DIMS = {
    "sites": "站点",
    "customers": "客户",
}

# P0 修复 (SPEC 统一契约): scope 语义区分 ALL / NONE / SCOPED。
# effective_perms() 的 scopes dict 内嵌 "_modes" 键 {dim: all|scoped|none}:
#   all    = 该维度不限 (个人与组均为空)
#   scoped = 限定范围 (值为生效清单)
#   none   = 个人与组均非空但交集为空 — 空交集 = 拒绝所有, 不是不限
# 旧调用方只读 sites/customers 列表, "_modes" 由 scope_allows 解释。
_SCOPE_MODES_KEY = "_modes"


def _merge_scope_dim(own: list, grp: list) -> tuple:
    """单维度合并: 取更严 (一方不限取另一方; 均非空取交集)

    Returns: (values, mode); 交集为空 → ([], "none") — 空交集不再被解释为不限。
    """
    if not own and not grp:
        return [], "all"
    if not own:
        return list(grp), "scoped"
    if not grp:
        return list(own), "scoped"
    inter = [x for x in dict.fromkeys(own) if x in set(grp)]
    return inter, ("scoped" if inter else "none")

# ═══ L2: 业务角色模板 (权限集命名打包; 授权快捷方式, 非独立机制) ═══
BIZ_PERM_ROLES: dict = {
    "值班工程师":   ["n8n.operate", "notify.manage", "observability.view", "devops.operate"],
    "变更经理":     ["approval.decide", "approval.ds_review", "vmops.operate", "observability.detail"],
    "自动化管理员": ["n8n.operate", "n8n.manage", "notify.manage", "devops.operate"],
    "知识运营编辑": ["knowledge.manage"],
    "全业务管理员": list(BIZ_PERMS.keys()),  # 全部权限点
}

# ═══ write 级动作 → 所需权限点 (权限问题 ≠ 变更审批, 无权限直接拒并指引授权) ═══
WRITE_PERM_MAP: dict = {
    ("vm_agent", "vm_snapshot"): "vmops.operate",
    ("dev_agent", "script_exec"): "devops.operate",
    ("dev_agent", "ansible_run"): "devops.operate",
}

_redis = None


def _norm_perm(p: str) -> str:
    """旧 key 归一到新命名空间"""
    return BIZ_PERM_ALIASES.get(p, p)


def clean_biz_perms(raw) -> list:
    """清洗业务权限: 旧 key 归一 + 仅保留目录内的权限点 (去重保序)"""
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(_norm_perm(p) for p in raw if _norm_perm(p) in BIZ_PERMS))


def clean_scopes(raw) -> dict:
    """清洗范围约束: {sites: [...], customers: [...]}; 空/缺省 = 不限"""
    out = {"sites": [], "customers": []}
    if isinstance(raw, dict):
        for dim in out:
            v = raw.get(dim) or []
            if isinstance(v, list):
                out[dim] = [str(x).strip() for x in v if str(x).strip()][:50]
    return out


def biz_label(perm: str) -> str:
    """权限点展示名 (目录外回退为 key 本身)"""
    meta = BIZ_PERMS.get(_norm_perm(perm))
    return meta["label"] if meta else perm


def biz_catalog() -> list:
    """权限点目录 (供前端动态渲染授权界面)"""
    return [
        {"key": key, "label": meta["label"], "desc": meta["desc"], "group": meta["group"]}
        for key, meta in BIZ_PERMS.items()
    ]


# 站点中文名 ↔ OPS-CORE 三字码互认 (与 OPS-CORE 站点字典一致; 规范化为三字码后比对)
_SITE_ALIAS = {"香港": "HKG", "上海": "SHH", "北京": "BEJ", "广州": "GUZ"}


def _site_norm(x) -> str:
    return _SITE_ALIAS.get(str(x or "").strip(), str(x or "").strip())


def scope_allows(scopes: dict, site: str = "", customer: str = "",
                 customer_gid: str = "") -> tuple:
    """范围校验 (P0: fail-closed)

    - 单据/资源字段为空且决策人该维度为 SCOPED → 拒绝 (资源信息缺失不跳过限制);
    - 该维度为 NONE (个人与组空交集) → 一律拒绝;
    - 站点: 中文名与三字码互认 (scope 配「香港」可拦单据 site=HKG, 反之亦然);
      完整站点代码 (如 HKHKGBBB) 包含城市三字码 (HKG) 即视为命中城市级范围;
    - 客户: 名称或 GID 任一命中即通过 (scope 配 GID 亦可拦截, 与 OPS-CORE CRM 联动)。
    Returns: (allowed: bool, reason: str)
    """
    modes = (scopes.get(_SCOPE_MODES_KEY) if isinstance(scopes, dict) else None) or {}
    sites = scopes.get("sites") or []
    customers = scopes.get("customers") or []
    site_mode = modes.get("sites") or ("scoped" if sites else "all")
    cust_mode = modes.get("customers") or ("scoped" if customers else "all")
    if site_mode == "none":
        return False, "你的个人与所属组站点范围交集为空 (无任何可管站点), 请联系管理员修正范围配置"
    if site_mode == "scoped":
        if not site:
            return False, "单据缺少站点字段, 无法确认是否在你的可管范围内 (请补充站点后重试)"
        s_norm = _site_norm(site)
        # 命中: 完全相等, 或范围配置的城市三字码被完整站点代码包含 (HKG ⊂ HKHKGBBB)
        if not any(s_norm == _site_norm(x)
                   or (len(_site_norm(x)) == 3 and _site_norm(x) in s_norm)
                   for x in sites):
            return False, f"站点「{site}」不在你的可管范围 ({'、'.join(sites[:6])})"
    if cust_mode == "none":
        return False, "你的个人与所属组客户范围交集为空 (无任何可管客户), 请联系管理员修正范围配置"
    if cust_mode == "scoped":
        gid = (customer_gid or "").strip()
        if not (customer or gid):
            return False, "单据缺少客户字段, 无法确认是否在你的可管范围内 (请补充客户后重试)"
        if customer not in customers and gid not in customers:
            return False, f"客户「{customer or gid}」不在你的可管范围 ({'、'.join(customers[:5])}...)"
    return True, ""


async def _get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


async def _load_user(username: str) -> dict:
    r = await _get_redis()
    raw = await r.hget(config.KEY_USERS, username)
    return json.loads(raw) if raw else {}


async def _load_group_perms(group: str) -> dict:
    """组级授权 (权限点 + 范围); 组不存在/未配置返回空

    兼容: 旧值为纯数组 (v2 仅权限点) → 视为 {perms: 旧值, scope 不限}
    """
    if not group:
        return {"perms": [], "scopes": {"sites": [], "customers": []}}
    r = await _get_redis()
    raw = await r.hget(config.KEY_GROUP_BIZPERMS, group)
    if not raw:
        return {"perms": [], "scopes": {"sites": [], "customers": []}}
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"perms": [], "scopes": {"sites": [], "customers": []}}
    if isinstance(val, list):  # v2 兼容
        return {"perms": clean_biz_perms(val), "scopes": {"sites": [], "customers": []}}
    if isinstance(val, dict):
        return {"perms": clean_biz_perms(val.get("perms")), "scopes": clean_scopes(val.get("scope"))}
    return {"perms": [], "scopes": {"sites": [], "customers": []}}


async def effective_perms(username: str) -> dict:
    """用户有效权限与范围: 个人 ∪ 所属组 (组级继承)

    Returns: {own, inherited, effective, role, scopes: {sites, customers}}
    """
    user = await _load_user(username)
    own = clean_biz_perms(user.get("biz_perms"))
    # TTL 临时授权: 到期自动失效 (读取时判定, 无需后台任务); 过期项保留在 own_ttl 供前端提示续期
    ttl_map = user.get("biz_perms_ttl") if isinstance(user.get("biz_perms_ttl"), dict) else {}
    now = time.time()
    expired = [p for p, exp in ttl_map.items() if isinstance(exp, (int, float)) and exp < now and p in own]
    active_ttl = {p: exp for p, exp in ttl_map.items() if isinstance(exp, (int, float)) and exp >= now and p in own}
    grp = await _load_group_perms(user.get("group") or "")
    inherited = [p for p in grp["perms"] if p not in own]
    own_scopes = clean_scopes(user.get("scopes"))
    # 范围合并语义 = 取更严: 一方为空(不限)取另一方; 均非空取交集 (约束不因组继承而放宽)
    # P0 修复: 交集为空 → mode=none (拒绝所有), 不再被 clean_scopes 的空列表语义 (不限) 吞掉
    merged = {}
    modes = {}
    for d in SCOPE_DIMS:
        vals, mode = _merge_scope_dim(own_scopes[d], grp["scopes"][d])
        merged[d] = vals
        modes[d] = mode
    merged[_SCOPE_MODES_KEY] = modes  # scope_allows 解释; 旧调用方读列表不受影响
    return {
        "own": own,
        "inherited": inherited,
        "effective": _expand_perms([p for p in own if p not in expired] + grp["perms"]),  # 过期剔除+展开蕴含
        "role": user.get("role", "user"),
        "scopes": merged,
        "expired": expired,          # 已过期自动失效的临时授权
        "active_ttl": active_ttl,    # 生效中的临时授权 {perm: expire_ts}
    }


async def bump_perm_metric(kind: str, perm: str):
    """权限使用度量: kind=used(通过) / denied(拒绝); 累计值, 供定期复核报表"""
    try:
        r = await _get_redis()
        await r.hincrby(config.KEY_PERM_METRICS, f"{kind}:{_norm_perm(perm)}", 1)
    except Exception:
        pass  # 度量失败不影响权限判定


async def require_biz_perm(username: str, perm: str):
    """业务操作门: 仅持有权限点 (含组继承, TTL 未过期) 可过; admin 无该业务权限也不行"""
    user = await _load_user(username)
    if _norm_perm(perm) in (await effective_perms(username))["effective"]:
        await bump_perm_metric("used", perm)
        return user
    await bump_perm_metric("denied", perm)
    raise HTTPException(403, f"需要业务权限「{biz_label(perm)}」; 请联系系统管理员在用户管理中授予")


async def require_admin_or_biz_perm(username: str, perm: str):
    """查看/审计门: admin (审计查看) 或持权者 (含组继承, TTL 未过期) 均可"""
    user = await _load_user(username)
    if user.get("role") == "admin":
        return user
    if _norm_perm(perm) in (await effective_perms(username))["effective"]:
        await bump_perm_metric("used", perm)
        return user
    await bump_perm_metric("denied", perm)
    raise HTTPException(403, f"仅系统管理员 (审计查看) 或持有「{biz_label(perm)}」业务权限的用户可访问")


# ═══ P0: 工具路径 write 门 (QueryEngine 工具循环; 与 WRITE_PERM_MAP 的 action 路径互补) ═══
# write 级工具名 → 所需权限点。未登记映射的 write 级工具 = 缺少授权元数据, 默认拒绝
# (SPEC: 未知工具或缺少授权元数据默认拒绝)。
WRITE_TOOL_PERM_MAP: dict = {
    "vm_snapshot_create": "vmops.operate",
    "script_exec": "devops.operate",
    "ansible_run": "devops.operate",
    "n8n_workflow_trigger": "n8n.operate",
}


async def check_write_allowed(username: str, tool_name: str, params: dict = None) -> tuple:
    """write 级工具调用门: 权限点 + 资源范围双重校验 (fail-closed)

    Returns: (allowed: bool, reason: str, required_perm: str)
    - 工具无权限映射 (缺授权元数据) → 拒绝;
    - 无法确认发起主体 (username 空) → 拒绝;
    - params 携带 site/customer/customer_gid 时做范围校验
      (SCOPED 且资源信息缺失 → 拒绝, 不跳过限制)。
    """
    required = WRITE_TOOL_PERM_MAP.get(tool_name)
    if not required:
        return False, f"write 级工具 {tool_name} 未登记业务权限映射 (缺少授权元数据, 默认拒绝)", ""
    username = (username or "").strip()
    if not username:
        return False, f"{tool_name} 需要业务权限「{biz_label(required)}」, 但当前请求无法确认发起用户", required
    eff = await effective_perms(username)
    if required not in eff["effective"]:
        await bump_perm_metric("denied", required)
        return False, f"需要业务权限「{biz_label(required)}」; 请联系系统管理员在用户管理中授予", required
    await bump_perm_metric("used", required)
    p = params or {}
    ok, why = scope_allows(eff["scopes"], str(p.get("site") or ""),
                           str(p.get("customer") or ""), str(p.get("customer_gid") or ""))
    if not ok:
        return False, why, required
    return True, "", required
