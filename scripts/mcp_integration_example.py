"""
Z-OPS v4 — MCP 工具集成示例

展示如何在系统启动时自动发现并注册 MCP 工具
"""

import asyncio
import logging
from core.tool_system import ToolPool
from core.mcp_adapter import register_mcp_tools, MCPToolAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("Z-OPS v4 - MCP 工具集成示例")
    logger.info("=" * 60)
    
    # 创建工具池
    tool_pool = ToolPool()
    
    # 方式1: 使用便捷函数 (推荐)
    logger.info("\n[方式1] 使用便捷函数注册 MCP 工具...")
    try:
        mcp_count = await register_mcp_tools(tool_pool)
        logger.info(f"✅ 成功注册 {mcp_count} 个 MCP 工具")
    except Exception as e:
        logger.error(f"❌ MCP 工具注册失败: {e}")
        mcp_count = 0
    
    # 方式2: 使用适配器 (更灵活)
    logger.info("\n[方式2] 使用 MCPToolAdapter...")
    try:
        adapter = MCPToolAdapter(tool_pool)
        
        # 发现工具
        tools = await adapter.discover_tools()
        logger.info(f"📋 发现 {len(tools)} 个 MCP 工具:")
        for tool in tools[:5]:  # 只显示前5个
            logger.info(f"  - {tool.get('name')}: {tool.get('description', '')[:50]}")
        if len(tools) > 5:
            logger.info(f"  ... 还有 {len(tools) - 5} 个工具")
        
        # 注册工具
        count = await adapter.register_all()
        logger.info(f"✅ 通过适配器注册 {count} 个工具")
        
    except Exception as e:
        logger.error(f"❌ 适配器注册失败: {e}")
    
    # 查看已注册的工具
    logger.info("\n" + "=" * 60)
    logger.info("📊 当前 ToolPool 状态:")
    logger.info("=" * 60)
    
    all_tools = tool_pool.get_all_definitions()
    logger.info(f"总计: {len(all_tools)} 个工具")
    
    # 分类统计
    mcp_tools = [t for t in all_tools if 'mcp' in str(t.get('function', {})).lower()]
    builtin_tools = [t for t in all_tools if t not in mcp_tools]
    
    logger.info(f"  - 内置工具: {len(builtin_tools)} 个")
    logger.info(f"  - MCP 工具: {len(mcp_tools)} 个")
    
    # 列出所有工具
    logger.info("\n📝 工具列表:")
    for i, tool_def in enumerate(all_tools, 1):
        func = tool_def.get('function', {})
        name = func.get('name', 'unknown')
        desc = func.get('description', '')[:60]
        logger.info(f"  {i:2d}. {name:30s} - {desc}")
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ MCP 工具集成示例完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
