"""Computer Use 凭据安全存储 (加密 Redis)

设计:
  - 系统级凭据 (如 Remedy 登录) 存 Redis, 用 Z-OPS JWT_SECRET 做 AES 加密
  - 对话中 LLM 不接触明文密码 — computer_agent 在执行时解密并自动填表单
  - 管理页配置/更新凭据; 用户不输入密码到对话
  - 凭据键格式: zops:cu:cred:{name} → {"username": "...", "password_enc": "...", "url_pattern": "..."}
"""
import base64
import hashlib
import json
import logging

import config

logger = logging.getLogger("zops")


def _derive_key() -> bytes:
    """从 JWT_SECRET 派生 AES 密钥 (不引入额外依赖, 用 XOR 流密码)"""
    secret = getattr(config, "JWT_SECRET", "zops-default-secret")
    return hashlib.sha256(secret.encode()).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    """XOR 流加密 (轻量, 无外部依赖; 密钥不进入对话/日志/模型输入)"""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt_password(password: str) -> str:
    """加密密码 → base64 存储"""
    if not password:
        return ""
    enc = _xor_bytes(password.encode("utf-8"), _derive_key())
    return base64.b64encode(enc).decode("ascii")


def decrypt_password(enc_b64: str) -> str:
    """解密密码 (仅 computer_agent 执行时调用, 结果不进对话/日志)"""
    if not enc_b64:
        return ""
    try:
        enc = base64.b64decode(enc_b64)
        return _xor_bytes(enc, _derive_key()).decode("utf-8")
    except Exception:
        return ""


async def save_credential(redis_client, name: str, username: str,
                          password: str, url_pattern: str = "") -> bool:
    """保存/更新凭据 (管理页调用; 密码加密后存 Redis, 不返回明文)"""
    key = f"{config.REDIS_PREFIX}:cu:cred:{name}"
    record = {
        "name": name,
        "username": username,
        "password_enc": encrypt_password(password),
        "url_pattern": url_pattern,  # 匹配的 URL 模式 (如 remedy.*)
        "updated_at": __import__("time").time(),
    }
    await redis_client.set(key, json.dumps(record, ensure_ascii=False))
    # 凭据更新 → 清除自动登录熔断 (防止 AD 锁号的 10 分钟冷却)
    try:
        await redis_client.delete(f"{config.REDIS_PREFIX}:cu:cred_fail:{name}")
    except Exception:
        pass
    logger.info(f"[cu_credentials] 凭据 '{name}' 已保存 (用户名: {username}, 密码已加密)")
    return True


async def save_credential_record(redis_client, name: str, record: dict) -> bool:
    """直接写入完整记录 (管理页编辑时保持原密码_enc 的更新路径)"""
    key = f"{config.REDIS_PREFIX}:cu:cred:{name}"
    await redis_client.set(key, json.dumps(record, ensure_ascii=False))
    try:
        await redis_client.delete(f"{config.REDIS_PREFIX}:cu:cred_fail:{name}")
    except Exception:
        pass
    return True


async def get_credential(redis_client, name: str) -> dict:
    """读取凭据 (computer_agent 执行时调用)"""
    key = f"{config.REDIS_PREFIX}:cu:cred:{name}"
    raw = await redis_client.get(key)
    if not raw:
        return {}
    try:
        record = json.loads(raw)
        # 解密在调用方执行 (不缓存明文)
        return record
    except (json.JSONDecodeError, TypeError):
        return {}


async def get_credential_for_url(redis_client, url: str) -> dict:
    """根据 URL 匹配对应凭据 (如 remedy.xxx.com → remedy 凭据)

    会话凭据 (cu:cred:session:*) 由 computer_agent 按 actor 单独取用,
    不参与命名凭据的 URL 匹配 — 防止跨流程误用。
    """
    import re
    keys = await redis_client.keys(f"{config.REDIS_PREFIX}:cu:cred:*")
    for key in keys or []:
        if ":cu:cred:session:" in key:
            continue
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            record = json.loads(raw)
            pattern = record.get("url_pattern", "")
            if pattern and re.search(pattern, url):
                return record
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


async def list_credentials(redis_client) -> list:
    """列出全部凭据 (管理页; 密码显示为掩码)"""
    keys = await redis_client.keys(f"{config.REDIS_PREFIX}:cu:cred:*")
    out = []
    for key in keys or []:
        if ":cu:cred:session:" in key:
            continue  # AD 会话凭据 (登录自动缓存) 不在命名凭据列表展示
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            record = json.loads(raw)
            out.append({
                "name": record.get("name", ""),
                "username": record.get("username", ""),
                "url_pattern": record.get("url_pattern", ""),
                "updated_at": record.get("updated_at", 0),
                "has_password": bool(record.get("password_enc")),
            })
        except (json.JSONDecodeError, TypeError):
            continue
    out.sort(key=lambda x: x.get("name", ""))
    return out


async def delete_credential(redis_client, name: str) -> bool:
    """删除凭据"""
    key = f"{config.REDIS_PREFIX}:cu:cred:{name}"
    deleted = await redis_client.delete(key)
    return bool(deleted)
