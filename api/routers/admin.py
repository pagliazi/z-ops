"""Z-OPS /api 认证与管理 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from webchat_api import (_BUILTIN_PROVIDERS, _ONEAPI_CACHE_TTL, _emit_daily_log, _get_all_groups, _log_user_op, _validate_group)  # noqa: F401  本域引用的主文件私有辅助
from fastapi import APIRouter

# SSO 后台引导任务强引用集 (防止 asyncio 弱引用导致任务被 GC 中途回收)
_SSO_BG_TASKS: set = set()

router = APIRouter()


# ═══ 业务权限点 (biz_perms; 与系统角色 role 分离) ═══
# 目录与权限门集中在 core/bizperms.py; 新增业务域在 BIZ_PERMS 注册后, 授权界面自动出现新选项。
def _norm_perm_key(k: str) -> str:
    from core.bizperms import BIZ_PERM_ALIASES
    return BIZ_PERM_ALIASES.get(str(k), str(k))


from core.bizperms import (  # noqa: F401  (兼容本域历史引用名)
    BIZ_PERMS as BIZ_PERMS_ALL,
    clean_biz_perms as _clean_biz_perms,
    clean_scopes as _clean_scopes,
    scope_allows,
    biz_catalog,
    biz_label,
    BIZ_PERM_ROLES,
    effective_perms,
    require_biz_perm,
    require_admin_or_biz_perm,
)


@router.get("/api/auth/biz-perms")
async def auth_biz_perm_catalog(current_user: str = Depends(verify_token)):
    """业务权限点目录 (登录用户可读, 供前端动态渲染授权界面; 授予操作仍走用户管理 PUT)"""
    return {"perms": biz_catalog(), "count": len(BIZ_PERMS_ALL), "roles": BIZ_PERM_ROLES}



# ═══ 认证 API ═══
@router.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    auth_type = body.get("auth_type", "local")  # "local" 或 "ldap"

    if not username or not password:
        raise HTTPException(400, "缺少用户名或密码")

    r = await get_redis()

    # ── LDAP 认证 (需管理员预先授权) ──
    if auth_type == "ldap":
        if not config.LDAP_ENABLED:
            raise HTTPException(400, "LDAP 认证未启用")

        # 1. AD 验证账号密码
        ldap_user = ldap_authenticate(username, password)
        if not ldap_user:
            raise HTTPException(401, "AD 域账号或密码错误")

        # 2. 检查是否已授权 (存在于 Redis)
        existing_raw = await r.hget(config.KEY_USERS, username)
        if not existing_raw:
            # 未授权 → 发邮件通知管理员，拒绝登录
            html_body = f"""
            <html><body style="font-family: Arial, sans-serif; color: #333;">
            <h2 style="color: #2563eb;">Z-OPS 新用户申请通知</h2>
            <p>以下 AD 用户尝试登录 Z-OPS 运维助手，但尚未被授权：</p>
            <div style="background: #f3f4f6; padding: 16px; border-radius: 8px; margin: 16px 0;">
                <p><strong>用户名:</strong> {username}</p>
                <p><strong>显示名:</strong> {ldap_user.get('display_name', '-')}</p>
                <p><strong>邮箱:</strong> {ldap_user.get('mail', '-')}</p>
                <p><strong>DN:</strong> {ldap_user.get('dn', '-')}</p>
                <p><strong>时间:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
            <p style="color: #6b7280; font-size: 14px;">请登录管理后台进行授权操作。</p>
            </body></html>
            """
            send_notify_email_async(f"Z-OPS 新用户申请：{username}", html_body)
            logger.info(f"[LDAP] 用户 '{username}' 未授权，已发送通知邮件给管理员")
            raise HTTPException(403, "您的账号尚未被授权访问 Z-OPS，请联系管理员审批")

        # 3. 已授权 → 更新信息并生成 Token
        existing = json.loads(existing_raw)
        existing["display_name"] = ldap_user["display_name"]
        existing["auth_source"] = "ldap"
        existing["mail"] = ldap_user.get("mail", "")
        existing["last_login"] = time.time()
        await r.hset(config.KEY_USERS, username, json.dumps(existing, ensure_ascii=False))
        role = existing.get("role", config.LDAP_DEFAULT_ROLE)
        display_name = existing["display_name"]

        token = secrets.token_hex(32)
        await r.set(config.key_token(token), username, ex=config.TOKEN_TTL)
        client_ip = request.client.host if request.client else "unknown"
        await _log_user_op(username, "login", f"LDAP 登录，角色: {role}", ip=client_ip)
        await _emit_daily_log("system", f"用户 {username} 登录 (LDAP, IP: {client_ip})", level="info", category="auth", username=username)

        # ── SSO 会话引导 (零密码存储): 用本次登录内存中的 AD 凭据一次性登录
        # Netcool, 只把会话 Cookie 存入 Redis (密码即弃, 不落盘不持久化)。
        # 后台执行不阻塞登录响应; 失败静默 (用户回退全局视图)。
        # 全局监控账号 (svc-opscore) 同样引导 — 其定时采集优先复用该会话 (SSO keep-alive)。
        if config.LDAP_ENABLED and username and password:
            async def _bootstrap():
                try:
                    from agents.nms_agent import nms_bootstrap_session
                    res = await nms_bootstrap_session(username, username, password)
                    if res.get("success"):
                        await _emit_daily_log("system",
                            f"NMS-SSO: {username} 的 NMS 会话已建立 "
                            f"(cookies={res.get('cookies')})",
                            level="info", category="auth", username=username)
                    else:
                        await _emit_daily_log("system",
                            f"NMS-SSO: {username} 会话引导失败: {res.get('error')}",
                            level="warning", category="auth", username=username)
                except Exception as e:
                    await _emit_daily_log("system",
                        f"NMS-SSO: {username} 会话引导异常: {type(e).__name__}: {e}",
                        level="warning", category="auth", username=username)
            # fire-and-forget 但必须持有引用 — 否则任务可能在执行中被 GC 回收
            # (asyncio 只保留弱引用, playwright 引导耗时 20s+ 极易中招)
            _t = asyncio.create_task(_bootstrap())
            _SSO_BG_TASKS.add(_t)
            _t.add_done_callback(_SSO_BG_TASKS.discard)

        return {
            "token": token, "username": username, "display_name": display_name,
            "role": role, "auth_source": "ldap", "expires_in": config.TOKEN_TTL,
            "biz_perms": (await effective_perms(username))["effective"],  # 个人∪组继承
        }

    # ── 本地认证 (默认) ──
    user_raw = await r.hget(config.KEY_USERS, username)
    if not user_raw:
        await _emit_daily_log("system", f"登录失败: {username} (用户不存在, IP: {request.client.host if request.client else 'unknown'})", level="warning", category="auth", username=username)
        raise HTTPException(401, "用户名或密码错误")

    user = json.loads(user_raw)
    if user["password_hash"] != hash_password(password):
        await _emit_daily_log("system", f"登录失败: {username} (密码错误, IP: {request.client.host if request.client else 'unknown'})", level="warning", category="auth", username=username)
        raise HTTPException(401, "用户名或密码错误")

    token = secrets.token_hex(32)
    await r.set(config.key_token(token), username, ex=config.TOKEN_TTL)
    client_ip = request.client.host if request.client else "unknown"
    await _log_user_op(username, "login", f"本地登录", ip=client_ip)
    await _emit_daily_log("system", f"用户 {username} 登录 (本地认证, IP: {client_ip})", level="info", category="auth", username=username)
    return {
        "token": token, "username": username,
        "display_name": user.get("display_name", username),
        "role": user.get("role", "user"),
        "auth_source": "local", "expires_in": config.TOKEN_TTL,
        "biz_perms": (await effective_perms(username))["effective"],  # 个人∪组继承
    }

@router.get("/api/auth/ldap-status")
async def ldap_status():
    """前端查询 LDAP 是否启用"""
    return {"ldap_enabled": config.LDAP_ENABLED}

@router.post("/api/auth/register")
async def auth_register(request: Request, current_user: str = Depends(verify_token)):
    """注册新用户 (仅管理员)"""
    r = await get_redis()
    admin_raw = await r.hget(config.KEY_USERS, current_user)
    if admin_raw:
        admin = json.loads(admin_raw)
        if admin.get("role") != "admin":
            raise HTTPException(403, "仅管理员可创建用户")
    else:
        raise HTTPException(403, "权限不足")

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    display_name = body.get("display_name", username)
    role = body.get("role", "user")
    group = body.get("group", "")

    if not username or not password:
        raise HTTPException(400, "缺少用户名或密码")
    await _validate_group(group)

    existing = await r.hget(config.KEY_USERS, username)
    if existing:
        raise HTTPException(409, "用户已存在")

    user_data = {
        "username": username,
        "password_hash": hash_password(password),
        "display_name": display_name,
        "role": role,
        "group": group,
        "biz_perms": _clean_biz_perms(body.get("biz_perms")),
        "created_at": time.time()
    }
    await r.hset(config.KEY_USERS, username, json.dumps(user_data, ensure_ascii=False))
    await _emit_daily_log("system", f"管理员 {current_user} 创建用户: {username} (角色: {role}, 业务权限: {user_data['biz_perms']})", level="info", category="admin", username=current_user)
    return {"status": "ok", "username": username}

@router.get("/api/auth/me")
async def auth_me(current_user: str = Depends(verify_token)):
    r = await get_redis()
    user_raw = await r.hget(config.KEY_USERS, current_user)
    if not user_raw:
        raise HTTPException(404, "用户不存在")
    user = json.loads(user_raw)
    user_group = user.get("group", "")
    role = user.get("role", "user")

    # 获取组别对应的可见TAB
    visible_tabs = []
    if user_group and role != "admin":
        tabs_raw = await r.hget(config.KEY_GROUP_TABS, user_group)
        if tabs_raw:
            visible_tabs = json.loads(tabs_raw)
    # 审批中心为全员默认入口 (申请/查询审批为基本能力): 组 TAB 白名单非空时强制包含
    # (空列表 = 默认全部可见, 不动; 前端 Sidebar 对 /approvals 亦做 always-visible 兜底)
    if visible_tabs and "/approvals" not in visible_tabs:
        visible_tabs.append("/approvals")

    return {
        "username": current_user,
        "display_name": user.get("display_name", current_user),
        "role": role,
        "group": user_group,
        "visible_tabs": visible_tabs,
        "biz_perms": (await effective_perms(current_user))["effective"],  # 个人∪组继承
    }

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    username = "unknown"
    if auth.startswith("Bearer "):
        token = auth[7:]
        r = await get_redis()
        username = await r.get(config.key_token(token)) or "unknown"
        await r.delete(config.key_token(token))
    client_ip = request.client.host if request.client else "unknown"
    await _emit_daily_log("system", f"用户 {username} 登出 (IP: {client_ip})", level="info", category="auth", username=username)
    return {"status": "ok"}

@router.post("/api/auth/authorize-ad")
async def authorize_ad_user(request: Request, current_user: str = Depends(verify_token)):
    """授权 AD 用户 (仅管理员，无需密码)"""
    await require_admin(current_user)
    body = await request.json()
    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip() or username
    role = body.get("role", "user")
    group = body.get("group", "")

    if not username:
        raise HTTPException(400, "缺少用户名")
    if role not in ("admin", "user"):
        raise HTTPException(400, "角色必须为 admin 或 user")
    await _validate_group(group)

    r = await get_redis()
    existing_raw = await r.hget(config.KEY_USERS, username)
    if existing_raw:
        # 已存在 → 更新 auth_source 为 ldap 并保留其他信息
        existing = json.loads(existing_raw)
        existing["auth_source"] = "ldap"
        if display_name and display_name != username:
            existing["display_name"] = display_name
        if role:
            existing["role"] = role
        if group is not None:
            existing["group"] = group
        if "biz_perms" in body:
            existing["biz_perms"] = _clean_biz_perms(body.get("biz_perms"))
        await r.hset(config.KEY_USERS, username, json.dumps(existing, ensure_ascii=False))
        return {"status": "ok", "username": username, "action": "updated", "role": role, "group": group}

    user_data = {
        "username": username,
        "password_hash": "",
        "display_name": display_name,
        "role": role,
        "group": group,
        "biz_perms": _clean_biz_perms(body.get("biz_perms")),
        "auth_source": "ldap",
        "created_at": time.time()
    }
    await r.hset(config.KEY_USERS, username, json.dumps(user_data, ensure_ascii=False))
    logger.info(f"[AUTH] 管理员 {current_user} 授权 AD 用户: {username} (角色: {role})")
    return {"status": "ok", "username": username, "action": "created", "role": role}

@router.get("/api/auth/users")
async def auth_users(current_user: str = Depends(verify_token)):
    """列出所有用户 (仅管理员)"""
    r = await get_redis()
    admin_raw = await r.hget(config.KEY_USERS, current_user)
    if admin_raw:
        admin = json.loads(admin_raw)
        if admin.get("role") != "admin":
            raise HTTPException(403, "仅管理员可查看")
    users_raw = await r.hgetall(config.KEY_USERS)
    # 组级业务权限一次性读入 (成员继承展示用)
    group_biz_map = {}
    for g, raw in (await r.hgetall(config.KEY_GROUP_BIZPERMS)).items():
        try:
            group_biz_map[g] = _clean_biz_perms(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            group_biz_map[g] = []
    users = []
    for uname, data in users_raw.items():
        u = json.loads(data)
        own = _clean_biz_perms(u.get("biz_perms"))
        inherited = [p for p in group_biz_map.get(u.get("group", ""), []) if p not in own]
        users.append({
            "username": uname,
            "display_name": u.get("display_name", uname),
            "role": u.get("role", "user"),
            "group": u.get("group", ""),
            "biz_perms": own,
            "inherited_perms": inherited,  # 从所属组继承的业务权限 (只读展示)
            "scopes": _clean_scopes(u.get("scopes")),  # 个人范围约束 (site/customer; 空=不限)
            "biz_perms_ttl": u.get("biz_perms_ttl") or {},  # 临时授权 {perm: expire_ts} (回显剩余期)
            "auth_source": u.get("auth_source", "local"),
            "mail": u.get("mail", ""),
            "created_at": u.get("created_at", 0),
            "last_login": u.get("last_login", 0),
        })
    return {"users": users, "count": len(users)}

@router.put("/api/auth/users/{username}")
async def update_user(username: str, request: Request, current_user: str = Depends(verify_token)):
    """编辑用户: 修改角色/显示名 (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, username)
    if not raw:
        raise HTTPException(404, "用户不存在")

    user = json.loads(raw)
    body = await request.json()

    if "role" in body:
        # 不能把自己降级
        if username == current_user and body["role"] != "admin":
            raise HTTPException(400, "不能修改自己的管理员角色")
        user["role"] = body["role"]
    if "display_name" in body:
        user["display_name"] = body["display_name"]
    if "group" in body:
        grp = body["group"]
        await _validate_group(grp)
        user["group"] = grp
    if "biz_perms" in body:
        new_perms = _clean_biz_perms(body.get("biz_perms"))
        old_perms = _clean_biz_perms(user.get("biz_perms"))
        # 权限变更审计: 谁在何时给谁授予/回收了哪些权限点 (永久留痕)
        granted = [p for p in new_perms if p not in old_perms]
        revoked = [p for p in old_perms if p not in new_perms]
        if granted or revoked:
            parts = []
            if granted: parts.append("授予 " + "、".join(f"{biz_label(p)}({p})" for p in granted))
            if revoked: parts.append("回收 " + "、".join(f"{biz_label(p)}({p})" for p in revoked))
            await _log_user_op(current_user, "biz_perms", f"变更 {username} 个人业务权限: " + "; ".join(parts))
            await _emit_daily_log("system", f"管理员 {current_user} 变更 {username} 业务权限: " + "; ".join(parts), level="warning", category="admin", username=current_user)
        user["biz_perms"] = new_perms
    if "biz_perms_ttl" in body:
        # TTL 临时授权: {perm: expire_ts}; 到期读取时自动失效 (core/bizperms.effective_perms)
        raw_ttl = body.get("biz_perms_ttl") if isinstance(body.get("biz_perms_ttl"), dict) else {}
        new_ttl = {}
        for k, v in raw_ttl.items():
            k2 = _norm_perm_key(k)
            if k2 in (user.get("biz_perms") or []) and isinstance(v, (int, float)) and v > 0:
                new_ttl[k2] = float(v)
        if new_ttl != (user.get("biz_perms_ttl") or {}):
            import datetime as _dt
            fmt_ttl = lambda t: "; ".join(
                f"{biz_label(p)} 至 {_dt.datetime.fromtimestamp(exp).strftime('%m-%d %H:%M')}" for p, exp in sorted(new_ttl.items()))
            await _log_user_op(current_user, "biz_perms_ttl", f"变更 {username} 临时授权有效期: {fmt_ttl(new_ttl) or '全部清除'}")
            await _emit_daily_log("system", f"管理员 {current_user} 变更 {username} 临时授权 ({len(new_ttl)} 项设限)",
                level="warning", category="admin", username=current_user)
        user["biz_perms_ttl"] = new_ttl
    if "scopes" in body:
        new_scopes = _clean_scopes(body.get("scopes"))
        old_scopes = _clean_scopes(user.get("scopes"))
        if new_scopes != old_scopes:
            fmt = lambda s: "、".join(s) if s else "不限"
            await _log_user_op(current_user, "biz_scope",
                f"变更 {username} 范围约束: 站点 {fmt(old_scopes['sites'])}→{fmt(new_scopes['sites'])}; "
                f"客户 {fmt(old_scopes['customers'])}→{fmt(new_scopes['customers'])}")
            await _emit_daily_log("system", f"管理员 {current_user} 变更 {username} 范围约束 "
                f"(站点: {fmt(new_scopes['sites']) or '不限'}, 客户: {fmt(new_scopes['customers']) or '不限'})",
                level="warning", category="admin", username=current_user)
        user["scopes"] = new_scopes

    await r.hset(config.KEY_USERS, username, json.dumps(user, ensure_ascii=False))
    return {"status": "ok", "username": username, "role": user.get("role"), "display_name": user.get("display_name"), "group": user.get("group", ""), "biz_perms": user.get("biz_perms") or [], "scopes": _clean_scopes(user.get("scopes"))}

@router.delete("/api/auth/users/{username}")
async def delete_user(username: str, current_user: str = Depends(verify_token)):
    """删除用户 (仅管理员，不能删自己)"""
    await require_admin(current_user)
    if username == current_user:
        raise HTTPException(400, "不能删除自己的账号")
    r = await get_redis()
    deleted = await r.hdel(config.KEY_USERS, username)
    if not deleted:
        raise HTTPException(404, "用户不存在")
    return {"status": "ok", "username": username}

@router.post("/api/auth/users/{username}/reset-password")
async def reset_password(username: str, request: Request, current_user: str = Depends(verify_token)):
    """重置用户密码 (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, username)
    if not raw:
        raise HTTPException(404, "用户不存在")

    body = await request.json()
    new_password = body.get("password", "")
    if not new_password or len(new_password) < 4:
        raise HTTPException(400, "密码至少4位")

    user = json.loads(raw)
    user["password_hash"] = hash_password(new_password)
    await r.hset(config.KEY_USERS, username, json.dumps(user, ensure_ascii=False))
    return {"status": "ok", "username": username}

# ═══ v4.0 Phase 4.2: destructive 操作审批门 API ═══

async def _viewer_role_group(username: str) -> tuple:
    """查看者 (role, group): admin 全局可见; 普通用户按组过滤"""
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, username)
    if not raw:
        return "user", ""
    u = json.loads(raw)
    return u.get("role") or "user", (u.get("group") or "").strip()


async def _ticket_group(r, rec: dict) -> str:
    """单据组别: 优先单据记录的 group; 无 (历史单/agent 单) 回查发起人当前组"""
    g = (rec.get("group") or "").strip()
    if g:
        return g
    u = (rec.get("username") or "").strip()
    if not u:
        return ""
    raw = await r.hget(config.KEY_USERS, u)
    if not raw:
        return ""
    try:
        return (json.loads(raw).get("group") or "").strip()
    except Exception:
        return ""


@router.get("/api/approvals")
async def list_approvals(current_user: str = Depends(verify_token)):
    """列出待流程决策的审批单 (全员可见 · 全分组只读透明)

    提单对全分组可见: 任何认证用户可查看待办池 (审计/跟进);
    决策操作仍需 approval.decide / approval.ds_review 业务权限 (decide 端点控制)。
    """
    from core.approval import list_pending_approvals
    pending = list_pending_approvals()
    role, my_group = await _viewer_role_group(current_user)
    if role != "admin":
        r = await get_redis()
        pending = [rec for rec in pending
                   if rec.get("username") == current_user
                   or await _ticket_group(r, rec) == my_group]
    return {"pending": pending, "count": len(pending),
            "note": "按组别可见 (管理员全局); 批准/拒绝需持有审批决策业务权限"}

@router.get("/api/approvals/mine")
async def list_my_approvals(current_user: str = Depends(verify_token)):
    """当前用户发起的审批申请列表 (含已决策; 单据 Redis TTL 24h, 完整决策历史在审计日志)"""
    from core.approval import list_user_approvals
    mine = list_user_approvals(current_user)
    return {"mine": mine, "count": len(mine)}

@router.get("/api/admin/perm-metrics")
async def perm_metrics_report(current_user: str = Depends(verify_token)):
    """权限使用度量报表 (定期复核依据): 每权限点的持有人数 / 通过次数 / 拒绝次数 + 治理建议

    used = 权限门通过累计; denied = 403 拒绝累计 (越权尝试信号);
    建议: denied 高 → 考虑补授权或关注滥用; used=0 且有持有人 → 考虑回收 (权限膨胀)。
    """
    r = await get_redis()
    raw_users = await r.hgetall(config.KEY_USERS)
    cur = json.loads(raw_users.get(current_user) or "{}")
    if cur.get("role") != "admin":
        raise HTTPException(403, "仅系统管理员可查看权限度量报表")
    raw = await r.hgetall(config.KEY_PERM_METRICS)
    rows = []
    for key, meta in BIZ_PERMS_ALL.items():
        holders = 0
        for uname in raw_users:
            try:
                if key in (await effective_perms(uname))["effective"]:
                    holders += 1
            except Exception:
                continue
        used = int(raw.get(f"used:{key}") or 0)
        denied = int(raw.get(f"denied:{key}") or 0)
        advice = ""
        if denied >= 10:
            advice = "频繁越权尝试, 关注并核对授权需求"
        elif used == 0 and holders > 0:
            advice = "从未使用, 考虑回收 (最小权限)"
        rows.append({"key": key, "label": meta["label"], "group": meta["group"],
                     "holders": holders, "used": used, "denied": denied, "advice": advice})
    return {"metrics": rows, "total_users": len(raw_users)}


async def _opscore_dict_sync(endpoint_key: str, params: dict, ttl: int, timeout: float = 8) -> dict:
    """OPS-CORE 平台字典同步 (带缓存, 全异常兜底)

    timeout: 单次拉取总上限 (默认 8s; 大数据量接口如客户全量可放宽)
    Returns: {"ok": bool, "data": dict} — ok=False 时调用方回退预置数据
    """
    try:
        import asyncio as _aio
        from agents.opscore_api import cached_query_api
        # 总上限: OPS-CORE 平台不可用 (凭据失效/网络慢) 时快速回退, 不阻塞调用方响应
        data = await _aio.wait_for(cached_query_api(endpoint_key, params, ttl=ttl), timeout=timeout)
        if isinstance(data, dict) and "error" not in data:
            return {"ok": True, "data": data}
        logger.debug(f"[opscore_dict_sync] {endpoint_key} 返回异常: {data}")
    except Exception as e:
        logger.debug(f"[opscore_dict_sync] {endpoint_key} 同步失败/超时: {e}")
    return {"ok": False, "data": {}}


def _norm_case(x: str) -> str:
    """case 号归一化: 平台双格式 5958943 / HD0000005955764 → 去 HD 前缀与前导零后比对"""
    x = str(x or "").strip().upper()
    if x.startswith("HD"):
        x = x[2:]
    x = x.lstrip("0")
    return x or "0"


@router.get("/api/approvals/meta")
async def approvals_meta(current_user: str = Depends(verify_token)):
    """申请表单元数据: 操作类型/服务类型/站点/客户/审批人候选 (下拉数据源)

    站点 = OPS-CORE 信息平台 site_info 同步 (失败回退: 预置清单 ∪ 历史审批单去重);
    客户 (GID+名称) = OPS-CORE 管理平台 CRM crm_customers 同步;
    审批人候选 = 有效持有 approval.decide 的用户 (含组继承, 排除申请人自己 — SoD 四眼原则)。
    """
    import re as _re
    from core.approval import APPROVAL_APPLY_TYPES, APPROVAL_CATEGORIES, OP_CATEGORY_HINTS
    from core.citp_templates import CITP_FAULT_TYPES
    r = await get_redis()
    preset_sites = [x.strip() for x in (config.APPROVAL_SITES or "").split(",") if x.strip()]
    # 对齐 OPS-CORE 平台服务模块 (vCenter/VCD 云主机, BRR/Veeam 备份, Snapshot 快照,
    # Nimble 存储, SmartDNS, NMS/Aria 监控; 历史 service_type 值经下方历史单去重补充兼容)
    # 对齐分类目录 (v2): 服务类型按 daily/maintenance 目录分组下发;
    # 历史 service_type 值经下方历史单去重补充兼容 (含旧「存储 (Nimble)」)
    from core.approval import APPROVAL_OP_CATALOG_STYPES as _CAT_STYPES
    preset_stypes = sorted({st for sts in _CAT_STYPES.values() for st in sts})
    sites, stypes = set(preset_sites), set(preset_stypes)
    tool_hist: set = set()  # 历史单操作类型 (未登记的补进「其他」分组, 不丢历史值)
    # OPS-CORE 站点同步 (信息管理系统): 按机楼逐条 → 完整站点代码 value (如 HKHKGBBB/CNGUZDDD)
    # 完整码优先取记录上的完整码字段 (含城市码的大写字母串); 无则 构造 region+city+mansion
    # (region 缺失时 city+mansion, 如 HKGBBB); 兜底仍不完整时退回城市码。
    # label = 机房中文名 (完整码); kw = 搜索域 (完整码+城市码+机楼码+中文名)。
    # popname 与 address 均空的壳记录剔除 (如 TPE-YUD/NYC-NAS)。
    _FULL_FIELDS = ("site", "site_code", "code", "sitename", "site_name", "name", "alias")
    _REGION_MAP = {"香港": "HK", "HONGKONG": "HK", "HONG KONG": "HK", "HK": "HK",
                   "台湾": "TW", "TAIWAN": "TW", "TW": "TW",
                   "中国": "CN", "CN": "CN", "CHINA": "CN", "广东": "CN", "上海": "CN",
                   "北京": "CN", "广州": "CN", "GUANGDONG": "CN"}

    def _site_full_code(rec: dict, city: str, mansion: str) -> str:
        for f in _FULL_FIELDS:
            v = str(rec.get(f) or "").strip().upper()
            if v and v.isalpha() and 5 <= len(v) <= 10 and city in v:
                return v
        if mansion and mansion.isalnum() and 2 <= len(mansion) <= 4:
            region = str(rec.get("region") or rec.get("province")
                         or rec.get("country") or "").strip().upper()
            rcode = _REGION_MAP.get(region, "")
            return f"{rcode}{city}{mansion}" if rcode else f"{city}{mansion}"
        return city

    sites_src = "preset"
    site_items: list = []
    opscore_sites = await _opscore_dict_sync("site_info", {"page_size": 200}, ttl=600)
    if opscore_sites["ok"]:
        by_site: dict = {}
        for x in (opscore_sites["data"].get("results") or []):
            if not isinstance(x, dict):
                continue
            city = str(x.get("city") or "").strip()
            popname = str(x.get("popname") or "").strip()
            mansion = str(x.get("mansion") or "").strip()
            if not city or (not popname and not str(x.get("address") or "").strip()):
                continue  # 无城市码或壳记录
            code = _site_full_code(x, city, mansion)
            label = popname or (f"{city}-{mansion}" if mansion else city)
            ent = by_site.setdefault(code, {"label": label, "kw": {city, code}})
            ent["kw"].update({city, code, mansion, label})
        # kw = 搜索域 (完整码 + 城市码 + 机楼码 + 机房中文名, 小写): 输入机楼名如 CTT/KXC 也能命中
        site_items = [{"value": c, "label": f"{e['label']} ({c})",
                       "kw": " ".join(sorted(x for x in e["kw"] if x)).lower()}
                      for c, e in sorted(by_site.items())]
        if site_items:
            opscore_codes = {it["value"] for it in site_items}
            opscore_cities = {c[:3] for c in opscore_codes if len(c) >= 3} | \
                          {x.get("city", "").strip() for x in (opscore_sites["data"].get("results") or [])}
            # 预置/历史站点语言不同 (中文城市名), 仅补充 OPS-CORE 未覆盖的;
            # 同城双语言去重 (香港↔HKG 等) 避免下拉出现两个"香港"
            cn_city_map = {"香港": "HKG", "上海": "SHH", "北京": "BEJ", "广州": "GUZ"}
            site_items += [{"value": x, "label": x} for x in sorted(sites - opscore_cities)
                           if cn_city_map.get(x, x) not in opscore_cities]
            sites.update(opscore_codes)
            sites_src = "opscore+preset"
    try:  # 历史审批单去重补充 (单据量小, TTL 24h)
        for key in r.scan_iter(match=f"{config.REDIS_PREFIX}:approval:*", count=200):
            d = r.hgetall(key)
            if not d:
                continue
            if d.get("site"):
                sites.add(d["site"].strip())
            if d.get("service_type"):
                stypes.add(d["service_type"].strip())
            if d.get("tool_name"):
                tool_hist.add(d["tool_name"].strip())
    except Exception as e:
        logger.debug(f"[approvals/meta] 历史单扫描失败: {e}")
    # 审批人候选: 遍历用户算 effective (含组继承), 排除自己
    # 两个清单: approvers (审批决策权, 技术/行政级候选) + approvers_ds (DS 配置复核权候选)
    candidates, ds_candidates = [], []
    try:
        for uname, raw in (await r.hgetall(config.KEY_USERS)).items():
            try:
                u = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if uname == current_user:
                continue
            eff = (await effective_perms(uname))["effective"]
            if "approval.ds_review" in eff:
                ds_candidates.append({"username": uname, "display_name": u.get("display_name") or uname})
            if "approval.decide" in eff:
                candidates.append({"username": uname, "display_name": u.get("display_name") or uname})
    except Exception as e:
        logger.debug(f"[approvals/meta] 候选人扫描失败: {e}")
    candidates.sort(key=lambda c: c["display_name"])
    ds_candidates.sort(key=lambda c: c["display_name"])
    from core.approval import APPROVAL_OP_GROUPS as _opg, APPROVAL_OP_CATALOG as _opc
    registered = {op for _, ops in _opg for op in ops}
    # 历史单里出现过但未登记的类型 (含自定义): 归入「平台服务/通用」组
    hist_extra = sorted(tool_hist - registered)

    def _build_groups(cat: str):
        out = []
        for product, services in _opc.get(cat, []):
            svcs = []
            for sv, ops in services:
                lst = [{"value": op, "label": APPROVAL_APPLY_TYPES[op]}
                       for op in ops if op in APPROVAL_APPLY_TYPES]
                if sv == "通用" and hist_extra:
                    lst += [{"value": t, "label": t} for t in hist_extra]
                if lst:
                    svcs.append({"service": sv, "ops": lst})
            if svcs:
                out.append({"product": product, "services": svcs})
        return out

    op_groups_by_category = {cat: _build_groups(cat) for cat in _opc}
    # 兼容扁平视图 operation_groups: stype = "产品/服务"
    merged: dict = {}
    for cat in ("daily", "maintenance"):
        for prod in op_groups_by_category[cat]:
            for svc in prod["services"]:
                st = f"{prod['product']}/{svc['service']}"
                ent = merged.setdefault(st, {"stype": st, "_ops": [], "_seen": set()})
                for o in svc["ops"]:
                    if o["value"] not in ent["_seen"]:
                        ent["_seen"].add(o["value"])
                        ent["_ops"].append(o)
    op_groups = [{"stype": st, "ops": e["_ops"]} for st, e in merged.items()]
    return {
        "apply_types": [{"value": k, "label": v} for k, v in APPROVAL_APPLY_TYPES.items()],
        "operation_groups": op_groups,  # 扁平兼容视图 [{stype: 产品/服务, ops}] 
        "operation_groups_by_category": op_groups_by_category,  # 三级目录 {cat: [{product, services: [{service, ops}]}]}
        "categories": [{"value": k, "label": v} for k, v in APPROVAL_CATEGORIES.items()],
        "category_hints": OP_CATEGORY_HINTS,  # 操作类型 → 建议分类 (表单联动预选, 可改)
        # CITP 故障类型模板目录 (维护审批; 内容按真实 CITP 附件格式填充)
        "citp_fault_types": [{"value": k, "label": v["label"], "source": v.get("source", "")}
                             for k, v in CITP_FAULT_TYPES.items()],
        "service_types": sorted(stypes),
        "sites": sorted(sites),
        "approvers": candidates,
        "approvers_ds": ds_candidates,  # DS 配置复核级候选 (持 approval.ds_review)
        "site_items": site_items,  # 结构化站点下拉 [{value: 三字码, label: 机房中文名 (码)}]
        "sources": {"sites": sites_src},  # 站点数据来源 (前端提示同步状态)
    }


@router.get("/api/approvals/customers")
async def approvals_customers(current_user: str = Depends(verify_token)):
    """客户字典 (OPS-CORE 管理平台 CRM 同步: GID + 客户名称 + Site ID 列表)

    Site ID 从 CRM 资源池名提取 (如 GIDTW43727C-HKGAAA → HKGAAA;
    OPS-CORE-MGT CRM 无独立 site id 字段, 站点标识嵌于 resource_pools 池名)。
    数据量大 (~千条) 且 146 平台序列化线性慢 (~30ms/条), 单页大 page_size 会超时;
    改为 5 页 × 200 并行拉取 (~7s 全量), 每页独立缓存 10min, 命中后秒回。
    平台不可用时返回 fallback=manual + 空列表 (表单客户字段可手输兜底)。
    """
    import asyncio as _aio
    import ast as _ast
    import re as _re
    _SITE_SEG = _re.compile(r"^[A-Z]{5,8}\d{0,2}$")

    def _pool_site_ids(rp) -> list:
        """resource_pools (str/list) → 站点代码列表 (池名大写段过滤, 去重保序)"""
        pools = rp
        if isinstance(pools, str):
            try:
                pools = _ast.literal_eval(pools)
            except Exception:
                pools = []
        out: list = []
        for p in (pools or []):
            if not isinstance(p, dict):
                continue
            for seg in str(p.get("pool_name") or "").upper().split("-"):
                if _SITE_SEG.match(seg) and seg not in out and not seg.startswith("GID"):
                    out.append(seg)
        return out[:8]

    customers = []
    try:
        from agents.opscore_api import cached_query_api
        pages = await _aio.gather(*[
            _aio.wait_for(cached_query_api("crm_customers",
                                           {"page_size": 200, "page": n}, ttl=600), timeout=15)
            for n in range(1, 6)])  # 5×200 ≥ 961 全量
        seen = set()
        for data in pages:
            for c in ((data.get("results") or []) if isinstance(data, dict) else []):
                if isinstance(c, dict):
                    gid = str(c.get("gid") or "").strip()
                    name = str(c.get("company_name") or "").strip()
                    if gid and gid not in seen:  # 无名称客户以 GID 兜底展示, 不丢弃
                        seen.add(gid)
                        customers.append({"gid": gid, "name": name or f"GID {gid}",
                                          "site_ids": _pool_site_ids(c.get("resource_pools")),
                                          "order_no": str(c.get("order_no") or "").strip()[:30]})
    except Exception as e:
        logger.debug(f"[approvals/customers] OPS-CORE CRM 同步失败: {e}")
    customers.sort(key=lambda x: x["name"])
    return {"customers": customers, "count": len(customers),
            "source": "opscore" if customers else "manual"}


@router.get("/api/approvals/case-lookup")
async def approval_case_lookup(case_no: str = "", current_user: str = Depends(verify_token)):
    """case 号联动校验 (与 OPS-CORE 信息管理系统 case_info 同步)

    返回 available (校验服务可用性) / found / cases 摘要 (编号/标题/状态/客户);
    平台不可用时 available=False, 前端提示但不阻断提交 (OPS-CORE 数据可能有延迟)。
    """
    import json as _json
    case_no = (case_no or "").strip()
    if len(case_no) < 3:
        return {"available": True, "found": False, "cases": []}
    try:
        from agents.opscore_api import cached_query_api
        data = await cached_query_api("case_info", {"search": case_no, "page_size": 10}, ttl=300)
    except Exception as e:
        return {"available": False, "found": False, "cases": [], "note": f"OPS-CORE 平台查询异常: {e}"}
    if isinstance(data, dict) and "error" in data:
        return {"available": False, "found": False, "cases": [], "note": f"OPS-CORE 信息平台不可用: {data['error']}"}
    items = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = []
    for c in items[:10]:
        if not isinstance(c, dict):
            continue
        # search 为模糊匹配, 本地再过滤: 原串包含 或 归一化比对 (HD0000005955764 ≡ 5955764)
        qn = _norm_case(case_no)
        if case_no.lower() not in _json.dumps(c, ensure_ascii=False).lower() and not any(
                qn == _norm_case(str(v)) for v in c.values() if str(v).strip()):
            continue
        # 145 case_info 实际字段: caseid/gid/CompanyName/Summary/create
        out.append({
            "case_no": str(c.get("caseid") or c.get("case_no") or c.get("number") or c.get("id") or "")[:100],
            "title": str(c.get("Summary") or c.get("title") or c.get("subject") or c.get("summary") or "")[:150],
            "status": str(c.get("Status") or c.get("status") or c.get("state") or "")[:60],
            "customer": str(c.get("CompanyName") or c.get("customer") or c.get("company_name") or "")[:150],
            "gid": str(c.get("gid") or c.get("customer_gid") or "")[:60],  # case 关联客户 GID (CRM 联动)
        })
    return {"available": True, "found": bool(out), "cases": out}


async def _match_active_alerts(r, site: str = "", customer: str = "",
                               case_no: str = "", host: str = "") -> dict:
    """按站点/客户/case/主机匹配 NMS 当前活动告警 (related-alerts 与 CITP 共用)

    快照源 {prefix}:nms_alarms:latest (nms_agent 定时抓取); 四维度任一命中即返回,
    matched 标注命中维度; case 维度告警 Hpdcaseid 归一化比对 (置顶), 其余按级别排序。
    """
    from webchat_api import _NMS_ACTIVE_SEV, _NMS_SEV_PRIORITY
    raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
    if not raw:
        return {"available": False, "alerts": [], "note": "NMS 快照未就绪 (监控任务尚未抓取)"}
    try:
        alarms = json.loads(raw).get("alarms") or []
    except Exception:
        return {"available": False, "alerts": [], "note": "NMS 快照解析失败"}
    cn = {"香港": "HKG", "上海": "SHH", "北京": "BEJ", "广州": "GUZ"}
    cn_rev = {v: k for k, v in cn.items()}
    site_q = (site or "").strip().upper()
    cust_q = (customer or "").strip().lower()
    case_qn = _norm_case(case_no) if (case_no or "").strip() else ""
    host_q = (host or "").strip().lower()
    out, uniq, total_active = [], set(), 0
    for a in alarms:
        if a.get("severity") not in _NMS_ACTIVE_SEV:
            continue
        total_active += 1
        node = str(a.get("node") or "")
        alias = str(a.get("node_alias") or "")
        summ = str(a.get("summary") or "")
        loc = str(a.get("location") or "")
        blob = f"{node} {alias} {summ} {loc}".lower()
        matched = []
        if case_qn and _norm_case(str(a.get("case_id") or "")) == case_qn:
            matched.append("case")
        if host_q and (host_q in blob or alias.lower() == host_q):
            matched.append("host")
        if site_q:
            # 站点: 三字码或城市中文名出现在 location/summary (城市名映射互认)
            s_terms = [t for t in (site_q, cn.get(site_q, ""), cn_rev.get(site_q, "")) if t]
            if any(t.lower() in blob for t in s_terms):
                matched.append("site")
        if cust_q and (cust_q in summ.lower() or cust_q in blob):
            matched.append("customer")
        if not matched:
            continue
        aid = str(a.get("alarm_id") or a.get("serial") or "")
        if not aid or aid in uniq:
            continue
        uniq.add(aid)
        out.append({"alarm_id": aid, "severity": a.get("severity"),
                    "node": node or alias, "summary": summ[:160],
                    "time": a.get("time") or a.get("last_time"),
                    "case_id": a.get("case_id") or "", "matched": matched})
    # case/host 精确命中置顶, 站点/客户泛匹配靠后 (避免淹没精确关联)
    out.sort(key=lambda x: (0 if "case" in x["matched"] else
                            (1 if "host" in x["matched"] else 2),
                            _NMS_SEV_PRIORITY.get(x["severity"], 9)))
    return {"available": True, "alerts": out[:30], "matched": len(out),
            "total_active": total_active}


@router.get("/api/approvals/related-alerts")
async def approvals_related_alerts(site: str = "", customer: str = "",
                                   case_no: str = "", host: str = "",
                                   current_user: str = Depends(verify_token)):
    """审批表单联动: 按站点/客户/case/主机匹配 NMS 当前活动告警 (可勾选关联)"""
    r = await get_redis()
    return await _match_active_alerts(r, site, customer, case_no, host)


@router.post("/api/approvals")
async def create_approval_api(request: Request, current_user: str = Depends(verify_token)):
    """用户主动发起审批申请 (审批只走流程: 申请仅留痕, 批准后由运维人员线下执行)

    body: {
      "tool_name": str (白名单内或自定义类型),
      "summary": str (申请事由, 必填),
      "params": dict, "risk_note": str,
      "site": str, "service_type": str, "case_no": str,
      "priority": str, "planned_ts": str,
      "impact_scope": str, "rollback_plan": str, "change_window": str,
      "customer": str (客户名称), "approvers": str (指定审批人, 逗号分隔),
    }
    """
    body = await request.json()
    tool_name = (body.get("tool_name") or "").strip()[:100]
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(400, "params 需为对象 (JSON 键值对)")
    if not tool_name:
        raise HTTPException(400, "tool_name 不能为空")
    summary = (body.get("summary") or "").strip()[:500]
    if not summary:
        raise HTTPException(400, "summary (申请事由) 为必填, 如: 客户请求移除这两个vm，已经提供SCCRF，检查已经关机，请审批")
    from core.approval import create_approval, APPROVAL_APPLY_TYPES
    site = (body.get("site") or "").strip()[:100]
    case_no = (body.get("case_no") or "").strip()[:100]
    if not site:
        raise HTTPException(400, "site (站点) 为必填")
    if not case_no:
        raise HTTPException(400, "case_no (case 号) 为必填")
    if not _norm_case(case_no).isdigit():
        raise HTTPException(400, "case_no 格式应为纯数字 (如 5958943) 或 HD 前缀 (如 HD0000005955764)")
    # DS 配置复核为必要级, 不能跳过 (分级审批流必含 DS)
    if not (body.get("approver_ds") or "").strip():
        raise HTTPException(400, "DS 配置复核为必要级, 不能跳过: 请指定 DS 复核人")
    # L4 申请侧范围约束: 申请人 scope (site/customer) 非空时, 申请字段须落在范围内
    customer_apply = (body.get("customer") or "").strip()[:200]
    # 客户 GID (OPS-CORE CRM 联动: 表单选客户自动带出; 需先于 scope 校验, 供 GID 维度匹配)
    customer_gid = (body.get("customer_gid") or "").strip()[:100]
    eff_apply = await effective_perms(current_user)
    ok_apply, why_apply = scope_allows(eff_apply["scopes"], site, customer_apply, customer_gid)
    if not ok_apply:
        raise HTTPException(400, f"范围约束: {why_apply}; 如需跨范围申请请联系管理员调整权限范围")
    risk_note = (body.get("risk_note") or "").strip()[:500]
    # 白名单类型用标准中文名; 其余视为自定义类型 (审批仅流程留痕, 放开无执行风险)
    if tool_name not in APPROVAL_APPLY_TYPES:
        risk_note = risk_note or f"自定义操作类型 {tool_name} (destructive) 申请"
    else:
        risk_note = risk_note or f"{APPROVAL_APPLY_TYPES[tool_name]} (destructive) 申请"
    # 回退关联原单 (临时操作回退链)
    original_approval_id = (body.get("original_approval_id") or "").strip()[:60]
    if original_approval_id:
        from core.approval import get_approval as _ga
        orig = _ga(original_approval_id)
        if not orig:
            raise HTTPException(400, f"关联原审批单不存在或已过期: {original_approval_id}")
        if (orig.get("username") or "").strip() != current_user:
            raise HTTPException(403, "仅可对自己发起的审批单发起回退申请")
        if orig.get("status") != "approved":
            raise HTTPException(400, f"仅已批准的单可发起回退 (原单状态: {orig.get('status')})")
    # 关联 NMS 告警 (表单勾选快照摘要; 仅保留白名单字段, 限 10 条)
    _rel = body.get("related_alerts") or []
    _rel_items = []
    if isinstance(_rel, list):
        for _it in _rel[:10]:
            if isinstance(_it, dict):
                _rel_items.append({k: str(_it.get(k) or "")[:160] for k in
                                   ("alarm_id", "severity", "node", "summary", "time", "case_id")})
    # 审批分类: daily=日常配置 / maintenance=维护 (维护单可携带 CITP 快照)
    from core.approval import APPROVAL_CATEGORIES
    category = (body.get("category") or "daily").strip()
    if category not in APPROVAL_CATEGORIES:
        raise HTTPException(400, f"category 仅支持 {'/'.join(APPROVAL_CATEGORIES)}")
    citp_str = ""
    if category == "maintenance":
        _citp = body.get("citp")
        if isinstance(_citp, dict) and _citp:
            _citp["tool_name"] = tool_name
            _citp["site"] = site
            citp_str = json.dumps(_citp, ensure_ascii=False)
        if len(citp_str) > 12000:
            raise HTTPException(400, "CITP 快照过大 (>12KB), 请精简参数/设备后重试")
    # 单据归属发起人组别 (审批中心按组可见)
    _raw_u = await (await get_redis()).hget(config.KEY_USERS, current_user)
    try:
        _grp = (json.loads(_raw_u or "{}").get("group") or "").strip()[:100]
    except Exception:
        _grp = ""
    rec = create_approval(
        tool_name=tool_name,
        params=params,
        agent="manual",          # 用户主动申请 (非 agent 拦截)
        group=_grp,
        username=current_user,
        risk_note=risk_note,
        site=site,
        service_type=(body.get("service_type") or "").strip()[:100],
        case_no=case_no,
        priority=(body.get("priority") or "").strip()[:50],
        planned_ts=(body.get("planned_ts") or "").strip()[:50],
        impact_scope=(body.get("impact_scope") or "").strip()[:500],
        rollback_plan=(body.get("rollback_plan") or "").strip()[:500],
        change_window=(body.get("change_window") or "").strip()[:100],
        customer=(body.get("customer") or "").strip()[:200],
        customer_gid=customer_gid,
        customer_site_ids=(body.get("customer_site_ids") or "").strip()[:200],
        customer_order_no=(body.get("customer_order_no") or "").strip()[:60],
        original_approval_id=original_approval_id,
        summary=summary,
        approvers=(body.get("approvers") or "").strip()[:200],
        approver_ds=(body.get("approver_ds") or "").strip()[:200],
        approver_tech=(body.get("approver_tech") or "").strip()[:200],
        approver_admin=(body.get("approver_admin") or "").strip()[:200],
        target_host=(body.get("target_host") or "").strip()[:200],
        related_alerts=json.dumps(_rel_items, ensure_ascii=False) if _rel_items else "",
        category=category,
        citp=citp_str,
    )
    if citp_str and isinstance(body.get("citp"), dict):
        _audit_citp_attached(rec, body["citp"])   # CITP 随单轨迹 (生成先于提单)
    return {"status": "ok", "approval_id": rec["approval_id"], "tool_name": tool_name,
            "note": "审批仅流程留痕, 系统不会自动执行; 批准后由运维人员按变更流程线下执行"}


def _audit_citp_attached(rec: dict, citp: dict):
    """维护单随单 CITP 审计 (CITP 在提单前生成, 编号不同于审批单号; 创建时补记到单据轨迹)"""
    try:
        from core.approval import _audit_async
        devs = citp.get("devices") or [citp.get("device")] if citp.get("device") else []
        matched = sum(1 for d in devs if d and d.get("matched"))
        _audit_async({"agent": "manual", "username": rec.get("username", ""),
                      "tool_name": f"CITP:{citp.get('reference_number') or citp.get('citp_no')}",
                      "approval_id": rec.get("approval_id"),
                      "params": {"fault_type": citp.get("fault_type_key"),
                                 "matched": matched, "total": len(devs),
                                 "vms": citp.get("vms_count", 0)},
                      "status": "generated"},
                     action="approval_citp_generated",
                     summary=(f"CITP attached ({citp.get('fault_type') or '-'}; "
                              f"设备匹配 {matched}/{len(devs)}; VM {citp.get('vms_count', 0)} 台; "
                              f"{citp.get('citp_no') or ''})"))
    except Exception as e:
        logger.debug(f"[approval] CITP 随单审计失败: {e}")


@router.post("/api/approvals/{approval_id}/execute")
async def confirm_execution_api(approval_id: str,
                                current_user: str = Depends(verify_token)):
    """执行确认: 已批准单线下执行完成后确认 (申请人/同组/管理员)

    approved → executed; 审计 approval_executed; 维护单提醒验收。
    body 可选 {"note": str} 记录执行备注。
    """
    from core.approval import get_approval, confirm_execution
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期")
    role, grp = await _viewer_role_group(current_user)
    if role != "admin" and (rec.get("username") or "") != current_user:
        r0 = await get_redis()
        if await _ticket_group(r0, rec) != grp:
            raise HTTPException(403, "该审批单不属于你所在的组别, 无权确认执行")
    out, err = confirm_execution(approval_id, current_user)
    if err:
        raise HTTPException(400, err)
    note = ""
    if note:
        r = await get_redis()
        await r.hset(f"{config.REDIS_PREFIX}:approval:{approval_id}", "executed_note", note)
    return {"status": "ok", "approval_id": approval_id, "decision": "executed",
            "note": "已确认执行" + ("; 维护单请完成网卡验收" if (rec.get("category") == "maintenance") else "")}


# ═══ 申请草稿 (服务端存储: 每用户一份, Redis 7 天过期, 跨设备可用) ═══
# 注意: 必须注册在 GET /api/approvals/{approval_id} 之前, 否则 /draft 被路径参数吞掉

_DRAFT_MAX_BYTES = 60000


@router.get("/api/approvals/draft")
async def get_approval_draft(current_user: str = Depends(verify_token)):
    """读取当前用户的申请草稿 (无草稿返回 draft=null)"""
    r = await get_redis()
    raw = await r.get(f"{config.REDIS_PREFIX}:approval_draft:{current_user}")
    if not raw:
        return {"draft": None}
    try:
        return {"draft": json.loads(raw), "saved_ts": json.loads(raw).get("savedAt")}
    except Exception:
        return {"draft": None}


@router.put("/api/approvals/draft")
async def save_approval_draft(request: Request, current_user: str = Depends(verify_token)):
    """保存申请草稿 (整份覆盖; 表单 JSON 快照, 含分类/CITP 快照/设备匹配结果)"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "草稿需为 JSON 对象")
    s = json.dumps(body, ensure_ascii=False)
    if len(s.encode()) > _DRAFT_MAX_BYTES:
        raise HTTPException(400, f"草稿过大 (上限 {_DRAFT_MAX_BYTES // 1000}KB), 请精简参数/CITP 内容")
    r = await get_redis()
    await r.set(f"{config.REDIS_PREFIX}:approval_draft:{current_user}", s, ex=7 * 86400)
    return {"status": "ok", "saved_ts": time.time()}


@router.delete("/api/approvals/draft")
async def clear_approval_draft(current_user: str = Depends(verify_token)):
    """清除当前用户的申请草稿 (提交成功或主动丢弃)"""
    r = await get_redis()
    await r.delete(f"{config.REDIS_PREFIX}:approval_draft:{current_user}")
    return {"status": "ok"}


@router.put("/api/approvals/{approval_id}")
async def update_approval_api(approval_id: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """申请人编辑待审批单 (仅本人 + pending 状态)

    业务字段全量更新 (summary/site/操作类型/case/分级审批人/CITP 等);
    编辑后分级审批流重置从头审 (done_levels 清空, level_log 留 edited 标记),
    重新推送飞书卡片; 已决策单 (approved/rejected) 不可编辑 — 审计红线,
    如需变更请新建申请或回退申请。审计日志记录 approval_updated。
    """
    from core.approval import get_approval, APPROVAL_CATEGORIES, _audit_async
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期")
    if (rec.get("username") or "").strip() != current_user:
        raise HTTPException(403, "仅申请人本人可编辑该审批单")
    if rec.get("status") != "pending":
        raise HTTPException(400, f"仅待审批单可编辑 (当前状态: {rec.get('status')}); 已决策单请新建回退申请")

    body = await request.json()
    tool_name = (body.get("tool_name") or rec.get("tool_name") or "").strip()[:100]
    summary = (body.get("summary") or "").strip()[:500]
    site = (body.get("site") or rec.get("site") or "").strip()[:100]
    case_no = (body.get("case_no") or rec.get("case_no") or "").strip()[:100]
    approver_ds = (body.get("approver_ds") or "").strip()[:200]
    if not tool_name:
        raise HTTPException(400, "tool_name 不能为空")
    if not summary:
        raise HTTPException(400, "summary (申请事由) 为必填")
    if not site:
        raise HTTPException(400, "site (站点) 为必填")
    if not case_no or not _norm_case(case_no).isdigit():
        raise HTTPException(400, "case_no 格式应为纯数字 (如 5958943) 或 HD 前缀 (如 HD0000005955764)")
    if not approver_ds:
        raise HTTPException(400, "DS 配置复核为必要级, 不能跳过: 请指定 DS 复核人")
    category = (body.get("category") or rec.get("category") or "daily").strip()
    if category not in APPROVAL_CATEGORIES:
        raise HTTPException(400, f"category 仅支持 {'/'.join(APPROVAL_CATEGORIES)}")
    customer_apply = (body.get("customer") or "").strip()[:200]
    customer_gid = (body.get("customer_gid") or "").strip()[:100]
    eff = await effective_perms(current_user)
    ok_scope, why_scope = scope_allows(eff["scopes"], site, customer_apply, customer_gid)
    if not ok_scope:
        raise HTTPException(400, f"范围约束: {why_scope}; 如需跨范围申请请联系管理员调整权限范围")
    params = body.get("params")
    if not isinstance(params, dict):
        params = json.loads(rec.get("params") or "{}")
    citp_str = ""
    if category == "maintenance" and isinstance(body.get("citp"), dict) and body.get("citp"):
        _citp = dict(body["citp"])
        _citp["tool_name"] = tool_name
        _citp["site"] = site
        citp_str = json.dumps(_citp, ensure_ascii=False)
        if len(citp_str) > 12000:
            raise HTTPException(400, "CITP 快照过大 (>12KB), 请精简参数/设备后重试")
    _rel = body.get("related_alerts")
    rel_str = rec.get("related_alerts") or ""
    if isinstance(_rel, list):
        _rel_items = [{k: str(it.get(k) or "")[:160] for k in
                       ("alarm_id", "severity", "node", "summary", "time", "case_id")}
                      for it in _rel[:10] if isinstance(it, dict)]
        rel_str = json.dumps(_rel_items, ensure_ascii=False) if _rel_items else ""

    # 编辑后审批流重置: done_levels 清空从头审, level_log 追加 edited 标记
    log = rec.get("level_log") or ""
    edit_mark = f"edited@{time.time():.0f}"
    upd = {
        "tool_name": tool_name, "summary": summary, "site": site, "case_no": case_no,
        "params": json.dumps(params, ensure_ascii=False)[:2000],
        "service_type": (body.get("service_type") or "").strip()[:100],
        "priority": (body.get("priority") or "").strip()[:50],
        "planned_ts": (body.get("planned_ts") or "").strip()[:50],
        "impact_scope": (body.get("impact_scope") or "").strip()[:500],
        "rollback_plan": (body.get("rollback_plan") or "").strip()[:500],
        "change_window": (body.get("change_window") or "").strip()[:100],
        "customer": customer_apply, "customer_gid": customer_gid,
        "customer_site_ids": (body.get("customer_site_ids") or rec.get("customer_site_ids") or "").strip()[:200],
        "customer_order_no": (body.get("customer_order_no") or rec.get("customer_order_no") or "").strip()[:60],
        "approvers": (body.get("approvers") or "").strip()[:200],
        "approver_ds": approver_ds,
        "approver_tech": (body.get("approver_tech") or "").strip()[:200],
        "approver_admin": (body.get("approver_admin") or "").strip()[:200],
        "target_host": (body.get("target_host") or "").strip()[:200],
        "risk_note": (body.get("risk_note") or "").strip()[:500],
        "category": category, "citp": citp_str or (rec.get("citp") if category == "maintenance" else ""),
        "related_alerts": rel_str,
        "edited_ts": time.time(),
        "edit_count": int(rec.get("edit_count") or 0) + 1,
        "done_levels": "",
        "level_log": f"{log};{edit_mark}" if log else edit_mark,
    }
    r = await get_redis()
    # 审计差异: 与原单比对出实际变更字段 (含审批人变更)
    _diff = [k for k, v in upd.items()
             if k in ("summary", "tool_name", "site", "case_no", "service_type", "priority",
                      "planned_ts", "impact_scope", "rollback_plan", "change_window", "customer",
                      "approvers", "approver_ds", "approver_tech", "approver_admin",
                      "target_host", "risk_note", "category", "params")
             and str(v) != str(rec.get(k) if k != "params" else (rec.get("params") or "{}"))]
    await r.hset(f"{config.REDIS_PREFIX}:approval:{approval_id}", mapping=upd)
    updated = {**rec, **upd}
    _audit_async(updated, action="approval_updated",
                 summary=(f"updated (申请人 {current_user} 编辑待审批单; 变更字段: "
                          f"{('、'.join(_diff[:12]) or '无')}; 审批流已重置; edit_count={upd['edit_count']})"))
    logger.warning(f"[approval] 审批单 {approval_id} 被申请人 {current_user} 编辑 (第 {upd['edit_count']} 次, 审批流重置)")
    try:  # 编辑后重推飞书卡片 (审批人看到最新内容)
        from core.approval import _notify_feishu
        _notify_feishu(updated)
    except Exception as e:
        logger.debug(f"[approval] 编辑后飞书通知失败 (不阻断): {e}")
    return {"status": "ok", "approval_id": approval_id, "edit_count": upd["edit_count"],
            "note": "审批单已更新, 分级审批流已重置从头审批"}


@router.get("/api/approvals/{approval_id}/history")
async def approval_history(approval_id: str, current_user: str = Depends(verify_token)):
    """审批单完整流程轨迹 (全员可读 · 提单透明)

    数据源: 近 7 天审计日志中该单的全部 approval_* 事件 (创建/编辑/逐级决策/终审/撤销/CITP),
    按时间正序返回; 审计日志 Redis 保留 90 天, 此处取近 7 天 (单据 TTL 24h, 足够覆盖)。
    """
    import datetime as _dt
    from core.approval import get_approval
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期")
    role, my_group = await _viewer_role_group(current_user)
    if role != "admin" and (rec.get("username") or "") != current_user:
        r0 = await get_redis()
        if await _ticket_group(r0, rec) != my_group:
            raise HTTPException(403, "该审批单不属于你所在的组别, 无权查看")
    events: list = []
    try:
        r = await get_redis()
        today = _dt.date.today()
        for i in range(7):
            key = f"{config.REDIS_PREFIX}:audit:{(today - _dt.timedelta(days=i)).isoformat()}"
            raws = await r.lrange(key, 0, -1)
            for raw in raws:
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                if not str(e.get("action") or "").startswith("approval"):
                    continue
                p = e.get("params") or {}
                if str(p.get("approval_id") or "") != approval_id:
                    continue
                events.append({
                    "ts": e.get("timestamp") or e.get("ts"),
                    "action": e.get("action"),
                    "username": e.get("username"),
                    "summary": e.get("result_summary") or "",
                })
        events.sort(key=lambda x: float(x.get("ts") or 0))
    except Exception as e:
        logger.debug(f"[approvals/history] 审计聚合失败: {e}")
    # 审计缺失时兜底: 从单据字段推导关键事件 (创建/撤销/决策)
    if not events:
        base = [("approval_created", float(rec.get("created_ts") or 0), rec.get("username"))]
        if rec.get("cancelled_ts"):
            base.append(("approval_cancelled", float(rec["cancelled_ts"]), rec.get("username")))
        if rec.get("decided_ts"):
            base.append((f"approval_{rec.get('status')}", float(rec["decided_ts"]), rec.get("approver")))
        events = [{"ts": ts, "action": act, "username": who, "summary": ""} for act, ts, who in base if ts]
    return {"approval_id": approval_id, "events": events, "count": len(events)}


@router.get("/api/approvals/stats")
async def approvals_stats(current_user: str = Depends(verify_token)):
    """审批中心统计概览 (管理员全局 / 普通用户本组+本人)

    返回: 待处理/今日新建/本周新建/维护占比/平均决策时长/超时待审清单(>24h)。
    """
    import datetime as _dt
    from core.approval import list_pending_approvals
    r = await get_redis()
    role, my_group = await _viewer_role_group(current_user)
    now = time.time()
    today0 = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week0 = today0 - 6 * 86400
    stats = {"pending": 0, "today": 0, "week": 0, "maintenance": 0, "daily": 0,
             "avg_decide_hours": 0.0, "slow_pending": [], "scope": "group" if role != "admin" else "all"}
    decide_secs = []
    n_total = 0
    for rec in list_pending_approvals():
        if role != "admin" and rec.get("username") != current_user \
                and await _ticket_group(r, rec) != my_group:
            continue
        stats["pending"] += 1
        age_h = (now - float(rec.get("created_ts") or 0)) / 3600
        if age_h > 24:
            stats["slow_pending"].append({
                "approval_id": rec.get("approval_id"), "tool_name": rec.get("tool_name"),
                "username": rec.get("username"), "site": rec.get("site"),
                "age_hours": round(age_h, 1)})
        stats["slow_pending"].sort(key=lambda x: -x["age_hours"])
    stats["slow_pending"] = stats["slow_pending"][:8]
    # 全量单据 (含已决策) — 一次性扫描统计 (365 天量级, 非轮询路径)
    try:
        for key in r.scan_iter(match=f"{config.REDIS_PREFIX}:approval:*", count=200):
            data = await r.hgetall(key)
            if not data or not data.get("created_ts"):
                continue
            if role != "admin" and data.get("username") != current_user \
                    and await _ticket_group(r, data) != my_group:
                continue
            n_total += 1
            ts = float(data["created_ts"])
            if ts >= today0:
                stats["today"] += 1
            if ts >= week0:
                stats["week"] += 1
            if (data.get("category") or "daily") == "maintenance":
                stats["maintenance"] += 1
            else:
                stats["daily"] += 1
            if data.get("decided_ts"):
                decide_secs.append(float(data["decided_ts"]) - ts)
    except Exception as e:
        logger.debug(f"[approvals/stats] 扫描失败: {e}")
    if decide_secs:
        stats["avg_decide_hours"] = round(sum(decide_secs) / len(decide_secs) / 3600, 1)
    stats["total"] = n_total
    return stats


@router.get("/api/approvals/{approval_id}")
async def get_approval_api(approval_id: str, current_user: str = Depends(verify_token)):
    """查询单个审批单状态 (按组别: 管理员全局, 本人/同组可见, 跨组 403)"""
    from core.approval import get_approval
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期 (决策记录永久保留在审计日志)")
    role, my_group = await _viewer_role_group(current_user)
    if role != "admin" and (rec.get("username") or "") != current_user:
        r = await get_redis()
        if await _ticket_group(r, rec) != my_group:
            raise HTTPException(403, "该审批单不属于你所在的组别, 无权查看")
    return rec

@router.post("/api/approvals/{approval_id}/decide")
async def decide_approval_api(approval_id: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """批准/拒绝审批单 (仅持有 approval 业务权限; admin 无业务权限时仅审计查看不可决策)

    body: {"decision": "approve" | "reject"} — 与飞书审批卡片上的指引一致
    审批只走流程: 决策仅为流程留痕 (批准=流程通过, 拒绝=流程否决),
    系统不会自动执行 destructive 操作, 实际执行由运维人员按变更流程线下完成。
    """
    body = await request.json()
    decision = (body.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision 仅支持 approve / reject")
    # SoD 四眼原则: 申请人不可审批自己发起的单 (agent 拦截单/用户申请单同规则)
    from core.approval import get_approval
    pending = get_approval(approval_id)
    if pending is None:
        raise HTTPException(404, "审批单不存在或已过期")
    # 权限点按当前审批级选择: 分级单当前待审级为 DS 配置复核 → approval.ds_review
    # (专门复核权, 无需 approval.decide); 其余 (技术/行政/无分级) → approval.decide
    _lvl_keys = [(k, (pending.get(f) or "").strip()) for k, f in
                 (("ds", "approver_ds"), ("tech", "approver_tech"), ("admin", "approver_admin"))]
    _lvl_keys = [(k, n) for k, n in _lvl_keys if n]
    _cur_is_ds = False
    if _lvl_keys and pending.get("status") == "pending":
        _done = {x for x in (pending.get("done_levels") or "").split(",") if x}
        _cur_is_ds = (next((k for k, _ in _lvl_keys if k not in _done), None) == "ds")
    await require_biz_perm(current_user, "approval.ds_review" if _cur_is_ds else "approval.decide")
    applicant = (pending.get("username") or "").strip()
    if applicant and applicant == current_user:
        raise HTTPException(403, f"四眼原则 (SoD): 不能审批自己发起的单 ({approval_id}), 请由其他持权人决策")
    # L4 范围约束: 决策人 scope (site/customer) 非空时, 单据字段须落在范围内
    eff = await effective_perms(current_user)
    allowed, why = scope_allows(eff["scopes"], pending.get("site") or "",
                                pending.get("customer") or "", pending.get("customer_gid") or "")
    if not allowed:
        raise HTTPException(403, f"范围约束: {why}")
    # v4 分级审批流: DS 配置复核 → 技术审批 → 行政审批 (配置了名单的级逐级解锁;
    # 任一级拒绝 → 终态 rejected; 全部级通过 → 终态 approved。
    # 每级名单内 (且持权 + SoD + scope) 的人可决策; 未配置分级的单据走下方指定审批人逻辑)
    _LEVELS = [("ds", "approver_ds", "DS 配置复核"),
               ("tech", "approver_tech", "技术审批"),
               ("admin", "approver_admin", "行政审批")]
    staged = [(k, f, lbl, (pending.get(f) or "").strip()) for k, f, lbl in _LEVELS]
    staged = [x for x in staged if x[3]]
    if staged:
        import re as _re0
        from core.bizperms import _load_user as _lu0
        done = {x for x in (pending.get("done_levels") or "").split(",") if x}
        cur = next((x for x in staged if x[0] not in done), None)
        if cur is None:
            raise HTTPException(400, "分级审批已全部通过, 单据应已决策 (请刷新状态)")
        lk, _lf, lbl, names = cur
        name_set = {x.strip() for x in _re0.split(r"[,，;；]", names) if x.strip()}
        du = await _lu0(current_user)
        if not ({current_user, (du.get("display_name") or "")} & name_set):
            raise HTTPException(403, f"当前待「{lbl}」级决策; 仅该级名单内的持权人可操作: {names}")
        from core.approval import decide_approval, advance_approval_level
        if decision == "reject":
            rec = decide_approval(approval_id, "reject", approver=current_user, level=lk)
            if rec is None:
                raise HTTPException(404, "审批单不存在或已过期")
            return {"status": "ok", "approval_id": approval_id, "decision": "rejected",
                    "level": lk, "approver": current_user,
                    "note": f"{lbl}级拒绝, 单据终态 rejected (审计已留痕)"}
        done.add(lk)
        nxt = next((x[2] for x in staged if x[0] not in done), "")
        if nxt:
            rec, err = advance_approval_level(approval_id, lk, current_user)
            if err:
                raise HTTPException(400, err)
            return {"status": "ok", "approval_id": approval_id, "staged": True,
                    "level": lk, "level_done": lbl, "next_level": nxt,
                    "decision": rec.get("status"), "approver": current_user,
                    "note": f"{lbl}已通过; 待「{nxt}」级决策 (单据保持待审批)"}
        rec = decide_approval(approval_id, "approve", approver=current_user, level=lk)
        if rec is None:
            raise HTTPException(404, "审批单不存在或已过期")
        return {"status": "ok", "approval_id": approval_id, "decision": "approved",
                "level": lk, "approver": current_user,
                "note": "全部分级审批通过, 单据终态 approved (审计已留痕)"}
    # 指定审批人强约束: 单据指定名单后, 仅名单内 (且持权) 的人可决策
    specified = (pending.get("approvers") or "").strip()
    if specified:
        import re as _re
        names = {x.strip() for x in _re.split(r"[,，;；]", specified) if x.strip()}
        from core.bizperms import _load_user as _lu
        decider = await _lu(current_user)
        if not ({current_user, (decider.get("display_name") or "")} & names):
            raise HTTPException(403, f"该单已指定审批人 ({specified}); 仅名单内的持权人可决策")
    from core.approval import decide_approval
    rec = decide_approval(approval_id, decision, approver=current_user)
    if rec is None:
        raise HTTPException(404, "审批单不存在或已过期")
    return {"status": "ok", "approval_id": approval_id, "decision": rec.get("status"),
            "approver": rec.get("approver")}


@router.post("/api/approvals/{approval_id}/cancel")
async def cancel_approval_api(approval_id: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """申请人撤销待审批单 (仅 pending 且本人; 撤销写审计日志永久留痕)

    临时操作回退逻辑: 误提交/计划变更时由申请人自行撤销;
    已决策单 (approved/rejected) 不可撤销 — 审计红线, 决策记录永久保留,
    如需回退已批准操作, 应新建回退申请单 (original_approval_id 关联原单)。
    body: {"reason": str}
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    reason = (body.get("reason") or "申请人主动撤销").strip()[:300]
    from core.approval import cancel_approval
    rec, err = cancel_approval(approval_id, current_user, reason)
    if err:
        raise HTTPException(400, err)
    return {"status": "ok", "approval_id": approval_id, "decision": rec.get("status"),
            "note": "审批单已撤销 (撤销记录已写入审计日志永久保留)"}

_analyzer_cache = {}

def _analyzer():
    """轻量 LLM 分析 agent (仅用 llm_chat, 不进 Registry 运行循环)"""
    if "a" not in _analyzer_cache:
        from agents.base import BaseAgent

        class _ApprovalAnalyzer(BaseAgent):
            name = "approval_analyzer"

        _analyzer_cache["a"] = _ApprovalAnalyzer()
    return _analyzer_cache["a"]


def _parse_sections(text: str) -> dict:
    """从 LLM 输出解析三段 (影响范围/风险说明/回退方案); 宽容格式 + raw 兜底

    兼容 markdown 装饰 (**影响范围**：) / 全角半角冒号 / 标记前后空白;
    全部解析失败时整段原文放入 impact_scope (前端展示后可人工编辑)。
    """
    import re as _re1
    # 规范化: 去 markdown 装饰符, 统一全角冒号
    t = _re1.sub(r"[*#>`]+", "", text or "").replace("：", ":")
    out = {"impact_scope": "", "risk_note": "", "rollback_plan": ""}
    marks = [("影响范围", "impact_scope"), ("风险说明", "risk_note"), ("回退方案", "rollback_plan")]
    for mark, key in marks:
        m = _re1.search(rf"{mark}\s*:\s*(.*?)(?=影响范围\s*:|风险说明\s*:|回退方案\s*:|$)",
                        t, _re1.S)
        if m:
            out[key] = m.group(1).strip()[:500]
    if not any(out.values()):
        out["impact_scope"] = (text or "").strip()[:500]  # 兜底: 原文入影响范围
    return out


# ═══ 申请内容自动分析 → 表单字段自动填充 ═══

@router.post("/api/approvals/autofill")
async def approvals_autofill(request: Request, current_user: str = Depends(verify_token)):
    """申请内容自动分析 → 其他表单字段自动填充 (规则提取 + LLM 分类)

    规则层 (确定性): case 号 (HD 前缀/纯数字)、设备代码 (站点码+编号+角色后缀)、IP、
    站点 (设备名前缀匹配 OPS-CORE 站点字典);
    LLM 层 (分类): 产品服务类型 + 操作类型 + 客户名称 (按分类目录约束输出)。
    body: {summary, category} → {fills: {字段: 值}, filled: [字段...], source: {...}}
    """
    import re as _re
    body = await request.json()
    summary = (body.get("summary") or "").strip()[:500]
    category = (body.get("category") or "daily").strip()
    if len(summary) < 6:
        raise HTTPException(400, "申请内容太短, 无法分析 (至少 6 字)")
    from core.approval import APPROVAL_OP_CATALOG, APPROVAL_APPLY_TYPES
    fills: dict = {}
    src: dict = {}

    # ── 规则层: case 号 / 设备代码 / IP / 站点 ──
    m = _re.search(r"\b(?:HD\d{6,}|\d{7,8})\b", summary)
    if m:
        fills["case_no"] = m.group(0)
        src["case_no"] = "规则提取"
    devs = [d for d in _re.findall(r"\b([A-Z]{2,8}\d{3,5}[A-Z]{0,4})\b", summary)
            if not _re.fullmatch(r"(?:HD|GID)\d+", d)][:3]
    ips = _re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", summary)[:3]
    if devs or ips:
        fills["target_host"] = ", ".join(devs + ips)
        src["target_host"] = "规则提取"
    # 站点: 设备名前缀 (region+city+mansion, 8 位) 匹配站点字典; 或正文显式站点码
    r = await get_redis()
    site_codes: set = set()
    try:
        opscore_sites = await _opscore_dict_sync("site_info", {"page_size": 200}, ttl=600)
        if opscore_sites["ok"]:
            for x in (opscore_sites["data"].get("results") or []):
                c = str(x.get("city") or "").strip()
                mn = str(x.get("mansion") or "").strip()
                if c and mn and mn.isalnum() and 2 <= len(mn) <= 4:
                    site_codes.add(f"{c}{mn}")
    except Exception:
        pass
    for d in devs:
        # 设备名 = [区域2位] + 站点码(city+mansion) + 编号 + 角色后缀;
        # 命中 6 位码 (HKGAAA) 时升级为带区域前缀的完整码 (HKHKGAAA), 与站点字典一致
        for cand in (d[:8], d[2:8], d[:6], d[2:5], d[:3]):
            if cand and cand in site_codes:
                fills["site"] = (d[:2] + cand) if (len(cand) == 6 and cand == d[2:8]) else cand
                src["site"] = "设备名前缀匹配"
                break
        if fills.get("site"):
            break
    if not fills.get("site"):
        m2 = _re.search(r"\b([A-Z]{2}[A-Z]{3}[A-Z]{2,4})\b", summary)
        if m2 and m2.group(1) in site_codes:
            fills["site"] = m2.group(1)
            src["site"] = "正文站点码"

    # ── 规则兜底: 关键词 → 操作类型 (LLM 偶发输出 "-" 时保证可用性) ──
    _KW_OPS = [
        (r"换盘|换硬盘|硬盘更换|磁盘更换|HDD", "hw_disk_replace"),
        (r"换内存|内存条|MEM更换|内存更换", "hw_memory_replace"),
        (r"网卡", "hw_nic_replace"),
        (r"光模块|SFP", "hw_sfp_replace"),
        (r"BIOS电池|主板电池|CR2032", "hw_bios_battery_replace"),
        (r"主板", "hw_motherboard_replace"),
        (r"电源|PSU", "hw_psu_replace"),
        (r"风扇", "hw_fan_replace"),
        (r"拔插|重新插拔", "hw_reseat"),
        (r"移除|删除.{0,6}[Vv][Mm]|下线.{0,6}[Vv][Mm]", "vm_remove"),
        (r"重启", "vm_power_reboot"),
        (r"关机|断电", "vm_shutdown"),
        (r"迁移|vMotion", "vm_migrate"),
        (r"快照回滚|回滚", "vm_snapshot_revert"),
        (r"快照", "vm_snapshot_create"),
        (r"磁盘扩容|扩容", "vm_disk_expand"),
        (r"备份恢复|恢复备份", "backup_restore"),
        (r"DNS|解析记录", "dns_record_change"),
    ]
    _kw_hit = next((op for pat, op in _KW_OPS if _re.search(pat, summary)), "")

    # ── LLM 层: 产品类型 + 服务类型 + 操作类型 + 客户名称 (三级目录约束) ──
    cat_groups = APPROVAL_OP_CATALOG.get(category) or APPROVAL_OP_CATALOG["daily"]
    catalog_desc = "; ".join(
        f"{p}/{sv}: {'/'.join(APPROVAL_APPLY_TYPES.get(o, o) for o in ops)}"
        for p, services in cat_groups for sv, ops in services if ops)
    prompt = (
        "根据运维变更申请内容, 从受限目录中选择并抽取字段。\n"
        f"申请内容: {summary}\n\n"
        f"产品类型/服务类型/操作类型 三级目录 (只能从中选择):\n{catalog_desc}\n\n"
        "严格按以下格式输出 (无把握的项输出 -, 不要编造):\n"
        "产品类型: <目录之一, 如 SCC/VONE/SF/硬件/平台服务>\n"
        "服务类型: <该产品下的服务, 如 主机/存储/备份>\n"
        "操作类型: <目录之一的中文或英文键>\n"
        "客户名称: <申请内容中的客户名, 无则 ->"
    )
    try:
        import asyncio as _aio
        text = await _aio.wait_for(
            _analyzer().llm_chat(prompt, temperature=0.1,
                                 system="你是运维变更单分类助手。只从给定目录中选择, 用简洁格式回答, 不确定就输出 -。"),
            timeout=60)
        t = _re.sub(r"[*#>`]+", "", text or "")
        mp = _re.search(r"产品类型\s*:\s*(.+)", t)
        mm = _re.search(r"服务类型\s*:\s*(.+)", t)
        mo = _re.search(r"操作类型\s*:\s*(.+)", t)
        mc = _re.search(r"客户名称\s*:\s*(.+)", t)
        label_to_op = {APPROVAL_APPLY_TYPES.get(o, o): o
                       for p_, services in cat_groups for sv_, ops_ in services for o in ops_}
        for p_, services in cat_groups:
            for sv_, ops_ in services:
                for o in ops_:
                    label_to_op.setdefault(o, o)
        hit = (mo and (label_to_op.get(mo.group(1).strip())
                       or label_to_op.get(mo.group(1).strip().replace(" ", "")))) or None
        prod = (mp and mp.group(1).strip()) or ""
        svc = (mm and mm.group(1).strip()) or ""
        if prod and prod in {p_ for p_, _ in cat_groups}:
            fills["product"] = prod
            src["product"] = "AI 分类"
            if svc and svc in {sv_ for p_, services in cat_groups if p_ == prod
                               for sv_, _ in services}:
                fills["service"] = svc
                fills["service_type"] = f"{prod}/{svc}"
                src["service_type"] = "AI 分类"
        if hit:
            fills["tool_name"] = hit
            src["tool_name"] = "AI 分类"
            if not fills.get("service_type"):
                _done2 = False
                for p_, services in cat_groups:
                    if _done2:
                        break
                    for sv_, ops_ in services:
                        if hit in ops_:
                            fills["product"] = fills.get("product") or p_
                            fills["service"] = sv_
                            fills["service_type"] = f"{fills['product']}/{sv_}"
                            _done2 = True
                            break
        if mc:
            cust = mc.group(1).strip()
            if cust and cust not in ("-", "无", "None") and 2 <= len(cust) <= 60:
                fills["customer"] = cust
                src["customer"] = "AI 抽取"
    except Exception as e:
        logger.debug(f"[autofill] LLM 分类失败 ({current_user}): {e}")
    # LLM 未给出操作类型 → 关键词兜底 (并推导三级归属)
    if not fills.get("tool_name") and _kw_hit:
        fills["tool_name"] = _kw_hit
        src["tool_name"] = "关键词匹配"
        _done = False
        for p_, services in cat_groups:
            if _done:
                break
            for sv_, ops_ in services:
                if _kw_hit in ops_:
                    fills["product"] = fills.get("product") or p_
                    fills["service"] = sv_
                    fills["service_type"] = f"{fills['product']}/{sv_}"
                    src["service_type"] = "关键词推导"
                    _done = True
                    break

    return {"fills": fills, "filled": list(fills.keys()), "source": src,
            "note": "规则提取 + AI 分类; 前端仅填充空字段, 已填字段不覆盖"}


@router.post("/api/approvals/analyze")
async def approvals_analyze(request: Request, current_user: str = Depends(verify_token)):
    """LLM 辅助影响分析: 依据操作类型/事由/站点/客户生成 影响范围/风险/回退建议

    结果仅供申请人参考 (前端回填表单后可人工编辑再提交), 不作为决策依据。
    """
    import asyncio as _aio1
    import json as _json1
    body = await request.json()
    tool_name = (body.get("tool_name") or "").strip()[:100]
    summary = (body.get("summary") or "").strip()[:500]
    if not tool_name and not summary:
        raise HTTPException(400, "请先填写操作类型或申请事由再分析")
    prompt = (
        "你是资深基础设施运维变更顾问。根据以下变更申请信息做影响分析。\n"
        f"操作类型: {tool_name or '-'}\n"
        f"申请事由: {summary or '-'}\n"
        f"站点: {(body.get('site') or '-').strip()[:60]}\n"
        f"客户: {(body.get('customer') or '-').strip()[:120]}"
        f" (GID: {(body.get('customer_gid') or '-').strip()[:40]})\n"
        f"Case 号: {(body.get('case_no') or '-').strip()[:60]}\n"
        f"服务类型: {(body.get('service_type') or '-').strip()[:60]}\n"
        f"参数: {_json1.dumps(body.get('params') or {}, ensure_ascii=False)[:400]}\n\n"
        "严格按以下三段输出, 每段以指定标记开头, 中文简洁专业, 回退方案给可执行步骤:\n"
        "影响范围: <受影响的系统/服务/客户业务, 2-3 句>\n"
        "风险说明: <主要风险与等级 (低/中/高), 2-3 句>\n"
        "回退方案: <2-4 条可执行回退步骤>"
    )
    try:
        text = await _aio1.wait_for(
            _analyzer().llm_chat(prompt, temperature=0.2,
                                 system="你是资深基础设施运维变更顾问, 擅长评估云主机/备份/存储/网络等变更的影响范围、风险与回退方案。用简洁专业的中文回答。"),
            timeout=90)
        out = _parse_sections(text or "")
        if not (out["impact_scope"] or out["risk_note"] or out["rollback_plan"]):
            return {"available": False, "error": "分析结果解析失败, 请人工填写"}
        out["available"] = True
        return out
    except Exception as e:
        logger.warning(f"[approvals/analyze] LLM 分析失败 ({current_user}): {e}")
        return {"available": False, "error": f"分析服务暂不可用: {e}"}


# ═══ CITP 自动生成 (维护审批: 按故障类型模板 + 资源管理系统设备匹配) ═══
# 格式对齐真实 CITP 附件: Plan (Reference/时间窗/Purpose + Change Management 步骤表)
# + Check_list (Step/Host/IP/Sub Step/Test item/Expected result); 详见 core/citp_templates.py

# 表单参数 → 设备目标键 (设备匹配候选来源, 与 target_host 字段合并去重)
_CITP_PARAM_KEYS = ("vm_name", "name", "host", "hosts", "instance", "volume",
                    "job_name", "service", "target", "cluster", "vip")


def _citp_targets(target_host: str, params: dict) -> list:
    """汇总设备目标: 目标主机字段 + 参数键值 → 去重保序 (逗号/分号/空白分隔, 上限 8)"""
    raw = [t.strip() for t in str(target_host or "").replace(";", ",").replace("，", ",").split(",")
           if t.strip()]
    for k in _CITP_PARAM_KEYS:
        v = str((params or {}).get(k) or "").strip()
        if v:
            raw += [t.strip() for t in v.replace(";", ",").replace("，", ",").split(",") if t.strip()]
    return list(dict.fromkeys(raw))[:8]


async def _cmdb_enrich(host: str) -> dict:
    """CMDB (信息管理系统) 补充字段: SN/机柜位置等; 字段名防御式提取, 平台不可用返回空"""
    out = {}
    try:
        from agents.opscore_api import query_api
        d = await query_api("cmdb_info", {"search": host, "page_size": 3})
        rec = None
        if isinstance(d, dict) and "error" not in d:
            for x in (d.get("results") or []):
                name = str(x.get("hostname") or x.get("name") or x.get("host") or "").lower()
                if name == host.lower():
                    rec = x
                    break
            rec = rec or (d.get("results") or [None])[0]
        if rec:
            getv = lambda *keys: next((str(rec[k]).strip() for k in keys if rec.get(k) and str(rec.get(k)).strip() not in ("", "None")), "")
            out["sn"] = getv("sn", "serial", "serial_number", "sn_number", "SN")
            out["rack"] = getv("rack", "cabinet", "rack_info", "location", "position")
            out["model"] = getv("model", "hardware_model")
    except Exception as e:
        logger.debug(f"[citp] CMDB 补充查询失败 ({host}): {e}")
    return {k: v for k, v in out.items() if v}


async def _citp_match_device(r, target: str) -> dict:
    """单个设备匹配: OPS-CORE-MGT 资源管理系统 → 统一 device dict (含平台/SN/机柜补充)"""
    ku = (target or "").strip()
    if not ku:
        return {"target": ku, "matched": False, "reason": "未填写设备"}
    from agents.nms_cmdb import _nms_query_cmdb_device, _nms_mgt_list_cached
    try:
        dev = await _nms_query_cmdb_device(r, ku)
    except Exception as e:
        logger.warning(f"[citp] 设备匹配异常 ({ku}): {e}")
        return {"target": ku, "matched": False, "reason": "资源管理系统查询异常"}
    if dev and dev.get("status") == "ok":
        device = {"target": ku, "matched": True, **{k: dev.get(k, "")
                  for k in ("name", "hostname", "ip", "sn", "vendor", "model", "firmware", "site")}}
        # 存储阵列的 site 常是阵列名 (system_name): 取设备名前 8 位为站点码 (如 CNGUZDDD)
        if device.get("site") == (device.get("name") or "") and len(device["site"]) >= 8:
            device["site"] = device["site"][:8].upper()
        from core.citp_templates import detect_platform
        device["platform"] = detect_platform(device)
        # 主机 (esxi) 带出 vCenter 实时状态: 维护模式/电源/连接 (CITP 提示用)
        if device["platform"] == "esxi" and device.get("name"):
            try:
                _vh = await _nms_mgt_list_cached(r, f"{config.REDIS_PREFIX}:nms:mgt_vhosts",
                                                 "/api/vcenter/hosts/")
                _short = device["name"].upper()
                for h in (_vh or []):
                    if str(h.get("name") or "").split(".")[0].upper() == _short:
                        device["maintenance_mode"] = str(h.get("maintenance_mode") or "").lower() == "true"
                        device["power_state"] = h.get("power_state") or ""
                        device["connection_state"] = h.get("connection_state") or ""
                        break
            except Exception as e:
                logger.debug(f"[citp] 主机维护状态读取失败: {e}")
        # SN/机柜缺失时 CMDB 补充 (信息管理系统)
        if not device.get("sn") or not device.get("rack"):
            extra = await _cmdb_enrich(device.get("name") or device.get("hostname") or ku)
            for k, v in extra.items():
                if not device.get(k):
                    device[k] = v
        return device
    if dev and dev.get("status") == "not_found":
        return {"target": ku, "matched": False,
                "reason": "资源管理系统未匹配到该设备, 请确认名称/IP/SN 后重试"}
    return {"target": ku, "matched": False, "reason": "资源管理系统暂不可达, 无法自动匹配"}


async def _citp_device_customers(device: dict) -> tuple:
    """受影响客户列表: ESXi 主机 → 其上 VM 归属客户; 存储 → 卷映射客户"""
    if not device or not device.get("matched"):
        return [], 0
    try:
        from agents.opscore_api import expand_host_code, query_vms_on_esxi_host
        code = expand_host_code(device.get("name") or device.get("target") or "")
        if code:
            info = await query_vms_on_esxi_host(code)
            if info and info.get("vms"):
                custs: dict = {}
                for vm in info["vms"]:
                    c = str(vm.get("customer_name") or vm.get("customer") or "").strip()
                    gid = str(vm.get("customer_gid") or "").strip()
                    if c or gid:
                        key = f"{c}|{gid}"
                        ent = custs.setdefault(key, {"name": c or gid, "gid": gid, "vms": 0})
                        ent["vms"] += 1
                return sorted(custs.values(), key=lambda x: -x["vms"]), len(info["vms"])
    except Exception as e:
        logger.debug(f"[citp] 主机客户列表查询失败: {e}")
    # 存储阵列 → 卷映射客户
    try:
        ip = device.get("ip")
        if ip:
            import redis.asyncio as _aioredis
            r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
            from agents.nms_cmdb import _nms_mcp_customer_impact
            imp = await _nms_mcp_customer_impact(r, [ip])
            custs = [{"name": k, "gid": "", "vms": v}
                     for k, v in (imp.get("customers") or {}).items()]
            return sorted(custs, key=lambda x: -x["vms"]), imp.get("vms", 0)
    except Exception as e:
        logger.debug(f"[citp] 存储客户影响查询失败: {e}")
    return [], 0


@router.get("/api/approvals/citp/devices")
async def approvals_citp_devices(q: str = "", current_user: str = Depends(verify_token)):
    """设备候选搜索 (CITP 目标设备下拉): OPS-CORE-MGT 宿主机 + 三家存储阵列

    q ≥ 2 字符; 短名(去域大写)/IP/SN 包含匹配, 返回前 10 条候选:
    [{name, kind: host|array, vendor, model, ip}] — 选中后前端自动走精确匹配带出详情。
    """
    import redis.asyncio as _aioredis
    ku = (q or "").strip()
    if len(ku) < 2:
        return {"candidates": [], "note": "输入至少 2 个字符"}
    r2 = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
    from agents.nms_cmdb import _nms_mgt_list_cached
    cands: list = []

    def _short(name) -> str:
        return str(name or "").strip().split(".")[0].upper()

    try:
        hosts = await _nms_mgt_list_cached(r2, f"{config.REDIS_PREFIX}:nms:mgt_vhosts",
                                           "/api/vcenter/hosts/")
        for h in (hosts or []):
            name_s = _short(h.get("name"))
            ip = str(h.get("ip_address") or "")
            if ku.upper() in name_s or (ip and ku == ip):
                cands.append({"name": name_s, "kind": "host",
                              "vendor": h.get("manufacturer") or "VMware ESXi",
                              "model": h.get("model") or "", "ip": ip})
            if len(cands) >= 10:
                break
        for ck, path, vendor in (
                ("mgt_compellent", "/api/storage/compellent-arrays/", "Dell Compellent"),
                ("mgt_netapp", "/api/storage/netapp-arrays/", "NetApp"),
                ("mgt_nimble", "/api/storage/nimble/", "HPE Nimble")):
            if len(cands) >= 10:
                break
            arrs = await _nms_mgt_list_cached(r2, f"{config.REDIS_PREFIX}:nms:{ck}", path)
            for a in (arrs or []):
                blob = " ".join(str(a.get(k) or "").upper() for k in ("name", "ip", "serial_number"))
                if ku.upper() in blob:
                    cands.append({"name": _short(a.get("name")), "kind": "array",
                                  "vendor": vendor, "model": a.get("model") or "",
                                  "ip": str(a.get("ip") or "")})
                if len(cands) >= 10:
                    break
    except Exception as e:
        logger.debug(f"[citp/devices] 候选搜索失败: {e}")
        return {"candidates": [], "note": "资源管理系统暂不可达"}
    return {"candidates": cands[:10], "count": len(cands)}


@router.post("/api/approvals/citp/match")
async def approvals_citp_match(request: Request, current_user: str = Depends(verify_token)):
    """设备手动选择 → 资源管理系统 (OPS-CORE-MGT) 匹配: SN/IP/型号/位置 + 受影响客户列表"""
    body = await request.json()
    target = (body.get("target") or "").strip()[:200]
    if not target:
        raise HTTPException(400, "请填写设备 (主机名/IP/SN)")
    r = await get_redis()
    device = await _citp_match_device(r, target)
    customers, vms_count = await _citp_device_customers(device)
    return {"device": device, "customers": customers[:30], "vms_count": vms_count}


@router.post("/api/approvals/citp")
async def approvals_citp(request: Request, current_user: str = Depends(verify_token)):
    """CITP 自动生成 (维护审批, 按真实 CITP 附件格式):

    前端手动选择: 故障类型 (模板库) + 目标设备 + 时间窗 → 资源管理系统匹配设备
    SN/IP/型号/机柜与受影响客户列表 → 模板占位符填充生成 Plan/Check_list。
    body: {fault_type, target, window, tz, case_no, site, customer, summary,
           tool_name, params, extra_targets, approved_by, target_host}
    """
    import re as _re
    from core.citp_templates import CITP_FAULT_TYPES, build_citp
    body = await request.json()
    fault_type = (body.get("fault_type") or "").strip()
    if not fault_type:
        raise HTTPException(400, "请选择故障类型 (fault_type 必填; Plan/Check_list 按该模板填充)")
    if fault_type not in CITP_FAULT_TYPES:
        raise HTTPException(400, f"fault_type 仅支持: {'/'.join(CITP_FAULT_TYPES)}")
    target = (body.get("target") or "").strip()[:200]
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    # 设备目标: 主选设备 (前端手选) + 备选 (target_host/参数键合并, 最多再取 4 台)
    extras_src = [t for t in _citp_targets(body.get("target_host") or "", params)
                  if t.lower() != target.lower()]
    primary = target or (extras_src[0] if extras_src else "")
    if not target and extras_src:
        extras_src = extras_src[1:]
    extras_src = extras_src[:4]
    window = (body.get("window") or "").strip()[:120]
    tz = (body.get("tz") or "UTC+8").strip()[:16]
    if window:
        window = f"{window} {tz}".strip()
    r = await get_redis()

    # 1) 设备匹配 (资源管理系统) + 客户列表
    device = await _citp_match_device(r, primary)
    mgt_ok = device.get("matched") or device.get("reason") != "资源管理系统暂不可达, 无法自动匹配"
    extras = []
    for t in extras_src:
        if t.lower() == (device.get("target") or "").lower():
            continue
        extras.append(await _citp_match_device(r, t))
    customers, vms_count = await _citp_device_customers(device)

    # 2) 关联活动告警 (设备/站点/客户维度, 求匹配数)
    alerts_matched = 0
    try:
        host_blob = ",".join(d["target"] for d in [device] + extras if d.get("target"))
        alerts = await _match_active_alerts(r, (body.get("site") or "").strip(),
                                            (body.get("customer") or "").strip(),
                                            (body.get("case_no") or "").strip(), host_blob)
        alerts_matched = alerts.get("matched", 0)
    except Exception as e:
        logger.debug(f"[citp] 告警匹配失败: {e}")

    # 3) 模板填充生成 (时间窗/设备为手选值; 未匹配字段以「现场核对」占位)
    citp = build_citp(
        fault_type=fault_type, device=device, window=window,
        case_no=(body.get("case_no") or "").strip(),
        extra_devices=extras,
        approved_by=(body.get("approved_by") or "").strip()[:60],
        customers=customers, vms_count=vms_count,
        alerts_matched=alerts_matched, mgt_available=bool(mgt_ok),
        vendor=(body.get("vendor") or "Microware").strip()[:40] or "Microware",
        platform_override=(body.get("platform") or "").strip(),
    )
    matched_devs = sum(1 for d in citp["devices"] if d.get("matched"))
    citp["matched_count"] = matched_devs
    citp["total_targets"] = len(citp["devices"])
    citp["available"] = True
    # 主机维护模式检查: 停机换件类 (plan 含维护模式步骤) 而主机未进维护模式 → 显著警告
    citp["maintenance_mode"] = device.get("maintenance_mode")
    _needs_maint = any("维护模式" in (p.get("desc") or "") + (p.get("command") or "")
                       for p in citp["plan"])
    if _needs_maint and device.get("platform") == "esxi" and device.get("matched") \
            and device.get("maintenance_mode") is False:
        citp["maintenance_ok"] = False
        citp["note"] = (f"⚠ 主机 {device.get('name')} 尚未进入维护模式, 请先在 vCenter 置入维护模式"
                        f" (迁走/暂停该主机 VM) 再实施维护; " ) + (citp.get("note") or "")
    else:
        citp["maintenance_ok"] = True
    citp["note"] = ("" if mgt_ok else "资源管理系统暂不可达, 设备信息不完整; ") + (citp.get("note") or "")
    if not device.get("matched"):
        citp["note"] += f"主设备「{device.get('target')}」未匹配, 请核对后重试或提交后人工补足; "
    citp["note"] += "内容按故障类型模板生成, 提交前可人工调整导出件"
    # 审计: CITP 生成 (故障类型/设备匹配结果/客户影响 留痕)
    try:
        from core.approval import _audit_async
        _audit_async({"agent": "manual", "username": current_user,
                      "tool_name": f"CITP:{citp['reference_number']}",
                      "approval_id": citp.get("citp_no") or citp.get("reference_number"),
                      "params": {"fault_type": fault_type, "target": target,
                                 "matched": matched_devs, "total": citp["total_targets"],
                                 "vms": vms_count, "window": window,
                                 "customers": len(customers)},
                      "status": "generated"},
                     action="approval_citp_generated",
                     summary=(f"CITP generated ({fault_type}; 设备匹配 {matched_devs}/"
                              f"{citp['total_targets']}; 客户 {len(customers)} 家; VM {vms_count} 台)"))
    except Exception as e:
        logger.debug(f"[citp] 生成审计写入失败: {e}")
    return citp


# ═══ 维护验收: 主机网卡测试 (OPS-CORE-MGT test-nic API) → 结果写入 CITP ═══

def _fmt_nic_test(data: dict) -> dict:
    """test-nic 原始结果 → 紧凑验收记录 (存入审批单 citp.acceptance, 控制体积)"""
    nics = []
    for n in (data.get("nics") or [])[:12]:
        issues = [f"{it.get('item')}: {it.get('detail')}"
                  for it in (n.get("items") or []) if it.get("status") != "pass"][:3]
        nics.append({"name": n.get("name"), "vswitch": n.get("vswitch"),
                     "link_up": bool(n.get("link_up")), "summary": n.get("summary"),
                     "issues": issues})
    overall = str(data.get("overall_status") or "").lower()
    return {
        "type": "nic_test",
        "tested_at": data.get("tested_at") or "",
        "overall_status": "green" if overall == "green" else ("red" if overall == "red" else "warn"),
        "vcenter": data.get("vcenter_ip") or "",
        "cluster": data.get("cluster") or "",
        "nics": nics,
        "pass_n": sum(1 for n in nics if n["summary"] == "pass"),
        "warn_n": sum(1 for n in nics if n["summary"] == "warn"),
        "fail_n": sum(1 for n in nics if n["summary"] in ("fail", "error", "red")),
    }


@router.post("/api/approvals/{approval_id}/acceptance")
async def approval_acceptance(approval_id: str, current_user: str = Depends(verify_token)):
    """维护验收: 执行主机网卡测试并将结果写入该单 CITP 验收记录

    流程: 定位单据 CITP 设备 (ESXi 主机) → OPS-CORE-MGT id → POST test-nic 触发
    (权限不足降级 GET 最新结果) → 紧凑化写入 citp.acceptance → 审计留痕
    (approval_acceptance); xlsx 导出自动包含「验收结果」页。
    """
    from core.approval import get_approval
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期")
    # 组权限: 管理员/本人/同组可执行验收 (跨组禁止操作他人单据)
    _role, _grp = await _viewer_role_group(current_user)
    if _role != "admin" and (rec.get("username") or "") != current_user:
        _r0 = await get_redis()
        if await _ticket_group(_r0, rec) != _grp:
            raise HTTPException(403, "该审批单不属于你所在的组别, 无权验收")
    if (rec.get("category") or "daily") != "maintenance":
        raise HTTPException(400, "仅维护审批支持网卡验收测试")
    try:
        citp = json.loads(rec.get("citp") or "{}")
    except Exception:
        citp = {}
    dev = citp.get("device") or {}
    if not dev.get("matched") or dev.get("platform") != "esxi":
        raise HTTPException(400, "该单 CITP 设备未匹配或非 ESXi 主机, 无法执行网卡测试")
    host_name = str(dev.get("name") or "").strip().upper()
    if not host_name:
        raise HTTPException(400, "CITP 设备名为空")

    # 主机 → OPS-CORE-MGT 数字 id (短名匹配)
    from core.approval import _audit_async
    r = await get_redis()
    from agents.nms_cmdb import _nms_mgt_list_cached
    vhosts = await _nms_mgt_list_cached(r, f"{config.REDIS_PREFIX}:nms:mgt_vhosts",
                                        "/api/vcenter/hosts/")
    mgt_id = next((str(h.get("id")) for h in (vhosts or [])
                  if str(h.get("name") or "").split(".")[0].upper() == host_name), "")
    if not mgt_id:
        raise HTTPException(404, f"资源管理系统未找到主机 {host_name}")

    # 触发测试 (POST 无权限时降级 GET 最新结果)
    from agents.opscore_api import get_mgt_client
    data, mode = None, ""
    try:
        data = await get_mgt_client().request("POST", f"/api/vcenter/hosts/{mgt_id}/test-nic/")
        mode = "post"
    except Exception as e:
        logger.info(f"[acceptance] test-nic POST 降级 GET ({e})")
    if not isinstance(data, dict) or "error" in data or not data.get("nics"):
        try:
            data = await get_mgt_client().request("GET", f"/api/vcenter/hosts/{mgt_id}/test-nic/")
            mode = mode or "get"
        except Exception as e:
            raise HTTPException(502, f"网卡测试服务不可达: {e}")
    if not isinstance(data, dict) or not data.get("nics"):
        raise HTTPException(502, "网卡测试无结果返回")
    if str(data.get("error") or ""):
        raise HTTPException(502, f"网卡测试失败: {data.get('error')}")

    acc = _fmt_nic_test(data)
    acc.update({"by": current_user, "ts": time.time(), "mode": mode})
    citp["acceptance"] = acc
    # 分级裁剪, 保证存入的永远是合法完整 JSON (绝不硬截断字符串)
    _LIMIT = 11800
    citp_str = json.dumps(citp, ensure_ascii=False)
    if len(citp_str) > _LIMIT:
        for n in acc["nics"]:
            n["issues"] = [i[:80] for i in n["issues"][:1]]
        citp_str = json.dumps(citp, ensure_ascii=False)
    if len(citp_str) > _LIMIT:
        for n in acc["nics"]:
            n["issues"] = []
        citp_str = json.dumps(citp, ensure_ascii=False)
    if len(citp_str) > _LIMIT:
        acc["nics"] = [{"name": n["name"], "summary": n["summary"]} for n in acc["nics"]][:16]
        citp_str = json.dumps(citp, ensure_ascii=False)
    await r.hset(f"{config.REDIS_PREFIX}:approval:{approval_id}", mapping={"citp": citp_str})
    _audit_async({**rec, "citp": citp_str}, action="approval_acceptance",
                 summary=(f"acceptance (网卡验收测试: {host_name}; 结论 {acc['overall_status']}; "
                          f"通过 {acc['pass_n']}/告警 {acc['warn_n']}/失败 {acc['fail_n']}; "
                          f"{acc['tested_at']})"))
    return {"status": "ok", "approval_id": approval_id, "acceptance": acc}


@router.get("/api/approvals/{approval_id}/citp.xlsx")
async def approval_citp_export(approval_id: str, current_user: str = Depends(verify_token)):
    """导出 CITP xlsx (严格复刻真实 CITP 模板版式: Plan/Check_list/Ping Snashot/Host List/Rack Layout)

    版式实现在 core/citp_xlsx.py (基准: 01-CITP-20240205-CEPH-SH-8066-4723516.xlsx);
    含网卡验收结果 (citp.acceptance) 时附加「验收结果」页。
    """
    from core.approval import get_approval
    rec = get_approval(approval_id)
    if not rec:
        raise HTTPException(404, "审批单不存在或已过期")
    # 组权限: 管理员/本人/同组可导出 (CITP 含客户/设备/验收数据, 跨组禁止)
    _role, _grp = await _viewer_role_group(current_user)
    if _role != "admin" and (rec.get("username") or "") != current_user:
        _r0 = await get_redis()
        if await _ticket_group(_r0, rec) != _grp:
            raise HTTPException(403, "该审批单不属于你所在的组别, 无权导出")
    try:
        citp = json.loads(rec.get("citp") or "{}")
    except Exception:
        citp = {}
    if not citp.get("plan"):
        raise HTTPException(404, "该审批单无 CITP 快照 (仅维护审批且已生成时提供)")

    import io
    from core.citp_xlsx import build_citp_workbook
    from fastapi.responses import StreamingResponse

    wb = build_citp_workbook(citp, rec)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"{(citp.get('reference_number') or citp.get('citp_no') or approval_id)}.xlsx"
    # 审计: CITP 导出 (数据外发动作留痕)
    try:
        from core.approval import _audit_async
        _audit_async({"agent": "manual", "username": current_user,
                      "tool_name": f"CITP-EXPORT:{fname}",
                      "approval_id": approval_id, "status": "exported"},
                     action="approval_citp_exported",
                     summary=f"CITP xlsx exported ({fname}; 单据 {approval_id})")
    except Exception as e:
        logger.debug(f"[citp] 导出审计写入失败: {e}")
    import urllib.parse as _up
    return StreamingResponse(
        bio, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_up.quote(fname)}"})


# ═══ v4.0 Phase 2.1: 路由评测摘要 (供 /agents 页路由准确率卡片) ═══

@router.get("/api/routing/eval-summary")
async def routing_eval_summary(current_user: str = Depends(verify_token)):
    """返回最近一次路由评测结果 (keyword/llm 两模式准确率, 读取 tests/routing/*.json 报告)"""
    from pathlib import Path as _P
    out = {"available": False}
    # v4.0 Phase 6 拆分后: api/routers/admin.py → 项目根需上溯 3 级
    base = _P(__file__).resolve().parent.parent.parent / "tests" / "routing"
    for mode, fname in (("keyword", "keyword_report.json"), ("llm", "llm_report.json")):
        f = base / fname
        if not f.exists():
            continue
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
            total, passed = rep.get("total", 0), rep.get("passed", 0)
            out[mode] = {
                "total": total, "passed": passed, "failed": rep.get("failed", 0),
                "accuracy": round(passed / total, 4) if total else None,
            }
            out["available"] = True
        except Exception:
            continue
    return out

# ═══ 记忆面板 (v4.0 Phase 3.2, 用户可控, 同 ChatGPT saved memories) ═══

@router.get("/api/admin/memory")
async def admin_memory_panel(current_user: str = Depends(verify_token)):
    """wiki 五类页面计数 + 最近合成条目 + 最近显式记忆 (仅管理员)"""
    await require_admin(current_user)
    from core.memory.synthesizer import memory_panel_data
    return await asyncio.to_thread(memory_panel_data)

@router.delete("/api/admin/memory/{page_type}/{page_id}")
async def admin_memory_delete(page_type: str, page_id: str,
                              current_user: str = Depends(verify_token)):
    """删除 wiki 记忆页面 (用户可控); 删除动作写 wiki 审计 .log 永久留痕"""
    await require_admin(current_user)
    from core.memory.synthesizer import delete_memory_page
    ok = await asyncio.to_thread(delete_memory_page, page_type, page_id, current_user)
    if not ok:
        raise HTTPException(404, f"记忆页面不存在: {page_type}/{page_id}")
    return {"deleted": True, "page_type": page_type, "page_id": page_id}

@router.post("/api/admin/memory/synthesize")
async def admin_memory_synthesize(current_user: str = Depends(verify_token)):
    """手动触发一次记忆合成 (验证/演示用; 幂等: 同一自然日不重复全量合成)"""
    await require_admin(current_user)
    from core.memory import synthesizer
    # 手动触发绕过当日幂等 (管理员显式行为), 重置记账后执行
    def _run():
        synthesizer._save_state({})
        return synthesizer.synthesize_recent_sessions()
    stats = await asyncio.to_thread(_run)
    return {"triggered_by": current_user, "stats": stats}

# ═══ 组权限配置 ═══

@router.get("/api/admin/groups")
async def admin_list_groups(current_user: str = Depends(verify_token)):
    """列出全部用户组（内置 + 自定义）及成员数 (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    groups = await _get_all_groups()
    custom_raw = await r.get(config.KEY_CUSTOM_GROUPS)
    custom = json.loads(custom_raw) if custom_raw else []
    # 成员数统计
    counts = {g: 0 for g in groups}
    users_raw = await r.hgetall(config.KEY_USERS)
    for _, data in users_raw.items():
        u = json.loads(data)
        g = u.get("group", "")
        if g in counts:
            counts[g] += 1
    return {"groups": groups, "builtin": config.USER_GROUPS, "custom": custom, "member_counts": counts}

@router.post("/api/admin/groups")
async def admin_create_group(request: Request, current_user: str = Depends(verify_token)):
    """新建自定义用户组 (仅管理员)"""
    await require_admin(current_user)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "组名不能为空")
    if len(name) > 20:
        raise HTTPException(400, "组名最长 20 字符")
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]+", name):
        raise HTTPException(400, "组名仅支持中英文、数字、下划线和短横线")
    existing = await _get_all_groups()
    if name in existing:
        raise HTTPException(409, f"组 {name} 已存在")
    r = await get_redis()
    custom_raw = await r.get(config.KEY_CUSTOM_GROUPS)
    custom = json.loads(custom_raw) if custom_raw else []
    custom.append(name)
    await r.set(config.KEY_CUSTOM_GROUPS, json.dumps(custom, ensure_ascii=False))
    await _emit_daily_log("system", f"管理员 {current_user} 创建用户组: {name}", level="info", category="admin", username=current_user)
    return {"status": "ok", "name": name, "groups": await _get_all_groups()}

@router.delete("/api/admin/groups/{name}")
async def admin_delete_group(name: str, current_user: str = Depends(verify_token)):
    """删除自定义用户组 (仅管理员，内置组不可删)：
    同时清理该组的权限配置，并将组内成员置为无组别"""
    await require_admin(current_user)
    if name in config.USER_GROUPS:
        raise HTTPException(400, f"{name} 是内置组，不可删除")
    r = await get_redis()
    custom_raw = await r.get(config.KEY_CUSTOM_GROUPS)
    custom = json.loads(custom_raw) if custom_raw else []
    if name not in custom:
        raise HTTPException(404, f"自定义组 {name} 不存在")
    custom.remove(name)
    await r.set(config.KEY_CUSTOM_GROUPS, json.dumps(custom, ensure_ascii=False))
    # 清理组权限配置
    await r.hdel(config.KEY_GROUP_TABS, name)
    await r.hdel(config.KEY_GROUP_NMS, name)
    await r.hdel(config.KEY_GROUP_BIZPERMS, name)
    # 组内成员置为无组别
    moved = []
    users_raw = await r.hgetall(config.KEY_USERS)
    for uname, data in users_raw.items():
        u = json.loads(data)
        if u.get("group") == name:
            u["group"] = ""
            await r.hset(config.KEY_USERS, uname, json.dumps(u, ensure_ascii=False))
            moved.append(uname)
    await _emit_daily_log("system", f"管理员 {current_user} 删除用户组: {name} (释放成员 {len(moved)} 人)", level="info", category="admin", username=current_user)
    return {"status": "ok", "name": name, "moved_members": moved, "groups": await _get_all_groups()}

@router.get("/api/admin/group-permissions")
async def get_group_permissions(current_user: str = Depends(verify_token)):
    """获取所有组的权限配置 (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    result = {}
    all_groups = await _get_all_groups()
    for grp in all_groups:
        tabs_raw = await r.hget(config.KEY_GROUP_TABS, grp)
        nms_raw = await r.hget(config.KEY_GROUP_NMS, grp)
        from core.bizperms import _load_group_perms
        grp_auth = await _load_group_perms(grp)  # 兼容旧数组值 (v2) → {perms, scopes}
        result[grp] = {
            "tabs": json.loads(tabs_raw) if tabs_raw else [],
            "nms_groups": json.loads(nms_raw) if nms_raw else [],
            "biz_perms": grp_auth["perms"],
            "scope": grp_auth["scopes"],
        }
    return {"groups": result, "available_groups": all_groups}

@router.post("/api/admin/group-permissions")
async def set_group_permissions(request: Request, current_user: str = Depends(verify_token)):
    """保存某组的权限配置 (仅管理员)"""
    await require_admin(current_user)
    body = await request.json()
    group = body.get("group", "").strip()
    if not group:
        raise HTTPException(400, "缺少组名")
    await _validate_group(group)

    tabs = body.get("tabs", [])
    nms_groups = body.get("nms_groups", [])
    if not isinstance(tabs, list) or not isinstance(nms_groups, list):
        raise HTTPException(400, "tabs 和 nms_groups 必须为数组")
    biz_perms = _clean_biz_perms(body.get("biz_perms", []))
    grp_scope = _clean_scopes(body.get("scope"))

    r = await get_redis()
    # 组级业务权限变更审计 (成员全体继承, 影响面大, 必须留痕)
    from core.bizperms import _load_group_perms
    old_auth = await _load_group_perms(group)
    old_biz = old_auth["perms"]
    if sorted(biz_perms) != sorted(old_biz):
        granted = [p for p in biz_perms if p not in old_biz]
        revoked = [p for p in old_biz if p not in biz_perms]
        parts = []
        if granted: parts.append("授予 " + "、".join(f"{biz_label(p)}({p})" for p in granted))
        if revoked: parts.append("回收 " + "、".join(f"{biz_label(p)}({p})" for p in revoked))
        await _log_user_op(current_user, "group_biz_perms", f"变更组 {group} 业务权限 (全体成员继承): " + "; ".join(parts))
        await _emit_daily_log("system", f"管理员 {current_user} 变更组 {group} 业务权限: " + "; ".join(parts), level="warning", category="admin", username=current_user)
    await r.hset(config.KEY_GROUP_TABS, group, json.dumps(tabs, ensure_ascii=False))
    await r.hset(config.KEY_GROUP_NMS, group, json.dumps(nms_groups, ensure_ascii=False))
    if sorted(biz_perms) != sorted(old_biz) or grp_scope != old_auth["scopes"]:
        scope_parts = []
        fmt = lambda s: "、".join(s) if s else "不限"
        scope_parts.append(f"站点 {fmt(grp_scope['sites'])}, 客户 {fmt(grp_scope['customers'])}")
        if sorted(biz_perms) != sorted(old_biz):
            granted = [p for p in biz_perms if p not in old_biz]
            revoked = [p for p in old_biz if p not in biz_perms]
            parts = []
            if granted: parts.append("授予 " + "、".join(f"{biz_label(p)}({p})" for p in granted))
            if revoked: parts.append("回收 " + "、".join(f"{biz_label(p)}({p})" for p in revoked))
            await _log_user_op(current_user, "group_biz_perms", f"变更组 {group} 业务权限 (全体成员继承): " + "; ".join(parts))
        await _emit_daily_log("system", f"管理员 {current_user} 变更组 {group} 业务授权 "
            f"(权限 {len(biz_perms)} 项; 范围: {'; '.join(scope_parts)})", level="warning", category="admin", username=current_user)
    await r.hset(config.KEY_GROUP_BIZPERMS, group, json.dumps({"perms": biz_perms, "scope": grp_scope}, ensure_ascii=False))
    logger.info(f"[ADMIN] {current_user} 更新组权限: {group} tabs={tabs} nms_groups={nms_groups} biz_perms={biz_perms} scope={grp_scope}")
    return {"status": "ok", "group": group, "tabs": tabs, "nms_groups": nms_groups, "biz_perms": biz_perms, "scope": grp_scope}

@router.post("/api/admin/group-members")
async def set_group_members(request: Request, current_user: str = Depends(verify_token)):
    """批量管理组成员: 将多个用户加入/移出指定组 (仅管理员)

    body: {group: "OPS-CORE", usernames: ["u1","u2"], action: "add"|"remove"}
    - action=add: 将 usernames 的组别设置为 group (group 为空字符串即清除组别)
    - action=remove: 仅当用户当前属于 group 时将其组别清空
    """
    await require_admin(current_user)
    body = await request.json()
    group = (body.get("group") or "").strip()
    usernames = body.get("usernames", [])
    action = body.get("action", "add")

    if action not in ("add", "remove"):
        raise HTTPException(400, "action 必须为 add 或 remove")
    if not isinstance(usernames, list) or not usernames:
        raise HTTPException(400, "usernames 必须为非空数组")
    await _validate_group(group)
    if action == "remove" and not group:
        raise HTTPException(400, "移出操作必须指定组别")

    r = await get_redis()
    updated, skipped = [], []
    for uname in usernames:
        raw = await r.hget(config.KEY_USERS, uname)
        if not raw:
            skipped.append(uname)
            continue
        user = json.loads(raw)
        if action == "add":
            user["group"] = group  # group 为空字符串即清除组别
        else:
            if user.get("group") != group:
                skipped.append(uname)
                continue
            user["group"] = ""
        await r.hset(config.KEY_USERS, uname, json.dumps(user, ensure_ascii=False))
        updated.append(uname)

    logger.info(f"[ADMIN] {current_user} 批量{'加入' if action == 'add' else '移出'}组 {group or '(清除组别)'}: updated={updated} skipped={skipped}")
    return {"status": "ok", "action": action, "group": group, "updated": updated, "skipped": skipped}

@router.get("/api/admin/ldap-config")
async def admin_ldap_config(current_user: str = Depends(verify_token)):
    """查看 LDAP 配置 (仅管理员，密码掩码)"""
    await require_admin(current_user)
    return {
        "ldap_enabled": config.LDAP_ENABLED,
        "server_uri": config.LDAP_SERVER_URI,
        "bind_dn": config.LDAP_BIND_DN,
        "bind_password": config.LDAP_BIND_PASSWORD[:3] + "***" if config.LDAP_BIND_PASSWORD else "",
        "search_base": config.LDAP_SEARCH_BASE,
        "search_filter": config.LDAP_SEARCH_FILTER,
        "default_role": config.LDAP_DEFAULT_ROLE,
    }

# ═══ API Keys (平台签发的 oneapi:* Token) ═══

def _gen_api_key() -> str:
    """生成随机 API Key (Base62, 32 字符)"""
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(32))

def _mask_key(key: str) -> str:
    """遮掩 key: 4位...4位"""
    if len(key) <= 8:
        return key[:2] + "..." + key[-2:]
    return key[:4] + "..." + key[-4:]

@router.get("/api/oneapi/keys")
async def oneapi_keys_list(current_user: str = Depends(verify_token)):
    """列出当前平台签发的所有 API Keys"""
    r = await get_redis()
    raw = await r.hgetall(config.KEY_ONEAPI_KEYS)
    keys = []
    for _hkey, data in raw.items():
        try:
            info = json.loads(data)
            keys.append({
                "label": info.get("label", ""),
                "key": _mask_key(info.get("key", "")),
                "id": info.get("id", ""),
                "created_at": info.get("created_at", ""),
                "created_by": info.get("created_by", ""),
            })
        except json.JSONDecodeError:
            continue
    keys.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"keys": keys, "count": len(keys)}

@router.post("/api/oneapi/keys")
async def oneapi_keys_create(request: Request, current_user: str = Depends(verify_token)):
    """创建新 API Key (仅管理员)"""
    await require_admin(current_user)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "缺少 name")
    label = f"oneapi:{name}"
    key_id = uuid.uuid4().hex[:8]
    r = await get_redis()
    # 检查 label 是否已存在
    existing = await r.hgetall(config.KEY_ONEAPI_KEYS)
    for _hkey, d in existing.items():
        try:
            if json.loads(d).get("label") == label:
                raise HTTPException(409, f"Label '{label}' 已存在")
        except json.JSONDecodeError:
            continue
    api_key = _gen_api_key()
    info = {
        "label": label,
        "key": api_key,
        "id": key_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": current_user,
    }
    await r.hset(config.KEY_ONEAPI_KEYS, key_id, json.dumps(info, ensure_ascii=False))
    await _log_user_op(current_user, "create_api_key", f"label={label}, id={key_id}")
    return {"status": "ok", "label": label, "key": api_key, "key_masked": _mask_key(api_key), "id": key_id}

@router.delete("/api/oneapi/keys/{key_id}")
async def oneapi_keys_delete(key_id: str, current_user: str = Depends(verify_token)):
    """删除 API Key (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    deleted = await r.hdel(config.KEY_ONEAPI_KEYS, key_id)
    if not deleted:
        raise HTTPException(404, f"Key '{key_id}' 不存在")
    await _log_user_op(current_user, "delete_api_key", f"id={key_id}")
    return {"status": "ok", "deleted": key_id}

def _normalize_base_url(url: str) -> str:
    """规范化 OpenAI 兼容 base URL; 非法输入抛 ValueError (调用方 400)。

    - 必须带 http(s):// 前缀 (拒绝主机片段如 "svc-opscore" 被默默存成坏 URL)
    - 剥掉误粘贴的 /chat/completions 端点后缀
    - 路径已含 /v1 等版本段则不再追加
    """
    import re as _re
    u = (url or "").strip()
    if not u:
        return ""
    if not _re.match(r"^https?://", u, _re.I):
        raise ValueError(f"Base URL 必须以 http:// 或 https:// 开头: {u!r}")
    u = u.rstrip("/")
    if u.lower().endswith("/chat/completions"):
        u = u[: -len("/chat/completions")].rstrip("/")
    if not _re.search(r"/v\d+(?:/|$)", _urlparse_path(u)):
        u = u + "/v1"
    return u


def _urlparse_path(u: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(u).path or "/"
    except Exception:
        return "/"


async def _get_all_providers() -> list:
    """合并内置 + Redis 自定义 Provider 列表"""
    import copy
    providers = copy.deepcopy(_BUILTIN_PROVIDERS)
    try:
        r = await get_redis()
        custom_raw = await r.get(config.KEY_CUSTOM_PROVIDERS)
        if custom_raw:
            custom_list = json.loads(custom_raw)
            for cp in custom_list:
                cp["builtin"] = False
            providers.extend(custom_list)
            # 自定义 provider 的默认 URL 回填路由表 (进程重启后 PROVIDER_URL_MAP 只含内置,
            # 不回填则 pref.base_url 为空时运行时静默落到 api.openai.com)
            for cp in custom_list:
                if cp.get("base_url"):
                    PROVIDER_URL_MAP[cp["id"]] = cp["base_url"]
    except Exception as e:
        logger.warning(f"[LLM] 加载自定义 Provider 失败: {e}")
    return providers

# 各 provider 动态模型缓存 (pid → {"models": [...], "ts": ...}; 就地变更不重绑, 避免跨模块失效)
_models_cache: dict = {}


def _resolve_provider_endpoint(pid: str, pref: dict, providers: list) -> tuple:
    """解析 provider 的 (base_url, api_key) — 优先当前 top-level 凭据, 其次 provider_creds 记忆槽"""
    if pid == "oneapi":
        return (pref.get("oneapi_base_url") or config.ONEAPI_BASE_URL or "",
                pref.get("oneapi_key") or config.ONEAPI_API_KEY or "")
    if pid == "anthropic":
        return "", ""
    slot = (pref.get("provider_creds") or {}).get(pid) or {}
    url = (pref.get("base_url") if pref.get("provider") == pid else "") or slot.get("base_url") or ""
    key = (pref.get("api_key") if pref.get("provider") == pid else "") or slot.get("api_key") or ""
    if not url:
        p = next((p for p in providers if p["id"] == pid), None)
        url = (p or {}).get("base_url") or ""
    return url, key


async def _fetch_gateway_models(pid: str, base_url: str, api_key: str) -> list:
    """从任意 OpenAI 兼容网关动态获取该凭据可用的模型列表 (per-provider 缓存)"""
    now = time.time()
    cached = _models_cache.get(pid) or {}
    if cached.get("models") and now - cached.get("ts", 0) < _ONEAPI_CACHE_TTL:
        return cached["models"]
    try:
        import httpx
        # base_url 已经包含 /v1，直接拼 /models
        url = f"{base_url.rstrip('/')}/models"
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                data = resp.json()
                # 过滤内部测试模型 (openclaw:* 等)
                models = [
                    m.get("id", "") for m in data.get("data", [])
                    if m.get("id") and not m.get("id", "").startswith("openclaw")
                ]
                if models:
                    _models_cache[pid] = {"models": models, "ts": now}
                    logger.info(f"[LLM] {pid} 网关可用模型 ({len(models)}): {models[:10]}...")
                    return models
                logger.warning(f"[LLM] {pid} 网关模型列表为空 (HTTP 200)")
    except Exception as e:
        logger.warning(f"[LLM] 获取 {pid} 网关模型列表失败: {e}")
    return cached.get("models", [])

@router.get("/api/llm/config")
async def llm_config_get(current_user: str = Depends(verify_token)):
    # P0: 模型配置含密钥 — 读取收归管理员, 密钥仅返回掩码与是否配置 (不回显原文)
    await require_admin(current_user)
    r = await get_redis()
    pref_raw = await r.get(config.KEY_LLM_PREF)
    pref = json.loads(pref_raw) if pref_raw else {}

    current_provider = pref.get("provider") or config.LLM_PROVIDER
    current_model = pref.get("model") or config.LLM_MODEL

    # 合并内置 + 自定义 providers
    providers = await _get_all_providers()

    # 动态模型列表推广到所有 provider: 各自凭据拉 /models; 同时合并记忆槽里的 models 历史
    # (同一 provider 接入更多模型: 网关返回的 + 用过记住的, 都进标签列表)
    oneapi_url = pref.get("oneapi_base_url", "") or config.ONEAPI_BASE_URL
    creds_all = pref.get("provider_creds") or {}
    # 并行拉取各 provider 动态模型列表 (串行会叠加超时: 单网关挂 → 配置页慢 10s×N)
    import asyncio as _aio
    _targets = []
    for p in providers:
        pid = p["id"]
        for m in ((creds_all.get(pid) or {}).get("models") or []):
            if m not in p["models"]:
                p["models"].append(m)
        if pid == "anthropic":
            continue
        gw_url, gw_key = _resolve_provider_endpoint(pid, pref, providers)
        if gw_url and gw_key:
            _targets.append((p, _fetch_gateway_models(pid, gw_url, gw_key)))
    if _targets:
        _dyn_results = await _aio.gather(*(t[1] for t in _targets), return_exceptions=True)
        for (p, _fut), dyn in zip(_targets, _dyn_results):
            for dm in (dyn if isinstance(dyn, list) else []):
                if dm not in p["models"]:
                    p["models"].append(dm)

    # 确保当前已保存的模型在对应 provider 的列表中
    for p in providers:
        if p["id"] == current_provider and current_model and current_model not in p["models"]:
            p["models"].insert(0, current_model)

    def _mask(v: str) -> str:
        # 2026-10-18: 不再回显 __KEEP__ 哨兵 (用户看到字面量误以为 Key 损坏重输)。
        # 已配置状态由 has_* 布尔表达; 前端空输入提交 = 保留原值 (POST PATCH 语义)。
        return ""

    # 各 provider 已存凭据摘要 (URL 非密钥可回显; key 只回是否已配置, 供前端切换时恢复表单)
    creds = dict(pref.get("provider_creds") or {})
    cur_slot = creds.setdefault(current_provider, {})
    cur_slot.setdefault("base_url", pref.get("base_url", ""))
    cur_slot.setdefault("api_key", pref.get("api_key", ""))
    if current_provider == "oneapi":
        cur_slot.setdefault("oneapi_base_url", pref.get("oneapi_base_url", ""))
        cur_slot.setdefault("oneapi_key", pref.get("oneapi_key", ""))
    if current_model:
        cur_slot.setdefault("model", current_model)
    provider_creds = {
        pid: {"base_url": slot.get("oneapi_base_url", "") or slot.get("base_url", ""),
              "has_api_key": bool(slot.get("api_key") or slot.get("oneapi_key")),
              "model": slot.get("model", ""),
              "models": slot.get("models", [])}
        for pid, slot in creds.items()
    }

    # 真实模型名: 网关验证映射 (该是什么模型就是什么模型) > 静态别名表 > 原名
    from core.model_gateway import verified_model_names, _display_real_model
    _vmap = await verified_model_names(r)

    return {
        "provider": current_provider,
        "model": current_model,
        "real_model": _display_real_model(_vmap, current_model),
        "model_names": _vmap,
        "api_key": _mask(pref.get("api_key", "")),
        "has_api_key": bool(pref.get("api_key")),
        "base_url": pref.get("base_url", ""),
        "anthropic_key": _mask(pref.get("anthropic_key", "")),
        "has_anthropic_key": bool(pref.get("anthropic_key")),
        "oneapi_key": _mask(pref.get("oneapi_key", "")),
        "has_oneapi_key": bool(pref.get("oneapi_key")),
        "oneapi_base_url": oneapi_url,
        "sanitize_enabled": pref.get("sanitize_enabled", True),
        "providers": providers,
        "provider_creds": provider_creds,
        "has_env_key": bool(config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY or config.ANTHROPIC_API_KEY or config.ONEAPI_API_KEY),
    }

@router.post("/api/llm/config")
async def llm_config_set(request: Request, current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    body = await request.json()
    r = await get_redis()
    # P0: PATCH 语义 — 未提交密钥 (缺省/空/掩码哨兵) 则保留原值; 清除必须显式 (clear_keys)
    _KEY_FIELDS = ("api_key", "anthropic_key", "oneapi_key")
    _CLEARABLE = set(_KEY_FIELDS) | {"base_url"}
    old_raw = await r.get(config.KEY_LLM_PREF)
    old_pref = json.loads(old_raw) if old_raw else {}
    allowed = {"provider", "model", "api_key", "base_url", "anthropic_key",
               "oneapi_key", "oneapi_base_url", "sanitize_enabled"}
    submitted = {k: v for k, v in body.items() if k in allowed and v not in ("", None, "__KEEP__")}
    clear_keys = [k for k in (body.get("clear_keys") or []) if k in _CLEARABLE]

    # URL 校验+规范化 (非法直接 400, 不再默默存坏值 — 如 "svc-opscore" → "svc-opscore/v1")
    for uk in ("base_url", "oneapi_base_url"):
        if uk in submitted:
            try:
                submitted[uk] = _normalize_base_url(str(submitted[uk]))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

    old_provider = old_pref.get("provider", "")
    new_provider = str(submitted.get("provider") or old_provider or config.LLM_PROVIDER)
    same_provider = new_provider == old_provider
    all_providers = await _get_all_providers()
    # 按 provider 记忆凭据 (切换不丢: 切走暂存, 切回恢复; top-level 始终=当前 provider 的凭据)
    creds_mem = {pid: dict(slot) for pid, slot in (old_pref.get("provider_creds") or {}).items()}
    mem = creds_mem.get(new_provider) or {}
    prov_def_url = ""
    for p in all_providers:
        if p["id"] == new_provider and p.get("base_url"):
            try:
                prov_def_url = _normalize_base_url(p["base_url"])
            except ValueError:
                prov_def_url = ""
            break

    # 基础字段: 以旧配置为底, 显式提交才覆盖
    clean = {k: v for k, v in old_pref.items() if k not in _KEY_FIELDS and k != "provider_creds"}
    clean.update({k: v for k, v in submitted.items()
                  if k not in _KEY_FIELDS and k not in ("base_url", "model")})
    clean["provider"] = new_provider
    # model: 同 provider PATCH 保留; 切换时用新 provider 的记忆值/默认首模型 (旧 model 属于旧 provider)
    if "model" in submitted:
        clean["model"] = submitted["model"]
    elif not same_provider:
        clean["model"] = mem.get("model", "") or next(iter(
            [p["models"][0] for p in all_providers
             if p["id"] == new_provider and p.get("models")]), "")
    # base_url / api_key: 归属当前 provider — 提交 > 同 provider 旧值 > 该 provider 记忆 > provider 默认 URL
    if "base_url" in clear_keys:
        clean.pop("base_url", None)
    elif "base_url" in submitted:
        clean["base_url"] = submitted["base_url"]
    elif same_provider and old_pref.get("base_url"):
        clean["base_url"] = old_pref["base_url"]
    else:
        clean["base_url"] = mem.get("base_url", "") or prov_def_url
    if "api_key" in clear_keys:
        clean.pop("api_key", None)
    elif "api_key" in submitted:
        clean["api_key"] = submitted["api_key"]
    elif same_provider and old_pref.get("api_key"):
        clean["api_key"] = old_pref["api_key"]
    else:
        clean["api_key"] = mem.get("api_key", "")
    # 其余密钥字段 (anthropic/oneapi 全局网关凭据): PATCH 保留/显式清除
    for k in ("anthropic_key", "oneapi_key"):
        if k in clear_keys:
            clean.pop(k, None)
        elif k in submitted:
            clean[k] = submitted[k]
        elif k in old_pref:
            clean[k] = old_pref[k]
    if "oneapi_base_url" in submitted:
        clean["oneapi_base_url"] = submitted["oneapi_base_url"]
    elif old_pref.get("oneapi_base_url") and "oneapi_base_url" not in clear_keys:
        clean["oneapi_base_url"] = old_pref["oneapi_base_url"]

    # 显式清除同时清掉对应记忆槽字段
    slot = dict(creds_mem.get(new_provider) or {})
    for ck in clear_keys:
        slot.pop({"api_key": "api_key", "oneapi_key": "oneapi_key",
                  "anthropic_key": "anthropic_key", "base_url": "base_url"}.get(ck, ck), None)
    # 更新当前 provider 记忆槽 (oneapi 用专用字段, 不占通用 api_key 槽)
    if new_provider == "oneapi":
        if clean.get("oneapi_base_url"):
            slot["oneapi_base_url"] = clean["oneapi_base_url"]
        if clean.get("oneapi_key"):
            slot["oneapi_key"] = clean["oneapi_key"]
    else:
        if clean.get("base_url"):
            slot["base_url"] = clean["base_url"]
        if clean.get("api_key"):
            slot["api_key"] = clean["api_key"]
    if clean.get("model"):
        slot["model"] = clean["model"]
        # 记住该 provider 用过的模型 (标签列表来源之一; 上限 30)
        used = list(slot.get("models") or [])
        if clean["model"] not in used:
            used.append(clean["model"])
            slot["models"] = used[-30:]
    creds_mem[new_provider] = {k: v for k, v in slot.items() if v}
    clean["provider_creds"] = creds_mem

    # 校验日志
    provider = clean.get("provider", "")
    if provider == "oneapi" and not clean.get("oneapi_key"):
        logger.warning(f"[llm_config] 用户 {current_user} 保存 OneAPI 配置但未设置 Token")
    if provider and not clean.get("model"):
        logger.warning(f"[llm_config] 用户 {current_user} 保存配置但未设置 model (provider={provider})")
    await r.set(config.KEY_LLM_PREF, json.dumps(clean, ensure_ascii=False))
    # 保存后清除模型缓存 (凭据可能变了, 动态列表需重拉)
    _models_cache.clear()
    await _log_user_op(current_user, "llm_config",
                       f"provider={clean.get('provider')}, model={clean.get('model')}"
                       + (f", clear_keys={clear_keys}" if clear_keys else ""))
    # 回显摘要: 记忆槽含密钥原文 → 只回掩码摘要, 不回 provider_creds 原文
    echo = {k: v for k, v in clean.items() if k != "provider_creds"}
    echo = {k: ("***" if "key" in k and v else v) for k, v in echo.items()}
    echo["provider_creds"] = {
        pid: {kk: ("***" if "key" in kk and vv else vv) for kk, vv in s.items()}
        for pid, s in creds_mem.items()}
    return {"status": "ok", "saved": echo}




# ═══ P2 收尾 (task-c17): 记忆检索 API — owner 强过滤 (SPEC: 检索前过滤/返回前复核) ═══

@router.get("/api/memory/search")
async def memory_search(q: str = "", limit: int = 10,
                        current_user: str = Depends(verify_token)):
    """当前用户的跨会话记忆搜索 (仅本人; 他人/画像不可见)"""
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    hits = await mem.search_past_sessions(q or "", user_id=current_user,
                                           limit=min(limit, 20))
    # 返回前复核 (防御实现变化)
    hits = [h for h in hits if (h.user_id or "") == current_user]
    return {"query": q,
            "results": [{"session_id": h.session_id, "created_at": h.created_at,
                         "summary": h.summary, "topics": (h.topics or [])[:8]}
                        for h in hits]}


@router.delete("/api/memory/item")
async def memory_delete_item(session_id: str,
                             current_user: str = Depends(verify_token)):
    """删除本人一条会话记忆 (owner 双重复核)"""
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    ok = await mem.delete_session_summary(session_id, user_id=current_user)
    if not ok:
        raise HTTPException(404, "记忆不存在或非本人")
    await _log_user_op(current_user, "memory_delete", f"session={session_id[:10]}")
    return {"status": "ok"}


@router.delete("/api/memory/all")
async def memory_clear_all(current_user: str = Depends(verify_token)):
    """清除本人全部记忆 (含 wiki 私有 summaries 页)"""
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    n = await mem.clear_user_memories(current_user)
    await _log_user_op(current_user, "memory_clear_all", f"{n} 条")
    return {"status": "ok", "deleted": n}


@router.post("/api/auth/change-password")
async def change_password(request: Request, current_user: str = Depends(verify_token)):
    """用户自助修改密码 (本地账号; LDAP 账号走 AD)"""
    from webchat_api import hash_password
    body = await request.json()
    old, new = str(body.get("old_password") or ""), str(body.get("new_password") or "")
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    user = json.loads(raw) if raw else {}
    if not user.get("password_hash"):
        raise HTTPException(400, "该账号为 LDAP/AD 域账号, 请在 AD 中修改密码")
    if user["password_hash"] != hash_password(old):
        raise HTTPException(400, "当前密码不正确")
    user["password_hash"] = hash_password(new)
    await r.hset(config.KEY_USERS, current_user, json.dumps(user, ensure_ascii=False))
    await _log_user_op(current_user, "change_password", "自助修改密码")
    return {"status": "ok"}


@router.put("/api/auth/profile")
async def update_profile(request: Request, current_user: str = Depends(verify_token)):
    """用户自助更新显示名"""
    body = await request.json()
    display = str(body.get("display_name") or "").strip()[:40]
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    user = json.loads(raw) if raw else {}
    user["display_name"] = display
    await r.hset(config.KEY_USERS, current_user, json.dumps(user, ensure_ascii=False))
    await _log_user_op(current_user, "update_profile", f"display_name={display}")
    return {"status": "ok", "display_name": display}


# ═══ 记忆后台管理 (admin: 全量可见可管 — 完整记忆不只用户自己看) ═══

@router.get("/api/admin/memories")
async def admin_memories_list(q: str = "", username: str = "", limit: int = 100,
                              current_user: str = Depends(verify_token)):
    """管理员查看全部用户的会话记忆 (含 owner; 按用户/关键词过滤)"""
    await require_admin(current_user)
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    if username:
        hits = await mem.search_past_sessions(q or "", user_id=username, limit=min(limit, 200))
    else:
        hits = await mem.search_past_sessions(q or "", limit=0) if False else []
        # 全量: 直接遍历 sessions (search 需要 query)
        all_sessions = list(mem._sessions.values())
        ql = (q or "").lower()
        hits = [s for s in all_sessions
                if (not ql or ql in (s.summary or "").lower()
                    or any(ql in str(t).lower() for t in (s.topics or [])))]
        hits.sort(key=lambda s: s.created_at, reverse=True)
        hits = hits[:min(limit, 200)]
    return {"memories": [{"username": s.user_id, "session_id": s.session_id,
                          "created_at": s.created_at, "summary": s.summary,
                          "topics": (s.topics or [])[:6]}
                         for s in hits], "total": len(hits)}


@router.delete("/api/admin/memories/item")
async def admin_memory_delete(session_id: str, username: str = "",
                              current_user: str = Depends(verify_token)):
    """管理员删除任意用户的单条记忆"""
    await require_admin(current_user)
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    target = username or ""
    if not target:
        s = mem._sessions.get(session_id)
        target = s.user_id if s else ""
    ok = await mem.delete_session_summary(session_id, user_id=target)
    if not ok:
        raise HTTPException(404, "记忆不存在")
    await _log_user_op(current_user, "admin_memory_delete", f"session={session_id[:10]} user={target}")
    return {"status": "ok"}


@router.delete("/api/admin/memories/user")
async def admin_memory_clear_user(username: str,
                                  current_user: str = Depends(verify_token)):
    """管理员清除指定用户全部记忆"""
    await require_admin(current_user)
    from core.memory.cross_session_memory import get_cross_session_memory
    mem = await get_cross_session_memory()
    n = await mem.clear_user_memories(username)
    await _log_user_op(current_user, "admin_memory_clear_user", f"user={username} ({n} 条)")
    return {"status": "ok", "deleted": n}


@router.get("/api/memory/settings")
async def memory_settings_get(current_user: str = Depends(verify_token)):
    """用户记忆开关 (默认开; 关闭后不注入也不新存)"""
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    enabled = True
    try:
        enabled = json.loads(raw).get("memory_enabled", True) if raw else True
    except Exception:
        pass
    return {"memory_enabled": enabled}


@router.put("/api/memory/settings")
async def memory_settings_set(request: Request,
                              current_user: str = Depends(verify_token)):
    """设置记忆开关 (关闭=既不注入也不新存; 已有记忆保留可随时清除)"""
    body = await request.json()
    enabled = bool(body.get("memory_enabled", True))
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    user = json.loads(raw) if raw else {}
    user["memory_enabled"] = enabled
    await r.hset(config.KEY_USERS, current_user, json.dumps(user, ensure_ascii=False))
    await _log_user_op(current_user, "memory_toggle",
                       f"memory_enabled={enabled}")
    return {"status": "ok", "memory_enabled": enabled}


@router.get("/api/memory/sessions/search")
async def memory_sessions_search(q: str = "", limit: int = 10,
                                 current_user: str = Depends(verify_token)):
    """全文搜索自己的历史会话消息 (FTS5; owner 强过滤 — SPEC: 会话搜索统一 ACL)"""
    from core.memory.session_search import get_session_search
    ss = await get_session_search()
    results = await ss.search(q or "", user_id=current_user, limit=min(limit, 20))
    return {"query": q, "results": [
        {"session_id": r.session_id, "matched_snippet": str(getattr(r, "matched_snippet", ""))[:300],
         "created_at": getattr(r, "created_at", "")}
        for r in results]}


# ═══ Computer Use 配置管理 (P3: 动态修改, 不需重启) ═══

import importlib

async def _cu_config_to_dict() -> dict:
    """读取当前 CU 配置 (Redis 动态覆盖 + 环境变量兜底)"""
    import agents.computer_agent as cu_mod
    r = await get_redis()
    raw = await r.hgetall(f"{config.REDIS_PREFIX}:cu_config")
    return {
        "allowed_domains": raw.get("allowed_domains",
                                   getattr(config, "CU_ALLOWED_DOMAINS", "")),
        "max_steps": int(raw.get("max_steps", cu_mod.MAX_STEPS)),
        "step_timeout_s": int(raw.get("step_timeout_s", cu_mod.STEP_TIMEOUT_S)),
        "total_budget_s": int(raw.get("total_budget_s", cu_mod.TOTAL_BUDGET_S)),
        "max_screenshots": int(raw.get("max_screenshots", cu_mod.MAX_SCREENSHOTS)),
        "headless": raw.get("headless", "true") != "false",
        "default_user_agent": raw.get("default_user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"),
        "session_ttl_s": int(raw.get("session_ttl_s", 1800)),
        "site_credentials": raw.get("site_credentials", ""),
    }


def _cu_apply(cfg: dict):
    """将配置应用到 computer_agent 模块 (运行时生效)"""
    import agents.computer_agent as cu_mod
    if "max_steps" in cfg:
        cu_mod.MAX_STEPS = int(cfg["max_steps"])
    if "step_timeout_s" in cfg:
        cu_mod.STEP_TIMEOUT_S = int(cfg["step_timeout_s"])
    if "total_budget_s" in cfg:
        cu_mod.TOTAL_BUDGET_S = int(cfg["total_budget_s"])
    if "max_screenshots" in cfg:
        cu_mod.MAX_SCREENSHOTS = int(cfg["max_screenshots"])
    if "allowed_domains" in cfg:
        setattr(config, "CU_ALLOWED_DOMAINS", str(cfg["allowed_domains"]).strip())


# ═══ 统一能力发现 API (前端能力中心 + 命令面板数据源) ═══

@router.get("/api/capabilities")
async def capabilities_list(current_user: str = Depends(verify_token)):
    """获取全部可调用能力 (agent/工具/脚本/系统), 按分类分组"""
    from core.capability_registry import get_all_capabilities
    caps = await get_all_capabilities()
    # 标记当前用户可用的 (业务权限过滤)
    from core.bizperms import effective_perms
    eff = await effective_perms(current_user)
    user_perms = set(eff["effective"])
    is_admin = eff.get("role") == "admin"

    def _visible(cap):
        perm = cap.get("perm", "")
        if not perm or perm == "admin":
            return is_admin or not perm
        return is_admin or perm in user_perms

    for group in caps.values():
        group[:] = [c for c in group if _visible(c)]

    return {"capabilities": caps, "user": current_user, "is_admin": is_admin}


@router.post("/api/capabilities/execute")
async def capabilities_execute(request: Request,
                               current_user: str = Depends(verify_token)):
    """能力执行入口 — 插件缝: tools/pre_execute (bail 守卫) / tools/post_execute (审计)

    body: {"capability_id": "nms_status", "params": {...}}
    - type=tool: 转发 HTTP 调用
    - type=script: 走 script_exec 受限沙箱
    - type=chat/browser: 返回提示 (应在对话中触发)
    """
    from core.capability_registry import get_capability_detail
    from core.plugin_system import get_event_bus
    body = await request.json()
    cap_id = body.get("capability_id", "")
    params = body.get("params", {})
    cap = await get_capability_detail(cap_id, "")
    if not cap:
        raise HTTPException(404, f"能力 '{cap_id}' 不存在")

    # dsh tools/pre-execute 语义: 策略插件可 bail 拒绝 ({"deny": true, "reason": "..."})
    bailed, reason = await get_event_bus().bail(
        "tools/pre_execute", {"capability_id": cap_id, "type": cap.get("type"),
                              "actor": current_user, "params": params})
    if bailed and isinstance(reason, dict) and reason.get("deny"):
        raise HTTPException(403, reason.get("reason") or "插件策略拒绝该操作")

    t0 = time.time()
    try:
        return await _capabilities_execute_impl(request, current_user, cap, cap_id, params)
    finally:
        # dsh tools/post-execute 语义: 观察/审计, 不改结果
        try:
            await get_event_bus().emit("tools/post_execute", {
                "capability_id": cap_id, "type": cap.get("type"), "actor": current_user,
                "duration_s": round(time.time() - t0, 2)})
        except Exception:
            pass


async def _capabilities_execute_impl(request: Request, current_user: str,
                                     cap: dict, cap_id: str, params: dict):
    body = {"capability_id": cap_id, "params": params}
    cap_type = cap.get("type")
    if cap_type == "tool":
        # 内部调用 (不走 HTTP 避免同进程死锁): 独立子进程发起调用 (绕开同进程连接池)
        path = cap.get("path", "")
        for k, v in params.items():
            path = path.replace(f"{{{k}}}", str(v))
        import subprocess
        import shlex
        tok = body.get("_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
        url = f"http://127.0.0.1:{config.WEBCHAT_PORT}{path}"
        method = cap.get("method", "GET")
        try:
            if method == "GET":
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-m", "15", "-H", f"Authorization: Bearer {tok}", url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            else:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-m", "15", "-X", method,
                    "-H", f"Authorization: Bearer {tok}",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(params), url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            raw = stdout.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                data = raw[:500]
            return {"success": proc.returncode == 0, "data": data, "status": proc.returncode}
        except asyncio.TimeoutError:
            return {"success": False, "data": "工具调用超时 (20s)", "status": 408}

    elif cap_type == "script":
        from core.script_exec import run_registered, ScriptRegistryError
        try:
            result = await run_registered(cap_id, params, "localhost")
            return {"success": result["ok"], "data": result}
        except ScriptRegistryError as e:
            raise HTTPException(400, f"{e.code}: {e.detail}")

    elif cap_type in ("chat", "browser"):
        return {"success": False,
                "hint": f"该能力通过对话触发: 发送「{cap.get('example', cap_id)}」给 {cap.get('name', '')}"}

    raise HTTPException(400, f"不支持的能力类型: {cap_type}")


# ═══ Computer Use 凭据管理 (加密存储, 密码不进对话/日志) ═══

@router.get("/api/admin/computer-use/credentials")
async def cu_credentials_list(current_user: str = Depends(verify_token)):
    """列出凭据 (密码掩码, 不返回明文)"""
    await require_admin(current_user)
    from core.cu_credentials import list_credentials
    r = await get_redis()
    creds = await list_credentials(r)
    return {"credentials": creds}


@router.put("/api/admin/computer-use/credentials/{name}")
async def cu_credentials_save(name: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """保存/更新凭据 (密码加密存储; 不回显)"""
    await require_admin(current_user)
    from core.cu_credentials import save_credential
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    url_pattern = str(body.get("url_pattern", "")).strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    r = await get_redis()
    # 编辑已有凭据时密码留空 = 保持原密码 (管理页编辑流程; 不回显明文故无法回填)
    if not password:
        from core.cu_credentials import get_credential
        existing = await get_credential(r, name)
        if not existing or not existing.get("password_enc"):
            raise HTTPException(400, "密码不能为空 (该凭据不存在, 新增必须提供密码)")
        from core.cu_credentials import save_credential_record
        await save_credential_record(r, name, {
            "name": name, "username": username,
            "password_enc": existing["password_enc"],
            "url_pattern": url_pattern,
            "updated_at": time.time(),
        })
        await _log_user_op(current_user, "cu_cred_save", f"name={name} user={username} (密码保持)")
        return {"status": "ok", "name": name, "username": username}
    await save_credential(r, name, username, password, url_pattern)
    await _log_user_op(current_user, "cu_cred_save", f"name={name} user={username}")
    return {"status": "ok", "name": name, "username": username}


@router.delete("/api/admin/computer-use/credentials/{name}")
async def cu_credentials_delete(name: str,
                               current_user: str = Depends(verify_token)):
    """删除凭据"""
    await require_admin(current_user)
    from core.cu_credentials import delete_credential
    r = await get_redis()
    ok = await delete_credential(r, name)
    if not ok:
        raise HTTPException(404, f"凭据 '{name}' 不存在")
    await _log_user_op(current_user, "cu_cred_delete", f"name={name}")
    return {"status": "ok"}


@router.get("/api/admin/computer-use/config")
async def cu_config_get(current_user: str = Depends(verify_token)):
    """获取 Computer Use 配置"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    return await _cu_config_to_dict()


@router.put("/api/admin/computer-use/config")
async def cu_config_set(request: Request,
                       current_user: str = Depends(verify_token)):
    """修改 Computer Use 配置 (运行时生效, 不需重启; 同时持久化到 Redis)"""
    await require_admin(current_user)
    body = await request.json()

    # 校验
    if "max_steps" in body:
        ms = int(body["max_steps"])
        if not 1 <= ms <= 100:
            raise HTTPException(400, "max_steps 须在 1-100 之间")
    if "total_budget_s" in body:
        tb = int(body["total_budget_s"])
        if not 30 <= tb <= 3600:
            raise HTTPException(400, "total_budget_s 须在 30-3600 秒之间")
    if "step_timeout_s" in body:
        st = int(body["step_timeout_s"])
        if not 5 <= st <= 120:
            raise HTTPException(400, "step_timeout_s 须在 5-120 秒之间")
    if "allowed_domains" in body:
        domains = str(body["allowed_domains"]).strip()
        if len(domains) > 2000:
            raise HTTPException(400, "域名白名单过长")
    if "site_credentials" in body:
        sc = str(body["site_credentials"]).strip()
        if len(sc) > 8000:
            raise HTTPException(400, "站点凭据配置过长")

    # 持久化
    r = await get_redis()
    mapping = {}
    for k in ("allowed_domains", "max_steps", "step_timeout_s",
              "total_budget_s", "max_screenshots", "headless",
              "default_user_agent", "session_ttl_s", "site_credentials"):
        if k in body:
            mapping[k] = str(body[k])
    if mapping:
        await r.hset(f"{config.REDIS_PREFIX}:cu_config", mapping=mapping)

    # 运行时应用
    _cu_apply(body)
    await _log_user_op(current_user, "cu_config_update",
                       f"keys={list(body.keys())}")
    return {"status": "ok", "config": await _cu_config_to_dict()}


@router.post("/api/admin/computer-use/test")
async def cu_test_domain(request: Request,
                         current_user: str = Depends(verify_token)):
    """测试域名是否允许访问 + URL 是否合法"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    body = await request.json()
    url = str(body.get("url") or "")
    from agents.computer_agent import _url_allowed
    ok, reason = _url_allowed(url)
    return {"url": url, "allowed": ok, "reason": reason}


@router.post("/api/admin/computer-use/sessions/clear")
async def cu_clear_sessions(current_user: str = Depends(verify_token)):
    """清除所有持久浏览器会话 (下次任务将新建 context)"""
    await require_admin(current_user)
    # 通知 computer_agent 清理会话
    r = await get_redis()
    await r.publish(config.channel("computer_agent"),
                    json.dumps({"action": "clear_sessions"}))
    await _log_user_op(current_user, "cu_clear_sessions", "清除浏览器会话")
    return {"status": "ok"}


@router.get("/api/memory/profile")
async def memory_profile(current_user: str = Depends(verify_token)):
    """当前用户的显式偏好画像 (opt-in: ZOPS_USER_PROFILE=true 时才写入; 仅显式记忆来源)"""
    if not getattr(config, "USER_PROFILE_ENABLED", False):
        return {"enabled": False, "notes": [],
                "note": "画像未启用 (SPEC: 画像推断不转授权; 设 ZOPS_USER_PROFILE=true 开启显式写入)"}
    from core.memory.user_profile import get_profile_manager
    pm = await get_profile_manager()
    profile = await pm.get_profile(current_user)
    notes = getattr(profile, "dialectic_notes", None)
    if notes is None and hasattr(pm, "_profiles"):
        prof = pm._profiles.get(current_user)
        notes = getattr(prof, "notes", None) or getattr(prof, "dialectic_notes", None) if prof else None
    return {"enabled": True, "username": current_user,
            "notes": [str(n)[:300] for n in (notes or [])][:20]}

# ═══ P2 (task-c17): 技能生命周期管理 (candidate→evaluated→approved→published→retired) ═══

async def _is_admin_user(username: str) -> bool:
    try:
        r = await get_redis()
        raw = await r.hget(config.KEY_USERS, username)
        return bool(raw and json.loads(raw).get("role") == "admin")
    except Exception:
        return False


def _lc():
    from core.harness.skill_lifecycle import get_skill_lifecycle, LifecycleError
    return get_skill_lifecycle(), LifecycleError


@router.get("/api/skills/lifecycle")
async def skills_lifecycle_list(current_user: str = Depends(verify_token)):
    """技能生命周期总览 (各版本状态/发布指针/审核记录/成功率口径) — admin"""
    await require_admin(current_user)
    lc, _ = _lc()
    out = []
    for name, info in lc.describe().items():
        rates = {}
        for ver, vv in (info.get("versions") or {}).items():
            rates[ver] = lc.success_rate(name) if info.get("published") == ver else None
        stats = lc.usage_stats(name) if hasattr(lc, "usage_stats") else {}
        out.append({
            "name": name,
            "published": info.get("published"),
            "versions": info.get("versions") or {},
            "success_rate": lc.success_rate(name),   # null=未验证 (SPEC: 不默认成功)
            "usage": stats.get("usage", 0),
            "verified": stats.get("verified", 0),
            "aux_budget_left": lc.aux_budget_left(),
        })
    out.sort(key=lambda x: x["name"])
    return {"skills": out, "aux_budget_daily": lc.aux_budget_daily}


@router.post("/api/skills/lifecycle/{name}/approve")
async def skills_lifecycle_approve(name: str, request: Request,
                                   current_user: str = Depends(verify_token)):
    """批准版本 (要求 evaluated + 通过记录; 仅 admin)"""
    await require_admin(current_user)
    lc, LifecycleError = _lc()
    body = await request.json()
    version = str(body.get("version", ""))
    try:
        lc.approve(name, version, approver=current_user,
                   is_admin=lambda: True)
    except LifecycleError as e:
        raise HTTPException(400, f"{e.code}: {e.detail}")
    await _log_user_op(current_user, "skill_approve", f"{name}@{version}")
    return {"status": "ok", "name": name, "version": version}


@router.post("/api/skills/lifecycle/{name}/publish")
async def skills_lifecycle_publish(name: str, request: Request,
                                   current_user: str = Depends(verify_token)):
    """发布版本 (仅已批准版本; 旧发布版本自动 retired) — 仅 admin"""
    await require_admin(current_user)
    lc, LifecycleError = _lc()
    body = await request.json()
    version = str(body.get("version", ""))
    try:
        lc.publish(name, version, approver=current_user, is_admin=lambda: True)
    except LifecycleError as e:
        raise HTTPException(400, f"{e.code}: {e.detail}")
    await _log_user_op(current_user, "skill_publish", f"{name}@{version}")
    return {"status": "ok", "name": name, "version": version, "published": version}


@router.post("/api/skills/lifecycle/{name}/rollback")
async def skills_lifecycle_rollback(name: str, request: Request,
                                    current_user: str = Depends(verify_token)):
    """版本回退 = 发布指针回移到已批准旧版 (内容不可变) — 仅 admin"""
    await require_admin(current_user)
    lc, LifecycleError = _lc()
    body = await request.json()
    version = str(body.get("version", ""))
    try:
        lc.rollback(name, version, approver=current_user, is_admin=lambda: True)
    except LifecycleError as e:
        raise HTTPException(400, f"{e.code}: {e.detail}")
    await _log_user_op(current_user, "skill_rollback", f"{name}@{version}")
    return {"status": "ok", "name": name, "published": version}


@router.post("/api/skills/lifecycle/{name}/retire")
async def skills_lifecycle_retire(name: str,
                                  current_user: str = Depends(verify_token)):
    """停用技能 (移除线上注入; 历史版本保留) — 仅 admin"""
    await require_admin(current_user)
    lc, LifecycleError = _lc()
    try:
        lc.retire(name, approver=current_user, is_admin=lambda: True)
    except LifecycleError as e:
        raise HTTPException(400, f"{e.code}: {e.detail}")
    await _log_user_op(current_user, "skill_retire", f"{name}")
    return {"status": "ok", "name": name, "retired": True}


# ═══ 自定义 Provider 管理 ═══
@router.get("/api/llm/providers")
async def llm_providers_list(current_user: str = Depends(verify_token)):
    """获取所有 Provider（内置 + 自定义）"""
    providers = await _get_all_providers()
    return {"providers": providers}

@router.post("/api/llm/providers")
async def llm_provider_add(request: Request, current_user: str = Depends(verify_token)):
    """添加自定义 Provider"""
    await require_admin(current_user)
    body = await request.json()
    pid = body.get("id", "").strip().lower()
    name = body.get("name", "").strip()
    base_url = body.get("base_url", "").strip()
    models = body.get("models", [])
    if not pid or not name:
        return JSONResponse({"error": "id 和 name 必填"}, status_code=400)
    # 不允许覆盖内置 provider
    builtin_ids = {p["id"] for p in _BUILTIN_PROVIDERS}
    if pid in builtin_ids:
        return JSONResponse({"error": f"ID '{pid}' 与内置 Provider 冲突"}, status_code=400)
    # 规范化 base_url (剥 /chat/completions、防坏 URL; 非法 400)
    if base_url:
        try:
            base_url = _normalize_base_url(base_url)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    new_provider = {"id": pid, "name": name, "base_url": base_url, "models": models}
    r = await get_redis()
    custom_raw = await r.get(config.KEY_CUSTOM_PROVIDERS)
    custom_list = json.loads(custom_raw) if custom_raw else []
    # 如果 id 已存在则更新
    custom_list = [p for p in custom_list if p["id"] != pid]
    custom_list.append(new_provider)
    await r.set(config.KEY_CUSTOM_PROVIDERS, json.dumps(custom_list, ensure_ascii=False))
    # 同步到 PROVIDER_URL_MAP
    if base_url:
        PROVIDER_URL_MAP[pid] = base_url
    await _log_user_op(current_user, "add_provider", f"id={pid}, name={name}")
    return {"status": "ok", "provider": new_provider}

@router.delete("/api/llm/providers/{provider_id}")
async def llm_provider_delete(provider_id: str, current_user: str = Depends(verify_token)):
    """删除自定义 Provider"""
    await require_admin(current_user)
    builtin_ids = {p["id"] for p in _BUILTIN_PROVIDERS}
    if provider_id in builtin_ids:
        return JSONResponse({"error": "不能删除内置 Provider"}, status_code=400)
    r = await get_redis()
    custom_raw = await r.get(config.KEY_CUSTOM_PROVIDERS)
    custom_list = json.loads(custom_raw) if custom_raw else []
    before = len(custom_list)
    custom_list = [p for p in custom_list if p["id"] != provider_id]
    if len(custom_list) == before:
        return JSONResponse({"error": f"Provider '{provider_id}' 不存在"}, status_code=404)
    await r.set(config.KEY_CUSTOM_PROVIDERS, json.dumps(custom_list, ensure_ascii=False))
    PROVIDER_URL_MAP.pop(provider_id, None)
    await _log_user_op(current_user, "delete_provider", f"id={provider_id}")
    return {"status": "ok"}

@router.post("/api/llm/test")
async def llm_test(request: Request, current_user: str = Depends(verify_token)):
    """测试 LLM 模型连通性 — 发送简短 prompt 验证模型可用"""
    await require_admin(current_user)
    body = await request.json()
    provider = body.get("provider", "")
    model = body.get("model", "")
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "")
    oneapi_key = body.get("oneapi_key", "")
    oneapi_base_url = body.get("oneapi_base_url", "")
    if not provider or not model:
        return JSONResponse({"error": "provider 和 model 必填"}, status_code=400)
    # 密钥/URL 解析: 空或掩码哨兵 → 该 provider 的存量凭据 (记忆槽 → top-level → env)。
    # 槽感知: 切换 provider 尚未保存时, 也用被测 provider 自己的 Key, 不再误用当前 top-level。
    api_key = "" if api_key in ("", None, "__KEEP__") else api_key
    oneapi_key = "" if oneapi_key in ("", None, "__KEEP__") else oneapi_key
    r = await get_redis()
    _pref_raw = await r.get(config.KEY_LLM_PREF)
    _pref = json.loads(_pref_raw) if _pref_raw else {}
    _slot = (_pref.get("provider_creds") or {}).get(provider) or {}
    _st_url, _st_key = _resolve_provider_endpoint(provider, _pref, await _get_all_providers())
    if provider == "oneapi":
        url = (oneapi_base_url or _st_url or config.ONEAPI_BASE_URL or "").rstrip("/")
        key = oneapi_key or _st_key or config.ONEAPI_API_KEY or ""
    elif provider == "anthropic":
        url = (base_url or _st_url or "").rstrip("/")
        key = api_key or _slot.get("anthropic_key") or _pref.get("anthropic_key") \
            or config.ANTHROPIC_API_KEY or ""
    else:
        url = (base_url or _st_url or PROVIDER_URL_MAP.get(provider, "")).rstrip("/")
        key = api_key or _st_key or ""
    if not url:
        return JSONResponse({"error": f"Provider '{provider}' 无 Base URL"}, status_code=400)
    if not key:
        return JSONResponse({"error": "未设置 API Key (留空将自动使用已保存的 Key)"}, status_code=400)
    # 规范化 URL (与保存口径一致; 坏 URL 明确报错)
    try:
        url = _normalize_base_url(url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Anthropic 协议不同 (/v1/messages + x-api-key 头), 单独分支 —
    # 此前混入 OpenAI /chat/completions 格式必然失败, 误导用户以为 Key 有问题
    if provider == "anthropic":
        import httpx as _hx
        t0 = time.time()
        try:
            async with _hx.AsyncClient(timeout=30, verify=False) as client:
                resp = await client.post(
                    f"{url}/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": 10,
                          "messages": [{"role": "user", "content": "Say OK in one word."}]},
                )
            elapsed = round(time.time() - t0, 2)
            if resp.status_code == 200:
                data = resp.json()
                reply = "".join(b.get("text", "") for b in data.get("content", [])).strip()
                model_returned = str(data.get("model") or model)
                try:
                    await r.hset(config.KEY_MODEL_NAMES, model, model_returned)
                except Exception:
                    pass
                return {"status": "ok", "reply": reply, "elapsed_seconds": elapsed,
                        "usage": data.get("usage", {}), "model_returned": model_returned}
            err_text = resp.text[:500]
            try:
                _j = resp.json()
                err_text = _j.get("error", {}).get("message", err_text) if isinstance(_j.get("error"), dict) else str(_j.get("error", err_text))
            except Exception:
                pass
            return {"status": "error", "error": err_text, "status_code": resp.status_code,
                    "elapsed_seconds": elapsed}
        except Exception as e:
            return {"status": "error", "error": str(e), "elapsed_seconds": round(time.time() - t0, 2)}

    # 发送测试请求
    import httpx
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.post(
                f"{url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say OK in one word."}],
                    "max_tokens": 10,
                    "temperature": 0,
                }
            )
        elapsed = round(time.time() - t0, 2)
        if resp.status_code == 200:
            data = resp.json()
            reply = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            usage = data.get("usage", {})
            model_returned = str(data.get("model") or model)
            # 回写验证映射: 该是什么模型就是什么模型 (展示层读 zops:model_names)
            try:
                await r.hset(config.KEY_MODEL_NAMES, model, model_returned)
            except Exception:
                pass
            return {
                "status": "ok",
                "reply": reply,
                "elapsed_seconds": elapsed,
                "usage": usage,
                "model_returned": model_returned,
            }
        else:
            err_text = resp.text[:500]
            try:
                err_json = resp.json()
                err_text = err_json.get("error", {}).get("message", err_text) if isinstance(err_json.get("error"), dict) else str(err_json.get("error", err_text))
            except Exception:
                pass
            return JSONResponse({
                "status": "error",
                "error": err_text,
                "status_code": resp.status_code,
                "elapsed_seconds": elapsed,
            }, status_code=200)
    except httpx.TimeoutException:
        elapsed = round(time.time() - t0, 2)
        return JSONResponse({"status": "error", "error": f"请求超时 ({elapsed}s)", "elapsed_seconds": elapsed}, status_code=200)
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return JSONResponse({"status": "error", "error": str(e), "elapsed_seconds": elapsed}, status_code=200)

@router.get("/api/llm/models/{provider_id}")
async def llm_models_list(provider_id: str, current_user: str = Depends(verify_token)):
    """获取指定 Provider 的可用模型列表 (预设 + 记忆 + 用该 provider 凭据动态查网关)"""
    r = await get_redis()
    pref_raw = await r.get(config.KEY_LLM_PREF)
    pref = json.loads(pref_raw) if pref_raw else {}
    providers = await _get_all_providers()
    target = next((p for p in providers if p["id"] == provider_id), None)
    models = list(target["models"]) if target else []
    # 记忆槽里用过的模型
    for m in ((pref.get("provider_creds") or {}).get(provider_id) or {}).get("models") or []:
        if m not in models:
            models.append(m)
    source = "preset"
    gw_url, gw_key = _resolve_provider_endpoint(provider_id, pref, providers)
    if gw_url and gw_key:
        # 强制刷新 (绕过缓存): 用户点「刷新模型列表」就是要拿最新
        _models_cache.pop(provider_id, None)
        dynamic = await _fetch_gateway_models(provider_id, gw_url, gw_key)
        if dynamic:
            source = "gateway"
            for m in dynamic:
                if m not in models:
                    models.append(m)
    return {"provider_id": provider_id, "models": models, "source": source}


@router.put("/api/llm/providers/{provider_id}")
async def llm_provider_update(provider_id: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """编辑自定义 Provider (名称 / Base URL / 预设模型列表) — 同一 provider 接入更多模型"""
    await require_admin(current_user)
    builtin_ids = {p["id"] for p in _BUILTIN_PROVIDERS}
    if provider_id in builtin_ids:
        return JSONResponse({"error": "不能编辑内置 Provider"}, status_code=400)
    body = await request.json()
    r = await get_redis()
    custom_raw = await r.get(config.KEY_CUSTOM_PROVIDERS)
    custom_list = json.loads(custom_raw) if custom_raw else []
    target = next((p for p in custom_list if p["id"] == provider_id), None)
    if not target:
        return JSONResponse({"error": f"Provider '{provider_id}' 不存在"}, status_code=404)
    if str(body.get("name", "")).strip():
        target["name"] = body["name"].strip()
    if "base_url" in body:
        bu = str(body.get("base_url") or "").strip()
        if bu:
            try:
                bu = _normalize_base_url(bu)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        target["base_url"] = bu
        if bu:
            PROVIDER_URL_MAP[provider_id] = bu
        else:
            PROVIDER_URL_MAP.pop(provider_id, None)
    if "models" in body and isinstance(body["models"], list):
        seen, models = set(), []
        for m in body["models"]:
            m = str(m).strip()
            if m and m not in seen:
                seen.add(m)
                models.append(m)
        target["models"] = models
    await r.set(config.KEY_CUSTOM_PROVIDERS, json.dumps(custom_list, ensure_ascii=False))
    _models_cache.clear()
    await _log_user_op(current_user, "update_provider",
                       f"id={provider_id}, models={len(target.get('models') or [])}")
    return {"status": "ok", "provider": target}

# ═══ 数据脱敏 ═══
@router.get("/api/sanitize/mappings")
async def sanitize_mappings_get(current_user: str = Depends(verify_token)):
    r = await get_redis()
    raw = await r.get(f"{config.REDIS_PREFIX}:sanitize_mappings")
    mappings = json.loads(raw) if raw else {}
    return {"mappings": mappings, "count": len(mappings)}

@router.post("/api/sanitize/mappings")
async def sanitize_mappings_set(request: Request, current_user: str = Depends(verify_token)):
    body = await request.json()
    r = await get_redis()
    mappings = body.get("mappings", {})
    await r.set(f"{config.REDIS_PREFIX}:sanitize_mappings", json.dumps(mappings, ensure_ascii=False))
    return {"status": "ok", "count": len(mappings)}

@router.post("/api/sanitize/test")
async def sanitize_test(request: Request, current_user: str = Depends(verify_token)):
    from utils.sanitizer import DataSanitizer
    body = await request.json()
    text = body.get("text", "")
    r = await get_redis()
    custom_raw = await r.get(f"{config.REDIS_PREFIX}:sanitize_mappings")
    custom_mappings = json.loads(custom_raw) if custom_raw else {}
    sanitizer = DataSanitizer(custom_mappings=custom_mappings)
    masked, ctx = sanitizer.sanitize(text)
    return {
        "original_length": len(text), "masked_text": masked,
        "items_masked": ctx.count, "summary": ctx.summary(),
        "details": [{"tag": v["tag"], "placeholder": v["placeholder"]} for v in ctx.mapping.values()]
    }
