"""协议 v2：绑定检查先于防重；SQLite WAL 在执行前提交执行意图。"""
import hashlib
import json
import math
import os
import sqlite3
import time
from .security import MAX_INPUT, Denied, encode, fields, identifier, integer, no_links, require, text

BINDINGS = ("protocol", "actor_id", "device_id", "session_id", "task_id", "command_id",
            "seq", "expiry", "connection_generation", "fencing_token")
READ_ONLY = {"file.list", "file.read", "document.extract", "system.info", "process.list",
             "log.collect", "browser.snapshot", "browser.screenshot", "desktop.inspect"}


def result(command, status, data=None, error=""):
    binding = {}
    for key in BINDINGS:
        value = command.get(key)
        if isinstance(value, str) and len(value) <= 160:
            binding[key] = value
        elif type(value) in (int, float) and math.isfinite(value) and abs(value) <= 2**63 - 1:
            binding[key] = value
    return {**binding, "type": "result", "status": status, "data": data or {}, "error": error}


def generation(value):
    if type(value) is int:
        return integer(value, 1)
    return identifier(value)


class Journal:
    def __init__(self, path):
        no_links(path, missing_leaf=True)
        self.db = sqlite3.connect(path)
        if os.name == "posix":
            os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA max_page_count=16384")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
                write_op INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, fence INTEGER NOT NULL,
                stopped INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        with self.db:
            # 无法证明未执行，包括读取，也不自动重跑。
            self.db.execute("UPDATE commands SET status='unknown' WHERE status='running'")
            self.db.execute("UPDATE tasks SET stopped=1")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def finish(self, command, status):
        with self.db:
            self.db.execute("UPDATE commands SET status=? WHERE id=?", (status, command["command_id"]))

    def stop_tasks(self):
        with self.db:
            self.db.execute("UPDATE tasks SET stopped=1")

    def close(self):
        self.db.close()


class Guard:
    def __init__(self, config, journal):
        self.config = config
        self.journal = journal
        self.welcome = None
        self.last_seq = -1
        self.active = None

    def bind(self, welcome):
        fields(welcome, ("type", "protocol", "actor_id", "device_id", "connection_generation"))
        require(welcome["type"] == "welcome" and type(welcome["protocol"]) is int
                and welcome["protocol"] == 2, "protocol_mismatch")
        identifier(welcome["actor_id"])
        require(welcome["device_id"] == self.config.device_id, "device_mismatch")
        generation(welcome["connection_generation"])
        if self.config.actor_id:
            require(welcome["actor_id"] == self.config.actor_id, "actor_mismatch")
        else:
            self.config.actor_id = welcome["actor_id"]
            self.config.save()
        old = self.journal.get("generation")
        current = welcome["connection_generation"]
        marker = "generation_seen:" + str(current)
        identity = [self.config.server, welcome["actor_id"], welcome["device_id"]]
        previous = self.journal.get("identity", identity)
        if previous != identity:
            # 重新配对会换发设备号: 旧设备号下的一次性代际标记与 fencing 计数不得沿用,
            # 否则重配后永久无法连接; 命令记录保留(启动时已统一标记 unknown, 不重放)。
            require(isinstance(previous, list) and len(previous) == 3
                    and previous[0] == identity[0] and previous[2] != identity[2],
                    "journal_identity_mismatch")
            with self.journal.db:
                self.journal.db.execute(
                    "DELETE FROM meta WHERE key='generation' OR key='fence'"
                    " OR key LIKE 'generation_seen:%'")
                # 旧设备号的停止标记不再适用于新设备; 命令记录保留且不重放。
                self.journal.db.execute("DELETE FROM tasks")
            old = None
        require(not self.journal.get(marker, False), "generation_reused")
        if type(old) is int and type(current) is int:
            require(current > old, "generation_rollback")
        with self.journal.db:
            self.journal.set("identity", identity)
            self.journal.set(marker, True)
            self.journal.set("generation", current)
        self.welcome = welcome
        self.last_seq = -1
        self.active = None

    def validate(self, command):
        encode(command, MAX_INPUT)
        fields(command, ("type", *BINDINGS, "method", "params"))
        require(self.welcome is not None, "welcome_required")
        require(command["type"] in ("command", "cancel", "pause"), "unsupported_message")
        require(type(command["protocol"]) is int and command["protocol"] == 2, "protocol_mismatch")
        for key in ("actor_id", "device_id", "connection_generation"):
            require(type(command[key]) is type(self.welcome[key]) and command[key] == self.welcome[key], "binding_mismatch")
        for key in ("session_id", "task_id", "command_id"):
            identifier(command[key])
        integer(command["seq"])
        integer(command["fencing_token"], 1)
        expiry = command["expiry"]
        require(type(expiry) in (int, float) and math.isfinite(expiry)
                and time.time() < expiry <= time.time() + 300, "command_expired")
        text(command["method"], 80)
        require(type(command["params"]) is dict)
        if command["type"] != "command":
            fields(command["params"])
        require(command["fencing_token"] >= self.journal.get("fence", 0), "stale_fence")

    def admit(self, command):
        self.validate(command)
        semantic = {k: command[k] for k in ("type", "actor_id", "device_id", "session_id",
                    "task_id", "command_id", "method", "params", "fencing_token")}
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db = self.journal.db
        with db:
            row = db.execute("SELECT digest,status FROM commands WHERE id=?", (command["command_id"],)).fetchone()
            if row:
                require(row[0] == digest, "command_id_collision")
                # 不持久化文件/截图内容；重复时明确 unknown，不假装已重新取得数据。
                return result(command, "unknown" if row[1] in ("ok", "running", "unknown") else row[1],
                              {"replayed": True, "previous_status": row[1]}, "not_reexecuted"), False
            require(command["seq"] > self.last_seq, "sequence_replayed")
            require(db.execute("SELECT count(*) FROM commands").fetchone()[0] < 100000, "journal_full")
            task = db.execute("SELECT session,fence,stopped FROM tasks WHERE id=?", (command["task_id"],)).fetchone()
            fence = self.journal.get("fence", 0)
            switch = self.active is not None and self.active != command["task_id"]
            if task:
                require(task[0] == command["session_id"] and task[1] == command["fencing_token"], "task_binding_mismatch")
                # 暂停后必须仍能停止: 只有新的执行命令被阻断, 控制消息始终可达。
                require(command["type"] != "command" or not task[2], "task_stopped")
            else:
                require(command["type"] == "command", "unknown_task")
                require(command["fencing_token"] > fence, "new_task_requires_new_fence")
                db.execute("UPDATE tasks SET stopped=1")
                db.execute("INSERT INTO tasks VALUES (?,?,?,0)",
                           (command["task_id"], command["session_id"], command["fencing_token"]))
            if command["type"] != "command":
                require(self.active == command["task_id"], "task_not_active")
                db.execute("UPDATE tasks SET stopped=1 WHERE id=?", (command["task_id"],))
            self.journal.set("fence", command["fencing_token"])
            db.execute("INSERT INTO commands VALUES (?,?,?,?)", (command["command_id"], digest, "running",
                       int(command["method"] not in READ_ONLY)))
        self.last_seq = command["seq"]
        self.active = command["task_id"]
        return None, switch
