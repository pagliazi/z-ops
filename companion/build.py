"""构建 Companion：未签名测试包（须显式确认）或已签名包（证书库指纹 + 强制验签）。

生产包只能来自签名并验签成功的产物；缺少签名条件时只产出名字里带 unsigned-test 的测试包。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIMESTAMP_URL = "http://timestamp.digicert.com"
SIGNING_PLATFORMS = ("nt",)
STAGING = "companion-unsigned-test"
# 这两个可执行文件都要签名；任何一个验签失败都不产出生产包。
EXECUTABLES = (("companion-unsigned-test", "companion"),
               ("companion-native-unsigned-test", "companion-native"))


def pinned_chromium():
    """从当前锁定的 Playwright 读取 Chromium 版本号, 只用于打印安装命令。"""
    try:
        import playwright
        data = json.loads((Path(playwright.__file__).parent / "driver" / "package"
                           / "browsers.json").read_text())
        for item in data.get("browsers", []):
            if item.get("name") == "chromium-headless-shell":
                return str(item.get("revision", ""))
    except Exception:
        return ""
    return ""


def build_plan(ack_unsigned, thumbprint, platform):
    """决定这次构建产出什么；不满足签名条件时只能产出明确标注的测试包。"""
    thumbprint = (thumbprint or "").strip().replace(" ", "").upper()
    if thumbprint and ack_unsigned:
        raise ValueError("signed_build_conflicts_with_unsigned_ack")
    if not thumbprint:
        if not ack_unsigned:
            raise ValueError("signing_material_required")
        return {"signed": False, "suffix": "unsigned-test", "thumbprint": ""}
    if platform not in SIGNING_PLATFORMS:
        # 非 Windows 没有 signtool 与证书库, 无法验签, 不能声称已签名。
        raise ValueError("signing_requires_windows_certificate_store")
    if len(thumbprint) not in (40, 64) or any(c not in "0123456789ABCDEF" for c in thumbprint):
        raise ValueError("invalid_certificate_thumbprint")
    return {"signed": True, "suffix": "", "thumbprint": thumbprint}


def sign_command(exe, thumbprint, timestamp_url=TIMESTAMP_URL):
    """证书库按指纹签名；SHA-256 摘要 + RFC3161 时间戳，避免仅本机有效。"""
    return ["signtool", "sign", "/sha1", thumbprint, "/fd", "sha256",
            "/tr", timestamp_url, "/td", "sha256", "/v", str(exe)]


def verify_command(exe):
    return ["signtool", "verify", "/pa", "/all", "/v", str(exe)]


def digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def receipt(plan, version, files, verified, created=None):
    """把"签了没有、验过没有、文件叫什么"写进产物目录，避免把测试包误当生产包。"""
    return {"schema": 1, "version": version, "signed": plan["signed"],
            "verified": bool(plan["signed"] and verified),
            "thumbprint": plan["thumbprint"], "test_only": not plan["signed"],
            "created": created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": {name: {"file": Path(path).name, "sha256": digest(path)}
                      for name, path in files.items()}}


def verify_release(path, require_signed=True):
    """核验一个产物目录：回执可解析、文件未被改动；生产包必须已签名且验签通过。

    改名后的文件名与逻辑名不同, 所以只能按回执里记下的真实文件名逐个核对。
    """
    root = Path(path)
    record_path = root / "build-receipt.json"
    if not record_path.is_file():
        return False, ["build_receipt_missing"]
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        return False, ["build_receipt_unreadable"]
    if record.get("schema") != 1:
        return False, ["unsupported_receipt_schema"]
    problems = []
    files = record.get("files") or {}
    if not files:
        problems.append("no_artifacts_recorded")
    for name, entry in files.items():
        filename = (entry or {}).get("file") or ""
        if not filename:
            problems.append(f"{name}:file_not_recorded")
            continue
        artifact = root / filename
        if not artifact.is_file():
            problems.append(f"{name}:file_missing")
        elif digest(artifact) != entry.get("sha256"):
            problems.append(f"{name}:digest_mismatch")
    if require_signed:
        if not record.get("signed"):
            problems.append("not_signed")
        if not record.get("verified"):
            problems.append("not_verified")
        if record.get("test_only"):
            problems.append("unsigned_test_package")
    return not problems, problems


def _run(argv):
    print("$ " + " ".join(argv), flush=True)
    return subprocess.run(argv, check=False).returncode


def _sign_and_verify(files, plan):
    for name, path in files.items():
        if _run(sign_command(path, plan["thumbprint"])) != 0:
            print(f"签名失败：{name}；不产出生产包。", file=sys.stderr)
            return False
    for name, path in files.items():
        if _run(verify_command(path)) != 0:
            print(f"验签失败：{name}；该产物不可发布。", file=sys.stderr)
            return False
    return True


def staged_files(dist, platform):
    """先构建到显式未签名的目录；只有验签成功才会改成生产名字。"""
    suffix = ".exe" if platform == "nt" else ""
    target = Path(dist) / STAGING
    return {name: target / (staged + suffix) for staged, name in EXECUTABLES}, target


def publish(files, target, version):
    """把验签通过的产物改成生产名字并返回新目录；改名失败时保留未签名目录名。"""
    for name, path in files.items():
        path.rename(path.with_name(name + path.suffix))
    final = Path(target).with_name("companion-" + version)
    if final.exists():
        raise FileExistsError(f"{final} 已存在，先移走再重建")
    Path(target).rename(final)
    return final


def finish(root, plan, version, platform):
    """构建完成后的收尾：验签→改名→回执。返回产物目录；失败返回 None。"""
    files, staged = staged_files(Path(root) / "dist", platform)
    for name, path in files.items():
        if not path.is_file():
            print(f"产物缺失：{path}", file=sys.stderr)
            return None
    if plan["signed"]:
        if not _sign_and_verify(files, plan):
            print("签名/验签未通过：保留 " + str(staged) + "（名字仍标注未签名）。", file=sys.stderr)
            return None
        try:
            target = publish(files, staged, version)
        except OSError as exc:
            print(f"改名失败：{exc}", file=sys.stderr)
            return None
        files = {name: target / (name + path.suffix) for name, path in files.items()}
    else:
        target = staged
    record = receipt(plan, version, files, plan["signed"])
    (target / "build-receipt.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建 Companion（测试包或已签名包）")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--ack-unsigned-test", action="store_true",
                        help="确认只产出明确标注的未签名测试包")
    choice.add_argument("--sign-thumbprint", metavar="SHA1",
                        help="Windows 证书库中的代码签名证书指纹；签名后强制验签")
    choice.add_argument("--verify-release", metavar="DIR",
                        help="只核验已有产物目录（回执、摘要、签名状态），不构建")
    parser.add_argument("--timestamp-url", default=TIMESTAMP_URL,
                        help=f"RFC3161 时间戳服务（默认 {TIMESTAMP_URL}）")
    parser.add_argument("--allow-test-package", action="store_true",
                        help="核验时允许明确标注的未签名测试包（不用于发布）")
    args = parser.parse_args(argv)
    if args.verify_release:
        ok, problems = verify_release(args.verify_release, not args.allow_test_package)
        print(("产物可发布：" if ok else "产物不可发布：") + args.verify_release)
        for problem in problems:
            print("  - " + problem)
        return 0 if ok else 1
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root.parent))
    from companion import __version__
    try:
        plan = build_plan(args.ack_unsigned_test, args.sign_thumbprint, os.name)
    except ValueError as exc:
        print(f"构建被拒绝：{exc}", file=sys.stderr)
        return 2
    print("签名构建：证书指纹 " + plan["thumbprint"] + "；签名后逐个验签。"
          if plan["signed"] else
          "仅测试构建：未签名；无更新器、安装器或生产验收；不打包 Chromium。", flush=True)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(root / ".build-cache"),
               PYTHONDONTWRITEBYTECODE="1")
    code = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--workpath", str(root / ".build"), "--distpath", str(root / "dist"),
        str(root / "companion.spec")], cwd=root, env=env, check=False).returncode
    if code != 0:
        return code
    if finish(root, plan, __version__, os.name) is None:
        return 1
    revision = pinned_chromium()
    print("浏览器运行时不在包内：操作端须按锁定的 Playwright 版本安装 Chromium Headless Shell"
          + (f"（revision {revision}）" if revision else "")
          + "：python -m playwright install chromium-headless-shell；"
          "共享安装时用 PLAYWRIGHT_BROWSERS_PATH 指向该目录，随后用 --doctor 确认 browser.* 能力，"
          "且必须以普通桌面用户运行（root 下 Chromium 沙箱不可用）。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
