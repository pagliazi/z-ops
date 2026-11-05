"""
Z-OPS v2.0.0 — SecretRef 敏感配置管理
提供安全的敏感配置注入和验证机制
"""

import os
import re
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("zops")


@dataclass
class SecretRef:
    """
    敏感配置引用
    
    使用方式:
    1. 环境变量：SECRETREF_env=MY_SECRET_KEY
    2. 文件引用：SECRETREF_file=/path/to/secret
    3. 直接值：SECRETREF_value=my_secret (不推荐，仅用于测试)
    """
    ref_type: str  # env | file | value
    ref_value: str
    
    def resolve(self) -> str:
        """解析敏感配置"""
        if self.ref_type == "env":
            value = os.environ.get(self.ref_value)
            if not value:
                raise ValueError(f"SecretRef env '{self.ref_value}' not found")
            return value
        elif self.ref_type == "file":
            path = Path(self.ref_value)
            if not path.exists():
                raise ValueError(f"SecretRef file '{self.ref_value}' not found")
            return path.read_text().strip()
        elif self.ref_type == "value":
            return self.ref_value
        else:
            raise ValueError(f"Invalid SecretRef type: {self.ref_type}")
    
    def __str__(self):
        return f"SecretRef({self.ref_type}=***)"


class SecretRefParser:
    """
    SecretRef 解析器
    
    支持格式:
    - ${env:VAR_NAME} - 从环境变量读取
    - ${file:/path/to/file} - 从文件读取
    - ${value:plain_text} - 直接值（仅测试用）
    """
    
    PATTERN = re.compile(r'\$\{(env|file|value):([^}]+)\}')
    
    @classmethod
    def parse(cls, value: str) -> Any:
        """
        解析配置值
        
        Args:
            value: 配置值，可能是普通字符串或 SecretRef 引用
            
        Returns:
            解析后的值（SecretRef 对象或原始值）
        """
        if not isinstance(value, str):
            return value
        
        match = cls.PATTERN.match(value)
        if match:
            ref_type, ref_value = match.groups()
            return SecretRef(ref_type=ref_type, ref_value=ref_value)
        
        return value
    
    @classmethod
    def resolve(cls, value: Any) -> str:
        """
        解析并获取最终值
        
        Args:
            value: 配置值（可能是 SecretRef 或普通字符串）
            
        Returns:
            解析后的字符串值
        """
        if isinstance(value, SecretRef):
            return value.resolve()
        return str(value) if value is not None else ""


class ConfigValidator:
    """
    配置验证器
    
    功能:
    1. 验证必需的配置项
    2. 检查 SecretRef 是否可解析
    3. 支持 dry-run 模式
    """
    
    # 必需的敏感配置项
    REQUIRED_SECRETS = [
        "JWT_SECRET",
        "WEBCHAT_AUTH_PASS",
        "LDAP_BIND_PASSWORD",
        "VCENTER_PASS",
        "PVE_TOKEN_SECRET",
        "OPSCORE_API_TOKEN",
        "OPSCORE_MGT_PASS",
        "KB_API_TOKEN",
        "HA_API_TOKEN",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "LDAP_ADMIN_USERS",
    ]
    
    @classmethod
    def validate_secrets(cls, dry_run: bool = False) -> Tuple[bool, list]:
        """
        验证所有必需的敏感配置
        
        Args:
            dry_run: 是否仅为验证，不抛出异常
            
        Returns:
            (是否全部通过，错误列表)
        """
        errors = []
        
        for secret_name in cls.REQUIRED_SECRETS:
            value = os.environ.get(secret_name)
            
            if not value:
                errors.append(f"❌ 缺失必需配置：{secret_name}")
                continue
            
            # 尝试解析 SecretRef
            try:
                parsed = SecretRefParser.parse(value)
                if isinstance(parsed, SecretRef):
                    resolved = parsed.resolve()
                    if dry_run:
                        logger.info(f"✓ {secret_name}: SecretRef({parsed.ref_type}) 可解析")
                else:
                    if dry_run:
                        logger.info(f"✓ {secret_name}: 已配置 (长度={len(value)})")
            except Exception as e:
                errors.append(f"❌ {secret_name}: SecretRef 解析失败 - {e}")
        
        all_passed = len(errors) == 0
        
        if not dry_run and not all_passed:
            raise ValueError("\n".join(errors))
        
        return all_passed, errors
    
    @classmethod
    def print_config_summary(cls):
        """打印配置摘要（脱敏）"""
        print("\n" + "="*60)
        print("Z-OPS 配置摘要 (v2.0.0)")
        print("="*60)
        
        # 非敏感配置
        print(f"\n✅ 基础配置:")
        print(f"   APP_NAME: Z-OPS")
        print(f"   REDIS_PREFIX: zops")
        print(f"   LLM_PROVIDER: {os.environ.get('LLM_PROVIDER', 'deepseek')}")
        print(f"   LLM_MODEL: {os.environ.get('LLM_MODEL', 'deepseek-chat')}")
        
        # 敏感配置（仅显示是否已配置）
        print(f"\n🔐 敏感配置:")
        for secret_name in cls.REQUIRED_SECRETS:
            value = os.environ.get(secret_name)
            if value:
                parsed = SecretRefParser.parse(value)
                if isinstance(parsed, SecretRef):
                    print(f"   ✓ {secret_name}: SecretRef({parsed.ref_type})")
                else:
                    # 脱敏显示
                    masked = value[:2] + "***" + value[-2:] if len(value) > 4 else "***"
                    print(f"   ✓ {secret_name}: {masked}")
            else:
                print(f"   ❌ {secret_name}: 未配置")
        
        print("="*60 + "\n")


def get_secret(key: str, required: bool = True) -> Optional[str]:
    """
    获取敏感配置（支持 SecretRef）
    
    Args:
        key: 配置项名称
        required: 是否必需
        
    Returns:
        解析后的值或 None
    """
    value = os.environ.get(key)
    if not value:
        if required:
            raise ValueError(f"Missing required secret: {key}")
        return None
    
    parsed = SecretRefParser.parse(value)
    return SecretRefParser.resolve(parsed)


# 全局单例
_validator = ConfigValidator()


def validate_all_secrets(dry_run: bool = False) -> Tuple[bool, list]:
    """验证所有必需的秘密配置"""
    return _validator.validate_secrets(dry_run)


def print_config_summary():
    """打印配置摘要"""
    _validator.print_config_summary()
