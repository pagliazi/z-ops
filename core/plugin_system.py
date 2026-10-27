"""Z-OPS Plugin System — "Everything is a Plugin" (参考 deepseek-ai/deepseek-harness 设计)

2026-10-22, 参考 DeepSeek Harness (dsh) 的 Cordis 架构移植到 Z-OPS (Python):

dsh 五个核心概念 → Z-OPS 对应实现:
  1. 插件是 Service 对象        → Plugin 基类, 生命周期由 PluginManager 驱动
  2. 上下文是服务容器 (ctx.key) → PluginContext.provide/require (稳定 key, 按名查找不导入实现)
  3. inject 声明服务依赖        → Plugin.inject, 依赖未就绪则等待 (拓扑序启动)
  4. 类型化事件通信             → EventBus 五种分发模式: emit/waterfall/parallel/serial/bail
  5. 注册是可逆副作用           → ctx.effect(disposer); disable/unload 逆序撤销

与 dsh 的差异 (刻意裁剪):
  - 无网络安装 (最小权限红线): 插件只从 plugins/ 本地目录加载, 管理页可启停
  - patch 层简化为 plugins/patch.yaml ({id: disabled} 映射), 对应 dsh cordis.patch.yml
  - 内置插件经过与外部插件相同的生命周期 (dsh: "产品的每一部分都是插件")

事件缝 (与 Z-OPS 现有管道对齐, 参考 dsh tools/ 三段瀑布):
  - session/summary_stored  (emit)      会话摘要落盘后 — wiki 编译等后处理
  - chat/pre_task           (bail)      chat 任务入队前 — 策略守卫可拒绝
  - tools/pre_execute       (bail)      能力执行前 — 策略/审批增强
  - tools/post_execute      (emit)      能力执行后 — 审计/指标
"""

import asyncio
import importlib
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("zops")

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
PATCH_FILE = PLUGINS_DIR / "patch.yaml"


# ═══════════════════════════════════════════════════════════════
# EventBus — 类型化事件, 五种分发模式 (对齐 Cordis 语义)
# ═══════════════════════════════════════════════════════════════

EVENT_MODES = {
    "session/summary_stored": "emit",
    "chat/pre_task": "bail",
    "tools/pre_execute": "bail",
    "tools/post_execute": "emit",
}


class EventBus:
    """按事件注册监听器; 每个事件有一种分发模式 (公开约定的一部分)"""

    def __init__(self, modes: dict = None):
        self._modes: dict = dict(modes or EVENT_MODES)
        self._listeners: dict = {}   # event -> list[(plugin_id, fn)]

    def declare(self, event: str, mode: str):
        """声明新事件 (插件可扩展事件域 — dsh: 事件就是扩展点)"""
        if event not in self._modes:
            self._modes[event] = mode

    def mode_of(self, event: str) -> str:
        return self._modes.get(event, "emit")

    def on(self, event: str, fn: Callable, plugin_id: str = "",
           prepend: bool = False) -> Callable:
        """注册监听器, 返回 disposer (可逆副作用 — dsh: 注册即副作用)"""
        self._modes.setdefault(event, "emit")
        lst = self._listeners.setdefault(event, [])
        if prepend:
            lst.insert(0, (plugin_id, fn))
        else:
            lst.append((plugin_id, fn))

        def _dispose():
            try:
                lst.remove((plugin_id, fn))
            except ValueError:
                pass
        return _dispose

    def listeners_of(self, event: str) -> list:
        return [fn for _, fn in self._listeners.get(event, [])]

    # ── emit: 按序观察, 不等待, 无返回值 ──
    async def emit(self, event: str, *args):
        for fn in self.listeners_of(event):
            try:
                r = fn(*args)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:
                logger.warning(f"[plugins] {event} 监听器异常 (不阻断): {e}")

    # ── parallel: 并行扇出, 等全部完成 ──
    async def parallel(self, event: str, *args) -> list:
        results = await asyncio.gather(
            *[_safe(fn, event, *args) for fn in self.listeners_of(event)],
            return_exceptions=True)
        return results

    # ── serial: 按序等待, 后者可见前者返回值 (追加式管道) ──
    async def serial(self, event: str, value=None, *args):
        for fn in self.listeners_of(event):
            try:
                r = fn(value, *args)
                if asyncio.iscoroutine(r):
                    r = await r
                if r is not None:
                    value = r
            except Exception as e:
                logger.warning(f"[plugins] {event} serial 监听器异常: {e}")
        return value

    # ── bail: 顺序执行直到某个监听器返回 bail 值 ──
    async def bail(self, event: str, *args):
        """返回 (bailed, value): bailed=True 表示有监听器做了决定"""
        for fn in self.listeners_of(event):
            try:
                r = fn(*args)
                if asyncio.iscoroutine(r):
                    r = await r
            except Exception as e:
                logger.warning(f"[plugins] {event} bail 监听器异常: {e}")
                continue
            if r:   # 非空/非 False 即 bail 值
                return True, r
        return False, None

    # ── waterfall: 环绕中间件 (dsh 语义) ──
    async def waterfall(self, event: str, value, *args):
        """监听器签名 (value, ctx_obj, next); next(value) 委托下游;
        不调用 next() 直接返回 = 短路 (策略监听器拥有决定权时)"""
        fns = self.listeners_of(event)

        async def dispatch(i, val):
            if i >= len(fns):
                return val
            fn = fns[i]

            async def nxt(v=None):
                return await dispatch(i + 1, v if v is not None else val)
            r = fn(val, args, nxt)
            if asyncio.iscoroutine(r):
                r = await r
            return r if r is not None else val

        try:
            return await dispatch(0, value)
        except Exception as e:
            logger.warning(f"[plugins] {event} waterfall 异常: {e}")
            return value


async def _safe(fn, event, *args):
    try:
        r = fn(*args)
        if asyncio.iscoroutine(r):
            return await r
        return r
    except Exception as e:
        return e


# ═══════════════════════════════════════════════════════════════
# PluginContext — 服务容器 (ctx.key 查找, 不导入具体实现)
# ═══════════════════════════════════════════════════════════════

class PluginContext:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._services: dict = {}
        self._disposers: dict = {}     # plugin_id -> [disposer]
        self._contributions: dict = {} # plugin_id -> [{kind, id, detail}]

    # ── 服务注册 (宿主预置; 插件也可贡献新服务) ──
    def provide(self, key: str, service: Any):
        self._services[key] = service

    def require(self, key: str) -> Any:
        if key not in self._services:
            raise KeyError(f"服务 '{key}' 未注册 (插件 inject 依赖未就绪)")
        return self._services[key]

    def has(self, key: str) -> bool:
        return key in self._services

    # ── 可逆副作用 ──
    def effect(self, plugin_id: str, disposer: Callable):
        self._disposers.setdefault(plugin_id, []).append(disposer)

    def contribute(self, plugin_id: str, kind: str, cid: str, detail: str = ""):
        """记录贡献 (管理页展示); 撤销在 teardown 时按 disposer 完成"""
        self._contributions.setdefault(plugin_id, []).append(
            {"kind": kind, "id": cid, "detail": detail})

    def teardown_plugin(self, plugin_id: str):
        """逆序执行该插件的全部 disposer (dsh: 卸载时撤销副作用)"""
        for d in reversed(self._disposers.get(plugin_id, [])):
            try:
                d()
            except Exception as e:
                logger.warning(f"[plugins] {plugin_id} disposer 异常: {e}")
        self._disposers[plugin_id] = []
        self._contributions[plugin_id] = []


# ═══════════════════════════════════════════════════════════════
# Plugin 基类
# ═══════════════════════════════════════════════════════════════

class Plugin:
    """插件 = Service 对象。子类实现 apply(ctx) 注册副作用;
    返回值可作为顶层 disposer (或用 ctx.effect 注册多个)"""
    id: str = ""
    version: str = "0.0.0"
    description: str = ""
    inject: list = []          # 依赖的 ctx 服务 key (未就绪则等待)
    builtin: bool = True

    async def apply(self, ctx: PluginContext):
        return None


# ═══════════════════════════════════════════════════════════════
# PluginManager — 发现/加载/启停/patch 覆盖
# ═══════════════════════════════════════════════════════════════

class PluginState:
    DISCOVERED = "discovered"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


class PluginManager:
    def __init__(self, ctx: PluginContext, plugins_dir: Path = PLUGINS_DIR,
                 patch_file: Path = PATCH_FILE):
        self.ctx = ctx
        self.plugins_dir = plugins_dir
        self.patch_file = patch_file
        self._plugins: dict = {}     # id -> Plugin 实例
        self._states: dict = {}      # id -> state
        self._errors: dict = {}      # id -> str
        self._lock = asyncio.Lock()

    # ── patch 层 (dsh cordis.patch.yml 最小版) ──
    def _read_patch(self) -> dict:
        if not self.patch_file.exists():
            return {}
        try:
            data = yaml.safe_load(self.patch_file.read_text()) or {}
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[plugins] patch 读取失败 (忽略): {e}")
            return {}

    def _write_patch(self, patch: dict):
        self.patch_file.parent.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in patch.items() if v is not None}
        self.patch_file.write_text(
            yaml.safe_dump(clean, allow_unicode=True, sort_keys=True))

    def is_disabled_by_patch(self, pid: str) -> bool:
        return bool(self._read_patch().get(pid, {}).get("disabled", False))

    # ── 发现 (扫描 plugins/*/plugin.yaml) ──
    def discover(self) -> list:
        out = []
        if not self.plugins_dir.exists():
            return out
        for mf in sorted(self.plugins_dir.glob("*/plugin.yaml")):
            try:
                meta = yaml.safe_load(mf.read_text()) or {}
                pid = str(meta.get("id") or mf.parent.name)
                out.append({"id": pid,
                            "version": str(meta.get("version", "0.0.0")),
                            "description": str(meta.get("description", "")),
                            "entry": str(meta.get("entry", "")),
                            "inject": list(meta.get("inject") or []),
                            "builtin": True,
                            "manifest": str(mf)})
            except Exception as e:
                out.append({"id": mf.parent.name, "error": f"清单解析失败: {e}",
                            "manifest": str(mf)})
        return out

    def _load_class(self, entry: str) -> type:
        """entry 格式: 'module.path:ClassName' (只允许 plugins/ 目录内的模块)"""
        mod_name, _, cls_name = entry.partition(":")
        if not cls_name:
            raise ValueError(f"entry 格式错误: {entry} (应为 module:Class)")
        if not (mod_name == "plugins" or mod_name.startswith("plugins.")):
            raise ValueError(f"仅允许 plugins/ 目录内模块: {mod_name}")
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        if not (isinstance(cls, type) and issubclass(cls, Plugin)):
            raise ValueError(f"{cls_name} 不是 Plugin 子类")
        return cls

    # ── 加载全部 (依赖注入拓扑序简化版: 两轮扫描) ──
    async def load_all(self):
        async with self._lock:
            for meta in self.discover():
                pid = meta.get("id", "")
                if meta.get("error"):
                    self._states[pid] = PluginState.ERROR
                    self._errors[pid] = meta["error"]
                    continue
                self._states.setdefault(pid, PluginState.DISCOVERED)
                self._errors.pop(pid, None)
                try:
                    cls = self._load_class(meta["entry"])
                    inst = cls()
                    inst.id = inst.id or pid
                    inst.builtin = True
                    # manifest 声明的 inject 优先 (dsh: 依赖声明驱动加载顺序)
                    if meta.get("inject"):
                        inst.inject = list(meta["inject"])
                    self._plugins[pid] = inst
                except Exception as e:
                    self._states[pid] = PluginState.ERROR
                    self._errors[pid] = f"加载失败: {e}"

            # enable 顺序: 无依赖的先 (inject 拓扑序)
            order = sorted(self._plugins.values(),
                           key=lambda p: len(getattr(p, "inject", []) or []))
            for inst in order:
                if self._states.get(inst.id) == PluginState.DISCOVERED:
                    if self.is_disabled_by_patch(inst.id):
                        self._states[inst.id] = PluginState.DISABLED
                    else:
                        await self._enable(inst)

    async def _enable(self, inst: Plugin):
        # inject: 服务未就绪 → 记错误不启动 (dsh: 等待服务就绪)
        for dep in (getattr(inst, "inject", []) or []):
            if not self.ctx.has(dep):
                self._states[inst.id] = PluginState.ERROR
                self._errors[inst.id] = f"依赖服务未就绪: {dep}"
                return
        try:
            disposer = await inst.apply(self.ctx)
            if disposer:
                self.ctx.effect(inst.id, disposer)
            self._states[inst.id] = PluginState.ENABLED
            logger.info(f"[plugins] {inst.id} v{inst.version} 已启用 "
                        f"({len(self.ctx._contributions.get(inst.id, []))} 项贡献)")
        except Exception as e:
            self.ctx.teardown_plugin(inst.id)
            self._states[inst.id] = PluginState.ERROR
            self._errors[inst.id] = f"启动失败: {e}"

    async def enable(self, pid: str) -> dict:
        async with self._lock:
            inst = self._plugins.get(pid)
            if not inst:
                return {"ok": False, "error": f"插件 '{pid}' 不存在"}
            patch = self._read_patch()
            patch.pop(pid, None)          # dsh: 与默认一致时不留 patch 项
            self._write_patch(patch)
            if self._states.get(pid) == PluginState.ENABLED:
                return {"ok": True, "state": PluginState.ENABLED}
            self.ctx.teardown_plugin(pid)
            await self._enable(inst)
            return {"ok": self._states.get(pid) == PluginState.ENABLED,
                    "state": self._states.get(pid),
                    "error": self._errors.get(pid, "")}

    async def disable(self, pid: str) -> dict:
        async with self._lock:
            if pid not in self._plugins:
                return {"ok": False, "error": f"插件 '{pid}' 不存在"}
            patch = self._read_patch()
            entry = patch.get(pid) or {}
            entry["disabled"] = True
            patch[pid] = entry
            self._write_patch(patch)
            self.ctx.teardown_plugin(pid)   # 逆序撤销全部副作用
            self._states[pid] = PluginState.DISABLED
            logger.info(f"[plugins] {pid} 已禁用 (副作用已撤销)")
            return {"ok": True, "state": PluginState.DISABLED}

    async def reload(self, pid: str = "") -> dict:
        """HMR-lite: 全部或单个插件 重载 (dispose → importlib 刷新 → enable)"""
        async with self._lock:
            targets = [pid] if pid else list(self._plugins.keys())
            for t in targets:
                if self._states.get(t) == PluginState.ENABLED:
                    self.ctx.teardown_plugin(t)
                    self._states[t] = PluginState.DISCOVERED
            for meta in self.discover():
                if meta.get("id") in targets and meta.get("entry"):
                    try:
                        importlib.reload(importlib.import_module(
                            meta["entry"].partition(":")[0]))
                    except Exception:
                        pass
            for t in targets:
                inst = self._plugins.get(t)
                if inst and self._states.get(t) == PluginState.DISCOVERED \
                        and not self.is_disabled_by_patch(t):
                    await self._enable(inst)
            return {"ok": True}

    def list(self) -> list:
        out = []
        for meta in self.discover():
            pid = meta.get("id", "")
            out.append({
                "id": pid,
                "version": meta.get("version", ""),
                "description": meta.get("description", ""),
                "state": self._states.get(pid, PluginState.DISCOVERED),
                "error": self._errors.get(pid, ""),
                "contributions": self.ctx._contributions.get(pid, []),
                "builtin": True,
            })
        return out


# ═══════════════════════════════════════════════════════════════
# 全局单例 (backend 进程; agent 进程的桥事件走 Redis, 见 cu_bridge)
# ═══════════════════════════════════════════════════════════════

_bus: Optional[EventBus] = None
_mgr: Optional[PluginManager] = None
_init_task: Optional[asyncio.Task] = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def get_plugin_manager() -> PluginManager:
    global _mgr
    if _mgr is None:
        ctx = PluginContext(get_event_bus())
        _mgr = PluginManager(ctx)
    return _mgr


async def startup_plugins(service_providers: dict = None):
    """backend lifespan 调用: 预置服务 → 加载全部插件 (不阻断主流程)"""
    global _init_task
    mgr = get_plugin_manager()
    for k, v in (service_providers or {}).items():
        mgr.ctx.provide(k, v)
    try:
        t0 = time.time()
        await mgr.load_all()
        enabled = sum(1 for p in mgr.list() if p["state"] == PluginState.ENABLED)
        logger.info(f"[plugins] 插件系统就绪: {enabled}/{len(mgr.list())} 启用 "
                    f"({round(time.time() - t0, 2)}s)")
    except Exception as e:
        logger.warning(f"[plugins] 插件系统启动失败 (不阻断): {e}")
