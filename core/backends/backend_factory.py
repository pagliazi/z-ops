"""
Z-OPS Terminal Backend Factory - Multi-Backend Architecture
终端后端工厂 - 参考 Hermes Agent 的多终端后端设计

支持的后端:
- local: 本地执行
- docker: Docker 容器执行
- ssh: SSH 远程执行
- modal: Modal 无服务器执行
"""

import logging
from typing import Dict, Any, Optional, Type
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("zops.backends.factory")


class BackendType(Enum):
    """后端类型"""
    LOCAL = "local"
    DOCKER = "docker"
    SSH = "ssh"
    MODAL = "modal"


@dataclass
class ExecutionResult:
    """执行结果"""
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time: float = 0.0
    backend_type: str = ""
    container_id: str = ""  # Docker 后端
    host: str = ""  # SSH 后端
    metadata: Dict[str, Any] = field(default_factory=dict)


class TerminalBackend(ABC):
    """终端后端抽象基类"""
    
    @abstractmethod
    async def execute(
        self,
        command: str,
        timeout: int = 60,
        env: Dict[str, str] = None,
        cwd: str = None
    ) -> ExecutionResult:
        """执行命令"""
        pass
    
    @abstractmethod
    async def execute_script(
        self,
        script: str,
        language: str = "bash",
        timeout: int = 60,
        env: Dict[str, str] = None
    ) -> ExecutionResult:
        """执行脚本"""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass
    
    @abstractmethod
    async def cleanup(self):
        """清理资源"""
        pass


class TerminalBackendFactory:
    """
    终端后端工厂
    
    参考 Hermes Agent 的多终端后端设计:
    支持多种执行环境，通过统一接口调用
    """
    
    _backends: Dict[str, Type[TerminalBackend]] = {}
    
    @classmethod
    def register_backend(cls, backend_type: str, backend_class: Type[TerminalBackend]):
        """注册后端"""
        cls._backends[backend_type] = backend_class
        logger.info(f"注册后端: {backend_type}")
    
    @classmethod
    def create_backend(
        cls,
        backend_type: str,
        config: Dict[str, Any] = None
    ) -> TerminalBackend:
        """
        创建后端实例
        
        Args:
            backend_type: 后端类型
            config: 配置参数
        
        Returns:
            TerminalBackend: 后端实例
        """
        if backend_type not in cls._backends:
            raise ValueError(f"未知的后端类型: {backend_type}")
        
        backend_class = cls._backends[backend_type]
        return backend_class(config or {})
    
    @classmethod
    def list_backends(cls) -> list:
        """列出可用后端"""
        return list(cls._backends.keys())


# 延迟导入注册
def _register_default_backends():
    """注册默认后端"""
    try:
        from .local_backend import LocalBackend
        TerminalBackendFactory.register_backend("local", LocalBackend)
    except ImportError:
        pass
    
    try:
        from .docker_backend import DockerBackend
        TerminalBackendFactory.register_backend("docker", DockerBackend)
    except ImportError:
        pass
    
    try:
        from .ssh_backend import SSHBackend
        TerminalBackendFactory.register_backend("ssh", SSHBackend)
    except ImportError:
        pass
    
    try:
        from .modal_backend import ModalBackend
        TerminalBackendFactory.register_backend("modal", ModalBackend)
    except ImportError:
        pass


# 自动注册
_register_default_backends()


def get_backend(backend_type: str = "local", config: Dict = None) -> TerminalBackend:
    """获取后端实例"""
    return TerminalBackendFactory.create_backend(backend_type, config)