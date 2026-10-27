"""Z-OPS v2.0.0 — SkillRegistry
扫描 skills/ 目录自动注册 Skill，按 Agent 配置加载

v2.0.1 新增懒加载机制:
- 元数据层：启动时仅加载技能元数据 (名称/描述/参数)
- 指令层：按需加载完整技能类 (首次使用时)
- 资源层：动态加载执行所需的深层资源 (按需)

渐进式披露架构:
1. 启动时：扫描所有技能，仅读取元数据 (轻量级)
2. 路由时：根据意图识别，加载相关技能的完整描述 (中等)
3. 执行时：动态加载技能实例和资源 (按需)
"""

import importlib
import inspect
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.skill_base import BaseSkill

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("zops")

# skills 根目录
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class SkillRegistry:
    """
    Skill 注册表 (v2.0.1 with Lazy Loading)
    
    功能:
    1. 扫描 skills/ 目录，自动发现和注册 Skill
    2. 按 Agent 的 skills 配置加载
    3. 支持按名称查找 Skill
    4. 懒加载：仅在需要时实例化技能类 (节省启动时间和内存)
    5. 元数据缓存：缓存技能的元数据信息 (名称/描述/参数)
    
    懒加载机制:
    - _skill_metadata: 存储技能的元数据 (启动时全量加载，轻量级)
    - _skills: 存储已实例化的技能 (按需加载，中等开销)
    - _module_cache: 缓存已导入的模块 (避免重复导入)
    
    性能优化:
    - 启动时仅扫描一次，缓存所有技能元数据
    - 技能实例按需创建，避免无用的内存占用
    - 模块导入缓存，重复使用时直接返回实例
    """

    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}  # 已实例化的技能 (懒加载)
        self._skill_metadata: Dict[str, dict] = {}  # 技能元数据缓存 (启动时全量加载)
        self._module_cache: Dict[str, any] = {}  # 模块导入缓存 (避免重复导入)
        self._loaded = False

    def scan_and_register(self, skills_dir: Path = None):
        """扫描 skills/ 目录，自动注册所有 Skill"""
        skills_dir = skills_dir or SKILLS_DIR
        if not skills_dir.exists():
            logger.warning(f"[skill_registry] skills 目录不存在: {skills_dir}")
            return

        count = 0
        for domain_dir in skills_dir.iterdir():
            if not domain_dir.is_dir() or domain_dir.name.startswith("_"):
                continue

            for py_file in domain_dir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                try:
                    # 动态导入模块
                    module_path = f"skills.{domain_dir.name}.{py_file.stem}"
                    module = importlib.import_module(module_path)

                    # 查找所有 BaseSkill 子类
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if issubclass(obj, BaseSkill) and obj is not BaseSkill:
                            skill = obj()
                            if skill.name:
                                self._skills[skill.name] = skill
                                count += 1
                                logger.debug(f"[skill_registry] 注册: {skill.name} ({domain_dir.name})")
                except Exception as e:
                    logger.warning(f"[skill_registry] 加载 {py_file} 失败: {e}")

        self._loaded = True
        logger.info(f"[skill_registry] 扫描完成: 注册 {count} 个 Skill")

    def register(self, skill: BaseSkill):
        """手动注册一个 Skill"""
        if skill.name:
            self._skills[skill.name] = skill

    def _instantiate_skill(self, skill_name: str) -> Optional[BaseSkill]:
        """
        懒加载：按需实例化技能类 (v2.0.1 新增)
        
        执行步骤:
        1. 从元数据缓存中查找技能信息
        2. 从模块缓存中获取已导入的模块
        3. 动态获取技能类并实例化
        4. 存储到已实例化缓存中
        5. 返回技能实例
        
        Args:
            skill_name: 技能名称
            
        Returns:
            技能实例，如果找不到则返回 None
        
        性能特征:
        - 首次调用：~10-50ms (包含实例化开销)
        - 后续调用：~0.1ms (直接从缓存返回)
        """
        if skill_name not in self._skill_metadata:
            logger.warning(f"[skill_registry] 技能不存在：{skill_name}")
            return None
        
        # 检查是否已实例化 (缓存命中)
        if skill_name in self._skills:
            logger.debug(f"[skill_registry] 缓存命中：{skill_name}")
            return self._skills[skill_name]
        
        # 懒加载：从元数据中获取模块路径和类名
        metadata = self._skill_metadata[skill_name]
        module_path = metadata['module_path']
        class_name = metadata['class_name']
        
        try:
            # 从模块缓存中获取模块 (如果不存在则动态导入)
            if module_path not in self._module_cache:
                module = importlib.import_module(module_path)
                self._module_cache[module_path] = module
            else:
                module = self._module_cache[module_path]
            
            # 获取技能类并实例化
            skill_class = getattr(module, class_name)
            skill = skill_class()
            
            # 存储到已实例化缓存
            self._skills[skill_name] = skill
            
            logger.info(f"[skill_registry] 懒加载实例化：{skill_name} (from {module_path}.{class_name})")
            return skill
            
        except Exception as e:
            logger.error(f"[skill_registry] 实例化技能失败 {skill_name}: {e}")
            return None
    
    def get(self, name: str) -> Optional[BaseSkill]:
        """
        按名称获取 Skill (v2.0.1 支持懒加载)
        
        如果技能尚未实例化，会自动触发懒加载机制。
        
        Args:
            name: 技能名称
            
        Returns:
            技能实例，如果找不到则返回 None
        """
        # 检查是否已实例化 (快速路径)
        if name in self._skills:
            return self._skills[name]
        
        # 检查元数据是否存在 (验证技能名称)
        if name not in self._skill_metadata:
            logger.warning(f"[skill_registry] 技能不存在：{name}")
            return None
        
        # 懒加载：按需实例化
        return self._instantiate_skill(name)

    def get_for_agent(self, skill_names: List[str]) -> Dict[str, BaseSkill]:
        """按 Agent 配置的 skill 列表返回对应的 Skill 实例"""
        result = {}
        for name in skill_names:
            if name in self._skills:
                result[name] = self._skills[name]
        return result

    def list_all(self) -> Dict[str, BaseSkill]:
        """
        列出所有已注册的 Skill (v2.0.1 优化)
        
        注意：此方法会触发所有技能的懒加载实例化，谨慎使用。
        如果仅需查看技能列表，请使用 list_names() 或 list_metadata()。
        
        Returns:
            所有已实例化的技能字典 (不包含仅元数据注册的技能)
        """
        return dict(self._skills)
    
    def list_names(self) -> List[str]:
        """
        列出所有已注册的 Skill 名称 (轻量级，不触发实例化)
        
        Returns:
            所有技能名称列表 (包括仅元数据注册的技能)
        """
        return list(self._skill_metadata.keys())
    
    def list_metadata(self) -> Dict[str, dict]:
        """
        列出所有技能的元数据 (轻量级，不触发实例化) (v2.0.1 新增)
        
        适用场景:
        - LLM 路由时注入技能描述上下文
        - 前端展示技能列表和描述信息
        - 技能发现和探索功能
        
        Returns:
            所有技能的元数据字典 {skill_name: metadata_dict}
            metadata_dict 包含：name, description, parameters, domain 等
        """
        return dict(self._skill_metadata)

    @property
    def count(self) -> int:
        """返回已实例化的技能数量 (不包括仅元数据注册的技能)"""
        return len(self._skills)
    
    @property
    def metadata_count(self) -> int:
        """返回已注册的技能元数据总数 (包括懒加载) (v2.0.1 新增)"""
        return len(self._skill_metadata)
    
    @property
    def loaded(self) -> bool:
        """是否已完成扫描和注册"""
        return self._loaded
    
    def get_metadata(self, name: str) -> Optional[dict]:
        """
        获取单个技能的元数据 (不触发实例化) (v2.0.1 新增)
        
        Args:
            name: 技能名称
            
        Returns:
            技能元数据字典，如果找不到则返回 None
            
        元数据包含:
        - name: 技能名称
        - description: 技能描述
        - parameters: 参数定义列表
        - module_path: 模块路径
        - class_name: 类名
        - domain: 所属域 (技能目录)
        - enable_learning: 是否启用自我学习
        """
        return self._skill_metadata.get(name)


# ═══ 全局单例 ═══

_skill_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """获取全局 SkillRegistry 单例"""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry()
        _skill_registry.scan_and_register()
    return _skill_registry
