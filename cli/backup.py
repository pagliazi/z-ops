#!/usr/bin/env python3
"""
Z-OPS v2.0.0 — CLI 备份工具
提供备份创建、验证、恢复功能

使用方式:
  zops backup create [--name=backup_name]
  zops backup verify <backup_name>
  zops backup list
  zops backup restore <backup_name>
"""

import os
import sys
import json
import asyncio
import shutil
import tarfile
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import logging

# 添加 Z-OPS 到路径
ZOPS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZOPS_DIR))

import config
from core.secret_ref import print_config_summary, validate_all_secrets

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("zops-cli")


class BackupManager:
    """备份管理器"""
    
    BACKUP_DIR = ZOPS_DIR / "backups"
    
    def __init__(self):
        self.BACKUP_DIR.mkdir(exist_ok=True)
    
    def create_backup(self, name: str = None) -> str:
        """
        创建备份
        
        Args:
            name: 备份名称（默认使用时间戳）
            
        Returns:
            备份文件路径
        """
        if not name:
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        backup_name = f"zops_backup_{name}"
        backup_path = self.BACKUP_DIR / f"{backup_name}.tar.gz"
        manifest_path = self.BACKUP_DIR / f"{backup_name}.json"
        
        logger.info(f"📦 创建备份：{backup_name}")
        
        # 准备备份清单
        manifest = {
            "name": backup_name,
            "created_at": datetime.now().isoformat(),
            "version": "2.0.0",
            "files": [],
            "redis_data": {},
            "checksums": {}
        }
        
        # 备份文件列表
        files_to_backup = [
            "zops.env",
            "webchat.env",
            "feishu.env",
            "config.py",
            "requirements.txt",
            "workspaces",
            "skills",
            "screenshots",
        ]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            
            # 复制文件到临时目录
            for file_or_dir in files_to_backup:
                src = ZOPS_DIR / file_or_dir
                if not src.exists():
                    logger.warning(f"⚠️  跳过不存在的文件：{file_or_dir}")
                    continue
                
                dest = tmpdir_path / file_or_dir
                if src.is_dir():
                    shutil.copytree(src, dest, symlinks=True)
                else:
                    shutil.copy2(src, dest)
                
                manifest["files"].append(file_or_dir)
                logger.info(f"  ✓ {file_or_dir}")
            
            # 导出 Redis 数据
            redis_data = self._export_redis_data()
            manifest["redis_data"] = redis_data
            
            # 保存 manifest
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
            
            # 创建 tar.gz 压缩包
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(tmpdir_path, arcname=backup_name)
            
            # 计算校验和
            checksum = self._calculate_checksum(backup_path)
            manifest["checksums"]["archive"] = checksum
            
            # 更新 manifest（包含 checksum）
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ 备份已创建：{backup_path}")
        logger.info(f"📋 Manifest: {manifest_path}")
        return str(backup_path)
    
    def verify_backup(self, backup_name: str) -> bool:
        """
        验证备份完整性
        
        Args:
            backup_name: 备份名称
            
        Returns:
            是否验证通过
        """
        backup_path = self.BACKUP_DIR / f"{backup_name}.tar.gz"
        manifest_path = self.BACKUP_DIR / f"{backup_name}.json"
        
        if not backup_path.exists():
            logger.error(f"❌ 备份文件不存在：{backup_path}")
            return False
        
        if not manifest_path.exists():
            logger.error(f"❌ Manifest 不存在：{manifest_path}")
            return False
        
        logger.info(f"🔍 验证备份：{backup_name}")
        
        # 验证校验和
        with open(manifest_path) as f:
            manifest = json.load(f)
        
        expected_checksum = manifest.get("checksums", {}).get("archive")
        if expected_checksum:
            actual_checksum = self._calculate_checksum(backup_path)
            if expected_checksum != actual_checksum:
                logger.error(f"❌ 校验和不匹配")
                logger.error(f"   预期：{expected_checksum}")
                logger.error(f"   实际：{actual_checksum}")
                return False
            logger.info(f"✓ 校验和验证通过")
        
        # 验证压缩包内容
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                members = tar.getmembers()
                logger.info(f"✓ 压缩包包含 {len(members)} 个文件")
                
                # 验证 manifest 中的文件
                for file_name in manifest.get("files", []):
                    found = any(file_name in m.name for m in members)
                    if not found:
                        logger.error(f"❌ 缺少文件：{file_name}")
                        return False
                    logger.info(f"  ✓ {file_name}")
        
        except Exception as e:
            logger.error(f"❌ 解压失败：{e}")
            return False
        
        logger.info(f"✅ 备份验证通过：{backup_name}")
        return True
    
    def list_backups(self) -> List[dict]:
        """列出所有备份"""
        backups = []
        
        for manifest_file in self.BACKUP_DIR.glob("*.json"):
            try:
                with open(manifest_file) as f:
                    manifest = json.load(f)
                
                backup_name = manifest.get("name", manifest_file.stem.replace("zops_backup_", ""))
                backup_path = self.BACKUP_DIR / f"{backup_name}.tar.gz"
                
                backups.append({
                    "name": backup_name,
                    "created_at": manifest.get("created_at", "unknown"),
                    "version": manifest.get("version", "unknown"),
                    "size": backup_path.stat().st_size if backup_path.exists() else 0,
                    "exists": backup_path.exists()
                })
            except Exception as e:
                logger.warning(f"读取 manifest 失败 {manifest_file}: {e}")
        
        # 按创建时间排序
        backups.sort(key=lambda x: x["created_at"], reverse=True)
        return backups
    
    def restore_backup(self, backup_name: str, force: bool = False) -> bool:
        """
        恢复备份
        
        Args:
            backup_name: 备份名称
            force: 是否强制覆盖
            
        Returns:
            是否恢复成功
        """
        backup_path = self.BACKUP_DIR / f"{backup_name}.tar.gz"
        manifest_path = self.BACKUP_DIR / f"{backup_name}.json"
        
        if not backup_path.exists():
            logger.error(f"❌ 备份文件不存在：{backup_path}")
            return False
        
        if not manifest_path.exists():
            logger.error(f"❌ Manifest 不存在：{manifest_path}")
            return False
        
        logger.warning(f"⚠️  即将恢复备份：{backup_name}")
        logger.warning(f"⚠️  当前数据将被覆盖！")
        
        if not force:
            response = input("确认继续？[y/N]: ")
            if response.lower() != 'y':
                logger.info("取消恢复")
                return False
        
        # 先验证备份
        if not self.verify_backup(backup_name):
            logger.error("❌ 备份验证失败，拒绝恢复")
            return False
        
        logger.info(f"📥 开始恢复：{backup_name}")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            
            # 解压备份
            with tarfile.open(backup_path, "r:gz") as tar:
                tar.extractall(tmpdir_path)
            
            # 找到解压后的目录
            extracted_dir = tmpdir_path / backup_name
            if not extracted_dir.exists():
                logger.error("❌ 解压后的目录不存在")
                return False
            
            # 恢复文件
            with open(manifest_path) as f:
                manifest = json.load(f)
            
            for file_name in manifest.get("files", []):
                src = extracted_dir / file_name
                dest = ZOPS_DIR / file_name
                
                if not src.exists():
                    logger.warning(f"⚠️  跳过不存在的文件：{file_name}")
                    continue
                
                if dest.exists() and not force:
                    logger.warning(f"⚠️  文件已存在，跳过：{file_name}")
                    continue
                
                if src.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
                
                logger.info(f"  ✓ 恢复：{file_name}")
            
            # 恢复 Redis 数据
            if manifest.get("redis_data"):
                logger.info("📇 恢复 Redis 数据...")
                self._import_redis_data(manifest["redis_data"])
        
        logger.info(f"✅ 备份恢复完成：{backup_name}")
        return True
    
    def _export_redis_data(self) -> dict:
        """导出 Redis 数据"""
        try:
            import redis
            r = redis.from_url(config.REDIS_URL)
            
            data = {}
            
            # 导出所有 zops: 前缀的键
            for key in r.keys("zops:*"):
                key_type = r.type(key).decode()
                
                if key_type == "string":
                    data[key.decode()] = r.get(key).decode()
                elif key_type == "hash":
                    data[key.decode()] = {
                        k.decode(): v.decode()
                        for k, v in r.hgetall(key).items()
                    }
                elif key_type == "list":
                    data[key.decode()] = [
                        v.decode() for v in r.lrange(key, 0, -1)
                    ]
                elif key_type == "set":
                    data[key.decode()] = [
                        v.decode() for v in r.smembers(key)
                    ]
                elif key_type == "zset":
                    data[key.decode()] = {
                        v.decode(): r.zscore(key, v)
                        for v in r.zrange(key, 0, -1)
                    }
            
            logger.info(f"✓ 导出 {len(data)} 个 Redis 键")
            return data
        
        except Exception as e:
            logger.error(f"Redis 导出失败：{e}")
            return {}
    
    def _import_redis_data(self, data: dict):
        """导入 Redis 数据"""
        try:
            import redis
            r = redis.from_url(config.REDIS_URL)
            
            count = 0
            for key, value in data.items():
                if isinstance(value, str):
                    r.set(key, value)
                elif isinstance(value, dict):
                    r.delete(key)
                    r.hset(key, mapping=value)
                elif isinstance(value, list):
                    r.delete(key)
                    if len(value) > 0:
                        r.rpush(key, *value)
                count += 1
            
            logger.info(f"✓ 导入 {count} 个 Redis 键")
        
        except Exception as e:
            logger.error(f"Redis 导入失败：{e}")
    
    def _calculate_checksum(self, file_path: Path) -> str:
        """计算文件 SHA256 校验和"""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


def main():
    """CLI 入口"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Z-OPS 备份工具 v2.0.0",
        formatter_class=argparse.RawDescriptionHelpText
    )
    
    subparsers = parser.add_subparsers(dest="command", help="命令")
    
    # backup create
    create_parser = subparsers.add_parser("create", help="创建备份")
    create_parser.add_argument("--name", "-n", help="备份名称")
    
    # backup verify
    verify_parser = subparsers.add_parser("verify", help="验证备份")
    verify_parser.add_argument("name", help="备份名称")
    
    # backup list
    list_parser = subparsers.add_parser("list", help="列出所有备份")
    
    # backup restore
    restore_parser = subparsers.add_parser("restore", help="恢复备份")
    restore_parser.add_argument("name", help="备份名称")
    restore_parser.add_argument("--force", "-f", action="store_true", help="强制覆盖")
    
    # config check
    config_parser = subparsers.add_parser("config", help="检查配置")
    config_parser.add_argument("--dry-run", action="store_true", help="仅验证，不抛出异常")
    
    args = parser.parse_args()
    
    backup_mgr = BackupManager()
    
    if args.command == "create":
        backup_path = backup_mgr.create_backup(args.name)
        print(f"\n✅ 备份已创建：{backup_path}")
    
    elif args.command == "verify":
        success = backup_mgr.verify_backup(args.name)
        sys.exit(0 if success else 1)
    
    elif args.command == "list":
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
    
    elif args.command == "restore":
        success = backup_mgr.restore_backup(args.name, args.force)
        sys.exit(0 if success else 1)
    
    elif args.command == "config":
        print_config_summary()
        all_passed, errors = validate_all_secrets(dry_run=args.dry_run)
        if all_passed:
            print("✅ 所有配置验证通过")
        else:
            print("❌ 配置验证失败:")
            for err in errors:
                print(f"  {err}")
            sys.exit(1)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
