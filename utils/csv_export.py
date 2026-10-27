"""
Z-OPS — CSV 导出工具
从 LLM 回复中的 Markdown 表格自动提取数据，生成可下载的 CSV 文件。
"""

import csv
import io
import os
import re
import time
import logging
from pathlib import Path
from typing import Optional, List, Tuple

logger = logging.getLogger("zops")

# 下载文件存储目录
DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# 文件保留时间 (秒): 7 天
FILE_TTL = 7 * 24 * 3600


def parse_markdown_tables(text: str) -> List[List[List[str]]]:
    """
    从 Markdown 文本中提取所有表格。
    
    Args:
        text: Markdown 格式的文本
    
    Returns:
        表格列表，每个表格是行列表，每行是单元格列表
        [
            [                       # 表格 1
                ["列1", "列2"],     # 表头行
                ["值1", "值2"],     # 数据行
            ],
            ...
        ]
    """
    tables = []
    lines = text.split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # 检测表格行 (至少包含一个 |)
        if '|' in line and line.startswith('|'):
            table_lines = []
            
            # 收集连续的表格行
            while i < len(lines) and '|' in lines[i].strip() and lines[i].strip().startswith('|'):
                row = lines[i].strip()
                
                # 跳过分隔行 (如 |---|---|)
                if re.match(r'^\|[\s\-:|]+\|$', row):
                    i += 1
                    continue
                
                # 解析单元格
                cells = [c.strip() for c in row.split('|')]
                # 移除首尾空元素 (因为 |col1|col2| 分割后首尾为空)
                if cells and cells[0] == '':
                    cells = cells[1:]
                if cells and cells[-1] == '':
                    cells = cells[:-1]
                
                if cells:
                    table_lines.append(cells)
                
                i += 1
            
            if len(table_lines) >= 2:  # 至少需要表头 + 1行数据
                tables.append(table_lines)
        else:
            i += 1
    
    return tables


def clean_cell_value(value: str) -> str:
    """清理单元格值：移除 Markdown 格式标记"""
    # 移除 emoji 状态标记
    value = re.sub(r'[🔴🟡🟢⚠️✅❌⛔]', '', value)
    # 移除 bold/italic
    value = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', value)
    # 移除 inline code
    value = re.sub(r'`([^`]+)`', r'\1', value)
    # 移除 links [text](url)
    value = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', value)
    # 移除 HTML tags
    value = re.sub(r'<[^>]+>', '', value)
    return value.strip()


def generate_csv_filename(query: str, suffix: str = "") -> str:
    """
    生成 CSV 文件名
    
    Args:
        query: 用户查询文本
        suffix: 可选后缀 (多表格时区分)
    
    Returns:
        文件名 (如 "vm_list_20260626_160403.csv")
    """
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    # 从查询中提取有意义的关键词作为文件名前缀
    prefix = "export"
    if any(k in query for k in ['虚拟机', 'vm', 'VM']):
        prefix = "vm_list"
    elif any(k in query for k in ['备份', 'backup']):
        prefix = "backup_list"
    elif any(k in query for k in ['快照', 'snapshot']):
        prefix = "snapshot_list"
    elif any(k in query for k in ['事件', 'event', '告警']):
        prefix = "events"
    elif any(k in query for k in ['客户', 'crm', 'GID', 'gid']):
        prefix = "customer"
    elif any(k in query for k in ['VCD', 'vcd']):
        prefix = "vcd_resources"
    elif any(k in query for k in ['CMDB', 'cmdb', '主机', '服务器']):
        prefix = "cmdb_hosts"
    elif any(k in query for k in ['DNS', 'dns']):
        prefix = "dns_records"
    elif any(k in query for k in ['存储', 'storage', '仓库']):
        prefix = "storage"
    elif any(k in query for k in ['监控', 'monitor', '监控']):
        prefix = "monitoring"
    elif '列表' in query or 'list' in query.lower():
        prefix = "data_list"
    
    if suffix:
        return f"{prefix}_{suffix}_{timestamp}.csv"
    return f"{prefix}_{timestamp}.csv"


def tables_to_csv(tables: List[List[List[str]]], query: str = "") -> List[Tuple[str, str]]:
    """
    将 Markdown 表格转换为 CSV 文件。
    多表格相同表头时自动合并为一个文件。
    """
    results = []
    
    # 清理过期文件 (懒清理)
    _cleanup_old_files()
    
    # 过滤有效表格 (至少 2 行: 表头 + 1 条数据)
    valid_tables = [t for t in tables if len(t) >= 2]
    if not valid_tables:
        return results
    
    # 多表格且表头相同时，合并为一个文件
    if len(valid_tables) > 1:
        headers = [t[0] for t in valid_tables]
        # 比较表头是否相同 (允许列数不同但前 N 列相同)
        first_header = headers[0]
        all_same = all(
            h[:len(first_header)] == first_header[:len(h)] for h in headers
        )
        if all_same:
            # 合并: 表头 + 所有数据行
            merged = [first_header]
            for table in valid_tables:
                merged.extend(table[1:])  # 跳过表头，追加数据行
            valid_tables = [merged]
            logger.info(f"[csv_export] 合并 {len(headers)} 个相同表头表格 → {len(merged)-1} 行数据")
    
    for idx, table in enumerate(valid_tables):
        if len(table) < 2:
            continue
        
        suffix = str(idx + 1) if len(valid_tables) > 1 else ""
        filename = generate_csv_filename(query, suffix)
        filepath = DOWNLOADS_DIR / filename
        
        # 写入 CSV (UTF-8 BOM 确保 Excel 正确识别中文)
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            for row in table:
                cleaned_row = [clean_cell_value(cell) for cell in row]
                writer.writerow(cleaned_row)
        
        logger.info(f"[csv_export] 生成文件: {filename} ({len(table)-1} 行数据)")
        results.append((str(filepath), filename))
    
    return results


def export_text_as_csv(text: str, query: str = "") -> List[Tuple[str, str]]:
    """
    从 LLM 回复文本中提取表格并生成 CSV。
    
    Args:
        text: LLM 回复文本 (Markdown 格式)
        query: 用户查询文本
    
    Returns:
        [(file_path, file_name), ...] 列表，空列表表示无表格
    """
    tables = parse_markdown_tables(text)
    if not tables:
        return []
    
    return tables_to_csv(tables, query)


def _cleanup_old_files():
    """清理过期的下载文件"""
    try:
        now = time.time()
        for f in DOWNLOADS_DIR.glob("*.csv"):
            if now - f.stat().st_mtime > FILE_TTL:
                f.unlink()
                logger.debug(f"[csv_export] 清理过期文件: {f.name}")
    except Exception as e:
        logger.warning(f"[csv_export] 清理过期文件失败: {e}")


def get_download_url(filename: str, base_url: str = "") -> str:
    """
    获取文件的下载 URL
    
    Args:
        filename: 文件名
        base_url: API 基础 URL (可选)
    
    Returns:
        下载 URL
    """
    return f"{base_url}/api/download/{filename}"
