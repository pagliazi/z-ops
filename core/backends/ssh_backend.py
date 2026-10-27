"""
Z-OPS SSH Backend - SSH Remote Execution Backend
SSH 远程执行后端 - 参考 Hermes Agent 的 SSH 远程执行
"""

import logging
import asyncio
import time
from typing import Dict, Any, Optional

from .backend_factory import TerminalBackend, ExecutionResult

logger = logging.getLogger("zops.backends.ssh")


class SSHBackend(TerminalBackend):
    """
    SSH 远程执行后端
    
    参考 Hermes Agent 的 SSH 后端设计:
    - 通过 SSH 连接远程服务器执行命令
    - 支持密钥和密码认证
    - 支持跳板机 (ProxyJump)
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.host = self.config.get("host", "localhost")
        self.port = self.config.get("port", 22)
        self.username = self.config.get("username", "root")
        self.password = self.config.get("password")
        self.key_file = self.config.get("key_file")
        self.proxy_host = self.config.get("proxy_host")
        self.default_timeout = self.config.get("timeout", 60)
    
    def _build_ssh_command(self, command: str, timeout: int = None) -> list:
        """构建 SSH 命令"""
        args = [
            "ssh",
            "-p", str(self.port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
        ]
        
        # 跳板机
        if self.proxy_host:
            args.extend(["-J", self.proxy_host])
        
        # 密钥文件
        if self.key_file:
            args.extend(["-i", self.key_file])
        
        # 目标主机
        args.append(f"{self.username}@{self.host}")
        
        # 命令
        args.append(command)
        
        return args
    
    async def execute(
        self,
        command: str,
        timeout: int = 60,
        env: Dict[str, str] = None,
        cwd: str = None
    ) -> ExecutionResult:
        """
        通过 SSH 执行命令
        
        Args:
            command: 要执行的命令
            timeout: 超时时间
            env: 环境变量
            cwd: 工作目录
        
        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        
        # 构建远程命令
        remote_cmd = command
        if cwd:
            remote_cmd = f"cd {cwd} && {command}"
        
        if env:
            env_exports = " ".join(f"export {k}='{v}';" for k, v in env.items())
            remote_cmd = f"{env_exports} {remote_cmd}"
        
        ssh_args = self._build_ssh_command(remote_cmd, timeout)
        
        try:
            process = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout or self.default_timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                
                return ExecutionResult(
                    success=False,
                    stderr=f"SSH command timed out after {timeout}s",
                    exit_code=-1,
                    execution_time=time.time() - start_time,
                    backend_type="ssh",
                    host=self.host
                )
            
            execution_time = time.time() - start_time
            
            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode,
                execution_time=execution_time,
                backend_type="ssh",
                host=self.host
            )
            
        except Exception as e:
            return ExecutionResult(
                success=False,
                stderr=str(e),
                exit_code=-1,
                execution_time=time.time() - start_time,
                backend_type="ssh",
                host=self.host
            )
    
    async def execute_script(
        self,
        script: str,
        language: str = "bash",
        timeout: int = 60,
        env: Dict[str, str] = None
    ) -> ExecutionResult:
        """
        通过 SSH 执行脚本
        
        将脚本通过 stdin 传递给远程
        """
        # 使用 heredoc 方式传递脚本
        if language == "python":
            remote_cmd = f"python3 - <<'EOF'\n{script}\nEOF"
        else:
            remote_cmd = f"bash - <<'EOF'\n{script}\nEOF"
        
        return await self.execute(remote_cmd, timeout, env)
    
    async def health_check(self) -> bool:
        """健康检查 - 验证 SSH 连接"""
        result = await self.execute("echo ok", timeout=10)
        return result.success and "ok" in result.stdout
    
    async def cleanup(self):
        """清理资源"""
        logger.debug(f"SSH backend cleanup for {self.host}")
    
    async def upload_file(self, local_path: str, remote_path: str) -> bool:
        """上传文件"""
        args = [
            "scp",
            "-P", str(self.port),
            "-o", "StrictHostKeyChecking=no",
        ]
        
        if self.key_file:
            args.extend(["-i", self.key_file])
        
        args.extend([
            local_path,
            f"{self.username}@{self.host}:{remote_path}"
        ])
        
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            await process.communicate()
            return process.returncode == 0
            
        except Exception as e:
            logger.error(f"上传文件失败: {e}")
            return False
    
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """下载文件"""
        args = [
            "scp",
            "-P", str(self.port),
            "-o", "StrictHostKeyChecking=no",
        ]
        
        if self.key_file:
            args.extend(["-i", self.key_file])
        
        args.extend([
            f"{self.username}@{self.host}:{remote_path}",
            local_path
        ])
        
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            await process.communicate()
            return process.returncode == 0
            
        except Exception as e:
            logger.error(f"下载文件失败: {e}")
            return False
    
    async def get_system_info(self) -> Dict[str, Any]:
        """获取远程系统信息"""
        info = {"host": self.host}
        
        # CPU 信息
        result = await self.execute("nproc", timeout=5)
        if result.success:
            try:
                info["cpu_count"] = int(result.stdout.strip())
            except:
                pass
        
        # 内存信息
        result = await self.execute("free -m | grep Mem | awk '{print $2}'", timeout=5)
        if result.success:
            try:
                info["memory_mb"] = int(result.stdout.strip())
            except:
                pass
        
        # 系统
        result = await self.execute("uname -a", timeout=5)
        if result.success:
            info["system"] = result.stdout.strip()
        
        return info