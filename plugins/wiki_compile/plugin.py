"""wiki_compile — LLM-Wiki 增量编译 (Karpathy 思想, 迁自 cross_session_memory)"""
from core.plugin_system import Plugin, PluginContext


class WikiCompilePlugin(Plugin):
    id = "wiki_compile"
    version = "1.0.0"
    description = "会话摘要落盘后增量编译进 LLM-Wiki (已存在页面跳过 LLM 编译)"

    async def apply(self, ctx: PluginContext):
        bus = ctx.bus

        async def on_summary_stored(payload: dict):
            try:
                from core.memory.wiki_ingest import ingest_sessions, ingest_user_profiles
                from core.memory.wiki_store import get_wiki_store
                store = get_wiki_store()
                ingest_sessions(store)
                ingest_user_profiles(store)
                store.write_index()
            except Exception as e:
                logger_warn(e)

        def logger_warn(e):
            import logging
            logging.getLogger("zops").warning(f"[wiki_compile] 增量编译失败 (不阻断): {e}")

        dispose = bus.on("session/summary_stored", on_summary_stored, self.id)
        ctx.contribute(self.id, "event_handler", "session/summary_stored",
                       "增量编译会话进 zops/wiki/ (页面已存在时不重复 LLM 编译)")
        return dispose
