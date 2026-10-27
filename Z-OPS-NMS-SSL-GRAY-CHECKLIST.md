# NMS SSL 调查闭环 — 灰度上线检查清单

> 适用：生产环境小范围启用 SSL 证书调查闭环（手动启动 → 只读取证 → 人工处置 → 自动恢复核验 → 案例审核）。
> 边界：只读取证，不自动 ACK / 屏蔽 / 换证书 / 调 LLM / 改生产配置；恢复必须同时满足「TLS 严格实测健康」与「NMS 明确清除原告警」。
> 本文只用于启用前评估与回滚，不替代变更审批。
> 其他告警类型的推进顺序（L1/L2/L3 与覆盖度数据）见 `Z-OPS-Alert-Closure-Roadmap.md`。

---

## 一、本次可观测性改动（灰度评估依据）

| 文件 | 改动 |
| --- | --- |
| `core/nms_ssl_workflow.py` | 进程内指标：租约续租尝试/失败、丢租、丢弃探测；探测轮次/耗时/unknown 比例；采集轮次/异常/空快照与异常率；调度周期/到期量/抢锁失败/槽满；`waiting_nms` 进入/退出/停留时长；恢复耗时（`manual_completed_at → recovered_at`）；`[nms_ssl_metrics]` 汇总日志；采集计数 Redis 跨进程聚合与只读报告 `metrics_report()` |
| `agents/nms_agent.py` | 每轮采集后输出 `[nms_ssl_metrics] collection ok\|error scope=... rounds=... errors=... recent_error_rate=...`，同时写入 Redis HASH `zops:nms:ssl:metrics`（采集进程与 backend 进程隔离，靠 Redis 汇总） |
| `config.py` / `zops.env.example` | 新增 `NMS_SSL_METRICS_LOG_INTERVAL_S`（默认 300 秒，`<=0` 关闭周期汇总） |
| `api/routers/agents.py` | 新增只读接口 `GET /api/nms/ssl-metrics`（`verify_token` + `require_admin`，不返回告警内容与凭据） |
| `tests/test_nms_ssl_metrics.py` / `tests/test_nms_ssl_agent_metrics.py` / `tests/test_nms_ssl_api.py` | 19 项指标单测（续租失败、异常率窗口、探测耗时、调度饱和、跨进程聚合、停留时长、恢复耗时、汇总节流）+ 4 项 agent 采集指标测试（写 Redis、空快照、异常、Redis 故障隔离）+ 3 项接口测试（管理员可读、Redis 合并、非管理员 403） |

指标为**计数**，进程重启归零（跨进程采集计数在 Redis 中，agent 重启后继续累加）；业务记录（调查/证据/案例）仍在 Redis 且不设 TTL，不受影响。

> 进程分工：调度 worker 在 `zops-backend`，NMS 采集在 `zops-agent@nms_agent`。
> 采集计数由 agent 写入 Redis `zops:nms:ssl:metrics`，backend 的汇总行与 `/api/nms/ssl-metrics` 会合并显示；
> 租约 / 探测 / 调度 / `waiting_nms` / 恢复耗时只在本进程（backend）统计，重启归零。

---

## 二、开关配置（灰度建议值）

| 变量 | 默认 | 灰度建议 | 作用 |
| --- | --- | --- | --- |
| `NMS_SSL_WORKFLOW_ENABLED` | `true` | `true`（回滚时 `false`） | 总开关；关闭后禁止新建调查与后台调度，历史记录只读 |
| `NMS_SSL_MAX_ACTIVE_PER_USER` | `10` | `1`～`2` | 单用户同时跟踪上限，控制影响面 |
| `NMS_SSL_GLOBAL_CONCURRENCY` | `2` | `1` | 全局并发探测槽，限制出口流量 |
| `NMS_SSL_CHECK_INTERVAL` | `60` | 保持 `60` | 复核间隔；同时决定清除证据最大年龄 `max(2×interval, 300s)`，灰度期不要拉长 |
| `NMS_SSL_CONSECUTIVE_HEALTHY` | `3` | 保持 `3` | 连续健康次数；放宽等于降低恢复门槛 |
| `NMS_SSL_HEALTHY_MIN_SPACING_S` | `60` | 保持 `60` | 相邻两次健康最小间隔 |
| `NMS_SSL_HEALTHY_MIN_SPAN_S` | `120` | 保持 `120` | 三次健康最小跨度 |
| `NMS_SSL_TRACK_WINDOW_S` | `86400` | 保持 `86400` | 跟踪窗口；到期自动暂停但保留历史 |
| `NMS_SSL_MIN_DAYS` | `30` | 保持 `30` | 恢复要求的最小剩余有效期（按秒比较） |
| `NMS_SSL_ALLOWED_PORTS` | `443` | 保持 `443` | 服务端强制端口白名单 |
| `NMS_SSL_CA_FILE` | 空 | 空（用系统信任库） | 额外 CA；留空时按 `配置 > certifi > /etc/ssl/certs/ca-certificates.crt` 解析 |
| `NMS_SSL_METRICS_LOG_INTERVAL_S` | `300` | `300`（量级大可调 `600`） | 指标汇总日志间隔；`0` 关闭汇总，逐次采集/租约失败日志仍保留 |

租约 `60s`、槽位 `90s`、每 `15s` 续租是代码常量，不是环境变量，不需要也无法通过 env 调整。

> 灰度值已写入 `zops.env`（单用户 2 条、全局并发 1），备份见 `zops.env.bak-gray-202610242358`；
> 当前生效值可用 `tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value zops-backend)/environ | grep NMS_SSL_` 核对。

---

## 三、启用前只读核验

```bash
# 1. 服务与路由
systemctl is-active zops-backend zops-frontend nginx zops-agent@nms_agent
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/nms                          # 期望 200（经 nginx 的前端页面）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/nms                         # 期望 200（直连前端）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7789/api/nms/ssl-investigations # 期望 401（未认证）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7789/api/nms/ssl-metrics        # 期望 401（未认证）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7789/api/nms/closure-coverage   # 期望 401（未认证）
# 注意：7789 是 API 服务，访问 /nms 会 404；页面入口是 nginx 443 或前端 3000

# 2. Redis 持久化（业务记录不可丢）
redis-cli CONFIG GET appendonly   # 期望 yes
redis-cli CONFIG GET save         # 期望含 900 1 / 300 10 / 60 10000
redis-cli CONFIG GET dir

# 3. 信任库
python3 -c "import certifi,os;print(os.path.exists('/etc/ssl/certs/ca-certificates.crt'))"

# 4. 端口监听
ss -lntp | grep -E ':(443|3000|7789)\b'
```

启用后确认启动日志（应用日志写文件，不在 journald）：

```bash
# backend：调度 worker 启动 + 指标汇总
tail -n 200 /opt/zops/logs/webchat_stderr.log | grep -E "SSL 调查后台已启动|nms_ssl_metrics"

# nms_agent：采集计数
tail -n 100 /opt/zops/logs/nms_agent.log | grep "nms_ssl_metrics"
```

管理员只读指标接口（需系统管理员会话 Cookie）：

```bash
curl -s -b "<admin cookie>" http://127.0.0.1:7789/api/nms/ssl-metrics | python3 -m json.tool
# 关注：metrics.lease_renew_failures / probe_unknown_rate / worker_due_total / slot_full
#       collection_remote.collection_rounds、error_rate（来自 nms_agent）
```

---

## 四、启用步骤（建议 10 分钟内完成）

1. 备份 `zops.env`，写入灰度值（至少改并发与单用户上限）。
2. `sudo systemctl restart zops-backend`，`sudo systemctl restart zops-agent@nms_agent`（两边都在启动时读取 env）。
3. 核对第三节启动日志与路由状态。
4. 用 1 个白名单账号创建 1 条调查，走完：首次取证 → 人工处置标记 → `waiting_nms`，观察 1 个采集周期。
5. 无异常后再扩到 3 个账号；本轮**不做自动恢复验证之外的写操作**，案例审核仅管理员执行。

---

## 五、日志观测点与判读

| 日志行 | 来源 | 含义 | 判读 |
| --- | --- | --- | --- |
| `[nms_ssl_metrics] summary ...` | backend | 每 300s 一次汇总；下一周期无采集/探测/到期/等待/恢复等活动时静默 | 看租约、探测、采集、调度、等待、恢复六组指标 |
| `[nms_ssl_metrics] probe 慢探测 id=... elapsed=...s avg=...` | backend | 单轮 TLS 取证耗时 ≥10s（租约 60s） | 偶发可容忍；连续出现说明目标侧慢或网络抖动 |
| `[nms_ssl_metrics] 租约续租失败 id=... 累计失败=N 次` | backend | 15s 心跳续租失败，随后立即终止探测 | 任何一条都要查 Redis；出现后该调查本轮不提交 |
| `[nms_ssl_workflow] 调查 ... 租约丢失，终止探测且不提交 (累计丢租=..., 丢弃探测=...)` | backend | 围栏令牌丢失，旧响应已丢弃 | 与上一条成对出现，说明 Redis 抖动或阻塞 |
| `[nms_ssl_metrics] 提交前租约失效 id=... 累计失败=N 次` | backend | 探测完成后令牌已过期，结果丢弃 | 同上；频繁出现说明探测耗时接近/超过 60s 租约 |
| `[nms_ssl_metrics] collection ok\|error scope=... rounds=... errors=... recent_error_rate=...` | nms_agent | 每轮采集结果与异常率（最近 50 轮窗口） | `error` 行必看 `last_error` |
| `[nms_ssl_metrics] waiting_nms 开始 id=... streak=...` | backend | 三次严格健康通过，进入等待清除证据 | 与下一次采集间隔对比 |
| `[nms_ssl_metrics] waiting_nms 结束 id=... outcome=recovered\|regressed\|paused\|track_window_expired dwell=...` | backend | 离开等待区的停留时长 | 关注 `regressed` 比例与 `dwell` 是否远超采集周期 |
| `[nms_ssl_metrics] recovered id=... recovery=...s avg=... max=...` | backend | 人工完成到恢复的总耗时 | 与人工处置时间对照 |
| `[nms_ssl_workflow] 调查 ... 保存冲突 / 复核失败` | backend | CAS 冲突或探测异常（既有日志） | 冲突可接受；复核失败需看堆栈 |
| `[nms_ssl_workflow] 采集观察失败 / 采集异常记录失败（不阻断采集）` | 双方 | 观测器自身异常 | 不影响调查状态，但需排查存储 |

### 指标口径

| 指标 | 计算 | 期望 |
| --- | --- | --- |
| 租约续租失败率 | `lease_failures / lease_attempts` | `0`；>0 即异常 |
| 探测耗时 / unknown 率 | `probe_avg`、`probe_max`；`probe_unknown_rate = probe_unknown / probe_rounds` | `probe_max ≲ 40s`（总超时）；`probe_unknown_rate` ≤20%；>50% 先查 NMS 侧目标可达性 |
| 调度饱和度 | `worker_due_total / worker_cycles`（每周期到期量）、`lease_busy`（抢锁失败）、`slot_full`（并发槽满） | `lease_busy`/`slot_full` 长期为 0；持续 >0 说明并发偏小或探测变慢 |
| 采集异常率 | `collection_errors / collection_rounds`；`recent_error_rate` 为最近 50 轮窗口（跨进程来自 Redis） | ≤10% 正常；10%～30% 观察；>30% 停止放量 |
| waiting_nms 停留 | 离开时 `dwell`（avg/max）；`waiting_pending_max` 为仍在等待的最长时长 | 中位数约一个采集周期（默认 10～30 分钟）；`pending_max > 2×采集周期` 需查清除证据是否持续推进 |
| 恢复耗时 | `recovered_at - manual_completed_at`（avg/max） | 约「采集周期 + ≥120s 三次核验」；>2h 且 NMS 已清除需查 due 调度与状态机 |

### 从日志快速估算异常率

```bash
BACKEND_LOG=/opt/zops/logs/webchat_stderr.log
AGENT_LOG=/opt/zops/logs/nms_agent.log

# 最近两小时的采集轮次与异常数（agent 进程）
grep -c "\[nms_ssl_metrics\] collection ok" "$AGENT_LOG"
grep -c "\[nms_ssl_metrics\] collection error" "$AGENT_LOG"

# 租约失败、丢租与慢探测（backend 进程）
grep -c "续租失败\|租约丢失" "$BACKEND_LOG"
grep -c "\[nms_ssl_metrics\] probe 慢探测" "$BACKEND_LOG"

# 最近一次汇总行
grep "\[nms_ssl_metrics\] summary" "$BACKEND_LOG" | tail -1
```

日志轮转后需按时间范围过滤时，用 `awk '$1" "$2 >= "2026-10-24 00:00"' "$BACKEND_LOG"` 或按偏移量截取，不依赖 journald。

---

## 六、灰度节奏与停止条件

| 阶段 | 范围 | 观察时长 | 进入下一阶段条件 |
| --- | --- | --- | --- |
| G1 | 1 个用户 / 1 条调查 | ≥2 个采集周期 | 无丢租；采集异常率 0；无保存冲突异常堆积 |
| G2 | 3～5 个用户，并发 1 | ≥1 天 | 采集异常率 ≤10%；`waiting_nms` 能正常进入/退出；无重复恢复 |
| G3 | 10% 用户，并发 2 | ≥3 天 | 恢复耗时中位数符合预期；案例审核无自审/越权 |
| 全量 | 全部用户 | — | G3 指标达标且无 P1 问题 |

**立即停止并回滚**：任一调查在 NMS 未明确清除时进入 `recovered`；`lease_lost` 连续出现；采集异常率连续 3 轮 >30%；恢复耗时出现负值/异常大值；前端出现 5xx。

---

## 七、回滚方式

### 7.1 配置回滚（首选，保留全部数据）

```bash
# 1) zops.env 置为关闭
#    NMS_SSL_WORKFLOW_ENABLED=false
# 2) 两个进程都要重启（env 在启动时读取）
sudo systemctl restart zops-backend
sudo systemctl restart zops-agent@nms_agent
```

生效表现：

- 新建调查返回 `503 workflow_disabled`（前端提示「SSL 调查闭环已停用」）；
- 后台 worker 仍打印启动日志，但 `run_due_once` 直接返回 0，不再探测/调度；
- 采集侧观察器与异常记录直接 no-op，不影响 NMS 采集主流程；
- 历史调查、证据、案例**全部保留可只读查看**，`zops:nms:ssl:*` 不删除。

验证：

```bash
tail -n 200 /opt/zops/logs/webchat_stderr.log | grep -c "\[nms_ssl_metrics\] 租约"   # 重启后应无新增
redis-cli HLEN zops:nms:ssl:inv                                                          # 应与回滚前一致（>0 说明数据保留）
redis-cli HGETALL zops:nms:ssl:metrics                                                   # 跨进程采集计数保留，可作历史对照
```

### 7.2 完全停止（极端情况）

```bash
sudo systemctl stop zops-agent@nms_agent     # 仅停采集；backend 仍可只读查询
sudo systemctl stop zops-backend             # 影响全站，非必要不做
```

### 7.3 代码回滚

- 本次改动集中在 `core/nms_ssl_workflow.py`、`agents/nms_agent.py`、`config.py`、`zops.env.example` 与新增测试，可按版本回退后重新构建/重启；
- 回退代码前先保留 Redis 数据；不要在回退脚本中清理 `zops:nms:ssl:*`。

### 7.4 回滚后确认

1. 前端 `/nms` 可见历史调查与证据，但无法新建；
2. 日志不再出现新的 `waiting_nms`/`recovered`/租约日志（`/opt/zops/logs/webchat_stderr.log`）；
3. `redis-cli --scan --pattern 'zops:nms:ssl:lease:*'` 为空（租约 60s 自动过期），`zops:nms:ssl:due` 不再增长。

---

## 八、已知边界（评估时必须说明）

- 指标为进程内统计，**重启归零**，不用于长期趋势；采集计数额外写在 Redis `zops:nms:ssl:metrics`（无 TTL，可用于跨重启对照）。
- 浏览器闭环验收使用**隔离数据 + 模拟清除证据**，证明的是判定与展示链路，**不代表生产环境已发生真实恢复**。
- 采集计数跨进程（Redis），但租约/探测/调度/等待/恢复只来自 `zops-backend` 进程；若 backend 多副本部署，`/api/nms/ssl-metrics` 只反映收到请求的那个副本，汇总以各副本日志为准。
- `zops:nms:ssl:metrics` 不会自动清理；若需重置口径，`redis-cli DEL` 后从 0 累加（不影响业务记录）。
- 灰度期间不要放宽 `MIN_DAYS` / 连续健康次数 / 健康跨度，也不要缩短 `CHECK_INTERVAL` 来「加快恢复」。

---

## 九、通用告警闭环（L1/L2）灰度要点

> 与 SSL 调查闭环**并行运行、互不影响**：SSL 有自己的状态机与 Redis 键；
> 通用闭环负责「设备级故障单元 + 备份/DNS/TCP/NTP 探针」，实现见
> `core/alert_closure_engine.py`、`core/alert_probes.py`，路线见 `Z-OPS-Alert-Closure-Roadmap.md`。

### 9.1 开关与灰度值

- 默认即启用，但**不会自动跟踪任何单元**：只有人工 `confirm` 后才进入恢复核验。
- 关闭：`NMS_CLOSURE_ENABLED=false` 后重启 backend；数据保留，`GET /api/nms/closure/summary` 仍可读。
- 建议先按默认阈值运行：`QUIET_S=1800`、`L1_HEALTHY=2`、`L1_MIN_SPACING_S=120`、
  `PROBE_UNKNOWN_LIMIT=3`、`PROBE_INTERVAL=300`、`TRACK_WINDOW_S=86400`、`MAX_PROBES=3`。

```bash
systemctl restart zops-backend
tr '\0' '\n' < /proc/$(pgrep -f 'webchat_api' | head -1)/environ | grep NMS_CLOSURE   # 生效值
```

### 9.2 观测点

```bash
grep -c '\[alert_closure\]' /opt/zops/logs/webchat_stderr.log        # 每 120s 一行刷新摘要
grep '\[alert_closure\]' /opt/zops/logs/webchat_stderr.log | tail -3
redis-cli HLEN zops:alert_closure:units                                        # 单元数（当前约 2021）
redis-cli HGETALL zops:alert_closure:metrics                                   # probe_ok/fail/unavailable、recovered_total
redis-cli LLEN zops:alert_closure:events                                       # 状态事件（保留最近 1000）
curl -sk https://127.0.0.1/api/nms/closure/summary                            # 无 Token 期望 401
```

- 日志格式：`[alert_closure] units=… tracked=… recovered=… expired=… new=… dropped=… empty=… probes=x/y`。
- `empty=True` 表示本轮快照为空：此时**不会**推进任何恢复，属预期保护行为。
- L1 探针失败（`probe_fail`）与无法取证（`probe_unavailable`）要分开看：后者不会误判健康。

### 9.3 回滚

```bash
# 1) 软回滚：停止刷新/探测，保留数据与只读查询
echo 'NMS_CLOSURE_ENABLED=false' >> /opt/zops/zops.env
sudo systemctl restart zops-backend
# 2) 确认：日志不再出现新的 [alert_closure] units= 行；已有单元仍可 GET 查询
```

- 不要 `DEL zops:alert_closure:*`：已确认单元与案例是审计记录；如需重置请先导出
  `redis-cli --raw HGETALL zops:alert_closure:units > closure-units.bak.json`。
- 代码回退涉及 `core/alert_closure_engine.py`、`core/alert_probes.py`、`api/routers/agents.py`、
  `webchat_api.py`（worker 启动/关闭）、`config.py`；回退后 backend 不再启动闭环 worker。

### 9.4 已知边界

- 单元聚合按 `kind:source`，同一设备的同类告警合成一个单元；`source` 为空时归为 `unknown`。
- 备份探针依赖 GCOC-MGT 数据覆盖：该主机没有备份作业记录时返回 `unknown`，
  连续 3 次后回退簇级判据，并在 `recovery_evidence` 明确写 `probe_unavailable`。
- 维护窗口复用 `zops:nms_maint_windows`：窗口内既不判恢复，也不清零静默计时。
- 通知走 `notify:feishu` 默认通道，只发「确认/恢复」两类事件，不发送告警原文全文。
- 未做自动 ACK/屏蔽；`confirm` 只改闭环状态，不影响 NMS。

## 十、非 443 端口的闭环：端口白名单与人工核验模式

### 10.1 问题与结论

原实现要求 `fields.Summary` 里的 position 端口必须在 `NMS_SSL_ALLOWED_PORTS`（默认 `[443]`）内，
否则创建调查直接 422 `invalid_target`，前端只能「关闭」——1280/8443 这类真实 TLS 管理口永远无法闭环。
现在按「能取证就严格取证，不能取证也要有可核验的闭环」处理：

| 目标端口 | 模式 `verification` | 是否发起 TLS 探测 | 恢复判据 |
| --- | --- | --- | --- |
| 在 `NMS_SSL_ALLOWED_PORTS` 内 | `tls` | 是 | TLS 连续 3 次严格健康 + NMS 明确清除 |
| 不在白名单（目标仍唯一合法） | `manual` | 否（`_run_one` 直接跳过） | 人工处置完成 + NMS 清除持续 ≥ `NMS_SSL_MANUAL_QUIET_S`（默认 1800s）+ 无同身份活跃替代 |
| 多目标 / URI / 凭据 / IP 字面量 / 缺字段 | — | 否 | 仍 422 `invalid_target`（无法确定唯一目标，不得猜） |

人工核验模式**不以证书健康作为证据**（`recovery.tls_ok = null`），案例 `evidence.verification = "manual"`，
`verification_result = "manual_clear_quiet"`；不会把「NMS 清除」写成「证书已验证」。

### 10.2 配置

```bash
# 能自动取证的端口（逗号分隔）；本环境真实 TLS 管理口为 1280/8443
NMS_SSL_ALLOWED_PORTS=443,1280,8443
# 人工核验模式的静默判定时长（秒）
NMS_SSL_MANUAL_QUIET_S=1800
```

- 改白名单只影响**新建**调查；已创建调查的 `verification` 落库不变。
- 缩小白名单即可把某些端口降级为人工核验；扩大白名单让新调查走严格取证。

### 10.3 API 字段

- 列表 `GET /api/nms/ssl-investigations`：每项新增 `verification`。
- 详情 `GET /api/nms/ssl-investigations/{id}`：
  - `verification` / `verification_note`（原始拒绝原因，如「端口 1280 不在允许取证范围 [443]」）；
  - `recovery.mode = "manual"`，`recovery.needs = ["manual_action", "nms_clear_quiet"]`，
    `recovery.tls_ok = null`，`recovery.manual_clear_first_at`、`recovery.manual_quiet_s`。
- `POST /{id}/recheck` 对 manual 返回 409 `manual_verification`（恢复由采集侧静默计时推进，不做单次探测）。
- `POST /{id}/manual-completed` 对 manual 直接把状态推进到 `waiting_nms`，不进入探测调度。

### 10.4 观测与回滚

```bash
# 调查模式分布（调查存于 hash：zops:nms:ssl:inv，field=inv_id）
redis-cli HGETALL zops:nms:ssl:inv | awk 'NR%2==0' | python3 -c 'import sys,json
for line in sys.stdin:
    d = json.loads(line); print(d.get("verification") or "tls", d["status"], d["target"])'
# 日志：人工核验模式不产生 probe 行，只会有 clear_streak_start / clear_reset / recovered
sudo grep -E 'clear_streak_start|clear_reset|manual_verification' /opt/zops/logs/webchat_stderr.log | tail
```

- 回滚：代码回退 `agents/ssl_check.py`（新增 `extract_target_lenient`）、`core/nms_ssl_workflow.py`、
  `web-next/src/components/NmsSslInvestigation.tsx`；
  历史 manual 调查仍可只读查看，只是不再推进（`verification` 字段保持原值）。
- 回滚后新创建的非 443 调查会重新返回 422，这是旧行为，不是新故障。

### 10.5 实测记录（2026-10-25，真实快照 45,086 条）

| 场景 | 结果 |
| --- | --- |
| `a2cweb-hkctt.hk.net:1280`（白名单含 1280） | 202，`verification=tls`，12s 后首次取证 `unhealthy/expiring_soon`（1 张证书），进入 `awaiting_action` |
| `a2cweb-shcjx.example.com:1280`（临时把白名单收回 `443`） | 202（不再 422），`verification=manual`，`status=awaiting_action`，`recovery.mode=manual`，`recheck` 返回 409 `manual_verification` |
| 单元测试 | `tests.test_nms_ssl_workflow` 新增 13 项 manual 用例（创建/探测跳过/静默窗口/活跃替代重置/清除行消失/空快照冻结/暂停恢复/案例证据），全量 180 项通过 |

### 10.6 边界（必须说明）

- 人工核验模式**不产生任何 TLS 证据**，恢复只代表「人工处置后 NMS 不再报」；根因未被证明。
- 告警从列表消失、ACK、旧快照、采集失败一律不算清除；空快照不推进恢复。
- 清除必须晚于人工处置完成时间，且不能存在同身份活跃替代记录；
- 静默计时只在**非空快照**中推进（空快照冻结）；清除行自然消失（NMS 保留期到期、无活跃替代）不重置计时，
  但重新出现同身份活跃记录会立即重置。
- 调查与案例存储在独立键空间，**告警消失后仍可只读查看**，不受当前快照影响。

