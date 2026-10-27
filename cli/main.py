#!/usr/bin/env python3
"""
Z-OPS v2.0.0 — CLI 命令行工具
用法: python -m cli.main <command> [options]

命令:
  agents list    列出所有 Agent 状态
  skills list    列出已注册 Skills
  doctor         系统健康检查
  backup create  创建配置备份
  backup verify  校验备份完整性
"""

import sys
import os
import argparse
import json
import time
import asyncio
from pathlib import Path
from datetime import datetime

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


# ═══ 输出格式化 ═══

class Colors:
    GREEN = '\033[32m'
    RED = '\033[31m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    CYAN = '\033[36m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

def ok(msg): print(f"  {Colors.GREEN}\u2714{Colors.RESET} {msg}")
def fail(msg): print(f"  {Colors.RED}\u2718{Colors.RESET} {msg}")
def warn(msg): print(f"  {Colors.YELLOW}\u26a0{Colors.RESET} {msg}")
def info(msg): print(f"  {Colors.BLUE}\u2139{Colors.RESET} {msg}")
def header(msg): print(f"\n{Colors.BOLD}{Colors.CYAN}{msg}{Colors.RESET}")


# ═══ agents list ═══

def cmd_agents_list(args):
    """列出所有 Agent 状态、心跳、技能数"""
    header("Z-OPS Agents")

    from core.registry import get_registry
    registry = get_registry()
    agents = registry.get_all()

    if not agents:
        fail("未找到任何 Agent 配置")
        return

    # 尝试从 Redis 获取心跳
    heartbeats = {}
    try:
        import redis
        r = redis.from_url(config.REDIS_URL, decode_responses=True)
        hb_data = r.hgetall(config.KEY_HEARTBEATS)
        for agent_id, raw in hb_data.items():
            try:
                heartbeats[agent_id] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        r.close()
    except Exception:
        warn("无法连接 Redis，心跳状态不可用")

    # 输出表格
    print(f"\n  {'ID':<20} {'名称':<16} {'状态':<10} {'Skills':<8} {'Model':<25} {'心跳'}")
    print(f"  {'─'*20} {'─'*16} {'─'*10} {'─'*8} {'─'*25} {'─'*20}")

    for agent_id, cfg in agents.items():
        name = cfg.name[:14]
        skills_count = len(cfg.skills)
        model = cfg.model[:23] if cfg.model else "-"

        hb = heartbeats.get(agent_id, {})
        status = hb.get("status", "unknown")
        last_hb = hb.get("last_heartbeat", 0)

        if status == "online":
            age = time.time() - last_hb if last_hb else 0
            if age < 30:
                status_str = f"{Colors.GREEN}online{Colors.RESET}"
                hb_str = f"{age:.0f}s ago"
            else:
                status_str = f"{Colors.YELLOW}stale{Colors.RESET}"
                hb_str = f"{age:.0f}s ago"
        elif status == "offline":
            status_str = f"{Colors.RED}offline{Colors.RESET}"
            hb_str = "-"
        else:
            status_str = f"{Colors.DIM}unknown{Colors.RESET}"
            hb_str = "-"

        enabled = "\u2714" if cfg.enabled else "\u2718"
        print(f"  {agent_id:<20} {name:<16} {status_str:<19} {skills_count:<8} {model:<25} {hb_str}")

    print(f"\n  总计: {len(agents)} 个 Agent")


# ═══ skills list ═══

def cmd_skills_list(args):
    """列出已注册 Skills 及归属 Agent"""
    header("Z-OPS Skills")

    from core.registry import get_registry
    from core.skill_registry import get_skill_registry

    registry = get_registry()
    skill_reg = get_skill_registry()

    skills = skill_reg.list_all()
    if not skills:
        warn("未发现已注册的 Skill")

    # 按域分组显示
    domains = {}
    skills_dir = config.BASE_DIR / "skills"
    if skills_dir.exists():
        for d in skills_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                domains[d.name] = []
                for py_file in d.glob("*.py"):
                    if not py_file.name.startswith("_"):
                        domains[d.name].append(py_file.stem)

    # 从 Registry 获取 Agent -> Skills 映射
    agent_skills = {}
    for agent_id, cfg in registry.get_all().items():
        for skill_name in cfg.skills:
            if skill_name not in agent_skills:
                agent_skills[skill_name] = []
            agent_skills[skill_name].append(agent_id)

    print(f"\n  {'Domain':<12} {'Skill':<20} {'已注册':<8} {'归属 Agent'}")
    print(f"  {'─'*12} {'─'*20} {'─'*8} {'─'*30}")

    total = 0
    registered = 0
    for domain, skill_files in sorted(domains.items()):
        for skill_file in sorted(skill_files):
            total += 1
            is_reg = skill_file in skills
            reg_str = f"{Colors.GREEN}\u2714{Colors.RESET}" if is_reg else f"{Colors.RED}\u2718{Colors.RESET}"
            if is_reg:
                registered += 1
            agents_str = ", ".join(agent_skills.get(skill_file, ["-"]))
            print(f"  {domain:<12} {skill_file:<20} {reg_str:<17} {agents_str}")

    # 显示注册了但没文件的 skills
    for name, skill in skills.items():
        if not any(name in files for files in domains.values()):
            total += 1
            registered += 1
            agents_str = ", ".join(agent_skills.get(name, ["-"]))
            print(f"  {'?':<12} {name:<20} {Colors.GREEN}\u2714{Colors.RESET}{'':>8} {agents_str}")

    print(f"\n  总计: {total} 个 Skill 文件, {registered} 个已注册")


# ═══ doctor ═══

def cmd_doctor(args):
    """系统健康检查"""
    header("Z-OPS Doctor — 系统健康检查")
    checks_ok = 0
    checks_fail = 0

    # 1. 配置文件
    header("配置文件")
    for f in ["agents.yaml", "zops.env", "config.py"]:
        p = config.BASE_DIR / f
        if p.exists():
            ok(f"{f} ({p.stat().st_size} bytes)")
            checks_ok += 1
        else:
            fail(f"{f} 不存在")
            checks_fail += 1

    # 2. agents.yaml 校验
    header("Agent 配置")
    try:
        from core.registry import get_registry
        registry = get_registry()
        agents = registry.get_all()
        ok(f"agents.yaml 加载成功: {len(agents)} 个 Agent")
        checks_ok += 1

        for agent_id, cfg in agents.items():
            issues = []
            if not cfg.soul:
                issues.append("无 SOUL.md")
            elif not Path(cfg.soul).exists():
                issues.append(f"SOUL.md 不存在: {cfg.soul}")
            if not cfg.workspace:
                issues.append("无 workspace")
            elif not Path(cfg.workspace).exists():
                issues.append(f"workspace 不存在")
            if issues:
                warn(f"{agent_id}: {', '.join(issues)}")
                checks_fail += 1
            else:
                ok(f"{agent_id}: OK")
                checks_ok += 1
    except Exception as e:
        fail(f"agents.yaml 加载失败: {e}")
        checks_fail += 1

    # 3. Redis
    header("Redis")
    try:
        import redis
        r = redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=3)
        r.ping()
        ok(f"Redis 连接正常: {config.REDIS_URL}")
        checks_ok += 1

        hb_count = r.hlen(config.KEY_HEARTBEATS)
        info(f"心跳注册数: {hb_count}")
        r.close()
    except Exception as e:
        fail(f"Redis 连接失败: {e}")
        checks_fail += 1

    # 4. LLM
    header("LLM 配置")
    provider = config.LLM_PROVIDER
    model = config.LLM_MODEL
    has_key = bool(config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY or config.ANTHROPIC_API_KEY or config.ONEAPI_API_KEY)
    if has_key:
        ok(f"Provider: {provider}, Model: {model}")
        if config.ONEAPI_API_KEY:
            info(f"OneAPI: {config.ONEAPI_BASE_URL}")
        checks_ok += 1
    else:
        fail(f"未配置 API Key (DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / ONEAPI_API_KEY)")
        checks_fail += 1

    # 5. Workspaces
    header("Workspaces")
    ws_dir = config.BASE_DIR / "workspaces"
    if ws_dir.exists():
        agent_dirs = [d for d in ws_dir.iterdir() if d.is_dir()]
        ok(f"{len(agent_dirs)} 个 Agent workspace")
        for d in sorted(agent_dirs):
            soul_ok = (d / "SOUL.md").exists()
            logs_ok = (d / "logs").exists()
            if soul_ok:
                ok(f"  {d.name}/SOUL.md")
            else:
                warn(f"  {d.name}/SOUL.md 缺失")
        checks_ok += 1
    else:
        fail("workspaces/ 目录不存在")
        checks_fail += 1

    # 6. Skills
    header("Skills")
    skills_dir = config.BASE_DIR / "skills"
    if skills_dir.exists():
        domains = [d for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
        total_skills = sum(1 for d in domains for f in d.glob("*.py") if not f.name.startswith("_"))
        ok(f"{len(domains)} 个域, {total_skills} 个 Skill 模块")
        checks_ok += 1
    else:
        fail("skills/ 目录不存在")
        checks_fail += 1

    # 7. 进程
    header("Agent 进程")
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-af", "python.*agents/"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            ok(f"{len(lines)} 个 Agent 进程运行中")
            for line in lines[:10]:
                info(f"  {line[:80]}")
            checks_ok += 1
        else:
            warn("未发现运行中的 Agent 进程")
            checks_fail += 1
    except Exception:
        warn("无法检测进程状态")

    # 汇总
    header("检查结果")
    total = checks_ok + checks_fail
    print(f"  通过: {Colors.GREEN}{checks_ok}{Colors.RESET} / {total}")
    if checks_fail > 0:
        print(f"  失败: {Colors.RED}{checks_fail}{Colors.RESET} / {total}")
    else:
        print(f"  {Colors.GREEN}所有检查通过!{Colors.RESET}")


# ═══ backup create / verify ═══

def cmd_backup_create(args):
    """创建配置备份归档"""
    import tarfile

    header("Z-OPS Backup — 创建备份")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"zops-backup-{timestamp}.tar.gz"
    backup_dir = config.BASE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / backup_name

    # 要备份的文件/目录
    backup_items = [
        ("agents.yaml", config.BASE_DIR / "agents.yaml"),
        ("zops.env", config.BASE_DIR / "zops.env"),
        ("config.py", config.BASE_DIR / "config.py"),
        ("requirements.txt", config.BASE_DIR / "requirements.txt"),
    ]

    # 添加 workspaces (SOUL.md 文件)
    ws_dir = config.BASE_DIR / "workspaces"
    if ws_dir.exists():
        for agent_dir in ws_dir.iterdir():
            if agent_dir.is_dir():
                soul_file = agent_dir / "SOUL.md"
                if soul_file.exists():
                    rel = f"workspaces/{agent_dir.name}/SOUL.md"
                    backup_items.append((rel, soul_file))

    # 添加 skills yaml
    skills_dir = config.BASE_DIR / "skills"
    if skills_dir.exists():
        for domain_dir in skills_dir.iterdir():
            if domain_dir.is_dir():
                yaml_file = domain_dir / "skill.yaml"
                if yaml_file.exists():
                    rel = f"skills/{domain_dir.name}/skill.yaml"
                    backup_items.append((rel, yaml_file))

    # 创建 tar.gz
    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            manifest = {"timestamp": timestamp, "files": []}
            for rel_name, abs_path in backup_items:
                if abs_path.exists():
                    tar.add(str(abs_path), arcname=rel_name)
                    manifest["files"].append({
                        "name": rel_name,
                        "size": abs_path.stat().st_size,
                    })
                    ok(f"备份: {rel_name}")
                else:
                    warn(f"跳过 (不存在): {rel_name}")

            # 写入清单
            manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
            import io
            manifest_bytes = manifest_json.encode("utf-8")
            tarinfo = tarfile.TarInfo(name="manifest.json")
            tarinfo.size = len(manifest_bytes)
            tar.addfile(tarinfo, io.BytesIO(manifest_bytes))

        size_kb = backup_path.stat().st_size / 1024
        ok(f"备份完成: {backup_path} ({size_kb:.1f} KB)")
        ok(f"包含 {len(manifest['files'])} 个文件")

    except Exception as e:
        fail(f"备份失败: {e}")


def cmd_backup_verify(args):
    """校验备份归档完整性"""
    import tarfile

    header("Z-OPS Backup — 校验备份")

    backup_dir = config.BASE_DIR / "backups"
    if not backup_dir.exists():
        fail("backups/ 目录不存在")
        return

    # 找到最新备份或指定文件
    backup_file = args.file if hasattr(args, 'file') and args.file else None
    if not backup_file:
        backups = sorted(backup_dir.glob("zops-backup-*.tar.gz"), reverse=True)
        if not backups:
            fail("未找到备份文件")
            return
        backup_file = str(backups[0])

    backup_path = Path(backup_file)
    if not backup_path.exists():
        fail(f"备份文件不存在: {backup_file}")
        return

    info(f"校验: {backup_path.name}")

    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            members = tar.getnames()
            ok(f"归档包含 {len(members)} 个条目")

            # 检查清单
            if "manifest.json" in members:
                f = tar.extractfile("manifest.json")
                manifest = json.loads(f.read().decode("utf-8"))
                ok(f"清单时间: {manifest.get('timestamp', 'N/A')}")
                for entry in manifest.get("files", []):
                    name = entry["name"]
                    if name in members:
                        ok(f"  {name} ({entry['size']} bytes)")
                    else:
                        fail(f"  {name} — 归档中缺失!")
            else:
                warn("清单文件 (manifest.json) 不存在")

            for member in members:
                if member != "manifest.json":
                    info(f"  {member}")

        ok("备份校验通过")

    except Exception as e:
        fail(f"校验失败: {e}")


# ═══ 主入口 ═══

def main():
    parser = argparse.ArgumentParser(
        prog="zops",
        description="Z-OPS 运维智能体 CLI 工具 v2.0.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m cli.main agents list    列出 Agent 状态
  python -m cli.main skills list    列出 Skills
  python -m cli.main doctor         健康检查
  python -m cli.main backup create  创建备份
  python -m cli.main backup verify  校验备份
        """
    )
    subparsers = parser.add_subparsers(dest="command")

    # agents
    agents_parser = subparsers.add_parser("agents", help="Agent 管理")
    agents_sub = agents_parser.add_subparsers(dest="subcommand")
    agents_sub.add_parser("list", help="列出所有 Agent")

    # skills
    skills_parser = subparsers.add_parser("skills", help="Skill 管理")
    skills_sub = skills_parser.add_subparsers(dest="subcommand")
    skills_sub.add_parser("list", help="列出所有 Skills")

    # doctor
    subparsers.add_parser("doctor", help="系统健康检查")

    # backup
    backup_parser = subparsers.add_parser("backup", help="备份管理")
    backup_sub = backup_parser.add_subparsers(dest="subcommand")
    backup_sub.add_parser("create", help="创建备份")
    verify_parser = backup_sub.add_parser("verify", help="校验备份")
    verify_parser.add_argument("--file", "-f", help="备份文件路径")

    args = parser.parse_args()

    if args.command == "agents" and getattr(args, "subcommand", None) == "list":
        cmd_agents_list(args)
    elif args.command == "skills" and getattr(args, "subcommand", None) == "list":
        cmd_skills_list(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "backup" and getattr(args, "subcommand", None) == "create":
        cmd_backup_create(args)
    elif args.command == "backup" and getattr(args, "subcommand", None) == "verify":
        cmd_backup_verify(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
