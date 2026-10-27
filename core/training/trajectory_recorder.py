"""
Z-OPS Trajectory Recorder - RL Training Data Generation
轨迹记录器 - 参考 Hermes Agent 的 Atropos RL 集成

核心功能:
1. 记录 Agent 执行轨迹
2. 轨迹压缩和摘要
3. Atropos 格式导出
4. 批量轨迹生成
"""

import logging
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import uuid

logger = logging.getLogger("zops.training.trajectory")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TRAJECTORY_DIR = DATA_DIR / "trajectories"


@dataclass
class TrajectoryStep:
    """轨迹步骤"""
    step_id: int
    action: str  # 工具调用 / 消息发送
    action_type: str  # tool_call / message / decision
    input_params: Dict[str, Any] = field(default_factory=dict)
    output_result: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    agent_id: str = ""
    skill_used: str = ""
    success: bool = True
    error: str = ""
    reasoning: str = ""  # Agent 的思考过程


@dataclass
class Trajectory:
    """执行轨迹"""
    trajectory_id: str
    session_id: str
    user_id: str
    agent_id: str
    task: str  # 用户原始请求
    steps: List[TrajectoryStep] = field(default_factory=list)
    
    # 结果
    success: bool = False
    final_response: str = ""
    
    # 统计
    total_steps: int = 0
    total_tools_called: int = 0
    total_tokens: int = 0
    execution_time: float = 0.0
    
    # 元数据
    created_at: str = ""
    completed_at: str = ""
    tags: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "trajectory_id": self.trajectory_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "task": self.task,
            "steps": [asdict(s) for s in self.steps],
            "success": self.success,
            "final_response": self.final_response,
            "total_steps": self.total_steps,
            "total_tools_called": self.total_tools_called,
            "total_tokens": self.total_tokens,
            "execution_time": self.execution_time,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "tags": self.tags,
        }


@dataclass
class CompressedTrajectory:
    """压缩轨迹"""
    trajectory_id: str
    task: str
    summary: str  # 任务执行摘要
    outcome: str  # 结果描述
    key_actions: List[str] = field(default_factory=list)  # 关键动作
    lessons: List[str] = field(default_factory=list)  # 经验教训
    tokens_used: int = 0
    compression_ratio: float = 0.0


class TrajectoryRecorder:
    """
    轨迹记录器
    
    参考 Hermes Agent 的轨迹记录和 Atropos 集成:
    1. 记录完整的执行过程
    2. 支持轨迹压缩
    3. 导出为训练数据格式
    """
    
    def __init__(self, redis_client=None, llm_client=None):
        self.redis = redis_client
        self.llm = llm_client
        self._current_trajectories: Dict[str, Trajectory] = {}
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        """确保目录存在"""
        TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)
    
    def start_trajectory(
        self,
        session_id: str,
        user_id: str,
        agent_id: str,
        task: str,
        tags: List[str] = None
    ) -> str:
        """
        开始记录轨迹
        
        Returns:
            trajectory_id: 轨迹 ID
        """
        trajectory_id = f"traj_{uuid.uuid4().hex[:8]}"
        
        trajectory = Trajectory(
            trajectory_id=trajectory_id,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            task=task,
            created_at=datetime.now().isoformat(),
            tags=tags or []
        )
        
        self._current_trajectories[trajectory_id] = trajectory
        logger.info(f"开始轨迹: {trajectory_id} ({task[:50]}...)")
        
        return trajectory_id
    
    def record_step(
        self,
        trajectory_id: str,
        action: str,
        action_type: str,
        input_params: Dict = None,
        output_result: Dict = None,
        agent_id: str = "",
        skill_used: str = "",
        success: bool = True,
        error: str = "",
        reasoning: str = ""
    ) -> TrajectoryStep:
        """记录步骤"""
        if trajectory_id not in self._current_trajectories:
            logger.warning(f"轨迹不存在: {trajectory_id}")
            return None
        
        trajectory = self._current_trajectories[trajectory_id]
        
        step = TrajectoryStep(
            step_id=len(trajectory.steps),
            action=action,
            action_type=action_type,
            input_params=input_params or {},
            output_result=output_result or {},
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            skill_used=skill_used,
            success=success,
            error=error,
            reasoning=reasoning
        )
        
        trajectory.steps.append(step)
        trajectory.total_steps += 1
        
        if action_type == "tool_call":
            trajectory.total_tools_called += 1
        
        logger.debug(f"记录步骤: {trajectory_id} step {step.step_id}")
        
        return step
    
    def complete_trajectory(
        self,
        trajectory_id: str,
        success: bool,
        final_response: str = "",
        tokens_used: int = 0
    ) -> Optional[Trajectory]:
        """完成轨迹"""
        if trajectory_id not in self._current_trajectories:
            return None
        
        trajectory = self._current_trajectories[trajectory_id]
        trajectory.success = success
        trajectory.final_response = final_response
        trajectory.total_tokens = tokens_used
        trajectory.completed_at = datetime.now().isoformat()
        
        # 计算执行时间
        if trajectory.created_at:
            start = datetime.fromisoformat(trajectory.created_at)
            end = datetime.fromisoformat(trajectory.completed_at)
            trajectory.execution_time = (end - start).total_seconds()
        
        # 保存
        self._save_trajectory(trajectory)
        
        # 从内存移除
        del self._current_trajectories[trajectory_id]
        
        logger.info(f"完成轨迹: {trajectory_id} ({'成功' if success else '失败'})")
        
        return trajectory
    
    def _save_trajectory(self, trajectory: Trajectory):
        """保存轨迹"""
        filepath = TRAJECTORY_DIR / f"{trajectory.trajectory_id}.json"
        filepath.write_text(json.dumps(trajectory.to_dict(), indent=2, ensure_ascii=False))
        logger.debug(f"保存轨迹: {filepath}")
    
    def load_trajectory(self, trajectory_id: str) -> Optional[Trajectory]:
        """加载轨迹"""
        filepath = TRAJECTORY_DIR / f"{trajectory_id}.json"
        
        if not filepath.exists():
            return None
        
        data = json.loads(filepath.read_text())
        
        trajectory = Trajectory(
            trajectory_id=data["trajectory_id"],
            session_id=data["session_id"],
            user_id=data["user_id"],
            agent_id=data["agent_id"],
            task=data["task"],
            success=data["success"],
            final_response=data["final_response"],
            total_steps=data["total_steps"],
            total_tools_called=data["total_tools_called"],
            total_tokens=data["total_tokens"],
            execution_time=data["execution_time"],
            created_at=data["created_at"],
            completed_at=data["completed_at"],
            tags=data["tags"],
        )
        
        for step_data in data.get("steps", []):
            trajectory.steps.append(TrajectoryStep(**step_data))
        
        return trajectory
    
    async def compress_trajectory(
        self,
        trajectory: Trajectory
    ) -> CompressedTrajectory:
        """
        压缩轨迹
        
        使用 LLM 生成摘要，减少存储和训练开销
        """
        # 构建摘要提示
        steps_text = "\n".join([
            f"- Step {s.step_id}: {s.action} ({'成功' if s.success else '失败'})"
            for s in trajectory.steps[:20]  # 最多 20 步
        ])
        
        summary = f"任务: {trajectory.task}\n步骤:\n{steps_text}\n结果: {'成功' if trajectory.success else '失败'}"
        
        # 提取关键动作
        key_actions = [
            f"{s.action}: {s.skill_used}" 
            for s in trajectory.steps 
            if s.action_type == "tool_call" and s.skill_used
        ][:5]
        
        # 经验教训
        lessons = []
        if not trajectory.success:
            failed_steps = [s for s in trajectory.steps if not s.success]
            lessons = [f"避免: {s.error}" for s in failed_steps[:3]]
        
        compressed = CompressedTrajectory(
            trajectory_id=trajectory.trajectory_id,
            task=trajectory.task,
            summary=summary,
            key_actions=key_actions,
            outcome="成功" if trajectory.success else "失败",
            lessons=lessons,
            tokens_used=trajectory.total_tokens,
            compression_ratio=0.0  # 计算压缩比
        )
        
        return compressed
    
    def export_for_atropos(
        self,
        trajectory: Trajectory
    ) -> Dict[str, Any]:
        """
        导出为 Atropos RL 格式
        
        参考 Hermes Agent 的 Atropos 集成
        """
        # 构建对话格式
        messages = []
        
        # 用户请求
        messages.append({
            "role": "user",
            "content": trajectory.task
        })
        
        # Agent 响应和工具调用
        for step in trajectory.steps:
            if step.action_type == "tool_call":
                messages.append({
                    "role": "assistant",
                    "content": step.reasoning or "",
                    "tool_calls": [{
                        "id": f"call_{step.step_id}",
                        "type": "function",
                        "function": {
                            "name": step.skill_used,
                            "arguments": json.dumps(step.input_params)
                        }
                    }]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{step.step_id}",
                    "content": json.dumps(step.output_result)
                })
            elif step.action_type == "message":
                messages.append({
                    "role": "assistant",
                    "content": step.action
                })
        
        # 最终响应
        if trajectory.final_response:
            messages.append({
                "role": "assistant",
                "content": trajectory.final_response
            })
        
        return {
            "trajectory_id": trajectory.trajectory_id,
            "messages": messages,
            "reward": 1.0 if trajectory.success else 0.0,
            "metadata": {
                "agent_id": trajectory.agent_id,
                "total_steps": trajectory.total_steps,
                "total_tools": trajectory.total_tools_called,
                "execution_time": trajectory.execution_time,
                "success": trajectory.success
            }
        }
    
    def export_batch(
        self,
        trajectory_ids: List[str] = None,
        output_format: str = "atropos"
    ) -> List[Dict]:
        """
        批量导出轨迹
        
        Args:
            trajectory_ids: 指定轨迹 ID，None 则导出全部
            output_format: 输出格式 (atropos / compressed)
        """
        if trajectory_ids is None:
            # 加载所有轨迹
            trajectory_ids = [
                f.stem for f in TRAJECTORY_DIR.glob("*.json")
            ]
        
        exported = []
        
        for tid in trajectory_ids:
            trajectory = self.load_trajectory(tid)
            if trajectory:
                if output_format == "atropos":
                    exported.append(self.export_for_atropos(trajectory))
                elif output_format == "compressed":
                    compressed = asyncio.run(self.compress_trajectory(trajectory))
                    exported.append(asdict(compressed))
        
        logger.info(f"批量导出: {len(exported)} 轨迹 ({output_format} 格式)")
        
        return exported
    
    def get_stats(self) -> Dict:
        """获取统计"""
        total = len(list(TRAJECTORY_DIR.glob("*.json")))
        successful = len([
            f for f in TRAJECTORY_DIR.glob("*.json")
            if json.loads(f.read_text()).get("success")
        ])
        
        return {
            "total_trajectories": total,
            "successful_trajectories": successful,
            "success_rate": successful / total if total else 0,
            "active_recordings": len(self._current_trajectories)
        }


# 单例
_recorder_instance: Optional[TrajectoryRecorder] = None


def get_trajectory_recorder() -> TrajectoryRecorder:
    """获取轨迹记录器实例"""
    global _recorder_instance
    if _recorder_instance is None:
        _recorder_instance = TrajectoryRecorder()
    return _recorder_instance