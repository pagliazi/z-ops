"""
Z-OPS Docker Backend - Docker Container Execution Backend
Docker 容器执行后端 - 参考 Hermes Agent 的容器隔离执行
"""

import logging
import asyncio
import time
import uuid
from typing import Dict, Any, Optional

from .backend_factory import TerminalBackend, ExecutionResult

logger = logging.getLogger("zops.backends.docker")


class DockerBackend(TerminalBackend):
    """
    Docker 容器执行后端
    
    参考 Hermes Agent 的容器隔离执行:
    - 每个任务在独立容器中执行
    - 支持资源限制
    - 自动清理容器
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.image = self.config.get("image", "python:3.11-slim")
        self.default_timeout = self.config.get("timeout", 300)
        self.auto_remove = self.config.get("auto_remove", True)
        self.network = self.config.get("network", "bridge")
        self.memory_limit = self.config.get("memory", "512m")
        self.cpu_limit = self.config.get("cpu_quota", 50000)  # 0.5 CPU
        self.volume_mounts = self.config.get("volumes", [])
        self._containers: set = set()
    
    async def _run_docker_command(self, args: list, timeout: int = 60) -> ExecutionResult:
        """运行 docker 命令"""
        cmd = ["docker"] + args
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                
                return ExecutionResult(
                    success=False,
                    stderr=f"Docker command timed out",
                    exit_code=-1,
                    backend_type="docker"
                )
            
            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode,
                backend_type="docker"
            )
            
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                stderr="Docker not found. Please install Docker.",
                exit_code=-1,
                backend_type="docker"
            )
    
    async def execute(
        self,
        command: str,
        timeout: int = 60,
        env: Dict[str, str] = None,
        cwd: str = None
    ) -> ExecutionResult:
        """
        在 Docker 容器中执行命令
        
        Args:
            command: 要执行的命令
            timeout: 超时时间
            env: 环境变量
            cwd: 工作目录
        
        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        container_name = f"zops_exec_{uuid.uuid4().hex[:8]}"
        
        # 构建 docker run 命令
        args = [
            "run",
            "--name", container_name,
            "--network", self.network,
            "--memory", self.memory_limit,
            "--cpu-quota", str(self.cpu_limit),
        ]

        # P0 安全加固: 非 root / 降权 / 只读 / pids 限制
        if self.config.get("user"):
            args.extend(["--user", self.config["user"]])
        if self.config.get("cap_drop", True):
            args.extend(["--cap-drop", "ALL"])
        if self.config.get("read_only"):
            args.extend(["--read-only"])
            args.extend(["--tmpfs", "/tmp:rw,size=64m"])
        if self.config.get("pids_limit"):
            args.extend(["--pids-limit", str(self.config["pids_limit"])])
        if self.config.get("security_opt"):
            args.extend(["--security-opt", "no-new-privileges"])
        
        # 添加环境变量
        if env:
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
        
        # 添加卷挂载
        for vol in self.volume_mounts:
            args.extend(["-v", vol])
        
        # 自动删除
        if self.auto_remove:
            args.append("--rm")
        
        # 工作目录
        if cwd:
            args.extend(["-w", cwd])
        
        # 镜像和命令
        args.extend([self.image, "sh", "-c", command])
        
        self._containers.add(container_name)
        
        try:
            result = await self._run_docker_command(args, timeout)
            result.execution_time = time.time() - start_time
            result.container_id = container_name
            return result
            
        finally:
            self._containers.discard(container_name)
    
    async def execute_script(
        self,
        script: str,
        language: str = "bash",
        timeout: int = 60,
        env: Dict[str, str] = None
    ) -> ExecutionResult:
        """
        在容器中执行脚本
        """
        # 根据语言选择命令
        if language == "python":
            command = f"python3 -c '{script}'"
        else:
            # bash
            command = f"sh -c '{script}'"
        
        return await self.execute(command, timeout, env)
    
    async def health_check(self) -> bool:
        """健康检查 - 验证 Docker 是否可用"""
        result = await self._run_docker_command(["info"], timeout=10)
        return result.success
    
    async def cleanup(self):
        """清理所有容器"""
        for container in self._containers:
            try:
                await self._run_docker_command(["rm", "-f", container], timeout=10)
            except Exception as e:
                logger.warning(f"清理容器失败: {container} - {e}")
        
        self._containers.clear()
        logger.info("Docker 后端清理完成")
    
    async def pull_image(self, image: str = None) -> bool:
        """拉取镜像"""
        img = image or self.image
        result = await self._run_docker_command(["pull", img], timeout=300)
        return result.success
    
    async def list_containers(self) -> list:
        """列出相关容器"""
        result = await self._run_docker_command(
            ["ps", "-a", "--filter", "name=zops_", "--format", "{{.Names}}"],
            timeout=10
        )
        
        if result.success:
            return result.stdout.strip().split("\n") if result.stdout.strip() else []
        return []
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取 Docker 统计"""
        return {
            "image": self.image,
            "active_containers": len(self._containers),
            "auto_remove": self.auto_remove,
            "memory_limit": self.memory_limit
        }