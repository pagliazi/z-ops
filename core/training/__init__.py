# [归档标注 2026-10-18] Atropos RL 轨迹记录暂无生产场景, 未接线;
# 保留实现。启用前须按 c17 SPEC 评估预算与 ACL (轨迹含对话内容)。
"""
Z-OPS Training Module - RL Training Data Generation
训练数据模块 - 参考 Hermes Agent 的 Atropos RL 集成

核心模块:
- trajectory_recorder: 轨迹记录器
- trajectory_compressor: 轨迹压缩
- atropos_export: Atropos 格式导出
"""

from .trajectory_recorder import (
    TrajectoryRecorder,
    Trajectory,
    TrajectoryStep,
    CompressedTrajectory,
    get_trajectory_recorder
)

__all__ = [
    "TrajectoryRecorder",
    "Trajectory",
    "TrajectoryStep",
    "CompressedTrajectory",
    "get_trajectory_recorder",
]

__version__ = "1.0.0"