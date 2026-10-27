# Computer Use 本机全面升级 — Spec 对照与阻塞记录

日期: 2026-10-23（2026-10-24 补充真实设备端到端、WSS 生命周期与设置页 UI 验收；2026-10-25 补充 UIA 元素层、真实桌面画面、浏览器任务切换竞态、打包产物浏览器运行时与当前代码的真实设备端到端复跑，以及 Linux X11 桌面后端与验收清单）
Spec: `Computer_Use_本机全面升级_task-eb6.md`
结论: Linux 侧全部可验证项已实现并取得运行证据（真实 Companion、真实无头浏览器按锁定 revision 实跑、真实文件/文档与工作流、双设备并发的真实 WSS 生命周期、真实浏览器操作设置页「电脑操作」、真实 Chromium 加载扩展经 Native Messaging 交接登录态、未签名测试包以普通桌面用户启动沙箱化 Chromium、当前代码在未预设 `SSL_CERT_FILE` 下 42/42 真实端到端复跑通过、Linux X11 桌面后端在真实 X 服务器与真实 GUI 客户端上跑通并发真实事件）；Windows 实机验收、生产签名与 macOS AX / Wayland Portal 仍阻塞（Spec 第 84 条明确「Linux 模拟测试不能算 Windows 验收」，Linux X11 的人工干扰与锁屏验收也尚未在带真实键鼠的桌面会话中执行）。

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

验收方式：同一用户下两台真实 Companion（各自独立 `HOME`/`XDG_RUNTIME_DIR`/系统 keyring，运行在独立 dbus 会话中解锁 SecretService），经 `wss://gcoc-ai.example.com/api/cu/devices/connect` 连接真实后端；命令、代际、fencing、吊销全部由真实 `core/cu` 服务下发与核对，无 fake Redis、无源码字符串断言。42 项检查全部通过。

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

验收方式：不复用旧脚本、不造 fake Redis；脚本已固化在 `scripts/cu_e2e_device_check.py`，必须显式 `ZOPS_CU_E2E=1` 才会运行（避免误触），不使用生产账号凭据。临时访问令牌只用来换一次性配对码；Companion 以普通系统用户 `cpcnet` 在独立 dbus 会话里解锁系统 keyring 后运行当前代码，经 `https://gcoc-ai.example.com` 走真实 nginx TLS 与真实 WSS，任务与命令由真实 `core/cu` 服务下发；浏览器为操作端 Playwright 无头 Chromium（`PLAYWRIGHT_BROWSERS_PATH=/opt/zops-pw-browsers`，revision 1194）。**Companion 进程未设置 `SSL_CERT_FILE`**，用来同时验证本轮新增的信任库回落。42 项检查全部通过，验收后设备已吊销，令牌/配对码/设备/任务键与临时目录已清理（任务审计 `:events` 为永久只追加，不自动删除）。

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
| 回归 | companion venv（3.11，`requires-python>=3.11`）13 个模块 `Ran 247 tests OK (skipped=5)`（跳过的 5 个是 opt-in 的真实对话框用例）；系统 python 3.10 上 4 个应用侧模块 `Ran 72 tests OK`；`tests/p3` `Ran 2 tests OK`。分工是环境决定的：`local_worker` 走 `asyncio.timeout`（3.11 起才有），所以文件/文档/工作流必须在 venv 里跑；反过来 `test_devices`/`test_agent_companion`/`test_chat_stop`/`test_download_guard` 依赖后端库，在 venv 里反而导入不了 |
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

`companion/dist/companion-unsigned-test` 共重建三次，最终产物 `sha256=2849e261…`（`created 2026-10-25T04:11:08Z`，`test_only=true`）；第三次是为了让包内代码对齐 `_close_dialog` 的句柄修复（`desktop.py` 在第二次构建后又被改过，包比源码旧就不能当证据）。包内实测：

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

### 一.11.3、Windows 对话框句柄与入口文档

| 项 | 实现与证据 |
| --- | --- |
| 缺陷：`_close_dialog` 的 HWND 可能被截断 | `visit(hwnd, _param)` 拿到的句柄原样传给 `GetWindowThreadProcessId/GetWindowTextLengthW/GetWindowTextW/PostMessageW`；ctypes 没设 `argtypes` 时按 32 位 int 解释，64 位句柄高地址会被截断，结果「到了 30 秒也关不掉验收对话框」——用户事后仍能点「写入验收记录」，等于把已经按到期判失败的请求重新批掉。现统一 `handle = ctypes.c_void_p(hwnd)` 再传；`tests/cu/test_desktop_safety.py` `Ran 29 tests OK` 覆盖该路径（无实机，只到源码与回归层） |
| 入口文档缺失 | `README.md`、`QUICKSTART.md` 原先完全不提 Linux 桌面验收入口，用户找不到 `scripts/linux_desktop_check.py` / `linux_desktop_attended.py`。现 README 增「本机（桌面）接管」段（默认全关、X11 only、Wayland 拒绝、四步命令），QUICKSTART 增「桌面接管验收（Linux / X11）」整节：配对与 `--allow-desktop-app`、`--accept-desktop` 的 90 秒/到期区分、6 步顺序与逐项注意（对话框在回车后才弹、`Ctrl+Alt+Pause` 立即作废、`lock_screen` 需要真实锁屏器）、清单 8 项 auto 与「缺项即未通过」、机器替身不算验收 |
| 旧解释器下的误判（排查记录，非缺陷） | 在宿主 python 3.10 上直接用库路径跑文件/工作流用例会报 `local_worker_outcome_unknown`（底层是 `AttributeError: module 'asyncio' has no attribute 'timeout'`）；走 CLI 则干净拒绝：`companion --doctor` → `python_311_required`。`companion/pyproject.toml` 已声明 `requires-python>=3.11`，冻结包内置 3.11，故不是产品缺陷；回归因此按解释器分工，入口文档也统一改用 `companion/.venv/bin/python`（本机甚至没有 `python` 命令，只有 `python3`） |

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
| 阶段4 macOS/Linux 先验收文件、脚本和浏览器；桌面适配未验收则禁用 | Linux 部分完成，macOS 仍缺 | Linux 文件/脚本/浏览器已实机验收（一.5）；**Linux X11 桌面后端已实现**（`companion/x11.py` + `X11Driver`：窗口枚举含 `_NET_WM_PID`/`WM_CLASS`、焦点设置与上溯、XTest 注入、XScreenSaver 空闲与屏保、`Ctrl+Alt+Pause` 独占抓取、tkinter 原生批准），在真实 Xvfb + 真实 GUI 客户端上取到真实事件；未在带真实键鼠的桌面会话做人工干扰/锁屏验收，因此本机 `--doctor` 默认仍不声明 desktop.*（需先 `--accept-desktop`）。X11 无控件树、无法识别密码框，能力表只有前三项，`desktop.screen` 以 `desktop_capture_not_accepted_on_platform` 明确标注；Wayland 会话一律拒绝。macOS AX 与 Wayland Portal 未实现，仍阻塞 |
| 边界：真实桌面无法同时不影响用户，必须临时让出控制 | 已声明并实现约束 | 接管需本机原生批准 + 60 秒租约；用户输入/窗口变化/锁屏即暂停（`test_user_input_window_change_and_lock_suspend`）；小窗文案「不占用你的鼠标键盘」 |
| 边界：不承诺任意桌面应用/最小化窗口/受保护页面/所有 Wayland | 已声明 | 设置页「未列出的应用不可操作」「真实桌面画面只在接管时推送（需 view 范围，只截被接管窗口）」；`desktop.py` 安全窗口黑名单 |
| 边界：不宣称抵御已取得终端管理员权限的恶意软件 | 已声明 | 授权模型只约束远端/网页/模型越权；文档与设置页未做超出该范围的承诺 |

### 测试与发布（第 78–85 条）

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| p3 CU 测试无导入副作用、强制隔离 Redis | 已实现（本轮加固） | unittest 化 + `guard_test_redis`；并显式固定 `config.REDIS_URL`，与 `tests/cu` 同进程同跑不再误连生产 Redis；合并运行 71 项 OK |
| `tests/cu/` 覆盖协议/安全/执行器/并发/过期/吊销/重连/未知 | 已实现 | `test_devices.py` 42 项、`test_agent_companion.py` 16 项、`test_chat_stop.py` 7 项、`test_download_guard.py` 5 项、companion 套件 87 项（0 skipped，含 12 项真实 Chromium、2 项执行器能力声明、3 项信任库回落、--doctor 预检可复核与固定错误码） |
| 合成秘密检查快照/网络/日志/模型输入 | 已实现 | `test_screenshot_task_only_ttl_size_and_redaction`、`test_access_log_ticket_is_redacted`、`test_hello_grants_summary_is_bounded_and_display_only` |
| 目录逃逸/恶意参数/提示注入/未授权下载 | 已实现 | `test_workflows.py::test_hostile_page_text_cannot_expand_local_capabilities`、`test_download_guard.py`、`files.py` 路径校验 |
| Windows 实机输入干扰/锁屏/急停/取消子进程 | 工具就绪，待实机 | `scripts/windows_acceptance.py` 13 项（auto 4 / measure 1 / manual 8；人工步骤无证据即未做，取消子进程改为实测 pid 消失、doctor 要求预检逐项为真）、32 项单测；模拟运行验证报告顺序与判定，**实机结论未产出**（Spec 第 84 条） |
| 浏览器 UI 实操并截图，四条端到端工作流 | 部分（Linux 三条实机） | 真实浏览器实操小窗并截图（执行方式/停止确认/暂停原因），并实操「设置 → 电脑操作」截图（真实设备/能力/授权/开关往返）；Linux 实机完成：文件与文档、终端与运维、跨应用工作流；**桌面应用工作流仍阻塞**（仅 Windows，Spec 第 84 条） |
| 前端构建后经 systemd 更新，禁止 nohup | 已实现 | `npm run build` 成功；`systemctl restart zops-backend zops-frontend zops-agent@computer_agent`，`is-active` 均 active |

## 三、阻塞清单（需外部条件）

1. **Windows 实机验收**：UIA（记事本/计算器/资源管理器/登记业务应用）、原生批准/拒绝/撤销/超时、锁屏、用户打字与切窗干扰、托盘与 `Ctrl+Alt+Pause`、取消子进程、剪贴板不变、MessageBox。
   → 工具已就绪：`scripts/windows_acceptance.py` 覆盖 13 项（auto 4 + measure 1 + manual 8），缺项、无证据、skip 一律判「未通过」，单测覆盖判定、doctor 解析与编排；批准超时已实现（对话框 30 秒无人操作自动关闭并拒绝，原因码 `desktop_approval_timeout`），该项判定标准随之改为可验证的「到期自动拒绝」。**待 Windows 实机执行**，本轮不产出验收结论。
2. **Windows 生产签名包**：证书在用户 Windows 证书库，本机没有。
   → 工具已就绪：`companion/build.py --sign-thumbprint <SHA1>` 按指纹签名 + 强制 `signtool verify /pa /all`，验签通过才改名为 `companion-<version>`，并写 `build-receipt.json`；`--verify-release` 复核回执/摘要/签名状态。当前目录里只有 `companion/dist/companion-unsigned-test/`（`test_only=true`，严格核验 rc=1）。
3. **macOS AX / Wayland Portal 桌面适配**：未实现，对应平台桌面能力保持禁用（Spec 第 76 条要求先验收文件/脚本/浏览器）。
   → Linux X11 部分已实现并通过真实 X 服务器验证：`companion/x11.py`（libX11/libXtst/libXss，纯 ctypes）+ `X11Driver` + `scripts/x11_target_window.py`（补 `_NET_WM_PID`/`WM_CLASS` 的真实验收窗口）+ `scripts/linux_desktop_check.py`（14 项清单：auto 8 / manual 6）+ `scripts/linux_desktop_attended.py`（把 6 项人工步骤接到真实对话框/租约/热键/锁屏/输入探测，输出可被 `--attended` 消费的证据）。**剩余条件**：一台带真实键鼠与 X11 会话的桌面 Linux（Wayland 会话不支持）与真实锁屏器（`lock_screen` 一项必须由真实锁屏证明），人在机旁执行 `python scripts/linux_desktop_attended.py --config <cfg>`，再 `python scripts/linux_desktop_check.py --attended <json>`。
4. **真实桌面 Chrome 登录态复用**：扩展→Native Host→本机 0600 交接文件→单次领取已在真实 Chromium 上跑通（见一.8）；仍需用户在**带真实站点账号的桌面 Chrome**上验收「免登录成功率」与二次验证降级提示。
5. **真实桌面画面实机验证**：采集代码（`desktop.screen` → PrintWindow、view 范围、敏感焦点暂停）已实现并有单测；需 Windows 实机验证 UWP 窗口（记事本/计算器）与锁屏/密码框暂停行为，不能以 Linux 单测代替。
   → 入口已并入 `windows_acceptance.py` 的 `uia_apps`、`lock_screen` 两项。

恢复条件（缺一不可，具备后从对应用例继续）：

| 阻塞 | 需要的外部条件 | 恢复后的首个动作 |
| --- | --- | --- |
| 1、5 | 一台可安装普通桌面用户的 Windows 10/11 实机（可从 Linux 访问或人工代跑），并配好系统 keyring | `companion-unsigned-test --pair <配对码> --init` → `--accept-desktop`（预检 + 原生确认，写本机验收记录）→ `python scripts/windows_acceptance.py --companion <path>`（13 项全 pass 才算验收通过） |
| 2 | 证书留在 Windows 证书库（给出指纹）+ 可访问 RFC3161 时间戳 | `python -m companion.build --sign-thumbprint <SHA1>`，再 `python -m companion.build --verify-release dist/companion-<version>` 必须 rc=0 |
| 3 | 带真实键鼠的 X11 桌面 Linux（Wayland 会话不支持），或 macOS 主机 | Linux：`--allow-desktop-app <进程>` → `--accept-desktop`（X11 预检 + 原生确认）→ `python scripts/linux_desktop_check.py`（14 项全 pass 才算验收通过）；macOS：新增 AX driver 并跑 `--doctor`；未验收前保持 `desktop.*` 不声明 |
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

# 4) 实机入口（必须在 Windows 上跑，本机不执行）
python scripts/windows_acceptance.py --companion <companion.exe 路径>
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
→ Ran 247 tests ... OK (skipped=5)（跳过的 5 个是 opt-in 的真实对话框用例）
ZOPS_LINUX_DESKTOP_LIVE=1 companion/.venv/bin/python -m unittest tests.cu.test_linux_desktop_live
→ 5 个用例（approve/reject/timeout/revoke/typing_interference）共 59.6s 全部 OK
python3 -m unittest tests.cu.test_devices tests.cu.test_agent_companion \
  tests.cu.test_chat_stop tests.cu.test_download_guard
→ Ran 72 tests ... OK
python3 -m unittest tests.p3.test_cu_login_guard tests.p3.test_cu_bridge → Ran 2 tests OK

# 2) 前端构建与部署
npx tsc --noEmit → rc=0；npm run build → rc=0（BUILD_ID=vFk0WlC_MJJ0cRqM8O33H）
systemctl restart zops-frontend → active
curl -k -H 'Host: gcoc-ai.example.com' https://127.0.0.1/ → 200，首页引用
  /_next/static/chunks/0n3~may8dxulm.js（含「30 秒无人操作自动拒绝」）且经 nginx 200

# 3) 无人值守时必须失败（Xvfb :95 + 真实 venv/配置/验收记录）
python scripts/linux_desktop_attended.py --config … < /dev/null
→ [approve] fail：非交互运行：没有人在机旁，步骤未执行；rc=1，JSON accepted=false
python scripts/linux_desktop_check.py --config … --attended <json> < /dev/null
→ approve 显示「attended: 非交互运行…」，其余 manual 仍 skip；verdict.accepted=false，rc=1

# 4) 冻结包第三次重建（对齐 _close_dialog 句柄修复），并包内复验
companion/.venv/bin/python -m companion.build --ack-unsigned-test
→ sha256=2849e261…，created 2026-10-25T04:11:08Z，test_only=true
--doctor → checks 7/7；WAYLAND_DISPLAY=wayland-9 → wayland_portal_not_implemented
--accept-desktop 无人值守 → 90s、rc=1、local_user_timeout、未写 desktop-acceptance.json
linux_desktop_check.py --companion <冻结包> → auto 8 pass / manual 6 skip / rc=1
```

结论：Linux X11 后端与工具链已真实跑通，但 **Linux 桌面验收仍未通过**：缺的正是「人在机旁」的 6 项人工步骤（批准/拒绝/超时/撤销/锁屏/打字干扰）。本轮已把这三件事补成可执行形态——对话框 30 秒无人操作到期自动**拒绝**（原因码与用户拒绝区分）、Wayland 判定不再漏 `WAYLAND_DISPLAY`、6 项人工步骤有了接真实代码路径的执行器与可被清单消费的证据；并用 XTest 充当「坐在机器前的人」把其中 5 项的**通过分支**在真实对话框上跑通（顺带修了 `await 同步方法`、租约残留、提示语颠倒、验收到期被当成拒绝四类真机缺陷），并把「批准/拒绝/到期/紧急停止/切窗打字」固化成 opt-in 回归（只有锁屏那一项仍需真实锁屏器）。入口文档补齐了 README/QUICKSTART（否则用户找不到这两个脚本），Windows 验收对话框的 HWND 改为 `c_void_p` 传递（否则到点也关不掉框，事后还能被点成同意）；冻结包第三次重建为 `sha256=2849e261…` 并在包内复验了到期、Wayland 与清单。剩下 `lock_screen` 需要真实锁屏器，且验收结论必须由真人在真实桌面完成——机器替身按按钮不算。所以本机默认仍不声明 `desktop.*`。macOS AX 与 Wayland Portal 仍未实现（阻塞 3 剩余部分）。
