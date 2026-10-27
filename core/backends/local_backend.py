"""
Z-OPS Local Backend - Local Execution Backend
本地执行后端
"""

import logging
import asyncio
import subprocess
import time
from typing import Dict, Any, Optional
import os
import tempfile

from .backend_factory import TerminalBackend, ExecutionResult

logger = logging.getLogger("zops.backends.local")


class LocalBackend(TerminalBackend):
    """
    本地执行后端
    
    直接在本地执行命令和脚本
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 300)
        self.workdir = self.config.get("workdir", os.getcwd())
    
    async def execute(
        self,
        command: str,
        timeout: int = 60,
        env: Dict[str, str] = None,
        cwd: str = None
    ) -> ExecutionResult:
        """
        执行命令
        
        Args:
            command: 要执行的命令
            timeout: 超时时间 (秒)
            env: 环境变量
            cwd: 工作目录
        
        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        
        # 准备环境
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        
        work_dir = cwd or self.workdir
        
        try:
            # 异步执行
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=exec_env,
                cwd=work_dir
            )
            
            # 等待完成 (带超时)
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
                    stderr=f"Command timed out after {timeout}s",
                    exit_code=-1,
                    execution_time=time.time() - start_time,
                    backend_type="local"
                )
            
            execution_time = time.time() - start_time
            
            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode,
                execution_time=execution_time,
                backend_type="local"
            )
            
        except Exception as e:
            return ExecutionResult(
                success=False,
                stderr=str(e),
                exit_code=-1,
                execution_time=time.time() - start_time,
                backend_type="local"
            )
    
    async def execute_script(
        self,
        script: str,
        language: str = "bash",
        timeout: int = 60,
        env: Dict[str, str] = None
    ) -> ExecutionResult:
        """
        执行脚本
        
        Args:
            script: 脚本内容
            language: 脚本语言 (bash, python)
            timeout: 超时时间
            env: 环境变量
        
        Returns:
            ExecutionResult: 执行结果
        """
        # 确定解释器
        interpreters = {
            "bash": "/bin/bash",
            "python": "python3",
            "python3": "python3",
            "sh": "/bin/sh"
        }
        
        interpreter = interpreters.get(language, "/bin/bash")
        
        # 写入临时文件
        suffix = ".sh" if language in ["bash", "sh"] else ".py"
        
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=suffix,
            delete=False
        ) as f:
            f.write(script)
            script_path = f.name
        
        try:
            # 执行
            command = f"{interpreter} {script_path}"
            result = await self.execute(command, timeout, env)
            
            result.metadata["script_path"] = script_path
            result.metadata["language"] = language
            
            return result
            
        finally:
            # 清理临时文件
            try:
                os.unlink(script_path)
            except:
                pass
    
    async def health_check(self) -> bool:
        """健康检查"""
        result = await self.execute("echo ok", timeout=5)
        return result.success and "ok" in result.stdout
    
    async def cleanup(self):
        """清理资源"""
        logger.debug("Local backend cleanup")
    
    async def get_system_info(self) -> Dict[str, Any]:
        """获取系统信息"""
        info = {}
        
        # CPU 信息
        result = await self.execute("nproc", timeout=5)
        if result.success:
            info["cpu_count"] = int(result.stdout.strip())
        
        # 内存信息
        result = await self.execute("free -m | grep Mem | awk '{print $2}'", timeout=5)
        if result.success:
            try:
                info["memory_mb"] = int(result.stdout.strip())
            except:
                pass
        
        # 磁盘信息
        result = await self.execute("df -h / | tail -1 | awk '{print $4}'", timeout=5)
        if result.success:
            info["disk_available"] = result.stdout.strip()
        
        return info