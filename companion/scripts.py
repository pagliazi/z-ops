"""本机登记的 Python 脚本；固定解释器、哈希和严格标量 schema，不接受代码。"""
import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from .security import MAX_TEXT, fields, identifier, integer, no_links, require, text
from .processes import minimal_env

# 与 core/script_exec.py 的登记结构对齐: 平台、权限、目标目录、版本与取消元信息。
PLATFORMS = {"windows", "linux", "darwin"}
PERMISSIONS = {"read", "write"}
METADATA = ("platform", "permissions", "target_dirs", "version", "cancel")


def platform_name():
    return "windows" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"


def validate_entry(entry):
    fields(entry, ("path", "sha256", "executable", "executable_sha256", "schema", "timeout"),
           METADATA)
    require(Path(entry["path"]).suffix == ".py", "only_registered_python_supported")
    require(Path(entry["executable"]).is_absolute(), "absolute_executable_required")
    require(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(entry["executable"]).name,
                         re.IGNORECASE), "python_executable_required")
    for key in ("sha256", "executable_sha256"):
        require(isinstance(entry[key], str) and re.fullmatch(r"[0-9a-f]{64}", entry[key]))
    integer(entry["timeout"], 1, 60)
    platforms = entry.get("platform", [platform_name()])
    require(type(platforms) is list and 0 < len(platforms) <= len(PLATFORMS)
            and set(platforms) <= PLATFORMS, "invalid_script_platform")
    permissions = entry.get("permissions", ["read"])
    require(type(permissions) is list and 0 < len(permissions)
            and set(permissions) <= PERMISSIONS, "invalid_script_permissions")
    target_dirs = entry.get("target_dirs", [])
    require(type(target_dirs) is list and len(target_dirs) <= 8, "invalid_script_target_dirs")
    for value in target_dirs:
        require(isinstance(value, str) and Path(value).is_absolute(), "invalid_script_target_dirs")
    version = entry.get("version", 1)
    require((type(version) is int and version >= 1)
            or (isinstance(version, str) and 0 < len(version) <= 64), "invalid_script_version")
    cancel = entry.get("cancel", {"grace_s": 5})
    require(type(cancel) is dict and set(cancel) <= {"grace_s"}
            and type(cancel.get("grace_s", 5)) is int
            and 1 <= cancel.get("grace_s", 5) <= 30, "invalid_script_cancel")
    schema = entry["schema"]
    fields(schema, ("type", "properties", "required", "additionalProperties"))
    require(schema["type"] == "object" and schema["additionalProperties"] is False)
    require(type(schema["properties"]) is dict and len(schema["properties"]) <= 16)
    require(type(schema["required"]) is list and set(schema["required"]) <= schema["properties"].keys())
    for key, rule in schema["properties"].items():
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key))
        fields(rule, ("type",), ("enum", "maxLength", "minimum", "maximum"))
        require(rule["type"] in ("string", "integer", "boolean"))
        if "maxLength" in rule:
            integer(rule["maxLength"], 0, 2048)
        if "enum" in rule:
            require(type(rule["enum"]) is list and len(rule["enum"]) <= 100)
    # 回写归一化后的元信息, 便于本机配置与界面展示版本/权限/目录/取消约定。
    return {**entry, "platform": platforms, "permissions": permissions,
            "target_dirs": target_dirs, "version": version,
            "cancel": {"grace_s": cancel.get("grace_s", 5)}}


def arguments(schema, args):
    fields(args, schema["required"], schema["properties"].keys())
    result = []
    for key, value in args.items():
        rule = schema["properties"][key]
        if rule["type"] == "string":
            text(value, rule.get("maxLength", 2048))
        elif rule["type"] == "integer":
            integer(value, rule.get("minimum", -2**31), rule.get("maximum", 2**31 - 1))
        else:
            require(type(value) is bool)
        if "enum" in rule:
            require(value in rule["enum"])
        result.append(f"--{key}={str(value).lower() if type(value) is bool else value}")
    return result


def verified_fd(path, digest, limit):
    path = no_links(path)
    from .files import Files
    with Files(None).directory(path.parent) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size <= limit, "unsafe_script_file")
        h = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, 65536):
            count += len(chunk)
            require(count <= limit, "script_too_large")
            h.update(chunk)
        require(h.hexdigest() == digest, "script_hash_mismatch")
        os.lseek(fd, 0, 0)
        return fd
    except BaseException:
        os.close(fd)
        raise


async def run_script(owner, config, params):
    fields(params, ("name", "args"))
    name = identifier(params["name"])
    require(name in config.scripts, "script_not_registered")
    entry = validate_entry(config.scripts[name])
    # 平台元信息不匹配时不执行, 由服务端如实报告能力不可用。
    require(platform_name() in entry.get("platform", [platform_name()]), "script_platform_unsupported")
    argv = arguments(entry["schema"], params["args"])
    if os.name == "nt":
        return await run_windows(owner, config, entry, argv)
    require(os.name == "posix" and Path("/proc/self/fd").exists(), "script_backend_unavailable")
    import fcntl
    import ctypes
    memfd_create = ctypes.CDLL(None, use_errno=True).memfd_create
    memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    memfd_create.restype = ctypes.c_int
    script = memfd_create(b"companion-registered", 0x0002 | 0x0001)
    require(script >= 0, "sealed_script_backend_unavailable")
    original = None
    try:
        original = verified_fd(entry["path"], entry["sha256"], 1024 * 1024)
        source = bytearray()
        while chunk := os.read(original, 65536):
            source.extend(chunk)
            require(len(source) <= 1024 * 1024, "script_too_large")
        require(hashlib.sha256(source).hexdigest() == entry["sha256"], "script_changed_during_open")
        os.write(script, source)
        os.lseek(script, 0, 0)
        # Linux UAPI F_ADD_SEALS；兼容未导出这些常量的 Python standalone 构建。
        fcntl.fcntl(script, 1033, 0x000f)
    except BaseException:
        os.close(script)
        raise
    finally:
        if original is not None:
            os.close(original)
    try:
        executable = verified_fd(entry["executable"], entry["executable_sha256"], 64 * 1024 * 1024)
        try:
            with tempfile.TemporaryDirectory(prefix="script-", dir=config.state) as cwd:
                try:
                    out, err, code = await owner.run(
                        [f"/proc/self/fd/{executable}", "-I", "-B", f"/proc/self/fd/{script}", *argv],
                        cwd=cwd, env=minimal_env(), timeout=entry["timeout"], limit=MAX_TEXT,
                        pass_fds=(script, executable))
                except Exception:
                    raise RuntimeError("script_outcome_unknown") from None
                return {"exit_code": code, "stdout": out.decode("utf-8", "replace"),
                        "stderr": err.decode("utf-8", "replace")}
        finally:
            os.close(executable)
    finally:
        os.close(script)


def verified_bytes(path, digest, limit):
    path = no_links(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "unsafe_script_file")
    raw = path.read_bytes()
    require(len(raw) <= limit and hashlib.sha256(raw).hexdigest() == digest, "script_hash_mismatch")
    return raw


async def run_windows(owner, config, entry, argv):
    """Windows: 校验后复制到任务私有目录运行, 子进程树由 Job Object 约束。"""
    source = verified_bytes(entry["path"], entry["sha256"], 1024 * 1024)
    verified_bytes(entry["executable"], entry["executable_sha256"], 64 * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="script-", dir=config.state) as cwd:
        script = Path(cwd) / "registered.py"
        fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            out, err, code = await owner.run(
                [entry["executable"], "-I", "-B", str(script), *argv],
                cwd=cwd, env=minimal_env(cwd), timeout=entry["timeout"], limit=MAX_TEXT)
        except Exception:
            raise RuntimeError("script_outcome_unknown") from None
        return {"exit_code": code, "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace")}
