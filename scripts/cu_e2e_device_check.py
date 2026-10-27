#!/usr/bin/env python3
"""真实设备端到端验收（人工触发, 不是 CI 用例）。

真实 Companion（普通系统用户 + 系统 keyring）+ 真实 nginx TLS + 真实 Redis/DeviceService。
覆盖：一次性配对 → WSS 上线 → 设备级互斥 → 真实无头 Chromium 打开/快照/截图帧/下载
     → 文件/文档/查找/日志脱敏/跨应用工作流 → 已授权与未授权目录的写、移、替、删
     → 登记脚本与未登记/未声明参数拒绝 → 取消正在执行的子进程 → 吊销 → 清理。

用法（需 root + 已安装 cpcnet 用户与 /opt/zops-pw-browsers）:
    ZOPS_CU_E2E=1 python3 scripts/cu_e2e_device_check.py
可选环境变量: ZOPS_CU_E2E_SERVER（默认线上地址）、ZOPS_CU_E2E_USER（默认 admin）、
          ZOPS_CU_E2E_BASE（默认 /tmp/cu-e2e）。运行会创建一次性配对码与临时 Redis
          令牌，并在结束时删除令牌/设备/任务键与临时目录（任务审计事件按规格保留）。
"""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 仅验收脚本自身：本机 python 构建的默认信任库为空，显式指向系统 CA 包。
# Companion 不设该变量，用来验证它对空信任库的回落到系统 CA 包。
os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")

ROOT = "/opt/zops"
PY = ROOT + "/companion/.venv/bin/python"
EXE = ROOT + "/companion/.python/cpython-3.11.14-linux-x86_64-gnu/bin/python3.11"
BASE = os.environ.get("ZOPS_CU_E2E_BASE", "/tmp/cu-e2e")
CFG = BASE + "/companion/config.json"
SITE = BASE + "/site"
WORK = BASE + "/work"
MANAGED = BASE + "/managed"
SCRIPTS = BASE + "/scripts"
SERVER = os.environ.get("ZOPS_CU_E2E_SERVER", "https://gcoc-ai.example.com")
NAME = "e2e-current-" + secrets.token_hex(3)
KR_PASS = "zopspass"

RESULTS = []
STATE = {"child": None, "site": None, "token": "", "redis": None, "device": "",
         "session": "", "actor": os.environ.get("ZOPS_CU_E2E_USER", "admin")}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (" | " + str(detail) if detail else ""), flush=True)
    return bool(ok)


def cpc(cmd, timeout=240):
    inner = (f'echo -n "{KR_PASS}" | gnome-keyring-daemon --login --components=secrets >/dev/null 2>&1; '
             + cmd)
    return subprocess.run(
        ["runuser", "-u", "cpcnet", "--", "env", "-i", "HOME=/home/cpcnet", "USER=cpcnet",
         "PATH=/usr/local/bin:/usr/bin:/bin", "XDG_RUNTIME_DIR=/run/user/1000",
         "PLAYWRIGHT_BROWSERS_PATH=/opt/zops-pw-browsers",
         "dbus-run-session", "--", "bash", "-c", inner],
        capture_output=True, text=True, timeout=timeout)


def api(path, method="GET", data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(SERVER + path, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if STATE["token"]:
        req.add_header("Authorization", "Bearer " + STATE["token"])
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400].decode(errors="replace")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def setup_dirs():
    shutil.rmtree(BASE, ignore_errors=True)
    for path in (BASE, SITE, WORK, MANAGED, SCRIPTS):
        os.makedirs(path, exist_ok=True)
    # dbus-run-session/keyring 需要属主私有的运行时目录（本机无登录会话时不自带）。
    os.makedirs("/run/user/1000", exist_ok=True)
    os.chmod("/run/user/1000", 0o700)
    subprocess.run(["chown", "cpcnet:cpcnet", "/run/user/1000"], check=True)
    with open(SITE + "/index.html", "w") as stream:
        stream.write("""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Z-OPS CU E2E</title></head><body>
<h1>Z-OPS 本机执行验收页</h1>
<button id="ok">确认下载报表</button>
<a id="csv" href="report.csv">下载 CSV 报表</a>
<p>sensitive-marker-8f2a</p></body></html>""")
    with open(SITE + "/report.csv", "w") as stream:
        stream.write("月份,金额\n2026-09,128\n2026-10,256\n")
    with open(SCRIPTS + "/probe.py", "w") as stream:
        stream.write("import json, sys\nprint(json.dumps({'args': sys.argv[1:]}, ensure_ascii=False))\n")
    with open(SCRIPTS + "/sleepy.py", "w") as stream:
        stream.write("import os, sys, time\n"
                     "from pathlib import Path\n"
                     "pidfile = sys.argv[1].split('=', 1)[1]\n"
                     "Path(pidfile).write_text(str(os.getpid()))\n"
                     "time.sleep(120)\n")
    for name in ("probe.py", "sleepy.py"):
        os.chmod(SCRIPTS + "/" + name, 0o600)
    subprocess.run(["chown", "-R", "cpcnet:cpcnet", BASE], check=True)


def scripts_config():
    exe_sha = digest(EXE)
    schema = {"type": "object", "properties": {"args": {"type": "string", "maxLength": 64}},
              "required": ["args"], "additionalProperties": False}
    entry = {"executable": EXE, "executable_sha256": exe_sha, "schema": schema,
             "timeout": 30, "platform": ["linux"], "permissions": ["read"],
             "target_dirs": [WORK], "version": 1, "cancel": {"grace_s": 3}}
    return {
        "probe": {**entry, "path": SCRIPTS + "/probe.py", "sha256": digest(SCRIPTS + "/probe.py")},
        "sleepy": {**entry, "path": SCRIPTS + "/sleepy.py", "sha256": digest(SCRIPTS + "/sleepy.py"),
                   "timeout": 60, "permissions": ["read", "write"]},
    }


def start_site():
    STATE["site"] = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8099", "--bind", "127.0.0.1", "--directory", SITE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            urllib.request.urlopen("http://127.0.0.1:8099/index.html", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def admin_session(r):
    for key in r.scan_iter(match="zops:session:*", count=500):
        meta = r.hgetall(key)
        if meta.get("username") == STATE["actor"]:
            return meta.get("session_id")
    return None


def main():
    import redis as redis_sync
    sys.path.insert(0, ROOT)
    import config
    r = redis_sync.Redis.from_url(config.REDIS_URL, decode_responses=True)
    STATE["redis"] = r
    session_id = admin_session(r)
    STATE["session"] = session_id or ""
    if not check("找到 admin 归属的真实会话", bool(session_id), session_id):
        return 1
    STATE["token"] = "e2e_" + secrets.token_urlsafe(24)
    r.set(config.key_token(STATE["token"]), STATE["actor"], ex=3600)
    check("临时访问令牌写入 Redis（不落盘、1 小时）", True, STATE["token"][:8] + "…")

    setup_dirs()
    check("本地验收站点与登记脚本就绪", start_site(), "http://127.0.0.1:8099")

    status, body = api("/api/cu/devices/pair-code", "POST")
    code = (body or {}).get("code") or ""
    if not check("一次性配对码（真实 HTTPS + Bearer）", status == 200 and bool(code), f"{status} {body}"):
        return 1

    done = cpc(f'{PY} -m companion --config {CFG} --server {SERVER} --name {NAME} --init')
    if not check("Companion 初始化本机私有配置", done.returncode == 0, done.stderr[-200:]):
        return 1
    with open(CFG) as stream:
        cfg = json.load(stream)
    cfg["roots"] = [{"path": WORK, "read": True, "write_new": True, "manage": False},
                    {"path": MANAGED, "read": True, "write_new": True, "manage": True}]
    cfg["origins"] = ["http://127.0.0.1:8099"]
    cfg["scripts"] = scripts_config()
    with open(CFG, "w") as stream:
        json.dump(cfg, stream, ensure_ascii=False)
    os.chmod(CFG, 0o600)
    subprocess.run(["chown", "cpcnet:cpcnet", CFG], check=True)
    check("本机授权目录/站点 origin/登记脚本写入", True, "work + managed + 8099 + probe/sleepy")

    done = cpc(f'{PY} -m companion --config {CFG} --pair {code} --init')
    if not check("一次性配对（Ed25519 公钥入库、私钥进 keyring）", done.returncode == 0,
                 (done.stdout or "").strip()[-160:] or done.stderr[-200:]):
        return 1

    log = open(BASE + "/companion.log", "wb")
    STATE["child"] = subprocess.Popen(
        ["runuser", "-u", "cpcnet", "--", "env", "-i", "HOME=/home/cpcnet", "USER=cpcnet",
         "PATH=/usr/local/bin:/usr/bin:/bin", "XDG_RUNTIME_DIR=/run/user/1000",
         "PLAYWRIGHT_BROWSERS_PATH=/opt/zops-pw-browsers",
         "dbus-run-session", "--", "bash", "-c",
         f'echo -n "{KR_PASS}" | gnome-keyring-daemon --login --components=secrets >/dev/null 2>&1; '
         f'exec {PY} -m companion --config {CFG}'],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    device = None
    for _ in range(60):
        st, body = api("/api/cu/devices")
        for item in (body or {}).get("devices") or []:
            if item.get("name") == NAME:
                device = item
        if device and device.get("online"):
            break
        time.sleep(1)
    if not check("真实 WSS 上线（challenge→ticket→hello）", bool(device and device.get("online")),
                 json.dumps(device, ensure_ascii=False)[:300] if device
                 else open(BASE + "/companion.log").read()[-300:]):
        return 1
    STATE["device"] = device["device_id"]
    caps = device.get("capabilities") or []
    check("能力表声明浏览器/文件/脚本能力",
          all(m in caps for m in ("browser.goto", "browser.download", "file.read",
                                  "document.write_new", "script.run")), len(caps))
    return asyncio.run(drive())


async def drive():
    from core.cu import devices as cudev
    from core.cu import protocol as p
    svc = await cudev.get_service()
    actor, session_id, device_id = STATE["actor"], STATE["session"], STATE["device"]
    tasks = []
    outcome = 0

    async def call(method, params, timeout=60):
        return await svc.send_command(actor, device_id, session_id, tasks[-1], method, params, timeout)

    async def stop():
        await svc.cancel_task(actor, tasks[-1])
        view = None
        for _ in range(60):
            view = await svc.get_task(actor, tasks[-1], with_frame=False)
            if view["state"] in p.TERMINAL:
                return view
            await asyncio.sleep(0.2)
        return view

    async def start():
        tid = "e2e" + secrets.token_hex(6)
        tasks.append(tid)
        return await svc.start_task(actor, session_id, tid, device_id)

    try:
        task = await start()
        check("任务开始（真实设备级互斥）", task["state"] == "running", task["state"])

        dup = None
        try:
            await svc.start_task(actor, session_id, tasks[-1] + "b", device_id)
        except ValueError as exc:
            dup = str(exc)
        check("同设备第二个任务被拒（资源锁）", bool(dup and "占用" in dup), dup)

        res = await call("browser.goto", {"url": "http://127.0.0.1:8099/index.html"})
        check("browser.goto 真实无头 Chromium 打开验收页", res.get("status") == "ok",
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:160])

        res = await call("browser.snapshot", {})
        snap = res.get("data") or {}
        text = json.dumps(snap, ensure_ascii=False)
        check("browser.snapshot 返回页面引用",
              res.get("status") == "ok" and "确认下载报表" in text, text[:160])
        link = next((i for i in snap.get("items") or [] if "下载 CSV" in str(i.get("text") or "")), {})

        res = await call("browser.screenshot", {})
        shot = res.get("data") or {}
        check("browser.screenshot 帧尺寸与执行方式",
              res.get("status") == "ok" and shot.get("w", 0) > 0
              and shot.get("execution_mode") == "background",
              f"{shot.get('w')}x{shot.get('h')} mode={shot.get('execution_mode')}")

        view = await svc.get_task(actor, tasks[-1], with_frame=True)
        frame = view.get("frame") or {}
        check("任务专属帧缓存（含 JPEG）", str(frame.get("jpeg", "")).startswith("/9j/"),
              f"frame={frame.get('width')}x{frame.get('height')} bytes={len(frame.get('jpeg') or '')}")

        # Spec 44：网页下载 → 本机读取/处理 → 生成汇总文档 → 回读预览。
        check("快照含下载链接引用", bool(link.get("ref")), str(link)[:120])
        res = await call("browser.download", {"ref": link.get("ref", ""), "name": "report.csv"}, 60)
        saved = res.get("data") or {}
        check("browser.download 落到授权根新文件",
              res.get("status") == "ok" and saved.get("saved") == "report.csv",
              json.dumps(saved, ensure_ascii=False)[:160])

        res = await call("file.read", {"path": saved.get("path") or WORK + "/report.csv"})
        csv_text = (res.get("data") or {}).get("text", "")
        check("file.read 读回真实下载内容", res.get("status") == "ok" and "2026-09" in csv_text,
              csv_text[:80].replace("\n", " "))

        res = await call("document.write_new", {"path": WORK + "/summary.xlsx",
            "rows": [["月份", "金额"], ["2026-09", 128], ["2026-10", 256], ["合计", 384]]})
        check("document.write_new 生成汇总 xlsx", res.get("status") == "ok",
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:160])

        res = await call("document.extract", {"path": WORK + "/summary.xlsx"})
        extracted = (res.get("data") or {}).get("text", "")
        check("document.extract 回读汇总内容", res.get("status") == "ok" and "384" in extracted,
              extracted[:80].replace("\n", " "))

        # 文本型 docx: 生成 → 回读
        res = await call("document.write_new", {"path": WORK + "/notes.docx",
            "content": "Z-OPS 本机验收\n第二行汇总"})
        check("document.write_new 生成 docx", res.get("status") == "ok",
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:120])
        res = await call("document.extract", {"path": WORK + "/notes.docx"})
        docx_text = (res.get("data") or {}).get("text", "")
        check("document.extract 读回 docx 文本", res.get("status") == "ok" and "第二行汇总" in docx_text,
              docx_text[:60].replace("\n", " "))

        # 授权根内结构化查找
        res = await call("file.list", {"path": WORK})
        names = [e.get("name") for e in (res.get("data") or {}).get("entries") or []]
        check("file.list 列出授权根内容", res.get("status") == "ok" and "summary.xlsx" in names,
              ",".join(str(n) for n in names)[:120])
        res = await call("file.find", {"path": WORK, "pattern": "*.xlsx"})
        found = [e.get("path") for e in (res.get("data") or {}).get("entries") or []]
        check("file.find 按通配符查找", res.get("status") == "ok"
              and any(str(p).endswith("summary.xlsx") for p in found), str(found)[:120])

        # 合成秘密: 日志采集必须先脱敏再返回
        secret_log = WORK + "/service.log"
        await call("file.write_new", {"path": secret_log, "content": (
            "2026-10-25T09:00:00Z INFO login password=Hunter2-EXAMPLE\n"
            "2026-10-25T09:00:01Z WARN Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e2e.sig\n"
            "2026-10-25T09:00:02Z INFO owner ops@example.com otp 483920\n")})
        res = await call("log.collect", {"path": secret_log, "max_bytes": 4096})
        log = res.get("data") or {}
        body = str(log.get("text"))
        check("log.collect 采集日志并脱敏合成秘密",
              res.get("status") == "ok" and "Hunter2-EXAMPLE" not in body
              and "ops@example.com" not in body and sum((log.get("redacted") or {}).values()) >= 2,
              json.dumps(log.get("redacted") or {}, ensure_ascii=False)
              + " | " + body[:60].replace("\n", " "))

        # manage 授权根: 新建 → 移动 → 原子替换 → 删除（未授权根上面仍拒绝）
        res = await call("file.write_new", {"path": MANAGED + "/draft.csv", "content": "a,b\n1,2\n"})
        check("manage 根内新建文件", res.get("status") == "ok",
              json.dumps(res.get("data") or {})[:100])
        res = await call("file.move", {"source": MANAGED + "/draft.csv",
                                       "target": MANAGED + "/final.csv"})
        check("授权根内移动文件", res.get("status") == "ok"
              and (res.get("data") or {}).get("moved") is True,
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:120])
        res = await call("file.replace", {"path": MANAGED + "/final.csv", "content": "a,b\n3,4\n"})
        check("已授权时原子替换内容", res.get("status") == "ok"
              and (res.get("data") or {}).get("replaced") is True,
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:120])
        res = await call("file.delete", {"path": MANAGED + "/final.csv"})
        check("已授权时删除文件", res.get("status") == "ok"
              and (res.get("data") or {}).get("deleted") is True,
              json.dumps(res.get("data") or {}, ensure_ascii=False)[:120])

        # Spec 27/29：默认新建、不覆盖；manage 未授权时破坏性操作不可声明。
        res = await call("file.write_new", {"path": saved.get("path") or "", "content": "overwrite"})
        check("重复写入同一路径被拒（不覆盖）", res.get("status") == "rejected",
              str(res.get("error"))[:80])
        res = await call("file.delete", {"path": saved.get("path") or ""})
        check("未授权 manage 时删除被拒", res.get("status") == "rejected",
              str(res.get("error"))[:80])

        final = await stop()
        check("停止由执行端确认（非仅请求）",
              final["state"] == "cancelled" and final.get("reason") == "device_confirmed",
              f"{final['state']}/{final.get('reason')}")
        check("停止后帧缓存已清理", "frame" not in (await svc.get_task(actor, tasks[-1], with_frame=True)))
        check("执行方式进入任务视图", final.get("execution_mode") == "background",
              final.get("execution_mode"))

        # Spec 37/39/41：登记脚本按哈希执行；未登记名字与未声明参数被拒。
        task = await start()
        res = await call("script.run", {"name": "probe", "args": {"args": "e2e"}}, 60)
        out = res.get("data") or {}
        check("script.run 执行登记脚本并回传退出码",
              res.get("status") == "ok" and out.get("exit_code") == 0
              and "--args=e2e" in str(out.get("stdout")), json.dumps(out, ensure_ascii=False)[:160])
        res = await call("script.run", {"name": "not_registered", "args": {"args": "x"}})
        check("未登记脚本被拒", res.get("status") == "rejected", str(res.get("error"))[:80])
        res = await call("script.run", {"name": "probe", "args": {"args": "x", "shell": "rm -rf /"}})
        check("未声明参数（shell）被拒", res.get("status") == "rejected", str(res.get("error"))[:80])

        # Spec 40/60：取消贯通到任务子进程，不留孤儿。
        pidfile = WORK + "/sleepy.pid"
        running = asyncio.create_task(call("script.run", {"name": "sleepy", "args": {"args": pidfile}}, 60))
        await asyncio.sleep(4)
        started = os.path.exists(pidfile)
        pid = int(open(pidfile).read()) if started else 0
        await svc.cancel_task(actor, tasks[-1])
        cancelled_at = time.monotonic()
        res = await running
        # 取消必须立刻贯通, 不能等到命令超时(60s)才返回。
        elapsed = time.monotonic() - cancelled_at
        final = None
        for _ in range(60):
            final = await svc.get_task(actor, tasks[-1], with_frame=False)
            if final["state"] in p.TERMINAL:
                break
            await asyncio.sleep(0.2)
        import psutil
        alive = bool(pid) and psutil.pid_exists(pid)
        # 结果未知(unknown)是正确回答: 执行端已终止, 不编造成功/失败。
        check("取消正在执行的脚本，子进程被终止",
              started and res.get("status") in ("unknown", "cancelled", "rejected")
              and final["state"] == "cancelled" and not alive and elapsed < 25,
              f"status={res.get('status')} state={final['state']} pid={pid} "
              f"alive={alive} elapsed={elapsed:.1f}s")

        # 取消后同设备可继续新任务。
        task = await start()
        res = await call("system.info", {})
        info = res.get("data") or {}
        check("取消后新任务可继续执行", task["state"] == "running" and res.get("status") == "ok"
              and bool(info.get("platform")), json.dumps(info, ensure_ascii=False)[:100])
        await stop()

        status, body = api("/api/cu/devices/" + device_id, "DELETE")
        check("吊销设备（真实接口）", status == 200, f"{status} {body}")
        time.sleep(2)
        st, body = api("/api/cu/devices")
        item = next((d for d in (body or {}).get("devices") or [] if d.get("device_id") == device_id), {})
        check("吊销后设备标记 revoked 且离线", item.get("revoked") is True and not item.get("online"),
              json.dumps(item, ensure_ascii=False)[:160])
        left = await svc.get_task(actor, tasks[0], with_frame=False)
        check("吊销不影响已终结任务审计", left["state"] == "cancelled", left["state"])
    except Exception as exc:
        check("端到端流程异常", False, f"{type(exc).__name__}: {exc}")
        outcome = 1
    finally:
        try:
            keys = [svc.device_key(device_id)]
            for tid in tasks:
                keys.append(svc.task_key(actor, device_id, session_id, tid))
                await svc.r.hdel(svc.index_key(actor), tid)
            await svc.r.delete(*keys)
            await svc.r.srem(svc.key("devices", actor), device_id)
        except Exception:
            pass
    return outcome


def cleanup():
    sys.path.insert(0, ROOT)
    import config
    r = STATE["redis"]
    if r is not None and STATE["token"]:
        r.delete(config.key_token(STATE["token"]))
    if STATE["child"]:
        for sig in (15, 9):
            try:
                os.killpg(os.getpgid(STATE["child"].pid), sig)
            except Exception:
                pass
            time.sleep(1)
    if STATE["site"]:
        STATE["site"].terminate()
    subprocess.run(["pkill", "-f", "gnome-keyring-daemon"], check=False)
    subprocess.run(["chown", "-R", "root:root", BASE], check=False)
    shutil.rmtree(BASE, ignore_errors=True)
    shutil.rmtree("/home/cpcnet/.local", ignore_errors=True)
    shutil.rmtree("/run/user/1000", ignore_errors=True)


if __name__ == "__main__":
    if os.environ.get("ZOPS_CU_E2E") != "1":
        raise SystemExit("需显式设置 ZOPS_CU_E2E=1 后运行：会创建一次性配对码与临时 Redis 令牌")
    try:
        code = main()
    finally:
        cleanup()
    print("\n=== 汇总 ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过" + ("" if not failed else "；失败：" + "、".join(failed)))
    raise SystemExit(1 if failed else 0)
