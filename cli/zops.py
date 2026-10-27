#!/usr/bin/env python3
"""
Z-OPS v2.0.0 — CLI 主入口
提供配置验证、备份、系统管理等命令行工具
"""

import sys
import argparse
from pathlib import Path

# 添加 Z-OPS 到路径
ZOPS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZOPS_DIR))

from core.secret_ref import print_config_summary, validate_all_secrets


def cmd_config(args):
    """配置检查和验证"""
    print("\n" + "="*60)
    print("Z-OPS 配置验证 v2.0.0")
    print("="*60)
    
    # 打印配置摘要
    print_config_summary()
    
    # 验证敏感配置
    print("\n🔐 验证敏感配置...")
    all_passed, errors = validate_all_secrets(dry_run=args.dry_run)
    
    if all_passed:
        print("\n✅ 所有配置验证通过")
        return 0
    else:
        print("\n❌ 配置验证失败:")
        for err in errors:
            print(f"  {err}")
        
        if not args.dry_run:
            print("\n💡 提示：请通过环境变量或 SecretRef 配置所有必需的敏感信息")
            print("   参考：zops.env.example 模板")
            return 1
        
        return 0


def cmd_backup(args):
    """备份管理"""
    from cli.backup import BackupManager
    
    backup_mgr = BackupManager()
    
    if args.backup_command == "create":
        backup_path = backup_mgr.create_backup(args.name)
        print(f"\n✅ 备份已创建：{backup_path}")
    
    elif args.backup_command == "verify":
        success = backup_mgr.verify_backup(args.name)
        return 0 if success else 1
    
    elif args.backup_command == "list":
        backups = backup_mgr.list_backups()
        if not backups:
            print("暂无备份")
        else:
            print(f"\n{'备份名称':<30} {'创建时间':<25} {'大小':<15} {'状态'}")
            print("-" * 80)
            for b in backups:
                status = "✓" if b["exists"] else "❌"
                size_str = f"{b['size'] / 1024 / 1024:.2f} MB"
                print(f"{b['name']:<30} {b['created_at']:<25} {size_str:<15} {status}")
    
    elif args.backup_command == "restore":
        success = backup_mgr.restore_backup(args.name, args.force)
        return 0 if success else 1
    
    return 0


def cmd_dry_run(args):
    """Dry-run 配置验证"""
    print("\n🔍 Dry-run 模式：验证配置但不实际执行\n")
    
    # 验证敏感配置
    all_passed, errors = validate_all_secrets(dry_run=True)
    
    if all_passed:
        print("\n✅ 所有配置可正常解析")
        return 0
    else:
        print("\n❌ 配置解析失败:")
        for err in errors:
            print(f"  {err}")
        return 1


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="Z-OPS CLI 工具 v2.0.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  zops config                    # 检查配置
  zops config --dry-run         # 仅验证，不抛出异常
  zops backup create            # 创建备份
  zops backup list              # 列出备份
  zops backup verify NAME       # 验证备份
  zops backup restore NAME      # 恢复备份
  zops dry-run                  # Dry-run 配置验证
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="命令")
    
    # config 命令
    config_parser = subparsers.add_parser("config", help="检查配置")
    config_parser.add_argument("--dry-run", action="store_true", help="仅验证，不抛出异常")
    config_parser.set_defaults(func=cmd_config)
    
    # backup 命令
    backup_parser = subparsers.add_parser("backup", help="备份管理")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command")
    
    # backup create
    create_parser = backup_subparsers.add_parser("create", help="创建备份")
    create_parser.add_argument("--name", "-n", help="备份名称")
    
    # backup list
    backup_subparsers.add_parser("list", help="列出所有备份")
    
    # backup verify
    verify_parser = backup_subparsers.add_parser("verify", help="验证备份")
    verify_parser.add_argument("name", help="备份名称")
    
    # backup restore
    restore_parser = backup_subparsers.add_parser("restore", help="恢复备份")
    restore_parser.add_argument("name", help="备份名称")
    restore_parser.add_argument("--force", "-f", action="store_true", help="强制覆盖")
    
    backup_parser.set_defaults(func=cmd_backup)
    
    # dry-run 命令
    dryrun_parser = subparsers.add_parser("dry-run", help="Dry-run 配置验证")
    dryrun_parser.set_defaults(func=cmd_dry_run)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return 0
    
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
