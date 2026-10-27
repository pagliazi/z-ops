#!/usr/bin/env python3
"""检查 Z-OPS WebChat 服务状态"""

import subprocess
import sys

def check_service():
    print("=" * 60)
    print("Z-OPS WebChat 服务状态检查")
    print("=" * 60)
    
    # 1. 检查进程
    print("\n1. 检查进程...")
    result = subprocess.run(['pgrep', '-f', 'webchat_api.py'], 
                          capture_output=True, text=True)
    if result.returncode == 0:
        pid = result.stdout.strip()
        print(f"   ✓ 进程正在运行 (PID: {pid})")
    else:
        print("   ✗ 进程未运行")
        return False
    
    # 2. 检查端口监听
    print("\n2. 检查端口监听...")
    result = subprocess.run(['ss', '-tlnp'], 
                          capture_output=True, text=True)
    if '7789' in result.stdout:
        print("   ✓ 端口 7789 正在监听")
    else:
        print("   ✗ 端口 7789 未监听")
        return False
    
    # 3. HTTP 请求测试
    print("\n3. HTTP 请求测试...")
    try:
        import requests
        response = requests.get('http://localhost:7789/', timeout=5)
        print(f"   ✓ HTTP 状态码：{response.status_code}")
        if response.status_code == 200:
            print("   ✓ 服务响应正常")
            return True
        else:
            print(f"   ⚠ 服务返回异常状态码：{response.status_code}")
            return False
    except Exception as e:
        print(f"   ✗ 请求失败：{e}")
        return False

if __name__ == '__main__':
    success = check_service()
    sys.exit(0 if success else 1)
