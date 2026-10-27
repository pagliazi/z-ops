"""skills/dev/script_gen.py — 脚本生成 Skill"""
from typing import Any, Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.skill_base import BaseSkill, SkillResult, SkillParam


class ScriptGenSkill(BaseSkill):
    name = "script_gen"
    description = "LLM 生成运维脚本 (Shell/Python/Ansible)"
    parameters = [
        SkillParam(name="requirement", type="string", description="脚本需求描述", required=True),
        SkillParam(name="type", type="string", description="脚本类型", default="shell"),
    ]

    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        requirement = params.get("requirement") or params.get("text", "")
        script_type = params.get("type", "shell")

        if not requirement:
            return SkillResult(success=False, error="缺少脚本需求描述")

        # 需要通过 context 获取 llm_func
        llm_func = context.get("llm_func") if context else None
        if not llm_func:
            return SkillResult(success=False, error="LLM 函数不可用")

        prompt = (
            f"请根据以下需求生成{script_type}脚本:\n\n"
            f"需求: {requirement}\n\n"
            f"脚本类型: {script_type}\n"
            f"请输出完整可执行的脚本代码，用 ```{script_type} 包裹。"
        )

        result = await llm_func(prompt=prompt, system=(
            "你是 Z-OPS 运维智能体的脚本开发模块。"
            "输出可直接执行的完整脚本，添加注释说明关键步骤。"
        ))

        return SkillResult(
            success=True,
            data={"type": script_type, "requirement": requirement},
            text=result
        )
