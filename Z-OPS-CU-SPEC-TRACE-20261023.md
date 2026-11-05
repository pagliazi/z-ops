# Computer Use 本机全面升级 — Spec 对照与阻塞记录

日期: 2026-10-23（2026-10-24 补充真实设备端到端、WSS 生命周期与设置页 UI 验收；2026-10-25 补充 UIA 元素层、真实桌面画面、浏览器任务切换竞态、打包产物浏览器运行时与当前代码的真实设备端到端复跑，以及 Linux X11 桌面后端与验收清单）
Spec: `Computer_Use_本机全面升级_task-eb6.md`
结论: Linux 侧全部可验证项已实现并取得运行证据（真实 Companion、真实无头浏览器按锁定 revision 实跑、真实文件/文档与工作流、双设备并发的真实 WSS 生命周期、真实浏览器操作设置页「电脑操作」、真实 Chromium 加载扩展经 Native Messaging 交接登录态、未签名测试包以普通桌面用户启动沙箱化 Chromium、当前代码在未预设 `SSL_CERT_FILE` 下 42/42 真实端到端复跑通过、Linux X11 桌面后端在真实 X 服务器与真实 GUI 客户端上跑通并发真实事件）；Windows 实机验收、生产签名与 macOS 实机验收仍阻塞（Spec 第 84 条明确「Linux 模拟测试不能算 Windows 验收」，Linux X11 的人工干扰与锁屏验收也尚未在带真实键鼠的桌面会话中执行）；Wayland 已实现 portal 探测层与 EIS(libei) 输入传输层（一.13、一.14），但 portal 与 EIS 协议里都只有整屏授权/屏幕 region，**没有窗口对象**，所以仍不声明任何桌面能力；要开放需要真实合成器实测，并由你定「整屏接管能否接受」。macOS AX 后端已实现（一.12），未验收前同样不声明 `desktop.*`；界面与 CLI 的平台表述已补齐 macOS（一.16）。

## 一、本轮新增与修复

| 项 | 变更 | 证据 |
| --- | --- | --- |
| 停止贯通 Companion（第 60 条） | `api/routers/chat.py` 新增 `_cu_cancel`：按 `msg_id` → 运行时 task_id 回退查找设备任务，非终态时下发 `control cancel`，返回 `{state, requested, confirmed, unknown}`；`task_stop` 与前端按「已确认 / 未确认 / 失联未知 / 无设备任务」四类分别提示 | 真实链路：HTTP 200，`cu.confirmed=true`，note 含「已确认停止」，设备任务终态 `cancelled/device_confirmed` |
| 请求 ≠ 已停止 | `agents/computer_agent.py` 新增 `CuStopRequested`、`_cu_state`、`_stop_result`；`rejected` 回执读设备状态区分 stopped/paused；`CuOutcomeUnknown` 先查状态，避免把用户已停止误报为结果未知 | `tests/cu/test_agent_companion.py::test_rejected_command_after_stop_reports_device_receipt` |
| 控制挂起期的回执竞争 | `core/cu/devices.py` `accept_result`：`control_id` 挂起时命令的 `unknown/cancelled` 不再提前终结任务或置设备离线，保持 `pause_requested/cancel_requested`(reason=`control_pending`)，等 control result 定状态 | `tests/cu/test_devices.py::test_interrupted_command_does_not_hide_control_receipt`、`test_pause_interrupting_read_command_stays_paused_not_cancelled` |
| 小窗执行方式（第 68 条） | Companion 随回执上报 `execution_mode`，服务端白名单过滤并写入任务视图，`BrowserLivePanel` 显示「执行方式：…」，未上报时明确「不推断为后台或桌面模式」 | 真实浏览器截图 `screenshots/cu-panel-execution-mode.png` |
| 小窗停止确认（第 68 条） | 小窗停止按钮走 `/api/cu/tasks/{id}/control`，仅设备回执 `cancelled` 才显示「已确认停止」 | 真实浏览器截图 `screenshots/cu-panel-stop-confirmed.png`（状态：已停止，原因 device_confirmed） |
| p3 测试无导入副作用（第 79 条） | `tests/p3/test_cu_bridge.py`、`test_cu_login_guard.py` 改为 unittest 驱动，导入不再执行检查；新增 `guard_test_redis`，拒绝在疑似生产 Redis(6379/0) 上 flushdb | `python3 -m unittest tests.p3.test_cu_login_guard tests.p3.test_cu_bridge` → Ran 2 tests, OK（10/10 与 21/21 断言全过） |
| 扩展↔Native Host 回执回环（第 15、57 条） | `extension/background.js` 的端口监听只把 `command`/`control` 帧交给任务队列；此前的实现对 `login_handoff_result`/`result`/`task_status` 也调用 `receive()`，被判为非法信封后又回显 `result`，扩展与 Native Host 之间会持续互发消息 | `extension/background.test.cjs::Native 回执不进入命令路径，不回显、不形成消息回环`；真实链路截图与输出见一.8 |
| 自己注入的输入被误判为用户输入（第 50、52 条） | `companion/desktop.py` 每次动作后都把租约基线推进到本次动作之后（`lease.baseline = driver.mark_baseline()`，`WindowsDriver.mark_baseline()` 返回新基线）。此前只刷新驱动内部 `_baseline`，租约仍拿批准时的基线比较 `GetLastInputInfo`，Companion 自己的键盘/鼠标注入会推进最后输入时间，约五次动作后就被误判为「用户输入」而暂停 | `test_desktop_safety.py::test_own_injected_input_does_not_suspend_but_user_input_does`（连续 8 次动作不暂停；真实用户输入 500ms 后仍暂停） |
| 紧急停止后桌面能力永久不可用（第 52 条） | 移除 `request_control` 中对 `revoked` 的一票否决：紧急停止只终结当前租约，再次接管必须重新走本机原生批准（每次都是独立决定），新批准成立后才清除 `revoked`（原代码该行不可达） | `test_desktop_safety.py::test_emergency_stop_ends_lease_but_new_native_approval_still_works` |

## 一.5、2026-10-24 真实设备验收（非源码断言）

验收方式：在专用系统用户 `cpcnet` 下运行真实 Companion（系统 keyring 存 Ed25519 私钥）→ nginx TLS → `wss://…/api/cu/devices/connect`，命令由服务端 `core/cu/devices.py` 下发到真实执行器；授权目录 `/tmp/zops-workspace`（read/write_new/manage），站点 origin `http://127.0.0.1:8099`；Windows 桌面能力未参与。

| 项 | 真实证据 |
| --- | --- |
| 配对与一次性 ticket | 真实配对码换发 device_id，challenge→ticket→WSS 握手成功；任务 `execution_mode=background` |
| 执行与停止 | `system.info`、`file.write_new` 真实落盘（sha256 与回执一致）；停止返回 `cancelled/device_confirmed`（执行端确认，非仅请求） |
| 独立无头浏览器 | Playwright Chromium 真实启动；`browser.goto/snapshot` 取到页面文本与引用；`browser.download` 落盘到授权根；截图帧进任务专属缓存且任务记录/审计无 jpeg |
| 登录态单次复用 | 经 `companion/login_state.store` 交接 1 个 cookie，私有页直接放行，交接文件随即删除（单次消费） |
| 登录墙暂停 | `/login` 触发 `paused`，任务视图 `pause_reason=sensitive_or_login_page`，暂停后仍可被停止（`cancelled/device_confirmed`） |
| 未授权站点 | `browser.goto` 到未授权端口返回 `rejected`，任务保持 `running`，无服务器浏览器回退 |
| 文件/文档 | `document.write_new` 生成 xlsx/docx、`file.write_new` 生成 txt，`document.extract`/`file.read` 回读一致；重复 `file.write_new` 被拒且原文件 sha 不变；`file.replace` 原子替换且无 `.companion-*` 残留；符号链接越界读写均被拒且外部文件未变；`file.list/find` 排除链接；`log.collect` 脱敏（credential/bearer/email 均命中） |
| 终端与运维 | `system.info`/`process.list` 成功；`service.list` 在无 systemd 权限时如实返回 `service_query_unavailable`（不伪造）；登记脚本 `script.run` 校验哈希后运行并回传退出码/摘要；未登记脚本与越界参数均 `rejected` |
| 跨应用工作流 | 下载 CSV → `file.read` → `document.write_new` 汇总 xlsx → `document.extract` 预览 → 本机登记脚本复核 sha256；随后新任务恢复时 `file.write_new` 重放被拒、用回执 sha 命中原文件、预览内容不变（不重放写操作） |
| 小窗 UI | 真实浏览器打开会话 → 任务小窗 → 选择任务：显示设备、`执行方式：后台执行`、`状态：已暂停`、`原因：页面需要登录或包含敏感输入，已在采集前暂停`、恢复入口；截图 `screenshots/cu-pause-reason-20261024.png` |

本轮修复的真实缺陷（均由上述实机链路暴露）：

| 缺陷 | 根因 | 修复 | 单测 |
| --- | --- | --- | --- |
| 吊销/重新配对后设备永久无法连接 | Journal 中旧 device_id 的 `generation`/`fence`/`generation_seen:*` 与 `tasks` 停止标记被沿用，`Guard.bind` 报 `journal_identity_mismatch`/`generation_reused` | 身份变化且同 server、device 变化时清 `generation`/`fence`/代际标记与 `tasks`；命令记录保留且启动时统一标记 unknown，不重放 | `test_repair_new_device_resets_connection_meta` |
| 暂停后无法停止 | `except Paused` 调 `journal.stop_tasks()`，而 `Guard.admit` 对控制消息也校验 `task_stopped`，取消被拒（`control_rejected_unconfirmed`） | `admit` 只阻断新的执行命令，控制消息始终可达 | `test_pause_reports_reason_and_task_still_stoppable` |
| 暂停原因只有 `local_authorization_required` | 引擎丢弃 `Paused` 静态码，服务端只落 `reason=device_confirmed` | `security.pause_code` → 引擎上报 `pause_reason` → 服务端 `p.pause_reason` 白名单 → 任务视图 → 小窗/智能体中文文案 | `test_pause_reports_reason_and_task_still_stoppable`、`test_paused_result_carries_static_pause_reason`、`test_paused_result_drops_non_static_pause_reason`、`test_pause_reason_accepts_only_static_codes`、`test_paused_reason_is_localized_without_page_content` |
| 真实网络下设备 WSS 一律 403（只有 loopback 可连） | 设备 WS 端点复用浏览器 `Origin` 强制校验；经 nginx 转发后客户端 IP 不再是 127.0.0.1，而 Companion 不是浏览器、不带 Origin，`transport_ok` 直接拒绝 | 设备通道只校验 TLS：无 cookie/Authorization 等浏览器凭据，鉴权完全由一次性票据 + 设备签名承担；若请求自带 Origin（浏览器场景）仍按白名单拒绝非法值 | `test_ws_route_accepts_native_client_over_tls_and_rejects_bad_origin` |
| 打包产物/自编译解释器把所有证书判为不可信 | `companion/client.py` 只用 `ssl.create_default_context()`；本机与 PyInstaller 产物同源的 python-build-standalone 把默认信任目录指向空目录（`/usr/local/ssl/certs`，`x509_ca=0`），真实 `POST /api/cu/devices/enroll` 直接 `CERTIFICATE_VERIFY_FAILED`，而 CLI 只打一行通用失败，看起来像配对码或 keyring 出错 | 新增 `default_context()`：默认信任库为空时回落到系统 CA 包（`/etc/ssl/certs/ca-certificates.crt`、`/etc/pki/tls/certs/ca-bundle.crt`、`/etc/ssl/cert.pem`），只换信任源、不关闭主机名校验；`HTTPS` 改用该上下文。实测未设置 `SSL_CERT_FILE` 时 venv 与冻结包都能完成真实 HTTPS 配对 | `test_empty_default_store_falls_back_to_system_bundle`、`test_non_empty_default_store_is_used_as_is`、`test_missing_bundles_keep_default_context_instead_of_disabling_verification` |
| p3 桥测试与 `tests/cu` 同进程运行时打到生产 Redis | `cu_bridge` 在调用时读 `config.REDIS_URL`，而 `tests.p3.test_cu_bridge` 只设了环境变量；若 `config` 已被前面的测试导入，地址就停在 6379，`flushdb` 只清了 6399，总线/心跳断言瞬间失败 | 测试在 `asyncSetUp`/`main` 中显式 `config.REDIS_URL = TEST_REDIS_URL` 并增加「隔离」断言，不依赖导入顺序 | `tests.p3.test_cu_bridge`（新增「隔离: 强制使用测试 Redis 实例」） |

逐条对照时补齐的缺口（可本机实现部分）：

| 缺口 | 补充实现 | 单测 |
| --- | --- | --- |
| 登记脚本缺平台/权限/目标目录/版本/取消元信息（Spec 第 52 条） | `scripts.validate_entry` 对齐 `core/script_exec.py`：校验并归一化 `platform`/`permissions`/`target_dirs`/`version`/`cancel.grace_s`；运行时平台不匹配拒绝执行；登记时 `target_dirs` 必须落在已授权可写根内；`--doctor` 输出元信息摘要（不含脚本内容） | `test_script_metadata_is_validated_and_platform_gated`、`test_script_platform_mismatch_is_not_executed`、`test_script_registration_requires_target_dirs_inside_granted_roots` |
| Native Messaging manifest 生成未纳入回归 | 无授权扩展时拒绝；有授权时只输出 stdio host 与 `allowed_origins` | `test_native_manifest_requires_granted_extension_and_is_stdio_only` |

## 一.6、2026-10-24 真实 WSS 生命周期验收（两台真实 Companion）

验收方式：同一用户下两台真实 Companion（各自独立 `HOME`/`XDG_RUNTIME_DIR`/系统 keyring，运行在独立 dbus 会话中解锁 SecretService），经 `wss://opscore-ai.example.com/api/cu/devices/connect` 连接真实后端；命令、代际、fencing、吊销全部由真实 `core/cu` 服务下发与核对，无 fake Redis、无源码字符串断言。42 项检查全部通过。

| 项 | 真实证据 |
| --- | --- |
| 双设备并发 | 两个不同 `device_id` 同时 online；并发 `system.info` 均 `ok`（0.20s）；并发 `file.write_new` 各自落盘（`created=true`，sha256 与回执一致） |
| 跨设备/跨任务隔离 | 同一任务不能绑定第二台设备（`任务已绑定其他设备或会话`）；用 B 下发 A 的任务命令 → `rejected`（绑定不匹配） |
| 跨用户隔离 | 其他用户使用/读取/吊销探针设备全部 `设备不存在或无权访问`，设备列表为空 |
| 回执丢失 | 执行中杀 Companion：`dispatch` 返回 `unknown / 命令结果未知；禁止自动重发`；任务终态 `unknown(disconnected)`；离线设备新命令直接 `rejected`；旧代际心跳 `False` |
| 断线重连 fencing | 重连后 `connection_generation` 轮换、`fencing_token 2→5`；重启后脚本未被重放（marker 仅 1 行），断连任务只登记过一次命令 |
| 旧代际不可认领 | 第二次重连 `5ea1ac19→793211ca`、`fence 5→9`；旧代际登记的信封 `claim_command=False`，同代际对照 `True` |
| 吊销 | `detach(revoked_by)` 后 `revoked=true`、离线、在途任务 `unknown/device_revoked`、新任务 `本机设备离线或未完成握手`；Companion 收到断连后重连失败（日志 0→4）且不降级授权 |

## 一.7、2026-10-24 设置页「电脑操作」真实 UI 验收（第 65、83 条）

验收方式：真实 Companion（cpcnet + 系统 keyring）配对上线后，用真实 Chromium 打开生产站点，注入登录态后派发 `zops:open-settings` 打开「设置 → 电脑操作」，点击「刷新状态」拉取真实设备列表，并真实点击「任务画面推送」开关往返一次；截图与接口返回值同时核对，不以源码字符串代替。

| 项 | 真实证据 |
| --- | --- |
| 设备列表 | 卡片显示「设计工作站 · 在线 · 已选择」，`linux · 版本 2.0.2 · dev_127ece6231020c126591aa51b4d03551`（截图 `screenshots/cu-settings-computer-devices-20261024.png`） |
| 版本与能力 | 「能力：document.extract、document.write_new、file.find、file.list、file.read、file.write_new、log.collect、process.list、service.list、system.info（能力不等于授权）」 |
| 授权摘要只读 | 「授权目录：work（读新建）」、授权站点/登记脚本/授权桌面应用/Native origin 均为「无」，并注明「本页只读；新增或扩大授权必须在本机 CLI 完成」 |
| 选择与吊销入口 | 已选择设备的「选择此设备」按钮禁用，「吊销」可用（不会把同账号最后连接设备当执行目标） |
| 画面/桌面/证据策略 | 桌面接管申请「已禁止」、任务画面推送「已开启」、截图证据保存「已关闭」，与 `CU_PREF_DEFAULTS` 一致 |
| 开关真实生效 | 点击任务画面推送：已开启 → 已关闭 → 已开启；`GET /api/cu/prefs` 返回 `{"push_frames": true, "desktop_optin": false, "persist_shots": false}`，页面提示「画面偏好已保存；实际可用性由执行器能力决定」（截图 `screenshots/cu-settings-computer-20261024.png`） |
| 接口与页面无错误 | 本次操作中 `/api/cu/devices`、`/api/cu/prefs` 全部 200；无 `pageerror` |
| 真实桌面画面文案（本轮新增，第 67 条） | 改完 `ComputerUseSettings.tsx` 后 `npm run build` + `systemctl restart zops-frontend`，再开真实 Chromium 重新验收：`设置 → 电脑操作 → 后台策略与本机授权` 一节显示「桌面接管申请 已禁止 / 任务画面推送 已开启 / 截图证据保存 已关闭」，截图证据段落完整出现「真实桌面画面只在接管时推送：需在本机批准时勾选 view 范围，且只截被接管的那个窗口，锁屏或密码框有焦点时自动暂停采集」；脚本断言 7 项全为 true，截图 `screenshots/cu-settings-view-scope-20261025.png`（验收用的临时 token 已从 Redis 删除） |

## 一.8、2026-10-24 扩展 ↔ Native Messaging 真实链路验收（第 15、33 条）

验收方式：真实 Chromium（headful + Xvfb，`--load-extension`）加载 `zops/extension` 2.x，service worker 主动 `runtime.connectNative` 启动本机 `companion.native_host`（stdio）；设备经 `/api/cu/devices/enroll` 一次性配对。打开的站点为本地 `http://127.0.0.1:8123`（下发 `sid` Cookie），全程不接触生产账号或模型。

| 项 | 真实证据 |
| --- | --- |
| 扩展加载与 ID | `EXTENSION pgakbhachalajnahghhobbckdnkhgide expected pgakbhachalajnahghhobbckdnkhgide match True`（未打包扩展 ID = 绝对路径 sha256 前 32 位映射 a–p） |
| Native Host 注册 | 清单 `net.zops.companion.json` + 可执行启动脚本；`hello` 回 `protocol 2 / route=isolated_headless / server_connected=false / shared_server_task=false`，`capabilities` 由本机实际可用能力决定 |
| 弹窗状态（真实 UI） | `POPUP_STATUS 本机通道已连接`；`POPUP_TASKS 本扩展会话任务数：0 · 截图不可用`（截图 `screenshots/cu-extension-native-popup-20261024.png`） |
| 非任务标签拒绝 | 真实点击「授权当前站点登录态」（弹窗自身是活动标签、非 http(s) 站点）→ `请先切到要授权的 http(s) 站点标签`，不发起任何读取（截图 `screenshots/cu-extension-native-guard-20261024.png`） |
| 显式点击交接 | 将活动标签指向验收站点后真实点击 → 弹窗显示 `已交给本机 Companion：1 个 Cookie，5 分钟内单次使用。`（截图 `screenshots/cu-extension-native-handoff-20261024.png`）；回执与页面文案只含数量，断言输出中不含 Cookie 值 |
| 本机落盘与单次消费 | `HANDOFF_FILE mode=600 size=190 origin=http://127.0.0.1:8123 cookies=1 single_use=True`；`login_state.claim` 校验 origin、Cookie 名、文件已删除，第二次 `claim` 为 `None` |
| 无云端回退 | 该链路中 Native Host 只写本机状态目录；`hello` 明确 `server_connected=false`，Cookie 不经服务端 |

本次实机验收发现的实现缺陷（已修复并加回归）：`extension/background.js` 把 Native Host 的所有帧（包括 `login_handoff_result`、`result`、`task_status`）都交给命令入口，命令校验失败后又回显 `result`，会与 Native Host 形成持续互发。现只在 `command`/`control` 帧进入任务队列。同时确认 Linux 上清单必须放在浏览器**用户数据目录**的 `NativeMessagingHosts/`（默认 `~/.config/google-chrome/`；带 `--user-data-dir` 时必须放在该目录），已写入 `extension/README.md` 安装步骤。

## 一.9、Windows 桌面接管与 UIA 元素级动作（源码 + 回归；无实机）

本轮审计 `companion/desktop.py` 全文，修复两处会直接破坏接管的真实缺陷，并把 Spec「Windows 首批适配 UIA」从「仅导入 pywinauto」落到元素级动作。

| 项 | 实现与证据 |
| --- | --- |
| 缺陷：自己注入的输入被当成用户输入 | `act()` 原本只刷新驱动内部基线，租约仍用批准时的旧基线比较 `GetLastInputInfo`，连续几次动作后会误判 `user_input` 暂停。现 `mark_baseline()` 返回新基线并由 `act()` 回写 `lease.baseline`；回归 `test_own_injected_input_does_not_suspend_but_user_input_does`（8 次动作不暂停；真实输入 500ms 后仍暂停） |
| 缺陷：紧急停止后桌面永久不可用 | `request_control` 里的 `require(not self.revoked)` 使 `revoked=False` 不可达，按过 `Ctrl+Alt+Pause` 后只能重启 Companion。现只终结当前租约，再次接管仍须本机原生批准（不保留永久同意）；回归 `test_emergency_stop_ends_lease_but_new_native_approval_still_works` |
| UIA 只读元素列表 | `desktop.inspect` 新增可选 `window_id`：只对本机按进程名授权的应用返回 `elements[{index,role,name,enabled}]`；`ELEMENT_TYPES` 仅 8 类可激活控件，文本/输入框/文档不入列；空名与离屏跳过；敏感名称（password/token/验证码/密码…）以 `[敏感控件]` 占位且禁止激活；证据 `test_uia_elements_are_read_only_filtered_and_allowlisted`（Edit、禁用项、离屏项均不可见） |
| UIA 元素级激活 | `desktop.act(action="invoke")` 必须同时带 `element/role/name`，先在本机重新解析元素列表比对（被替换/改名→`desktop_element_changed`，禁用→`desktop_element_disabled`），驱动层激活前再核一次，且只用 `invoke()` / `iface_invoke.Invoke()`，绝不回退坐标点击；回执只含 `{index, role}`，不回显名称；证据 `test_uia_invoke_requires_lease_scope_and_exact_element` |
| 协议与服务端动作映射 | `core/cu/protocol.py` 新增 `DESKTOP_SCOPES`（含 `invoke`）与 `DESKTOP_ELEMENT_ROLES`/`DESKTOP_ELEMENT_LIMIT`；`desktop.inspect` 接受 `window_id`，`desktop.act` 对 invoke 要求 `{action,element,role,name}` 严格等价且拒绝 `edit` 等非激活角色；`agents/computer_agent.py` 支持 `desktop_inspect(window_id)` 与 `desktop_action="invoke"` 并提示「优先 invoke 而非坐标点击」 |
| UIA 验收不只靠 import | `--accept-desktop` 现在实测：`probe_uia()` 枚举桌面窗口并读一个真实控件树；`uia_app_report()` 对已打开的记事本/计算器/资源管理器及已授权登记应用逐个试读，只回报可读性与控件数（不含名称/内容），任何一个不可读或全部为 0 就拒绝写验收记录；验收记录新增可选 `uia_apps` 且校验结构；证据 `test_acceptance_uia_apps_must_be_readable_and_bounded`、`test_uia_app_report_only_counts_standard_and_granted_apps` |
| 真实桌面画面（Spec 第 67 条） | 新增 `view` 授权范围：只有 Native 对话框里勾选了 view 的 60 秒租约才允许 `desktop.screen`；Windows 侧只用 `PrintWindow` 截被接管窗口（不截整屏/其他应用、不读剪贴板），缩放至 1280×800、JPEG ≤ 384KB；锁屏或焦点在密码框时返回 `sensitive_input` 且不采集、不终结租约；服务端只接受空参数、帧只允许来自 `browser.screenshot`/`desktop.screen`；agent 只在取得含 view 的租约后推真实桌面帧；证据 `test_desktop_frame_requires_view_scope_and_pauses_on_sensitive_focus`、`test_desktop_screen_frame_only_for_authorized_desktop_commands` |
| 回归 | `companion/.venv` 7 个模块（browser/cli/local/protocol/desktop/documents_pdf/workflows）`Ran 85 tests ... OK`（0 skipped）；系统 python `test_devices`+`test_agent_companion` `Ran 58 tests OK`、`test_chat_stop`+`test_download_guard` `Ran 12 tests OK`、`tests/p3` 两模块 `Ran 2 tests OK`；打包二进制重建后 `--doctor` 输出包含 `desktop.screen = linux_x11_wayland_desktop_not_accepted` |
| 缺陷：任务切换时迟到请求污染新任务（浏览器网络层） | `stop_task()` 原先先置 `blocked=False`，而停止前已在途的 `route()` 请求随后异常时会无条件 `self.blocked = True`，使下一个任务第一次 `guard()` 误判 `origin_blocked`（假暂停，任务被迫中止）。现 `Browser.generation` 在每次 `stop_task` 递增；`route()` 进入时快照 `(generation, http)`，失败只在「代际未变且会话仍在」时置 `blocked`，并改用局部 `http` 避免复用已关闭的会话；证据 `test_late_route_from_previous_task_cannot_block_new_task`（含正对照：当前代际失败仍拦截）、`test_route_after_stop_uses_no_previous_session` |
| 真实浏览器回归不再被跳过 | Playwright 锁定的 `chromium_headless_shell-1194` 本机没装（缓存里只有更新的 1228），11 个浏览器用例全部 `skipped`，等于没有证据。现按锁定 revision 安装后走默认路径真实执行（不再需要 `COMPANION_TEST_CHROMIUM` 覆盖）：快照剔除输入值/隐藏字段/脚本与 query 串、fill/select/scroll 与截图尺寸 1024×720、登录态一次性注入且二次任务无 cookie、密码/隐藏 token/OTP/登录页暂停、跨源与单跳重定向拦截、取消只关任务浏览器、下载只落新文件、Native Host stdio 全链路 |
| 打包产物的浏览器运行时（第 12、32 条） | 只证明源码能起浏览器不够：重建未签名测试包后，以普通桌面用户跑 `--doctor`，`capabilities` 含 `browser.act/goto/screenshot/scroll/snapshot`（无写授权根故不声明 `browser.download`），说明冻结包内 Playwright 驱动与外部 Chromium 协同正常；同一二进制以 root 运行时报 `run_as_normal_desktop_user_not_root`（原先一律报误导的 `sandboxed_chromium_unavailable`） |
| 慢观看者不得托住停止（第 61 条） | 帧是在步内串行拉取的（避免旧帧覆盖新帧），但超时原为 20 秒，设备响应慢时最长会拖 20 秒才回到停止检查；现改为 8 秒（短于动作超时），并保留每 1.5 秒一帧的上限；取消本身走控制频道，不依赖该等待 |
| 安装步骤不靠口口相传 | `companion/build.py` 构建成功后直接打印锁定 revision、`python -m playwright install chromium-headless-shell` 与 `PLAYWRIGHT_BROWSERS_PATH` 共享安装说明，并提示必须以普通桌面用户运行；证据 `test_browser_unavailable_reason_reflects_run_identity`、`test_no_origin_grant_reports_missing_grant_not_browser_failure`（未授权站点时不得去启动浏览器） |
| 仍阻塞 | 无 Windows 实机：未在记事本/计算器/资源管理器/登记业务应用上验收元素列表与 elevate 控件；UIA 元素动作也未在真实应用上跑过 |

## 一.10、2026-10-25 真实设备端到端复跑（当前工作区代码）

验收方式：不复用旧脚本、不造 fake Redis；脚本已固化在 `scripts/cu_e2e_device_check.py`，必须显式 `ZOPS_CU_E2E=1` 才会运行（避免误触），不使用生产账号凭据。临时访问令牌只用来换一次性配对码；Companion 以普通系统用户 `cpcnet` 在独立 dbus 会话里解锁系统 keyring 后运行当前代码，经 `https://opscore-ai.example.com` 走真实 nginx TLS 与真实 WSS，任务与命令由真实 `core/cu` 服务下发；浏览器为操作端 Playwright 无头 Chromium（`PLAYWRIGHT_BROWSERS_PATH=/opt/zops-pw-browsers`，revision 1194）。**Companion 进程未设置 `SSL_CERT_FILE`**，用来同时验证本轮新增的信任库回落。42 项检查全部通过，验收后设备已吊销，令牌/配对码/设备/任务键与临时目录已清理（任务审计 `:events` 为永久只追加，不自动删除）。

| 项 | 真实证据 |
| --- | --- |
| 配对与上线 | `POST /api/cu/devices/pair-code` 200 → `--pair … --init` 输出「配对成功。默认没有目录、站点或脚本权限。」→ `challenge→ticket→wss` 上线，`/api/cu/devices` 返回该设备 `online`，能力 20 项（两个授权根: 只读/新建 + 含 manage） |
| 信任库回落 | 同一台机器的默认信任库为 `x509_ca=0`；未设环境变量时 Companion 仍完成 enroll/challenge/ticket，说明 `default_context()` 回落到系统 CA 包后校验正常（不是关闭校验） |
| 设备级互斥 | `start_task` → `running`；同设备第二个任务被拒：`设备已被其他任务占用；请先查询原任务状态`（Spec 第 58 条「后台任务按资源加锁」的真实形态） |
| 真实无头浏览器 | `browser.goto` 打开 `http://127.0.0.1:8099/index.html`，回执含按钮/链接引用且 `execution_mode=background`；`browser.snapshot` 返回元素引用；`browser.screenshot` 1024×720、JPEG 15.8KB |
| 帧通道 | `get_task(with_frame=True)` 取到任务专属帧（`/9j/` 开头、1024×720），任务终结后帧缓存被删除 |
| 跳应用工作流（第 44 条） | 真实页面快照取到下载链接引用 → `browser.download` 写入 `<写授权根>/Z-OPS下载/report.csv`（`saved=report.csv`，sha256 回传）→ `file.read` 读回 CSV 内容（`2026-09,128`）→ `document.write_new` 生成 `summary.xlsx`（4916B）→ `document.extract` 回读「合计 384」 |
| 不覆盖/不越权 | 对已下载文件再 `file.write_new` → `rejected`（不静默覆盖）；未授权 manage 时 `file.delete` → `capability_unavailable`（不声明、不尝试） |
| 文档与结构化查找（第 27、28 条） | `document.write_new` 生成 `notes.docx` → `document.extract` 读回「Z-OPS 本机验收 / 第二行汇总」；`file.list` 列出 `notes.docx`、`Z-OPS下载`、`summary.xlsx`；`file.find{pattern:"*.xlsx"}` 命中 `summary.xlsx` |
| 日志脱敏（第 38、62、81 条） | 写入含合成秘密的 `service.log`（`password=Hunter2-EXAMPLE`、`Authorization: Bearer …`、`ops@example.com`）后 `log.collect` 回执 `redacted={"bearer":1,"credential":2,"email":1}`，正文已为 `password=[REDACTED]`，原始秘密未返回 |
| 已授权的管理类操作（第 29 条） | 在 `manage=true` 的第二个授权根内：`file.write_new` → `file.move`（`moved=true`）→ `file.replace`（`replaced=true`，原子覆盖）→ `file.delete`（`deleted=true`）；未授权根上同样操作仍拒绝 —— 证明覆盖/移动/删除只对单独授权的目录开放 |
| 停止 | `control cancel` 后任务终态 `cancelled/device_confirmed`（执行端确认，不是仅请求），`execution_mode=background` 保留在任务视图 |
| 取消后可继续 | 取消并回收任务后，同设备新任务 `running` 且 `system.info` 正常返回（取消不会把设备永久卡死） |
| 登记脚本执行（第 37、42 条） | `script.run{name:probe,args:{args:"e2e"}}` → `exit_code=0`，stdout 为 `{"args": ["--args=e2e"]}`：按登记项解释器 + memfd 只传固定 argv，不回显任何路径 |
| 未登记/未声明参数被拒（第 39、41 条） | `script.run{name:not_registered}` → `rejected`；`probe` 附带未在 schema 声明的 `shell=rm -rf /` → `capability_unavailable`（参数白名单在执行端拦截，不进入子进程） |
| 取消贯通到子进程（第 40、60 条） | `sleepy` 脚本先写 `getpid()` 再 `sleep(120)`；取消后 `script.run` 回执 `unknown`（不伪造成功/失败）、任务终态 `cancelled/device_confirmed`，取消后 0.0s 即返回，`psutil.pid_exists(pid)=False`（无孤儿） |
| 吊销 | `DELETE /api/cu/devices/{id}` 200 → 设备 `revoked=true` 且离线，已终结任务审计不变 |
| 冻结包同样可配对 | 重建 `companion-unsigned-test` 后，以 `cpcnet` 未设 `SSL_CERT_FILE` 跑 `--pair … --init`：`rc=0`、「配对成功」，服务端确实新增随后被吊销的设备 —— 证明证书回落不只在 venv 生效 |

仍缺的同场景证据：Windows 实机（见第三节阻塞 1、5）。

## 一.11、2026-10-25 批准到期、Wayland 判定与人工步骤执行器

上一轮把 Linux X11 后端做完后，清单里仍有 6 项「manual」没人能真做：批准对话框没有超时、没有真实桌面任务就没有对话框、Wayland 判定漏信号。本轮把这三处补成可执行、可观察、可复算的形态。

| 项 | 实现与证据 |
| --- | --- |
| 缺陷：批准对话框永不到期（第 51、82 条） | Windows `MessageBoxW` 与 Linux `tkinter.messagebox` 都没有超时参数，无人操作就一直挂着；而引擎按 `asyncio.timeout(min(60, expiry - now))` 中止命令——命令已按过期失败、对话框还在，之后点「允许」就是批准一个已作废的请求。现 `APPROVAL_SECONDS=30`（严格短于 60 秒命令上限）、`ACCEPTANCE_SECONDS=90`，到期自动关闭并按**拒绝**处理，原因码 `desktop_approval_timeout` 与用户拒绝 `desktop_approval_declined` 区分；Windows 用子线程弹框 + `EnumWindows/GetWindowThreadProcessId/GetWindowTextW` 只关本进程同标题窗口（`PostMessageW(WM_CLOSE)`），X11 用 `root.after(ms, expire)`，`expire()` 先置 `timeout=True` 再 `destroy()` 以避开与「允许」的竞态；`Desktop.request_control` 依 `driver.last_approval` 判定原因 |
| 文案与验收标准同步 | `agents/computer_agent.py` 的 `PAUSE_REASON_TEXT`、`web-next/.../BrowserLivePanel.tsx` 的 `pauseReasons` 都加上到期文案（与拒绝不同字），设置页说明补「对话框 30 秒无人操作自动拒绝」；`scripts/windows_acceptance.py`、`scripts/linux_desktop_check.py` 的 timeout 判定改为「30 秒无人操作自动关闭并拒绝」——不实现超时前这项永远不可能通过 |
| 缺陷：Wayland 判定 fail-open（第 52 条） | 原先只看 `XDG_SESSION_TYPE`。若会话只设 `WAYLAND_DISPLAY`、`DISPLAY` 指向 XWayland，XTest 对原生客户端无效、焦点核对却「看起来成功」，等于把不可信环境当可信 X11。现 `companion/desktop.py::wayland_session()` 双信号判定（`XDG_SESSION_TYPE in wayland` **或** `WAYLAND_DISPLAY` 非空），`Desktop.prepare`、`X11Driver.prepare` 与清单的 `probe_session()` 共用同一个函数（明细里输出 `WAYLAND_DISPLAY`） |
| 缺陷：6 项人工步骤无法执行 | 原清单的 manual 步骤只是「你做了吗」的问答：没有真实任务就没有对话框，人坐在机器前也做不了。新增 `scripts/linux_desktop_attended.py`：把每一步接进真实 `Desktop.request_control/act/release`（原生对话框 → 60 秒租约 → XTest 注入 → 热键/锁屏/输入探测），判定只看观察到的状态——`lease/revoked/suspended/driver.secure()/external_input_since(baseline)/foreground()`；`approve` 还要求目标应用日志在本次动作**之后**新增 `CLICK` 行（`wait_log(prefix, since=before)`），否则算注入没到达应用 |
| 人工步骤不得默认通过 | 每步都先要求人在机旁（`prompt()` 遇 `EOFError` → 「非交互运行：没有人在机旁，步骤未执行」）；批准无租约、拒绝仍建租约、到期被报成拒绝、输入干扰后动作仍执行、锁屏未识别、热键未生效、租约残留，任一情况一律 fail；意外异常也是 fail 而不是 skip；`accepted` 只由逐步 `pass` 合成 |
| 证据如何被清单消费 | `scripts/linux_desktop_check.py` 新增 `--attended <json>`：只接受 `platform=linux`、`DISPLAY` 与会话一致、只覆盖 `manual` 步骤；未知步骤 id、重复步骤直接拒绝；`fail` 原样计入、`skip` 忽略、缺步骤仍按原 skip 处理。自动项不受人工证据影响 |
| 端到端（无人值守必须失败） | Xvfb `:95` + 真实 venv + 真实配置 + 真实验收记录：`--doctor` 报 `desktop_enabled=true`、caps 只多 3 项；`linux_desktop_attended.py` 在非交互下 `approve` 判 fail（「没有人在机旁」）、rc=1、JSON `accepted=false`；清单加 `--attended` 后该步显示 `attended: 非交互运行…`，其余 manual 仍 skip，`verdict.accepted=false`、rc=1 |
| 部署 | `npx tsc --noEmit` rc=0；`npm run build` 成功（`BUILD_ID=vFk0WlC_MJJ0cRqM8O33H`）；`systemctl restart zops-frontend` 后 HTTPS 首页 200，且首页引用的 `_next/static/chunks/0n3~may8dxulm.js`（含「30 秒无人操作自动拒绝」）经 nginx 200 |
| 回归 | companion venv（3.11，`requires-python>=3.11`）13 个模块 `Ran 250 tests OK (skipped=5)`（跳过的 5 个是 opt-in 的真实对话框用例）；系统 python 3.10 上 4 个应用侧模块 `Ran 72 tests OK`；`tests/p3` `Ran 2 tests OK`。分工是环境决定的：`local_worker` 走 `asyncio.timeout`（3.11 起才有），所以文件/文档/工作流必须在 venv 里跑；反过来 `test_devices`/`test_agent_companion`/`test_chat_stop`/`test_download_guard` 依赖后端库，在 venv 里反而导入不了 |
| 仍阻塞 | 6 项人工步骤仍须**人在真实 X11 桌面前**执行（脚本只保证「做了才算过」）；Wayland/Windows/macOS 见第三节 |

### 一.11.1、用「机器替身」把通过分支真跑了一遍（随后发现的真实缺陷）

上一版 attended 只有未通过分支被验证过。用 XTest 写了一个「坐在机器前的人」（找到原生对话框 → Tab/空格 选择），把每一步的**通过分支**在 Xvfb + 真实对话框上跑一遍，立刻暴露出 3 个只有真机才会出现的缺陷：

| 缺陷/改动 | 证据与修法 |
| --- | --- |
| 真实 `Desktop.act` 是同步方法，执行器却 `await` 它 | 原先 6 个步骤在真机上第 2 步就 `TypeError: object dict can't be used in 'await' expression`；替身把 `act` 写成 async 把这个错完全掩掉了。现改为直接调用，并把测试里的替身 `FakeDesktop.act` 改成同步，使它不能再掩盖这类错 |
| 失败步骤留下的租约会毒化后续步骤 | `Desktop.request_control` 在已有租约时抛 `desktop_lease_active`；原实现只有 `approve`/`lock_screen` 自己 release，其它步骤中途失败后，下一步只会看到这个拒绝，真实原因被盖掉。现 `run()` 在 `finally` 里 `settle()`：只要还戴着租约就释放（释放本身报错也不影响结论） |
| 提示语与真实顺序颠倒 | 对话框类步骤的对话框是在按回车**之后**才弹出，原文却写「完成后按回车」，人会提前找不存在的按钮，到期步骤也会被误以为已开始。现按步骤分开提示（`DIALOG_INTRO`），到期步骤明确写「不要碰它，等它自己关闭后按回车」 |
| 验收确认到期报错成「用户拒绝」 | 冻结包实测：无人操作 91 秒后报 `local_user_declined` —— 实际没人点。现两个驱动都在 `confirm_acceptance` 记录 `last_approval`，CLI 区分 `local_user_timeout` 与 `local_user_declined`；X11 侧新增真实 Xvfb 用例 |
| 键名表补全 | `KEY_NAMES` 增加 `space`/`pause`/`break`：停止热键就是 `Ctrl+Alt+Pause`，原先只能在内部常量里用，外部无法按名称发送 |
| 模拟替身的实测结果（Xvfb，真实对话框/租约/注入/热键） | `approve` pass（`lease 60s`，`click_log="CLICK 240 130"`，`log_lines 2→3`）、`reject` pass（`desktop_approval_declined`、无租约）、`timeout` pass（31s、`desktop_approval_timeout`、无租约）、`typing_interference` pass（在第二个窗口真实打字后动作被拒、焦点未被抢回）、`revoke` pass（`Ctrl+Alt+Pause` → `revoked=true`、`desktop_control_not_approved`）；`lock_screen` **未模拟**（本机没有真实锁屏器，不伪造锁屏窗口）| 
| 固化为可重复回归 | `tests/cu/test_linux_desktop_live.py` 现在覆盖 6 项人工步骤中的 5 项（默认跳过，`ZOPS_LINUX_DESKTOP_LIVE=1` 时在 Xvfb 上用真实对话框跑，5 个用例共 59.6s）：`approve`（60 秒租约 + `CLICK` 到达应用）、`reject`（`desktop_approval_declined`、无租约）、`timeout`（不按键，30 秒到期报 `desktop_approval_timeout`、无租约）、`revoke`（批准后按 `Ctrl+Alt+Pause` → `emergency_stop`、租约作废）、`typing_interference`（切到第二窗口真实打字 → `user_input` 暂停、焦点未被抢回、收尾无残留租约）。只剩 `lock_screen` 没有，因为它必须由真实锁屏器证明。文件头写明：机器替身按按钮**不构成人工验收结论** |

机器替身的边界（不夸大）：它证明的是「执行器与真实对话框/租约/注入/热键链路能跑通」，不证明人在真实桌面上做同样的动作也顺利，也不代替 Spec 第 82、84 条的人工验收。`lock_screen` 必须由真实锁屏器证明，本轮不用伪造的锁屏窗口凑这一项。

### 一.11.2、冻结包重建与包内验证

`companion/dist/companion-unsigned-test` 共重建四次（本轮 macOS 接线后又做了第五次，见一.12），那四次的最终产物为 `sha256=52081378…`（`created 2026-11-01T13:10:41Z`，`test_only=true`）；第三次是对齐 `_close_dialog` 的句柄修复，第四次是对齐 `_input_desktop_name` 的句柄声明（`desktop.py` 每改一次都必须重建，包比源码旧就不能当证据）。包内实测（复验要用没被占用的显示号：上次遗留的 :96 会话会让 `--doctor` 误报 `desktop_enabled=false`，换 :191 后恢复 7/7）：

```
--version → 2.0.2
python -m companion.build --verify-release dist/companion-unsigned-test --allow-test-package → rc=0（仅测试包）
  不带 --allow-test-package 仍 rc=1（not_signed / not_verified / unsigned_test_package，符合未签名测试包预期）
--doctor（Xvfb + 本机验收记录）→ desktop_enabled=true，checks 7/7 全 true，caps 仅 desktop.inspect/request_control/act，
  unavailable.desktop.screen=desktop_capture_not_accepted_on_platform
--doctor（XDG_SESSION_TYPE=x11 但 WAYLAND_DISPLAY=wayland-9）→ desktop_enabled=false、reason=wayland_portal_not_implemented、caps 无 desktop.*
--local-desktop-test → checks 7/7 全 true，reason=probe_does_not_enable_remote_desktop
--accept-desktop 无人值守（Xvfb :97 + 可见窗口）→ 90s 后 rc=1 local_user_timeout，验收记录未写入（config.state 只有 client.lock）
linux_desktop_check.py --companion <冻结包> → auto 8 全 pass（doctor/session/x11_windows/lock_query/injection/hotkey/no_elements/tray）、
  manual 6 skip，verdict=false、rc=1；--attended 后 manual 6 全显示 attended 证据并计 fail
```

结论：包内确实包含本轮新代码（`wayland_session` 双信号、到期拒绝、验收到期与拒绝分开报）；到期不会写入验收记录，也不会默认同意。

### 一.11.3、Windows 句柄修复与入口文档

| 项 | 实现与证据 |
| --- | --- |
| 缺陷：`_close_dialog` 的 HWND 可能被截断 | `visit(hwnd, _param)` 拿到的句柄原样传给 `GetWindowThreadProcessId/GetWindowTextLengthW/GetWindowTextW/PostMessageW`；ctypes 没设 `argtypes` 时按 32 位 int 解释，64 位句柄高地址会被截断，结果「到了 30 秒也关不掉验收对话框」——用户事后仍能点「写入验收记录」，等于把已经按到期判失败的请求重新批掉。现统一 `handle = ctypes.c_void_p(hwnd)` 再传；`tests/cu/test_desktop_safety.py` `Ran 32 tests OK` 覆盖该路径（无实机，只到源码与回归层） |
| 缺陷：`OpenInputDesktop` 返回的 HDESK 可能被截断 | 同类问题：`user32.OpenInputDesktop` 没设 `restype`，默认按 `c_int` 返回，64 位句柄高地址被截断；截断后 `CloseDesktop` 关不掉原句柄（泄漏），名字查询失败又会被 `secure()` 当成“已锁屏”，桌面能力会永久不可用。现对 `OpenInputDesktop` / `GetUserObjectInformationW` / `CloseDesktop` 全部显式声明 `restype/argtypes`（句柄一律 `c_void_p`），并由 `InputDesktopHandleTest` 三条用例覆盖（正常返回 `Default`、拿不到句柄时不误关、`Winlogon` 等锁定桌面名原样上报） |
| 入口文档缺失 | `README.md`、`QUICKSTART.md` 原先完全不提 Linux 桌面验收入口，用户找不到 `scripts/linux_desktop_check.py` / `linux_desktop_attended.py`。现 README 增「本机（桌面）接管」段（默认全关、X11 only、Wayland 拒绝、四步命令），QUICKSTART 增「桌面接管验收（Linux / X11）」整节：配对与 `--allow-desktop-app`、`--accept-desktop` 的 90 秒/到期区分、6 步顺序与逐项注意（对话框在回车后才弹、`Ctrl+Alt+Pause` 立即作废、`lock_screen` 需要真实锁屏器）、清单 8 项 auto 与「缺项即未通过」、机器替身不算验收 |
| 旧解释器下的误判（排查记录，非缺陷） | 在宿主 python 3.10 上直接用库路径跑文件/工作流用例会报 `local_worker_outcome_unknown`（底层是 `AttributeError: module 'asyncio' has no attribute 'timeout'`）；走 CLI 则干净拒绝：`companion --doctor` → `python_311_required`。`companion/pyproject.toml` 已声明 `requires-python>=3.11`，冻结包内置 3.11，故不是产品缺陷；回归因此按解释器分工，入口文档也统一改用 `companion/.venv/bin/python`（本机甚至没有 `python` 命令，只有 `python3`） |

## 一.12、2026-11-01 macOS AX 桌面适配（Spec 第 76 条；代码就绪，未实机验收）

新增 `companion/ax.py`（CoreFoundation + CoreGraphics + ApplicationServices 的纯 ctypes 封装，~890 行）与 `companion/desktop.py` 的 `MacDriver`。定位与 `x11.py` 一致：这一层只做“能不能做到”的实测，符号缺失、权限未授予、返回异常一律原样报固定错误码，由预检决定禁用桌面能力。**本机不是 Mac，所以 macOS 桌面能力在本轮结束时仍是“代码就绪、未验收”，没有被标为已验收**（Spec 第 84 条同样适用）。

| 项 | 实现与证据 |
| --- | --- |
| 窗口枚举与“普通窗口”判定 | `CGWindowListCopyWindowInfo(OnScreenOnly\|ExcludeDesktopElements)`，只收 `kCGWindowLayer==0`、有 `kCGWindowNumber`/`kCGWindowOwnerPID`、有标题且能取到 bounds 的窗口；Dock、壁纸、面板因此天然不在列表里。`AXWindows` + `AXUIElementGetWindow` 把 CGWindowID 映回 AX 窗口元素（数组元素归数组所有，`CFAutorelease` 交回收周期） |
| 控件树 | 只收 `ACTIONABLE_ROLES`（AXButton/AXMenuItem/AXCheckBox/AXRadioButton/AXLink/AXTabButton/AXRow/AXCell）且有 `AXTitle`/`AXDescription`、未 `AXHidden` 的控件；深度≤6、子节点≤200、总数≤80，顺序为文档序深度优先。`invoke_element` 要求下标+角色+名称三项全对再发 `AXPress`，不回落坐标点击；被拒报 `desktop_element_not_actionable` |
| 解析不到 AX 不算“没有控件” | `_collect` 拿不到 AX 窗口元素时抛 `desktop_window_ax_unavailable`（本轮自审发现并修掉的误报：否则权限缺失会被报成“这个应用没有可点的东西”，验收与远端列表都被假空结果误导） |
| 密码框识别（Linux 做不到那一项） | `AXFocusedUIElement` 的 `AXSubrole == AXSecureTextField` 即敏感；因此 macOS 可声明 `desktop.screen`（`CAPABILITIES_BY_PLATFORM["darwin"] = DESKTOP_CAPABILITIES`），Linux 仍只有前三项。焦点读取异常按敏感处理（fail closed） |
| 窗口采集 | `CGWindowListCreateImage`（只取窗口自己的 rect，IgnoreFraming），仅接受 `bitsPerPixel==32`，字节不足或布局未知一律 `desktop_capture_unavailable`；`encode_frame` 按 `FRAME_BYTES` 缩质降级，驱动侧再核一次上限，超限报 `desktop_capture_too_large`。屏幕录制权限用 `CGPreflightScreenCaptureAccess`（10.15+；老系统符号缺失时不声称已授予，交给实际采集） |
| 锁屏与空闲 | `CGSessionCopyCurrentDictionary` 的 `CGSSessionScreenIsLocked`；`lock_state()` 区分“查到了/没锁屏/查不到”，查不到时预检报 `lock_query_unavailable`（不把 API 故障当成用户在场）。空闲哨兵用 `CGEventSourceSecondsSinceLastEventType(kCGAnyInputEventType)`，与 X11 的 XScreenSaver 同一基线语义 |
| 全局停止热键 | Mac 键盘没有 Pause/Break，按平台适配为 `Ctrl+Option+Cmd+S`：listen-only `CGEventTapCreate` + 独立 `CFRunLoop`，只观察不改写用户输入；建不起来或 runloop 起不来 → `emergency_hotkey_unavailable` → 桌面接管整体禁用（Spec 第 53 条）。文案改由驱动 `hotkey_text` 提供，托盘菜单/状态/批准对话框随之切换，Linux 仍为 `Ctrl+Alt+Pause` |
| 权限申请引导 | 辅助功能未授权 → `macos_accessibility_permission_required`；屏幕录制未授权 → `macos_screen_capture_permission_required`；都是固定错误码，远端与文档能给出“系统设置 → 隐私与安全性”的可执行指引 |
| 安全界面黑名单 | `MACOS_SECURITY_PROCESSES`（loginwindow/securityagent/authorizationhost/coreauthd/coreautha/keychainaccessd/opendirectoryd/screencaptureui）；`Desktop._forbidden` 同时合并三平台黑名单，驱动层与 Desktop 层双重过滤 |
| 共用原生层（结构改动） | X11 与 macOS 完全一致的部分（tkinter 批准与验收对话框、托盘、热键轮询线程、窗口规范化与过滤、租约状态、输入基线、`foreground/focus/perform`、`close`）抽为 `NativeDriverBase`，两个驱动只留平台相关的 `prepare/_preflight` 与能力差异，避免“两份实现逐轮漂移”。这是安全关键重构，因此包内 Linux 行为重新复验（见下） |
| 平台判定修正 | `platform_key()` 原读 `os.name`，而 macOS 上 `os.name` 也是 `posix`——会把 Mac 当 Linux X11 驱动（能力表、验收记录、驱动选择全部张冠李戴）。现改读 `sys.platform`，`UNAVAILABLE = _reasons(platform_key())`；`acceptance_platform("darwin") → ("macos", ACCEPTANCE_CHECKS_MACOS)`，三平台验收记录互不冒充 |
| CLI | `--accept-desktop` 增 macos 分支：`capture_probe()` 真采一帧 + `ax_app_report()` 只报控件数量（与 Windows `uia_apps` 同形状，沿用同一记录校验）；`--local-desktop-test` 的 `hotkey` 字段按驱动报告 |
| 测试 | `tests/cu/test_macos_ax.py` 19 项（键码表覆盖 `_validate_keys` 放行的每一个键——本机就是靠它抓出缺 `m`/`n`；UTF-16 计数；帧编码/降级/短字节拒绝；CGRect 必须 double；CFMachPort 原型不得挂在 CoreGraphics 上；控件树过滤/三重核对/限界）、`tests/cu/test_desktop_macos.py` 33 项（闸门与逐项失败原因码、验收记录隔离、窗口与进程授权、锁屏 fail closed、焦点与注入转发、坐标越界、能力表、Desktop 接线启用与伪造记录拒绝） |

回归与包内复验（第五次→第六次重建）：`companion/.venv/bin/python -m unittest` 15 个模块 **Ran 302 tests OK (skipped=5)**（原 250 + macOS 两模块 52）；系统 python3 4 模块 **Ran 72 tests OK**；`tests/p3` **10/10**。第五次产物 `sha256=8ba6e5ba…`（`created 2026-11-01T15:17:25Z`）；本轮又改了 `companion/build.py`（见下表“发布措辞修正”），而 `companion.spec` 用的是 `collect_submodules("companion")`——`build.py` 确实被打进包内，所以按“包比源码旧就不能当证据”的纪律做了第六次重建：`sha256=ce9dd3a417b02c3c…`（`created 2026-11-01T15:30:53Z`，`--verify-release --allow-test-package` rc=0 且输出已改为“未签名测试包…不得发布”，不带 `--allow-test-package` 仍 rc=1）；`find companion -maxdepth 1 -name '*.py' -newer <receipt>` 为空，确认包不比源码旧。

包内 Linux 行为复验（重构后必须证明没变）：第六次包（Xvfb :191）`--doctor` 7/7、caps 仍三项、`unavailable.desktop.screen=desktop_capture_not_accepted_on_platform`；Wayland 双信号仍 `wayland_portal_not_implemented`；`--local-desktop-test` hotkey 仍 `Ctrl+Alt+Pause`、reason 仍 `probe_does_not_enable_remote_desktop`。第七次包（加了 portal 层之后）再把时间敏感的两项重跑一遍（Xvfb :195，避免复用旧显示号上遗留的会话）：`--accept-desktop` 无人值守 91 秒后 rc=1 `local_user_timeout` 且不写记录（`config.state` 只有 `client.lock`）；`linux_desktop_check.py --companion <冻结包>` auto 8 pass（doctor/session/x11_windows/lock_query/injection/hotkey/no_elements/tray）/ manual 6 skip、`verdict.accepted=false`、rc=1（输出 `started=2026-11-01T16:02:34Z display=:195`）。也就是说：新加的探测层没有改变 X11 的任何行为，也没有让无人值守时自动获得授权。

| 项 | 实现与证据 |
| --- | --- |
| 发布措辞修正（本轮改 `build.py` 抓出的真缺陷） | `--verify-release --allow-test-package` 以前只要回执没报错就打印“产物可发布”，把一个 `test_only=true` 的未签名包说成了正式发布物（用户照着这个输出就可能发给别人）。现在 `read_test_flag()` 单独读回执的 `test_only`，测试包走“产物核验通过（未签名测试包）… 不得发布、不得作为更新源、不得安装给他人”文案， rc 仍为 0（工具链自检用）；严格核验不变 |

**未做的事**：没有把任何 macOS 项标成已验收；没有 Mac 实机，`--accept-desktop` 与 AX 真实调用尚未跑过。（Wayland Portal 已在下一节实现，见一.13——本节写完时它仍是空白。）

## 一.13、2026-11-01 Wayland xdg-desktop-portal 探测层（Spec 第 76 条最后一项；结论是“不开桌面能力”，但原因可区分）

新增 `companion/portal.py`（用 jeepney 纯 Python D-Bus，含在冻结包内）。先把结论说清：**Wayland 会话仍然不声明任何 `desktop.*` 能力**，这一层做的不是“能操作了”，而是把“为什么不能”从一句《未适配》拆成可验证的事实。

为什么不能直接开：RemoteDesktop v2（官方 XML：CreateSession/SelectDevices/Start + Notify*/ConnectToEIS）确实能做输入，但 portal 只按“整个桌面会话”授权，**给不出“哪个窗口在前台/哪个窗口被接管”的作用域**；Screenshot 只有整屏/用户手选/活动窗口三种目标，没有“只截被接管窗口”的稳定通道；RemoteDesktop 也没有窗口枚举接口。批准必须绑定设备/任务/应用窗口/动作范围/有效期（Spec 第 50 条）、每次输入前还要复核目标窗口（第 51 条）——缺了窗口作用域就只能要么盲猜要么整屏接管，两者都不能开。（写完本节后的后续：把 ConnectToEIS 后面的输入传输层——EIS/libei 协议——真正写出来了，见一.14；结论没变，但现在有协议级证据：**EIS 里也没有“窗口”对象，设备只带一个屏幕 `region`**。）

| 项 | 实现与证据 |
| --- | --- |
| 真实能力探测 | `Portal.probe()` 读 `org.freedesktop.portal.Desktop` 的 NameHasOwner、RemoteDesktop `version`/`SupportedMethods`、Screenshot `version`/`AvailableTargets`，并查 logind 座位的 `GetIdleHint`/`GetIdleSinceHint`；属性读不到一律 None（不拿 0 冒充“读到”） |
| 原因码可区分 | `block_reason()` 分别返回 `wayland_dbus_session_missing` / `wayland_portal_binding_unavailable` / `wayland_portal_bus_unreachable` / `wayland_portal_not_running` / `wayland_remote_desktop_unavailable` / `wayland_eis_channel_missing` / `wayland_window_scope_not_supported`；探测层自己出错才回到兜底 `wayland_portal_not_implemented` |
| fail-closed | `window_query`/`window_scoped_capture`/`window_scoped_input` 写死为 False，不是“查不到当可用”；logind 查不到时 `lock_query`/`idle_query` 记 False；`_wayland_reason()` 只透传 `wayland_*` 形式的码，其它内容（比如异常里带出的路径）一律换回兜底码 |
| 平台接线 | `Desktop.prepare()` 的 Wayland 分支改为 `await asyncio.to_thread(_wayland_reason)`，四个 `desktop.*` 同一个精确码，依旧 `enabled=false`、`capabilities=[]`、不创建驱动 |
| 打包 | `companion.spec` 在 Linux 下显式增加 `jeepney.io.blocking/bus_messages/wrappers` 为 hiddenimports（原本只是 keyring→SecretStorage 的传递依赖，不能依赖碰运气）；Analysis toc 内 jeepney 命中 23 次 |
| 测试 | `tests/cu/test_companion_portal.py` 15 项：假连接验证闸门与错误码（包含“没有 portal 时不能当成可用”“未知属性只能报错不能当成 0”），另有 **真 `dbus-daemon` 会话总线集成用例**（ stub portal 服务 + 真客户端编解码）；加 EIS 通道码后为 19 项（含真总线上“有通道/无通道”两条分支） |

包内实证据（第七次重建 `sha256=31b446c559de0b04…`，`created 2026-11-01T15:56:28Z`）：在 `dbus-run-session` 里拉起 stub portal 后跑**冻结包二进制** `--doctor`（`XDG_SESSION_TYPE=wayland WAYLAND_DISPLAY=wayland-0`）→ `desktop_enabled=false`、`reason=wayland_window_scope_not_supported`、caps 无 `desktop.*`；stub 侧日志逐条列出确实收到了 4 次属性读（RemoteDesktop version=2 / SupportedMethods=[CreateSession, SelectDevices, Start]、Screenshot version=3 / AvailableTargets=11）——证明探测不是字符串判断，而是真走了总线。本机无会话总线时同一包报 `wayland_dbus_session_missing`；X11 行为不变（doctor 7/7、caps 三项、`desktop_capture_not_accepted_on_platform`、hotkey 仍 `Ctrl+Alt+Pause`）。

回归：venv 3.11 十六个模块 **Ran 320 tests OK (skipped=5)**；系统 python3.10 四模块 **72 tests OK**；`tests/p3` **10/10**（含子集 25/25）。README/QUICKSTART 的 Wayland 段已按精确原因码重写。

**未做的事**：没有因为“portal 能连上”就声明任何桌面能力；本机无 xdg-desktop-portal，探测层在真机上的行为仍待在真实 Wayland 桌面上跑 `--doctor` 复核。本层写时 EIS/libei 输入传输层还未实现（下一节做了，见一.14）。

## 一.14、2026-11-02 EIS（libei）输入传输层 + 包内自检（Spec 第 76 条收尾；结论仍是“不开桌面能力”，但从推测变成协议事实）

一.13 只做到“把 portal 问清楚”，当时写的“要真正做完整必须再上 libei/EIS”是推测。本轮先拿规范，再把传输层真写出来：

| 事实来源 | 拿到什么 |
| --- | --- |
| `org.freedesktop.portal.InputCapture` v2 官方接口文档 | `SupportedCapabilities(u)` 位掩码（1=KEYBOARD/2=POINTER/4=TOUCHSCREEN）、`version`、`CreateSession2`、`Start(session, parent_window, {capabilities,restore_token,persist_mode})`、`GetZones()→(zones a(uuii), zone_set u)`、`SetPointerBarriers`、`Enable/Disable/Release(activation_id, cursor_position)`、`Activated/Deactivated/Disabled/ZonesChanged` 信号，以及 **`ConnectToEIS(session,options)→fd h`** |
| 同一文档对事件传输的说明 | portal 只管“何时可以捕获”，**输入事件本身由 libei 经 `ConnectToEIS` 交出的 fd 承载**（RemoteDesktop 侧同样有 `ConnectToEIS`） |
| libei 仓库 `proto/protocol.xml`（126 KB）+ `doc/protocol/{overview,initial-handshake,specification,types}.md` | EI 是**二进制协议**（不是 JSON）：头 16 字节 = `uint64 object_id + uint32 length(含头) + uint32 opcode`；参数 4 字节对齐；string = 长度(含 NUL)+内容+补零（`NULL`→4 字节、`""`→8 字节、`"hello"`→12 字节）；`fd` 在消息里占 0 字节、走 SCM_RIGHTS；整数用 EIS 实现（合成器）原生字节序；opcode = XML 声明顺序；客户端 id 从 1 且 < `0xff00000000000000`，对端 id ≥ 该值；后续消息必须回填 `last_serial` |
| 同上（安全语义） | **能力掩码不是常量**，由合成器在 `ei_seat.capability(mask, interface)` 逐个公布（文档举例 pointer 0x1/keyboard 0x4/touch 0x8，但明说“实现自选”）；`ei_device.ready` 是 v3 对 `done` 的强制响应；新公布的设备处于 paused，未 resumed 就发输入属 client bug；`frame` 才是硬件事务边界（时间戳必须 CLOCK_MONOTONIC 微秒）；协议里设备只有 `region`（屏幕区域），**没有任何窗口对象** |

| 项 | 实现与证据 |
| --- | --- |
| 协议客户端 | 新增 `companion/eis.py`（纯标准库）：`encode_message/encode_string/decode_string/unpack_fds` + `EIS` 类（`connect/handshake/wait_seat/bind/wait_devices/wait_resumed/pump/start_emulating/stop_emulating/_emit/key/button/motion/motion_absolute/scroll/frame/click/state/close`），申报 12 个客户端接口（`ei_connection/callback/pingpong/seat(v2)/device(v3)/pointer/pointer_absolute/scroll/button/keyboard/text/touchscreen(v2)`），固定错误码 22 个（`eis_timeout/eis_handshake_unexpected/eis_protocol_violation/eis_no_seat/eis_capability_unavailable/eis_device_paused/eis_not_emulating/eis_region_unknown/eis_out_of_region/eis_connection_closed/eis_unknown_object` 等） |
| 三道闸门（安全模型要求，不是可选） | 未 `start_emulating` 发输入 → `eis_not_emulating`；设备被 `paused` → 停注入并回 `eis_device_paused`；绝对指针 region 未知 → `eis_region_unknown`，越界 → `eis_out_of_region`；`disconnected` 后一律 `eis_connection_closed`，不再假装还连着 |
| 不硬编码能力 | `bind()` 的掩码只能 OR 合成器公布的值；用例故意给 `0x400/0x2/0x20` → 必须回 `0x402`；给未公布的接口 → `eis_capability_unavailable` |
| `ready` 的时机 | 规范要求 v3 收到 `done` 必须回 `ready`，因此放进 `_track` 的 `done` 分支（原先放在 `wait_devices` 里会让时序不确定，测试立刻抓出） |
| 包内自检 | 新增 `companion/selfcheck.py` + CLI `--eis-selfcheck`：按规范**独立实现**的假合成器（自己 `struct` 拼包拆包，不与客户端共用编码器）跑在真 `socketpair` 上；8 步（握手/seat/设备/闸门拒注/输入与 frame/窗口作用域必须为假/paused 停注/绝对指针前置条件）零失败且跑齐才 rc=0——否则冻结包在交付机器上无法自证“包里那份 EIS 真能跑”（试过 `--inspect`/`-c`：参数被我们自己的 argparse 拒掉，这个“不能任意执行”的约束保留） |
| portal 侧接线 | `probe()` 新增 `ic_version/ic_methods/ic_capabilities`（InputCapture v2 的 `SupportedCapabilities` 与方法清单）；`block_reason()` 新增 `wayland_eis_channel_missing`：只在**真读到方法清单且两侧都没 `ConnectToEIS`** 时才这么断，读不到清单不断言（保守回落到 `wayland_window_scope_not_supported`）；`desktop.py::_wayland_reason` 原样透传新码 |
| 构建即预检 | `companion/build.py` 新增 `run_selfcheck()` 并接到 `finish()`：**写完回执前拿刚构出来的二进制跑 `--eis-selfcheck`**，读不到/跑不齐/报错/超时/自称拿到窗口作用域，一律不写回执、不改名、不交付（rc=1）；跳过它必须显式给 `--skip-native-selfcheck`，代价是“该包算未预检”（写进 help）。理由：交付机上没有仓库源码也没有单测，“`collect_submodules` 收进去了”不等于“能跑”；`build.py` 本身也在包里，所以每次构建都得重建（本轮照这条纪律重做到第十次） |
| 测试 | `tests/cu/test_companion_eis.py` 20 项（含 golden bytes 锁死 types.md 的字符串与头布局例子）、`tests/cu/test_companion_selfcheck.py` 4 项（含“把闸门注空 → 自检必须报错”，防止自检永远报通过）、`tests/cu/test_companion_portal.py` 15→19 项（新增真 `dbus-daemon` 上“有通道/无通道”两条分支）、`tests/cu/test_companion_build.py` 28→38 项（预检闸门 9 项用真 shell 脚本当产物跑，不靠 mock；含“不通过就不出回执”与“跳过只能显式”） |
| 文档 | README/QUICKSTART 的 Wayland 段补 `wayland_eis_channel_missing` 与“EIS 里只有屏幕 region、没有窗口对象”；`desktop.py` 的兜底码注释同步 |

坑（都是本机实测出来的）：`struct.Struct` 没有 `__len__`（要 `.size`）；`bytes` 不支持 `del buf[:]`（收发两侧缓冲区都得是 `bytearray`）；本机解释器上 `socket.pack_fds/unpack_fds` 不存在（`recv_fds/send_fds` 倒是有），fd 传递一律用 `recvmsg/sendmsg + CMSG_SPACE + 自写 unpack_fds`，并把这一条写进注释——冻结包用的就是同一解释器，写在那里能跑才算能跑。

包内实证据（第十次重建 `sha256=3bb47747033dae18…`，`created 2026-11-01T17:19:23Z`，`test_only=true`，`--version` → 2.0.2；第八次 `d0738acc…`/17:01:18Z 只多了 EIS 客户端本身，第九次 `6239bb7f…`/17:10:34Z 加了包内自检，第十次把预检接进构建闸门后又重做了一次）：
- **闸门被真验过两面**：正向——第十次构建时自检在包内二进制上跑过才写出回执；负向——同一次实验里把 `run_selfcheck` 换成失败后重跑构建，输出 `包内自检未通过：selfcheck_rc_1；不写回执、不交付。` 且 rc=1、目录里真的没有 `build-receipt.json`（那次实验把产物覆盖掉了，所以随后又跑了一次正常重建，上表的 sha 就是这一次的）。
- 冻结包 `--eis-selfcheck` → 8 步全过、`failed=[]`、`window_scoped=false`、`messages=23`、rc=0；PYZ 目录内 `companion` 模块 24 个，含 `companion.eis` 与 `companion.portal`（`collect_submodules("companion")` 自动收录，无新增依赖）。
- X11 行为未变（Xvfb :197）：`--doctor` caps 仍 `process.list/service.list/system.info` 三项、四个 `desktop.*` 仍 `linux_x11_wayland_desktop_not_accepted`、`headed_login/attach_user_browser` 照旧、`desktop.hotkey` 仍 `Ctrl+Alt+Pause`、`desktop.reason=x11_no_visible_windows`（该显示上没挂验收窗口，如实报未知）。
- Wayland 两条精确码（`dbus-run-session` + stub portal，跑**包内二进制** `--doctor`）：两侧方法清单都没 `ConnectToEIS` → `reason=wayland_eis_channel_missing`；RDP 与 InputCapture 都报出 `ConnectToEIS` → `reason=wayland_window_scope_not_supported`；两种情况下 `desktop_enabled=false`、caps 都不含任何 `desktop.*`；stub 侧日志逐条列出真收到的 7 次属性读（含 InputCapture version=2 / SupportedCapabilities=3）。
- 时间敏感的两项在第十次包上重跑（Xvfb :197）：`--accept-desktop` 无人值守 91 秒后 rc=1 `local_user_timeout`、不写验收记录（`config.state` 只有 `client.lock`）；`linux_desktop_check.py --companion <冻结包>` auto 8 pass（doctor/session/x11_windows/lock_query/injection/hotkey/no_elements/tray）/ manual 6 skip、`verdict.accepted=false`、rc=1（`started=2026-11-01T17:21:43Z display=:197`；第九次包同两项分别为 `started=2026-11-01T17:13:29Z`与同为 91s/`local_user_timeout`）。也就是说：新增的 EIS 层、自检入口与构建闸门没有改变 X11 的任何行为，也没有让无人值守时自动获得授权。

**结论没变，但依据变了**：Wayland 仍不声明任何 `desktop.*`。EIS 与 portal 都给不出“哪个窗口”的作用域（协议里设备只带屏幕 `region`），而批准必须绑定应用窗口（Spec 第 50/51 条），所以要么盲注要么整屏接管，两者都不开。要开放还差真实合成器（GNOME/KDE + portal 的 EIS 支持）上的实机验收，属 Spec 第 89 条的明确边界。

**未做的事**：没有在没有合成器的机器上假装 Wayland 已可用（自检只证明线路格式与闸门）；没实现 region 之外的 zone/barrier 语义（`GetZones`/`SetPointerBarriers` 目前只作为 portal 侧事实探测，未接进注入路径）；`selfcheck` 的假合成器不是真 EIS 实现，不能替代真机验收。

回归：venv 3.11 **十八个模块 Ran 358 tests OK (skipped=5)**（15→18：新增 EIS 20 项、自检 4 项，portal 15→19 项，构建 28→38 项）；系统 python3.10 四模块 **72 tests OK**；`tests/p3` 两个 CU 模块 **2/2 OK**。那四个模块在 venv 下报 `ModuleNotFoundError: No module named 'httpx'`（它们本来就跑系统 3.10，不是回归），已核实不误读为失败。入口文档：README 补“构建即预检”一段，QUICKSTART 新增“📦 冻结包预检（构建即验）”小节（含 `--eis-selfcheck` 与 `--skip-native-selfcheck` 的代价）。

## 二、逐条对照

### 架构（第 3–15 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| `zops/companion/` 用户态组件、不默认提权、不开公网入站 | 已实现 | `companion/client.py` 主动 WSS；无监听端口 |
| Python 3.11+ 与固定依赖 | 已实现 | `companion/pyproject.toml`（websockets 15.0.1、cryptography 46.0.3、keyring 25.6.0、psutil、playwright、openpyxl、python-docx、pypdf；win32 条件依赖 pywin32/pywinauto/pystray）+ `uv.lock` |
| PyInstaller 打包、未签名只给标注测试包 | 已实现（签名待证书） | `companion/build.py`、`companion.spec`；无证书时只能 `--ack-unsigned-test` 出 `companion-unsigned-test`（`build-receipt.json` 标 `test_only=true`，严格核验 rc=1），有证书时 `--sign-thumbprint` 签名 + 强制验签通过才改名为 `companion-<version>`；25 项单测 + `--verify-release` 实跑（签名/摘要/篡改三类拒绝）；`companion-unsigned-test --version` → `2.0.2` |
| 私钥仅存系统 keyring、一次性配对、可吊销 | 已实现并验证 | `companion/identity.py` 只允许 SecretService/macOS/Windows 后端；本机无 SecretService 时实测 `Denied secure_keyring_unavailable`（无明文回退）；`tests/cu/test_devices.py::test_ticket_single_use_expiry_and_revocation`、`test_revoke_stops_socket_and_rejects_inflight_result` |
| `core/cu/` + `api/routers/cu_devices.py` | 已实现 | 目录与路由存在，设备/任务/控制接口配套测试 |
| 扩展经 Native Messaging 接入，旧扩展仅诊断 | 已实现并实机验证 | `extension/background.js` 用 `runtime.connectNative`；`/api/cu/bridge/ext|view` 只回 `legacy_disabled` 并关闭；真实 Chromium 加载扩展→`native_host` hello→弹窗显示「本机通道已连接」（见一.8） |

### 先修复现有执行边界（第 17–23 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 固定任务设备、无服务器回退、无服务器 Shell | 已实现 | `tests/cu/test_agent_companion.py::test_no_device_pauses_without_server_fallback`、`test_unsupported_shell_is_rejected_without_device_call`；`tests/p3/test_cu_login_guard.py` 源码断言无 `"bash", "-c"` |
| 设备/会话/任务隔离、代际、有效期、有界队列 | 已实现 | `test_reconnect_fences_old_generation_and_stale_disconnect`、`test_every_binding_and_digest_fails_closed`、`test_command_expiry_and_invalid_size_no_execution`、`test_ws_large_message_disconnects_and_task_unknown` |
| 扩展不枚举/接管非任务标签 | 已实现 | `extension/background.js` 无 `tabs.query/activeTab/currentWindow` 调用 |
| 快照排除密码/token/OTP，无 `skip_sanitize` | 已实现 | 源码断言无 `skip_sanitize`；`test_screenshot_task_only_ttl_size_and_redaction` |
| 登录只在本机，无密码中转 | 已实现 | `/api/cu/manual-login`、旧配对/切换接口均 410；`tests/p3/test_cu_login_guard.py` 10/10 |
| 截图预览/下载归属校验，历史文件不删除 | 已实现 | `tests/cu/test_download_guard.py` 5 项（owner-only、无法证明归属拒绝且保留） |

### 首批能力（第 25–46 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 授权目录内结构化文件/文档操作，防符号链接/junction 越界 | 已实现并实机验证 | `companion/files.py`（realpath + `follow_symlinks=False` + 禁止保护路径）；`test_workflows.py::test_files_and_document_workflow_creates_new_files_only`；本机真实运行时 `file.list`/`file.find` 在授权根内命中预期文件（见一.10） |
| 文本/CSV/XLSX/DOCX/文本 PDF，不执行宏 | 已实现并实机验证 | `tests/cu/test_documents_pdf.py` 4 项（加密 PDF 拒绝、页数上限、文本提取、未知格式拒绝）；本机真实运行时 docx 生成→回读、xlsx 生成→回读（见一.10） |
| 默认新建、原子提交、覆盖/移动/删除单独审批 | 已实现并实机验证 | `files.py` 临时文件 + `os.link` 原子发布；`test_destructive_file_actions_require_separate_approval`；本机真实运行：未授权根拒绝，`manage=true` 根上新建/移动/原子替换/删除全成功（见一.10） |
| 操作端 Playwright 独立无头浏览器、画面不可用不抢焦点 | 已实现 | `companion/browser.py`；`BrowserLivePanel` 明确「暂无已授权的任务画面」；帧 1.5s 节流、512KB 上限 |
| 需要现有 Chrome 登录态时经扩展单次授权 | 已实现并验证（本机链路） | `companion/login_state.py`（TTL 300s、origin/domain 绑定）；实机已验证交接后私有页放行且交接文件立即删除（单次消费）；真实 Chromium 加载扩展后 `chrome.cookies` → Native Messaging → 0600 交接文件 → 单次领取全部真实跑通（见一.8），仍待用户在带真实站点账号的桌面 Chrome 上验收 |
| Windows UIA、记事本/计算器/资源管理器/登记业务应用 | 已实现（待实机） | `companion/desktop.py`：`ELEMENT_TYPES` 只放行可激活控件，`inspect(window_id)` 返回 `index/role/name/enabled` 且不含文本内容，敏感名称占位 `[敏感控件]`；`act(action="invoke")` 按下标+角色+名称三重校验并只用 UIA InvokePattern（不回退坐标点击）；协议 `DESKTOP_SCOPES/DESKTOP_ELEMENT_ROLES` 同步校验；agent 支持 `desktop_inspect(window_id)` 与 `desktop_action="invoke"`；`--doctor` 现输出 `desktop.checks` 与 `desktop.uia`（实测窗口数/控件树可读）供实机复核。证据：`test_uia_elements_are_read_only_filtered_and_allowlisted`、`test_uia_invoke_requires_lease_scope_and_exact_element`、`test_admin_ceiling_desktop_requires_explicit_server_enable`（invoke 白名单）、`test_desktop_actions_require_capability_and_map_native_approval_params`（invoke 映射）、`test_status_exposes_preflight_checks_and_uia_summary`。仍无 Windows 实机：未在真实记事本/计算器/资源管理器/登记业务应用上验收 |
| 登记脚本元信息（平台/权限/目录/版本摘要/取消） | 已实现 | `companion/scripts.py` 对齐 `core/script_exec.py` 登记结构：`platform`/`permissions`/`target_dirs`/`version`/`cancel.grace_s` 校验并归一化保存，运行前按平台门控（`script_platform_unsupported`），登记时 `target_dirs` 必须落在已授权可写根内（`script_target_not_authorized`），`--doctor` 输出摘要；`test_workflows.py::test_terminal_ops_workflow_redacts_secrets_and_runs_registered_script`、`test_script_metadata_is_validated_and_platform_gated`、`test_script_platform_mismatch_is_not_executed`、`test_script_registration_requires_target_dirs_inside_granted_roots` |
| 系统信息/进程服务查询/脱敏日志登记动作 | 已实现并实机验证 | `executor.py` 能力表 + `worker.py` `system.info` 等；`computer_agent.py` 动作映射；本机真实运行 `log.collect` 对合成秘密回执 `{"bearer":1,"credential":2,"email":1}` 且正文已 `[REDACTED]`（见一.10） |
| 固定可执行文件 + 结构化 argv，禁止拼接 Shell/前缀放行 | 已实现 | `scripts.py`；登录护栏源码断言 |
| 最小环境、目录白名单、时间/输出限额、Windows Job Objects | 已实现 | `processes.py`（CreateJobObject + 限额 + TerminateJobObject，只杀任务进程） |
| 跨应用工作流：下载→处理→汇总→预览，检查点恢复 | 已实现并实机验证 | `test_workflows.py::test_web_download_to_summary_workflow_stays_in_authorized_root`；服务端 `test_write_receipt_is_checkpointed_and_reused_on_resume`；实机：下载 CSV → 读 → 汇总 xlsx → 预览 → 登记脚本复核 sha，恢复时重放被拒且回执 sha 命中（见一.5） |
| 外发/提交类副作用单独确认 | 已实现 | `test_risky_click_requires_one_time_user_approval`、`test_desktop_actions_require_capability_and_map_native_approval_params` |

### 接管与紧急停止（第 48–54 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 三层条件独立（管理员上限/业务审批/本机同意） | 已实现 | `test_admin_ceiling_desktop_requires_explicit_server_enable`、`test_desktop_optin_setting_can_only_tighten_device_capabilities` |
| 接管绑定设备/任务/窗口/范围/有效期，原生界面确认 | 已实现（Windows 代码路径） | `companion/desktop.py` 原生对话框；`test_remote_approval_fields_cannot_bypass_native_dialog`、`test_target_replaced_during_native_dialog_is_rejected` |
| 60 秒租约、不静默续期 | 已实现 | `test_lease_binds_device_task_session_window_scope_and_60_seconds` |
| 每次输入前复核窗口/租约；用户输入、窗口变化、锁屏即暂停 | 已实现 | `test_user_input_window_change_and_lock_suspend`、`test_own_injected_input_does_not_suspend_but_user_input_does`（区分自己注入的输入与真实用户输入） |
| 紧急停止后不保留永久同意，再次接管需重新本机批准 | 已实现 | `test_emergency_stop_ends_lease_but_new_native_approval_still_works` |
| 托盘停止 + Ctrl+Alt+Pause，热键失败禁用桌面接管 | 已实现（未实机） | `desktop.py` `RegisterHotKey` + pystray；`test_preflight_failure_disables_desktop`；预检逐项结果（含 hotkey/tray）现可通过 `--doctor` 的 `desktop.checks` 复核，验收器 `doctor` 步骤要求 uia/input_sentinel/focus_query/hotkey/tray 全为 true |
| 不支持 UAC 安全桌面/提权/绕过 MFA | 已实现 | `desktop.py` 安全类窗口黑名单；`test_security_and_shell_windows_are_never_targets` |

### 可靠性、取消与审计（第 56–62 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 协议全绑定 + 代际 + fencing | 已实现 | `core/cu/protocol.py`；`test_every_binding_and_digest_fails_closed` |
| 复用 task_runtime 账本 + 设备 Journal + 互斥锁 | 已实现 | `companion/locking.py`、`core/task_runtime/`；`test_pause_is_not_cancel_and_holds_device_lock` |
| 回执丢失=未知，不自动重放写操作 | 已实现 | `test_timeout_unknown_never_resend_and_no_secret_audit`、`test_unknown_result_is_not_treated_as_stopped` |
| 停止贯通 Companion/扩展/子进程，区分请求/确认/未知 | 已实现并验证 | 本轮真实链路：HTTP stop → Redis → WSS cancel → 设备回执；`tests/cu/test_chat_stop.py` 7 项 |
| 截图通道与控制分离、尺寸/频率限制、丢过时帧 | 已实现 | `MAX_FRAME=512KB`、`frame_from` 校验；代理 1.5s 节流；仅保留最新帧；控制走独立待确认通道（`test_slow_writer_timeout_detaches_without_blocking_reader`） |
| 审计脱敏 + 历史留存不自动删除 | 已实现 | `test_timeout_unknown_never_resend_and_no_secret_audit`、`test_paused_lease_expiry_and_terminal_audit_survive`；证据上限 20 张、不自动删 |

### 设置与小窗（第 64–70 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 设置页「电脑操作」：配对/吊销/版本能力/默认设备/策略/四类授权/画面 | 已实现并截图 | `ComputerUseSettings.tsx` 四节：在已选设备上执行、设备、安装与配对、后台策略与本机授权；`SettingsModal.tsx` 挂载 web 标签；真实 UI 操作截图 `screenshots/cu-settings-computer-devices-20261024.png`、`cu-settings-computer-20261024.png` |
| 当前设备无法可靠识别时要求明确选择，禁止用最后连接设备代替 | 已实现 | 设置页文案与 `get_selected_device`；`test_no_default_selection_no_fallback_and_multi_device` |
| 画面默认随小窗开启；桌面画面显式授权；证据保存另行授权 | 已实现 | `CU_PREF_DEFAULTS`（frames=True、desktop=False、persist=False）；真实桌面画面需在接管批准时勾选 `view` 范围，`desktop.screen` 只截被接管窗口（PrintWindow）、锁屏/密码框有焦点时暂停采集且不终结租约；证据保存另行授权：`test_screenshot_evidence_needs_separate_optin_and_is_bounded`、`test_desktop_frame_requires_view_scope_and_pauses_on_sensitive_focus`、`test_desktop_screen_frame_only_for_authorized_desktop_commands` |
| 小窗显示设备/执行方式/步骤/审批/暂停原因/恢复入口/停止确认/能力不足 | 已实现并截图 | `BrowserLivePanel.tsx`；`screenshots/cu-panel-execution-mode.png`、`cu-panel-stop-confirmed.png`、`cu-pause-reason-20261024.png`（真实设备暂停原因） |
| 会话/设备切换清空旧画面与临时登录态 | 已实现 | `TaskPanel` 以 `[sessionId,taskId,deviceId]` 为 key 重挂载；`ChatApp` 切会话重置绑定 |
| 视觉延续浅灰底/白卡/细边框/outline 图标 | 已实现 | 截图 |

### 分阶段交付与明确边界（第 72–76、87–90 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 阶段1 安全收口：取消回退、限制归属、修复敏感采集与截图权限、隔离测试 | 已完成 | `agents/computer_agent.py` 无服务器回退（`test_no_device_pauses_without_server_fallback`）；无服务器 Shell（`test_unsupported_shell_is_rejected_without_device_call`）；截图/下载归属校验（`test_download_guard.py`）；`tests/p3` 强制隔离 Redis |
| 阶段2 Windows 后台闭环（Companion/协议/取消/设置页/文件/脚本/独立浏览器） | 代码已交付，Windows 实机后台不抢占未验收 | 设置页与协议已上线；Linux 实机闭环已验证（一.5、一.6、一.7）；Windows 需实机验收（阻塞 1） |
| 阶段3 Windows 完整能力（本机批准/UIA/急停/登录态复用/四类工作流） | 代码与验收工具就绪，待实机 | 代码路径与单测就绪（接管与紧急停止表）；命令级验收入口 `scripts/windows_acceptance.py`（13 项，含 pid 实测）；**Windows 实机结论未产出**（Spec 第 84 条） |
| 阶段4 macOS/Linux 先验收文件、脚本和浏览器；桌面适配未验收则禁用 | Linux 文件/脚本/浏览器已验收；X11 与 macOS 代码就绪、桌面未实机验收 | Linux 文件/脚本/浏览器已实机验收（一.5）；**Linux X11 桌面后端已实现**（`companion/x11.py` + `X11Driver`：窗口枚举含 `_NET_WM_PID`/`WM_CLASS`、焦点设置与上溯、XTest 注入、XScreenSaver 空闲与屏保、`Ctrl+Alt+Pause` 独占抓取、tkinter 原生批准），在真实 Xvfb + 真实 GUI 客户端上取到真实事件；未在带真实键鼠的桌面会话做人工干扰/锁屏验收，因此本机 `--doctor` 默认仍不声明 desktop.*（需先 `--accept-desktop`）。X11 无控件树、无法识别密码框，能力表只有前三项，`desktop.screen` 以 `desktop_capture_not_accepted_on_platform` 明确标注；Wayland 会话一律拒绝。**macOS AX 已实现**（`companion/ax.py` + `MacDriver`，52 项回归，见一.12），但没有 Mac 实机，因此 macOS 桌面能力同样是“代码就绪、未验收”，不在本机声明；Wayland 已新增 portal 探测层（一.13），因 portal 给不出窗口作用域而仍不开 `desktop.*`，真正可用需 libei/EIS，仍阻塞 |
| 边界：真实桌面无法同时不影响用户，必须临时让出控制 | 已声明并实现约束 | 接管需本机原生批准 + 60 秒租约；用户输入/窗口变化/锁屏即暂停（`test_user_input_window_change_and_lock_suspend`）；小窗文案「不占用你的鼠标键盘」 |
| 边界：不承诺任意桌面应用/最小化窗口/受保护页面/所有 Wayland | 已声明 | 设置页「未列出的应用不可操作」「真实桌面画面只在接管时推送（需 view 范围，只截被接管窗口）」；`desktop.py` 安全窗口黑名单 |
| 边界：不宣称抵御已取得终端管理员权限的恶意软件 | 已声明 | 授权模型只约束远端/网页/模型越权；文档与设置页未做超出该范围的承诺 |

### 测试与发布（第 78–85 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| p3 CU 测试无导入副作用、强制隔离 Redis | 已实现（本轮加固） | unittest 化 + `guard_test_redis`；并显式固定 `config.REDIS_URL`，与 `tests/cu` 同进程同跑不再误连生产 Redis；合并运行 71 项 OK |
| `tests/cu/` 覆盖协议/安全/执行器/并发/过期/吊销/重连/未知 | 已实现 | `test_devices.py` 42 项、`test_agent_companion.py` 16 项、`test_chat_stop.py` 7 项、`test_download_guard.py` 5 项、companion 套件 87 项（0 skipped，含 12 项真实 Chromium、2 项执行器能力声明、3 项信任库回落、--doctor 预检可复核与固定错误码）、macOS 适配 52 项（`test_macos_ax.py` 19 + `test_desktop_macos.py` 33）、Wayland portal 探测 15 项（`test_companion_portal.py`，含真 `dbus-daemon` 总线集成用例）、发布核验 28 项；venv 3.11 全量 320 项 OK（skipped=5）、系统 python3.10 四模块 72 项 OK、`tests/p3` 10/10 |
| 合成秘密检查快照/网络/日志/模型输入 | 已实现 | `test_screenshot_task_only_ttl_size_and_redaction`、`test_access_log_ticket_is_redacted`、`test_hello_grants_summary_is_bounded_and_display_only` |
| 目录逃逸/恶意参数/提示注入/未授权下载 | 已实现 | `test_workflows.py::test_hostile_page_text_cannot_expand_local_capabilities`、`test_download_guard.py`、`files.py` 路径校验 |
| Windows 实机输入干扰/锁屏/急停/取消子进程 | 工具就绪，待实机 | `scripts/windows_acceptance.py` 13 项（auto 4 / measure 1 / manual 8；人工步骤无证据即未做，取消子进程改为实测 pid 消失、doctor 要求预检逐项为真）、32 项单测；模拟运行验证报告顺序与判定，**实机结论未产出**（Spec 第 84 条） |
| 浏览器 UI 实操并截图，四条端到端工作流 | 截图一半已复验（一.15），工作流三条通过/一条阻塞 | 真实浏览器实操小窗并截图（执行方式/停止确认/暂停原因），并实操「设置 → 电脑操作」截图（真实设备/能力/授权/开关往返）；2026-11-02 因浏览器 MCP 通道故障改用 Playwright 真浏览器在当前构建上重新实操取证的七张截图与逐条文案比对，见一.15；Linux 实机完成：文件与文档、终端与运维、跨应用工作流；**桌面应用工作流仍阻塞**（仅 Windows，Spec 第 84 条） |
| 前端构建后经 systemd 更新，禁止 nohup | 已实现 | `npm run build` 成功；`systemctl restart zops-backend zops-frontend zops-agent@computer_agent`，`is-active` 均 active |

## 三、阻塞清单（需外部条件）

1. **Windows 实机验收**：UIA（记事本/计算器/资源管理器/登记业务应用）、原生批准/拒绝/撤销/超时、锁屏、用户打字与切窗干扰、托盘与 `Ctrl+Alt+Pause`、取消子进程、剪贴板不变、MessageBox。
   → 工具已就绪：`scripts/windows_acceptance.py` 覆盖 13 项（auto 4 + measure 1 + manual 8），缺项、无证据、skip 一律判「未通过」，单测覆盖判定、doctor 解析与编排；批准超时已实现（对话框 30 秒无人操作自动关闭并拒绝，原因码 `desktop_approval_timeout`），该项判定标准随之改为可验证的「到期自动拒绝」。**待 Windows 实机执行**，本轮不产出验收结论。
2. **Windows 生产签名包**：证书在用户 Windows 证书库，本机没有。**且 PyInstaller 不能跨平台构建**：
   `companion/build.py` 无 `--platform` 交叉编译入参，`--sign-thumbprint` 取的又是 Windows
   证书库指纹——所以 **.exe 产物本身必须在 Windows 上生成**，不只是“缺证书”，是“连测试包都
   造不出来”。这一条已在一.16/一.17 复核。
   → 工具已就绪：`companion/build.py --sign-thumbprint <SHA1>` 按指纹签名 + 强制 `signtool verify /pa /all`，验签通过才改名为 `companion-<version>`，并写 `build-receipt.json`；`--verify-release` 复核回执/摘要/签名状态。当前目录里只有 `companion/dist/companion-unsigned-test/`（`test_only=true`，严格核验 rc=1）。
3. **macOS 实机验收 / Wayland 窗口作用域**：macOS AX 代码已写完（见一.12），但本机不是 Mac——辅助功能/屏幕录制授权真实弹窗、`Ctrl+Option+Cmd+S` 事件塔、原生批准对话框、真实锁屏（loginwindow）、AX 控件树在真实应用上的表现均无法在此验证；Wayland 侧已新增 portal 探测层（见一.13）与 EIS(libei) 输入传输层（见一.14），结论是 portal 与 EIS 都给不出窗口作用域（协议里没有窗口对象），所以桌面能力仍全关；要开放只能按整屏接管，需你接受这一前提并在真合成器上验收。
   → macOS 验收入口与 Linux/Windows 完全对称：`--accept-desktop` 的 macos 分支会真采一帧（`capture_probe`）并写入 `platform=macos` 的验收记录（三平台记录互不冒充，`acceptance_platform` 已隔离）；预检七项（`ax`/`native_dialog`/`input_sentinel`/`lock_query`/`focus_query`/`hotkey`/`capture`）任一不过即不声明 `desktop.*`。注意 Spec 第 53 条写的 `Ctrl+Alt+Pause` 在 Mac 键盘上不存在 Pause/Break 键，因此 macOS 用 `Ctrl+Option+Cmd+S`（`HOTKEY_TEXT_MAC`，驱动文案由 `hotkey_text` 参数化，Linux/Windows 仍为 `Ctrl+Alt+Pause`）——这是一处平台偏差，已显式声明而非默默替换。
4. **真实桌面 Chrome 登录态复用**：扩展→Native Host→本机 0600 交接文件→单次领取已在真实 Chromium 上跑通（见一.8）；仍需用户在**带真实站点账号的桌面 Chrome**上验收「免登录成功率」与二次验证降级提示。
5. **真实桌面画面实机验证**：采集代码（`desktop.screen` → PrintWindow、view 范围、敏感焦点暂停）已实现并有单测；需 Windows 实机验证 UWP 窗口（记事本/计算器）与锁屏/密码框暂停行为，不能以 Linux 单测代替。
   → 入口已并入 `windows_acceptance.py` 的 `uia_apps`、`lock_screen` 两项。
6. **Linux X11 真人验收**：`companion/x11.py`（libX11/libXtst/libXss，纯 ctypes）+ `X11Driver` + `scripts/x11_target_window.py`（补 `_NET_WM_PID`/`WM_CLASS` 的真实验收窗口）+ `scripts/linux_desktop_check.py`（14 项清单：auto 8 / manual 6）+ `scripts/linux_desktop_attended.py`（把 6 项人工步骤接到真实对话框/租约/热键/锁屏/输入探测，输出可被 `--attended` 消费的证据）均已在真实 Xvfb + 真实 GUI 客户端上取到真实事件。**剩余条件**：一台带真实键鼠与 X11 会话的桌面 Linux（Wayland 会话不支持）与真实锁屏器（`lock_screen` 一项必须由真实锁屏证明），人在机旁执行 `python scripts/linux_desktop_attended.py --config <cfg>`，再 `python scripts/linux_desktop_check.py --attended <json>`。

恢复条件（缺一不可，具备后从对应用例继续）：

| 阻塞 | 需要的外部条件 | 恢复后的首个动作 |
| --- | --- | --- |
| 1、5 | 一台可安装普通桌面用户的 Windows 10/11 实机（可从 Linux 访问或人工代跑），并配好系统 keyring | `companion-unsigned-test.exe --pair <配对码> --init` → `--accept-desktop`（预检 + 原生确认，写本机验收记录）→ `companion-unsigned-test.exe --windows-acceptance`（13 项全 pass 才算验收通过；验收器已随包，测试机不需要装 Python） |
| 2 | 证书留在 Windows 证书库（给出指纹）+ 可访问 RFC3161 时间戳 | `python -m companion.build --sign-thumbprint <SHA1>`，再 `python -m companion.build --verify-release dist/companion-<version>` 必须 rc=0 |
| 6 | 带真实键鼠的 X11 桌面 Linux（Wayland 会话不支持） | `--allow-desktop-app <进程>` → `--accept-desktop`（X11 预检 + 原生确认）→ `python scripts/linux_desktop_check.py`（14 项全 pass 才算验收通过） |
| 3（macOS） | 一台 macOS 主机（普通桌面用户，需在系统设置→隐私与安全性里授予辅助功能与屏幕录制） | `companion --pair <配对码> --init` → `--accept-desktop`（macOS 七项预检 + 原生确认 + 真采一帧，写 `platform=macos` 记录）→ 机旁验证批准/拒绝/到期、`Ctrl+Option+Cmd+S` 急停、真实锁屏暂停；未验收前保持 `desktop.*` 不声明 |
| 3（Wayland） | 一台真实 Wayland 桌面（GNOME 46+/KDE 6.x）验证探测层与 EIS 在真 portal/真合成器上的行为；**并需用户先定“整屏接管能否接受”** | EIS(libei) 输入传输层已实现且有包内自检（一.14），所以剩下的不是“再写代码”：portal 与 EIS 协议里都**没有窗口对象**（只有整屏授权与屏幕 `region`），因此 `desktop.*` 仍不声明。真机上先跑 `--doctor` 核对精确原因码（`wayland_eis_channel_missing` / `wayland_window_scope_not_supported` / `wayland_dbus_session_missing`，而不是“未适配”兜底）+ `--eis-selfcheck`；若要按整屏接管开放，必须你明确接受“无法限定到单个窗口”这个前提，再做真合成器验收才能写验收记录 |
| 4 | 用户自己的桌面 Chrome + 目标站点真实账号 | 扩展弹窗授权 → 交接 → 打开目标站点判定是否免登录，并把任务/会话 id 记入验收记录 |

## 四、本轮验证命令与结果

```
python3 -m unittest tests.cu.test_devices tests.cu.test_agent_companion tests.cu.test_chat_stop tests.cu.test_download_guard
→ Ran 64 tests ... OK

companion/.venv/bin/python -m unittest tests.cu.test_companion_protocol ... test_workflows
→ Ran 65 tests ... OK (skipped=7)

python3 -m unittest tests.p3.test_cu_login_guard tests.p3.test_cu_bridge
→ Ran 2 tests ... OK（内部断言 10/10、21/21）

web-next: npx tsc --noEmit → 0；npm run build → 成功
systemctl is-active zops-backend zops-frontend zops-agent@computer_agent → active

真实链路（脚本已清理）：HTTP 200 {"cu":{"state":"cancelled","confirmed":true,"unknown":false},
"note":"...本机执行端已确认停止..."}；设备任务终态 cancelled/device_confirmed
真实浏览器：执行方式「不聚焦任务窗口（非完整后台）」；停止后「状态：已停止 / 已确认停止」
```

### 2026-10-24 实机验收输出（临时验收脚本已删除，设备与 Redis 键已清理）

```
# 真实 Companion（cpcnet + gnome-keyring + Ed25519）
DEVICE_ONLINE dev_… 2.0.2；CAPS 19 项（system/process/service/file/document/log/script）
PASS real Companion paired, executed file/system.info, and confirmed stop

# 真实 headless Chromium
PASS real headless browser, login reuse, download and login-wall pause verified
LOGIN_WALL paused device_confirmed sensitive_or_login_page；STOP cancelled device_confirmed
ORIGIN_BLOCKED rejected operation_not_authorized_or_invalid（任务仍 running，无服务器回退）

# 真实文件/文档
PASS real files/documents: xlsx/docx/txt, no-overwrite, atomic replace, escape blocked, redacted log
LOG_REDACTED {"bearer": 1, "credential": 2, "email": 1}

# 真实运维 + 跨应用工作流
SCRIPT_RUN size=19B sha256=f593d5a1…；SERVICE_LIST 0 service_query_unavailable
PASS real ops (system/process/service/registered script) and download->summary->preview->resume workflow
RESUME_SKIPPED_WRITE 回执 sha 命中, 未重放写操作

# 小窗 UI（真实浏览器操作 + 截图）
UI_OK screenshots/cu-pause-reason-20261024.png
原因：页面需要登录或包含敏感输入，已在采集前暂停；执行方式：后台执行

# 真实 WSS 生命周期（两台真实 Companion）
PASS 42/42：并发执行、跨设备/跨用户隔离、断连 unknown 不重放、重连 generation/fence 轮换、旧代际 claim 拒绝、吊销断连与重连失败

# 设置页「电脑操作」（真实 Chromium 操作 + 截图）
UI name = 设计工作站；在线 · 已选择
UI capabilities = 能力：document.extract、document.write_new、file.find、file.list、file.read、file.write_new、log.collect、process.list、service.list、system.info（能力不等于授权）
UI grants = 授权目录：work（读新建）；授权站点/脚本/应用/Native origin = 无
SWITCH 任务画面推送 已开启 -> 已关闭 -> 已开启 | prefs {"push_frames": true, "desktop_optin": false, "persist_shots": false}
PASS 设置页电脑操作：真实设备在线、已选择、能力/授权只读展示、画面推送开关往返生效
SCREENSHOT screenshots/cu-settings-computer-devices-20261024.png 152153
SCREENSHOT screenshots/cu-settings-computer-20261024.png 169774

# 打包产物（PyInstaller，无需系统 Python）
companion-unsigned-test --version → 2.0.2
companion-unsigned-test --doctor → build=unsigned_test_only；desktop_enabled=false；
desktop.* = linux_x11_wayland_desktop_not_accepted；未授权时 capabilities 仅 system/process/service

# 扩展 ↔ Native Messaging（真实 Chromium + 真实 native_host，Xvfb）
DEVICE dev_...（一次性配对后已清理）
EXTENSION pgakbhachalajnahghhobbckdnkhgide expected pgakbhachalajnahghhobbckdnkhgide match True
POPUP_STATUS 本机通道已连接 | Companion 本机通道已连接；扩展只显示状态，任务由 Companion 执行
POPUP_GUARD 请先切到要授权的 http(s) 站点标签
HANDOFF_RESULT {"ok": true, "ui": "已交给本机 Companion：1 个 Cookie，5 分钟内单次使用。"}
HANDOFF_FILE mode=600 size=190 origin=http://127.0.0.1:8123 cookies=1 single_use=True
PASS 扩展 Native Messaging：真实 Chromium ↔ native_host hello、真实 Cookie 交接、单次领取、弹窗拒绝非任务标签
SCREENSHOT screenshots/cu-extension-native-popup-20261024.png
SCREENSHOT screenshots/cu-extension-native-guard-20261024.png
SCREENSHOT screenshots/cu-extension-native-handoff-20261024.png

# 扩展 Node 回归
node --test extension/background.test.cjs → tests 20, pass 20, fail 0
node --test extension/frontend-contract.test.cjs → pass 12, fail 0
```

### 2026-10-25 本轮复跑（仅取当前工作区）

```
# companion 套件（含 12 项真实 Chromium，按锁定的 chromium_headless_shell-1194）
companion/.venv/bin/python -m unittest tests.cu.test_companion_browser tests.cu.test_companion_local \
  tests.cu.test_companion_protocol tests.cu.test_desktop_safety tests.cu.test_workflows \
  tests.cu.test_documents_pdf tests.cu.test_companion_cli
→ Ran 85 tests ... OK

python3 -m unittest tests.cu.test_devices tests.cu.test_agent_companion      → Ran 58 tests ... OK
python3 -m unittest tests.cu.test_chat_stop tests.cu.test_download_guard     → Ran 12 tests ... OK
python3 -m unittest tests.p3.test_cu_bridge tests.p3.test_cu_login_guard     → Ran 2 tests ... OK
node --test extension/background.test.cjs (tests 20) / frontend-contract.test.cjs (12) → 全过

# 重新构建未签名测试包并实测浏览器运行时
companion/.venv/bin/python companion/build.py --ack-unsigned-test
→ 构建成功；提示「浏览器运行时不在包内：… chromium-headless-shell（revision 1194）… 必须以普通桌面用户运行」
companion-unsigned-test --version → 2.0.2
cpcnet + PLAYWRIGHT_BROWSERS_PATH=/opt/zops-pw-browsers --doctor
→ capabilities: browser.act, browser.goto, browser.screenshot, browser.scroll, browser.snapshot, process.list, service.list, system.info
→ desktop.* = linux_x11_wayland_desktop_not_accepted；desktop_enabled=false
root --doctor → unavailable.browser = run_as_normal_desktop_user_not_root

# 设置页「电脑操作」重新验收（改文案 + 重新构建后）
UI_CHECK {"电脑操作标签": true, "view 范围说明": true, "只截被接管窗口": true,
  "敏感输入暂停采集": true, "设备区": true, "配对区": true, "策略区": true}
SCREENSHOT screenshots/cu-settings-view-scope-20261025.png 219506

systemctl restart zops-agent@computer_agent → active；zops-backend / zops-frontend 均 active

# 当前代码的真实设备端到端复跑（设备与临时目录已清理；Companion 未设 SSL_CERT_FILE；任务审计事件按规格保留）
# 固定入口（需 root；未设 ZOPS_CU_E2E=1 会直接拒绝运行）：
ZOPS_CU_E2E=1 python3 scripts/cu_e2e_device_check.py
PASS 一次性配对码（真实 HTTPS + Bearer）
PASS Companion 初始化本机私有配置 / 写入授权目录与站点 origin
PASS 一次性配对（Ed25519 公钥入库、私钥进 keyring）
PASS 真实 WSS 上线（challenge→ticket→hello）· 能力 20 项（两个授权根）
PASS 任务开始（running）/ 同设备第二个任务被拒（资源锁）
PASS browser.goto（execution_mode=background）/ browser.snapshot / browser.screenshot 1024x720
PASS 任务专属帧缓存（1024x720、15880B）/ 停止后帧清理
PASS browser.download → work/Z-OPS下载/report.csv（sha256 回传）
PASS file.read 读回 CSV（2026-09,128）
PASS document.write_new 生成 summary.xlsx（4916B）/ document.extract 回读「合计 384」
PASS document.write_new 生成 notes.docx / document.extract 读回 docx 文本
PASS file.list 列出授权根 / file.find *.xlsx 命中 summary.xlsx
PASS log.collect 脱敏合成秘密（bearer 1 / credential 2 / email 1，正文 [REDACTED]）
PASS manage 根内新建 → move → replace → delete 均成功（未授权根仍拒绝）
PASS 重复写入被拒（不覆盖）/ 未授权 manage 时删除 capability_unavailable
PASS 停止由执行端确认 cancelled/device_confirmed / 执行方式保留
PASS 取消后同设备新任务 running + system.info ok
PASS script.run 登记脚本 probe → exit_code=0 / stdout [--args=e2e]
PASS 未登记脚本 rejected / 未声明参数（shell）capability_unavailable
PASS 取消正在执行的 sleepy：回执 unknown、任务 cancelled/device_confirmed、子进程 pid 已不存在
PASS 吊销设备（revoked=true 且离线）/ 已终结任务审计不变
→ 42/42 通过

# 冻结包真实配对（未设 SSL_CERT_FILE，验证信任库回落）
companion-unsigned-test --pair … --init → rc=0「配对成功」；服务端可见设备后已吊销
```

回归（本轮复跑）：`tests/cu` companion venv 7 个模块（browser/cli/local/protocol/desktop/documents_pdf/workflows）`Ran 87 tests ... OK`（浏览器用例按锁定 revision 真实执行，0 skipped；本轮 +2：`--doctor` 预检可复核、失败回固定错误码）；系统 python `test_devices`+`test_agent_companion` `Ran 58 tests ... OK`（invoke/view 租约与参数白名单、desktop.screen 帧）、`test_chat_stop` + `test_download_guard` `Ran 12 tests ... OK`，`tests/p3` 两个模块单独运行 `Ran 2 tests ... OK`。打包二进制重建后 `--version` → 2.0.2；以普通用户跑 `--doctor` 声明 `browser.act/goto/screenshot/scroll/snapshot` 且 `desktop.* = linux_x11_wayland_desktop_not_accepted`，以 root 跑 `--doctor` 报 `run_as_normal_desktop_user_not_root`；冻结包在未设 `SSL_CERT_FILE` 时完成真实 HTTPS 配对。

### 2026-10-25 验收工具链（无实机，先备工具；**不代表验收通过**）

用户确认「暂无 Windows 实机、签名证书在 Windows 证书库」，因此本轮只把工具做实，并用单测 + 负向用例证明它不会给出假结论。

```
# 1) 工具自身单测（57 项）
companion/.venv/bin/python -m unittest tests.cu.test_windows_acceptance tests.cu.test_companion_build
→ test_windows_acceptance Ran 32 tests OK / test_companion_build Ran 25 tests OK
  - 判定：缺项、重复项、skip、无证据 一律 accepted=false；默认按整份清单（13 项）核对
  - 编排：模拟完整运行，报告顺序与 STEPS 一致，写出 JSON + Markdown 表；缺 companion 直接拒绝
  - 取消子进程：先确认 pid 在 → 取消后轮询到消失才算 pass；pid 不存在或仍在均不出 pass
  - 构建：签名决策矩阵、signtool 参数、验签失败不改名、回执记录真实文件名、发布核验 6 类拒绝

# 2) 未签名测试包重建（回执改为记录磁盘上的真实文件名）
companion/.venv/bin/python companion/build.py --ack-unsigned-test
→ dist/companion-unsigned-test/build-receipt.json
  {"schema":1,"version":"2.0.2","signed":false,"verified":false,"test_only":true,
   "files":{"companion":{"file":"companion-unsigned-test","sha256":"be67834d…"},
            "companion-native":{"file":"companion-native-unsigned-test","sha256":"be67834d…"}}}

# 3) 发布核验闸门（同一目录，两种声明，结果必须相反）
python -m companion.build --verify-release companion/dist/companion-unsigned-test
→ 产物不可发布：not_signed / not_verified / unsigned_test_package；rc=1
python -m companion.build --verify-release ... --allow-test-package
→ 产物可发布（仅测试包）；rc=0
# 向产物追加一个字节后：
→ 产物不可发布：companion:digest_mismatch；rc=1

# 4) 实机入口（必须在 Windows 上跑，本机不执行；验收器已随包，不要求操作端装 Python）
companion-unsigned-test.exe --windows-acceptance        # 等价外壳：python scripts/windows_acceptance.py --companion <exe>
→ 13 项：auto 4（doctor 声明自洽 + Ctrl+Alt+Pause + 后台焦点/剪贴板不变）
          measure 1（取消子进程：取消后 60s 内实测 pid 消失，OpenProcess 查，不靠自述）
          manual 8（批准/拒绝/超时/撤销/锁屏/打字干扰/托盘/UIA 应用，需填证据）
→ 任一缺项/无证据/未通过 → 「未通过（…）」+ rc=1

# 5) --doctor 可复核（冻结包，普通桌面用户 + 共享 Chromium）
companion-unsigned-test --server … --name … --config … --doctor
→ desktop keys = apps, checks, enabled, hotkey, lease, reason, suspended, uia
→ 本机（Linux）实测 checks={} uia={} enabled=false reason=linux_x11_wayland_desktop_not_accepted
  （未启用桌面时不伪造预检结果；Windows 启用后这里应逐项为 true）
→ 失败时只回固定码：state_directory_not_private（不回显路径）

# 6) 改完工具后重跑真实设备端到端（当前工作区代码）
ZOPS_CU_E2E=1 python3 scripts/cu_e2e_device_check.py
→ 42/42 通过；能力表 20 项；取消子进程 alive=False elapsed=0.0s
→ 清理后 zops:cu2:device* / zops:cu2:pair* 均为空（无残留设备与配对码）
```

### 2026-10-25 Linux X11 桌面后端（阻塞 3 的 Linux 部分；**仍不代表验收通过**）

新增：`companion/x11.py`（libX11/libXtst/libXss，纯 ctypes，无新增 C 运行时）、`companion/desktop.py` 的 `X11Driver` 与平台化验收/能力表、`companion/__main__.py` 的 `--accept-desktop` 平台分流、`scripts/x11_target_window.py`（补 `_NET_WM_PID`/`WM_CLASS` 的真实验收窗口）、`scripts/linux_desktop_check.py`（14 项清单）、`tests/cu/test_desktop_x11.py`（35 项）、`tests/cu/test_linux_desktop_check.py`（22 项）；`companion/pyproject.toml` 增加 `pystray; sys_platform in (win32,linux)` 与 `python-xlib; sys_platform == 'linux'`。

```
# 1) 底层原语在真实 X 服务器上逐项实测（无桌面环境，用 Xvfb 代替显示器）
Xvfb :96 -screen 0 1280x800x24 & DISPLAY=:96 python scripts/x11_target_window.py --log …
→ READY pid=404689 xid=6291496；xprop -id <xid> → _NET_WM_PID=…、WM_CLASS="ZopsTarget"
driver 视图 → {'title':'Z-OPS 验收窗口','class_name':'ZopsTarget','pid':404689,'rect':[80,60,560,320]}

# 2) 注入真实到达应用（不是只到达 X 服务器）
tests.cu.test_desktop_x11::RealX11Test → 应用日志逐条落盘
→ KEY 'h' h / KEY 'i' i / KEY '\x01' a（Ctrl+A）/ CLICK 20 40；注入后 Xss idle 归零

# 3) 单测与集成
companion/.venv/bin/python -m unittest tests.cu.test_desktop_x11 tests.cu.test_linux_desktop_check tests.cu.test_desktop_safety
→ Ran 82 tests OK（X11 驱动 35、清单 22、安全链 25）
companion/.venv/bin/python -m unittest（browser/cli/local/protocol/desktop/documents_pdf/工作流/windows_acceptance/build/x11/清单）
→ Ran 204 tests OK
python3 -m unittest tests.cu.test_devices tests.cu.test_agent_companion → Ran 58 tests OK
python3 -m unittest tests.cu.test_chat_stop tests.cu.test_download_guard → Ran 12 tests OK
python3 -m unittest tests.p3.test_cu_bridge tests.p3.test_cu_login_guard → Ran 2 tests OK
web-next: npx tsc --noEmit rc=0；npm run build 成功；systemctl restart zops-frontend → active，GET / 200

# 4) 端到端：本机验收记录 → --doctor → Linux 清单（临时 Xvfb + 真实 GUI 客户端）
companion -m companion --config … --doctor
→ desktop_enabled=true，checks 7/7 全 true（x11/native_dialog/input_sentinel/lock_query/
  focus_query/hotkey/injection），capabilities 仅 desktop.inspect/request_control/act，
  unavailable = desktop.screen=desktop_capture_not_accepted_on_platform、headed_login、attach_user_browser
  （修了 executor 旧瑕疵：已声明的能力不得再出现在 unavailable 里，否则远端无法判断真假）
python scripts/linux_desktop_check.py --config … < /dev/null
→ auto 8 全 pass（doctor / session / x11_windows / lock_query / injection / hotkey / no_elements / tray）
  manual 6 全 skip（无人回答）
→ 「未通过（approve、lock_screen、reject、revoke、timeout、typing_interference）」rc=1

# 5) Wayland 会话的原因码可区分（同一份验收记录，只改 XDG_SESSION_TYPE）
XDG_SESSION_TYPE=wayland companion -m companion --config … --doctor
→ enabled=false，reason=wayland_portal_not_implemented，capabilities 无 desktop.*，
  unavailable 四项 desktop.* 全为该码（不再与 desktop_acceptance_required 混淆）
```

fail-closed 约束（已写入代码与测试）：没有 `_NET_WM_PID` 的窗口一律不可接管也不出现在列表（libXRes 不存在，无法从 XID 反查 pid，不降级成按窗口类授权）；X11 识别不了密码框，因此不声明 `desktop.screen`，`invoke`/`capture` 直接以固定错误码拒绝；Wayland/XWayland 会话整体拒绝并单独报 `wayland_portal_not_implemented`（与“还没验收”区分）；锁屏或 Xss 查询失败按“用户不在场”处理；`Ctrl+Alt+Pause` 是停止硬门槛，托盘只在有托盘宿主时启动（否则如实记 false，不再往 stderr 堆栈）。

打包适配（不修就等于 Linux 桌面只能源码跑）：`companion/companion.spec` 原来无条件 `excludes=["pytest","tkinter"]`，而 Linux 的原生批准/验收对话框正是 tkinter；现改为仅非 Linux 排除 tkinter，Linux 额外收 `pystray`/`Xlib`（Windows 的 MessageBox 不需要）。重建后的未签名测试包已在真实 X 服务器上验证：

```
companion/.venv/bin/python companion/build.py --ack-unsigned-test
→ dist/companion-unsigned-test/build-receipt.json  sha256=be32fc92…（test_only=true）
DISPLAY=:94 companion-unsigned-test --local-desktop-test
→ {"enabled": false, "checks": {"x11":true,"native_dialog":true,"input_sentinel":true,
   "lock_query":true,"focus_query":true,"hotkey":true,"injection":true},
   "hotkey":"Ctrl+Alt+Pause", "uia":{"windows":1,...,"tray":false,"elements":"not_supported"},
   "reason":"probe_does_not_enable_remote_desktop"}
# 冻结包 PYZ 核对：companion.x11=True、tkinter 已打包、pystray/Xlib 已打包
```

### 2026-10-25 本轮验证（批准到期 / Wayland 双信号 / attended 执行器）

```bash
# 1) 回归（companion 要求 3.11：文件/文档/工作流必须在 venv 里跑）
companion/.venv/bin/python -m unittest tests.cu.test_companion_build tests.cu.test_companion_cli \
  tests.cu.test_companion_local tests.cu.test_companion_protocol tests.cu.test_companion_browser \
  tests.cu.test_desktop_safety tests.cu.test_desktop_x11 tests.cu.test_documents_pdf \
  tests.cu.test_linux_desktop_attended tests.cu.test_linux_desktop_check \
  tests.cu.test_linux_desktop_live tests.cu.test_windows_acceptance tests.cu.test_workflows
→ Ran 250 tests ... OK (skipped=5)（跳过的 5 个是 opt-in 的真实对话框用例）
ZOPS_LINUX_DESKTOP_LIVE=1 companion/.venv/bin/python -m unittest tests.cu.test_linux_desktop_live
→ 5 个用例（approve/reject/timeout/revoke/typing_interference）共 59.6s 全部 OK
python3 -m unittest tests.cu.test_devices tests.cu.test_agent_companion \
  tests.cu.test_chat_stop tests.cu.test_download_guard
→ Ran 72 tests ... OK
python3 -m unittest tests.p3.test_cu_login_guard tests.p3.test_cu_bridge → Ran 2 tests OK

# 2) 前端构建与部署
npx tsc --noEmit → rc=0；npm run build → rc=0（BUILD_ID=vFk0WlC_MJJ0cRqM8O33H）
systemctl restart zops-frontend → active
curl -k -H 'Host: opscore-ai.example.com' https://127.0.0.1/ → 200，首页引用
  /_next/static/chunks/0n3~may8dxulm.js（含「30 秒无人操作自动拒绝」）且经 nginx 200

# 3) 无人值守时必须失败（Xvfb :95 + 真实 venv/配置/验收记录）
python scripts/linux_desktop_attended.py --config … < /dev/null
→ [approve] fail：非交互运行：没有人在机旁，步骤未执行；rc=1，JSON accepted=false
python scripts/linux_desktop_check.py --config … --attended <json> < /dev/null
→ approve 显示「attended: 非交互运行…」，其余 manual 仍 skip；verdict.accepted=false，rc=1

# 4) 冻结包第四次重建（对齐 _input_desktop_name 句柄声明），并包内复验
companion/.venv/bin/python -m companion.build --ack-unsigned-test
→ sha256=52081378…，created 2026-11-01T13:10:41Z，test_only=true
--doctor → checks 7/7；WAYLAND_DISPLAY=wayland-9 → wayland_portal_not_implemented
--accept-desktop 无人值守 → 90s、rc=1、local_user_timeout、未写 desktop-acceptance.json
linux_desktop_check.py --companion <冻结包> → auto 8 pass / manual 6 skip / rc=1
```

结论：Linux X11 后端与工具链已真实跑通，但 **Linux 桌面验收仍未通过**：缺的正是「人在机旁」的 6 项人工步骤（批准/拒绝/超时/撤销/锁屏/打字干扰）。本轮已把这三件事补成可执行形态——对话框 30 秒无人操作到期自动**拒绝**（原因码与用户拒绝区分）、Wayland 判定不再漏 `WAYLAND_DISPLAY`、6 项人工步骤有了接真实代码路径的执行器与可被清单消费的证据；并用 XTest 充当「坐在机器前的人」把其中 5 项的**通过分支**在真实对话框上跑通（顺带修了 `await 同步方法`、租约残留、提示语颠倒、验收到期被当成拒绝四类真机缺陷），并把「批准/拒绝/到期/紧急停止/切窗打字」固化成 opt-in 回归（只有锁屏那一项仍需真实锁屏器）。入口文档补齐了 README/QUICKSTART（否则用户找不到这两个脚本），Windows 验收对话框的 HWND 与输入桌面句柄都改为 `c_void_p` 传递（前者否则到点也关不掉框、事后还能被点成同意，后者否则把“拿不到句柄”误判成锁屏、桌面能力永久不可用）；冻结包第四次重建为 `sha256=52081378…` 并在包内复验了到期、Wayland 与清单。剩下 `lock_screen` 需要真实锁屏器，且验收结论必须由真人在真实桌面完成——机器替身按按钮不算。所以本机默认仍不声明 `desktop.*`。（本段是当时的记录：macOS AX 已在后续一轮实现，Wayland 也已加上 portal 探测层，分别见一.12、一.13；两者仍卡在实机/真人验收。）

---

## 一.15、2026-11-02 界面实机复验（Spec 第 83 条「浏览器 UI 必须实际操作并截取验证截图」）

本轮浏览器 MCP（browser-use）整条通道故障：任何调用都返回
`Notification handler already registered for method: aicoding.nativeBrowser.cdpProxy.event`，
子代理与主对话都调不动。为了不把手工读源码当成"界面已验证"，改用仓库里已锁定的
Playwright + Chromium 起真浏览器（`python3`，系统解释器同时具备 playwright 与 redis），
产出同类证据：真实渲染 + 真实点击 + 截图文件。脚本固化在
`scripts/ui_probe_computer_use.py`。

登录说明（如实记录，避免证据被高估）：界面要求 AD 域账号，代跑不持有也不应留存任何
口令。这里用后端**自己的**会话机制在 Redis 为本地 `admin` 账号临时签了一个短 TTL token
（等同 `secrets.token_hex(32)` + `SET zops:token:<hex> <user> EX`，即 auth_login 本地分支
的最后两步），导航前写入 `localStorage.zops_token/zops_user`，用完在 `finally` 里立即
`DELETE`。不改密码、不建号、不授权、不新增权限。因此这组截图证明的是"**界面按登录态
真实渲染出这些内容**"，不证明"某个真实 AD 用户完成了登录"——后者仍需真人输口令。

执行过程（严格只读：白名单外的按钮一律跳过，未点保存/吊销/删除/清空会话，未向任何
输入框写入内容）：

| 步骤 | 真实动作 | 渲染结果（截屏所见原文，非源码断言） | 证据 |
| --- | --- | --- | --- |
| 落地页 | 打开 `http://localhost:3000/` | 未登录时只有「登录 / AD 域账号 / 密码」——说明必须登录态才能看到功能面 | — |
| 登录态首页 | 写入会话后重载 | 标题「有什么可以帮忙？」，快捷能力区出现「电脑操作」按钮；导航含「系统设置」 | `ui/01-authed-landing.png` |
| 点「电脑操作」 | 真实点击 | 出现「连接本机 Companion」引导卡，按钮变为「任务小窗」「打开电脑操作设置」；底部出现 BrowserConnectCard 文案 | `ui/02-entry-0.png` |
| 点「打开电脑操作设置」 | 真实点击 | 设置模态四个 tab：通用 / 记忆 / **电脑操作** / 数据控制；分组标题实机渲染为「电脑操作 · 在已选设备上执行」「设备」「安装与配对」「后台策略与本机授权」「桌面接管申请」「任务画面推送」「截图证据保存」 | `ui/02b-0-1.png`、`ui/05-cu-settings-modal.png` |
| 管理页 | 访问 `/admin` 并切 tab | 侧栏出现「Computer Use」，页面渲染「Computer Use 配置」「站点凭据库」，含域名白名单输入、`测试`/`清除所有浏览器会话`/`保存配置`/`保存凭据` 等动作按钮（**均未点击**） | `ui/03-admin.png`、`ui/04-admin-tab-0.png` |

模态正文逐条与 Spec 第 65–70 条对齐（这些文字是浏览器里渲染出来的，不是我从 .tsx 里
grep 出来的）：设备未配对时明示「尚无配对设备，请先安装并配对 Companion」并拒绝自动
选唯一在线设备；「已保存的选择也不代表这台浏览器所在的电脑」；后台优先说明 + 真实桌面
接管默认关闭 + **Wayland 会话一律拒绝** + Linux 不提供控件树与窗口截图 + 无
`_NET_WM_PID` 的窗口不可接管；设备离线/拒绝/结果未知时任务暂停且「不会改用服务器执行」；
四类授权（目录/站点/应用/脚本）以设备上报为准、本页只读、扩大授权必须本机 CLI；桌面接管
默认禁止、60 秒租约不可续期、对话框 30 秒无人操作自动拒绝；画面推送仅临时查看、不切换
用户标签页；截图证据保存默认关闭、开启后每任务最多 20 张且至少间隔 10 秒、关闭不删除
既有证据；锁屏或密码框获焦时自动暂停采集。Windows 侧仍如实标注"执行器构建尚未签名，
当前不提供可用的 exe 下载承诺"。

一次失败也记录：`任务小窗` 按钮点击 4s 超时（当时无绑定任务，面板不挂载），这是预期
行为而非缺陷——没有任务时不该凭空出现小窗；Spec 第 61 条的小窗实机证据由此前轮次的
真实任务截图承担。

结论：Spec 第 83 条中「浏览器 UI 必须实际操作并截取验证截图」这一半，本轮在当前代码
与当前前端构建（`zops-frontend.service`，next-server）上重新取得实机证据，界面文案与
Spec 65–70 的约束逐条可见、无遗漏项，也未被前端"假装已授权"。该条的另一半「完成四条
端到端工作流」仍是**三条通过、一条阻塞**：文件与文档、终端与运维、跨应用工作流已在真
实设备上跑完并留证；桌面应用工作流需要 Windows 实机 + 真实桌面验收（Spec 第 84 条明确
Linux 模拟不能替代），继续挂在外部阻塞项上。浏览器 MCP 通道故障属工具侧问题，已如实
保留原始报错串，未因此降级验证标准。

---

## 一.16、2026-11-02 交付面平台表述补齐 macOS（界面/CLI 与代码事实一致）+ 第十一次冻结包复验

起因是一.15 的实机截图比对：设置页渲染出来的正文写着"本机桌面验收（Windows UIA / Linux X11）"，
但 `companion/desktop.py` 里 macOS 早已是独立平台分支——`CAPABILITIES_BY_PLATFORM` 给
`darwin` 声明完整四项 `desktop.*`（含 `desktop.screen`），验收项是
`ACCEPTANCE_CHECKS_MACOS = (ax, native_dialog, input_sentinel, lock_query, focus_query, hotkey, capture)`，
未验收原因码是 `macos_ax_desktop_not_accepted`。**界面少说一个平台，用户就没法知道 Mac 上该
做什么验收**；这不是排版问题，是交付面与实现不一致。`companion/__main__.py` 的
`--accept-desktop` 帮助文本同样漏了 macOS。

改动（只改文字，不动逻辑，且先核对代码事实再写文案，不反向）：

| 文件 | 改动 | 依据 |
| --- | --- | --- |
| `web-next/src/components/ComputerUseSettings.tsx:177` | 平台列举补 `macOS AX`；新增"macOS 由 AX 读控件树并只采集被接管窗口（需辅助功能与屏幕录制授权）"；Wayland 拒绝补原因括注"portal 与 EIS 只给整屏授权与屏幕区域，没有窗口对象" | `desktop.py` darwin 声明 `desktop.screen`；`MacDriver.prepare` 要求 `trusted()`（辅助功能）与 `capture_allowed()`（屏幕录制）；`ax.capture(number)` 按窗口号采集，不是整屏 |
| `web-next/src/components/ComputerUseSettings.tsx:236` | 桌面接管申请说明补 `macOS AX` | 同上 |
| `companion/__main__.py:214` | `--accept-desktop` 帮助文本补 `macOS AX` | `ACCEPTANCE_CHECKS_MACOS` |

我在这一步把"拒绝"写成了"拒绍"（三处），改完立即用 `grep -o` 计数发现并 `sed` 修回，随后对
`拒绍/开仿/没备/失格` 做了全仓错字扫描，结果为空——记录在此以免这类错误被悄悄覆盖过去。

发布与实机复验（Spec 第 85 条：构建后经 systemd，禁止 nohup）：

```
npm run build → 成功（静态路由表正常输出）
systemctl restart zops-frontend → active；curl http://localhost:3000/ → 200
真浏览器复跑（Playwright，临时 token 用完即删）→ 渲染文本实测含：
  “…本机桌面验收（Windows UIA / macOS AX / Linux X11）…Wayland 会话一律拒绝（portal 与 EIS
   只给整屏授权与屏幕区域，没有窗口对象）…”  → screenshots/ui/06-cu-settings-copy-fixed.png
```

改了 `companion/__main__.py` 就必须重建冻结包（源码与包不一致是纪律问题）。第十一次重建
`sha256=acc8786c…`，`created 2026-11-01T17:39:19Z`，`test_only=true`，包内复验：

```
--help | grep "本机验收（…）"        → 本机验收（Windows UIA / macOS AX / Linux X11）
--eis-selfcheck                    → passed 8 / failed [] / window_scoped False
WAYLAND_DISPLAY=wayland-9 --doctor → wayland_dbus_session_missing（无总线，精确原因码）
Xvfb :201 + --doctor               → desktop_enabled=False、caps 3 项、reason=x11_no_visible_windows
linux_desktop_check.py --companion → auto 8 通过（doctor、session、x11_windows、lock_query、
                                     injection、hotkey、no_elements、tray）/ 人工 6 未做
                                     （approve、reject、timeout、revoke、lock_screen、
                                     typing_interference）、accepted=false、rc=1
--accept-desktop 无人值守          → 91s、rc=1、local_user_timeout、未写 desktop-acceptance.json
```

两条过程性错误如实记录：①第一次复验我拿 `pgrep -f "Xvfb :199"` 当"是否已启动"的守卫，而脚本
自身命令行里就含这个串，于是误判"已启动"、Xvfb 从未起来，导致 `--doctor` 报
`x11_display_unavailable`、清单 13 项全失败——那**不是代码回归**，是我这次的验证脚本缺陷，
换用新显示号 :201 后与第十次包行为完全一致；②`--accept-desktop` 第一次 0 秒失败在
`state_directory_not_private`（我用 `mkdir -p` 建 state 目录没设 700），这条恰好证明权限闸门
在包内是真的生效，补 `chmod 700` 后按预期跑满 91 秒超时。

回归（三组解释器分工不变）：`companion/.venv/bin/python`（3.11）18 模块 **Ran 358 OK
(skipped=5)**；系统 `python3`（3.10）四模块 **Ran 72 OK**；`tests.p3.test_cu_bridge +
test_cu_login_guard` **Ran 2 OK**。服务 `zops-backend / zops-frontend /
zops-agent@computer_agent` 均 active。

结论：交付面的平台表述现在与实现一致——三个平台的验收路径、macOS 的窗口级采集能力与
Wayland 被拒的真实原因都能在界面上读到，不需要用户去翻代码。此项不改变任何能力声明：
macOS 仍未实机验收，`desktop.*` 依旧不开放。

---

## 一.17、2026-11-02 真人陪跑路径按文档原样执行验证 + 验收记录绑定核实

Spec 第 84/89 条与 README 都把"用脚本扮演机旁的人不算验收"写死了，所以最后那 20 分钟必须真人跑。
既然要真人跑，**文档里给的解释器和命令顺序本身就必须是对的**——真人照着敲却报错，阻塞就从"缺人"
变成"缺人+缺可执行路径"。本轮按 README/QUICKSTART 原文逐条执行（不改解释器、不换参数）：

```
C=<已配对配置，config 600 / 目录与 config.state 700>；Xvfb :203；DISPLAY=:203 XDG_SESSION_TYPE=x11

companion/.venv/bin/python scripts/linux_desktop_attended.py --config $C/config.json --output …
  无验收记录时 → stderr「桌面不可用：desktop_acceptance_required；先运行 companion --accept-desktop
                 写入本机验收记录。」 rc=2      ← 文档声称的先后顺序成立
  有验收记录时 → 六步逐个提示真人操作，全部 fail「非交互运行：没有人在机旁，步骤未执行」，
                 rc=1，报告 accepted=false      ← 无真人时拒绝编造结论
companion/.venv/bin/python scripts/linux_desktop_check.py --config $C/config.json --output …  → rc=1
```

两个结论：①文档写的 venv 解释器（`companion/.venv/bin/python`）确实能跑这两个脚本，此前我用系统
`python3` 只是习惯差异，不是文档写错；②attended 执行器对六步（approve / reject / timeout /
typing_interference / lock_screen / revoke）都有真人提示与判定，**包括之前认为"最难"的锁屏**——
它等 `driver.secure()` 变真再发注入，并核对暂停原因必须是 `secure_desktop`，解锁由人做。也就是说
真人陪跑在工具侧已无缺口，缺的只是人。

顺带核实了验收记录的绑定语义（担心"验一次永久有效"）：`validate_acceptance()`
（`companion/desktop.py:189-205`）逐项要求 `version==1`、`platform` 与当前平台名一致、
`device_id == config.device_id`（否则 `acceptance_device_mismatch`）、
**`accepted_version == __version__`（版本一升就 `acceptance_stale`，必须重验）**、检查项集合与
该平台的 `ACCEPTANCE_CHECKS*` 完全相等且全为 True。记录本身不设时间过期，这是有意的：它只代表
"这台机器、这个版本上这些原生前提成立"，而每次接管仍要本机弹窗批准 + 60 秒租约不可续期 + 30 秒
无人操作自动拒绝；`expires=None` 不等于"永久可用"。本轮如实记录该设计，避免被误读成漏洞。

数值约束也逐条对了源码，交付面文案没有虚报：`LEASE_SECONDS=60`、`APPROVAL_SECONDS=30`、
`ACCEPTANCE_SECONDS=90`、`EVIDENCE_MAX=20`、`EVIDENCE_INTERVAL_S=10`（`core/cu/devices.py:18-19`，
`persist_evidence` 真的是先查 `shot_evidence_enabled` 授权、再限频、再限数、且不删历史），
扩展侧 Cookie 交接 `expiry = now + 300`（`extension/background.js:327`，即界面上写的"5 分钟"）。

---

## 一.18、2026-11-02 逐条完成度复审计：更正两处已过时的阻塞描述

做最后一轮"以当前工作区为准"的复核（不靠记忆），逐条对 Spec 各节取证据，结果分两类。

**已核实无需再改的（本轮重新取样）**：Spec 第 65 条要求"在 `SettingsModal.tsx` 将网页设置整合为电脑操作"——`SettingsModal.tsx:14` 引入 `ComputerUseSettings`，`:24` 与 `:153` 把原 `web` 分组的标签渲染为**电脑操作**（配合 `:187` 的标题映射），四个 tab 为 通用/记忆/电脑操作/数据控制；这与一.15 真浏览器里看到的模态一致，即"命名工件"与"实机渲染"两条证据都对上。Spec 第 12 条的依赖与浏览器版本锁定，`companion/uv.lock` 存在，Chromium Headless Shell revision 由构建输出与 `--doctor` 的 `browser.*` 判定共同约束。Spec 第 79-82 条的协议/并发/吊销/断连/回执丢失与秘密扫描类，由本轮刚跑绿的三组回归覆盖（venv 358 / 系统 72 / p3 2）。

**发现两处 trace 里的阻塞描述已经过时，据实更正**（这类错误最危险：它会让用户以为"还有代码可写"，或以为"只要给证书就能出包"）：

1. **阻塞 2（Windows 生产签名包）原先只写"证书在用户证书库，本机没有"，说轻了。**
   实际核查 `companion/build.py`：没有 `--platform` 交叉编译入参，`build_plan(…, platform)` 的
   platform 只用于挑选当前主机的产物布局；而 `--sign-thumbprint` 取的是 Windows 证书库指纹。
   PyInstaller 本身不支持交叉构建——所以 **`.exe` 连"未签名测试包"都无法在这台 Linux 上产出**，
   必须有 Windows 主机。已把这句补进第三节阻塞清单，避免用户按"给个证书指纹就行"来安排资源。
2. **阻塞 3（Wayland）恢复动作原先写"需新增 EIS/libei 输入"，已经过时**：EIS(libei) 输入传输层
   在一.14 就实现并有包内自检（本轮第十一次包再次复验 `--eis-selfcheck` passed 8）。真正剩下的
   不是代码，而是协议事实：portal 与 EIS 都没有窗口对象，只有整屏授权与屏幕 `region`。因此该行
   已改写为"需用户先定整屏接管能否接受 + 真合成器实测"，并把要核对的精确原因码列全
   （`wayland_eis_channel_missing` / `wayland_window_scope_not_supported` / `wayland_dbus_session_missing`，
   本轮包内实测到的是最后一种，因为这台机器上没有 D-Bus 会话总线）。

**过程性错误也记在这里**：本轮编辑又触发了一次同类字符错乱——"拒绝"被写成"拒绍"（一.16，已修）、
"跨平台构建"先被写成"跳构建"再被写成"交又构建"（已用脚本按字符串计数定位并修回）。修完对
trace/README/QUICKSTART/前端组件/CLI 做了 `交又|跳构建|拒绍|必須|開發|運行` 全文扫描，仅剩
一.16 中"如实记录这次错字"的两行自述命中，无残留错字。教训是：这个编辑链路上中文文本有
被同音/近形字替换的风险，**每次改完用户可见文案都必须做字节级回读与错字扫描**，不能假定 diff 里
看到的就是写进去的。

---

## 一.19、2026-11-02 界面取证脚本进入口文档（证据必须可复现）

一.15/一.16 的界面证据是本轮用 `scripts/ui_probe_computer_use.py` 产出的，但脚本当时只躺在
`scripts/` 里，README/QUICKSTART 都没提——**取证工具不可发现，等于证据只能由我本人复现**，
下一个人只会重新退回"读源码断言界面"。本轮补上：

- `QUICKSTART.md` 新增「🖼️ 界面实机取证（浏览器 UI 验证）」一节，并把目录补齐为 7 项
  （原先连上一轮加的「📦 冻结包预检」都没进目录）；
- `README.md` 桌面验收段落之后加了指向该脚本与 QUICKSTART 新锚点的一句话。

文档里写死了三条边界，避免这套取证被高估或被误用：①**只读**（内置动作黑名单，不点
保存/吊销/删除/清空会话/生成配对码，不向输入框写内容）；②**登录态是本地签发的临时会话**，
不持有也不留存口令，用完在 `finally` 里立即删除，因此证明的是"界面按登录态真实渲染"，
**不证明**"真实用户完成 AD 登录"；③界面文案必须与代码常量对得上（60/30/90 秒、20 张/10 秒、
300 秒 Cookie 交接），对不上即为交付面缺陷（一.16/一.17 已逐条对过）。

按文档原样重跑取证：`python3 scripts/ui_probe_computer_use.py` → rc=0，日志含
`[cleanup] 已删除临时 token`，`screenshots/ui/` 共 8 张 PNG，Redis `zops:token:*` 回到 9 条
（均为此前他人真实会话，我签的已全部清除）。本轮只改 Markdown，未动源码与前端，
因此不需要重建冻结包。写完后对两个文档做了字节级回读：必需关键词全部命中，
`交又/跳构建/拒绍/必須` 等异常串 0 命中（得自一.18 的教训——中文文案必须回读，不能只信 diff）。

## 一.20（第 20 轮）用户指定“先做 Windows”：把验收器装进包里

触发：用户回答“先做 windows”。本轮不做“Linux 代跑 Windows 验收”那种假动作（Spec 第 84 条），
而是先把“到了 Windows 机器上怎么跑”这条路修成真的可用。

### 1. 先核实 Windows 代码侧到底缺什么

逐个文件确认，缺的**不是实现**：

- Job Objects 管理任务子进程：`companion/processes.py:40-60`（`CreateJobObjectW` +
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + `AssignProcessToJobObject`，退出时 `TerminateJobObject`）；
- 全局热键与托盘：`companion/desktop.py:757`（`RegisterHotKey`）、`:776`（pystray 托盘）；
- 安全桌面不可操作：`companion/desktop.py:413/434/525` 均 `driver.secure()` 则 `Paused("desktop_secure_desktop")`。

所以真正的障碍是“工具体”那侧。核实发现一个与 Spec 相冲的事实：

> Spec 第 12 条写的是“PyInstaller 打包，**不要求操作端安装 Python**”，但原
> `scripts/windows_acceptance.py` 要测试机上装 Python + playwright + 克隆仓库才能跑。
> 即拿不到 exe 也跑不了验收，两个口径相互矛盾。

### 2. 做法：验收器实现住进包里

- 新增 `companion/winaccept.py`：13 项清单的实现（`collect_submodules("companion")` 会自动把它进包）；
- `companion/__main__.py` 新增 `--windows-acceptance` 与 `--acceptance-output`（照 `--eis-selfcheck` 同一样例），
  默认报告路径 `~/zops-windows-acceptance.json`；在 Windows 测试机上只带 exe 就能跑完整份清单；
- `scripts/windows_acceptance.py` 改为**外壳**（re-export 同一实现），保留源码方式与旧文档/测试入口，
  不存在第二套判定口径（新增一条单测用 `assertIs` 钉住 STEPS/run/verdict 是同一对象）；
- `HOTKEY_ID`/`HOTKEY_TEXT` 不再自带副本，改为仍从 `companion/desktop.py` 复用；否则会出现
  “验收测的是另一个热键、驱动注册的是又一个”这种假通过；
- 报告新增 `companion_version`；`schema` 保持 1，不据未核实的下游读取随便改版。

### 3. 搬家保真：用 AST 比对而不是眼看

拿 `git show HEAD:scripts/windows_acceptance.py` 作基准，逐函数比较去掉 docstring 后的 AST dump：

```
COMMON 13  OLD_ONLY ['_last_error']  NEW_ONLY ['_version', 'resolve_companion']
DIFFERING ['main', 'probe_doctor', 'probe_hotkey', 'run']
```

17 个函数中 13 个逐字节一致；差异只在上述四处，且逐处可说明：`main`（参数改可选 + 默认输出路径）、
`probe_doctor`（`str(companion)`）、`probe_hotkey`（ctypes 上提 + 复用驱动常量）、`run`（exe 自解析 +
版本号字段）。**判定相关的 `verdict`/`ask`/`measure_cancel_child`/`pid_exists`/`background_step`/
`markdown`/`probe_auto`/`probe_background` 完全未动**；13 项 id/spec/kind 逐条核过。

### 4. 测试与回归

- 原 32 项测试 patch 的是“脚本模块属性”，实现搬家后会打空 → 把测试目标改为 `companion.winaccept`，
  并新增 4 项：外壳与包同一实现 / 非 Windows 拒绝且不写任何文件 / CLI flag 端到端（跑
  `python -m companion --help` 确认真的 wired）/ 热键常量与驱动一致 → `Ran 36 tests OK`；
- 全量：venv3.11 `Ran 366`（errors=4 为已知需 httpx 的四个模块 loader 报错、skipped=5，与基线一致）；
  系统 python3.10 四模块 `Ran 72 OK`；`tests.p3.test_cu_bridge` + `test_cu_login_guard` 用**系统 python3.10** 跑 `Ran 2 OK`。
  注：本轮自查脚本误用 venv 跑 p3，得到 `FAILED (errors=2)`——这是解释器选错导致的假警报（p3 依赖 httpx，venv3.11 无该包），
  不是代码回归；换回系统 python3 后 OK。两个解释器的分工已经写进记忆，但写验证脚本时仍错了一次：
  **验证脚本里的解释器选择也要当成一个要校核的变量，不能只校结果。**

### 5. 第十二次冻结包重建与包内复验

源码动了（`winaccept.py`/`__main__.py`），按纪律重建：`sha256[:8]=a57687fd`（主二进制）、
`created 2026-11-01T18:35:19Z`、version 2.0.2、`test_only=true`。包内逐项复验（不是看源码）：

- `./companion-unsigned-test --help` → 含 `--windows-acceptance` 与 `--acceptance-output`；
- `HOME=/tmp/pkgcheck-home ./companion-unsigned-test --windows-acceptance` → rc=1 且输出
  `windows_only：本清单必须在 Windows 实机上运行`；`/tmp/pkgcheck-home` **从未被创建**
  （证明非 Windows 不可能意外生成一份“验收报告”）；
- `--eis-selfcheck` → passed 8 / failed 0，无回归。

另确认一点判据：`companion.spec` 只 `collect_submodules("companion")`，不 collect tests，
所以本轮后续“只改测试文件”不需要重建包。

### 6. 边界：仍然没有闭合 Windows 实机

本轮只做到“拿上 exe 就能跑”，依然不能产出 Windows 验收结论：

- `.exe` 仍无法在这台 Linux 上产出（PyInstaller 不支持交叉构建），所以包内新入口在 Windows 上的
  **实跑结果仍未取证**；Linux 包只验证了“存在且会拒绝”；
- `background_focus`/`background_clipboard` 依赖 Chromium Headless Shell（revision 1194）；测试机上出
  现浏览器运行时缺失时，这两项会是 `skip`（带异常原因）而不是通过，整体结论仍为“未通过”；
- 签名仍需用户给出证书指纹；`--doctor`/`--accept-desktop` 那两项也只能在实机上完成。

恢复后的首个动作已改写为单行：`companion-unsigned-test.exe --pair … --init` → `--accept-desktop` →
`companion-unsigned-test.exe --windows-acceptance`。

### 7. 本轮工具链异常（必须记下来的干扰）

本轮 harness 的 stdout/diff **多次**把不相干 CJK 文本互相穿插显示（包括 Read 工具看到的 trace
片段、SearchReplace 的 diff、以及我自己 Bash 命令的回显）。处理方式是不信显示层，每次都用
纯 ASCII 短输出做字节断言：第 417 行 `LEN 256 PIPES 4` 且必需关键词全命中、不应出现的穿插串 0 命中；
四个改动源文件错字表 0 命中；13 项步骤长度逐一打印对比。另修正一次**自己**在替换文本里
掉字（“并配好系统 keyring”写成“系 keyring”），同样靠字节回读而不是 diff 发现。
