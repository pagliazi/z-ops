// Z-OPS Companion 2.x：仅接收本机 Native Host 的任务绑定命令，不存在云端直连降级。
'use strict';

const NATIVE_HOST = 'net.zops.companion';
const BINDING = ['actor_id', 'device_id', 'session_id', 'task_id', 'command_id', 'seq', 'expiry', 'connection_generation', 'fencing_token'];
const METHODS = new Set(['browser.goto', 'browser.snapshot', 'browser.act', 'browser.scroll', 'browser.screenshot']);
const JOURNAL = 'cu_v2_seen_tasks';

// 此函数注入 isolated world；所有检查与 DOM 操作在同一次注入内完成。
function pageCommand(method, params, context) {
  const fail = (status, code) => ({ status, error: { code } });
  if (Date.now() >= context.deadline) return fail('denied', 'expired');
  const attr = (e, key) => (e.getAttribute && e.getAttribute(key)) || '';
  const sensitive = e => {
    const marker = ['type', 'name', 'id', 'autocomplete', 'placeholder', 'aria-label'].map(k => attr(e, k)).join(' ');
    const autofill = attr(e, 'autocomplete').toLowerCase();
    return /password|passwd|pwd|secret|token|otp|one.?time|verification|credential|验证码|密码|口令|信用卡|cc-/i.test(marker) ||
      (!!autofill && !['on', 'off'].includes(autofill));
  };
  const inputs = [...document.querySelectorAll('input, textarea, select')];
  const login = /(?:login|log-in|signin|sign-in|sso|oauth|authorize|authentication|verify|otp)(?:[/.?_-]|$)/i.test(location.pathname) ||
    inputs.some(e => /password/i.test(attr(e, 'type')) || /password|one-time-code/.test(attr(e, 'autocomplete')) || /(?:password|passwd|pwd|otp|验证码)/i.test(attr(e, 'name') + ' ' + attr(e, 'id')));
  if (login) return fail('paused', 'login_required');
  const visible = e => {
    const r = e.getBoundingClientRect();
    const s = window.getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && !e.disabled;
  };
  const controlTag = /^(INPUT|TEXTAREA|SELECT|OPTION|SCRIPT|STYLE|NOSCRIPT|IFRAME)$/;
  const protectedNode = e => sensitive(e) || e.isContentEditable || e.hasAttribute?.('contenteditable') ||
    e.hasAttribute?.('hidden') || attr(e, 'aria-hidden') === 'true';
  const protectedTree = e => {
    let depth = 0;
    for (let node = e; node; node = node.parentElement) {
      if (++depth > 64 || protectedNode(node)) return true;
    }
    return false;
  };
  function safeText(node, depth = 0) {
    if (!node || depth > 24) return '';
    if (node.nodeType === 3) return node.textContent || '';
    if (controlTag.test(node.tagName || '') || protectedNode(node)) return '';
    if (node.nodeType === 1) {
      const style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return '';
    }
    return [...(node.childNodes || [])].slice(0, 300).map(n => safeText(n, depth + 1)).join(' ').slice(0, 4000);
  }
  const redact = text => text.replace(/(?:bearer\s+\S+|(?:password|token|secret|otp|密码|验证码)\s*[:=：]\s*\S+)/gi, '[已隐藏]').slice(0, 2000);
  const elements = () => [...document.querySelectorAll('a, button, input, select, textarea, [role="button"], [role="link"]')].filter(visible).slice(0, 100);
  if (method === 'browser.snapshot') {
    const els = elements();
    globalThis.__zopsCuSnapshot = { id: context.command_id, task: context.task_id, els };
    return { status: 'ok', data: {
      snapshot_id: context.command_id, origin: location.origin, frame_ids: [0],
      elements: els.map((e, n) => ({ n, tag: (e.tagName || '').toLowerCase(),
        text: protectedTree(e) ? '[敏感字段]' : redact(safeText(e)).trim().slice(0, 80), sensitive: protectedTree(e) })),
      body: redact(safeText(document.body)),
    } };
  }
  if (method === 'guard') return { status: 'ok', data: {} };
  if (method === 'browser.scroll') {
    window.scrollBy(0, params.dy);
    return { status: 'ok', data: { scrolled: true } };
  }
  if (method !== 'browser.act') return fail('denied', 'method_not_allowed');
  const snapshot = globalThis.__zopsCuSnapshot;
  if (!snapshot || snapshot.task !== context.task_id || snapshot.id !== params.snapshot_id) return fail('denied', 'stale_snapshot');
  const e = snapshot.els[params.ln];
  if (!e || !document.contains(e) || !visible(e)) return fail('denied', 'stale_element');
  if (protectedTree(e)) return fail('paused', 'sensitive_input');
  // 防止链接/表单自动开启窗口或激活其他浏览上下文。
  const form = e.closest?.('form');
  if ((attr(e, 'target') && attr(e, 'target') !== '_self') || (form && attr(form, 'target') && attr(form, 'target') !== '_self') || attr(e, 'formtarget')) return fail('denied', 'new_context_not_allowed');
  if (params.verb === 'click') {
    e.click();
  } else if (params.verb === 'fill' && /^(INPUT|TEXTAREA)$/.test(e.tagName)) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e), 'value')?.set;
    if (setter) setter.call(e, params.value); else e.value = params.value;
    e.dispatchEvent(new Event('input', { bubbles: true }));
    e.dispatchEvent(new Event('change', { bubbles: true }));
  } else if (params.verb === 'select' && e.tagName === 'SELECT') {
    e.value = params.value;
    e.dispatchEvent(new Event('change', { bubbles: true }));
  } else return fail('denied', 'action_not_allowed');
  delete globalThis.__zopsCuSnapshot;
  return { status: 'ok', data: { performed: true } };
}

function createCompanionBridge(api, options = {}) {
  const now = options.now || Date.now;
  const tasks = new Map();
  const seen = new Set();
  let port = null, connected = false, stopped = false, identity = null;
  let message = '未连接 Companion，请安装并注册本机 Native Host';
  let writeChain = Promise.resolve();
  const pick = m => Object.fromEntries(BINDING.map(k => [k, m[k]]));
  const deadlineOf = expiry => typeof expiry === 'number' ? (expiry < 1e12 ? expiry * 1000 : expiry) : NaN;
  const send = data => { if (port) { try { port.postMessage(data); } catch { disconnect(); } } };
  const result = (m, status, data, code) => {
    const out = { type: 'result', protocol: 2, ...pick(m), status, data: data || null, error: code ? { code } : null };
    send(out); return out;
  };
  const event = task => send({ type: 'task_status', protocol: 2, ...pick(task.last), status: task.state, error: { code: task.reason } });
  function halt(task, reason, cancel = false) {
    if (task.state === 'cancelled' || task.state === 'unknown') return;
    task.state = task.inflightWrite ? 'unknown' : cancel ? 'cancelled' : 'paused';
    task.reason = task.inflightWrite ? 'write_outcome_unknown' : reason;
    task.snapshot = ''; event(task);
  }
  function disconnect() {
    port = null; connected = false; identity = null;
    message = 'Companion 未连接或已断开；任务已暂停，未知写操作不会自动重放';
    for (const task of tasks.values()) halt(task, 'native_disconnected');
  }
  const ready = (async () => {
    // 清除 1.x 遗留凭据；绝不读取或复用旧 token。
    await api.storage.local.remove(['server', 'token', 'username', 'authType']);
    const saved = await api.storage.local.get([JOURNAL]);
    if (saved[JOURNAL] !== undefined && !Array.isArray(saved[JOURNAL])) throw new Error('invalid_journal');
    for (const id of saved[JOURNAL] || []) seen.add(id);
  })();
  // 先持久化任务墓碑再触发副作用；重启不接管旧窗口、不恢复未确认操作。
  function journal() {
    const save = () => api.storage.local.set({ [JOURNAL]: [...seen] });
    writeChain = writeChain.then(save);
    return writeChain;
  }
  function envelope(m) {
    if (!m || m.protocol !== 2 || !['command', 'control'].includes(m.type)) return 'protocol_required';
    for (const key of ['actor_id', 'device_id', 'session_id', 'task_id', 'command_id', 'fencing_token']) {
      if (typeof m[key] !== 'string' || !m[key].trim() || m[key].length > 256) return 'binding_required';
    }
    if (!Number.isSafeInteger(m.seq) || m.seq < 1 || !Number.isSafeInteger(m.connection_generation) || m.connection_generation < 1) return 'invalid_sequence';
    const deadline = deadlineOf(m.expiry);
    if (!Number.isFinite(deadline) || deadline <= now() || deadline - now() > 300000) return 'expired_or_invalid_expiry';
    if (identity && (m.actor_id !== identity.actor_id || m.device_id !== identity.device_id || m.connection_generation !== identity.connection_generation)) return 'connection_binding_mismatch';
    if (m.type === 'command' && !METHODS.has(m.method)) return 'method_not_allowed';
    if (m.type === 'control' && !['pause', 'cancel'].includes(m.action)) return 'control_not_allowed';
    return '';
  }
  function sameTask(task, m) {
    return ['actor_id', 'device_id', 'session_id', 'task_id', 'connection_generation', 'fencing_token'].every(k => task.binding[k] === m[k]);
  }
  function guard(task, m) {
    if (!port || stopped || task.state !== 'running') throw new Error(task.reason || 'task_not_running');
    if (deadlineOf(m.expiry) <= now()) throw new Error('expired');
  }
  async function owned(task, m) {
    guard(task, m);
    if (task.tabId == null) throw new Error('owned_tab_required');
    const tab = await api.tabs.get(task.tabId);
    guard(task, m);
    if (tab.id !== task.tabId || tab.windowId !== task.windowId) { halt(task, 'owned_tab_moved'); throw new Error('owned_tab_moved'); }
    return tab;
  }
  function paramsOf(task, m) {
    const p = m.params;
    if (!p || typeof p !== 'object' || Array.isArray(p)) throw new Error('invalid_params');
    const allowed = {
      'browser.goto': ['url'], 'browser.snapshot': [], 'browser.screenshot': [],
      'browser.act': ['ln', 'verb', 'value', 'snapshot_id'], 'browser.scroll': ['direction', 'amount', 'dy'],
    }[m.method];
    for (const key of Object.keys(p)) {
      if (key === 'tab_id') {
        if (task.tabId == null || p.tab_id !== task.tabId) throw new Error('personal_tab_forbidden');
      } else if (key === 'window_id') {
        if (task.windowId == null || p.window_id !== task.windowId) throw new Error('personal_window_forbidden');
      } else if (!allowed.includes(key)) throw new Error('param_not_allowed');
    }
    if (m.method === 'browser.goto') {
      const url = new URL(p.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('url_not_allowed');
    }
    if (m.method === 'browser.act') {
      if (!['click', 'fill', 'select'].includes(p.verb) || !Number.isInteger(p.ln) || p.ln < 0 || p.ln >= 100 ||
          !task.snapshot || p.snapshot_id !== task.snapshot ||
          (p.verb !== 'click' && (typeof p.value !== 'string' || p.value.length > 4000))) throw new Error('invalid_or_stale_action');
    }
    if (m.method === 'browser.scroll') {
      const dy = p.dy ?? ((p.direction === 'up' ? -1 : 1) * (p.amount ?? 600));
      if (!Number.isFinite(dy) || Math.abs(dy) > 3000) throw new Error('invalid_scroll');
      return { ...p, dy };
    }
    return p;
  }
  async function inject(task, m, method, params) {
    await owned(task, m);
    guard(task, m);
    const data = await api.scripting.executeScript({ target: { tabId: task.tabId, frameIds: [0] },
      func: pageCommand, args: [method, params, { command_id: m.command_id, task_id: m.task_id, deadline: deadlineOf(m.expiry) }] });
    guard(task, m);
    const answer = data?.[0]?.result;
    if (!answer || !['ok', 'paused', 'denied'].includes(answer.status)) throw new Error('invalid_page_result');
    if (answer.status === 'paused') { task.inflightWrite = false; halt(task, answer.error?.code || 'local_confirmation_required'); }
    return answer;
  }
  async function execute(task, m) {
    let sideEffect = false;
    try {
      guard(task, m);
      const p = paramsOf(task, m);
      if (m.method === 'browser.screenshot') return result(m, 'unavailable', null, 'background_screenshot_unavailable');
      if (m.method === 'browser.goto') {
        if (task.tabId == null) {
          guard(task, m); sideEffect = true; task.inflightWrite = true;
          const win = await api.windows.create({ url: 'about:blank', focused: false, type: 'normal', width: 1180, height: 820 });
          // 只认本次创建 API 返回的标签，不查询现存标签，也不收养 popup。
          if (!Number.isInteger(win?.id) || win.tabs?.length !== 1 || !Number.isInteger(win.tabs[0].id)) throw new Error('owned_window_unavailable');
          task.windowId = win.id; task.tabId = win.tabs[0].id;
          guard(task, m); task.inflightWrite = false;
        } else {
          const check = await inject(task, m, 'guard', {});
          if (check.status !== 'ok') return result(m, check.status, check.data, check.error?.code);
        }
        await owned(task, m); guard(task, m);
        sideEffect = true; task.inflightWrite = true; task.snapshot = '';
        await api.tabs.update(task.tabId, { url: p.url });
        guard(task, m); task.inflightWrite = false;
        return result(m, 'ok', { tab_id: task.tabId, execution_mode: 'unfocused_window', navigation: 'requested', fully_background: false });
      }
      await owned(task, m);
      if (m.method === 'browser.act') { sideEffect = true; task.inflightWrite = true; }
      const answer = await inject(task, m, m.method, p);
      task.inflightWrite = false;
      if (m.method === 'browser.snapshot' && answer.status === 'ok') task.snapshot = m.command_id;
      if (m.method === 'browser.act') task.snapshot = '';
      return result(m, answer.status, answer.data, answer.error?.code);
    } catch (error) {
      const unknown = sideEffect || task.state === 'unknown';
      if (unknown) { task.state = 'unknown'; task.reason = 'write_outcome_unknown'; task.snapshot = ''; }
      return result(m, unknown ? 'unknown' : task.state === 'running' ? 'denied' : task.state, null,
        unknown ? 'write_outcome_unknown' : error.message || 'command_failed');
    } finally { task.inflightWrite = false; }
  }
  async function receive(m) {
    if (!port || stopped) return;
    const incomingPort = port;
    try { await ready; } catch { return result(m || {}, 'denied', null, 'local_journal_unavailable'); }
    if (port !== incomingPort || stopped) return;
    const invalid = envelope(m);
    if (invalid) return result(m || {}, 'denied', null, invalid);
    if (!identity) identity = { actor_id: m.actor_id, device_id: m.device_id, connection_generation: m.connection_generation };
    connected = true; message = 'Companion 本机通道已连接；授权范围由本机配置决定';
    let task = tasks.get(m.task_id);
    if (task && !sameTask(task, m)) return result(m, 'denied', null, 'task_binding_mismatch');
    if (!task) {
      if (seen.has(m.task_id)) return result(m, 'paused', null, 'restart_requires_local_review');
      if (seen.size >= 10000) return result(m, 'denied', null, 'local_journal_full');
      if (m.seq !== 1 || (m.type === 'command' && m.method !== 'browser.goto')) return result(m, 'denied', null, 'new_task_requires_goto');
      task = { binding: pick(m), last: { ...pick(m), type: m.type }, seq: 0, ids: new Set(), state: 'running', reason: '',
        tabId: null, windowId: null, snapshot: '', inflightWrite: false, queue: Promise.resolve() };
      tasks.set(m.task_id, task); seen.add(m.task_id);
      task.persisted = journal();
    }
    if (m.seq !== task.seq + 1 || task.ids.has(m.command_id)) return result(m, 'denied', null, 'replay_or_out_of_order');
    if (task.ids.size >= 10000) return result(m, 'denied', null, 'task_command_limit');
    task.seq = m.seq; task.ids.add(m.command_id); task.last = { ...pick(m), type: m.type };
    // 控制消息不排在写动作之后，先阻止后续命令；在途写操作只能报告 unknown。
    if (m.type === 'control') {
      halt(task, m.action === 'cancel' ? 'cancelled_by_companion' : 'paused_by_companion', m.action === 'cancel');
      try { await task.persisted; } catch { return result(m, 'denied', null, 'local_journal_unavailable'); }
      return result(m, task.state, null, task.reason);
    }
    const job = task.queue.then(async () => {
      try { await task.persisted; } catch { halt(task, 'local_journal_unavailable'); return result(m, 'denied', null, 'local_journal_unavailable'); }
      return execute(task, m);
    });
    task.queue = job.catch(() => {});
    return job;
  }
  function connect() {
    if (port || stopped) return;
    connected = false; message = '正在检查 Companion，本机 Native Host 未响应前不视为已连接';
    try {
      const native = api.runtime.connectNative(NATIVE_HOST); port = native;
      native.onMessage.addListener(m => {
        if (port !== native) return;
        if (m?.type === 'hello' && m.protocol === 2) {
          // 只展示本机通道状态; 不因此获得任何云端任务或用户标签权限。
          identity = { actor_id: m.actor_id, device_id: m.device_id, connection_generation: m.connection_generation };
          connected = true;
          message = 'Companion 本机通道已连接；扩展只显示状态，任务由 Companion 执行';
          return;
        }
        if (m?.type === 'pong') return;
        // 只把命令/控制帧交给任务队列。本机回执（login_handoff_result、result、task_status）
        // 若进入命令路径会被判为非法信封再回显，形成扩展与 Native Host 之间的消息回环。
        if (m?.type === 'command' || m?.type === 'control') void receive(m);
      });
      native.onDisconnect.addListener(() => {
        void api.runtime.lastError;
        if (port === native) disconnect();
      });
      native.postMessage({ type: 'hello', protocol: 2, version: '2.0.0', capabilities: [...METHODS].filter(m => m !== 'browser.screenshot'), screenshot: 'unavailable' });
    } catch { disconnect(); }
  }
  // 用户在本机弹窗明确点击后，只把当前站点 Cookie 交给本机 Companion；
  // 不导出整个配置、不经过 Z-OPS 服务器、不写入扩展存储。
  async function handoff(origin) {
    if (!port && !stopped) connect();
    if (!port || stopped) return { ok: false, error: 'native_not_connected' };
    let site;
    try { site = new URL(origin); } catch { return { ok: false, error: 'invalid_origin' }; }
    if (!['http:', 'https:'].includes(site.protocol) || site.username || site.password) {
      return { ok: false, error: 'invalid_origin' };
    }
    if (!api.cookies || typeof api.cookies.getAll !== 'function') return { ok: false, error: 'cookies_unavailable' };
    let list;
    try { list = await api.cookies.getAll({ url: site.origin + '/' }); }
    catch { return { ok: false, error: 'cookies_unavailable' }; }
    if (!Array.isArray(list)) return { ok: false, error: 'cookies_unavailable' };
    const cookies = list.filter(c => c && typeof c.name === 'string' && c.name &&
        typeof c.value === 'string' && c.value.length <= 4096).slice(0, 200).map(c => {
      const item = { name: c.name, value: c.value };
      if (typeof c.domain === 'string' && c.domain) item.domain = c.domain;
      if (typeof c.path === 'string' && c.path) item.path = c.path;
      item.secure = c.secure === true;
      item.httpOnly = c.httpOnly === true;
      if (['no_restriction', 'lax', 'strict', 'unspecified'].includes(c.sameSite)) item.sameSite = c.sameSite;
      if (Number.isFinite(c.expirationDate)) item.expirationDate = c.expirationDate;
      return item;
    });
    if (!cookies.length) return { ok: false, error: 'no_cookies_for_site' };
    try {
      port.postMessage({ type: 'login_handoff', protocol: 2, origin: site.origin,
        cookies, expiry: Date.now() / 1000 + 300 });
    } catch { disconnect(); return { ok: false, error: 'native_not_connected' }; }
    return { ok: true, count: cookies.length };
  }
  api.windows.onRemoved.addListener(id => { for (const t of tasks.values()) if (t.windowId === id) halt(t, 'task_window_closed'); });
  api.tabs.onRemoved.addListener(id => { for (const t of tasks.values()) if (t.tabId === id) halt(t, 'task_tab_closed'); });
  api.tabs.onDetached?.addListener(id => { for (const t of tasks.values()) if (t.tabId === id) halt(t, 'owned_tab_moved'); });
  api.tabs.onUpdated.addListener((id, change) => {
    for (const t of tasks.values()) {
      if (t.tabId !== id || t.state !== 'running') continue;
      if (change.url || change.status === 'loading') t.snapshot = '';
      // 只检查任务标签的登录墙，不推送用户活动标签状态。
      if (change.status === 'complete' && t.last.type === 'command') {
        const m = t.last;
        void inject(t, m, 'guard', {}).catch(() => {});
      }
    }
  });
  api.runtime.onMessage.addListener((m, sender, reply) => {
    if (sender.id !== api.runtime.id || sender.url !== api.runtime.getURL('popup.html')) return false;
    if (m?.type === 'cu-local-status') reply({ connected, protocol: 2, message, tasks: tasks.size, screenshot: 'unavailable' });
    if (m?.type === 'cu-local-reconnect') { connect(); reply({ connected, message }); }
    if (m?.type === 'cu-login-handoff') {
      // 只接受显式点击触发的站点；回执只含数量/错误码，从不回传 Cookie 值。
      if (typeof m.origin !== 'string' || m.origin.length > 2048) reply({ ok: false, error: 'invalid_origin' });
      else void handoff(m.origin).then(reply);
      return true;
    }
    return false;
  });
  void ready.then(connect).catch(() => { message = '本机安全记录不可用，已禁止执行'; });
  return { ready, receive, status: () => ({ connected, message }), dispose() { stopped = true; const previous = port; disconnect(); previous?.disconnect(); } };
}

if (typeof module !== 'undefined' && module.exports) module.exports = { createCompanionBridge, pageCommand };
else createCompanionBridge(chrome);
