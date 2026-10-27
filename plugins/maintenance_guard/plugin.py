"""maintenance_guard — 维护窗口策略插件 (dsh tools/pre-execute bail 语义示范)

配置 (plugins/patch.yaml):
  maintenance_guard:
    disabled: false
    start: "02:00"      # 窗口开始 (服务器本地时间)
    end: "04:00"        # 窗口结束
    block_tools: true   # 窗口期拒绝能力执行
    block_chat: false   # 窗口期拒绝新聊天任务
"""
import logging
import time
from datetime import datetime

from core.plugin_system import Plugin, PluginContext

logger = logging.getLogger("zops")


def _in_window(start: str, end: str) -> bool:
    try:
        now = datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end   # 跨午夜
    except Exception:
        return False


class MaintenanceGuardPlugin(Plugin):
    id = "maintenance_guard"
    version = "1.0.0"
    description = "维护窗口期内拒绝高风险操作 (策略缝, 默认停用)"
    _config = {"start": "02:00", "end": "04:00",
               "block_tools": True, "block_chat": False}

    def _load_config(self):
        try:
            import yaml
            from core.plugin_system import PATCH_FILE
            if PATCH_FILE.exists():
                data = yaml.safe_load(PATCH_FILE.read_text()) or {}
                cfg = (data.get(self.id) or {})
                self._config.update({k: v for k, v in cfg.items()
                                     if k != "disabled"})
        except Exception:
            pass

    async def apply(self, ctx: PluginContext):
        self._load_config()
        bus = ctx.bus
        disposers = []

        async def guard_tools(meta: dict):
            if not self._config.get("block_tools"):
                return None
            if _in_window(self._config["start"], self._config["end"]):
                logger.warning(f"[maintenance_guard] 维护窗口内拒绝能力执行: "
                               f"{meta.get('capability_id')} (actor: {meta.get('actor')})")
                return {"deny": True,
                        "reason": f"当前处于维护窗口 ({self._config['start']}-"
                                  f"{self._config['end']}), 高风险操作已暂停; "
                                  f"如需紧急执行请联系管理员调整维护窗口"}

        async def guard_chat(meta: dict):
            if not self._config.get("block_chat"):
                return None
            if _in_window(self._config["start"], self._config["end"]):
                return {"deny": True,
                        "reason": f"维护窗口内 ({self._config['start']}-"
                                  f"{self._config['end']}) 暂停受理新任务, 请稍后再试"}

        disposers.append(bus.on("tools/pre_execute", guard_tools, self.id))
        disposers.append(bus.on("chat/pre_task", guard_chat, self.id))
        ctx.contribute(self.id, "policy", "tools/pre_execute",
                       f"维护窗口 ({self._config['start']}-{self._config['end']}) 拒绝能力执行")
        ctx.contribute(self.id, "policy", "chat/pre_task",
                       "维护窗口拒绝新任务 (block_chat 可配)")

        def dispose_all():
            for d in disposers:
                d()
        return dispose_all
