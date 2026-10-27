"""
Z-OPS Modal Backend - Modal Serverless Execution Backend
Modal 无服务器执行后端 - 参考 Hermes Agent 的 Modal 集成

Modal 特性:
- 按需启动，空闲时不计费
- GPU 支持
- 自动扩展
"""

import logging
import time
from typing import Dict, Any, Optional

from .backend_factory import TerminalBackend, ExecutionResult

logger = logging.getLogger("zops.backends.modal")


class ModalBackend(TerminalBackend):
    """
    Modal 无服务器执行后端
    
    参考 Hermes Agent 的 Modal 后端设计:
    - 适合需要 GPU 或弹性计算的任务
    - 按需启动，成本优化
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.app_name = self.config.get("app_name", "zops-executor")
        self.default_timeout = self.config.get("timeout", 300)
        self.cpu = self.config.get("cpu", 1)
        self.memory = self.config.get("memory", 512)  # MB
        self.gpu = self.config.get("gpu")  # "A10G", "A100", etc.
        self._modal_available = None
    
    def _check_modal_available(self) -> bool:
        """检查 Modal 是否可用"""
        if self._modal_available is not None:
            return self._modal_available
        
        try:
            import modal
            self._modal_available = True
        except ImportError:
            logger.warning("Modal 未安装。运行: pip install modal")
            self._modal_available = False
        
        return self._modal_available
    
    async def execute(
        self,
        command: str,
        timeout: int = 60,
        env: Dict[str, str] = None,
        cwd: str = None
    ) -> ExecutionResult:
        """
        在 Modal 中执行命令
        
        Args:
            command: 要执行的命令
            timeout: 超时时间
            env: 环境变量
            cwd: 工作目录 (在 Modal 中通常忽略)
        
        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        
        if not self._check_modal_available():
            return ExecutionResult(
                success=False,
                stderr="Modal 未安装。运行: pip install modal",
                exit_code=-1,
                backend_type="modal"
            )
        
        try:
            import modal
            
            # 创建 Modal 应用
            app = modal.App(self.app_name)
            
            # 定义镜像
            image = modal.Image.debian_slim().pip_install("requests")
            
            # 定义函数
            @app.function(
                image=image,
                cpu=self.cpu,
                memory_mb=self.memory,
                gpu=self.gpu,
                timeout=timeout or self.default_timeout
            )
            def run_command(cmd: str) -> tuple:
                import subprocess
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True
                )
                return result.returncode, result.stdout, result.stderr
            
            # 执行
            with app.run():
                exit_code, stdout, stderr = run_command.remote(command)
            
            execution_time = time.time() - start_time
            
            return ExecutionResult(
                success=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                execution_time=execution_time,
                backend_type="modal"
            )
            
        except Exception as e:
            return ExecutionResult(
                success=False,
                stderr=str(e),
                exit_code=-1,
                execution_time=time.time() - start_time,
                backend_type="modal"
            )
    
    async def execute_script(
        self,
        script: str,
        language: str = "bash",
        timeout: int = 60,
        env: Dict[str, str] = None
    ) -> ExecutionResult:
        """
        在 Modal 中执行脚本
        """
        if language == "python":
            # 直接执行 Python 代码
            return await self._execute_python_code(script, timeout)
        else:
            # 包装为 shell 命令
            return await self.execute(script, timeout, env)
    
    async def _execute_python_code(self, code: str, timeout: int = 60) -> ExecutionResult:
        """执行 Python 代码"""
        start_time = time.time()
        
        if not self._check_modal_available():
            return ExecutionResult(
                success=False,
                stderr="Modal 未安装",
                exit_code=-1,
                backend_type="modal"
            )
        
        try:
            import modal
            
            app = modal.App(f"{self.app_name}-python")
            
            @app.function(
                cpu=self.cpu,
                memory_mb=self.memory,
                gpu=self.gpu,
                timeout=timeout
            )
            def run_code(code: str) -> tuple:
                import sys
                from io import StringIO
                
                old_stdout = StringIO()
                sys.stdout = old_stdout
                
                try:
                    exec(code, {"__name__": "__main__"})
                    output = old_stdout.getvalue()
                    return 0, output, ""
                except Exception as e:
                    return 1, old_stdout.getvalue(), str(e)
                finally:
                    sys.stdout = sys.__stdout__
            
            with app.run():
                exit_code, stdout, stderr = run_code.remote(code)
            
            return ExecutionResult(
                success=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                execution_time=time.time() - start_time,
                backend_type="modal"
            )
            
        except Exception as e:
            return ExecutionResult(
                success=False,
                stderr=str(e),
                exit_code=-1,
                execution_time=time.time() - start_time,
                backend_type="modal"
            )
    
    async def health_check(self) -> bool:
        """健康检查"""
        if not self._check_modal_available():
            return False
        
        try:
            import modal
            # 简单测试
            result = await self.execute("echo ok", timeout=30)
            return result.success
        except:
            return False
    
    async def cleanup(self):
        """清理资源"""
        logger.debug("Modal backend cleanup")
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取配置统计"""
        return {
            "app_name": self.app_name,
            "cpu": self.cpu,
            "memory_mb": self.memory,
            "gpu": self.gpu,
            "modal_available": self._check_modal_available()
        }