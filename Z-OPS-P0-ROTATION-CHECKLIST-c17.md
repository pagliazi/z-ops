# Z-OPS P0 凭据轮换清单（管理员执行）— task-c17 / 2026-10-13

按 SPEC 红线：本轮**封闭暴露、不回显、不擅自轮换**。以下凭据存在历史暴露面，建议管理员按变更流程逐一轮换。**本文档不含任何密钥值。**

## 暴露面

1. `zops.env.bak-20260805`（2026-08-05 备份）今日前为全局可读（644，含 13+ 项真实凭据）——已收紧为 600，**内容未改动、未删除**。备份存续期间本机任意本地用户可读。
2. GET `/api/llm/config` 在本修复前向**任意登录用户**回显原始 LLM 密钥（api_key/anthropic_key/oneapi_key，存量值存于 Redis `zops:llm_pref`）——现已改为管理员 + `__KEEP__` 掩码。
3. `/api/webhook/n8n_callback` 修复前无鉴权（影响的是回调真实性，不直接泄密）。

## 建议轮换项（优先级从高到低）

| 项 | 位置 | 原因 |
|---|---|---|
| `JWT_SECRET` | zops.env | 已知值可铸造任意用户 token（认证根） |
| `WEBCHAT_AUTH_PASS` | zops.env | 本地认证口令 |
| `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `ONEAPI_API_KEY` | zops.env + Redis `zops:llm_pref` | 经 /api/llm/config 对所有登录用户暴露过；轮换后需同步更新管理页 LLM 配置 |
| `VCENTER_PASS` / `PVE_TOKEN_SECRET` | zops.env | 基础设施写凭据 |
| `GCOC_INFO_PASS` / `GCOC_MGT_PASS` / `NMS_PASSWORD` | zops.env | NMS 密码此前已标记待更新；P0 起 NMS 周期任务只认服务主体 |
| `KB_API_TOKEN` / `OUTLINE_API_TOKEN` / `HA_API_TOKEN` | zops.env | 第三方系统凭据 |
| `LDAP_BIND_PASSWORD` | zops.env | AD 绑定账号 |
| `TELEGRAM_BOT_TOKEN` / `WECOM_SECRET` | zops.env | 通知渠道凭据 |

轮换操作：管理员更新 zops.env 对应值 → 按部署流程重启相关服务 → 在管理页更新 LLM 密钥配置。**不要把新值写入任何 .bak/.example/报告文件。**

## 可执行代码扫描结论（P0-G）

- `*.py` / `*.sh` / `*.yaml` 未发现硬编码凭据字面量（模式 + 人工复核）；凭据统一经 env/config 受控引用（`core/secret_ref.py` 提供 `${env:}`/`${file:}` 引用能力）。
- `*.example` 文件均为占位符，无真实凭据。
- 敏感文件权限已收紧：`zops.env.bak-20260805` 644→600；`zops.env`/`feishu.env`/`n8n.env`/`webchat.env` 均为 600。

## n8n 回调签名升级（运维操作指引）

`zops.env` 已配置 `N8N_CALLBACK_SECRET`。回调端点现已强制签名（未升级的工作流回调将被 401/503 拒绝，即 SPEC 的暂停语义）。n8n 侧升级方法——在回调 HTTP Request 节点前加一个 Code 节点生成签名头：

```js
const crypto = require('crypto');
const secret = '＜N8N_CALLBACK_SECRET 的值，建议存 n8n 凭据而非硬编码＞';
const ts = String(Math.floor(Date.now() / 1000));
const nonce = crypto.randomBytes(8).toString('hex');
const body = JSON.stringify($input.first().json);   // 必须与实际发出的 body 完全一致
const payload = ['POST', '/api/webhook/n8n_callback', body, ts, nonce].join('\n');
const sig = crypto.createHmac('sha256', secret).update(payload).digest('hex');
return { json: { body, headers: {
  'Content-Type': 'application/json',
  'X-Zops-Timestamp': ts, 'X-Zops-Nonce': nonce, 'X-Zops-Signature': sig } } };
```

HTTP Request 节点改为：Body = `{{ $json.body }}`（Send Raw JSON），Header 引用 `$json.headers`。时间窗 ±5 分钟；同一 nonce/事件重复回调会被幂等拒绝（返回 `reason: duplicate` 不报错）。
