"""Z-OPS /api 自动化与通知 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from core.bizperms import require_biz_perm, require_admin_or_biz_perm  # 业务权限门 (与系统角色分离)
from fastapi import APIRouter

router = APIRouter()



# ═══ N8N 集成 API ═══
# 参考 OpenClaw n8n 集成最佳实践
@router.get("/api/n8n/status")
async def n8n_status():
    """获取 N8N 集成状态"""
    try:
        # 动态导入避免循环依赖
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        return await n8n.get_status()
    except Exception as e:
        logger.error(f"[N8N] 获取状态失败：{e}")
        return {"enabled": False, "status": "error", "error": str(e)}

@router.post("/api/n8n/webhook")
async def n8n_webhook(request: Request):
    """
    N8N Webhook 回调接口
    N8N 触发的工作流可以通过此接口回调 Z-OPS

    P0 (全面体检补漏): trigger 端点鉴权 — X-Webhook-Key 必须匹配 N8N_WEBHOOK_KEY
    (n8n 侧节点 header 配置); 未配置密钥则暂停 (fail-closed, 不裸奔)。
    驱动审批/动作的 n8n_callback 走完整 HMAC (core/n8n_callback_auth)。
    """
    _wk = (getattr(config, "N8N_WEBHOOK_KEY", "") or "").strip()
    if not _wk:
        raise HTTPException(status_code=503,
                            detail="N8N_WEBHOOK_KEY 未配置, trigger 端点已暂停 (fail-closed)")
    _got = (request.headers.get("X-Webhook-Key") or "").strip()
    if _got != _wk:
        logger.warning("[N8N] Webhook key 校验失败, 已拒绝")
        raise HTTPException(status_code=401, detail="X-Webhook-Key 校验失败")
    try:
        body = await request.json()
        logger.info(f"[N8N] 收到 Webhook: {body.get('action', 'unknown')}")
        
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        result = await n8n.process_webhook(body)
        
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[N8N] Webhook 处理失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/n8n/trigger/{workflow_name}")
async def n8n_trigger_workflow(
    workflow_name: str,
    request: Request,
    current_user: str = Depends(verify_token)
):
    """
    动态触发 N8N 工作流 (基于 WORKFLOW_REGISTRY)
    用法: POST /api/n8n/trigger/backup_check  {"gid": "46445", "site": "HKHKGBBB"}
    """
    await require_biz_perm(current_user, "n8n.operate")  # 业务操作门: 仅持「工作流编排」权限 (admin 无权限也不行)
    try:
        body = await request.json() if await request.body() else {}
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        
        # 使用 WORKFLOW_REGISTRY 动态触发
        result = await n8n.trigger_by_registry(workflow_name, body.get("params", body))
        
        return {
            "success": result.get("success", False),
            "workflow": workflow_name,
            "status": result.get("status", "triggered"),
            "approval_id": result.get("approval_id"),
            "elapsed_s": result.get("elapsed_s"),
            "message": result.get("message", ""),
        }
    except Exception as e:
        logger.error(f"[N8N] 触发工作流失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/n8n/notify")
async def n8n_notify(
    request: Request,
    current_user: str = Depends(verify_token)
):
    """
    通过 N8N 发送通知
    用法: POST /api/n8n/notify with {"title": "...", "message": "...", "channels": ["feishu"]}
    """
    try:
        body = await request.json()
        title = body.get("title", "Z-OPS 通知")
        message = body.get("message", "")
        channels = body.get("channels", ["feishu"])
        
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        result = await n8n.send_notification(title, message, channels)
        
        return {"success": result.get("success", False), "title": title}
    except Exception as e:
        logger.error(f"[N8N] 发送通知失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))

# ═══ 邮件通知 API ═══
@router.post("/api/notify/send")
async def api_notify_send(
    request: Request,
    current_user: str = Depends(verify_token)
):
    # 权限对齐 (2026-10-18 集成): 真实发送类操作 — admin 或持「通知渠道管理」权限
    await require_admin_or_biz_perm(current_user, "notify.manage")
    """
    发送邮件通知
    POST /api/notify/send {"subject": "...", "body": "...", "to": "..."}
    支持用户组: {"group": "组名"} 自动解析组内成员邮箱作为收件人
    """
    try:
        data = await request.json()
        subject = data.get("subject", "Z-OPS 通知")
        body = data.get("body", "")
        to = data.get("to", "")
        group = data.get("group", "")
        # 用户组发送: group -> 解析成员邮箱
        if group and not to:
            r = await get_redis()
            g_raw = await r.hget(config.KEY_NOTIFY_GROUPS, group)
            if not g_raw:
                raise HTTPException(status_code=404, detail=f"通知用户组 '{group}' 不存在")
            g = json.loads(g_raw)
            mails = [m.get("mail", "") for m in g.get("members", []) if m.get("mail")]
            if not mails:
                raise HTTPException(status_code=400, detail=f"用户组 '{group}' 没有配置邮箱的成员")
            to = ";".join(mails)
        if not to:
            to = config.ADMIN_NOTIFY_EMAIL
        if not body:
            raise HTTPException(status_code=400, detail="缺少邮件内容 (body)")
        result = send_notify_email(subject, body, to)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MAIL API] 发送失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notify/test/{channel}")
async def api_notify_test(
    channel: str,
    request: Request,
    current_user: str = Depends(verify_token)
):
    # 权限对齐 (2026-10-18 集成): 测试发送 — admin 或持「通知渠道管理」权限
    await require_admin_or_biz_perm(current_user, "notify.manage")
    """
    测试通知渠道
    POST /api/notify/test/email  (or feishu/wecom/webhook)
    """
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if channel == "email":
        # 获取前端传入的 SMTP 配置 (可选)
        try:
            data = await request.json()
        except Exception:
            data = {}
        to = data.get("to", config.ADMIN_NOTIFY_EMAIL)

        html = f'''<div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
  <div style="background: #1e40af; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
    <h2 style="margin:0;">Z-OPS 运维智能体 - 邮件测试</h2>
  </div>
  <div style="padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
    <p>这是一封来自 <strong>Z-OPS 运维智能体平台</strong> 的测试邮件。</p>
    <table style="width:100%; border-collapse:collapse; margin:16px 0;">
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">发送时间</td><td style="padding:8px; border:1px solid #e5e7eb;">{now}</td></tr>
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">发件人</td><td style="padding:8px; border:1px solid #e5e7eb;">{config.SMTP_FROM}</td></tr>
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">服务器</td><td style="padding:8px; border:1px solid #e5e7eb;">opscore-ai.example.com</td></tr>
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">MX 直投</td><td style="padding:8px; border:1px solid #e5e7eb;">{"开启" if config.SMTP_MX_DIRECT else "关闭"}</td></tr>
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">测试操作人</td><td style="padding:8px; border:1px solid #e5e7eb;">{current_user}</td></tr>
      <tr><td style="padding:8px; border:1px solid #e5e7eb; background:#f9fafb; font-weight:600;">结果</td><td style="padding:8px; border:1px solid #e5e7eb; color:green; font-weight:600;">✅ 邮件通知功能正常</td></tr>
    </table>
    <p style="color:#6b7280; font-size:12px;">此邮件由 Z-OPS 平台自动发送，请勿回复。</p>
  </div>
</div>'''
        result = send_notify_email(f"[Z-OPS] 邮件通知测试 - {now}", html, to)
        return result

    elif channel == "feishu":
        # 飞书测试通知
        try:
            from channels.feishu import FeishuChannel
            ch = FeishuChannel()
            await ch.send_notification(f"Z-OPS 测试通知 - {now}", "feishu_test")
            return {"success": True, "channel": "feishu", "time": now}
        except Exception as e:
            return {"success": False, "channel": "feishu", "error": str(e)}

    else:
        return {"success": False, "error": f"不支持的通知渠道: {channel}", "supported": ["email", "feishu"]}

@router.get("/api/notify/config")
async def api_notify_config(current_user: str = Depends(verify_token)):
    """获取当前通知配置状态 (admin 或持「通知渠道管理」权限 — 与通知测试/配置页权限一致)"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    return {
        "email": {
            "enabled": True,
            "smtp_host": config.SMTP_HOST,
            "smtp_port": config.SMTP_PORT,
            "smtp_from": config.SMTP_FROM,
            "mx_direct": config.SMTP_MX_DIRECT,
            "admin_email": config.ADMIN_NOTIFY_EMAIL,
        },
        "feishu": {
            "enabled": bool(os.getenv("FEISHU_WEBHOOK_URL")),
        },
    }

# ═══ 通知用户组 (通知人管理) ═══
@router.get("/api/notify/groups")
async def notify_groups_list(current_user: str = Depends(verify_token)):
    """列出所有通知用户组"""
    await require_admin_or_biz_perm(current_user, "notify.manage")  # 查看/审计门
    r = await get_redis()
    raw = await r.hgetall(config.KEY_NOTIFY_GROUPS)
    groups = []
    for _k, d in raw.items():
        try:
            groups.append(json.loads(d))
        except json.JSONDecodeError:
            continue
    groups.sort(key=lambda g: g.get("created_at", ""))
    return {"groups": groups, "count": len(groups)}

@router.post("/api/notify/groups")
async def notify_groups_save(request: Request, current_user: str = Depends(verify_token)):
    """创建/更新通知用户组
    {"name": "...", "description": "...", "members": [{username, display_name, mail}]}"""
    await require_admin_or_biz_perm(current_user, "notify.manage")  # 业务操作门 (admin 或持权者; 原 require_biz_perm 会把不持权的 admin 也拒掉)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "缺少用户组名称")
    members = []
    seen = set()
    for m in body.get("members", []) or []:
        uname = str(m.get("username", "")).strip()
        if not uname or uname in seen:
            continue
        seen.add(uname)
        members.append({
            "username": uname,
            "display_name": str(m.get("display_name", "")).strip() or uname,
            "mail": str(m.get("mail", "")).strip(),
        })
    r = await get_redis()
    existing_raw = await r.hget(config.KEY_NOTIFY_GROUPS, name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    group = {
        "name": name,
        "description": str(body.get("description", "")).strip(),
        "members": members,
        "created_at": json.loads(existing_raw).get("created_at", now) if existing_raw else now,
        "updated_at": now,
        "updated_by": current_user,
    }
    await r.hset(config.KEY_NOTIFY_GROUPS, name, json.dumps(group, ensure_ascii=False))
    logger.info(f"[NOTIFY] 用户组 '{name}' 已保存 ({len(members)} 名成员), 操作人: {current_user}")
    return {"success": True, "group": group}

@router.delete("/api/notify/groups/{name}")
async def notify_groups_delete(name: str, current_user: str = Depends(verify_token)):
    """删除通知用户组"""
    await require_admin_or_biz_perm(current_user, "notify.manage")  # 业务操作门 (admin 或持权者; 原 require_biz_perm 会把不持权的 admin 也拒掉)
    r = await get_redis()
    removed = await r.hdel(config.KEY_NOTIFY_GROUPS, name)
    if not removed:
        raise HTTPException(404, f"用户组 '{name}' 不存在")
    logger.info(f"[NOTIFY] 用户组 '{name}' 已删除, 操作人: {current_user}")
    return {"success": True, "name": name}

@router.get("/api/notify/ldap/users")
async def notify_ldap_users(keyword: str = "", limit: int = 20,
                            current_user: str = Depends(verify_token)):
    """搜索可加入通知组的用户: 优先 LDAP/AD，LDAP 无结果时回退本地已授权用户"""
    await require_admin_or_biz_perm(current_user, "notify.manage")  # 查看/审计门
    users = ldap_search_users(keyword, limit)
    source = "ldap" if users else "local"
    if not users:
        # LDAP 未启用/无结果 → 回退本地用户
        r = await get_redis()
        raw = await r.hgetall(config.KEY_USERS)
        kw = keyword.strip().lower()
        for uname, d in raw.items():
            try:
                u = json.loads(d)
            except json.JSONDecodeError:
                continue
            disp = u.get("display_name", "") or ""
            mail = u.get("mail", "") or ""
            if not kw or kw in uname.lower() or kw in disp.lower() or kw in mail.lower():
                users.append({"username": uname, "display_name": disp, "mail": mail, "source": "local"})
        users.sort(key=lambda x: x["username"])
    limit = max(1, min(int(limit or 20), 100))
    return {"users": users[:limit], "count": len(users[:limit]), "source": source}

@router.get("/api/n8n/registry")
async def n8n_workflow_registry(current_user: str = Depends(verify_token)):
    """获取工作流注册表 (所有可触发的工作流)"""
    try:
        from core.n8n_workflows import get_all_workflows, WORKFLOW_CATEGORIES
        workflows = get_all_workflows()
        return {
            "categories": WORKFLOW_CATEGORIES,
            "workflows": workflows,
            "count": len(workflows),
        }
    except Exception as e:
        logger.error(f"[N8N] 获取注册表失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/n8n/workflows")
async def n8n_list_workflows(current_user: str = Depends(verify_token)):
    """
    获取 N8N 实例中的工作流列表
    先从 N8N API 获取，失败则回退到本地注册表
    """
    try:
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        workflows = await n8n.get_workflows()
        if workflows:
            return {"workflows": workflows, "source": "n8n_api", "count": len(workflows)}
        # 回退到本地注册表
        from core.n8n_workflows import get_all_workflows
        local = get_all_workflows()
        return {"workflows": local, "source": "local_registry", "count": len(local)}
    except Exception as e:
        logger.warning(f"[N8N] 获取工作流列表失败，回退本地: {e}")
        from core.n8n_workflows import get_all_workflows
        local = get_all_workflows()
        return {"workflows": local, "source": "local_registry", "count": len(local)}

@router.get("/api/n8n/executions")
async def n8n_list_executions(
    workflow_id: str = None,
    limit: int = 20,
    current_user: str = Depends(verify_token)
):
    """
    获取 N8N 工作流执行记录
    """
    try:
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        executions = await n8n.get_executions(workflow_id=workflow_id, limit=limit)
        return {"executions": executions, "count": len(executions)}
    except Exception as e:
        logger.error(f"[N8N] 获取执行记录失败：{e}")
        return {"executions": [], "count": 0, "error": str(e)}

@router.post("/api/n8n/workflows/{workflow_id}/activate")
async def n8n_activate_workflow(workflow_id: str, current_user: str = Depends(verify_token)):
    """激活 N8N 工作流"""
    await require_biz_perm(current_user, "n8n.manage")  # 工作流定义管理门 (与触发 n8n.operate 分离)
    try:
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        ok = await n8n.activate_workflow(workflow_id, active=True)
        return {"success": ok, "workflow_id": workflow_id, "active": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/n8n/workflows/{workflow_id}/deactivate")
async def n8n_deactivate_workflow(workflow_id: str, current_user: str = Depends(verify_token)):
    """停用 N8N 工作流"""
    await require_biz_perm(current_user, "n8n.manage")  # 工作流定义管理门 (与触发 n8n.operate 分离)
    try:
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        ok = await n8n.activate_workflow(workflow_id, active=False)
        return {"success": ok, "workflow_id": workflow_id, "active": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══ N8N 回调端点 (Human-in-the-loop 审批流) ═══
@router.post("/api/webhook/n8n_callback")
async def n8n_callback(request: Request):
    """
    N8N 回调端点
    用于 N8N 工作流执行完成后回调 Z-OPS，包括审批结果通知

    P0 安全门 (core/n8n_callback_auth):
    - HMAC 签名校验 (方法/路径/原始 body/时间戳/nonce), 时间窗 ±5 分钟;
    - nonce 防重放 + 回调事件持久幂等去重;
    - 密钥未配置 → 端点暂停 (503), 不开放匿名回退;
    - 回调主体是 n8n 服务, body 内 approver 等字段为数据而非认证主体;
    - approval_result 必须绑定已存在的审批单; workflow_result 必须带 execution_id。
    """
    from core.n8n_callback_auth import verify_n8n_callback, claim_callback_event
    raw_body = await request.body()
    ok, code, reason = await verify_n8n_callback(request, raw_body)
    if not ok:
        raise HTTPException(status_code=code, detail=reason)
    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="body 不是合法 JSON")
    callback_type = body.get("callback_type", "workflow_result")
    logger.info(f"[N8N Callback] 类型: {callback_type}, 数据: {body}")

    try:
        if callback_type == "approval_result":
            # 人工审批结果回调
            approval_id = body.get("approval_id")
            approved = body.get("approved", False)
            approver = body.get("approver", "unknown")   # 数据字段, 不作为认证主体
            comment = body.get("comment", "")
            if not approval_id:
                raise HTTPException(status_code=400, detail="缺少 approval_id (绑定校验)")
            # 绑定校验: 审批单必须真实存在 (伪造/过期单据直接拒绝)
            from core.approval import get_approval
            rec = get_approval(approval_id)
            if not rec:
                raise HTTPException(status_code=404,
                                    detail=f"审批单 {approval_id} 不存在或已过期")
            # 持久幂等: 同一单据同一决策只处理一次 (重试回调不再重复触发)
            first = await claim_callback_event("approval_result",
                                               f"{approval_id}:{bool(approved)}")
            if not first:
                logger.info(f"[N8N Callback] 重复回调 (幂等忽略): {approval_id}")
                return {"success": True, "approval_id": approval_id,
                        "processed": False, "reason": "duplicate"}

            # 存储审批结果到 Redis
            r = await get_redis()
            await r.set(f"approval:{approval_id}", json.dumps({
                "approved": approved,
                "approver": approver,
                "comment": comment,
                "timestamp": body.get("timestamp")
            }), ex=3600)  # 1小时过期

            # 如果审批通过，通知相关 Agent 继续执行
            # P0: 主体为 n8n 服务 — approver 仅作数据透传, 不再作为自报 username
            if approved:
                task_id = body.get("task_id")
                if task_id:
                    msg = AgentMessage(
                        source="n8n_callback",
                        target="orchestrator",
                        action="approval_approved",
                        text=f"审批通过: {approval_id}",
                        params={"approval_id": approval_id, "task_id": task_id,
                                "approver": approver},
                        channel_source="n8n",
                        session_id=rec.get("session_id") or "",
                    )
                    await r.publish(config.channel("orchestrator"), msg.to_json())

            return {"success": True, "approval_id": approval_id, "processed": True}

        elif callback_type == "workflow_result":
            # 工作流执行结果回调 (绑定校验: 必须携带 execution_id)
            workflow_id = body.get("workflow_id")
            execution_id = body.get("execution_id")
            status = body.get("status", "completed")
            result = body.get("result", {})
            if not execution_id:
                raise HTTPException(status_code=400, detail="缺少 execution_id (绑定校验)")
            first = await claim_callback_event("workflow_result",
                                               f"{execution_id}:{status}")
            if not first:
                logger.info(f"[N8N Callback] 重复回调 (幂等忽略): execution={execution_id}")
                return {"success": True, "execution_id": execution_id, "status": status,
                        "processed": False, "reason": "duplicate"}

            # 记录执行结果
            r = await get_redis()
            await r.set(f"workflow_result:{execution_id}", json.dumps({
                "workflow_id": workflow_id,
                "status": status,
                "result": result,
                "timestamp": body.get("timestamp")
            }), ex=3600)

            return {"success": True, "execution_id": execution_id, "status": status}

        elif callback_type == "alert":
            # 告警触发回调 (n8n 服务主体转发, 不携带个人主体)
            alert_type = body.get("alert_type")
            alert_data = body.get("data", {})
            _dedup_key = (f"{alert_type}|"
                          + json.dumps(alert_data, ensure_ascii=False, sort_keys=True)[:200])
            first = await claim_callback_event("alert", _dedup_key)
            if not first:
                return {"success": True, "alert_type": alert_type,
                        "forwarded": False, "reason": "duplicate"}

            # 转发给 ops_agent 进行诊断
            msg = AgentMessage(
                source="n8n_alert",
                target="ops_agent",
                action="diagnose",
                text=f"收到告警: {alert_type}",
                params={"alert_type": alert_type, "alert_data": alert_data},
                channel_source="n8n",
            )
            r = await get_redis()
            await r.publish(config.channel("ops_agent"), msg.to_json())

            return {"success": True, "alert_type": alert_type, "forwarded": True}

        return {"success": False, "error": "未知回调类型"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[N8N Callback] 处理失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
