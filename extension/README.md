# Z-OPS Companion 浏览器扩展 2.x

本扩展只通过**本机 Native Messaging** `net.zops.companion` 与 Companion 通信：
不连接 Z-OPS 服务器、不使用连接码或账号密码、不接管个人标签、不读取密码。
扩展本身不是执行器安装包；网页任务由 Companion 的独立无头浏览器执行。

## 安装（开发者模式）

1. 在 Z-OPS「设置 → 电脑操作」下载扩展 zip（或访问 `/api/cu/extension/download`）。
2. 解压后打开 `chrome://extensions/` → 开启「开发者模式」→「加载已解压的扩展程序」。
3. 先安装 Companion 并在本机配置中登记扩展 origin：
   `native_origins: ["chrome-extension://<扩展ID>/"]`。
4. 注册 Native Messaging Host 清单（`type: stdio`，`allowed_origins` 与上一步完全一致）。
   Chromium/Chrome 只从**用户数据目录**下的 `NativeMessagingHosts/` 读取清单：
   - Linux 默认配置：`~/.config/google-chrome/NativeMessagingHosts/`（Chromium 为 `~/.config/chromium/`）；
   - 若浏览器用 `--user-data-dir=/path/profile` 启动（含自动化/独立工作配置），
     清单必须放到 `/path/profile/NativeMessagingHosts/`，否则 `connectNative` 只会得到
     `Can't find manifest for native messaging host net.zops.companion`；
   - Windows：`HKCU\Software\Google\Chrome\NativeMessagingHosts\net.zops.companion`
     指向清单文件；macOS：`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`。
   清单文件名必须是 `net.zops.companion.json`，`path` 指向可执行的 Companion 启动脚本。
5. 在 Z-OPS 生成设备配对码，在 Companion 本机输入并确认，然后在设置页明确选择设备。
6. 点击扩展图标可查看本机通道状态；「重新检查本机连接」只重建本机端口。

扩展只显示状态，不授予桌面同意、不批准接管，也不会绕过 Companion 的本机授权。

## 复用当前 Chrome 登录态（可选）

某些站点需要现有 Chrome 登录态时，可主动点击弹窗中的「授权当前站点登录态」：

- 只读取**当前标签所在站点**的 Cookie，其他站点不读取，也不导出整个浏览器配置。
- Cookie 经 Native Messaging 交给本机 Companion，写入用户私有状态目录（0600），
  最多 5 分钟有效、单次使用；任务浏览器创建时领取并立即删除。
- Cookie 不经过 Z-OPS 服务器、不进入模型输入、快照、日志或审计；弹窗只显示数量。
- 登录态只在任务浏览器中生效，不修改你的日常 Chrome；不保证一定免登录，
  站点要求二次验证时仍会在小窗提示需要本机登录。
- 站点必须在 Companion 本机 `origins` 中已登记；未登记的站点直接拒绝。
- 不想复用时不要点击；已写入的交接文件会在下次任务创建或过期时删除。

## 安全边界

- 端口只接受协议 2、完整任务绑定、序号/过期/代际/fencing 校验的命令；
  本机回执（`result`/`task_status`/`login_handoff_result`）不进入命令路径，避免回执被当成
  非法命令再回显而形成消息回环。
- 只操作本次创建的任务窗口，不枚举、不激活、不接管个人标签；窗口被移走即暂停。
- 不提供后台截屏；截图不可用时如实返回 `unavailable`，不切换标签补截图。
- 密码、OTP、token 输入框在页面侧拒绝读取和填写；遇到登录墙暂停。
- 旧 1.x 的 server/token/username 凭据在启动时清除，云端直连已删除，无静默回退。

## 测试

```bash
cd /opt/zops && node --test extension/background.test.cjs
```

当前 20 条 Node 用例覆盖：只连本机 Host、旧凭据清除、弹窗不能授权/执行、命令绑定与重放、
登录态单次交接与回执不含值、本机回执不回环、写结果 unknown 不重放。
