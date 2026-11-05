"""
Z-OPS v2.0.0 — Self-Improving Example Skill
自我进化示例技能

演示如何使用 self-improving 机制让技能越来越聪明
"""

import logging
from typing import Dict, Any
from core.skill_base import BaseSkill, SkillParam, SkillResult
from core.self_improving import record_skill_execution

logger = logging.getLogger("zops")


class ExampleSelfImprovingSkill(BaseSkill):
    """
    示例：自我进化技能
    
    功能：演示如何使用 self-improving 机制
    
    特性:
    - 自动记录每次执行
    - 从历史经验中学习
    - 动态调整信心分数
    - 提供优化建议
    """
    
    name = "example_self_improving"
    description = "示例自我进化技能（演示用）"
    parameters = [
        SkillParam(
            name="task",
            type="string",
            description="任务描述",
            required=True
        ),
        SkillParam(
            name="complexity",
            type="int",
            description="任务复杂度 (1-10)",
            default=5
        ),
    ]
    
    enable_learning = True  # 启用自我学习
    
    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        """
        执行技能
        
        注意：实际使用时应该调用 execute_with_learning() 而非直接调用 execute()
        """
        import time
        
        task = params.get("task", "")
        complexity = params.get("complexity", 5)
        
        logger.info(f"[example_self_improving] 执行任务：{task} (复杂度={complexity})")
        
        # 模拟执行（实际业务逻辑）
        await asyncio.sleep(0.5 * (complexity / 5))  # 模拟执行时间
        
        # 模拟成功/失败（实际应该根据业务逻辑）
        import random
        success = random.random() > 0.2  # 80% 成功率
        
        if success:
            return SkillResult(
                success=True,
                data={"task_completed": task, "complexity": complexity},
                text=f"✅ 任务完成：{task}"
            )
        else:
            return SkillResult(
                success=False,
                error=f"任务执行失败：{task}"
            )


class OpscoreQuerySkill(BaseSkill):
    """
    OPS-CORE 查询技能（带自我进化）
    
    功能：查询 OPS-CORE 平台数据，并从历史查询中学习
    
    学习点:
    - 常见查询模式
    - 优化查询参数
    - 避免常见错误
    - 提升查询性能
    """
    
    name = "opscore_query_optimized"
    description = "OPS-CORE 平台智能查询技能（带自我优化）"
    parameters = [
        SkillParam(
            name="query_type",
            type="string",
            description="查询类型：site/vm/backup/status",
            required=True
        ),
        SkillParam(
            name="site",
            type="string",
            description="站点名称（可选）"
        ),
        SkillParam(
            name="filters",
            type="dict",
            description="过滤条件"
        ),
    ]
    
    enable_learning = True
    
    async def on_before_execute(self, params: Dict[str, Any], context: Dict[str, Any] = None):
        """执行前：利用历史经验优化查询"""
        await super().on_before_execute(params, context)
        
        # 基于信心分数调整查询策略
        if self.confidence_score < 0.6:
            logger.warning(f"[opscore_query] 信心分数较低 ({self.confidence_score:.2f})，将使用保守查询策略")
            # 可以在这里调整查询参数
        
        # 获取相似查询的经验
        from core.self_improving import get_improving_engine
        engine = get_improving_engine()
        
        similar = engine.get_similar_experiences(self.name, params, limit=5)
        if similar:
            logger.info(f"[opscore_query] 发现 {len(similar)} 条相似查询经验")
            
            # 分析历史查询的成功模式
            success_count = sum(1 for exp in similar if exp.success)
            if success_count < len(similar) * 0.6:
                logger.warning(f"[opscore_query] 历史查询成功率较低 ({success_count}/{len(similar)})")
                # 可以在这里应用优化策略
    
    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        """执行 OPS-CORE 查询"""
        import time
        
        query_type = params.get("query_type", "")
        site = params.get("site", "")
        filters = params.get("filters", {})
        
        logger.info(f"[opscore_query] 查询类型={query_type}, 站点={site or '全部'}")
        
        start_time = time.time()
        
        try:
            # 实际查询逻辑（这里简化）
            from agents.opscore_api import OpscoreApiClient
            
            client = OpscoreApiClient()
            
            if query_type == "site":
                # 查询站点信息
                result = await client.get_sites()
                data = {"sites": result, "count": len(result)}
            
            elif query_type == "vm":
                # 查询虚拟机
                result = await client.get_vms(site=site, filters=filters)
                data = {"vms": result, "count": len(result)}
            
            elif query_type == "backup":
                # 查询备份
                result = await client.get_backups(site=site)
                data = {"backups": result, "count": len(result)}
            
            else:
                return SkillResult(
                    success=False,
                    error=f"未知查询类型：{query_type}"
                )
            
            execution_time = time.time() - start_time
            
            logger.info(f"[opscore_query] 查询完成，耗时={execution_time:.2f}s")
            
            return SkillResult(
                success=True,
                data=data,
                text=f"✅ 查询完成：找到 {data.get('count', 0)} 条记录"
            )
        
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"[opscore_query] 查询失败：{e}")
            
            return SkillResult(
                success=False,
                error=f"OPS-CORE 查询失败：{str(e)}"
            )


class NetworkScanSkill(BaseSkill):
    """
    网络扫描技能（带自我进化）
    
    功能：网络扫描，并从历史扫描中学习优化策略
    
    学习点:
    - 优化扫描范围
    - 避免超时目标
    - 提升扫描效率
    """
    
    name = "network_scan_optimized"
    description = "智能网络扫描技能（带自我优化）"
    parameters = [
        SkillParam(
            name="target",
            type="string",
            description="扫描目标（IP/网段）",
            required=True
        ),
        SkillParam(
            name="ports",
            type="string",
            description="端口范围（如 1-1000 或 80,443）"
        ),
        SkillParam(
            name="timeout",
            type="int",
            description="超时时间（秒）",
            default=30
        ),
    ]
    
    enable_learning = True
    
    async def on_before_execute(self, params: Dict[str, Any], context: Dict[str, Any] = None):
        """执行前：基于历史经验优化扫描参数"""
        await super().on_before_execute(params, context)
        
        # 如果目标曾经扫描失败，调整策略
        from core.self_improving import get_improving_engine
        engine = get_improving_engine()
        
        similar = engine.get_similar_experiences(self.name, params, limit=10)
        if similar:
            # 分析历史超时情况
            timeout_failures = [
                exp for exp in similar
                if "timeout" in exp.error_message.lower()
            ]
            
            if len(timeout_failures) > len(similar) * 0.3:
                logger.warning(f"[network_scan] 历史超时率较高 ({len(timeout_failures)}/{len(similar)})")
                # 自动增加超时时间
                if "timeout" in params:
                    params["timeout"] = int(params["timeout"] * 1.5)
                    logger.info(f"[network_scan] 自动调整超时时间：{params['timeout']}s")
    
    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        """执行网络扫描"""
        import time
        
        target = params.get("target", "")
        ports = params.get("ports", "1-1000")
        timeout = params.get("timeout", 30)
        
        logger.info(f"[network_scan] 扫描目标={target}, 端口={ports}, 超时={timeout}s")
        
        start_time = time.time()
        
        try:
            # 实际扫描逻辑（简化示例）
            import subprocess
            
            # 构建 nmap 命令
            cmd = f"nmap -p {ports} --host-timeout {timeout}s {target}"
            
            result = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                result.communicate(),
                timeout=timeout + 10
            )
            
            execution_time = time.time() - start_time
            
            if result.returncode == 0:
                return SkillResult(
                    success=True,
                    data={"output": stdout.decode(), "ports": ports, "target": target},
                    text=f"✅ 网络扫描完成，耗时 {execution_time:.1f}s"
                )
            else:
                return SkillResult(
                    success=False,
                    error=f"扫描失败：{stderr.decode()}"
                )
        
        except asyncio.TimeoutError:
            execution_time = time.time() - start_time
            return SkillResult(
                success=False,
                error=f"扫描超时（超过 {timeout + 10}s）"
            )
        
        except Exception as e:
            execution_time = time.time() - start_time
            return SkillResult(
                success=False,
                error=f"扫描异常：{str(e)}"
            )


# 导出技能列表
__all__ = [
    "ExampleSelfImprovingSkill",
    "OpscoreQuerySkill",
    "NetworkScanSkill",
]
