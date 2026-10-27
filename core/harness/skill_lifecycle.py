"""Z-OPS P2 — 技能生命周期管理 (SPEC: 可验证学习、知识 ACL 与技能发布)

生命周期: candidate → evaluated → approved → published → retired

红线 (SPEC):
  - 内容变更创建不可变新版本 (同版本号不同内容 → 自动递增新版本, 拒绝覆盖);
  - 发布指针只指向已批准版本; 线上文件 (skills/zops_generated/) 仅由 publish 写入;
  - 越级发布拒绝 (candidate 未经 evaluated/approved 不得 published);
  - 每版绑定来源 execution/纠错证据、content hash、owner、审核记录;
  - 未验证成功率为 null/unknown, 不将初始值设为成功; 成功率只从可核验验收证据计算;
  - 后台复盘只能写候选 (submit_candidate), 不能改生产权限或自动发布。

存储:
  data/skill_lifecycle/index.json              — 生命周期索引 (状态/指针/审核记录)
  data/skill_lifecycle/{name}/{version}.yaml   — 不可变版本内容 (含 provenance)
  skills/zops_generated/{name}.yaml             — 线上发布内容 (publish 独占写入)
"""

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("zops.harness.lifecycle")

BASE = Path("/opt/zops/data/skill_lifecycle")
LIVE_DIR = Path("/opt/zops/skills/zops_generated")

STATES = ("candidate", "evaluated", "approved", "published", "retired")
# 允许的状态迁移 (越级即拒绝)
TRANSITIONS = {
    "candidate": {"evaluated"},
    "evaluated": {"approved", "candidate"},   # evaluated → candidate: 退回重评
    "approved": {"published", "retired"},
    "published": {"retired"},
    "retired": {"published"},                 # 仅指针回移 (回退到已批准旧版)
}


class LifecycleError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _content_hash(skill_data: dict) -> str:
    import yaml
    canon = yaml.dump(skill_data, allow_unicode=True, sort_keys=True,
                      default_flow_style=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _bump_version(version: str) -> str:
    try:
        parts = version.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (ValueError, IndexError):
        return f"{version}.1"


class SkillLifecycle:
    def __init__(self, base: Path = BASE, live_dir: Path = LIVE_DIR,
                 aux_budget_daily: int = 50):
        self.base = base
        self.live_dir = live_dir
        self.aux_budget_daily = aux_budget_daily   # 复盘辅助模型预算 (次/日)
        (self.base).mkdir(parents=True, exist_ok=True)
        self._idx_path = self.base / "index.json"
        self._index = self._load_index()

    # ── 索引 ──

    def _load_index(self) -> dict:
        try:
            if self._idx_path.exists():
                return json.loads(self._idx_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[lifecycle] 索引读取失败, 重建: {e}")
        return {"skills": {}, "updated_at": ""}

    def _save_index(self):
        self._index["updated_at"] = datetime.now().isoformat()
        self._idx_path.write_text(
            json.dumps(self._index, indent=2, ensure_ascii=False), encoding="utf-8")

    def _skill(self, name: str) -> dict:
        return self._index["skills"].setdefault(name, {"versions": {}, "published": None})

    def _ver(self, name: str, version: str) -> dict:
        s = self._skill(name)
        if version not in s["versions"]:
            raise LifecycleError("version_not_found", f"{name}@{version}")
        return s["versions"][version]

    # ── 复盘预算 (SPEC: 缺服务授权/模型预算则暂停; 辅助模型独立预算) ──

    def aux_budget_left(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        used = self._index.get("aux_budget", {}).get(today, 0)
        return max(0, self.aux_budget_daily - used)

    def aux_budget_consume(self, n: int = 1) -> bool:
        """复盘 LLM 调用计数; 超预算返回 False (调用方应暂停复盘, 不影响主链路)"""
        today = datetime.now().strftime("%Y-%m-%d")
        bucket = self._index.setdefault("aux_budget", {})
        if bucket.get(today, 0) + n > self.aux_budget_daily:
            logger.warning(f"[lifecycle] 复盘辅助模型预算耗尽 ({today}), 复盘暂停")
            return False
        bucket[today] = bucket.get(today, 0) + n
        self._save_index()
        return True

    # ── 提交候选 (后台复盘唯一允许的写入口) ──

    def submit_candidate(self, skill_data: dict, author: str = "retro",
                         source_execution: str = "", evidence: Optional[List[str]] = None,
                         allow_auto_publish: bool = False) -> str:
        """写入候选版本。内容不可变: 同版本号已存在且 hash 不同 → 自动递增版本号。
        绝不直接发布 (allow_auto_publish 参数仅作显式审计标记, 恒拒绝)。"""
        if allow_auto_publish:
            raise LifecycleError("auto_publish_forbidden",
                                 "复盘不得自动发布; 走 evaluate→approve→publish")
        if not skill_data or not skill_data.get("name"):
            raise LifecycleError("invalid_skill", "缺少 name")
        name = str(skill_data["name"])
        version = str(skill_data.get("version", "1.0.0"))
        chash = _content_hash(skill_data)
        s = self._skill(name)
        # 同版本号不可覆盖 (SPEC: 同版本覆盖禁止); 同内容重复提交幂等返回
        while version in s["versions"]:
            if s["versions"][version].get("content_hash") == chash:
                logger.info(f"[lifecycle] 重复候选幂等返回: {name}@{version}")
                return version
            version = _bump_version(version)   # 内容变更 → 不可变新版本
        skill_data = dict(skill_data)
        skill_data["version"] = version
        vdir = self.base / name
        vdir.mkdir(parents=True, exist_ok=True)
        vfile = vdir / f"{version}.yaml"
        import yaml
        vfile.write_text(yaml.dump(skill_data, allow_unicode=True,
                                   default_flow_style=False, sort_keys=False),
                         encoding="utf-8")
        s["versions"][version] = {
            "state": "candidate",
            "content_hash": chash,
            "author": author,
            "source_execution": source_execution,
            "evidence": list(evidence or []),
            "submitted_at": datetime.now().isoformat(),
            "tests": [], "review": None,
            "outcomes": [],          # {ts, success, verified}
        }
        self._save_index()
        logger.info(f"[lifecycle] 新候选: {name}@{version} (author={author}, "
                    f"evidence={len(evidence or [])})")
        return version

    # ── 评估 / 审核 / 发布 ──

    def _transition(self, name: str, version: str, to_state: str) -> dict:
        v = self._ver(name, version)
        cur = v["state"]
        if to_state not in TRANSITIONS.get(cur, set()):
            raise LifecycleError(
                "illegal_transition",
                f"{name}@{version}: {cur} → {to_state} (越级/非法迁移拒绝)")
        v["state"] = to_state
        return v

    def evaluate(self, name: str, version: str, passed: bool,
                 tests: Optional[List[str]] = None, notes: str = "") -> None:
        self._transition(name, version, "evaluated")
        v = self._ver(name, version)
        v["tests"].append({
            "ts": datetime.now().isoformat(), "passed": bool(passed),
            "tests": list(tests or []), "notes": notes[:300],
        })
        if not passed:
            # 评估未过 → 退回 candidate 重改 (保持候选, 不得继续 approve)
            v["state"] = "candidate"
            v["eval_failed"] = True
        self._save_index()

    def approve(self, name: str, version: str, approver: str,
                is_admin: Callable[[], bool] = lambda: False) -> None:
        v = self._ver(name, version)
        if v["state"] != "evaluated":
            raise LifecycleError("illegal_transition",
                                 f"{name}@{version}: {v['state']} → approved (必须先 evaluated)")
        if not is_admin():
            raise LifecycleError("not_authorized", "仅管理员可批准技能版本")
        if not any(t.get("passed") for t in v.get("tests") or []):
            raise LifecycleError("no_passing_tests", "无通过记录的版本不得批准")
        self._transition(name, version, "approved")
        v["review"] = {"approver": approver,
                       "approved_at": datetime.now().isoformat()}
        self._save_index()

    def publish(self, name: str, version: str, approver: str,
                is_admin: Callable[[], bool] = lambda: False) -> None:
        if not is_admin():
            raise LifecycleError("not_authorized", "仅管理员可发布")
        s = self._skill(name)
        v = self._ver(name, version)
        if v["state"] not in ("approved", "retired"):
            raise LifecycleError("illegal_transition",
                                 f"{name}@{version}: {v['state']} → published (仅已批准版本可发布)")
        # 旧发布版本 → retired (指针迁移, 不改内容)
        if s.get("published") and s["published"] != version:
            old = s["versions"].get(s["published"])
            if old and old["state"] == "published":
                old["state"] = "retired"
                old["retired_at"] = datetime.now().isoformat()
        # 发布指针 + 线上文件写入 (唯一写入点)
        v["state"] = "published"
        v.setdefault("publish_log", []).append(
            {"approver": approver, "ts": datetime.now().isoformat()})
        s["published"] = version
        import yaml
        vfile = self.base / name / f"{version}.yaml"
        content = yaml.safe_load(vfile.read_text(encoding="utf-8"))
        self.live_dir.mkdir(parents=True, exist_ok=True)
        (self.live_dir / f"{name}.yaml").write_text(
            yaml.dump(content, allow_unicode=True, default_flow_style=False,
                      sort_keys=False), encoding="utf-8")
        self._save_index()
        logger.warning(f"[lifecycle] 发布: {name}@{version} (approver={approver})")

    def rollback(self, name: str, to_version: str, approver: str,
                 is_admin: Callable[[], bool] = lambda: False) -> None:
        """版本回退 = 指针回移到已批准版本 (内容不可变, 不重写历史)"""
        v = self._ver(name, to_version)
        if v["state"] not in ("approved", "retired"):
            raise LifecycleError("rollback_target_not_approved",
                                 f"{name}@{to_version} 状态 {v['state']} (仅已批准版本可回退)")
        self.publish(name, to_version, approver, is_admin=is_admin)

    def retire(self, name: str, approver: str,
               is_admin: Callable[[], bool] = lambda: False) -> None:
        if not is_admin():
            raise LifecycleError("not_authorized", "仅管理员可停用")
        s = self._skill(name)
        if not s.get("published"):
            raise LifecycleError("nothing_published", name)
        v = s["versions"][s["published"]]
        self._transition(name, s["published"], "retired")
        v["retired_at"] = datetime.now().isoformat()
        v["retired_by"] = approver
        # 停用 = 移除线上文件 (检索不再注入), 历史版本保留
        live = self.live_dir / f"{name}.yaml"
        try:
            live.unlink()
        except FileNotFoundError:
            pass
        self._save_index()

    # ── 读取 / 成功率 ──

    def get_published(self, name: str) -> Optional[dict]:
        s = self._index["skills"].get(name)
        if not s or not s.get("published"):
            return None
        v = s["versions"].get(s["published"])
        if not v or v.get("state") != "published":
            return None
        import yaml
        vfile = self.base / name / f"{s['published']}.yaml"
        if not vfile.exists():
            return None
        return yaml.safe_load(vfile.read_text(encoding="utf-8"))

    # ── P2 收尾: 技能消费端 (published-only 注入; 未发布/停用技能绝不进 prompt) ──

    def prompt_for_task(self, task_text: str, limit: int = 3) -> tuple:
        """按任务文本匹配已发布技能 → (prompt 块, [技能名])。

        渐进披露: 列表只注入名+summary, top 命中附 procedure 片段 (控制 token)。
        只读 published 版本 — candidate/evaluated/approved/retired 一律不注入。
        """
        text = (task_text or "").lower()
        if not text:
            return "", []
        scored = []
        for skill in self.published_skills():
            score = 0
            meta = ((skill.get("metadata") or {}).get("zops") or {})
            if skill.get("name", "").lower() in text:
                score += 10
            for tag in (meta.get("tags") or []):
                if str(tag).lower() in text:
                    score += 3
            wtu = str(skill.get("when_to_use") or "").lower()
            if wtu and len(wtu) < 120 and any(w in text for w in wtu.split()[:4] if len(w) > 1):
                score += 2
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda x: -x[0])
        top = [s for _, s in scored[:limit]]
        if not top:
            return "", []
        lines = ["\n## 可用技能 (已发布)"]
        names = []
        for s in top:
            names.append(s["name"])
            brief = s.get("summary") or s.get("description", "")
            lines.append(f"- **{s['name']}**: {brief}")
        for s in top[:1]:
            proc = str(s.get("procedure") or "")[:400]
            if proc:
                lines.append(f"\n### {s['name']} 参考步骤\n{proc}...")
        return "\n".join(lines) + "\n", names

    def usage_stats(self, name: str) -> dict:
        """使用统计: 总次数 (全部 outcome) / 可核验次数 / 成功率 (verified-only)"""
        s = self._index["skills"].get(name)
        if not s or not s.get("published"):
            return {"usage": 0, "verified": 0, "success_rate": None}
        outcomes = s["versions"].get(s["published"], {}).get("outcomes", [])
        verified = [o for o in outcomes if o.get("verified")]
        return {
            "usage": len(outcomes),
            "verified": len(verified),
            "success_rate": (sum(1 for o in verified if o["success"]) / len(verified)
                             if verified else None),
        }

    def record_skill_outcomes(self, names: list, success: bool,
                              verified: bool = False) -> None:
        """技能使用结果批量回写 (verified 默认 False — 回复成功≠业务验收, 口径不虚高)"""
        for n in (names or []):
            try:
                self.record_outcome(n, success=success, verified=verified)
            except Exception:
                pass

    def published_skills(self) -> List[dict]:
        out = []
        for name in self._index.get("skills", {}):
            sk = self.get_published(name)
            if sk:
                out.append(sk)
        return out

    def record_outcome(self, name: str, success: bool, verified: bool) -> None:
        """使用结果记录; 成功率只统计 verified=True 的可核验证据 (SPEC)"""
        s = self._index["skills"].get(name)
        if not s or not s.get("published"):
            return
        v = s["versions"].get(s["published"], {})
        v.setdefault("outcomes", []).append({
            "ts": time.time(), "success": bool(success), "verified": bool(verified)})
        v["outcomes"] = v["outcomes"][-200:]
        self._save_index()

    def success_rate(self, name: str):
        """未验证 → None (不默认成功); 只从可核验证据计算"""
        s = self._index["skills"].get(name)
        if not s or not s.get("published"):
            return None
        outcomes = [o for o in s["versions"].get(s["published"], {}).get("outcomes", [])
                    if o.get("verified")]
        if not outcomes:
            return None
        return sum(1 for o in outcomes if o["success"]) / len(outcomes)

    def describe(self, name: str = "") -> dict:
        if name:
            s = self._index["skills"].get(name)
            return dict(s) if s else {}
        return {k: {"published": v.get("published"),
                    "versions": {ver: {"state": vv["state"],
                                       "hash": vv.get("content_hash")}
                                 for ver, vv in v.get("versions", {}).items()}}
                for k, v in self._index.get("skills", {}).items()}


_lifecycle: Optional[SkillLifecycle] = None


def _bootstrap_existing(lc: "SkillLifecycle") -> int:
    """存量线上技能收编: 注册为 approved+published (证据=legacy-bootstrap)。

    必须先于门控生效执行一次, 否则 P2 门控会让存量技能"失联"。
    内容 hash 以收编时线上文件为准; 后续变更走正常候选流程。
    """
    import yaml
    n = 0
    if not lc.live_dir.exists():
        return 0
    for f in lc.live_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[lifecycle] 收编跳过损坏文件 {f.name}: {e}")
            continue
        if not data or not data.get("name"):
            continue
        name = str(data["name"])
        version = str(data.get("version", "1.0.0"))
        s = lc._index["skills"].setdefault(name, {"versions": {}, "published": None})
        if s.get("published"):
            continue   # 已收编
        chash = _content_hash(data)
        vdir = lc.base / name
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{version}.yaml").write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False,
                      sort_keys=False), encoding="utf-8")
        s["versions"][version] = {
            "state": "published",
            "content_hash": chash,
            "author": data.get("author", "legacy"),
            "source_execution": "",
            "evidence": ["legacy-bootstrap: 收编自线上文件 (P2 门控前已存在)"],
            "submitted_at": datetime.now().isoformat(),
            "tests": [{"ts": datetime.now().isoformat(), "passed": True,
                       "tests": ["legacy"], "notes": "存量收编"}],
            "review": {"approver": "bootstrap-20261013",
                       "approved_at": datetime.now().isoformat()},
            "outcomes": [],
        }
        s["published"] = version
        n += 1
    if n:
        lc._save_index()
        logger.info(f"[lifecycle] 存量技能收编: {n} 个 (approved+published)")
    return n


def get_skill_lifecycle() -> SkillLifecycle:
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = SkillLifecycle()
        try:
            _bootstrap_existing(_lifecycle)
        except Exception as e:
            logger.warning(f"[lifecycle] 存量收编失败 (下次重试): {e}")
    return _lifecycle
