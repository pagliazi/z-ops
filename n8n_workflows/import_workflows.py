#!/usr/bin/env python3
"""
N8N 工作流自动导入脚本
将 n8n_workflows/*.json 批量导入到 N8N 实例

用法:
  python import_workflows.py                 # 导入所有工作流
  python import_workflows.py --activate      # 导入并激活
  python import_workflows.py --dry-run       # 仅列出，不导入
  python import_workflows.py --file backup_check.json  # 导入指定文件
"""

import os
import sys
import json
import glob
import argparse
import requests
from datetime import datetime

# 添加项目根路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 默认配置
DEFAULT_N8N_URL = "http://localhost:5678"
DEFAULT_API_KEY = ""

WORKFLOW_DIR = os.path.dirname(os.path.abspath(__file__))


def load_n8n_config():
    """从 n8n.env 加载配置"""
    env_file = os.path.join(PROJECT_ROOT, "n8n.env")
    config = {
        "url": DEFAULT_N8N_URL,
        "api_key": DEFAULT_API_KEY,
    }
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key == "N8N_URL":
                    config["url"] = val
                elif key == "N8N_API_KEY":
                    config["api_key"] = val
    return config


def get_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-N8N-API-Key"] = api_key
    return h


def get_existing_workflows(base_url: str, headers: dict) -> dict:
    """获取已有工作流 {name: id}"""
    try:
        resp = requests.get(f"{base_url}/api/v1/workflows", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            workflows = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(workflows, list):
                return {wf["name"]: wf["id"] for wf in workflows if "name" in wf}
    except Exception as e:
        print(f"  ⚠️  获取已有工作流失败: {e}")
    return {}


def import_workflow(base_url: str, headers: dict, wf_data: dict, existing: dict, activate: bool = False) -> bool:
    """导入单个工作流"""
    name = wf_data.get("name", "unnamed")

    # 清理不需要的字段
    payload = {k: v for k, v in wf_data.items() if k not in ("id", "createdAt", "updatedAt", "versionId")}

    if name in existing:
        # 已存在 → 更新
        wf_id = existing[name]
        try:
            resp = requests.patch(
                f"{base_url}/api/v1/workflows/{wf_id}",
                headers=headers, json=payload, timeout=15
            )
            if resp.status_code in (200, 201):
                print(f"  ✅ 更新: {name} (id={wf_id})")
                if activate:
                    _activate(base_url, headers, wf_id, name)
                return True
            else:
                print(f"  ❌ 更新失败: {name} → HTTP {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  ❌ 更新异常: {name} → {e}")
            return False
    else:
        # 不存在 → 创建
        try:
            resp = requests.post(
                f"{base_url}/api/v1/workflows",
                headers=headers, json=payload, timeout=15
            )
            if resp.status_code in (200, 201):
                new_id = resp.json().get("id", "?")
                print(f"  ✅ 创建: {name} (id={new_id})")
                if activate:
                    _activate(base_url, headers, new_id, name)
                return True
            else:
                print(f"  ❌ 创建失败: {name} → HTTP {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  ❌ 创建异常: {name} → {e}")
            return False


def _activate(base_url: str, headers: dict, wf_id: str, name: str):
    """激活工作流"""
    try:
        resp = requests.patch(
            f"{base_url}/api/v1/workflows/{wf_id}",
            headers=headers, json={"active": True}, timeout=10
        )
        if resp.status_code == 200:
            print(f"  🟢 已激活: {name}")
        else:
            print(f"  ⚠️  激活失败: {name} → HTTP {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️  激活异常: {name} → {e}")


def main():
    parser = argparse.ArgumentParser(description="导入 N8N 工作流")
    parser.add_argument("--activate", action="store_true", help="导入后自动激活")
    parser.add_argument("--dry-run", action="store_true", help="仅列出工作流，不实际导入")
    parser.add_argument("--file", type=str, help="指定导入的 JSON 文件名")
    parser.add_argument("--url", type=str, help="N8N URL (覆盖 n8n.env)")
    parser.add_argument("--api-key", type=str, help="N8N API Key (覆盖 n8n.env)")
    args = parser.parse_args()

    config = load_n8n_config()
    base_url = (args.url or config["url"]).rstrip("/")
    api_key = args.api_key or config["api_key"]
    headers = get_headers(api_key)

    cst_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"  Z-OPS N8N 工作流导入工具")
    print(f"  目标: {base_url}")
    print(f"  时间: {cst_now}")
    print(f"{'='*50}\n")

    # 收集 JSON 文件
    if args.file:
        json_files = [os.path.join(WORKFLOW_DIR, args.file)]
        if not os.path.exists(json_files[0]):
            print(f"❌ 文件不存在: {json_files[0]}")
            sys.exit(1)
    else:
        json_files = sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.json")))

    if not json_files:
        print("❌ 未找到 JSON 工作流文件")
        sys.exit(1)

    print(f"📋 找到 {len(json_files)} 个工作流文件:\n")
    for f in json_files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            name = data.get("name", "unnamed")
            nodes = len(data.get("nodes", []))
            tags = ", ".join(data.get("tags", []))
            print(f"  • {os.path.basename(f):35s} → {name} ({nodes} 节点) [{tags}]")
        except Exception as e:
            print(f"  ⚠️  {os.path.basename(f):35s} → 解析失败: {e}")

    if args.dry_run:
        print(f"\n✨ Dry-run 完成，共 {len(json_files)} 个工作流")
        return

    # 检查 N8N 可达性
    print(f"\n🔍 检查 N8N 服务...")
    try:
        resp = requests.get(f"{base_url}/healthz", timeout=5)
        print(f"  ✅ N8N 在线 (HTTP {resp.status_code})")
    except Exception as e:
        print(f"  ⚠️  N8N 不可达: {e}")
        print(f"  继续尝试导入...\n")

    # 获取已有工作流
    existing = get_existing_workflows(base_url, headers)
    if existing:
        print(f"  已有 {len(existing)} 个工作流\n")

    # 逐个导入
    print("📥 开始导入...\n")
    success = 0
    failed = 0

    for f in json_files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            if import_workflow(base_url, headers, data, existing, args.activate):
                success += 1
            else:
                failed += 1
        except json.JSONDecodeError as e:
            print(f"  ❌ JSON 解析失败: {os.path.basename(f)} → {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ 异常: {os.path.basename(f)} → {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  导入完成: ✅ {success} 成功, ❌ {failed} 失败")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
