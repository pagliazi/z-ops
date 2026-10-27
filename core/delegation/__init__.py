"""
Z-OPS Delegation Module - Subagent Delegation System
子代理委派模块 - 参考 Hermes Agent 的子代理机制

核心模块:
- subagent_manager: 子代理管理器
- rpc_bridge: RPC 通信桥接
- parallel_executor: 并行执行器
"""

from .subagent_manager import (
    SubagentManager,
    SubagentHandle,
    SubagentResult,
    TaskSpec,
    get_subagent_manager
)

from .rpc_bridge import (
    RPCBridge,
    RPCRequest,
    RPCResponse,
    get_rpc_bridge
)

from .parallel_executor import (
    ParallelExecutor,
    ParallelTask,
    TaskResult,
    get_parallel_executor
)

__all__ = [
    # 子代理管理
    "SubagentManager",
    "SubagentHandle",
    "SubagentResult",
    "TaskSpec",
    "get_subagent_manager",
    
    # RPC 桥接
    "RPCBridge",
    "RPCRequest",
    "RPCResponse",
    "get_rpc_bridge",
    
    # 并行执行
    "ParallelExecutor",
    "ParallelTask",
    "TaskResult",
    "get_parallel_executor",
]

__version__ = "1.0.0"