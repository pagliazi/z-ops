"""Z-OPS v4.1 (P0-A) — 请求上下文隔离 (RequestContext)

背景 (SPEC P0「请求隔离与会话访问」):
  BaseAgent 为单例, 旧实现把 _current_username/_current_session_id/_current_msg_id
  挂在 self 上, 同一 Agent 并发处理多条消息 (asyncio.create_task(self._safe_handle))
  时在 await 点互相覆盖 — 用户 A 的请求可能以用户 B 的身份记账/查权限。

设计:
  - RequestContext: 不可变 (frozen) 数据类, 承载请求/任务/执行关联、actor、
    服务主体、可信委托引用、channel、session、audience、权限版本、scope、
    deadline、预算与取消关联。不含任何原始凭据 (最小权限红线)。
  - ContextVar 承载: 每个 asyncio task 拥有独立 context 拷贝, set 不外溢;
    绑定采用 token + finally reset, 覆盖初始化异常路径。
  - 旧实例属性 (_current_username 等) 仅作兼容回退读取, 不再是权威来源。
  - 线程显式传播: ContextVar 不随 Thread/executor 自动传播,
    run_with_context() 用 contextvars.copy_context() 显式带过去。

服务端认证产生主体: RequestContext.actor 只能由认证层 (verify_token /
飞书回调 / n8n 签名校验) 构造, 禁止信任客户端自报 username 字段。
"""

import asyncio
import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("zops")


# ═══ scope 语义 (SPEC 统一契约: ALL / NONE / SCOPED / UNKNOWN) ═══

class ScopeMode(str, Enum):
    ALL = "all"          # 不限 (显式配置为空)
    NONE = "none"        # 空交集/显式拒绝 — 空集不是不限
    SCOPED = "scoped"    # 限定范围
    UNKNOWN = "unknown"  # 未知 — 未知不能解释为不限


@dataclass(frozen=True)
class ScopeSpec:
    """单维度范围: mode + 显式值列表 (规范化后的三字码/GID)"""
    mode: ScopeMode = ScopeMode.UNKNOWN
    values: tuple = ()

    @classmethod
    def all(cls) -> "ScopeSpec":
        return cls(mode=ScopeMode.ALL)

    @classmethod
    def none(cls) -> "ScopeSpec":
        return cls(mode=ScopeMode.NONE)

    @classmethod
    def scoped(cls, values) -> "ScopeSpec":
        vals = tuple(dict.fromkeys(str(v).strip() for v in (values or []) if str(v).strip()))
        # 归一化后为空的 SCOPED = 未知 (不能当不限)
        return cls(mode=ScopeMode.SCOPED, values=vals) if vals else cls(mode=ScopeMode.UNKNOWN)

    def allows(self, value: str, aliases: Optional[dict] = None) -> bool:
        """单值校验: ALL→True; NONE/UNKNOWN→False; SCOPED→命中"""
        if self.mode == ScopeMode.ALL:
            return True
        if self.mode != ScopeMode.SCOPED:
            return False
        v = str(value or "").strip()
        if not v:
            return False  # 资源信息缺失 + SCOPED → 拒绝 (SPEC: 缺失不跳过限制)
        amap = aliases or {}
        def _norm(x: str) -> str:
            x = str(x or "").strip()
            return amap.get(x, x)
        vn = _norm(v)
        for x in self.values:
            xn = _norm(x)
            if vn == xn:
                return True
            # 城市三字码 ⊂ 完整站点代码 (HKG ⊂ HKHKGBBB), 与 bizperms 现行互认规则一致
            if len(xn) == 3 and xn in vn:
                return True
        return False


# ═══ 不可变请求上下文 ═══

@dataclass(frozen=True)
class RequestContext:
    """一次请求/任务的完整关联 (无凭据)

    - request_id / task_id / run_id: 请求-任务-执行三级关联 (P1 持久任务沿用)
    - actor: 认证产生的用户主体 (服务端 verify_token 等)
    - service_principal: 服务身份 (NMS 周期任务/n8n 回调等, 与 actor 二选一或并存)
    - delegation_ref: 可信委托引用 (子任务代父执行时的父上下文指纹)
    - audience: 输出受众 (群会话=组共同可见; 控制缓存/历史注入边界)
    - authz_version: 权限快照版本 (撤权失效判定)
    - scopes: {sites: ScopeSpec, customers: ScopeSpec}
    - deadline_ts / budget_tokens / cancel_key: 截止/预算/取消关联 (P1 预算结算沿用)
    """
    request_id: str = ""
    task_id: str = ""
    run_id: str = ""
    actor: str = ""                     # 用户主体 (认证产生; 禁止客户端自报)
    service_principal: str = ""         # 服务主体 (显式配置授权)
    delegation_ref: str = ""            # 委托引用 (父 request_id)
    channel: str = ""                   # webchat / feishu / oneapi / n8n / nms
    session_id: str = ""
    audience: str = ""                  # "" = 私有 (仅 actor); 群会话填群标识
    authz_version: str = ""
    scopes: dict = field(default_factory=dict)   # {dim: ScopeSpec}
    deadline_ts: float = 0.0            # 0 = 未设置
    budget_tokens: int = 0              # 0 = 未设置
    cancel_key: str = ""                # 取消关联 (Redis key / task registry id)
    msg_id: str = ""                    # 兼容: 触发消息 id

    def __post_init__(self):
        if not self.request_id:
            object.__setattr__(self, "request_id", f"req_{uuid.uuid4().hex[:12]}")

    # ── 主体判定 ──
    @property
    def principal(self) -> str:
        """生效主体: 用户 actor 优先, 否则服务主体"""
        return self.actor or self.service_principal

    def is_service(self) -> bool:
        return bool(self.service_principal) and not self.actor

    # ── 兼容读取 (旧 _current_* 接口) ──
    @property
    def username(self) -> str:
        return self.actor

    def to_public_dict(self) -> dict:
        """对外/日志安全的投影 (无凭据字段 — 本类即不含凭据, 全量可出)"""
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "actor": self.actor,
            "service_principal": self.service_principal,
            "channel": self.channel,
            "session_id": self.session_id,
            "audience": self.audience,
            "authz_version": self.authz_version,
            "msg_id": self.msg_id,
        }

    def derive_child(self, **overrides) -> "RequestContext":
        """派生子任务上下文: 权限/受众/范围不扩大 (SPEC P3 子任务交集原则)

        子上下文只能收窄: delegation_ref 指向父 request_id, deadline/budget 取更紧。
        """
        base = dict(
            task_id=overrides.pop("task_id", self.task_id),
            run_id=overrides.pop("run_id", ""),
            delegation_ref=self.request_id,
            actor=self.actor,
            service_principal=self.service_principal,
            channel=self.channel,
            session_id=self.session_id,
            audience=self.audience,
            authz_version=self.authz_version,
            scopes=self.scopes,
            deadline_ts=self.deadline_ts,
            budget_tokens=self.budget_tokens,
            cancel_key=self.cancel_key,
            msg_id=overrides.pop("msg_id", self.msg_id),
        )
        # deadline/budget 只收紧不放宽
        if "deadline_ts" in overrides:
            d = float(overrides.pop("deadline_ts") or 0)
            base["deadline_ts"] = min(d, self.deadline_ts) if (d and self.deadline_ts) else (d or self.deadline_ts)
        if "budget_tokens" in overrides:
            b = int(overrides.pop("budget_tokens") or 0)
            base["budget_tokens"] = min(b, self.budget_tokens) if (b and self.budget_tokens) else (b or self.budget_tokens)
        base.update(overrides)
        return RequestContext(**base)


# ═══ ContextVar 承载 + 兼容接口 ═══

_ctx_var: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar("zops_request_ctx", default=None)


def current_request_context() -> Optional[RequestContext]:
    """当前上下文 (无则 None — 调用方按未认证/后台任务处理, 不得默认放行)"""
    return _ctx_var.get()


def current_username() -> str:
    """兼容读取: 当前 actor (无上下文返回空串)"""
    ctx = _ctx_var.get()
    return ctx.actor if ctx else ""


def current_session_id() -> str:
    ctx = _ctx_var.get()
    return ctx.session_id if ctx else ""


def current_msg_id() -> str:
    ctx = _ctx_var.get()
    return ctx.msg_id if ctx else ""


def bind_request_context(ctx: RequestContext) -> contextvars.Token:
    """绑定上下文, 返回 token (必须配 reset_request_context(token) 于 finally)"""
    return _ctx_var.set(ctx)


def reset_request_context(token: contextvars.Token) -> None:
    """解绑 (覆盖初始化与异常路径 — 始终在 finally 调用)"""
    try:
        _ctx_var.reset(token)
    except (ValueError, LookupError) as e:  # token 失效 (context 已切换) — 记录不抛
        logger.debug(f"[request_context] reset 失败 (token 不属于当前 context): {e}")


@contextmanager
def request_scope(ctx: RequestContext):
    """同步/异步通用的绑定作用域

    async with request_scope(ctx): ... / with request_scope(ctx): ...
    退出时无条件 reset, 覆盖异常路径。
    """
    token = bind_request_context(ctx)
    try:
        yield ctx
    finally:
        reset_request_context(token)


# ═══ 线程/executor 显式传播 ═══

def run_with_context(fn: Callable, *args, _ctx: Optional[RequestContext] = None, **kwargs) -> Any:
    """在线程中带上下文执行 fn (contextvars 不随 Thread 自动传播)

    用法: executor.submit(run_with_context, my_fn, arg1, _ctx=ctx)
    """
    ctx = _ctx or current_request_context()

    def _wrapped():
        if ctx is None:
            return fn(*args, **kwargs)
        token = _ctx_var.set(ctx)
        try:
            return fn(*args, **kwargs)
        finally:
            _ctx_var.reset(token)

    return _wrapped()


def copy_context_runner(ctx: Optional[RequestContext]) -> Callable:
    """返回在拷贝 context 中执行任意 fn 的 runner (to_thread 场景)

    用法: await asyncio.to_thread(copy_context_runner(ctx), fn, *args)
    """
    ctx = ctx or current_request_context()
    if ctx is None:
        return lambda fn, *a, **kw: fn(*a, **kw)
    var = _ctx_var

    def _run(fn, *a, **kw):
        token = var.set(ctx)
        try:
            return fn(*a, **kw)
        finally:
            var.reset(token)
    return _run


def context_from_agent_message(msg) -> RequestContext:
    """从 AgentMessage 构造 RequestContext (BaseAgent._safe_handle 入口)

    msg.username 来自服务端认证链路 (webchat verify_token / 飞书回调 /
    oneapi token 校验后写入), 非客户端自报字段。
    """
    prov = dict(getattr(msg, "provenance", None) or {})
    return RequestContext(
        actor=(getattr(msg, "username", "") or "").strip(),
        channel=getattr(msg, "channel_source", "") or "",
        session_id=getattr(msg, "session_id", "") or "",
        msg_id=getattr(msg, "msg_id", "") or "",
        audience=str(prov.get("audience") or ""),
        deadline_ts=time.time() + 3600,  # 默认 1h 硬顶 (P1 接管前兜底)
    )
