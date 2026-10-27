"""
Z-OPS v2.0.0 — Sandbox 沙箱隔离
提供 Skill 执行的隔离环境，防止危害系统安全

实现方式:
1. Docker 容器隔离（生产环境）
2. Linux namespace 隔离（轻量级）
3. 资源限制（CPU/内存/网络）
"""

import os
import json
import logging
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
import asyncio

logger = logging.getLogger("zops")


@dataclass
class SandboxConfig:
    """沙箱配置"""
    mode: str = "namespace"  # docker | namespace | none
    cpu_limit: float = 1.0  # CPU 核心数限制
    memory_limit: str = "512m"  # 内存限制
    network_enabled: bool = False  # 是否允许网络访问
    allowed_paths: list = None  # 允许访问的路径
    timeout: int = 30  # 执行超时（秒）
    
    def __post_init__(self):
        if self.allowed_paths is None:
            self.allowed_paths = ["/tmp", "/opt/zops/workspaces"]


class SandboxExecutor:
    """
    沙箱执行器
    
    功能:
    1. 在隔离环境中执行代码/命令
    2. 限制资源使用
    3. 捕获输出和错误
    4. 超时自动终止
    """
    
    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()
        self._validate_mode()
    
    def _validate_mode(self):
        """验证沙箱模式是否可用"""
        if self.config.mode == "docker":
            if not shutil.which("docker"):
                logger.warning("Docker 不可用，降级到 namespace 模式")
                self.config.mode = "namespace"
        elif self.config.mode == "namespace":
            if not shutil.which("unshare"):
                logger.warning("unshare 不可用，降级到无沙箱模式")
                self.config.mode = "none"
    
    async def execute(
        self,
        code: str = None,
        command: str = None,
        context: Dict[str, Any] = None
    ) -> Tuple[bool, str, str]:
        """
        在沙箱中执行代码或命令
        
        Args:
            code: Python 代码字符串
            command: Shell 命令
            context: 执行上下文
            
        Returns:
            (success, stdout, stderr)
        """
        if self.config.mode == "none":
            logger.warning("⚠️ 沙箱已禁用，执行不受隔离")
            return await self._execute_unsafe(code, command, context)
        elif self.config.mode == "namespace":
            return await self._execute_namespace(code, command, context)
        elif self.config.mode == "docker":
            return await self._execute_docker(code, command, context)
        else:
            raise ValueError(f"Unknown sandbox mode: {self.config.mode}")
    
    async def _execute_unsafe(
        self,
        code: str = None,
        command: str = None,
        context: Dict[str, Any] = None
    ) -> Tuple[bool, str, str]:
        """无沙箱执行（仅用于测试/开发）"""
        try:
            if code:
                # 执行 Python 代码
                exec_globals = {"__builtins__": __builtins__}
                if context:
                    exec_globals.update(context)
                
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                    f.write(code)
                    f.flush()
                    temp_file = f.name
                
                try:
                    result = subprocess.run(
                        ["python3", temp_file],
                        capture_output=True,
                        text=True,
                        timeout=self.config.timeout,
                        cwd="/tmp"
                    )
                    return result.returncode == 0, result.stdout, result.stderr
                finally:
                    os.unlink(temp_file)
            elif command:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout,
                    cwd="/tmp"
                )
                return result.returncode == 0, result.stdout, result.stderr
            else:
                return False, "", "No code or command provided"
        except subprocess.TimeoutExpired:
            return False, "", f"Execution timeout ({self.config.timeout}s)"
        except Exception as e:
            return False, "", str(e)
    
    async def _execute_namespace(
        self,
        code: str = None,
        command: str = None,
        context: Dict[str, Any] = None
    ) -> Tuple[bool, str, str]:
        """使用 Linux namespace 隔离执行"""
        # 准备执行脚本
        if code:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                temp_script = f.name
            
            exec_cmd = f"python3 {temp_script}"
        else:
            exec_cmd = command
            temp_script = None
        
        try:
            # 构建 unshare 命令
            # --mount: 新的 mount namespace
            # --pid: 新的 PID namespace
            # --fork: fork 进程
            # --mount-proc: 挂载新的 /proc
            unshare_cmd = [
                "unshare",
                "--mount",
                "--pid",
                "--fork",
                "--mount-proc=/proc"
            ]
            
            # 如果禁用网络，添加 --net
            if not self.config.network_enabled:
                unshare_cmd.extend(["--net", "--net=lo"])
            
            # 添加资源限制
            ulimit_cmd = [
                "sh", "-c",
                f"ulimit -v {int(self.config.memory_limit.replace('m', '')) * 1024} && "
                f"timeout {self.config.timeout} {exec_cmd}"
            ]
            
            full_cmd = unshare_cmd + ulimit_cmd
            
            # 创建临时工作目录
            with tempfile.TemporaryDirectory() as workdir:
                # 复制允许访问的文件
                for src_path in self.config.allowed_paths:
                    if os.path.exists(src_path):
                        dest = os.path.join(workdir, os.path.basename(src_path))
                        try:
                            if os.path.isdir(src_path):
                                shutil.copytree(src_path, dest, symlinks=True)
                            else:
                                shutil.copy2(src_path, dest)
                        except Exception as e:
                            logger.warning(f"复制 {src_path} 失败：{e}")
                
                result = await asyncio.create_subprocess_exec(
                    *full_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env={**os.environ, "WORKDIR": workdir}
                )
                
                try:
                    stdout, stderr = await asyncio.wait_for(
                        result.communicate(),
                        timeout=self.config.timeout + 5
                    )
                    
                    # 清理临时脚本
                    if temp_script:
                        os.unlink(temp_script)
                    
                    return result.returncode == 0, stdout.decode(), stderr.decode()
                
                except asyncio.TimeoutError:
                    # 超时强制终止
                    result.kill()
                    await result.wait()
                    return False, "", f"Execution timeout ({self.config.timeout}s)"
        
        except Exception as e:
            logger.error(f"Namespace 执行失败：{e}")
            return False, "", str(e)
        finally:
            if temp_script and os.path.exists(temp_script):
                os.unlink(temp_script)
    
    async def _execute_docker(
        self,
        code: str = None,
        command: str = None,
        context: Dict[str, Any] = None
    ) -> Tuple[bool, str, str]:
        """使用 Docker 容器隔离执行"""
        # 创建临时工作目录
        with tempfile.TemporaryDirectory() as workdir:
            # 准备执行脚本
            if code:
                script_path = os.path.join(workdir, "script.py")
                with open(script_path, 'w') as f:
                    f.write(code)
                exec_cmd = f"python3 /workspace/script.py"
            else:
                exec_cmd = command
            
            # 构建 docker run 命令
            docker_cmd = [
                "docker", "run", "--rm",
                "--cpus", str(self.config.cpu_limit),
                "--memory", self.config.memory_limit,
            ]
            
            # 如果禁用网络
            if not self.config.network_enabled:
                docker_cmd.extend(["--network", "none"])
            
            # 挂载工作目录
            docker_cmd.extend([
                "-v", f"{workdir}:/workspace:ro",
                "-w", "/workspace",
            ])
            
            # 使用 Python 镜像
            docker_cmd.extend([
                "python:3.10-slim",
                "sh", "-c",
                f"timeout {self.config.timeout} {exec_cmd}"
            ])
            
            try:
                result = await asyncio.create_subprocess_exec(
                    *docker_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.config.timeout + 10
                )
                
                return result.returncode == 0, stdout.decode(), stderr.decode()
            
            except asyncio.TimeoutError:
                result.kill()
                await result.wait()
                return False, "", f"Execution timeout ({self.config.timeout}s)"
            except Exception as e:
                logger.error(f"Docker 执行失败：{e}")
                return False, "", str(e)


# 全局单例
_sandbox_executor: Optional[SandboxExecutor] = None


def get_sandbox_executor(config: SandboxConfig = None) -> SandboxExecutor:
    """获取全局沙箱执行器"""
    global _sandbox_executor
    if _sandbox_executor is None:
        _sandbox_executor = SandboxExecutor(config)
    return _sandbox_executor


async def execute_in_sandbox(
    code: str = None,
    command: str = None,
    context: Dict[str, Any] = None,
    config: SandboxConfig = None
) -> Tuple[bool, str, str]:
    """
    在沙箱中执行代码或命令（便捷函数）
    
    Args:
        code: Python 代码
        command: Shell 命令
        context: 执行上下文
        config: 沙箱配置
        
    Returns:
        (success, stdout, stderr)
    """
    executor = get_sandbox_executor(config)
    return await executor.execute(code, command, context)
