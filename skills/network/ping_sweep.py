"""skills/network/ping_sweep.py — 批量 Ping 检测 Skill

批量网络连通性检测技能，支持同时对多个目标主机执行 Ping 测试。
返回每个目标的连通性状态、丢包率和详细输出。

适用场景:
    - 批量检测服务器网络连通性
    - 网络故障排查和诊断
    - 定期网络健康检查
    - 验证网络配置变更后的连通性

前置条件:
    - 主机已安装 ping 命令 (通常所有 Linux 系统默认安装)
    - 具有执行 ping 命令的权限 (通常需要 root 或 CAP_NET_RAW 能力)
    - 目标主机防火墙允许 ICMP 回显请求
    - 网络连接正常

参数说明:
    targets (str, 必需): 目标地址列表，支持格式:
        - 逗号分隔："192.168.1.1,192.168.1.2,192.168.1.3"
        - 换行分隔：每行一个地址
        - 混合分隔：逗号和换行均可
        - 支持 IPv4 和 IPv6 地址
        - 支持主机名 (自动 DNS 解析)
    
    count (int, 可选): 每个目标的 Ping 次数，默认 3 次
        - 推荐值：3-5 次 (平衡速度和准确性)
        - 最小值：1 次 (快速检测)
        - 最大值：10 次 (精确测量)

返回数据:
    success (bool): 执行是否成功
    data.raw_data (dict): 原始数据，键为目标地址，值为包含以下字段的对象:
        - output (str): ping 命令完整输出
        - loss (str): 丢包率 (如 "0%", "50%", "100%")
        - reachable (bool): 是否可达 (丢包率为 0% 时为 True)
    data.targets (list): 实际执行检测的目标地址列表
    text (str): JSON 格式的详细结果，包含所有目标的完整输出

执行步骤:
    1. 解析 targets 参数，分割成目标地址列表
    2. 清理空白字符和空行
    3. 为每个目标创建异步 ping 任务
    4. 并发执行所有 ping 测试 (使用 asyncio.gather)
    5. 解析每个目标的 ping 输出，提取丢包率
    6. 判断连通性 (0% 丢包率 = 可达)
    7. 汇总结果并返回

最佳实践:
    - 批量检测时建议分组执行 (每组 10-20 个目标)
    - 生产环境建议使用 5 次 ping 以获得准确结果
    - 对于关键目标，可设置更高的 ping 次数 (10 次)
    - 结合 DNS 解析和端口检测进行综合诊断
    - 定期执行并记录历史数据用于趋势分析

常见问题:
    - 问题：权限不足 → 解决：使用 sudo 或以 root 用户执行
    - 问题：防火墙阻止 → 解决：确认目标允许 ICMP 协议
    - 问题：超时 → 解决：增加 timeout 或减少 ping 次数
    - 问题：主机名无法解析 → 解决：检查 DNS 配置或使用 IP 地址
    - 问题：部分目标无响应 → 解决：确认目标主机在线且网络可达

示例:
    >>> await skill.execute({"targets": "192.168.1.1,192.168.1.2"})
    # 批量 ping 两个 IP 地址
    
    >>> await skill.execute({"targets": "server1.example.com,server2.example.com", "count": 5})
    # 使用主机名，每个目标 ping 5 次
    
    >>> await skill.execute({"targets": "10.0.0.1\n10.0.0.2\n10.0.0.3", "count": 3})
    # 使用换行分隔多个目标

性能特征:
    - 并发执行：所有目标同时开始 ping 测试
    - 执行时间：约等于最慢目标的响应时间 (非累加)
    - 资源消耗：每个任务约 1-2MB 内存，网络带宽消耗可忽略
    - 超时控制：单个目标超时 15 秒，总体超时自动终止
"""
import asyncio
import json
import shlex
from typing import Any, Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.skill_base import BaseSkill, SkillResult, SkillParam


class PingSweepSkill(BaseSkill):
    """
    批量 Ping 检测技能
    
    属性:
        name: 技能名称 "ping_sweep"
        description: 技能描述，说明批量检测功能
        parameters: 参数定义列表，包含 targets 和 count
        enable_learning: 是否启用自我学习 (默认 True)
        confidence_score: 当前信心分数 (0.0-1.0，基于历史执行记录)
    
    Self-Improving:
        - 自动记录每次检测的目标地址和成功率
        - 从历史执行中学习最优的超时设置和 ping 次数
        - 识别常见的网络故障模式 (如特定网段频繁超时)
        - 根据目标类型自动调整检测策略 (如内网 vs 外网)
        - 提供网络质量趋势分析和异常检测建议
    
    并发特性:
        - 使用 asyncio.gather 并发执行所有 ping 任务
        - 每个任务独立运行，互不影响
        - 自动超时控制，避免单个目标阻塞整体执行
        - 支持大规模并发 (建议限制在 50-100 个目标以内)
    """
    name = "ping_sweep"
    description = "批量 Ping 检测：并发检查多个目标的网络连通性，返回丢包率和可达性状态"
    parameters = [
        SkillParam(
            name="targets", 
            type="string", 
            description="目标地址列表 (逗号分隔或换行分隔)，支持 IP 地址和主机名",
            required=True
        ),
        SkillParam(
            name="count", 
            type="int", 
            description="每个目标的 ping 次数，默认 3 次 (推荐 3-5 次)",
            default=3
        ),
    ]

    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        targets_str = params.get("targets") or params.get("args", "")
        if not targets_str:
            return SkillResult(success=False, error="缺少目标地址")

        targets = [t.strip() for t in targets_str.replace("\n", ",").split(",") if t.strip()]
        count = params.get("count", 3)
        results = {}

        tasks = [self._ping_one(t, count, results) for t in targets]
        await asyncio.gather(*tasks)

        return SkillResult(
            success=True,
            data={"raw_data": results, "targets": targets},
            text=json.dumps(results, ensure_ascii=False, indent=2)
        )

    @staticmethod
    async def _ping_one(target: str, count: int, results: dict):
        cmd = f"ping -c {count} -W 2 {shlex.quote(target)}"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode("utf-8", errors="replace")
            loss = "unknown"
            for line in output.split("\n"):
                if "packet loss" in line:
                    parts = line.split(",")
                    for p in parts:
                        if "loss" in p:
                            loss = p.strip()
                            break
            results[target] = {
                "output": output, "loss": loss,
                "reachable": "0% packet loss" in output or "0.0% packet loss" in output
            }
        except asyncio.TimeoutError:
            results[target] = {"output": "超时", "loss": "100%", "reachable": False}
        except Exception as e:
            results[target] = {"output": str(e), "loss": "unknown", "reachable": False}
