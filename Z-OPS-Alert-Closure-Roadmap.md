# Z-OPS 告警闭环推进路线（SSL 之后）

> 定位：回答「SSL 证书只是告警里的一小部分，其他告警怎么推进」。
> 口径：只读取证、人工处置、自动核验恢复；不自动 ACK / 屏蔽 / 改生产配置 / 调 LLM。
> 数据快照：2026-10-24 23:40（`zops:nms_alarms:latest`），数字每天会变，结论按同一口径复算。

---

## 一、先看数据：为什么不能照搬 SSL 的做法

| 指标 | 数值 | 含义 |
| --- | --- | --- |
| 活跃告警 | 38,418 条 | `severity ∈ critical/major/minor/warning/indeterminate` |
| 命中 Checklist 预案 | 46 条（**0.12%**） | 预案覆盖率是真正的瓶颈，不是闭环类型太少 |
| 已有主动探针可闭环（L1） | 54 条（**0.1%**） | 只做 SSL 式单告警闭环，天花板就在这里 |
| 可簇级/可扩展取证（L2） | 36,490 条（**95.0%**） | 网络协议类 + 认证类 |
| 仅能建议（L3） | 1,874 条（4.9%） | 长尾/无稳定探针 |

按类型的集中度（同一快照）：

| 类型 | 条数 | 设备数 | event_key 数 | 分层 |
| --- | --- | --- | --- | --- |
| 网络协议/链路（BGP/BFD/LSP/LINK/VRF…） | 34,944 | 592 | 12,228 | L2 |
| 其他/长尾 | 1,874 | 848 | 153 | L3 |
| 认证/账号 | 1,515 | 408 | **6** | L2 |
| 存储 | 30 | 24 | 13 | L2（待接探针） |
| 备份 | 35 | 25 | 25 | L1（已有探针） |
| 证书 | 16 | 15 | 16 | L1（已闭环） |
| DNS | 3 | 2 | 3 | L1（已有探针） |
| 邮件队列 | 1 | 1 | 1 | L2（待接探针） |

**关键结论**：

1. 网络类占活跃量 **91%**，但只有 **592 台设备**；按 `source × event_group` 聚合仅 **2,236 个单元**，
   top5 设备就占 21%、top50 占 55%。→ 闭环单元应是**设备级故障单元**，不是单条告警。
2. 认证类 1,515 条只来自 **6 个模板**、408 台设备；典型「一个模板 × 多设备」，
   但其中大量可能来自采集会话失效（已知坑），必须先与「NMS 全局会话失效」告警分离，否则会误判为业务故障。
3. 备份/DNS/证书三类加起来 54 条，量小但**探针已经现成**（`tool_checks.backup/dns/ssl`），
   改造成本最低，适合作为「L1 多类型」验证。
4. 最有价值的顺序不是「按条数」，而是「先拿 95% 的簇级能力，再用 L1 补精度」。

---

## 二、三层能力模型（按取证方式分层，不按告警等级）

| 层 | 定义 | 恢复判定 | 典型类型 |
| --- | --- | --- | --- |
| **L1 单对象闭环** | 有主动探针，能对目标做严格实测 | 连续 N 次健康 + NMS 明确清除（与 SSL 一致） | 证书、DNS、TCP 端口、备份作业、NTP |
| **L2 设备级簇闭环** | 无单目标探针，但能靠 NMS 自身状态判定 | 该设备上同簇告警**全部清除** + M 分钟内无复发 + 无新增同源告警 | BGP/BFD/LSP/链路、认证、存储、邮件队列 |
| **L3 观察建议** | 无稳定判据 | 不自动核验，只做预案/知识推荐 + 人工确认 | 长尾 syslog、一次性事件 |

### 闭环单元（重要）

- L1：`scope + 探针目标（host:port / 域名 / 作业 ID）`
- L2：`scope + source（设备/主机）+ 归一类（network/auth/storage/...）`，**不是** alarm_id、不是 event_key

依据：12,228 个 event_key 与 5,532 个 incident 都过于碎片化（同一设备多个 BGP 邻居各成一簇），
按 `source × 归一类` 收敛到 2,236 个单元后，人工确认量才可接受。

---

## 三、推进顺序（按「覆盖量 ÷ 改造成本」）

### P0：已交付（本次）

- SSL 单告警闭环灰度（`NMS_SSL_MAX_ACTIVE_PER_USER=2`、`NMS_SSL_GLOBAL_CONCURRENCY=1`）。
- 运行时可观测性 + `GET /api/nms/ssl-metrics`（管理员只读）。
- 闭环覆盖度评估：`GET /api/nms/closure-coverage`，按类型/分层/设备组返回量与推荐顺序；
  纯函数实现在 `core/alert_closure.py`，可每日复算，不读告警原文、不触发处置。

### P1：L1 多类型（已交付）

| 顺序 | 类型 | 探针实现 | 数据真源 | 健康判据 |
| --- | --- | --- | --- | --- |
| 1 | 备份作业 | `core/alert_probes.probe_backup` | GCOC-MGT `/api/brr/backups/`、`/api/veeam-m365/backup-jobs/` | 该主机 `server_url` 精确匹配的作业无 `Failed` |
| 2 | DNS 解析 | `probe_dns` | `agents.dns_api`（GCOC DNS） | 解析出 ≥1 条 A 记录 |
| 3 | TCP 端口 | `probe_tcp` | socket 连接 | 端口可建立连接 |
| 4 | NTP 同步 | `probe_ntp` | UDP/123 标准 48 字节查询 | `stratum` 1–15 且 mode 4/5 |

要点：

- 证书仍走 SSL 专有工作流（单元上标 `handoff=nms_ssl_workflow`），不重复实现。
- 备份探针必须「前 3 段搜索 + `server_url` 精确过滤」：GCOC-MGT 的 `search` 不支持完整 IP，
  直接搜 `198.18.16.222` 返回 0 条（实测），按 `198.18.16` 搜索再过滤才准。
- 连续 3 次「无法取证」（如该主机没有备份作业数据）自动回退簇级判据，
  并在 `recovery_evidence` 标注 `probe_unavailable=N+cluster_clear=Ns`，不把未知当健康。

### P2：L2 设备级簇闭环（已交付）

实现在 `core/alert_closure_engine.py`：

1. **单元聚合**：`unit_key = kind:source`，复用 `core/alert_closure.classify` 的归一类；
   证据字段含告警量、严重度分布、event_key/协议组数、首见时间、样本摘要、ACK 数、探针目标。
2. **状态机**：`observed → confirmed → waiting_clear → recovered`，另有 `paused` / `expired`；
   只有 `confirmed/waiting_clear` 参与恢复核验，默认不跟踪任何单元（需人工确认）。
3. **恢复核验**：单元活跃告警清零后连续静默 `NMS_CLOSURE_QUIET_S`（默认 1800s）；
   空快照/采集失败不推清除；维护窗口内不判定恢复；确认后超过 `NMS_CLOSURE_TRACK_WINDOW_S`
   （默认 24h）自动 `expired`，避免长期挂着假跟踪。
4. **维护窗口**：直接复用已有 `zops:nms_maint_windows`（`POST /api/nms/suppress/maint` 写入），
   不需要 CMDB 接口即可排除计划维护；这也是原先待决策项 1 的落地方式。
5. **风险控制**：不自动 ACK/屏蔽/改配置；所有状态变更写事件（`zops:alert_closure:events`）并记录操作人。

### P3：认证类与长尾（已纳入同一引擎，只做观察与预案）

- 认证类 418 个单元、长尾 932 个单元已自动聚合，状态默认 `observed`，不做自动核验。
- 与「采集会话失效」伪告警的分离仍未做（`zops:nms_session:alert_sent` 已有信号，属后续专项）；
  在分离前不建议对认证单元开启自动恢复核验。
- 长尾闭环不做，只补预案覆盖率（`GET /api/nms/closure-coverage` 的 `checklist_coverage` 是长期指标）。

---

## 三·五、API 与配置（已上线）

| 端点 | 权限 | 作用 |
| --- | --- | --- |
| `GET /api/nms/closure/summary` | 管理员 | 单元状态分布、跟踪数、阈值、运行指标 |
| `GET /api/nms/closure/units?status=&tier=&kind=&q=` | 管理员 | 单元列表（默认按加权量降序） |
| `GET /api/nms/closure/units/{key}` | 管理员 | 单元详情 + 最近 50 条事件 + 案例 |
| `POST /api/nms/closure/units/{key}/confirm` | 管理员 | 确认根因，进入自动恢复核验 |
| `POST /api/nms/closure/units/{key}/pause|resume|reopen` | 管理员 | 暂停 / 恢复跟踪 / 重开已关闭单元 |
| `POST /api/nms/closure/units/{key}/recheck` | 管理员 | 立即核验（L1 真探测，L2 用最近快照重算） |
| `POST /api/nms/closure/units/{key}/case` | 管理员 | 归档案例（仅 `recovered` 单元） |
| `GET /api/nms/closure/cases` | 管理员 | 案例库 |
| `POST /api/nms/closure/refresh` | 管理员 | 立即跑一轮刷新（与后台 worker 同一逻辑） |

配置（`zops.env` 可覆盖，均为只读核验参数）：

```
NMS_CLOSURE_ENABLED=true              # 总开关，false=worker 停止刷新/探测
NMS_CLOSURE_QUIET_S=1800              # L2 静默恢复门槛
NMS_CLOSURE_L1_HEALTHY=2              # L1 连续健康次数
NMS_CLOSURE_L1_MIN_SPACING_S=120      # L1 相邻健康最小间隔
NMS_CLOSURE_PROBE_UNKNOWN_LIMIT=3     # 连续无法取证→回退簇级判据
NMS_CLOSURE_PROBE_INTERVAL=300        # L1 探测节流
NMS_CLOSURE_PROBE_TIMEOUT=8           # 单次探测超时（备份探针至少 30s）
NMS_CLOSURE_MAX_PROBES=3              # 每轮最多探测单元数
NMS_CLOSURE_TRACK_WINDOW_S=86400      # 确认后跟踪窗口
NMS_CLOSURE_WORKER_INTERVAL=120       # worker 扫描间隔
NMS_CLOSURE_FORGET_S=604800           # observed 单元保留时长
NMS_CLOSURE_MAX_UNITS=3000            # 单元总量上限（超限保留已确认+高权重）
```

Redis 键：`zops:alert_closure:units`（HASH）、`:cases`、`:events`（LIST，保留最近 1000 条）、
`:metrics`（HASH）、`:probe_lock`（探测互斥）。

---

## 四、判定规则（避免「假恢复」）

| 类型 | 恢复必须同时满足 |
| --- | --- |
| L1 证书 | 连续 3 次严格 TLS 健康（间隔 ≥60s、跨度 ≥120s、证据 ≤5min）+ NMS 明确清除（晚于人工完成、年龄 ≤ max(2×采集间隔, 300s)） |
| L1 备份/DNS/TCP/NTP | 连续 2 次探针健康（相邻 ≥120s、最近健康 ≤max(2×探测间隔,300s)）+ NMS 清除；主机无数据连续 3 次则回退簇级判据并标注 `probe_unavailable` |
| L2 设备簇 | 单元活跃告警清零 + 连续静默 1800s（维护窗口内不判定） |
| 任意 | ACK、告警从列表消失、旧快照、采集失败、字段缺失 —— **一律不算清除** |
| 任意 | 确认后 24h 未恢复 → `expired`（需人工 `reopen`，不会自动续跟踪） |

---

## 五、可观测性与验收

- 覆盖度：`GET /api/nms/closure-coverage`
  - 关注 `checklist_coverage`（预案覆盖率）、`by_tier`、`top_event_groups`、`recommendations`。
- 闭环运行：`GET /api/nms/closure/summary`（单元分布/跟踪数阈值/指标）、
  日志前缀 `[alert_closure]`，每个刷新周期一行：

```
[alert_closure] units=2021 tracked=0 recovered=0 expired=0 new=2021 dropped=0 empty=False probes=0/0
```

- 指标：`redis-cli HGETALL zops:alert_closure:metrics`
  - `probe_ok` / `probe_fail` / `probe_unavailable`：探针结果分布；`probe_rounds`：已探测轮次；
  - `recovered_total` / `expired_total` / `confirmed_total` / `cases_total`；
  - `last_refresh_at` / `last_units` / `last_empty`（采集为空标记）。
- 交付验收（本次实测）：
  - 首轮刷新 45,434 条告警 → 2,021 个单元，耗时约 4s；分层 L1 44 / L2 1,045 / L3 932。
  - L1 真实探测：`198.19.0.169` → `ok`（23 个作业全成功，streak=2）；
    `198.18.128.169` → `fail`（2 个作业失败，streak=0，拒绝恢复）；
    `198.18.16.222` → `unknown`（无该主机备份作业），保持等待。
  - 单测 176 项通过（SSL 122 + 闭环 54），含状态机边界、假恢复、维护窗口、空快照。
- 每个类型上线时都必须给出：单测（判定边界）+ 灰度清单（开关/日志/回滚）+ 一期真实数据对照。

---

## 六、运行手册（值班）

| 场景 | 动作 |
| --- | --- |
| 告警处置完，想让它自动核验关闭 | `POST /units/{key}/confirm`，填根因；之后自动判定，无需盯 |
| 确认错了 / 还在变更中 | `POST /units/{key}/pause`；变更完 `resume` |
| 已恢复但想继续盯 | `POST /units/{key}/reopen`（需新依据） |
| 想知道现在到底好没好 | `POST /units/{key}/recheck`（L1 真探测，L2 重算快照） |
| 计划维护 | 先 `POST /api/nms/suppress/maint` 写维护窗口，再确认；窗口内不会判恢复 |
| 误报太多想先停 | `NMS_CLOSURE_ENABLED=false` 重启 backend（数据保留，API 只读可用） |

---

## 七、决策记录与剩余事项

已决策（本轮）：

1. **维护窗口**：不依赖 CMDB，直接复用 `zops:nms_maint_windows`（`/api/nms/suppress/maint` 写入），
   与现有静音/抑制链路同源。
2. **P2 形态**：先 API + 飞书卡片（`notify:feishu` 频道，确认/恢复事件），前端列表后补。
3. **P1 顺序**：备份 → DNS → TCP → NTP，四类已全部落地（证书沿用 SSL 工作流）。

仍待输入（不阻断当前运行）：

1. **前端列表**：是否需要在 `/nms` 页面加「故障单元」页签（当前只有 API）。
2. **认证类伪告警分离**：与「NMS 全局会话失效」告警去重后再开自动核验，需确认判定信号。
3. **通知通道**：确认/恢复卡片现在发到 `notify:feishu` 默认通道，是否要按设备分组路由到不同群。

---

## 八、明确不做

- 不做「按告警条数批量闭环」：38,418 条里 91% 是同一批设备的协议告警，逐条闭环是负收益。
- 不做自动 ACK / 自动屏蔽 / 自动改配置。
- 不把「告警从列表消失」当作恢复证据（与 SSL 闭环同口径）。
- 不在维护窗口内判定恢复：计划维护用 `zops:nms_maint_windows` 标记，窗口内一律保持跟踪。
