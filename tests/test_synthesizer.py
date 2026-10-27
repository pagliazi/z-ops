"""Phase 3.2 记忆合成 + 记忆面板 冒烟测试"""
import asyncio
import json
import sys
import time
sys.path.insert(0, "/opt/zops")

from core.memory import synthesizer as S


def t(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} - {name}" + (f" | {detail}" if detail and not ok else ""))
    return ok


all_ok = True

# ── 1. capture_explicit_memory 正反例 ──
print("1. capture_explicit_memory 正反例")
cases = [
    ("记住：XX客户的备份窗口是周日凌晨", "XX客户的备份窗口是周日凌晨"),
    ("记住: GID 12345 是数据库主机组", "GID 12345 是数据库主机组"),
    ("帮我记住一下，网络部值班电话是 1234", "网络部值班电话是 1234"),
    ("请记住 MQ 集群周三例行维护", "MQ 集群周三例行维护"),
    ("怎么配置告警通知?", ""),           # 非记忆指令
    ("忘记了密码怎么办", ""),            # 不是"记住"开头
    ("不要记住这条", ""),                # 否定式不匹配 (不以记住开头? "不要记住这条"以"不"开头, 不匹配 ^记住)
]
for text, expect in cases:
    got = S.capture_explicit_memory(text)
    all_ok &= t(f"capture({text[:18]}...)", got == expect, f"expect={expect!r} got={got!r}")

# ── 2. save_explicit_memory 落盘 + 幂等 ──
print("2. save_explicit_memory")
pid = S.save_explicit_memory("冒烟测试记忆: SMOKETEST 客户备份窗口周日 03:00", "smoke_user")
all_ok &= t("落盘返回 page_id", bool(pid) and pid.startswith("memory-"), f"pid={pid}")
pid2 = S.save_explicit_memory("冒烟测试记忆: SMOKETEST 客户备份窗口周日 03:00", "smoke_user")
all_ok &= t("同内容幂等 (page_id 相同)", pid == pid2)

from core.memory.wiki_store import get_wiki_store
store = get_wiki_store()
page = store.get("concepts", pid)
all_ok &= t("wiki 页面存在 + tags", page is not None and "explicit-memory" in (page.frontmatter.get("tags") or []))
all_ok &= t("sources 标注 explicit:username", "explicit:smoke_user" in (page.frontmatter.get("sources") or []))

# ── 3. memory_panel_data ──
print("3. memory_panel_data")
panel = S.memory_panel_data()
all_ok &= t("五类计数", set(panel["counts"].keys()) == {"entities", "concepts", "summaries", "synthesis", "queries"})
explicit_ids = [it["page_id"] for it in panel["recent_explicit"]]
all_ok &= t("显式记忆出现在面板", pid in explicit_ids)

# ── 4. delete_memory_page 删除 + 审计 ──
print("4. delete_memory_page")
ok = S.delete_memory_page("concepts", pid, "smoke_admin")
all_ok &= t("删除成功", ok)
all_ok &= t("页面已消失", store.get("concepts", pid) is None)
all_ok &= t("不存在页面返回 False", S.delete_memory_page("concepts", pid, "smoke_admin") is False)

# ── 5. _collect_recent_dialogs (构造临时 transcript) ──
print("5. _collect_recent_dialogs")
from core import transcript as TR
async def write_test_session():
    await TR.append_message("smoke-synth-1", "user", "SMOKETEST: 核心交换机 CPU 95% 告警怎么处理?")
    await TR.append_message("smoke-synth-1", "assistant", "经排查为 ARP 广播风暴, 已在接入层启用风暴抑制。")
asyncio.run(write_test_session())
chunks = S._collect_recent_dialogs(hours=1)
joined = "\n".join(chunks)
all_ok &= t("收集到含测试会话", "SMOKETEST" in joined, f"chunks={len(chunks)}")
all_ok &= t("过期会话被过滤", S._collect_recent_dialogs(hours=0) == [])

# ── 6. LLM 提炼容错: 解析层 ──
print("6. 提炼 JSON 容错解析 (复用 _llm_extract_memories 内部逻辑等价物)")
# 无 Redis pref / LLM 不可用时优雅降级返回 []
items = S._llm_extract_memories("user: 测试")
all_ok &= t("LLM 不可用降级为 []", items == [], f"got={items}")

# ── 7. synthesize_recent_sessions 幂等 + 状态记账 ──
print("7. synthesize_recent_sessions")
S._save_state({})  # 重置状态
stats1 = S.synthesize_recent_sessions(hours=1)
all_ok &= t("首次运行返回统计", "sessions" in stats1 and "extracted" in stats1, f"stats={stats1}")
stats2 = S.synthesize_recent_sessions(hours=1)
all_ok &= t("同日二次运行跳过", stats2.get("skipped") is True)

# ── 8. 调度函数: _seconds_until_next_run ──
print("8. 调度计算")
secs = S._seconds_until_next_run()
all_ok &= t("下一轮间隔 60s~24h", 60 <= secs <= 86400, f"secs={secs}")

# 清理测试痕迹
S.delete_memory_page("concepts", "memory-smoke", "smoke_admin")
try:
    (TR.TRANSCRIPT_DIR / "smoke-synth-1.jsonl").unlink(missing_ok=True)
except Exception:
    pass

print()
print("ALL PASS" if all_ok else "SOME FAILED")
sys.exit(0 if all_ok else 1)
