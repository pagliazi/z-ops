'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '../web-next');
const projectRequire = createRequire(path.join(root, 'package.json'));
const ts = projectRequire('typescript');
const settle = async () => { for (let n = 0; n < 30; n++) await new Promise(resolve => setImmediate(resolve)); };
const device = { device_id: 'd1', name: '测试电脑', platform: 'windows', online: true, revoked: false, capabilities: ['browser.goto'], version: '2.0.0' };
const task = { task_id: 't1', device_id: 'd1', session_id: 's1', state: 'running', device_name: '测试电脑', frame: { jpeg: '/9j/AA==', w: 640, h: 480 } };
const response = data => new Response(data === undefined ? '' : JSON.stringify(data), { status: 200 });

// 模拟 hooks 调度以实际执行组件 effect/处理器；不对源码字符串做断言。
function environment(mocks = {}) {
  let runner = null, timerId = 0;
  const timers = new Map(), events = new Map(), calls = [], cache = new Map();
  const network = { handler: async () => response({}), token: 'test-bearer' };
  const hooks = {
    useState(initial) {
      const owner = runner, index = owner.cursor++;
      if (!(index in owner.values)) owner.values[index] = typeof initial === 'function' ? initial() : initial;
      return [owner.values[index], next => {
        if (!owner.mounted) owner.lateWrites++;
        owner.values[index] = typeof next === 'function' ? next(owner.values[index]) : next;
      }];
    },
    useRef(initial) {
      const index = runner.cursor++;
      if (!(index in runner.values)) runner.values[index] = { current: initial };
      return runner.values[index];
    },
    useEffect(fn, deps) {
      const index = runner.cursor++, previous = runner.effects[index];
      if (!previous || !deps || deps.some((d, i) => !Object.is(d, previous.deps[i]))) runner.pending.push(() => {
        previous?.cleanup?.(); runner.effects[index] = { deps, cleanup: fn() };
      });
    },
    useMemo(fn, deps) {
      const index = runner.cursor++, previous = runner.values[index];
      if (!previous || !deps || deps.some((d, i) => !Object.is(d, previous.deps[i]))) runner.values[index] = { deps, value: fn() };
      return runner.values[index].value;
    },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
  };
  const globals = {
    Error, AbortController, Response, ReadableStream, TextDecoder, console,
    crypto: { randomUUID: () => 'test-unique-request' },
    requestAnimationFrame: fn => fn(),
    localStorage: { getItem: key => key === 'zops_token' ? network.token : null },
    fetch: async (url, options) => { calls.push({ url, ...options }); return network.handler(url, options); },
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearInterval: id => timers.delete(id),
    Event: class Event { constructor(type) { this.type = type; } },
    window: {
      history: { replaceState() {} }, location: { search: '' },
      confirm: () => true,
      addEventListener: (type, fn) => { if (!events.has(type)) events.set(type, new Set()); events.get(type).add(fn); },
      removeEventListener: (type, fn) => events.get(type)?.delete(fn),
      dispatchEvent: event => events.get(event.type)?.forEach(fn => fn()),
    },
  };
  function load(relative) {
    const file = path.join(root, 'src', relative);
    if (cache.has(file)) return cache.get(file);
    const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } });
    const exports = {}; cache.set(file, exports);
    vm.runInNewContext(compiled.outputText, { ...globals, exports, require: id => id === 'react' ? hooks
      : Object.hasOwn(mocks, id) ? mocks[id]
      : id.startsWith('@/') ? load(id.slice(2) + '.tsx') : projectRequire(id) }, { filename: file });
    return exports;
  }
  function mount(component, props = {}) {
    const own = { values: [], effects: [], cursor: 0, pending: [], mounted: true, lateWrites: 0 };
    return {
      state: own,
      render() { runner = own; own.cursor = 0; own.pending = []; const tree = component(props); own.pending.forEach(fn => fn()); runner = null; return tree; },
      unmount() { own.mounted = false; own.effects.forEach(e => e?.cleanup?.()); },
    };
  }
  async function tick(delay) {
    const due = [...timers.entries()].filter(([, timer]) => timer.delay === delay);
    due.forEach(([id, timer]) => { timers.delete(id); timer.fn(); });
    await settle();
  }
  return { load, mount, calls, network, globals, timers, tick };
}
function nodes(tree, type) {
  if (tree == null || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(t => nodes(t, type));
  return [...(tree.type === type ? [tree] : []), ...nodes(tree.props?.children, type)];
}
function textOf(tree) {
  if (tree == null || typeof tree === 'boolean') return '';
  if (typeof tree !== 'object') return String(tree);
  if (Array.isArray(tree)) return tree.map(textOf).join('');
  return textOf(tree.props?.children);
}

test('前端合同：Bearer 仅认证头、no-store、空成功响应及失败退出', async () => {
  const e = environment(), { cuRequest } = e.load('components/ComputerUseSettings.tsx');
  e.network.handler = async () => response();
  assert.equal(await cuRequest('/api/cu/tasks/t1/control', new AbortController().signal, 'POST', { action: 'pause' }), undefined);
  const call = e.calls[0];
  assert.equal(call.url, '/api/cu/tasks/t1/control'); assert.equal(call.cache, 'no-store');
  assert.equal(call.headers.Authorization, 'Bearer test-bearer'); assert.deepEqual(JSON.parse(call.body), { action: 'pause' });
  e.network.handler = async () => new Response(JSON.stringify({ detail: '拒绝' }), { status: 403 });
  await assert.rejects(cuRequest('/api/cu/devices', new AbortController().signal), /403/);
  e.network.token = '';
  await assert.rejects(cuRequest('/api/cu/devices', new AbortController().signal), /登录已失效/);
  assert.equal(e.calls.length, 2);
});

test('前端合同：AbortController 取消实际在途请求，超时不重发', async () => {
  const e = environment(), { cuRequest } = e.load('components/ComputerUseSettings.tsx');
  e.network.handler = async (_url, opts) => new Promise((_resolve, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  const controller = new AbortController();
  const work = cuRequest('/api/cu/devices', controller.signal);
  controller.abort(); await assert.rejects(work);
  assert.equal(e.calls[0].signal.aborted, true);
  const timed = cuRequest('/api/cu/devices', new AbortController().signal);
  const rejected = assert.rejects(timed, /请求超时/);
  await e.tick(15000); await rejected;
  assert.equal(e.calls.length, 2); assert.equal(e.timers.size, 0);
});

test('单个在线设备也不自动选择；连续点击仅提交一次并等服务器确认', async () => {
  const e = environment(), { ComputerUseSettings } = e.load('components/ComputerUseSettings.tsx');
  let selected = null, finish;
  e.network.handler = async (url, opts) => {
    if (url === '/api/cu/prefs') return response({ push_frames: true });
    if (opts.method === 'PUT') { await new Promise(r => { finish = r; }); selected = 'd1'; return response(); }
    return response({ devices: [device], selected_device_id: selected });
  };
  const ui = e.mount(ComputerUseSettings); ui.render(); await settle();
  const tree = ui.render(); assert.equal(e.calls.filter(c => c.method === 'PUT').length, 0);
  const choose = nodes(tree, 'button').find(b => textOf(b) === '选择此设备');
  choose.props.onClick(); choose.props.onClick(); await settle();
  assert.equal(e.calls.filter(c => c.method === 'PUT').length, 1);
  assert.equal(nodes(ui.render(), 'button').find(b => textOf(b) === '选择此设备').props.disabled, true);
  finish(); await settle();
  assert.match(textOf(ui.render()), /已选择 测试电脑/);
  ui.unmount();
});

test('关闭设置会中止所有请求，晚到响应不写入旧状态', async () => {
  const e = environment(), { ComputerUseSettings } = e.load('components/ComputerUseSettings.tsx');
  e.network.handler = async (_url, opts) => new Promise((_resolve, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  const ui = e.mount(ComputerUseSettings); ui.render(); ui.unmount(); await settle();
  assert.equal(e.calls.length, 2); assert.ok(e.calls.every(c => c.signal.aborted)); assert.equal(ui.state.lateWrites, 0);
});

test('任务、设备、会话三重绑定；未授权/暂停/unknown/非法 frame 不展示', () => {
  const e = environment(), { validateCuTask, taskFrame, BrowserLivePanel } = e.load('components/chat/BrowserLivePanel.tsx');
  assert.equal(validateCuTask(task, 's1', 't1', 'd1'), task);
  for (const args of [['s2', 't1', 'd1'], ['s1', 't2', 'd1'], ['s1', 't1', 'd2'], ['s1', 't1', '']]) assert.throws(() => validateCuTask(task, ...args));
  assert.equal(taskFrame(task, false), ''); assert.equal(taskFrame(task, true), 'data:image/jpeg;base64,/9j/AA==');
  for (const state of ['unknown', 'paused', 'cancelled', 'denied']) assert.equal(taskFrame({ ...task, state }, true), '');
  assert.equal(taskFrame({ ...task, frame: { jpeg: 'https://private/frame.jpg', w: 10, h: 10 } }, true), '');
  const a = BrowserLivePanel({ open: true, sessionId: 's1', taskId: 't1', deviceId: 'd1' });
  const b = BrowserLivePanel({ open: true, sessionId: 's2', taskId: 't1', deviceId: 'd1' });
  assert.notEqual(a.key, b.key); assert.equal(BrowserLivePanel({ open: false }), null);
});

test('小窗切换时中止在途请求并清除画面，跨设备状态不能渲染', async () => {
  const e = environment(), { BrowserLivePanel } = e.load('components/chat/BrowserLivePanel.tsx');
  e.network.handler = async url => url.includes('?session_id=') ? response({ tasks: [task] })
    : url.endsWith('/status') ? response({ ...task, device_id: 'another-device' }) : response({ push_frames: true });
  const panel = BrowserLivePanel({ open: true, sessionId: 's1', taskId: 't1', deviceId: 'd1' });
  const ui = e.mount(panel.type, panel.props); ui.render(); await settle();
  assert.equal(nodes(ui.render(), 'img').length, 0); assert.match(textOf(ui.render()), /绑定不匹配/);
  e.network.handler = async (_url, opts) => new Promise((_resolve, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  await e.tick(2000); ui.unmount(); await settle();
  assert.equal(e.calls.at(-1).signal.aborted, true); assert.equal(ui.state.lateWrites, 0);
});

test('暂停请求不冒充暂停确认；真正状态确认后清除画面', async () => {
  const e = environment(), { BrowserLivePanel } = e.load('components/chat/BrowserLivePanel.tsx');
  let state = 'running';
  e.network.handler = async (url, opts) => url.includes('?session_id=') ? response({ tasks: [task] })
    : url.endsWith('/status') ? response({ ...task, state }) : url.endsWith('/control') ? response() : response({ push_frames: true });
  const panel = BrowserLivePanel({ open: true, sessionId: 's1', taskId: 't1', deviceId: 'd1' });
  const ui = e.mount(panel.type, panel.props); ui.render(); await settle();
  assert.equal(nodes(ui.render(), 'img').length, 1);
  const pause = nodes(ui.render(), 'button').find(b => textOf(b) === '暂停');
  pause.props.onClick(); pause.props.onClick(); await settle();
  let tree = ui.render(); await settle(); tree = ui.render();
  assert.equal(e.calls.filter(c => c.url.endsWith('/control')).length, 1);
  assert.match(textOf(tree), /已请求暂停，等待执行器确认/); assert.equal(nodes(tree, 'img').length, 0);
  state = 'paused'; await e.tick(2000);
  assert.match(textOf(ui.render()), /已确认暂停/); ui.unmount();
});

function chatEnvironment() {
  const e = environment({
    'next/navigation': { useRouter: () => ({ push() {} }), useSearchParams: () => new URLSearchParams() },
    '@/lib/api': { getSessions: async () => [], getUsage: async () => ({}) },
    '@/components/Sidebar': {}, '@/components/chat/ResizeHandle': {},
    '@/components/MarkdownContent': { MarkdownContent: () => null },
    '@/components/chat/ComputerUseMessage': { ComputerUseMessage: () => null },
  });
  return { ...e, chat: e.load('components/chat/ChatApp.tsx') };
}

test('历史与事件补读保留 Companion 元数据，并拒绝跨会话历史', async () => {
  const e = chatEnvironment(), metadata = { cu_backend: 'companion', cu_task_id: 't1', device_id: 'd1', session_id: 's1' };
  e.network.handler = async () => response({ session_id: 's1', messages: [{ role: 'assistant', metadata }, { metadata: { session_id: 's2' } }] });
  const history = await e.chat.readSessionHistory('s1', new AbortController().signal);
  assert.equal(history.length, 1); assert.equal(history[0].metadata.cu_task_id, 't1');
  await assert.rejects(e.chat.readSessionHistory('s2', new AbortController().signal), /绑定不匹配/);
  e.network.handler = async () => response({ terminal: true, events: [{ seq: 1, type: 'reply', data: { text: '完成', metadata } }] });
  const recovered = [];
  for await (const event of e.chat.recoverSessionReply('chat-task', new AbortController().signal)) recovered.push(event);
  assert.equal(recovered[0].metadata.device_id, 'd1');
  assert.equal(e.calls.at(-1).method, 'GET');
});

test('聊天流实际取消底层读取和请求；不重发 POST、不丢失任务元数据', async () => {
  const e = chatEnvironment(); let cancelled = 0;
  e.network.handler = async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"progress","metadata":{"cu_backend":"companion","task_id":"t1"}}\n\n')); },
    cancel() { cancelled++; },
  }));
  const controller = new AbortController(), stream = e.chat.streamSessionReply({ message: '测试' }, controller.signal);
  const first = await stream.next(); assert.equal(first.value.metadata.task_id, 't1');
  const next = stream.next(); controller.abort(); await assert.rejects(next);
  assert.equal(cancelled, 1); assert.equal(e.calls.length, 1); assert.equal(e.calls[0].signal.aborted, true);
  assert.equal(e.calls[0].headers['Idempotency-Key'], 'test-unique-request'); assert.equal(e.timers.size, 0);
});

test('聊天会话切换会中止 SSE 与历史请求，旧事件不进入新会话', async () => {
  const e = chatEnvironment(); let cancelled = 0;
  e.network.handler = async url => {
    if (url === '/api/chat') return new Response(new ReadableStream({
      start(c) { c.enqueue(new TextEncoder().encode('data: {"type":"start","session_id":"s1","task_id":"chat-task"}\n\n')); },
      cancel() { cancelled++; },
    }));
    if (url.startsWith('/api/chat/messages/')) return new Promise(() => {});
    if (url === '/api/cu/devices') return response({ devices: [], selected_device_id: null });
    return response({});
  };
  const ui = e.mount(e.chat.default); ui.render(); await settle();
  const send = nodes(ui.render(), 'div').flatMap(n => Array.isArray(n.props.children) ? n.props.children : [n.props.children])
    .find(n => n?.props?.onSend && n?.props?.onStop)?.props.onSend;
  assert.equal(typeof send, 'function');
  const work = send('测试任务'); await settle();
  const sessionPanel = ui.render().props.children[0]; sessionPanel.props.onNew(); await work; await settle();
  assert.equal(cancelled, 1); assert.equal(e.calls.find(c => c.url === '/api/chat').signal.aborted, true);
  assert.equal(ui.render().props.children[2].props.sessionId, '');
  ui.render().props.children[0].props.onSelect('s2'); await settle();
  ui.render().props.children[0].props.onNew();
  assert.equal(e.calls.find(c => c.url.includes('/messages/s2')).signal.aborted, true);
  ui.unmount();
});

test('旧 CU 问询和子代理记录不能作为普通自动续跑或图片消息', () => {
  const e = chatEnvironment();
  for (const fields of [{ cu_ask: {} }, { agents: ['computer_agent'] }, { steps: [{ source: 'computer_agent' }] }, { file_url: '/api/cu/old.jpg' }]) {
    assert.equal(e.chat.isCuMessage({ id: 'x', role: 'assistant', content: '', ...fields }), true);
  }
  assert.equal(e.chat.isCuMessage({ id: 'x', role: 'assistant', content: '普通文字' }), false);
});

test('CU Markdown 不加载图片/HTML/下载地址，仍保留基本文本格式', async () => {
  const marked = await import(pathToFileURL(projectRequire.resolve('marked')).href);
  const file = path.join(root, 'src/components/chat/ComputerUseMessage.tsx');
  const source = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(source, { exports, require: id => id === 'marked' ? marked : projectRequire(id) });
  const html = exports.computerUseHtml('**完成**\n\n![截图](/api/cu/frame.jpg)\n\n[下载](/api/cu/frame.jpg)\n\n<img src="https://private.test" onerror="alert(1)">');
  assert.match(html, /<strong>完成<\/strong>/);
  assert.doesNotMatch(html, /<img\b|<a\b|<script\b/i);
});
