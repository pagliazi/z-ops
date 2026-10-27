"""仅本机配置授权；配置不含私钥、票据、登录凭据。"""
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from .security import decode, encode, fields, identifier, no_links, origin, private_dir, require, server_url, text


@dataclass
class Config:
    server: str
    name: str
    device_id: str = ""
    key_id: str = ""
    actor_id: str = ""
    roots: list = field(default_factory=list)
    origins: list = field(default_factory=list)
    scripts: dict = field(default_factory=dict)
    native_origins: list = field(default_factory=list)
    # 桌面应用白名单: 只登记进程名 (小写); 未列出的应用窗口一律不可接管。
    desktop_apps: list = field(default_factory=list)
    path: Path | None = None

    @property
    def state(self):
        require(self.path is not None, "configuration_required")
        return self.path.parent / (self.path.stem + ".state")

    def validate(self):
        self.server = server_url(self.server)
        text(self.name, 100)
        require(bool(self.name), "name_required")
        for value in (self.device_id, self.key_id, self.actor_id):
            if value:
                identifier(value)
        require(type(self.roots) is list and len(self.roots) <= 32)
        for root in self.roots:
            # 旧配置没有 manage; 默认关闭破坏性操作。
            root.setdefault("manage", False)
            fields(root, ("path", "read", "write_new", "manage"))
            no_links(root["path"])
            require(Path(root["path"]).is_dir(), "root_not_directory")
            require(type(root["read"]) is bool and type(root["write_new"]) is bool
                    and type(root["manage"]) is bool)
        require(type(self.origins) is list and len(self.origins) <= 64)
        for site in self.origins:
            require(site == origin(site), "canonical_origin_required")
        require(type(self.scripts) is dict and len(self.scripts) <= 32)
        from .scripts import validate_entry
        for name, entry in self.scripts.items():
            identifier(name)
            validate_entry(entry)
        require(type(self.native_origins) is list and len(self.native_origins) <= 16)
        for entry in self.native_origins:
            require(isinstance(entry, str) and entry.startswith("chrome-extension://")
                    and entry.endswith("/") and len(entry) == 52
                    and all(c in "abcdefghijklmnop" for c in entry[19:-1]), "invalid_extension_origin")
        require(type(self.desktop_apps) is list and len(self.desktop_apps) <= 16
                and len(set(self.desktop_apps)) == len(self.desktop_apps), "invalid_desktop_apps")
        for app in self.desktop_apps:
            # 进程名小写规范化; 不接受路径、通配符或窗口标题。
            require(isinstance(app, str) and re.fullmatch(r"[a-z0-9._-]{1,64}", app) is not None,
                    "invalid_desktop_app")
        return self

    def public(self):
        return {k: getattr(self, k) for k in (
            "server", "name", "device_id", "key_id", "actor_id", "roots", "origins", "scripts",
            "native_origins", "desktop_apps")}

    @classmethod
    def load(cls, path):
        path = no_links(Path(path).absolute())
        if os.name != "nt":
            s = path.stat()
            require(s.st_uid == os.getuid() and not s.st_mode & 0o077, "configuration_not_private")
        with path.open("rb") as stream:
            data = decode(stream.read(262145))
        fields(data, ("server", "name"), (
            "device_id", "key_id", "actor_id", "roots", "origins", "scripts", "native_origins",
            "desktop_apps"))
        cfg = cls(**data, path=path).validate()
        private_dir(cfg.state)
        return cfg

    def save(self):
        self.validate()
        require(self.path is not None, "configuration_required")
        private_dir(self.path.parent)
        no_links(self.path, missing_leaf=True)
        tmp = self.path.with_name(".config-" + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encode(self.public(), 262144))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        private_dir(self.state)


def default_path():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) if os.name == "nt" else Path.home() / ".config"
    return base / "zops-companion" / "config.json"
