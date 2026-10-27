"""Ed25519 私钥仅存系统 keyring；拒绝明文/替代后端。"""
import base64
import hashlib
import secrets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from .security import identifier, require, text


class Identity:
    def __init__(self, config):
        import keyring
        from keyring.backends import chainer
        backend = keyring.get_keyring()
        allowed = {"keyring.backends.SecretService", "keyring.backends.macOS", "keyring.backends.Windows"}
        if isinstance(backend, chainer.ChainerBackend):
            candidates = [b for b in backend.backends if type(b).__module__ in allowed]
            require(bool(candidates), "secure_keyring_unavailable")
            backend = candidates[0]
        require(type(backend).__module__ in allowed, "secure_keyring_unavailable")
        self.backend = backend
        self.config = config
        self.service = "zops-companion:" + hashlib.sha256(config.server.encode()).hexdigest()[:32]

    def key(self, create=False):
        cfg = self.config
        if not cfg.key_id:
            require(create and not cfg.device_id, "device_key_missing")
            cfg.key_id = secrets.token_hex(16)
        raw = self.backend.get_password(self.service, cfg.key_id)
        if raw is None:
            require(create and not cfg.device_id, "device_key_missing")
            private = Ed25519PrivateKey.generate()
            raw = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
            self.backend.set_password(self.service, cfg.key_id, raw)
            require(self.backend.get_password(self.service, cfg.key_id) == raw, "keyring_write_failed")
        try:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw, validate=True))
        except Exception:
            raise RuntimeError("device_key_invalid") from None

    def public(self):
        return base64.b64encode(self.key(create=True).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def sign(self, challenge_id, nonce):
        identifier(challenge_id)
        text(nonce, 4096)
        require(bool(nonce), "invalid_challenge")
        return base64.b64encode(self.key().sign((challenge_id + ":" + nonce).encode("utf-8"))).decode()
