#!/usr/bin/env python3
"""
Z-OPS v2.1.0 Hermes Enhancement 验证脚本
展示新增模块的功能效果
"""

import asyncio
import sys
import os
from pathlib import Path

# 添加路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

print("=" * 60)
print("Z-OPS v2.1.0 Hermes Enhancement 功能验证")
print("=" * 60)


async def test_cross_session_memory():
    """测试跨会话记忆系统"""
    print("\n[1] 跨会话记忆系统测试")
    print("-" * 40)
    
    from core.memory.cross_session_memory import CrossSessionMemory, get_cross_session_memory
    
    memory = await get_cross_session_memory()
    
    # 存储会话摘要
    session = await memory.store_session_summary(
        session_id="test_session_001",
        user_id="test_user",
        agent_id="ops_agent",
        summary="用户查询了服务器状态，执行了健康检查操作",
        key_learnings=["需要先检查网络连通性", "OPS-CORE API 响应较慢需要超时处理"],
        topics=["服务器健康检查", "网络诊断"]
    )
    print(f"  ✓ 存储会话摘要: {session.session_id}")
    
    # 搜索历史会话
    results = await memory.search_past_sessions("健康检查", user_id="test_user")
    print(f"  ✓ 搜索'健康检查': 找到 {len(results)} 个相关会话")
    
    # 获取用户画像
    profile = await memory.get_user_model("test_user")
    print(f"  ✓ 用户画像: session_count={profile.session_count}, common_tasks={profile.common_tasks[:3]}")
    
    return True


async def test_user_profile():
    """测试用户画像系统"""
    print("\n[2] Honcho 用户画像测试")
    print("-" * 40)
    
    from core.memory.user_profile import HonchoProfileManager, get_profile_manager
    
    manager = await get_profile_manager()
    
    # 记录用户行为观察 (dialectic)
    note = await manager.observe(
        user_id="ops_user_001",
        observation="用户频繁使用 vmware 相关命令，熟悉虚拟化运维",
        interpretation="用户是虚拟化运维专家，偏好使用 vm_agent",
        action_taken="自动推荐 vm_agent 处理虚拟化任务",
        outcome="success",
        confidence=0.85
    )
    print(f"  ✓ Dialectic Note: {note.observation[:30]}...")
    
    # 获取个性化提示词
    prompt = await manager.get_personalized_prompt("ops_user_001", "vm_agent")
    print(f"  ✓ 个性化提示词: {prompt[:60]}...")
    
    return True


async def test_session_search():
    """测试 FTS5 会话搜索"""
    print("\n[3] FTS5 全文搜索测试")
    print("-" * 40)
    
    from core.memory.session_search import SessionSearch, Message, get_session_search
    
    search = await get_session_search()
    
    # 索引测试会话
    await search.index_session(
        session_id="search_test_001",
        user_id="test_user",
        agent_id="network_agent",
        messages=[
            Message(role="user", content="检查 DNS 解析是否正常"),
            Message(role="assistant", content="DNS 解析正常，响应时间 50ms"),
        ],
        summary="DNS 解析健康检查",
        topics=["DNS", "网络"]
    )
    print(f"  ✓ 索引会话: search_test_001")
    
    # 搜索
    results = await search.search("DNS")
    print(f"  ✓ 搜索'DNS': 找到 {len(results)} 个结果")
    
    # 获取统计
    stats = await search.get_stats()
    print(f"  ✓ 统计: {stats['total_sessions']} 会话, {stats['total_messages']} 消息")
    
    return True


async def test_memory_nudge():
    """测试记忆推送"""
    print("\n[4] 记忆推送系统测试")
    print("-" * 40)
    
    from core.memory.memory_nudge import MemoryNudgeSystem, get_memory_nudge_system
    
    nudge_system = await get_memory_nudge_system()
    
    # 检查并生成推送
    nudges = await nudge_system.check_and_generate_nudges(
        user_id="test_user",
        context={"has_failure": True, "skill_success_rate": 0.7}
    )
    print(f"  ✓ 生成推送: {len(nudges)} 条")
    
    for nudge in nudges[:3]:
        print(f"    - [{nudge.trigger_type}] {nudge.content[:50]}...")
    
    # 获取统计
    stats = await nudge_system.get_stats()
    print(f"  ✓ 统计: {stats['active_rules']} 活跃规则")
    
    return True


async def test_skill_improver():
    """测试技能改进器"""
    print("\n[5] 技能自我改进测试")
    print("-" * 40)
    
    from core.harness.skill_improver import SkillImprover, get_skill_improver
    
    improver = await get_skill_improver()
    
    # 记录失败反馈
    fb = await improver.record_feedback(
        skill_name="vm_backup_check",
        session_id="test_session",
        user_id="test_user",
        success=False,
        error_type="timeout",
        error_message="VMware API 连接超时"
    )
    print(f"  ✓ 记录失败反馈: {fb.skill_name}")
    
    # 再记录几次失败触发改进
    for i in range(3):
        await improver.record_feedback(
            skill_name="vm_backup_check",
            session_id=f"test_session_{i}",
            user_id="test_user",
            success=False,
            error_type="timeout",
            error_message="VMware API 连接超时"
        )
    
    # 获取待处理改进
    pending = await improver.get_pending_improvements("vm_backup_check")
    print(f"  ✓ 待处理改进: {len(pending)} 条")
    
    # 分析使用模式
    analysis = await improver.analyze_usage_patterns("vm_backup_check")
    print(f"  ✓ 使用分析: 总使用={analysis.total_uses}, 成功率={analysis.recent_success_rate:.1%}")
    
    return True


async def test_subagent_manager():
    """测试子代理管理器"""
    print("\n[6] 子代理委派测试")
    print("-" * 40)
    
    from core.delegation.subagent_manager import SubagentManager, TaskSpec, get_subagent_manager
    
    manager = await get_subagent_manager()
    
    # 创建子代理
    handle = await manager.spawn_subagent(
        task="检查服务器健康状态",
        agent_type="ops_agent",
        isolated=True,
        timeout=30
    )
    print(f"  ✓ 创建子代理: {handle.subagent_id} ({handle.agent_type})")
    
    # 等待完成
    await asyncio.sleep(1)
    
    status = await manager.get_subagent_status(handle.subagent_id)
    print(f"  ✓ 子代理状态: {status.status}")
    
    # 列出活跃子代理
    active = await manager.list_active_subagents()
    print(f"  ✓ 活跃子代理: {len(active)} 个")
    
    return True


async def test_parallel_executor():
    """测试并行执行器"""
    print("\n[7] 并行执行器测试")
    print("-" * 40)
    
    from core.delegation.parallel_executor import ParallelExecutor, ParallelTask, get_parallel_executor
    
    executor = get_parallel_executor(max_workers=3)
    
    # 定义测试任务
    async def test_task(name: str, delay: float):
        await asyncio.sleep(delay)
        return f"{name} 完成"
    
    tasks = [
        ParallelTask(
            task_id=f"task_{i}",
            task_func=test_task,
            args=(f"任务{i}", 0.5),
            timeout=5
        )
        for i in range(5)
    ]
    
    # 并行执行
    results = await executor.execute_parallel(tasks)
    print(f"  ✓ 并行执行: {results['successful']}/{results['total_tasks']} 成功")
    print(f"  ✓ 成功率: {results['success_rate']:.1%}")
    
    return True


async def test_backends():
    """测试多终端后端"""
    print("\n[8] 多终端后端测试")
    print("-" * 40)
    
    from core.backends.backend_factory import TerminalBackendFactory, get_backend
    from core.backends.local_backend import LocalBackend
    
    # 注册本地后端
    TerminalBackendFactory.register_backend("local", LocalBackend)
    
    # 获取本地后端
    backend = get_backend("local")
    print(f"  ✓ 可用后端: {TerminalBackendFactory.list_backends()}")
    
    # 执行测试命令
    result = await backend.execute("echo 'Hello from Z-OPS v2.1.0'", timeout=5)
    print(f"  ✓ 本地执行: exit_code={result.exit_code}, output={result.stdout.strip()}")
    
    # 健康检查
    healthy = await backend.health_check()
    print(f"  ✓ 健康检查: {'通过' if healthy else '失败'}")
    
    # 获取系统信息
    info = await backend.get_system_info()
    print(f"  ✓ 系统信息: CPU={info.get('cpu_count', 'N/A')} 核")
    
    return True


async def test_trajectory_recorder():
    """测试轨迹记录器"""
    print("\n[9] RL 训练数据生成测试")
    print("-" * 40)
    
    from core.training.trajectory_recorder import TrajectoryRecorder, get_trajectory_recorder
    
    recorder = get_trajectory_recorder()
    
    # 开始轨迹
    traj_id = recorder.start_trajectory(
        session_id="traj_test_session",
        user_id="test_user",
        agent_id="ops_agent",
        task="执行服务器健康检查",
        tags=["health", "check"]
    )
    print(f"  ✓ 开始轨迹: {traj_id}")
    
    # 记录步骤
    recorder.record_step(
        trajectory_id=traj_id,
        action="host_check",
        action_type="tool_call",
        skill_used="host_check",
        input_params={"host": "192.168.1.1"},
        output_result={"status": "healthy"},
        success=True
    )
    print(f"  ✓ 记录步骤: host_check")
    
    # 完成轨迹
    trajectory = recorder.complete_trajectory(
        trajectory_id=traj_id,
        success=True,
        final_response="服务器状态正常",
        tokens_used=150
    )
    print(f"  ✓ 完成轨迹: steps={trajectory.total_steps}, time={trajectory.execution_time:.2f}s")
    
    # 导出 Atropos 格式
    atropos_data = recorder.export_for_atropos(trajectory)
    print(f"  ✓ Atropos 导出: {len(atropos_data['messages'])} messages, reward={atropos_data['reward']}")
    
    # 统计
    stats = recorder.get_stats()
    print(f"  ✓ 统计: {stats['total_trajectories']} 轨迹, 成功率={stats['success_rate']:.1%}")
    
    return True


async def test_skill_schema():
    """测试技能 Schema (agentskills.io 兼容)"""
    print("\n[10] agentskills.io 兼容性测试")
    print("-" * 40)
    
    from core.harness.skill_schema import Skill, SkillMetadata, import_skill_from_agentskills_io
    
    # 创建技能
    skill = Skill(
        name="test_health_check",
        description="测试健康检查技能",
        version="1.0.0",
        when_to_use="当需要检查服务器健康状态时",
        procedure="1. 连接服务器\n2. 执行检查命令\n3. 返回结果",
        pitfalls="注意网络超时",
        metadata=SkillMetadata(
            tags=["health", "check", "server"],
            category="ops",
            n8n_workflow="health_check_workflow"
        )
    )
    
    # 导出为 agentskills.io 格式
    exported = skill.to_agentskills_io()
    print(f"  ✓ 导出 agentskills.io: {list(exported.keys())}")
    
    # 导出为 YAML
    yaml_str = skill.to_yaml()
    print(f"  ✓ 导出 YAML: {len(yaml_str)} bytes")
    
    # 导出为 OpenAI Tools 格式
    openai_tool = skill.to_openai_tool()
    print(f"  ✓ 导出 OpenAI Tools: {openai_tool['type']}")
    
    # 从 agentskills.io 导入
    imported = import_skill_from_agentskills_io(exported)
    print(f"  ✓ 导入技能: {imported.name}")
    
    return True


async def main():
    """运行所有测试"""
    tests = [
        ("跨会话记忆", test_cross_session_memory),
        ("用户画像", test_user_profile),
        ("FTS5搜索", test_session_search),
        ("记忆推送", test_memory_nudge),
        ("技能改进", test_skill_improver),
        ("子代理委派", test_subagent_manager),
        ("并行执行", test_parallel_executor),
        ("多终端后端", test_backends),
        ("轨迹记录", test_trajectory_recorder),
        ("技能标准", test_skill_schema),
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            success = await test_func()
            results.append((name, success, None))
        except Exception as e:
            results.append((name, False, str(e)))
            print(f"  ✗ 错误: {e}")
    
    # 总结
    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)
    
    passed = sum(1 for _, s, _ in results if s)
    total = len(results)
    
    for name, success, error in results:
        status = "✓ 通过" if success else "✗ 失败"
        print(f"  {status}: {name}")
        if error:
            print(f"         错误: {error[:50]}")
    
    print(f"\n总计: {passed}/{total} 测试通过")
    print("=" * 60)
    
    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)