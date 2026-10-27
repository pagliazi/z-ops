"""
Z-OPS Terminal Backends Module - Multi-Backend Execution System
终端后端模块 - 参考 Hermes Agent 的多终端后端设计

支持的后端:
- local: 本地执行
- docker: Docker 容器执行
- ssh: SSH 远程执行
- modal: Modal 无服务器执行
"""

from .backend_factory import (
    TerminalBackend,
    TerminalBackendFactory,
    BackendType,
    ExecutionResult,
    get_backend
)

from .local_backend import LocalBackend
from .docker_backend import DockerBackend
from .ssh_backend import SSHBackend
from .modal_backend import ModalBackend

__all__ = [
    # 抽象基类和工厂
    "TerminalBackend",
    "TerminalBackendFactory",
    "BackendType",
    "ExecutionResult",
    "get_backend",
    
    # 具体后端
    "LocalBackend",
    "DockerBackend",
    "SSHBackend",
    "ModalBackend",
]

__version__ = "1.0.0"