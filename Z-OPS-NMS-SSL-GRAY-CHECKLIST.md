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
- 备份探针依赖 OPS-CORE-MGT 数据覆盖：该主机没有备份作业记录时返回 `unknown`，
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

---

## 十一、历史告警库的覆盖缺口：长期活跃事件“从历史里消失”

### 11.1 问题与结论

现象：`/nms` 的「历史告警」页里找不到正在响的告警（本次现场：12 个 SSL 证书告警仅 1 个在历史库中）。
历史数据本体未丢（`history_z`/`history_data` 一致、接口可查），缺的是**写入覆盖**与**老化口径**：

| 环节 | 原行为 | 后果 |
| --- | --- | --- |
| 写入 | 只有「本轮新增（Serial 不在 seen）」才入历史 | 上线前存量、以及摘要每日漂移换了内容键的事件（如 `ssl证书可用天数: 27` 倒数）永远补不进来 |
| 基线导入 | `_nms_bootstrap_history` 靠 `history_bootstrap:v1` 标记只跑一次 | 首次之后的新增覆盖缺口不会自愈，“修过一次又没了” |
| 老化 | `zremrangebyscore -inf (now-30d)` 只看首见时间 | 存活超 30 天但**仍在告警**的事件被当过期数据删除 |

修复（`agents/nms_agent.py`）：

- 新增 `_nms_repair_history`：**每轮幂等**以当前快照活跃事件为准补录缺失（`occurrences=1`，首见取告警 FirstTime，已有不覆盖），取代一次性基线导入；
- 新增 `_nms_trim_history`：老化与条数保险丝从写入路径移出，统一在修复尾部执行，并**豁免当前仍活跃的事件**；
- 历史写入/补录失败由 `logger.debug` 升为 `logger.warning`（不再静默吞掉）；
- `agents/base.py` Agent 共用 Redis 客户端连接池 `max_connections=10` → 可配 `REDIS_POOL_MAX`（默认 50）：
  日志中反复出现的 `心跳更新失败: Too many connections`（nms_agent 单日 108 次）是 redis-py 本地连接池耗尽，
  会使采集侧 Redis 写入静默失败，是历史缺口的放大器。

### 11.2 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `NMS_HISTORY_RETENTION_DAYS` | 30 | 保留期，按首见时间老化，**活跃事件豁免** |
| `NMS_HISTORY_ZSET_MAX` | 100000 | 条数保险丝，同样豁免活跃事件 |
| `NMS_HISTORY_REPAIR_MAX` | 8000 | 每轮覆盖修复单轮补写上限（缺口大时分轮消化，不阻塞采集） |
| `REDIS_POOL_MAX` | 50 | Agent 常驻进程 Redis 连接池上限 |

### 11.3 观测与回滚

```bash
# 覆盖率：快照活跃事件（内容键去重）与历史库交集，缺失应为 0~小量漂移
# 看日志即可，每轮一条：“历史覆盖修复: 快照活跃 N 个事件 → 补录 M 个 (缺失 M)” 与“历史老化清理… 活跃事件豁免”
grep -a "历史覆盖修复\|历史老化清理" logs/nms_agent.log | tail -4
redis-cli ZCARD zops:nms_alarms:history_z; redis-cli HLEN zops:nms_alarms:history_data
# 连接池耗尽是否消失
grep -c "Too many connections" logs/nms_agent.log
```

回滚：`git checkout -- agents/nms_agent.py agents/base.py config.py && systemctl restart 'zops-agent@nms_agent.service'`；
已补录的历史条目无需清理（只多了不应少的活跃事件记录）。

### 11.4 实测记录（2026-11-02，真实快照 42,310 条）

| 验证点 | 结果 |
| --- | --- |
| 修复前覆盖 | 快照活跃事件（内容键去重）36,235 个，其中 **19,040 个不在历史库**；12 个 SSL 证书告警 **11 个缺失** |
| 手动跑同一代码路径（5 pass，单轮上限 8000） | 共补录 19,040 条，`history_z` 66,493 → 85,533，缺失降为 **0** |
| 重启后自动轮次（采集侧真实代码） | `历史覆盖修复: 快照活跃 36242 个事件 → 补录 56 个 (缺失 56)`、`历史老化清理: 删除 20 条 (> 30 天, 活跃事件豁免)` —— 收敛后只剩漂移量 |
| SSL 证书告警在历史库 | 12 个全部命中，首见时间保留真实值（10/20 、 10/29 、 11/02） |
| 单元测试 | `tests/test_nms_analyzer.py` 全量通过（H3 改为“写入路径不老化 + 活跃豁免”、H4 重写为覆盖修复含幂等/单轮上限）；十一个 SSL/闭环/探针文件合并 180 项 OK |

### 11.5 边界（必须说明）

- 历史库排序与时间窗口筛选仍以**首见时间**为口径：长期活跃事件若首见在 30 天前，选“今天”窗口仍不会出现在首页，
  需拉长时间窗口或到「当前告警」页看活跃项（本次未改口径，避免历史页与告警发生时间语义不一致）。
- 「历史告警」页的关键字搜索是前端在本页已加载的 200 条内过滤（服务端无 `q` 参数），
  对稀疏事件（如 12 条 SSL）会“搜不到”，属于接口能力缺口，需单独评估后再动。
- 内容键含摘要文本，摘要内数值每日变化（证书倒数天数等）会产生新事件行；这是“一天一行”的真实语义，
  未做跨天归并（归并需屏蔽摘要数字，会与 IP/实例号撞键，风险大于收益）。
- `history_bootstrap:v1` / `history_z:fmt` 两个一次性标记键不再被读取，保留不影响行为，可手动删除。

## 十二、告警读路径加固：把「请求失败」与「确实没有告警」分开

### 12.1 症状与真实原因

用户两次反馈「历史告警又没了」「当前告警变成 暂无现存告警 / 将用您的 AD 账号登录 NMS 抓取」。核查结论：

| 核查项 | 结果 |
| --- | --- |
| 全局快照 | `zops:nms_alarms:latest` 存在且新鲜（12:15 轮次，42,800 条），admin 与 op1855 两条身份口径均返回 `filtered_total=43`（OPSCORE_use） |
| 历史库 | `history_z` 正常，`历史覆盖修复 / 历史老化清理` 日志按轮收敛 |
| 服务 | 12:23:51 全量重启（11 个 agent + backend + frontend，并触发一次 next 构建），12:24:37 拉起 |
| 结论 | **数据没丢**，是①重启窗口内前端请求失败被渲染成空态；②单次请求成本过高放大了失败面 |

真正需要修的三个缺陷：

1. **前端把读失败当「无告警」**：`fetchData` / `fetchHistory` 对非 2xx 与异常一律静默 `console.error`，
   空态分支于是显示「暂无现存告警」＋「将用您的 AD 账号登录 NMS 抓取」＋「用我的 AD 账号抓取」按钮，
   语义上把「我读不到」说成「没有告警」，这是「又没了」的直接来源。
2. **单请求成本**：快照本体 74MB，每次 `/api/nms/alarms`、`snapshot-meta`、`analysis`、`checklist` 都 GET 一次
   并在事件循环里 `json.loads`（实测 0.24s + 1.17s，同步阻塞整个 worker），多人轮询 + 探测并发即可把后端拖到超时。
3. **个人快照可能永久陈旧**：会话有效时读 `{prefix}:nms_alarms:user:{name}:latest`，
   该键一旦写下不再更新也不会失效（现场存在 2026-07-17 的个人快照）；
   页面按陈旧个人快照渲染，用户看到的就是「告警凭空变少/变空」。

### 12.2 改动

| 文件 | 改动 |
| --- | --- |
| `web-next/src/app/nms/page.tsx` | 新增 `loadError` 状态；`fetchData` / `fetchHistory` / `loadMore` 区分 HTTP 失败、后端 `{error}`、网络异常三种口径，**失败时保留上一次成功数据**；空态前插入「告警数据读取失败（不是没有告警）」分支并给「重试」按钮；`configured && latest` 全空时的提示改为按 `is_personal` 分别措辞，不再无条件诱导点「用我的 AD 账号抓取」 |
| `api/routers/agents.py` | 新增 `_nms_snapshot_ts` / `_nms_load_snapshot` / `_nms_read_base`：旁路小键 `latest:ts` 做新鲜度判定（禁止为取时间戳 GET 74MB）；进程内按 `(base, ts)` 缓存解析结果（上限 2 个 base，LRU 驱逐），解析走 `asyncio.to_thread`；`alarms` / `snapshot-meta` / `incidents` / `history` / `analysis` / `checklist` / `_nms_ssl_load_snapshot` 统一走 `_nms_read_base`，响应新增 `view_note` |
| `agents/nms_agent.py` | 写快照时同步写旁路键 `{base}:latest:ts`（API 侧对旧快照会一次性回填） |

回退口径（`_nms_read_base`）：个人快照缺失 → 用全局；个人快照比全局旧超过 3 个采集周期 → 用全局并给出 `view_note`；
个人快照不比全局旧（刚抓过） → 继续用个人视图；旧快照没有旁路时间戳（无法判定） → 保持原「个人优先」行为不误伤。
展示端点与 SSL 调查创建共用同一 base 口径，否则服务端重算的 `snapshot_ref` 会对不上（已有回归用例覆盖）。

### 12.3 观测

```bash
# 旁路时间戳（应与快照 timestamp 一致）
redis-cli GET zops:nms_alarms:latest:ts
redis-cli GET zops:nms_alarms:latest | head -c 120

# 读路径耗时：首次需解析，之后应命中缓存
curl -s -o /dev/null -w '%{time_total}\n' -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:7789/api/nms/alarms?page=1&page_size=5&status=active&groups=OPSCORE_use'

# 个人视图回退说明（正常全局视图应为空串）
curl -s -H "Authorization: Bearer $TOK" 'http://127.0.0.1:7789/api/nms/snapshot-meta' | python3 -c 'import json,sys;print(json.load(sys.stdin).get("view_note"))'
```

### 12.4 实测

| 验证点 | 结果 |
| --- | --- |
| 数据面 | `zops:nms_alarms:latest` 42,800 条（12:15 轮次）；OPSCORE_use `filtered_total=43`，`severity_counts={critical:4, major:31, minor:7, warning:1}` |
| 身份口径 | Basic(admin) 与 Bearer(op1855) 两条路径均 `is_personal=false`（服务账号 op1855 即 `NMS_USERNAME`，按设计走全局共享键） |
| 读路径提速 | 同一请求：首次（需解析 74MB）**1.74s** → 二次（命中缓存）**0.24s** |
| 旁路键 | API 已回填 `zops:nms_alarms:latest:ts`，采集侧改动重启后每轮随快照写入 |
| 浏览器 | `/nms` 当前告警 49 行、历史告警 200/98,755 条（11/04 13:34 → 11/05 12:22），无「暂无现存告警」「NMS 凭据未配置」「告警数据读取失败」文案，控制台 0 报错 |
| 单测 | 新增 `tests/test_nms_snapshot_view.py`（11 项：过期回退/新鲜保留/旧快照不误伤/缓存命中不再 GET/新 ts 重载/旁路键回填/缓存上限/SSL 与展示端点同 base）；与既有 10 个 NMS/闭环/探针文件合并 **191 项 OK** |
| 构建 | `npx tsc --noEmit` 通过，`npm run build` 通过；backend / nms_agent / frontend 均 active，重启后日志无新增 Traceback |

### 12.5 边界与遗留

- 缓存只在进程内、按 `(base, ts)` 失效，多 worker 部署时每个 worker 各自一份（当前单进程，成立）。
  若将来改成多 uvicorn worker，需要把上限与内存占用重新核算。
- 重启期间（本次约 45s：agent SIGTERM 退出慢 + next 构建）前端仍会短暂读失败，现在会明确显示「读取失败 + 重试」，
  不再谎报「没有告警」；但**服务重启本身仍是这类「数据消失」错觉的主要来源**，后续应把 agent 的 SIGTERM 退出耗时压下来。
- 个人视图的「过期回退全局」是展示层兜底，不改变权限语义：个人快照仍按用户键独立存储，
  回退时页面拿到的是全局共享（服务账号）范围数据，`is_personal` 会变为 `false`，前端不应再声称「仅显示你权限范围内的告警」。
- 「历史告警」页关键字搜索仍是前端在本页 200 条内过滤（服务端无 `q` 参数），未在本次处理。

## 十三、把第十二节遗留的两项坐实：Agent 秒级优雅退出 + 历史告警服务端全库搜索

第十二节 12.5 明确留了两项：①服务重启本身仍是「数据消失」错觉的主要来源，应压 agent 的 SIGTERM 退出耗时；
②「历史告警」页关键字搜索只在前端本页 200 条里过滤，服务端无 `q` 参数。本次全部推进落地。

### 13.1 Agent 优雅退出（把重启窗口从 ~45s 压到亚秒）

真因：`agents/base.py run_agent` 旧实现用 `signal.signal(SIGTERM, handler)`，handler 里
`loop.create_task(agent.stop())`。信号处理函数在主线程同步执行，而此刻事件循环正阻塞在
`epoll wait`，新任务没人调度 → `stop()` 根本不跑；`agent.stop()` 又只把 `_running=False`，
卡死在 `pubsub.listen()`（无消息时永久阻塞）与采集轮次里的任务不会被取消。于是每个 agent
都要等 systemd `TimeoutStopSec=45` 到点 SIGKILL，journal 里可见连带追杀 playwright 的
`V8Worker / libuv-worker / SignalInspector` 一排子进程。nms_agent 重启期间 `/api/nms/*` 全失败，
前端就表现为「告警又没了」。

修复（三处协同）：
- `run_agent`：改用 `loop.add_signal_handler(SIGTERM/SIGINT, ...)`（经 self-pipe 唤醒事件循环，回调必然被调度），
  回调里编排 `_graceful_shutdown`；第二次信号直接取消全部任务并停循环；每轮 `run_until_complete` 前重装 handler
  （asyncio 在 run 结束时会拆掉旧式 handler），保证连续重启都有界。
- `BaseAgent._graceful_shutdown`：置停止标志 → 给业务 `grace=6s` 自行收尾 → 超时才 `cancel` 残留任务并
  `gather(return_exceptions=True)` → `wait_for(_cleanup(), 4s)` → `loop.stop()`。
- `BaseAgent.start` 消息循环：`async for pubsub.listen()`（无消息无限阻塞）改为 `get_message(timeout=1.0)`
  有界轮询，`_running=False` 后 ≤1s 退出；退出对 `unsubscribe/aclose` 各自 try/except，绝不让退出卡死。
- `nms_agent`：监控首轮等待 30s 拆成 30×1s 可中断；`_nms_wait_next_round` 分段从 30s 降到 1s（SIGTERM 后 ≤1s 退出，
  同时采集间隔热生效更快）；`_cleanup` 关浏览器/playwright 各加 `wait_for(timeout=3)`，避免与 node 子进程通信卡住拖到 SIGKILL。
- `deploy/zops-agent@.service`（并同步 /etc）：`TimeoutStopSec` 45 → 15（新退出路径 ≤11s，15s 仅作真卡死兜底）。

### 13.2 历史告警服务端全库关键字搜索

`nms_history_read` 新增 `q` 参数：在 `history_z` 全库分批扫描时，先在**原始字节**上做子串匹配（多词空格=AND），
命中才 `json.loads` 并用 `_match()` 复核分组/关键字，未命中不解码——避免整库上百 MB 解码。
无关键字仍走原 rank 分页快路径。`/api/nms/history` 透传 `q` 并回 `q_partial`（带关键字且未 `has_more` = 已在扫描上限内扫完，
不代表全库无更多命中）。个人视图带关键字时不做「空则回退全局」二次扫描（尊重用户权限边界）。
前端 `page.tsx`：历史页签关键字改由服务端过滤（`fetchHistory` 带 `q`，输入防抖 500ms 重查第一页），
客户端 `filteredAlarms` 对历史页不再重复用 `searchText` 过滤（口径不同会漏合法命中）；空态按是否有关键字分别措辞
（「未找到匹配“X”的历史告警」而非误导的「暂无现存告警」）。

### 13.3 实测（2026-11-05，服务账号 op1855=全局口径）

| 验证点 | 结果 |
| --- | --- |
| 优雅退出（旧代码进程） | `systemctl stop` = **15.25s**，`Failed with result 'timeout'` + 追杀 `V8Worker/libuv-worker` 一排（改造前形态） |
| 优雅退出（新代码，空闲） | `systemctl stop` = **0.59s**，`Deactivated successfully`，无 SIGKILL |
| 优雅退出（新代码，采集轮次进行中且浏览器已开） | `systemctl stop` = **0.35s**，`Deactivated successfully`，`playwright/driver/node` + `chrome-headless` 子进程**孤儿数=0**（Python 进程内已随 `_cleanup` 收干净，systemd 无需追杀） |
| 历史搜索 `q=link` | HTTP 200，命中含 `SNMP_TRAP_LINK_DOWN / Link Down / uplink` 等；`has_more=true` |
| 历史搜索 `q=23506`（早期稀有 token） | HTTP 200，命中（10/26 事件），证明跨全库、非仅首页 200 条 |
| 中文关键字 `q=规则编号` | UTF-8 字节匹配生效，命中 wazuh 事件；多词 `q=SNMP_TRAP link` 按 AND 命中 |
| 搜索成本 | `limit=200` 带 q：**0.16s**；无 q 快路径：0.07s |
| 读路径 | `/api/nms/alarms?groups=OPSCORE_use` 重启后首请求 1.60s（进程内缓存冷）→ `filtered_total=45`，`is_personal=false`，`view_note=''` |
| 浏览器 | `/nms` 当前告警 45 行（无「暂无现存告警」/「用我的 AD 账号抓取」/「读取失败」文案）；历史 `link` 命中 21 条、时间追溯至 10/21（远超首页），控制台 0 报错 |
| 单测 | 新增 `tests/test_nms_history_search_and_shutdown.py`（10 项：跨页命中/多词 AND/无命中/大小写不敏感/分组+关键字/字节预筛选不整库解析/空 q 走快路径；run_agent 用 add_signal_handler 且信号回调编排 _graceful_shutdown；宽限期到取消残留任务并清理）；与 12 个 NMS/闭环/探针文件合并 **212 项 OK** |
| 构建 | `npx tsc --noEmit` 通过，`npm run build` 通过；backend / nms_agent / frontend 均 active |

### 13.4 边界

- 优雅退出把「重启即数据消失」的错觉窗口从 ~45s 压到亚秒，但采集本身仍有周期（默认 900s）：
  刚重启的首轮抓取前 `snapshot-meta` 仍可能 `pending=true`，前端已明确区分 pending（正常等待）与读取失败（可重试）。
- 服务端 `q` 走全库分批扫描，命中稀疏时受 `scan_cap=6 万` 限制：极老且极稀有的关键字可能只覆盖扫描窗口内的命中，
  前端用 `q_partial` 文案提示「可清空搜索或放宽时间范围」，不谎报「无匹配」。
- 全库关键字匹配按「序列化整条记录」子串命中（含字段名与聚合 raw 原文），因此数字类关键字可能命中聚合事件的子字段
  （如 23506 命中一条已合并 wazuh 子事件的 windows 记录），这是全库搜索的预期语义，不是 bug。
- 多 uvicorn worker 部署时进程内快照缓存每 worker 一份，退出编排不受影响（信号是进程级）。
