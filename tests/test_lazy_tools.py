#!/usr/bin/env python3
"""Phase 2.3 工具 schema 延迟加载 冒烟测试 (mock _call_llm, 不打真实 LLM/Redis)"""
import asyncio
import json
import os
import sys

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from core.query_engine import QueryEngine, QueryConfig, _one_line
from core.tool_schema import ToolDef, ToolInputSchema, ToolParam, ParamType

# ── 1) _one_line 摘要 ──
assert _one_line("第一句。第二句。") == "第一句", _one_line("第一句。第二句。")
assert _one_line("") == ""
assert len(_one_line("字" * 200)) <= 62
print("PASS 1: _one_line 摘要提取")

captured_names = []   # 每轮 LLM 调用看到的工具名列表
captured_specs = []   # 每轮完整 specs (校验 parameters 形态)
call_seq = {"i": 0}


async def fake_call_llm(self, messages, tools=None, config=None, query_config=None, turn_number=0, **kw):
    call_seq["i"] += 1
    captured_names.append([s.get("function", {}).get("name") for s in (tools or [])])
    captured_specs.append(list(tools or []))
    if call_seq["i"] == 1:
        return {
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "search_tool_detail",
                                         "arguments": json.dumps({"tool_name": "tool_3"})}}],
            "finish_reason": "tool_calls", "input_tokens": 10, "output_tokens": 5,
        }
    return {"content": "done", "tool_calls": [], "finish_reason": "stop",
            "input_tokens": 10, "output_tokens": 5}


QueryEngine._call_llm = fake_call_llm


def make_tools(count=21):
    async def fn(params=None, **kw):
        return None
    return [ToolDef(
        name=f"tool_{i}",
        description=f"测试工具{i}, 查询某类数据。第二句不出现在摘要。第三句。",
        input_schema=ToolInputSchema(params=[ToolParam(name="search", type=ParamType.STRING, description="搜索关键词")]),
        execute_fn=fn,
    ) for i in range(count)]


CFG = {"model": "m", "provider": "oneapi", "api_key": "x", "base_url": "http://x"}


async def main_lazy():
    eng = QueryEngine(llm_config=CFG)
    qc = QueryConfig(max_turns=3, enable_tool_calls=True, tool_choice="auto")
    async def idle_exec(name, args):
        return None
    res = await eng.query(
        messages=[{"role": "user", "content": "hello"}],
        system="s", tools=make_tools(), query_config=qc,
        tool_executor=idle_exec, lazy_tool_schemas=True,
    )
    assert res.output == "done", res.output
    # 第一轮: 21 精简 + search_tool_detail = 22
    assert len(captured_names[0]) == 22, captured_names[0]
    assert "search_tool_detail" in captured_names[0]
    # 第一轮全部精简: 无 parameters 定义
    for s in captured_specs[0]:
        if s["function"]["name"] == "tool_0":
            assert "parameters" not in s["function"], s
        if s["function"]["name"] == "tool_0":
            assert "第二句" not in s["function"]["description"], s
    # 第二轮 (search_tool_detail 展开 tool_3 后): tool_3 是完整 spec (替换而非追加)
    assert captured_names[1].count("tool_3") == 1
    assert len(captured_names[1]) == 22, captured_names[1]
    for s in captured_specs[1]:
        if s["function"]["name"] == "tool_3":
            assert "search" in s["function"]["parameters"].get("properties", {}), s
    print("PASS 2: 精简注入 + search_tool_detail 拦截 + 惰性展开 (后续轮次全量)")
    full_chars = sum(len(json.dumps(t.to_function_spec(), ensure_ascii=False)) for t in make_tools())
    slim_chars = sum(len(json.dumps(s, ensure_ascii=False)) for s in eng._active_tool_specs)
    print(f"  schema 体积: 全量={full_chars} chars → 精简(+1展开)={slim_chars} chars "
          f"(首轮省 {(1 - (slim_chars - len(json.dumps(next(s for s in eng._active_tool_specs if s['function']['name'] == 'tool_3'), ensure_ascii=False))) / full_chars) * 100:.0f}%+)")


asyncio.run(main_lazy())

# ── 3) 默认不启用: 全量注入不变 ──
captured_names.clear()
captured_specs.clear()
call_seq["i"] = 0


async def main_default():
    eng = QueryEngine(llm_config=CFG)
    qc = QueryConfig(max_turns=1, enable_tool_calls=True)
    await eng.query(messages=[{"role": "user", "content": "hi"}], tools=make_tools(),
                    query_config=qc, tool_executor=None)
    assert len(captured_names[0]) == 21, captured_names[0]
    assert "search_tool_detail" not in captured_names[0]
    for s in captured_specs[0]:
        if s["function"]["name"] == "tool_0":
            assert "search" in s["function"]["parameters"].get("properties", {}), s
    print("PASS 3: 默认路径全量注入不变 (回退安全)")


asyncio.run(main_default())

# ── 4) 惰性模式下直接调用工具 → 顺手展开 ──
captured_names.clear()
captured_specs.clear()
call_seq["i"] = 0


async def fake_call_llm2(self, messages, tools=None, config=None, query_config=None, turn_number=0, **kw):
    call_seq["i"] += 1
    captured_names.append([s.get("function", {}).get("name") for s in (tools or [])])
    captured_specs.append(list(tools or []))
    if call_seq["i"] == 1:
        return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "tool_5", "arguments": "{}"}}],
                "finish_reason": "tool_calls", "input_tokens": 10, "output_tokens": 5}
    return {"content": "done", "tool_calls": [], "finish_reason": "stop",
            "input_tokens": 10, "output_tokens": 5}


QueryEngine._call_llm = fake_call_llm2


async def main_direct():
    async def exec_fn(name, args):
        from core.tool_schema import ToolExecution
        return ToolExecution(tool_name=name, success=True, output="ok")

    eng = QueryEngine(llm_config=CFG)
    qc = QueryConfig(max_turns=2, enable_tool_calls=True)
    res = await eng.query(messages=[{"role": "user", "content": "hi"}], tools=make_tools(),
                          query_config=qc, tool_executor=exec_fn, lazy_tool_schemas=True)
    assert res.output == "done"
    # 直接调 tool_5 被执行 (工具结果 ok) 且其 schema 在第二轮已展开
    assert any(r["name"] == "tool_5" and r["result"] == "ok" for r in res.tool_results)
    for s in captured_specs[1]:
        if s["function"]["name"] == "tool_5":
            assert "search" in s["function"]["parameters"].get("properties", {}), s
    print("PASS 4: 惰性模式直接调用仍可执行 + schema 自动展开")


asyncio.run(main_direct())
print("\n全部通过 ✓")
