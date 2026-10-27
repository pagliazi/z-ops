'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { createCompanionBridge } = require('./background.js');

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn), emit: (...args) => [...listeners].map(fn => fn(...args)) };
}
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const text = value => ({ nodeType: 3, textContent: value });
function element(tag, attributes = {}, children = []) {
  let writes = [], reads = 0, clicks = 0;
  return {
    nodeType: 1, tagName: tag, childNodes: children, disabled: false,
    get value() { reads++; throw new Error('禁止读取 DOM value'); },
    set value(value) { writes.push(value); },
    getAttribute: key => attributes[key] || '',
    hasAttribute: key => Object.hasOwn(attributes, key),
    getBoundingClientRect: () => ({ width: 100, height: 20 }),
    closest: () => null, dispatchEvent() {}, click() { clicks++; },
    inspect: () => ({ reads, writes, clicks }), attributes,
  };
}
function page(nodes = [element('BUTTON', {}, [text('执行')])]) {
  const body = element('BODY', {}, [element('P', {}, [text('公开内容')]), ...nodes]);
  const document = {
    body, contains: e => nodes.includes(e),
    querySelectorAll: selector => selector === 'input, textarea, select'
      ? nodes.filter(e => /^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)) : nodes,
  };
  const context = vm.createContext({ document, location: { pathname: '/work', origin: 'https://example.test' },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), scrollBy() {} }, Event: class Event {} });
  return { nodes, context };
}
async function mock(options = {}) {
  const calls = { creates: [], updates: [], gets: [], scripts: [], queries: 0, captures: 0, activations: 0, hosts: [], cookies: [] };
  const messages = [], store = options.store || { token: 'legacy-secret', server: 'https://old.invalid' };
  const personal = page([element('BUTTON', {}, [text('个人页面')])]);
  const tabs = new Map([[7, { id: 7, windowId: 8, page: personal }]]);
  const native = { onMessage: event(), onDisconnect: event(), postMessage: m => messages.push(m), disconnect() {} };
  const api = {
    storage: { local: {
      async remove(keys) { keys.forEach(k => delete store[k]); },
      async get(keys) { return Object.fromEntries(keys.filter(k => k in store).map(k => [k, store[k]])); },
      async set(values) { if (options.failJournal) throw new Error('磁盘不可写'); Object.assign(store, structuredClone(values)); },
    } },
    runtime: { id: 'test-extension', getURL: path => `chrome-extension://test-extension/${path}`, onMessage: event(),
      connectNative(host) { calls.hosts.push(host); if (options.noHost) throw new Error('未安装'); return native; } },
    windows: { onRemoved: event(), async create(params) {
      calls.creates.push(params); if (params.focused !== false) calls.activations++;
      const tab = { id: 100 + calls.creates.length, windowId: 200 + calls.creates.length, page: options.page || page() };
      tabs.set(tab.id, tab);
      if (options.createGate) await options.createGate.promise;
      return { id: tab.windowId, tabs: [{ id: tab.id, windowId: tab.windowId }] };
    }, async update() { calls.activations++; throw new Error('不应激活窗口'); } },
    tabs: { onRemoved: event(), onUpdated: event(), onDetached: event(),
      async get(id) { calls.gets.push(id); const tab = tabs.get(id); if (!tab) throw new Error('标签已关闭'); return { id, windowId: tab.windowId }; },
      async query() { calls.queries++; throw new Error('不应枚举标签'); },
      async captureVisibleTab() { calls.captures++; throw new Error('不应截取用户可见页面'); },
      async update(id, params) {
        calls.updates.push({ id, params }); if (params.active) calls.activations++;
        if (options.updateGate) await options.updateGate.promise;
        const tab = tabs.get(id); const url = new URL(params.url);
        tab.page.context.location = { pathname: url.pathname, origin: url.origin };
        return { id, windowId: tab.windowId };
      },
    },
    scripting: { async executeScript(spec) {
      calls.scripts.push(spec.target);
      const tab = tabs.get(spec.target.tabId);
      tab.page.context.args = structuredClone(spec.args);
      const answer = vm.runInContext(`(${spec.func.toString()})(...args)`, tab.page.context);
      return [{ frameId: 0, result: structuredClone(answer) }];
    } },
    cookies: { async getAll(params) { calls.cookies.push(params); return options.cookies || []; } },
  };
  const bridge = createCompanionBridge(api);
  await bridge.ready; await Promise.resolve();
  return { api, bridge, calls, tabs, native, messages, store, personal };
}
function command(seq = 1, overrides = {}) {
  return { type: 'command', protocol: 2, actor_id: 'actor-1', device_id: 'device-1', session_id: 'session-1', task_id: 'task-1',
    command_id: `cmd-${seq}`, seq, expiry: Date.now() + 60000, connection_generation: 1, fencing_token: 'fence-1',
    method: 'browser.goto', params: { url: 'https://example.test/work' }, ...overrides };
}
async function waitUntil(check) { for (let i = 0; i < 100; i++) { if (check()) return; await new Promise(r => setImmediate(r)); } throw new Error('等待超时'); }

test('只连接 Native Host，清理旧 token；popup 不能授予权限或执行命令', async () => {
  const m = await mock();
  assert.deepEqual(m.calls.hosts, ['net.zops.companion']);
  assert.equal(m.store.token, undefined); assert.equal(m.store.server, undefined);
  let reply;
  m.api.runtime.onMessage.emit(command(), { id: 'test-extension', url: m.api.runtime.getURL('popup.html') }, r => { reply = r; });
  assert.equal(reply, undefined); assert.equal(m.calls.creates.length, 0);
  m.api.runtime.onMessage.emit({ type: 'cu-local-status' }, { id: 'other', url: m.api.runtime.getURL('popup.html') }, r => { reply = r; });
  assert.equal(reply, undefined);
  m.api.runtime.onMessage.emit({ type: 'cu-local-status' }, { id: 'test-extension', url: m.api.runtime.getURL('popup.html') }, r => { reply = r; });
  assert.equal(reply.connected, false);
});

test('登录态只在显式点击后交接，只含当前站点且回执不回传值', async () => {
  const cookies = [
    { name: 'sid', value: 'SYNTHETIC_COOKIE', domain: '.example.test', path: '/',
      secure: true, httpOnly: true, sameSite: 'lax', expirationDate: Math.floor(Date.now() / 1000) + 3600 },
    { name: '', value: 'empty-name-rejected' },
    { name: 'big', value: 'x'.repeat(4097) },
  ];
  const m = await mock({ cookies });
  let reply;
  const returned = m.api.runtime.onMessage.emit(
    { type: 'cu-login-handoff', origin: 'https://example.test/work?token=SYNTHETIC_QUERY' },
    { id: 'test-extension', url: m.api.runtime.getURL('popup.html') }, r => { reply = r; })[0];
  assert.equal(returned, true);
  await waitUntil(() => reply);
  assert.deepEqual(reply, { ok: true, count: 1 });
  const message = m.messages.find(x => x.type === 'login_handoff');
  assert.equal(message.origin, 'https://example.test');
  assert.equal(message.cookies.length, 1);
  assert.equal(message.cookies[0].value, 'SYNTHETIC_COOKIE');
  assert.ok(message.cookies[0].httpOnly);
  assert.ok(message.expiry > Date.now() / 1000);
  assert.ok(!JSON.stringify(reply).includes('SYNTHETIC'));
  assert.ok(!JSON.stringify(m.store).includes('SYNTHETIC_COOKIE'));
});

test('登录态交接拒绝非 http(s)、无 Cookie、非弹窗来源与未连接', async () => {
  const empty = await mock({ cookies: [] });
  let reply;
  empty.api.runtime.onMessage.emit({ type: 'cu-login-handoff', origin: 'chrome-extension://test/popup.html' },
    { id: 'test-extension', url: empty.api.runtime.getURL('popup.html') }, r => { reply = r; });
  await waitUntil(() => reply);
  assert.equal(reply.error, 'invalid_origin');
  assert.equal(empty.calls.cookies.length, 0);
  reply = undefined;
  empty.api.runtime.onMessage.emit({ type: 'cu-login-handoff', origin: 'https://example.test/' },
    { id: 'test-extension', url: empty.api.runtime.getURL('popup.html') }, r => { reply = r; });
  await waitUntil(() => reply);
  assert.equal(reply.error, 'no_cookies_for_site');
  reply = undefined;
  empty.api.runtime.onMessage.emit({ type: 'cu-login-handoff', origin: 'https://example.test/' },
    { id: 'other', url: empty.api.runtime.getURL('popup.html') }, r => { reply = r; });
  await new Promise(r => setImmediate(r));
  assert.equal(reply, undefined);
  const missing = await mock({ noHost: true });
  missing.api.runtime.onMessage.emit({ type: 'cu-login-handoff', origin: 'https://example.test/' },
    { id: 'test-extension', url: missing.api.runtime.getURL('popup.html') }, r => { reply = r; });
  await waitUntil(() => reply);
  assert.equal(reply.error, 'native_not_connected');
});

test('原生 Host 缺失时诚实未连接，绝不执行或回退', async () => {
  const m = await mock({ noHost: true });
  assert.equal(m.bridge.status().connected, false);
  assert.equal(await m.bridge.receive(command()), undefined);
  assert.equal(m.calls.creates.length, 0);
});

test('Native Port 真实入口执行协议 2，result 回传完整绑定', async () => {
  const m = await mock(); const request = command();
  m.native.onMessage.emit(request);
  await waitUntil(() => m.messages.some(x => x.type === 'result'));
  const response = m.messages.find(x => x.type === 'result');
  for (const field of ['actor_id', 'device_id', 'session_id', 'task_id', 'command_id', 'seq', 'expiry', 'connection_generation', 'fencing_token']) assert.equal(response[field], request[field]);
  assert.equal(response.status, 'ok'); assert.equal(response.data.fully_background, false);
  assert.equal(m.calls.creates[0].focused, false);
});

test('个人 tab、枚举、activate_tab、旧协议与任意 frame 参数均拒绝', async () => {
  const m = await mock();
  const personal = await m.bridge.receive(command(1, { params: { url: 'https://example.test', tab_id: 7 } }));
  assert.equal(personal.error.code, 'personal_tab_forbidden');
  assert.equal(m.calls.gets.length, 0); assert.equal(m.calls.creates.length, 0);
  for (const method of ['activate_tab', 'tabs', 'open_tab']) {
    const answer = await m.bridge.receive(command(2, { method, params: { tab_id: 7 } }));
    assert.equal(answer.status, 'denied');
  }
  assert.equal((await m.bridge.receive(command(2, { protocol: 1 }))).status, 'denied');
  assert.equal((await m.bridge.receive(command(2, { params: { url: 'https://example.test', frame_ids: [4] } }))).status, 'denied');
  assert.equal(m.calls.queries, 0); assert.equal(m.calls.activations, 0);
  assert.equal(m.personal.nodes[0].inspect().clicks, 0);
});

test('已拥有标签仍不能访问个人标签或其他任务标签', async () => {
  const m = await mock(); await m.bridge.receive(command());
  await m.bridge.receive(command(1, { task_id: 'task-2', command_id: 'other-1' }));
  const answer = await m.bridge.receive(command(2, { method: 'browser.snapshot', params: { tab_id: 102 } }));
  assert.equal(answer.error.code, 'personal_tab_forbidden');
  assert.equal(m.calls.scripts.length, 0);
});

test('截图明确 unavailable，绝不枚举、激活或调用 captureVisibleTab', async () => {
  const m = await mock(); await m.bridge.receive(command());
  const answer = await m.bridge.receive(command(2, { method: 'browser.screenshot', params: {} }));
  assert.equal(answer.status, 'unavailable'); assert.equal(answer.data, null);
  assert.equal(m.calls.queries, 0); assert.equal(m.calls.activations, 0); assert.equal(m.calls.captures, 0);
  assert.deepEqual(m.calls.updates.map(x => x.params), [{ url: 'https://example.test/work' }]);
});

test('任务取消确认后排队及后续操作不再执行', async () => {
  const m = await mock(); await m.bridge.receive(command());
  const answer = await m.bridge.receive(command(2, { type: 'control', action: 'cancel' }));
  assert.equal(answer.status, 'cancelled');
  assert.equal((await m.bridge.receive(command(3))).status, 'cancelled');
  assert.equal(m.calls.updates.length, 1); assert.equal(m.calls.creates.length, 1);
});

test('取消在途写操作返回 unknown，后续写操作不自动重放', async () => {
  const updateGate = defer(), m = await mock({ updateGate });
  const writing = m.bridge.receive(command());
  await waitUntil(() => m.calls.updates.length === 1);
  const cancel = await m.bridge.receive(command(2, { type: 'control', action: 'cancel' }));
  assert.equal(cancel.status, 'unknown');
  updateGate.resolve(); assert.equal((await writing).status, 'unknown');
  assert.equal((await m.bridge.receive(command(3))).status, 'unknown');
  assert.equal((await m.bridge.receive(command(4, { type: 'control', action: 'cancel' }))).status, 'unknown');
  assert.equal(m.calls.updates.length, 1);
});

test('取消窗口创建期间不继续导航；窗口关闭不自动重建', async () => {
  const createGate = defer(), m = await mock({ createGate });
  const first = m.bridge.receive(command());
  await waitUntil(() => m.calls.creates.length === 1);
  await m.bridge.receive(command(2, { type: 'control', action: 'pause' }));
  createGate.resolve(); assert.equal((await first).status, 'unknown');
  assert.equal(m.calls.updates.length, 0);
  const normal = await mock(); await normal.bridge.receive(command());
  normal.api.windows.onRemoved.emit(201);
  assert.equal((await normal.bridge.receive(command(2))).status, 'paused');
  assert.equal(normal.calls.creates.length, 1);
});

test('移出任务窗口、任务标签关闭均暂停，不收养个人标签', async () => {
  for (const move of [true, false]) {
    const m = await mock(); await m.bridge.receive(command());
    if (move) m.tabs.get(101).windowId = 8;
    else m.api.tabs.onRemoved.emit(101);
    const response = await m.bridge.receive(command(2, { method: 'browser.snapshot', params: {} }));
    assert.equal(response.status, 'paused'); assert.equal(m.calls.scripts.length, 0);
  }
});

test('过期、重放、乱序、跨身份/会话/设备、fencing/generation 均拒绝', async () => {
  const m = await mock(); await m.bridge.receive(command());
  const variations = [
    { seq: 1 }, { seq: 9 }, { expiry: Date.now() - 1 }, { actor_id: 'other' }, { device_id: 'other' },
    { session_id: 'other' }, { fencing_token: 'other' }, { connection_generation: 2 }, { command_id: 'cmd-1' },
  ];
  for (const override of variations) assert.equal((await m.bridge.receive(command(2, override))).status, 'denied');
  assert.equal(m.calls.updates.length, 1);
});

test('重启墓碑阻止旧任务重建；安全记录写失败先于副作用拒绝', async () => {
  const m = await mock(); await m.bridge.receive(command()); m.bridge.dispose();
  const restarted = await mock({ store: m.store });
  assert.equal((await restarted.bridge.receive(command())).error.code, 'restart_requires_local_review');
  assert.equal(restarted.calls.creates.length, 0);
  const failing = await mock({ failJournal: true });
  assert.equal((await failing.bridge.receive(command())).status, 'denied');
  assert.equal(failing.calls.creates.length, 0);
});

test('DOM snapshot 不读取任何 input.value，token 与敏感 autofill 不进入快照', async () => {
  const secret = element('INPUT', { name: 'api_token', type: 'text' });
  const autofill = element('INPUT', { autocomplete: 'cc-number' });
  const textarea = element('TEXTAREA', {}, [text('私密草稿')]);
  const normal = element('INPUT', { name: 'search' });
  const button = element('BUTTON', {}, [text('查询')]);
  const m = await mock({ page: page([secret, autofill, textarea, normal, button]) });
  await m.bridge.receive(command());
  const response = await m.bridge.receive(command(2, { method: 'browser.snapshot', params: {} }));
  assert.equal(response.status, 'ok');
  assert.deepEqual(response.data.elements.map(e => e.text), ['[敏感字段]', '[敏感字段]', '', '', '查询']);
  assert.equal(response.data.body.replace(/\s+/g, ' ').trim(), '公开内容 查询');
  for (const node of [secret, autofill, textarea, normal]) assert.equal(node.inspect().reads, 0);
});

test('登录页/password/OTP 必须暂停且从未读取密码', async () => {
  for (const attrs of [{ type: 'password' }, { autocomplete: 'one-time-code' }, { name: 'otp' }]) {
    const input = element('INPUT', attrs), m = await mock({ page: page([input]) });
    await m.bridge.receive(command());
    const answer = await m.bridge.receive(command(2, { method: 'browser.snapshot', params: {} }));
    assert.equal(answer.status, 'paused'); assert.equal(answer.error.code, 'login_required');
    assert.equal(input.inspect().reads, 0);
    assert.equal((await m.bridge.receive(command(3))).status, 'paused');
  }
});

test('动作使用当前任务 snapshot 的真实节点，敏感输入拒绝且不会读取', async () => {
  const input = element('INPUT', { name: 'query', autocomplete: 'off' });
  const m = await mock({ page: page([input]) }); await m.bridge.receive(command());
  await m.bridge.receive(command(2, { method: 'browser.snapshot', params: {} }));
  const fill = await m.bridge.receive(command(3, { method: 'browser.act', params: { ln: 0, verb: 'fill', value: 'hello', snapshot_id: 'cmd-2' } }));
  assert.equal(fill.status, 'ok'); assert.deepEqual(input.inspect(), { reads: 0, writes: ['hello'], clicks: 0 });
  assert.equal((await m.bridge.receive(command(4, { method: 'browser.act', params: { ln: 0, verb: 'click', snapshot_id: 'cmd-2' } }))).status, 'denied');
  await m.bridge.receive(command(5, { method: 'browser.snapshot', params: {} }));
  input.attributes.name = 'api_token';
  const secret = await m.bridge.receive(command(6, { method: 'browser.act', params: { ln: 0, verb: 'fill', value: 'blocked', snapshot_id: 'cmd-5' } }));
  assert.equal(secret.status, 'paused'); assert.deepEqual(input.inspect().writes, ['hello']);
});

test('敏感祖先与空 contenteditable 不泄露子节点，也不接受动作', async () => {
  for (const attrs of [{ id: 'secret' }, { contenteditable: '' }, { hidden: '' }, { 'aria-hidden': 'true' }]) {
    const child = element('BUTTON', {}, [text('synthetic-secret-8831')]);
    const parent = element('DIV', attrs, [child]); child.parentElement = parent;
    const dom = page([child]); dom.context.document.body.childNodes = [parent];
    const m = await mock({ page: dom }); await m.bridge.receive(command());
    const snapshot = await m.bridge.receive(command(2, { method: 'browser.snapshot', params: {} }));
    assert.equal(snapshot.status, 'ok'); assert.equal(snapshot.data.body.trim(), '');
    assert.equal(snapshot.data.elements[0].text, '[敏感字段]');
    const action = await m.bridge.receive(command(3, { method: 'browser.act', params: { ln: 0, verb: 'click', snapshot_id: 'cmd-2' } }));
    assert.equal(action.status, 'paused'); assert.equal(child.inspect().clicks, 0);
  }
});

test('用户活动 tab 更新无状态上报；Native 断开后不重连重放', async () => {
  const m = await mock(); await m.bridge.receive(command());
  const count = m.messages.length;
  m.api.tabs.onUpdated.emit(7, { status: 'complete', url: 'https://private.test' });
  await Promise.resolve(); assert.equal(m.messages.length, count);
  m.native.onDisconnect.emit(); assert.equal(m.bridge.status().connected, false);
  assert.equal(await m.bridge.receive(command(2)), undefined);
  assert.equal(m.calls.hosts.length, 1); assert.equal(m.calls.updates.length, 1);
});

test('Native 回执不进入命令路径，不回显、不形成消息回环', async () => {
  const m = await mock();
  const before = m.messages.length;
  for (const ack of [{ type: 'login_handoff_result', protocol: 2, ok: true, count: 1 },
    { type: 'result', protocol: 2, status: 'denied' },
    { type: 'task_status', protocol: 2, status: 'paused' }]) {
    m.native.onMessage.emit(ack);
  }
  await new Promise(r => setImmediate(r));
  assert.equal(m.messages.length, before);
});
