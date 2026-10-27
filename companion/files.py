"""POSIX 目录句柄相对操作，拒绝链接/硬链接，新文件原子发布且不覆盖。"""
import contextlib
import fnmatch
import hashlib
import io
import os
import re
import secrets
import stat
import zipfile
from pathlib import Path
from .security import Denied, MAX_FILE, MAX_TEXT, fields, integer, no_links, require, text


# 日志脱敏只保留排障需要的结构, 凭据/私钥/邮箱/长不透明串不出本机。
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----.*?-----END [A-Z ]{0,32}PRIVATE KEY-----", re.S)
_CREDENTIAL = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|authorization|cookie|session[_-]?id)\b(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{8,}=*")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_OPAQUE_TOKEN = re.compile(r"\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/_\-]{48,}={0,2}\b")


def redact(value):
    """返回脱敏文本与每类命中次数; 不改动数字、时间戳和普通路径。"""
    counts = {}

    def apply(pattern, label, replacement):
        nonlocal value
        value, hits = pattern.subn(replacement, value)
        if hits:
            counts[label] = hits

    apply(_PRIVATE_KEY, "private_key", "[REDACTED PRIVATE KEY]")
    # 先遮蔽 Bearer/Basic 凭据, 避免 Authorization 键先被替换后留下裸 token。
    apply(_BEARER, "bearer", lambda m: f"{m.group(1)} [REDACTED]")
    apply(_CREDENTIAL, "credential", lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]")
    apply(_URL_USERINFO, "url_userinfo", lambda m: f"{m.group(1)}[REDACTED]@")
    apply(_EMAIL, "email", "[REDACTED EMAIL]")
    apply(_OPAQUE_TOKEN, "opaque_token", "[REDACTED]")
    return value, counts


class Files:
    def __init__(self, config):
        self.config = config

    def checked(self, value, write=False, manage=False, missing_leaf=None):
        if missing_leaf is None:
            missing_leaf = write
        path = no_links(text(value), missing_leaf=missing_leaf)
        permission = "manage" if manage else "write_new" if write else "read"
        if os.name == "nt":
            # Windows: realpath 先解析 junction/符号链接, 再校验仍在授权根内;
            # no_links 已逐段拒绝 reparse point。
            resolved = Path(os.path.realpath(path))
            allowed = [Path(os.path.realpath(root["path"])) for root in self.config.roots
                       if root[permission]]
            require(any(resolved == root or root in resolved.parents for root in allowed),
                    "path_not_authorized")
            forbidden = [Path(os.path.realpath(__file__)).parent,
                         Path(os.path.realpath(self.config.path)).parent]
            for entry in self.config.scripts.values():
                forbidden += [Path(os.path.realpath(entry["path"])).parent,
                              Path(os.path.realpath(entry["executable"])).parent]
            require(not any(resolved == x or x in resolved.parents for x in forbidden), "protected_path")
            return path
        forbidden = [Path(__file__).resolve().parent, self.config.path.parent]
        for entry in self.config.scripts.values():
            forbidden += [Path(entry["path"]).parent, Path(entry["executable"]).parent]
        require(not any(path == x or x in path.parents for x in forbidden), "protected_path")
        require(any(root[permission] and (path == Path(root["path"]) or Path(root["path"]) in path.parents)
                    for root in self.config.roots), "path_not_authorized")
        return path

    @contextlib.contextmanager
    def directory(self, path):
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def collect_log(self, params):
        """只读授权目录内日志尾部, 先脱敏再返回; 不跟随链接、不读取设备/配置目录。"""
        fields(params, ("path",), ("max_bytes", "tail"))
        limit = params.get("max_bytes", 65536)
        require(type(limit) is int and 1024 <= limit <= 262144, "invalid_log_limit")
        tail = params.get("tail", True)
        require(type(tail) is bool, "invalid_log_tail")
        raw, truncated = self._read_tail(self.checked(params["path"]), limit, tail)
        body = raw.decode("utf-8", "replace")
        if truncated and tail:
            # 尾读可能从半行开始; 丢掉第一行以免误读拼接内容。
            body = body.partition("\n")[2]
        value, counts = redact(body)
        clipped = len(value) > MAX_TEXT
        if clipped:
            value = value[-MAX_TEXT:]
        return {"text": value, "bytes": len(raw), "truncated": truncated or clipped,
                "sha256": hashlib.sha256(raw).hexdigest(), "redacted": counts}

    def _read_tail(self, path, limit, tail):
        if os.name == "nt":
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode), "regular_file_required")
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                require((opened.st_ino, opened.st_dev) == (info.st_ino, info.st_dev),
                        "file_changed_during_open")
                size = opened.st_size
                if tail and size > limit:
                    stream.seek(size - limit)
                    return stream.read(limit), True
                raw = stream.read(limit + 1)
                return raw[:limit], len(raw) > limit
        with self.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular_file_required")
                if tail and info.st_size > limit:
                    stream.seek(info.st_size - limit)
                    return stream.read(limit), True
                raw = stream.read(limit + 1)
                return raw[:limit], len(raw) > limit

    def read_bytes(self, value, limit=MAX_FILE):
        path = self.checked(value)
        if os.name == "nt":
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "regular_file_required")
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                require((opened.st_ino, opened.st_dev) == (info.st_ino, info.st_dev),
                        "file_changed_during_open")
                raw = stream.read(limit + 1)
            require(len(raw) <= limit, "file_too_large")
            return raw
        with self.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular_file_required")
                require(info.st_size <= limit, "file_too_large")
                raw = stream.read(limit + 1)
                require(len(raw) <= limit, "file_too_large")
                return raw

    def write_bytes(self, value, raw):
        path = self.checked(value, write=True)
        require(len(raw) <= MAX_FILE, "file_too_large")
        # 回执带文件摘要, 供工作流检查点与审计核对实际落盘内容。
        receipt = {"created": True, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if os.name == "nt":
            # 临时文件 + 硬链接发布: 目标已存在即失败, 内容完整后才可见 (NTFS)。
            require(not path.exists(), "target_exists")
            name = path.with_name(".companion-" + secrets.token_hex(12))
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(name, path)
                except OSError:
                    raise Denied("atomic_publish_unavailable") from None
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
            return receipt
        with self.directory(path.parent) as parent:
            name = ".companion-" + secrets.token_hex(12)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # link 的 EEXIST 是不可覆盖保证；目标直到内容完整才可见。
                os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                os.unlink(name, dir_fd=parent)
        return receipt

    def find(self, params):
        """在授权目录内按文件名通配符递归查找；跳过链接、junction 与硬链接。"""
        fields(params, ("path", "pattern"), ("max_results",))
        base = self.checked(params["path"])
        pattern = text(params["pattern"], 64)
        require(pattern and "/" not in pattern and "\\" not in pattern and ".." not in pattern,
                "invalid_pattern")
        limit = integer(params.get("max_results", 50), 1, 200)
        entries, state = [], {"truncated": False}

        def scan(directory, depth):
            try:
                listing = os.scandir(directory)
            except OSError:
                # 个别子目录不可读时继续扫描其余目录, 不让整次查找失败。
                return False
            with listing:
                for entry in listing:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        continue
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    if fnmatch.fnmatchcase(entry.name, pattern):
                        entries.append({"name": entry.name[:200],
                                        "path": str(Path(directory) / entry.name)[:1024],
                                        "directory": is_dir,
                                        "size": 0 if is_dir else info.st_size})
                        if len(entries) >= limit:
                            state["truncated"] = True
                            return True
                    if is_dir and depth < 6 and scan(Path(directory) / entry.name, depth + 1):
                        return True
            return False

        scan(base, 0)
        return {"entries": entries, "truncated": state["truncated"]}

    def move(self, params):
        """在 manage 授权根内移动文件; 硬链接发布, 目标已存在时拒绝覆盖。"""
        fields(params, ("source", "target"))
        source = self.checked(params["source"], manage=True)
        target = self.checked(params["target"], manage=True, missing_leaf=True)
        require(source != target, "invalid_move")
        if os.name == "nt":
            info = source.lstat()
            require(stat.S_ISREG(info.st_mode) and not source.is_symlink(), "regular_file_required")
            require(not target.exists(), "target_exists")
            try:
                os.link(source, target)
            except OSError:
                raise Denied("move_unsupported") from None
            os.unlink(source)
            return {"moved": True, "source": str(source), "target": str(target),
                    "size": info.st_size}
        with contextlib.ExitStack() as stack:
            src = stack.enter_context(self.directory(source.parent))
            dst = stack.enter_context(self.directory(target.parent))
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular_file_required")
                # 目标已存在时 link 返回 EEXIST, 不会静默覆盖。
                os.link(source.name, target.name, src_dir_fd=src, dst_dir_fd=dst,
                        follow_symlinks=False)
                os.unlink(source.name, dir_fd=src)
                os.fsync(dst)
        return {"moved": True, "source": str(source), "target": str(target), "size": info.st_size}

    def delete(self, params):
        """删除 manage 授权根内的单个普通文件; 不递归、不删目录、不跟随链接。"""
        fields(params, ("path",))
        path = self.checked(params["path"], manage=True)
        if os.name == "nt":
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "regular_file_required")
            path.unlink()
            return {"deleted": True, "path": str(path)}
        with self.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular_file_required")
                os.unlink(path.name, dir_fd=parent)
                os.fsync(parent)
        return {"deleted": True, "path": str(path)}

    def replace(self, params):
        """原子覆盖 manage 授权根内已存在的普通文件; 不做隐式备份。"""
        fields(params, ("path", "content"))
        path = self.checked(params["path"], manage=True)
        raw = text(params["content"], 48 * 1024).encode("utf-8")
        require(len(raw) <= 48 * 1024, "file_too_large")
        receipt = {"created": False, "replaced": True, "size": len(raw),
                   "sha256": hashlib.sha256(raw).hexdigest()}
        if os.name == "nt":
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "regular_file_required")
            name = path.with_name(".companion-" + secrets.token_hex(12))
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
            return receipt
        with self.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular_file_required")
            name = ".companion-" + secrets.token_hex(12)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent)
        return receipt

    def dispatch(self, method, params):
        if method == "file.list":
            fields(params, ("path",))
            path = self.checked(params["path"])
            entries = []
            if os.name == "nt":
                for entry in os.scandir(path):
                    if len(entries) >= 500:
                        raise Denied("directory_too_large")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    entries.append({"name": entry.name, "directory": stat.S_ISDIR(info.st_mode),
                                    "size": info.st_size})
                return {"entries": entries}
            with self.directory(path) as fd, os.scandir(fd) as scan:
                for entry in scan:
                    if len(entries) >= 500:
                        raise Denied("directory_too_large")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or info.st_nlink > 1 and stat.S_ISREG(info.st_mode):
                        continue
                    entries.append({"name": entry.name, "directory": stat.S_ISDIR(info.st_mode), "size": info.st_size})
            return {"entries": entries}
        if method == "file.find":
            return self.find(params)
        if method == "file.move":
            return self.move(params)
        if method == "file.delete":
            return self.delete(params)
        if method == "file.replace":
            return self.replace(params)
        if method == "file.read":
            fields(params, ("path",))
            return {"text": self.read_bytes(params["path"], MAX_TEXT).decode("utf-8")}
        if method == "log.collect":
            return self.collect_log(params)
        if method == "file.write_new":
            fields(params, ("path", "content"))
            return self.write_bytes(params["path"], text(params["content"], MAX_TEXT).encode())
        if method == "document.extract":
            fields(params, ("path",))
            raw = self.read_bytes(params["path"])
            return {"text": extract(Path(params["path"]).suffix.lower(), raw)}
        if method == "document.write_new":
            fields(params, ("path",), ("content", "rows"))
            self.checked(params["path"], write=True)
            return self.write_bytes(params["path"], document_bytes(Path(params["path"]).suffix.lower(), params))
        raise Denied("unsupported_method")


def safe_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        require(len(infos) <= 1000 and sum(x.file_size for x in infos) <= 32 * 1024 * 1024, "document_zip_limit")
        require(all(x.file_size <= 8 * 1024 * 1024 and x.file_size <= max(1, x.compress_size) * 100
                    and not x.flag_bits & 1 for x in infos), "document_zip_limit")
        # XML 不允许实体/DTD；宏、外部关系不参与执行。
        for item in infos:
            if item.filename.endswith((".xml", ".rels")):
                data = archive.read(item)
                require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "unsafe_document_xml")


def extract(suffix, raw):
    parts = []
    size = 0
    def add(value):
        nonlocal size
        value = str(value or "")
        size += len(value.encode()) + 1
        require(size <= MAX_TEXT, "document_text_limit")
        parts.append(value)
    if suffix in (".docx", ".xlsx"):
        safe_zip(raw)
    if suffix == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(raw))
        for para in doc.paragraphs:
            add(para.text)
        for table in doc.tables:
            for row in table.rows:
                add("\t".join(cell.text for cell in row.cells))
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
        try:
            count = 0
            for sheet in book:
                require((sheet.max_row or 0) <= 10000 and (sheet.max_column or 0) <= 100, "document_cell_limit")
                for row in sheet.iter_rows(values_only=True):
                    count += len(row)
                    require(count <= 50000, "document_cell_limit")
                    add("\t".join(str(c) if c is not None else "" for c in row))
        finally:
            book.close()
    elif suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        require(not reader.is_encrypted, "encrypted_document")
        require(len(reader.pages) <= 100, "document_page_limit")
        for page in reader.pages:
            add(page.extract_text())
    elif suffix in (".txt", ".csv"):
        add(raw.decode("utf-8"))
    else:
        raise Denied("unsupported_document")
    return "\n".join(parts)


def document_bytes(suffix, params):
    out = io.BytesIO()
    if suffix == ".docx":
        require("content" in params and "rows" not in params)
        from docx import Document
        doc = Document()
        for line in text(params["content"], MAX_TEXT).splitlines():
            doc.add_paragraph(line)
        doc.save(out)
    elif suffix == ".xlsx":
        require("rows" in params and "content" not in params)
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        rows = params["rows"]
        require(type(rows) is list and len(rows) <= 10000)
        book = Workbook(write_only=True)
        sheet = book.create_sheet("Data")
        count = 0
        for row in rows:
            require(type(row) is list and len(row) <= 100)
            count += len(row)
            require(count <= 50000)
            cells = []
            for value in row:
                require(value is None or type(value) in (str, int, float, bool))
                cell = WriteOnlyCell(sheet, value=value)
                if isinstance(value, str):
                    text(value, 4096)
                    cell.data_type = "s"  # 禁止远端内容成为 Excel 公式。
                cells.append(cell)
            sheet.append(cells)
        book.save(out)
    elif suffix == ".txt":
        require("content" in params and "rows" not in params)
        return text(params["content"], MAX_TEXT).encode()
    else:
        raise Denied("unsupported_document_write")
    return out.getvalue()
