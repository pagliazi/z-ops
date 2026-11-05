# Z-OPS · 企业级 AI 运维智能体平台

> **Z-OPS**（Intelligent Operations Claw）是一款面向企业的 AI 运维智能体平台，通过 8 个专业 Agent + 智能编排引擎 + LLM-Wiki 记忆体系，实现基础设施运维的自然语言化、自主化、可沉淀。

[![Version](https://img.shields.io/badge/version-v2.0.0-blue)]()
[![Python](https://img.shields.io/badge/python-3.10+-green)]()
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)

---

## ✨ 核心特性

- **8 Agent 专业分工** — general / ops / network / vm / dev / browser / kb / opscore
- **Agents-as-Tools 编排** — LLM function_calling 路由，多轮上下文感知
- **OneAPI 统一网关** — 7 Provider / 20+ 模型，Qwen3-235B 主力
- **自我进化闭环** — LLM 反思失败 → 根因分析 → 技能改进 → A/B 验证
- **LLM-Wiki 记忆** — 基于 Karpathy 的 LLM-Wiki 思想，知识在 ingest 时编译
- **N8N 工作流** — 20 个生产级工作流，Agent-工作流无缝桥接

## 🧠 LLM-Wiki 记忆系统

Z-OPS 采用 ingest-time understanding 的记忆模式：多源数据（会话/画像/技能反馈/Qoder memory）在入库时由 LLM 编译为结构化 Markdown 知识库，BM25 检索整页入上下文，替代 RAG 的 retrieval-time understanding。

Wiki 作为独立仓库发布：**[pagliazi/zops-wiki](https://github.com/pagliazi/zops-wiki)**

## 🚀 快速开始

### 1. 克隆

```bash
git clone git@github.com:pagliazi/zops.git z-ops
cd z-ops
```

### 2. 准备配置

```bash
# 复制 .env 模板并填入真实值
cp zops.env.example zops.env
cp feishu.env.example feishu.env
cp webchat.env.example webchat.env
cp n8n.env.example n8n.env
# 编辑 zops.env，至少填 ONEAPI_API_KEY、REDIS_URL、JWT_SECRET
```

### 3. 初始化数据

```bash
# 可选：用脱敏示例初始化
cp data/sample/cross_session_memory.json data/
cp data/sample/user_profiles.json data/
cp data/sample/skill_improvements.json data/
echo '{"nudges": []}' > data/memory_nudges.json
```

### 4. 启动

```bash
./manage_zops.sh start
# 访问 https://<host>:7789
```

### 5.（可选）挂载 LLM-Wiki

```bash
git clone git@github.com:pagliazi/zops-wiki.git wiki
python3 -m core.memory.wiki_ingest   # 首次编译
```

## 🖥️ 本机（桌面）接管

桌面操作能力默认全关，且必须先在**本机**做一遍人工验收；验收记录只留在本机，每次接管仍要
在你的桌面上点一次“允许”（60 秒有效，30 秒无人操作自动拒绝）。当前仅支持 X11，Wayland
会话（含 XWayland）会以 `wayland_portal_not_implemented` 拒绝，Linux 也不做窗口采集。

```bash
# companion 要求 Python 3.11+；源码方式是仓库内 venv，交付方式是打包好的可执行文件
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json --allow-desktop-app firefox
companion/.venv/bin/python -m companion --config ~/.config/zops/config.json --accept-desktop
companion/.venv/bin/python scripts/linux_desktop_attended.py --config ~/.config/zops/config.json
companion/.venv/bin/python scripts/linux_desktop_check.py --config ~/.config/zops/config.json \
  --attended ~/zops-linux-attended.json
```

清单里缺项、未做、未通过，结论都是“未通过”；用脚本扮演机旁的人不算验收。
详细步骤与注意事项见 [QUICKSTART.md](./QUICKSTART.md#-桌面接管验收linux--x11)。

## 📁 目录结构

```
z-ops/
├── core/               # 核心引擎（QueryEngine / ToolPool / Memory / Self-Improving）
├── agents/             # 8 个 Agent 实现
├── skills/             # 技能定义（per-agent）
├── cli/                # 命令行工具
├── channels/           # 接入通道（FastAPI / 飞书 / 企微）
├── n8n_workflows/      # N8N 工作流定义
├── web-next/           # Next.js 前端控制台
├── webchat_frontend/   # 纯 HTML 简版前端
├── data/               # 运行时数据（含脱敏示例 sample/）
├── wiki/               # LLM-Wiki（独立仓库，.gitignore 排除）
├── deploy/             # 部署脚本
├── templates/          # 提示词模板
├── utils/              # 工具函数
└── *.env.example       # 配置模板
```

## 🏗️ 架构文档

- [Z-OPS-Architecture-v3.md](./Z-OPS-Architecture-v3.md) — 完整架构
- [Z-OPS-v4-ARCHITECTURE-PLAN.md](./Z-OPS-v4-ARCHITECTURE-PLAN.md) — 架构演进计划
- [SERVICES_GUIDE.md](./SERVICES_GUIDE.md) — 服务管理
- [QUICKSTART.md](./QUICKSTART.md) — 快速开始

## 📜 License

本项目基于 **Apache License 2.0** 发布，版权归 Z-OPS Project 所有（2024-2026）。

- 许可证全文：[LICENSE](./LICENSE)
- 版权归属声明：[NOTICE](./NOTICE)

使用、修改、分发时需遵守：

- 保留 `LICENSE` 与 `NOTICE` 中的版权、归属与免责声明；
- 按许可证第 4 节要求，对修改过的文件附变更说明；
- 若本项目包含 `NOTICE` 文件，分发时需一并保留。

第三方依赖遵循各自许可证，详见 `requirements.txt`、`web-next/package.json`、`companion/pyproject.toml`。
