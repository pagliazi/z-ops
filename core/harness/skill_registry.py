"""
Z-OPS Harness 技能注册表 - 支持版本管理
参考 Hermes Agent 的技能管理机制
"""

import logging
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger("zops.harness.registry")


class SkillRegistry:
    BASE_DIR = Path("/opt/zops/skills/zops_generated")
    HISTORY_DIR = Path("/opt/zops/data/skill_versions")
    
    def __init__(self, redis=None):
        self.redis = redis
        self.skills: Dict[str, dict] = {}
        self.versions: Dict[str, List[dict]] = {}  # skill_name -> version history
        self._ensure_dirs()
        self._load_from_files()
        self._load_version_history()
    
    def _ensure_dirs(self):
        """确保目录存在"""
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    
    def _load_version_history(self):
        """加载版本历史"""
        history_file = self.HISTORY_DIR / "versions.json"
        if history_file.exists():
            try:
                data = json.loads(history_file.read_text())
                for skill_name, versions in data.get("versions", {}).items():
                    self.versions[skill_name] = versions
                logger.debug(f"加载版本历史: {len(self.versions)} 技能")
            except Exception as e:
                logger.warning(f"加载版本历史失败: {e}")
    
    def _load_from_files(self):
        if not self.BASE_DIR.exists():
            return
        for f in self.BASE_DIR.glob("*.yaml"):
            try:
                import yaml
                data = yaml.safe_load(f.read_text())
                if data and "name" in data:
                    self.skills[data["name"]] = data
            except Exception as e:
                logger.error(f"加载技能失败 {f.name}: {e}")
        logger.info(f"技能注册表: {len(self.skills)} 个技能")
    
    def get(self, name: str) -> Optional[dict]:
        return self.skills.get(name)
    
    def get_all(self) -> List[dict]:
        return list(self.skills.values())
    
    def get_by_agent(self, agent_id: str) -> List[dict]:
        return [s for s in self.skills.values() if agent_id in s.get("metadata", {}).get("zops", {}).get("requires_agent", [])]
    
    def find_similar(self, task: str, limit: int = 5) -> List[dict]:
        task_lower = task.lower()
        scored = []
        for sid, skill in self.skills.items():
            score = 0
            if skill.get("name", "").lower() in task_lower:
                score += 10
            for tag in skill.get("metadata", {}).get("zops", {}).get("tags", []):
                if tag in task_lower:
                    score += 3
            if score > 0:
                scored.append((score, skill))
        scored.sort(reverse=True)
        return [s[1] for s in scored[:limit]]
    
    def get_prompt_for_task(self, task: str) -> str:
        similar = self.find_similar(task)
        if not similar:
            return ""
        # v4.0 (Phase 5.3): 渐进披露 — 列表只注入名+summary (控制 token),
        # 完整 SOP 仅对 top 命中注入
        prompt = "\n## 可用技能\n"
        for s in similar[:5]:
            brief = s.get("summary") or s.get("description", "")
            prompt += f"- **{s['name']}**: {brief}\n"
        for s in similar[:3]:
            prompt += f"\n### {s['name']}\n{s.get('procedure', '')[:200]}...\n"
        return prompt
    
    async def save_skill(self, skill_data: dict, backup_version: bool = True,
                         _lifecycle_publish: bool = False):
        """保存技能，支持版本备份

        P2 (task-c17): 线上技能文件只允许经生命周期 publish 写入
        (_lifecycle_publish 由 core/harness/skill_lifecycle 调用);
        复盘/生成器等调用方必须走 submit_candidate → evaluate → approve → publish,
        直接覆盖线上技能被拒绝。
        """
        if not _lifecycle_publish:
            raise PermissionError(
                "P2 技能生命周期: 直接写线上技能被拒绝; "
                "用 core.harness.skill_lifecycle.submit_candidate 提交候选, "
                "经 evaluate/approve/publish 上线")
        import yaml
        name = skill_data.get("name", "unnamed")
        new_version = skill_data.get("version", "1.0.0")
        
        # 备份旧版本
        if backup_version and name in self.skills:
            old_skill = self.skills[name]
            old_version = old_skill.get("version", "1.0.0")
            if old_version != new_version:
                await self._backup_version(name, old_skill)
        
        # 保存新版本
        filepath = self.BASE_DIR / f"{name}.yaml"
        with open(filepath, "w") as f:
            yaml.dump(skill_data, f, allow_unicode=True, default_flow_style=False)
        self.skills[name] = skill_data
        
        logger.info(f"技能已保存: {name} v{new_version}")
    
    async def _backup_version(self, skill_name: str, skill_data: dict):
        """备份技能版本"""
        version = skill_data.get("version", "1.0.0")
        backup_file = self.HISTORY_DIR / f"{skill_name}_v{version}.yaml"
        
        import yaml
        skill_data_backup = dict(skill_data)
        skill_data_backup["backup_at"] = datetime.now().isoformat()
        skill_data_backup["backup_reason"] = "version_upgrade"
        
        with open(backup_file, "w") as f:
            yaml.dump(skill_data_backup, f, allow_unicode=True, default_flow_style=False)
        
        # 更新版本历史
        if skill_name not in self.versions:
            self.versions[skill_name] = []
        
        self.versions[skill_name].append({
            "version": version,
            "backup_at": skill_data_backup["backup_at"],
            "backup_file": str(backup_file)
        })
        
        # 保存版本索引
        await self._save_version_index()
        logger.debug(f"备份版本: {skill_name} v{version}")
    
    async def _save_version_index(self):
        """保存版本索引"""
        history_file = self.HISTORY_DIR / "versions.json"
        data = {
            "versions": self.versions,
            "updated_at": datetime.now().isoformat()
        }
        history_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    
    def get_version_history(self, skill_name: str) -> List[dict]:
        """获取技能版本历史"""
        return self.versions.get(skill_name, [])
    
    async def rollback_version(self, skill_name: str, target_version: str) -> bool:
        """回滚到指定版本"""
        import yaml
        backup_file = self.HISTORY_DIR / f"{skill_name}_v{target_version}.yaml"
        
        if not backup_file.exists():
            logger.warning(f"找不到版本备份: {skill_name} v{target_version}")
            return False
        
        try:
            skill_data = yaml.safe_load(backup_file.read_text())
            # 移除备份元数据
            skill_data.pop("backup_at", None)
            skill_data.pop("backup_reason", None)
            skill_data["restored_at"] = datetime.now().isoformat()
            
            await self.save_skill(skill_data, backup_version=False)
            logger.info(f"回滚成功: {skill_name} -> v{target_version}")
            return True
        except Exception as e:
            logger.error(f"回滚失败: {e}")
            return False
    
    def get_usage_stats(self, skill_name: str) -> dict:
        """获取技能使用统计"""
        skill = self.skills.get(skill_name, {})
        return {
            "usage_count": skill.get("usage_count", 0),
            "success_rate": skill.get("success_rate", 1.0),
            "version": skill.get("version", "1.0.0"),
            "last_used": skill.get("updated_at", "")
        }


_registry = None

async def get_skill_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
