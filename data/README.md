# Z-OPS 运行时数据目录

本目录存放 Z-OPS 运行时产生的**真实运维记忆数据**，包括跨会话记忆、用户画像、技能反馈等。

> ⚠️ **此目录内容不提交到 Git**：可能含用户 ID、VM 名、业务报错等敏感信息。
>
> 克隆仓库后，运行时会自动创建这些文件。若想预览数据格式，请看 `sample/` 脱敏示例。

## 文件说明

| 文件 | 说明 | 读写模块 |
|---|---|---|
| `cross_session_memory.json` | 跨会话记忆：sessions + users | `core/memory/cross_session_memory.py` |
| `user_profiles.json` | 用户画像 + Honcho dialectic notes | `core/memory/user_profile.py` |
| `skill_improvements.json` | 技能执行反馈（成功/失败/超时） | `core/memory/skill_improvement.py` |
| `memory_nudges.json` | 记忆提醒队列 | `core/memory/memory_nudge.py` |
| `trajectories/traj_*.json` | 任务执行轨迹（回放用） | `core/memory/trajectory.py` |

## 脱敏示例

`sample/` 目录提供了每类文件的最小脱敏示例，字段命名规则如下：

- `user_id`: `demo_user_*` / `sample_user_*`
- `vm_name` / `asset`: `vm-demo` / `demo-target`
- `session_id`: `sample_session_*`
- `skill`: `demo_skill`

## 初始化

首次运行 Z-OPS 时，若 `data/*.json` 不存在，各模块会自动创建空结构。也可手动复制示例：

```bash
cp data/sample/cross_session_memory.json data/
cp data/sample/user_profiles.json data/
cp data/sample/skill_improvements.json data/
echo '{"nudges": []}' > data/memory_nudges.json
```
