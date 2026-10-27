"""Agent 门面及设备子任务账本；无本机设备时绝不回退服务器执行。"""

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from pathlib import Path

from . import protocol as p
from .store import DeviceStore

logger = logging.getLogger(__name__)

# 截图证据仅在用户另行授权后落盘: 每任务至少间隔 10s、最多 20 张; 不自动删除历史。
EVIDENCE_INTERVAL_S = 10
EVIDENCE_MAX = 20


def _write_evidence(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class CapabilityDenied(ValueError):
    """方法不在设备能力或管理员上限内; 单独区分以便界面提示能力不足。"""


class DeviceService(DeviceStore):
    def __init__(self, redis, prefix="zops:cu2", clock=time.time, session_check=None,
                 ceiling=p.SAFE_METHODS, evidence_dir=None):
        super().__init__(redis, prefix, clock)
        self.session_check = session_check
        self.ceiling = frozenset(ceiling) & p.METHODS
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None

    async def check_session(self, actor, session_id):
        p.identifier(session_id, "会话")
        if self.session_check:
            await self.session_check(self.r, session_id, actor)
        else:
            from core.session_access import ensure_session_access
            await ensure_session_access(self.r, session_id, actor, action="本机设备任务")

    async def task_record(self, actor, task_id, check=True):
        p.identifier(task_id, "任务")
        key = await self.r.hget(self.index_key(actor), task_id)
        if isinstance(key, bytes):
            key = key.decode()
        task = await self.json(key) if key else None
        if not task or task["actor_id"] != actor or task["task_id"] != task_id:
            raise ValueError("任务不存在或无权访问")
        if check:
            await self.check_session(actor, task["session_id"])
        return key, task

    async def reconcile(self, key, task):
        dkey = self.device_key(task["device_id"])

        async def op(pipe):
            current, device = await self.json(key, pipe), await self.json(dkey, pipe)
            if not current or current["state"] in p.TERMINAL:
                return current
            valid = (self.online(device) and device["active_task"] == key
                     and device["connection_generation"] == current["connection_generation"]
                     and device["fencing_token"] == current["fencing_token"]
                     and current["lease_expires"] > self.clock())
            if valid:
                return current
            pipe.multi()
            self.close_task(pipe, key, current, "unknown", "lease_or_connection_lost")
            if device and device["active_task"] == key:
                device.update(ready=False, online_until=0, active_task="")
                pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return current

        return await self.atomic([key, dkey], op)

    async def start_task(self, actor, session_id, task_id, device_id=None):
        actor = p.text(actor, 128)
        p.identifier(task_id, "任务")
        await self.check_session(actor, session_id)
        if device_id is None:
            selected = await self.get_selected_device(actor)
            if not selected:
                raise ValueError("未选择本机设备")
            device_id = selected["device_id"]
        dkey, index = self.device_key(device_id), self.index_key(actor)
        tkey = self.task_key(actor, device_id, session_id, task_id)
        # 复用现有任务运行时的读取契约，不抢占父任务 lease、不伪造其终态。
        from core.task_runtime.ledger import k_task
        parent_key = k_task(task_id)

        async def op(pipe):
            device = self.owned(await self.json(dkey, pipe), actor)
            if not self.online(device):
                raise ValueError("本机设备离线或未完成握手")
            existing = await pipe.hget(index, task_id)
            if existing:
                if isinstance(existing, bytes):
                    existing = existing.decode()
                if existing != tkey:
                    raise ValueError("任务已绑定其他设备或会话")
                await pipe.watch(tkey)
                return await self.json(tkey, pipe)
            old = await self.old_task(pipe, device)
            if old and old[1]["state"] not in p.TERMINAL:
                raise ValueError("设备已被其他任务占用；请先查询原任务状态")
            parent = await pipe.hgetall(parent_key)
            parent = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in parent.items()}
            if parent and (parent.get("actor") != actor or parent.get("session_id") != session_id):
                raise ValueError("运行时任务归属不匹配")
            if parent and parent.get("status") in p.TERMINAL:
                raise ValueError("运行时任务已结束")
            device["fencing_token"] += 1
            device["active_task"] = tkey
            task = {"actor_id": actor, "task_id": task_id, "device_id": device_id,
                    "device_name": device["name"], "session_id": session_id, "state": "running",
                    "reason": "", "updated_at": self.clock(), "lease_expires": self.clock() + p.TASK_LEASE,
                    "connection_generation": device["connection_generation"], "fencing_token": device["fencing_token"],
                    "command_id": "", "control_id": "", "runtime_task_id": task_id if parent else None}
            pipe.multi()
            self.audit(pipe, tkey, task, "task_started")
            pipe.set(tkey, p.dumps(task))
            pipe.set(dkey, p.dumps(device))
            pipe.hset(index, task_id, tkey)
            await pipe.execute()
            return task

        task = await self.atomic([dkey, index, parent_key], op)
        return await self.reconcile(tkey, task)

    async def renew(self, actor, task_id):
        """长任务心跳：只在连接/代际/fencing 全部有效时续租；否则落到 unknown。"""
        key, task = await self.task_record(actor, task_id)
        if task["state"] in p.TERMINAL:
            return self.task_view(task)
        dkey = self.device_key(task["device_id"])

        async def op(pipe):
            current, device = await self.json(key, pipe), await self.json(dkey, pipe)
            if not current or current["state"] in p.TERMINAL:
                return current or task
            valid = (self.online(device) and device["active_task"] == key
                     and device["connection_generation"] == current["connection_generation"]
                     and device["fencing_token"] == current["fencing_token"])
            pipe.multi()
            if valid:
                current["lease_expires"] = self.clock() + p.TASK_LEASE
                current["updated_at"] = self.clock()
                pipe.set(key, p.dumps(current))
            else:
                self.close_task(pipe, key, current, "unknown", "lease_or_connection_lost")
                if device and device.get("active_task") == key:
                    device.update(ready=False, online_until=0, active_task="")
                    pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return current

        return self.task_view(await self.atomic([key, dkey], op))

    @staticmethod
    def task_view(task):
        view = {k: task[k] for k in ("task_id", "device_id", "device_name", "state", "reason", "session_id", "updated_at")}
        # 进度、审批与执行方式只上报聚合值; 不含动作参数、输入值或任何凭据。
        for key in ("step", "approval", "execution_mode", "pause_reason"):
            if key in task:
                view[key] = task[key]
        return view

    async def progress(self, actor, task_id, step=None, approval=None):
        """记录脱敏进度 (步号/是否等待本机确认); 不改状态、不延长租约。"""
        key, _task = await self.task_record(actor, task_id)

        async def op(pipe):
            current = await self.json(key, pipe)
            if not current or current["state"] in p.TERMINAL:
                return current
            before = bool(current.get("approval"))
            if step is not None:
                current["step"] = max(0, min(9999, int(step)))
            current["approval"] = bool(approval)
            current["updated_at"] = self.clock()
            pipe.multi()
            if current["approval"] != before:
                self.audit(pipe, key, current, "task_approval",
                           "approval_required" if current["approval"] else "approval_cleared")
            pipe.set(key, p.dumps(current))
            await pipe.execute()
            return current

        return self.task_view(await self.atomic([key], op))

    async def get_task(self, actor, task_id, with_frame=True):
        key, task = await self.task_record(actor, task_id)
        task = await self.reconcile(key, task)
        view = self.task_view(task)
        if with_frame:
            frame = await self.json(key + ":frame")
            if frame and all(frame.get(k) == task.get(k) for k in ("actor_id", "device_id", "session_id", "task_id", "connection_generation", "fencing_token")):
                view["frame"] = frame
        evidence = await self.evidence_list(actor, task_id)
        if evidence:
            view["evidence"] = evidence
        return view

    def evidence_key(self, actor, task_id):
        return self.key("evidence", p.text(actor, 128), p.identifier(task_id))

    async def evidence_list(self, actor, task_id):
        """只返回文件名（不含用户路径）; 访问图片仍需下载接口的归属校验。"""
        names = await self.r.lrange(self.evidence_key(actor, task_id), 0, EVIDENCE_MAX - 1)
        return [name.decode() if isinstance(name, bytes) else str(name) for name in names]

    async def persist_evidence(self, actor, task_id, frame):
        """用户另行授权后才落盘; 默认关闭, 失败不影响任务结果。"""
        from core import cu_bridge
        if not await cu_bridge.shot_evidence_enabled(self.r, actor):
            return None
        # 同一任务限频, 避免每一帧都写盘; 不删除已保存的历史证据。
        slot = self.key("evidence-slot", p.text(actor, 128), p.identifier(task_id))
        if not await self.r.set(slot, "1", nx=True, ex=EVIDENCE_INTERVAL_S):
            return None
        key = self.evidence_key(actor, task_id)
        if int(await self.r.llen(key) or 0) >= EVIDENCE_MAX:
            return None
        import config
        raw = base64.b64decode(frame["jpeg"], validate=True)
        stamp = int(self.clock() * 1000)
        digest = hashlib.sha256(str(task_id).encode()).hexdigest()[:12]
        name = f"computer_use/cu_{digest}_{stamp}_{secrets.token_hex(2)}.jpg"
        directory = self.evidence_dir or (Path(config.BASE_DIR) / "downloads")
        await asyncio.to_thread(_write_evidence, directory / name, raw)
        await self.r.hset(f"{config.REDIS_PREFIX}:cu:shot_owner", name, actor)
        await self.r.lpush(key, name)
        return name

    async def list_tasks(self, actor, session_id):
        await self.check_session(actor, session_id)
        tasks = []
        async for task_id, key in self.r.hscan_iter(self.index_key(actor)):
            if isinstance(key, bytes):
                key = key.decode()
            task = await self.json(key)
            if task and task["actor_id"] == actor and task["session_id"] == session_id:
                tasks.append(self.task_view(await self.reconcile(key, task)))
        return {"tasks": sorted(tasks, key=lambda t: t["updated_at"], reverse=True)}

    async def finish_task(self, actor, task_id, state, reason=""):
        if state not in p.TERMINAL | {"paused"}:
            raise ValueError("无效任务状态")
        key, task = await self.task_record(actor, task_id)
        dkey = self.device_key(task["device_id"])

        async def op(pipe):
            current, device = await self.json(key, pipe), await self.json(dkey, pipe)
            if current["state"] in p.TERMINAL:
                return current
            if state in {"cancelled", "paused"}:
                # 只能通过设备确认的 control result 宣称完成暂停/取消。
                raise ValueError("暂停或取消必须由本机设备确认")
            if current["command_id"] or current["control_id"]:
                raise ValueError("存在未确认命令，不能结束任务")
            if not self.online(device) or current["lease_expires"] <= self.clock() or device["active_task"] != key or device["fencing_token"] != current["fencing_token"] or device["connection_generation"] != current["connection_generation"]:
                final, why = "unknown", "lease_or_connection_lost"
            else:
                final, why = state, ("agent_finished" if not reason else "agent_finished_with_reason")
            # 自由文本 reason 可能携带参数或秘密，不进入永久审计。
            pipe.multi()
            self.close_task(pipe, key, current, final, why)
            if device["active_task"] == key:
                device["active_task"] = ""
                pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return current

        return await self.atomic([key, dkey], op)

    async def prepare(self, actor, device_id, session_id, task_id, method, params, timeout, kind="command"):
        timeout = p.timeout_value(timeout)
        key, task = await self.task_record(actor, task_id)
        if task["device_id"] != device_id or task["session_id"] != session_id:
            raise ValueError("命令绑定不匹配")
        dkey = self.device_key(device_id)
        command_id = "cmd_" + secrets.token_hex(16)
        ckey = self.key("command", command_id)

        async def op(pipe):
            device, current = await self.json(dkey, pipe), await self.json(key, pipe)
            self.owned(device, actor)
            if not self.online(device) or device["active_task"] != key or current["lease_expires"] <= self.clock() or device["connection_generation"] != current["connection_generation"] or device["fencing_token"] != current["fencing_token"]:
                raise ValueError("设备连接或任务租约已失效")
            if kind == "command":
                if current["state"] != "running" or current["command_id"] or current["control_id"]:
                    raise ValueError("任务暂停、控制中或已有在途命令")
                if not p.allowed(method, params, device["capabilities"], self.ceiling):
                    raise CapabilityDenied("方法不在设备能力或管理员安全上限内")
            elif kind not in {"cancel", "pause"} or current["state"] in p.TERMINAL or current["control_id"] or method != kind or params != {}:
                raise ValueError("任务已结束或控制请求无效")
            device["seq"] += 1
            envelope = {"type": kind, "protocol": 2, "actor_id": actor, "device_id": device_id,
                        "session_id": session_id, "task_id": task_id, "command_id": command_id,
                        "seq": device["seq"], "expiry": self.clock() + timeout,
                        "connection_generation": current["connection_generation"], "fencing_token": current["fencing_token"],
                        "method": method, "params": params}
            record = {"envelope": {k: envelope[k] for k in p.BINDINGS}, "digest": p.digest(p.dumps(envelope)),
                      "kind": kind, "method": method, "task_key": key, "phase": "pending"}
            current["command_id" if kind == "command" else "control_id"] = command_id
            current["lease_expires"] = max(self.clock() + p.TASK_LEASE, envelope["expiry"] + 5)
            current["updated_at"] = self.clock()
            if kind != "command":
                current["state"] = "cancel_requested" if kind == "cancel" else "pause_requested"
            pipe.multi()
            self.audit(pipe, key, current, "command_registered" if kind == "command" else "control_requested")
            pipe.set(ckey, p.dumps(record), ex=int(timeout) + p.RESULT_TTL + 10)
            pipe.set(key, p.dumps(current))
            pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return envelope

        return await self.atomic([dkey, key, ckey], op)

    async def claim_command(self, envelope):
        """WS 写入前原子 claim：签名之外再核验登记摘要、归属、代际和序号。"""
        if not isinstance(envelope, dict):
            return False
        try:
            command_id = p.identifier(envelope.get("command_id"))
            dkey = self.device_key(envelope.get("device_id"))
        except ValueError:
            return False
        ckey = self.key("command", command_id)

        async def op(pipe):
            record, device = await self.json(ckey, pipe), await self.json(dkey, pipe)
            if not record or record["phase"] != "pending" or record["digest"] != p.digest(p.dumps(envelope)):
                return False
            key = record["task_key"]
            await pipe.watch(key)
            task = await self.json(key, pipe)
            if not self.online(device) or not task or device["actor_id"] != envelope["actor_id"] or device["active_task"] != key or device["connection_generation"] != envelope["connection_generation"] or device["fencing_token"] != envelope["fencing_token"] or envelope["seq"] <= device["last_sent_seq"] or envelope["expiry"] <= self.clock() or task["lease_expires"] <= self.clock():
                return False
            if record["kind"] == "command" and (task["state"] != "running" or not p.allowed(envelope["method"], envelope["params"], device["capabilities"], self.ceiling)):
                return False
            if record["kind"] != "command" and task["control_id"] != command_id:
                return False
            record["phase"] = "sent"
            device["last_sent_seq"] = envelope["seq"]
            pipe.multi()
            pipe.set(ckey, p.dumps(record), keepttl=True)
            pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return True

        return await self.atomic([ckey, dkey], op)

    async def accept_result(self, device_id, generation, message):
        if not isinstance(message, dict) or message.get("type") != "result" or not isinstance(message.get("status"), str) or message["status"] not in p.STATUSES or not isinstance(message.get("data"), dict) or not isinstance(message.get("error"), str):
            return False
        try:
            if len(p.dumps(message).encode()) > p.MAX_MESSAGE or len(message["error"]) > 2048:
                return False
            command_id = p.identifier(message.get("command_id"))
            frame = p.frame_from(message["data"])
        except (ValueError, TypeError, RecursionError):
            return False
        ckey, dkey = self.key("command", command_id), self.device_key(device_id)

        async def op(pipe):
            record, device = await self.json(ckey, pipe), await self.json(dkey, pipe)
            if not record or record["phase"] != "sent" or not p.binding_matches(message, record["envelope"]):
                return False
            envelope, key = record["envelope"], record["task_key"]
            await pipe.watch(key)
            task = await self.json(key, pipe)
            if not self.online(device) or not task or task["state"] in p.TERMINAL or device["device_id"] != envelope["device_id"] or device["actor_id"] != envelope["actor_id"] or device["connection_generation"] != generation or generation != envelope["connection_generation"] or device["fencing_token"] != envelope["fencing_token"] or device["active_task"] != key or envelope["expiry"] <= self.clock() or task["lease_expires"] <= self.clock():
                return False
            kind, status = record["kind"], message["status"]
            if kind != "command" and status not in {"paused" if kind == "pause" else "cancelled", "rejected", "unknown"}:
                return False
            if frame and (kind != "command" or record["method"] not in {"browser.screenshot",
                                                                  "desktop.screen"}
                          or status != "ok"):
                return False
            # 截图只进入任务专属短缓存，不进入响应总线或永久审计。
            data = {k: v for k, v in message["data"].items() if k != "jpeg"}
            mode = p.execution_mode(data.get("execution_mode"))
            if mode:
                # 执行方式由执行器上报并固定下来; 控制结果不清空, 供审计与界面核对。
                task["execution_mode"] = mode
            response = p.result(status, data, message["error"])
            record.update(phase="done", result=response)
            task["command_id" if kind == "command" else "control_id"] = ""
            task["updated_at"] = self.clock()
            pending_control = ""
            if kind == "command" and task["control_id"]:
                # 控制请求进行中的命令结果只能作为审计, 不能提前把任务置为终态,
                # 否则 pause/cancel 的 control result 会被丢弃, 界面无法区分已请求与已确认。
                control = await self.json(self.key("command", task["control_id"]), pipe)
                pending_control = control["kind"] if control else ""
            if status == "unknown" and not pending_control:
                task["state"], task["reason"] = "unknown", "device_uncertain"
                device.update(ready=False, online_until=0)
            elif kind == "command" and pending_control and status in {"paused", "cancelled", "unknown"}:
                task["state"] = "pause_requested" if pending_control == "pause" else "cancel_requested"
                task["reason"] = "control_pending"
            elif kind != "command" or not task["control_id"]:
                if status in {"paused", "cancelled"}:
                    task["state"], task["reason"] = status, "device_confirmed"
                    detail = p.pause_reason(data.get("pause_reason"))
                    if status == "paused" and detail:
                        # 只保存执行端静态暂停码, 供小窗展示原因; 页面内容与参数不进入审计。
                        task["pause_reason"] = detail
                elif kind != "command":
                    # 控制被拒绝仍不恢复发送，须人工检查，不宣称完成。
                    task["state"] = "pause_requested" if kind == "pause" else "cancel_requested"
                    task["reason"] = "control_rejected_unconfirmed"
            pipe.multi()
            self.audit(pipe, key, task, "command_result", "device_" + status, envelope)
            if task["state"] in p.TERMINAL:
                device["active_task"] = ""
                pipe.delete(key + ":frame")
            elif frame:
                frame.update({k: envelope[k] for k in p.BINDINGS})
                frame["updated_at"] = self.clock()
                pipe.set(key + ":frame", p.dumps(frame), ex=p.FRAME_TTL)
            pipe.set(key, p.dumps(task))
            pipe.set(dkey, p.dumps(device))
            pipe.set(ckey, p.dumps(record), ex=p.RESULT_TTL)
            await pipe.execute()
            return {"envelope": envelope, "result": response}

        accepted = await self.atomic([ckey, dkey], op)
        if accepted:
            if frame and accepted["result"].get("status") == "ok":
                try:
                    await self.persist_evidence(accepted["envelope"]["actor_id"],
                                                accepted["envelope"]["task_id"], frame)
                except Exception as exc:
                    # 证据保存失败不得改变命令结果, 也不得回放命令。
                    logger.debug(f"[cu] 截图证据保存失败: {exc}")
            await self.publish(self.response_channel(command_id), accepted)
        return bool(accepted)

    async def expire_command(self, envelope):
        ckey = self.key("command", envelope["command_id"])
        dkey = self.device_key(envelope["device_id"])

        async def op(pipe):
            record = await self.json(ckey, pipe)
            if record and record["phase"] == "done":
                return record["result"]
            key = self.task_key(envelope["actor_id"], envelope["device_id"], envelope["session_id"], envelope["task_id"])
            await pipe.watch(key)
            task, device = await self.json(key, pipe), await self.json(dkey, pipe)
            response = p.result("unknown", error="命令结果未知；禁止自动重发")
            if task and task["state"] in {"cancelled", "paused"} and task["reason"] == "device_confirmed":
                response = p.result(task["state"])
            pipe.multi()
            if record:
                record.update(phase="done", result=response)
                pipe.set(ckey, p.dumps(record), ex=p.RESULT_TTL)
            if task and task["state"] not in p.TERMINAL and response["status"] == "unknown":
                self.close_task(pipe, key, task, "unknown", "command_timeout_or_transport_lost")
                if device and device["active_task"] == key and device["connection_generation"] == envelope["connection_generation"]:
                    device.update(ready=False, online_until=0, active_task="")
                    pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return response

        return await self.atomic([ckey, dkey], op)

    async def dispatch(self, actor, device_id, session_id, task_id, method, params, timeout=30.0, kind="command"):
        try:
            envelope = await self.prepare(actor, device_id, session_id, task_id, method, params, timeout, kind)
        except CapabilityDenied:
            # 能力不足与绑定/租约失败区分开, 界面据此提示, 而不是误导为设备离线。
            return p.result("rejected", error="capability_unavailable")
        except (ValueError, TypeError, RecursionError):
            return p.result("rejected", error="设备、任务绑定或权限校验失败")
        command_id = envelope["command_id"]
        sub = self.r.pubsub()
        try:
            # 必须先订阅再发布，避免快速设备响应丢失。所有进程均走同一总线。
            await sub.subscribe(self.response_channel(command_id))
            count = await self.publish(self.channel(device_id, envelope["connection_generation"], kind != "command"), envelope)
            if not count:
                return await self.expire_command(envelope)
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                raw = await sub.get_message(ignore_subscribe_messages=True, timeout=min(0.2, max(0.001, deadline - asyncio.get_running_loop().time())))
                if raw:
                    try:
                        answer = p.verified(await self.secret(), raw["data"])
                        if p.binding_matches(answer.get("envelope", {}), envelope):
                            # Redis 结果记录是权威；伪造/重放响应不能覆盖尚未确认的命令。
                            rec = await self.json(self.key("command", command_id))
                            if rec and rec["phase"] == "done" and answer.get("result") == rec["result"]:
                                return rec["result"]
                    except (ValueError, TypeError, AttributeError):
                        pass
                task_key = self.task_key(actor, device_id, session_id, task_id)
                current = await self.json(task_key)
                if current and current["state"] in p.TERMINAL | {"paused"}:
                    return await self.expire_command(envelope)
                await asyncio.sleep(0)
            return await self.expire_command(envelope)
        except asyncio.CancelledError:
            await asyncio.shield(self.expire_command(envelope))
            raise
        except Exception:
            # 异常内容可能包含载荷，不记录异常文本，也不重发。
            return await self.expire_command(envelope)
        finally:
            await sub.aclose()

    async def send_command(self, actor, device_id, session_id, task_id, method, params, timeout=30.0):
        return await self.dispatch(actor, device_id, session_id, task_id, method, params, timeout)

    async def control_task(self, actor, task_id, action):
        if action not in {"cancel", "pause"}:
            raise ValueError("无效控制动作")
        _, task = await self.task_record(actor, task_id)
        if task["state"] in p.TERMINAL:
            return self.task_view(task)
        await self.dispatch(actor, task["device_id"], task["session_id"], task_id, action, {}, 10.0, action)
        return await self.get_task(actor, task_id, with_frame=False)

    async def cancel_task(self, actor, task_id):
        return await self.control_task(actor, task_id, "cancel")


_service = None


async def get_service():
    global _service
    if _service is None:
        import config
        import redis.asyncio as redis
        _service = DeviceService(redis.from_url(config.REDIS_URL, decode_responses=True),
                                 prefix=f"{config.REDIS_PREFIX}:cu2",
                                 ceiling=p.admin_ceiling(
                                     desktop=bool(getattr(config, "CU_DESKTOP_CEILING", False)),
                                     scripts=bool(getattr(config, "CU_SCRIPT_CEILING", True))))
    return _service


async def get_selected_device(actor):
    return await (await get_service()).get_selected_device(actor)


async def start_task(actor, session_id, task_id, device_id=None):
    return await (await get_service()).start_task(actor, session_id, task_id, device_id)


async def send_command(actor, device_id, session_id, task_id, method, params, timeout=30.0):
    return await (await get_service()).send_command(actor, device_id, session_id, task_id, method, params, timeout)


async def finish_task(actor, task_id, state, reason=""):
    return await (await get_service()).finish_task(actor, task_id, state, reason)


async def cancel_task(actor, task_id):
    return await (await get_service()).cancel_task(actor, task_id)


async def progress(actor, task_id, step=None, approval=None):
    return await (await get_service()).progress(actor, task_id, step, approval)


async def renew_task(actor, task_id):
    return await (await get_service()).renew(actor, task_id)


async def get_task(actor, task_id, with_frame=True):
    return await (await get_service()).get_task(actor, task_id, with_frame)
