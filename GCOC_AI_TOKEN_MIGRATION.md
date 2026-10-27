# gcoc-ai（198.18.18.147）对接 GCOC 改造说明（Token 鉴权）

> 版本：2026-10-27　影响范围：147 上所有调用 `gcoc-info.example.com` 的脚本/服务

## 1. 为什么要改

147 现在的方式是「用 svc-gcoc 这个**个人 AD 账号 + 密码**登录 GCOC 拿 `sessionid`，再带 Cookie 调接口」。问题：

| 问题 | 后果 |
|---|---|
| 每次登录都会在 AD 侧做一次绑定 | 密码一旦变更未同步，1 秒内并发重试 6 次即可把 svc-gcoc 锁死（2026-10-08 已发生） |
| session 只有 24 小时（`SESSION_COOKIE_AGE=86400`） | 过期即 401，脚本又去登录，形成「登录→过期→登录」循环 |
| 借用个人账号 | 操作日志里无法区分是 svc-gcoc 本人在用还是脚本；限流（30 次/分钟）与本人员工浏览器共用配额 |

实测（nginx 访问日志）：147 近一周内调 `/api/auth/login/` **143 次**，仅 2026-10-27 当天就有 3 次
（13:08、13:18、14:05，均 200）。每次登录都是一次 AD 绑定，密码一变更就是连环锁号风险。

从 2026-10-27 起，145 侧已改为 **Token 鉴权**，并且**已关闭 svc-gcoc 等 AD 账号在 GCOC 本地的密码兜底**：
如果 147 仍用本地密码登录，会直接 401；即使 AD 密码正确，也请尽快切 Token，避免再次触发锁号。

## 2. 交付物

| 文件 | 说明 |
|---|---|
| `refer/gcoc_ai_client.py` | 可直接拷贝到 147 使用的客户端，封装 Token、连接复用、429/5xx 退避、401/403 直接失败 |

依赖：`httpx`（147 现有 0.26.0，无需新增）。

## 3. 改造三步

### 3.1 放文件 + 配 Token

```bash
cp gcoc_ai_client.py /path/to/gcoc_ai/
export GCOC_BASE_URL='https://gcoc-info.example.com'   # 写进服务配置/systemd Environment
export GCOC_API_TOKEN='<向 GCOC 管理员索取>'
```

Token 由 GCOC 侧发放，带 scope + IP 白名单（`198.18.18.147`）。
按 147 当前实际调用的接口，已发放的 gcoc-ai Token 授权为 `all`，覆盖：

| 147 调用路径 | 频次（近一周） | 客户端方法 |
|---|---|---|
| `/api/dns/query/` | 19689 | `gcoc.dns_query()` |
| `/api/dns/compare/` | 8 | `gcoc.dns_compare()` |
| `/api/cmdb_info/` | 122 | `gcoc.cmdb_page()` |
| `/api/site_info/` | 96 | `gcoc.site_info()` |
| `/api/alarm_info/` | 24 | `gcoc.alarm_info()` |
| `/api/monitor_info/` | 14 | `gcoc.monitor_info()` |
| `/api/case_info/` | 9 | `gcoc.case_info()` |
| `/api/auth/login/` | 143 ← **要删掉的就是它** | —— |

查看/调整命令（在 145 上执行）：

```bash
python manage.py dns_token list
python manage.py dns_token set-scopes --token <TOKEN> --scopes all
```

### 3.2 自检

```bash
python3 gcoc_ai_client.py
# BASE = https://gcoc-info.example.com
# [OK] cmdb    共 248 条资产
# [OK] site    22 个站点
# [OK] alarm   9 条告警
# [OK] monitor 8 组监控项
# [OK] case    49720 条案例
# [OK] dns     qq.com A 记录 2 条
# [OK] compare 1 个域名对比完成, consistent=None
```

出现 `401 / 403` 不要重试、更不要改回账号密码登录，直接找 GCOC 管理员核对 Token/scope/IP。

### 3.3 替换调用

**改前（登录 + Cookie）**

```python
import httpx

BASE = 'https://gcoc-info.example.com'
def login(user, password):
    r = httpx.post(f'{BASE}/api/auth/login/', json={'username': user, 'password': password})
    r.raise_for_status()
    return r.cookies                      # 之后每次请求都带这份 Cookie

cookies = login('svc-gcoc', os.environ['SVC_GCOC_PWD'])
resp = httpx.get(f'{BASE}/api/cmdb_info/', params={'page_size': 500, 'search': 'xxx'}, cookies=cookies)
ans = httpx.post(f'{BASE}/api/dns/query/', json={'domain': d, 'record_type': 'A'}, cookies=cookies)
site = httpx.get(f'{BASE}/api/site_info/', cookies=cookies)
alarm = httpx.get(f'{BASE}/api/alarm_info/', cookies=cookies)
mon = httpx.get(f'{BASE}/api/monitor_info/', params={'page_size': 50}, cookies=cookies)
case = httpx.get(f'{BASE}/api/case_info/', params={'page_size': 50}, cookies=cookies)
```

**改后（Token）**

```python
from gcoc_ai_client import gcoc            # 进程内单例，自动带 Bearer 并复用连接

page = gcoc.cmdb_page(search='xxx', page_size=500)
for item in page['results']:
    print(item['name'], item['ip'], item['cluster'])

site = gcoc.site_info()['results']
alarm = gcoc.alarm_info()['results']
mon = gcoc.monitor_info(page=1, page_size=50)
case = gcoc.case_info(page=1, page_size=50, status='open')

ans = gcoc.dns_query(['qq.com', 'baidu.com'], 'A')
for r in ans['successful_results']:
    print(r['domain'], r['result'], r['ttl'])

cmp = gcoc.dns_compare(['qq.com'], 'A', servers=['223.5.5.5', '8.8.8.8'])
print(cmp['consistent'], cmp['domains'][0]['servers'])
```

如果 147 里是「一个会话对象到处传」的写法，最小改动是：

```python
# 旧：client = httpx.Client(cookies=login(...))
client = GCOCClient()                      # 新：同一个变量类型用法完全一致
# 旧：resp = client.get(f'{BASE}/api/cmdb_info/', params={...})
resp = client._client.get('/api/cmdb_info', params={...})   # base_url 已内置，不需再拼域名
```

> 建议优先改成 `gcoc.cmdb_page()` / `gcoc.dns_query()` 两个语义方法，错误处理和重试已经内置。

## 4. 错误码约定（务必遵守）

| 状态码 | 含义 | 正确处理 |
|---|---|---|
| 200 + `success:true` | 正常 | 使用返回数据 |
| 401 | Token 缺失/无效/被吊销 | 打印错误、告警给管理员。**禁止回退到账号密码登录** |
| 403 | 无对应 scope，或来源 IP 不在白名单 | 找 GCOC 管理员补 scope / IP（147 固定为 198.18.18.147） |
| 429 | 超过 30 次/分钟 | 退避后重试（客户端已内建 5/10/15 秒三次退避） |
| 500 | GCOC 侧异常 | 退避重试，持续失败则告警 |

## 5. 上线检查清单

- [ ] 代码里已无 `/api/auth/login` 调用、无 `svc-gcoc` 账号密码
- [ ] 服务配置里能看到 `GCOC_API_TOKEN`（不要明文进 git，用 systemd `EnvironmentFile` 或密钥管理）
- [ ] `python3 gcoc_ai_client.py` 自检两项都 OK
- [ ] 观察 145 上 `gcoc_dns_query_log.username` 变成 `token:gcoc-ai`（而不是 `svc-gcoc`）
- [ ] 观察 145 上 `gcoc_login_log` 不再出现来自 198.18.18.147 的登录记录
- [ ] 删除 147 上保存的 svc-gcoc 密码变量 / 定时登录任务 / 缓存 cookie 文件

## 6. 回滚

如 Token 通道出现故障需要临时回到旧方式，需要 GCOC 侧先恢复账号本地密码
（145 上执行 `backup/password_hash_backup_*.sql` 中的 UPDATE），并**同时**接受 AD 锁号风险。
不建议回滚，优先修 Token 通道（401/403 都是一分钟能定位的问题）。

## 7. 常见问题

**Q：只调 DNS，能不能只给 dns scope？**
A：可以，`set-scopes --scopes dns`。但 147 还会查 cmdb/site/alarm/monitor/case，只给 dns 会 403。

**Q：还需要别的接口怎么办？**
A：告诉 GCOC 管理员接口路径，145 侧在对应视图上挂 `api_token_or_session('<新scope>')` 并给 Token 补 scope 即可。

**Q：能否继续用 Cookie/Session？**
A：浏览器页面可以（人和以前一样登录），**脚本一律用 Token**。
