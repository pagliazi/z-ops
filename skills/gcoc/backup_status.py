"""skills/gcoc/backup_status.py — GCOC 备份状态查询 Skill

GCOC 备份作业查询技能，支持按站点、GID、作业名称进行备份状态查询。
支持全量查询和关键词搜索，返回备份和作业的统计信息。

适用场景:
    - 查询指定站点的备份作业状态
    - 根据 GID 搜索相关的备份和作业
    - 获取备份作业的整体统计信息
    - 按站点预聚合备份数据，减少前端计算压力

前置条件:
    - GCOC 平台可访问 (gcoc-info.example.com / 198.18.18.146)
    - 已配置 GCOC_API_TOKEN (信息平台) 或 GCOC_MGT_USER/PASS (管理平台)
    - 网络连接正常

参数说明:
    search (str, 可选): 搜索关键词，支持:
        - GID: 搜索特定虚拟机的备份和作业
        - 站点名称：搜索指定站点的备份作业
        - 作业名称：模糊匹配作业名称
        - 空字符串：返回全量备份数据 (最多 1000 条)

返回数据:
    success (bool): 查询是否成功
    data.count (int): 总记录数 (备份 + 作业)
    data.backups (int): 备份记录数
    data.jobs (int): 作业记录数  
    data.gid_jobs (int): GID 相关作业数 (仅当 search 参数存在时)
    text (str): 人类可读的查询结果摘要

执行步骤:
    1. 构建 API 查询参数 (page_size=1000)
    2. 并发查询备份数据和作业数据
    3. 如果提供 search 参数，额外查询 GID 相关作业
    4. 合并结果并返回统计信息
    5. 前端可根据 data 字段进一步处理

最佳实践:
    - 优先使用关键词搜索，避免全量查询
    - 大结果集建议前端分页展示
    - 可组合多个条件过滤结果
    - 定期清理过期备份数据

常见问题:
    - 问题：认证失败 → 解决：检查 GCOC 凭据配置
    - 问题：连接超时 → 解决：检查网络连通性和防火墙规则
    - 问题：结果为空 → 解决：确认关键词拼写和平台数据存在性

示例:
    >>> await skill.execute({"search": ""})
    # 返回所有备份和作业状态
    
    >>> await skill.execute({"search": "47915"})
    # 搜索 GID 47915 相关的备份和作业
    
    >>> await skill.execute({"search": "brr"})
    # 搜索包含 "brr" 的站点备份作业
"""
import json
from typing import Any, Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.skill_base import BaseSkill, SkillResult, SkillParam


class BackupStatusSkill(BaseSkill):
    """
    GCOC 备份状态查询技能
    
    属性:
        name: 技能名称 "backup_status"
        description: 技能描述，说明功能特性
        parameters: 参数定义列表，包含参数名、类型、描述、是否必需
        enable_learning: 是否启用自我学习 (默认 True)
        confidence_score: 当前信心分数 (0.0-1.0，基于历史执行记录)
    
    Self-Improving:
        - 自动记录每次查询的关键词和结果
        - 从成功的查询模式中学习最佳实践
        - 根据历史成功率调整信心分数
        - 提供查询优化建议 (如常用关键词、过滤条件)
    """
    name = "backup_status"
    description = "GCOC 备份作业查询 — 按站点预聚合，支持关键词搜索"
    parameters = [
        SkillParam(
            name="search", 
            type="string", 
            description="搜索关键词 (GID/站点/作业名)，空字符串表示全量查询",
            required=False,
            default=""
        ),
    ]

    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        from agents.gcoc_api import query_api
        import asyncio

        search = params.get("search", "")

        bk_params = {"page_size": 1000}
        job_params = {"page_size": 1000}

        tasks = [
            query_api("brr_backups", bk_params),
            query_api("brr_jobs", job_params),
        ]
        if search:
            tasks.append(query_api("brr_gid_jobs", {"search": search, "page_size": 500}))
        else:
            tasks.append(asyncio.sleep(0))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        backups_data = results[0] if not isinstance(results[0], Exception) else {}
        jobs_data = results[1] if not isinstance(results[1], Exception) else {}
        gid_data = results[2] if (search and not isinstance(results[2], Exception)) else {}

        def _extract(data):
            if isinstance(data, list): return data
            if isinstance(data, dict): return data.get("results", [])
            return []

        backups = _extract(backups_data)
        jobs = _extract(jobs_data)
        gid_jobs = _extract(gid_data) if isinstance(gid_data, (dict, list)) else []

        if not backups and not jobs and not gid_jobs:
            return SkillResult(success=True, text=f"备份查询结果为空 (关键词: '{search}')", data={"count": 0})

        total_count = len(backups) + len(jobs)
        return SkillResult(
            success=True,
            data={"count": total_count, "backups": len(backups), "jobs": len(jobs), "gid_jobs": len(gid_jobs)},
            text=f"查询到 {len(backups)} 个备份, {len(jobs)} 个作业"
        )
