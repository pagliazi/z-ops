"""
Z-OPS RPC Bridge - RPC Communication Bridge
RPC 通信桥接 - 参考 Hermes Agent 的 RPC 调用机制

核心功能:
1. 子代理与主进程间的 RPC 通信
2. 工具调用的远程执行
3. 结果回传和状态同步
"""

import logging
import asyncio
import json
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
import uuid

logger = logging.getLogger("zops.delegation.rpc")


@dataclass
class RPCRequest:
    """RPC 请求"""
    request_id: str
    subagent_id: str
    tool_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    timeout: int = 30


@dataclass
class RPCResponse:
    """RPC 响应"""
    request_id: str
    success: bool
    result: Any = None
    error: str = ""
    execution_time: float = 0.0
    timestamp: str = ""


class RPCBridge:
    """
    RPC 桥接器
    
    参考 Hermes Agent 的 RPC 通信设计:
    1. 通过 Redis 或内存队列传递消息
    2. 支持同步和异步调用模式
    3. 请求/响应追踪
    """
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._pending_requests: Dict[str, RPCRequest] = {}
        self._callbacks: Dict[str, Callable] = {}
        self._response_handlers: Dict[str, asyncio.Queue] = {}
    
    async def send_request(
        self,
        subagent_id: str,
        tool_name: str,
        params: Dict[str, Any],
        timeout: int = 30,
        callback: Callable = None
    ) -> RPCResponse:
        """
        发送 RPC 请求
        
        Args:
            subagent_id: 目标子代理
            tool_name: 工具名称
            params: 参数
            timeout: 超时时间
            callback: 回调函数 (可选)
        
        Returns:
            RPCResponse: 响应结果
        """
        request_id = f"rpc_{uuid.uuid4().hex[:8]}"
        
        request = RPCRequest(
            request_id=request_id,
            subagent_id=subagent_id,
            tool_name=tool_name,
            params=params,
            timestamp=datetime.now().isoformat(),
            timeout=timeout
        )
        
        self._pending_requests[request_id] = request
        
        if callback:
            self._callbacks[request_id] = callback
        
        # 创建响应队列
        response_queue = asyncio.Queue()
        self._response_handlers[request_id] = response_queue
        
        # 发送请求
        await self._publish_request(request)
        
        # 等待响应
        try:
            response = await asyncio.wait_for(
                response_queue.get(),
                timeout=timeout
            )
            return response
        except asyncio.TimeoutError:
            logger.warning(f"RPC 请求超时: {request_id}")
            return RPCResponse(
                request_id=request_id,
                success=False,
                error="timeout"
            )
        finally:
            # 清理
            self._pending_requests.pop(request_id, None)
            self._response_handlers.pop(request_id, None)
            self._callbacks.pop(request_id, None)
    
    async def _publish_request(self, request: RPCRequest):
        """发布请求到消息通道"""
        if self.redis:
            # 使用 Redis Pub/Sub
            channel = f"zops:rpc:requests:{request.subagent_id}"
            message = json.dumps({
                "request_id": request.request_id,
                "tool_name": request.tool_name,
                "params": request.params,
                "timestamp": request.timestamp
            })
            await self.redis.publish(channel, message)
        else:
            # 内存模式 - 直接调用
            logger.debug(f"RPC 请求 (内存模式): {request.request_id}")
    
    async def handle_response(self, response_data: Dict[str, Any]):
        """
        处理响应
        
        由子代理调用，传递执行结果
        """
        request_id = response_data.get("request_id")
        
        if request_id not in self._pending_requests:
            logger.warning(f"收到未知响应: {request_id}")
            return
        
        response = RPCResponse(
            request_id=request_id,
            success=response_data.get("success", False),
            result=response_data.get("result"),
            error=response_data.get("error", ""),
            execution_time=response_data.get("execution_time", 0.0),
            timestamp=datetime.now().isoformat()
        )
        
        # 传递给等待者
        if request_id in self._response_handlers:
            await self._response_handlers[request_id].put(response)
        
        # 触发回调
        if request_id in self._callbacks:
            callback = self._callbacks[request_id]
            try:
                await callback(response)
            except Exception as e:
                logger.error(f"回调执行失败: {e}")
        
        logger.debug(f"RPC 响应处理: {request_id}")
    
    async def broadcast_tool_result(
        self,
        subagent_id: str,
        request_id: str,
        result: Any,
        error: str = ""
    ):
        """
        子代理广播工具执行结果
        
        Args:
            subagent_id: 子代理 ID
            request_id: 原始请求 ID
            result: 执行结果
            error: 错误信息
        """
        response_data = {
            "request_id": request_id,
            "subagent_id": subagent_id,
            "success": not bool(error),
            "result": result,
            "error": error,
            "execution_time": 0.0,
            "timestamp": datetime.now().isoformat()
        }
        
        if self.redis:
            channel = "zops:rpc:responses"
            await self.redis.publish(channel, json.dumps(response_data))
        else:
            # 直接处理
            await self.handle_response(response_data)
    
    def register_handler(self, subagent_id: str, handler: Callable):
        """注册子代理请求处理器"""
        self._callbacks[f"handler_{subagent_id}"] = handler
    
    async def start_listener(self):
        """启动响应监听器 (Redis Pub/Sub 模式)"""
        if not self.redis:
            logger.info("RPC 桥接器运行在内存模式")
            return
        
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("zops:rpc:responses")
        
        logger.info("RPC 响应监听器已启动")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await self.handle_response(data)
                except Exception as e:
                    logger.error(f"处理响应消息失败: {e}")
    
    async def get_stats(self) -> Dict:
        """获取 RPC 统计"""
        return {
            "pending_requests": len(self._pending_requests),
            "active_handlers": len(self._response_handlers),
            "registered_callbacks": len(self._callbacks)
        }


# 单例
_rpc_instance: Optional[RPCBridge] = None


async def get_rpc_bridge() -> RPCBridge:
    """获取 RPC 桥接器实例"""
    global _rpc_instance
    if _rpc_instance is None:
        _rpc_instance = RPCBridge()
    return _rpc_instance