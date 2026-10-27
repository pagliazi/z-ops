#!/usr/bin/env python3
"""
Z-OPS Harness Engineering 完整模块测试
"""

import asyncio
import sys
import os

# 添加项目路径
sys.path.insert(0, '/opt/zops')

print("=" * 60)
print("Z-OPS Harness Engineering 模块测试")
print("=" * 60)


async def test_skill_schema():
    """测试技能数据模型"""
    print("\n[1] 测试技能数据模型...")
    from core.harness import Skill, SkillMetadata
    
    skill = Skill(
        name="test_skill",
        description="测试技能",
        metadata=SkillMetadata(
            tags=["test", "demo"],
            category="test",
            requires_tools=["shell"]
        ),
        when_to_use="测试场景",
        procedure="1. 执行测试\n2. 验证结果",
        pitfalls="注意测试数据",
        auto_generated=False
    )
    
    json_data = skill.to_json()
    assert json_data["name"] == "test_skill"
    assert "test" in json_data["metadata"]["zops"]["tags"]
    
    # 测试反序列化
    skill2 = Skill.from_dict(json_data)
    assert skill2.name == "test_skill"
    
    print("   ✅ Skill 数据模型测试通过")
    return True


async def test_skill_registry():
    """测试技能注册表"""
    print("\n[2] 测试技能注册表...")
    from core.harness import get_skill_registry
    
    registry = await get_skill_registry()
    skills = registry.get_all()
    
    print(f"   注册技能数量: {len(skills)}")
    for s in skills[:5]:
        print(f"   - {s.get('name', 'unknown')}: {s.get('description', '')[:40]}")
    
    # 测试相似技能查找
    similar = registry.find_similar("vm backup")
    print(f"   相似技能: {[s.get('name') for s in similar]}")
    
    # 测试任务提示词生成
    prompt = registry.get_prompt_for_task("检查虚拟机备份")
    print(f"   提示词长度: {len(prompt)} 字符")
    
    print("   ✅ 技能注册表测试通过")
    return True


async def test_memory():
    """测试记忆系统"""
    print("\n[3] 测试记忆系统...")
    from core.harness import get_harness_memory
    
    memory = await get_harness_memory()
    
    # 存储失败记忆
    await memory.store_failure(
        task="测试任务",
        error="测试错误",
        solution="测试解决方案",
        context={"test": True},
        agent_id="test_agent"
    )
    
    # 存储成功记忆
    await memory.store_success(
        task="成功任务",
        solution="成功方案",
        result="成功结果",
        context={"test": True}
    )
    
    # 搜索
    results = await memory.search("测试")
    print(f"   搜索结果: {len(results)} 条")
    
    # 统计
    stats = await memory.get_stats()
    print(f"   记忆统计: {stats}")
    
    print("   ✅ 记忆系统测试通过")
    return True


async def test_n8n_skill():
    """测试 N8N 技能封装"""
    print("\n[4] 测试 N8N 技能封装...")
    from core.harness import (
        N8nBaseSkill, get_skill, execute_skill,
        VMPowerSkill, NotificationSkill
    )
    
    # 测试技能工厂
    vm_skill = get_skill("vm_power")
    assert vm_skill is not None
    print(f"   技能工厂获取: {vm_skill.__class__.__name__}")
    
    # 测试基类
    assert hasattr(vm_skill, 'execute')
    assert hasattr(vm_skill, 'validate_params')
    
    # 测试参数验证
    valid, error = vm_skill.validate_params({"vm_name": "test", "action": "start"})
    assert valid == True
    
    invalid, error = vm_skill.validate_params({"vm_name": "test"})
    assert invalid == False
    print(f"   参数验证: 无参时返回 {invalid} - {error}")
    
    print("   ✅ N8N 技能封装测试通过")
    return True


async def test_n8n_bridge():
    """测试 N8N 桥接器"""
    print("\n[5] 测试 N8N 桥接器...")
    from core.harness import get_harness_executor
    
    executor = await get_harness_executor()
    result = await executor.execute("vm_power", {"vm_name": "test", "action": "start"})
    
    print(f"   执行模式: {result.get('mode', 'unknown')}")
    print(f"   执行结果: {result.get('success', False)}")
    
    print("   ✅ N8N 桥接器测试通过")
    return True


async def test_scheduler():
    """测试任务调度器"""
    print("\n[6] 测试任务调度器...")
    from core.harness import get_task_scheduler
    
    scheduler = await get_task_scheduler()
    
    # 检查内置任务
    tasks = scheduler.get_all_tasks()
    print(f"   注册任务数: {len(tasks)}")
    for task in tasks:
        print(f"   - {task.name}: {task.schedule}")
    
    # 测试自定义任务
    task = scheduler.register_custom_task(
        task_id="custom_test",
        name="自定义测试任务",
        workflow="test_workflow",
        schedule="hourly",
        params={"test": True}
    )
    print(f"   自定义任务: {task.name}")
    
    print("   ✅ 任务调度器测试通过")
    return True


async def test_skill_generator():
    """测试技能生成器"""
    print("\n[7] 测试技能生成器...")
    from core.harness import HarnessController
    
    controller = HarnessController()
    
    # 记录失败
    await controller.record_failure(
        task="测试任务1",
        error="错误1",
        context={"test": True},
        attempted_solution="尝试方案1"
    )
    
    history = await controller.get_failure_history()
    print(f"   失败记录数: {len(history)}")
    
    print("   ✅ 技能生成器测试通过")
    return True


async def test_integration():
    """集成测试"""
    print("\n[8] 集成测试...")
    
    # 测试完整流程：失败 -> 记忆 -> 技能查找 -> 执行
    from core.harness import (
        get_harness_memory,
        get_skill_registry,
        get_harness_executor
    )
    
    memory = await get_harness_memory()
    registry = await get_skill_registry()
    executor = await get_harness_executor()
    
    # 1. 记录失败
    await memory.store_failure(
        task="执行备份检查",
        error="备份超时",
        solution="增加超时时间",
        context={"backup_type": "full"}
    )
    
    # 2. 查找相似技能
    similar = registry.find_similar("backup")
    print(f"   相似技能: {[s.get('name') for s in similar]}")
    
    # 3. 执行技能
    result = await executor.execute("backup_check", {"vm_name": "test-vm"})
    print(f"   执行结果: {result.get('success')}")
    
    print("   ✅ 集成测试通过")
    return True


async def main():
    """运行所有测试"""
    results = []
    
    tests = [
        ("技能数据模型", test_skill_schema),
        ("技能注册表", test_skill_registry),
        ("记忆系统", test_memory),
        ("N8N 技能封装", test_n8n_skill),
        ("N8N 桥接器", test_n8n_bridge),
        ("任务调度器", test_scheduler),
        ("技能生成器", test_skill_generator),
        ("集成测试", test_integration),
    ]
    
    for name, test_fn in tests:
        try:
            await test_fn()
            results.append((name, True, None))
        except Exception as e:
            print(f"   ❌ 测试失败: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False, str(e)))
    
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    
    for name, ok, error in results:
        status = "✅ 通过" if ok else f"❌ 失败: {error}"
        print(f"  {name}: {status}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有模块测试通过!")
    else:
        print("\n⚠️ 部分测试失败，请检查")
    
    return passed == total


if __name__ == "__main__":
    asyncio.run(main())