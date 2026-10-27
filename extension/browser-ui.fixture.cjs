'use strict';

// 隔离浏览器验证：只在内存加载当前前端源码，全部 API 使用合成数据，不构建/重启主应用。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { createRequire } = require('node:module');
const { randomUUID } = require('node:crypto');
const root = path.resolve(__dirname, '../web-next');
const projectRequire = createRequire(path.join(root, 'package.json'));
const ts = projectRequire('typescript');
const prefix = `/cu-ui-${randomUUID()}`;
const port = Number(process.env.CU_UI_PORT || 3019);
const modules = new Map();
const compile = (source, filename) => ts.transpileModule(source, {
  fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, allowJs: true },
}).outputText;
const mocks = {
  'next/navigation': `const React = require('react'); exports.useSearchParams = () => new URLSearchParams(React.useSyncExternalStore(cb => { window.addEventListener('popstate', cb); return () => window.removeEventListener('popstate', cb); }, () => location.search)); exports.useRouter = () => ({ push(url) { history.pushState(null, '', url); window.dispatchEvent(new Event('popstate')); } });`,
  '@/components/chat/GlobalSidebar': `exports.initialsOf = value => (value || '测试').slice(0, 2);`,
  '@/lib/api': `exports.getSessions = async () => (await (await fetch('/api/chat/sessions')).json()).sessions; exports.getUsage = async () => ({}); exports.deleteSession = async () => {}; exports.renameSession = async () => {};`,
};
const entry = `
import { Fragment, useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { SettingsModal } from '@/components/SettingsModal';
import ChatApp from '@/components/chat/ChatApp';
function Fixture() {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState('web');
  const [note, setNote] = useState('全部接口为合成数据；不是 Companion 实机验收');
  useEffect(() => {
    const listener = e => { setTab(['web', 'computer', 'computer-use'].includes(e.detail?.tab) ? 'web' : e.detail?.tab || 'web'); setOpen(true); };
    window.addEventListener('zops:open-settings', listener);
    return () => window.removeEventListener('zops:open-settings', listener);
  }, []);
  const change = state => { window.__cuFixture.tasks[0].state = state; window.__cuFixture.tasks[0].reason = state === 'unknown' ? '回执丢失，需本机核对，禁止重放写操作' : state === 'paused' ? '执行器确认暂停' : ''; setNote('合成执行器状态：' + state); };
  return <Fragment>
    <header className="border-b border-gray-200 bg-gray-50 p-3 text-xs flex gap-3 items-center">
      <strong>当前源码 · 隔离界面验证</strong>
      <button className="rounded-lg border border-gray-200 bg-white p-2" onClick={() => setOpen(true)}>打开电脑操作设置</button>
      <button className="rounded-lg border border-gray-200 bg-white p-2" onClick={() => change('paused')}>合成执行器确认暂停</button>
      <button className="rounded-lg border border-gray-200 bg-white p-2" onClick={() => change('unknown')}>模拟结果未知</button>
      <span>{note}</span>
    </header>
    <div style={{ height: 'calc(100vh - 70px)' }}><ChatApp /></div>
    <SettingsModal open={open} onClose={() => setOpen(false)} initialTab={tab} username="cu-ui-synthetic" displayName="合成测试" role="user" />
  </Fragment>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`;
function collect(id, source) {
  if (modules.has(id)) return id;
  const code = source === undefined ? fs.readFileSync(id, 'utf8') : source;
  const compiled = /\.tsx?$/.test(id) || id === 'fixture-entry' || /marked[\\/]lib/.test(id) ? compile(code, id + '.tsx') : code;
  const deps = {};
  modules.set(id, { compiled, deps });
  for (const match of compiled.matchAll(/\brequire\(['"]([^'"]+)['"]\)/g)) {
    const name = match[1];
    if (name === 'util' || name === 'stream') throw new Error('验证模块不允许 Node 内置依赖');
    if (Object.hasOwn(mocks, name)) deps[name] = collect(`mock:${name}`, mocks[name]);
    else {
      let target;
      if (name.startsWith('@/')) target = path.join(root, 'src', name.slice(2) + '.tsx');
      else target = createRequire(path.isAbsolute(id) ? id : path.join(root, 'package.json')).resolve(name);
      deps[name] = collect(target);
    }
  }
  return id;
}
collect('fixture-entry', entry);
const bundle = `const process = {env:{NODE_ENV:'production'}}; const modules={${[...modules].map(([id, m]) => `${JSON.stringify(id)}:[function(require,module,exports){\n${m.compiled}\n},${JSON.stringify(m.deps)}]`).join(',')}}; const cache={}; function load(id){if(cache[id])return cache[id].exports;const module=cache[id]={exports:{}};const [fn,deps]=modules[id];fn(name=>load(deps[name]),module,module.exports);return module.exports;} load('fixture-entry');`;
const mockScript = `
localStorage.setItem('zops_token','synthetic-ui-token');
localStorage.setItem('zops_user',JSON.stringify({username:'cu-ui-synthetic',role:'user'}));
const canvas=document.createElement('canvas');canvas.width=640;canvas.height=320;
const ctx=canvas.getContext('2d');ctx.fillStyle='#f3f4f6';ctx.fillRect(0,0,640,320);ctx.fillStyle='#374151';ctx.font='24px sans-serif';ctx.fillText('合成任务画面 · 无个人数据',80,155);
const frame={jpeg:canvas.toDataURL('image/jpeg').split(',')[1],w:640,h:320};
const fixture=window.__cuFixture={calls:[],selected:null,pushFrames:true,devices:[
  {device_id:'d1',name:'测试 Windows 电脑',platform:'windows',online:true,revoked:false,version:'2.0.0',capabilities:['browser.goto','browser.snapshot']},
  {device_id:'d2',name:'测试离线电脑',platform:'linux',online:false,revoked:false,version:'2.0.0',capabilities:['files.read']}],
  tasks:[{task_id:'t1',device_id:'d1',device_name:'测试 Windows 电脑',session_id:'s1',state:'running',reason:'',execution_mode:'unfocused_window',frame}]
};
const reply=(data,status=200)=>new Response(data===undefined?'':JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
window.fetch=async (input,options={})=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href),method=options.method||'GET';
  fixture.calls.push({url:url.pathname,method,body:options.body||null});
  if(options.signal?.aborted)throw new DOMException('取消','AbortError');
  if(url.pathname==='/api/cu/devices')return reply({devices:fixture.devices,selected_device_id:fixture.selected});
  if(url.pathname==='/api/cu/devices/selection'){fixture.selected=JSON.parse(options.body).device_id;return reply();}
  if(url.pathname==='/api/cu/devices/pair-code')return reply({code:'UI-MOCK-ONLY',expires_in:90});
  if(url.pathname.startsWith('/api/cu/devices/')&&method==='DELETE'){fixture.devices.find(d=>d.device_id===url.pathname.split('/').pop()).revoked=true;return reply();}
  if(url.pathname==='/api/cu/prefs'){if(method==='PUT')fixture.pushFrames=JSON.parse(options.body).push_frames;return reply({push_frames:fixture.pushFrames});}
  if(url.pathname==='/api/cu/tasks')return reply({tasks:fixture.tasks.filter(t=>t.session_id===url.searchParams.get('session_id'))});
  if(url.pathname.endsWith('/status'))return reply(fixture.tasks[0]);
  if(url.pathname.endsWith('/control'))return reply();
  if(url.pathname==='/api/chat/sessions')return reply({sessions:[{id:'s1',title:'合成任务会话',updated_at:'测试'},{id:'s2',title:'空白会话（隔离检查）',updated_at:'测试'}]});
  if(url.pathname.startsWith('/api/chat/messages/')){const sid=url.pathname.split('/').pop();return reply({session_id:sid,messages:sid==='s1'?[{role:'assistant',source:'computer_agent',content:'正在处理合成任务。![旧截图](/api/cu/old.jpg)',metadata:{cu_backend:'companion',device_id:'d1',cu_task_id:'t1',session_id:'s1'}}]:[]});}
  if(url.pathname==='/api/memory/search')return reply({results:[]});
  if(url.pathname==='/api/memory/settings')return reply({memory_enabled:true});
  if(url.pathname==='/api/auth/me')return reply({username:'cu-ui-synthetic',role:'user'});
  if(url.pathname==='/api/chat')return new Response('data: '+JSON.stringify({type:'start',session_id:'s1',task_id:'chat-t1'})+'\\n\\ndata: '+JSON.stringify({type:'reply',text:'合成回复',metadata:{cu_backend:'companion',device_id:'d1',cu_task_id:'t1'}})+'\\n\\ndata: [DONE]\\n\\n',{headers:{'Content-Type':'text/event-stream'}});
  return reply({});
};
`;
const css = process.env.CU_UI_CSS || 'http://198.18.18.147:3000/_next/static/chunks/172nx.1m7yh.l.css';
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Computer Use 当前源码隔离验证</title><link rel="stylesheet" href="${css}"><style>body{margin:0;background:#fff;color:#374151;font-family:Arial,sans-serif}button{cursor:pointer}button:disabled{cursor:default}aside[aria-label="电脑操作任务小窗"]{width:410px;max-width:calc(100vw - 2rem)}</style></head><body><div id="root"></div><script>${mockScript}</script><script src="${prefix}/app.js"></script></body></html>`;
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  res.setHeader('Cache-Control', 'no-store');
  if (pathname === prefix + '/app.js') { res.setHeader('Content-Type', 'application/javascript'); res.end(bundle); }
  else if (pathname === prefix || pathname === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html); }
  else { res.statusCode = 404; res.end('隔离验证路径不存在'); }
});
server.listen(port, '0.0.0.0', () => console.log(`当前源码隔离验证：http://198.18.18.147:${port}${prefix}`));
process.on('SIGTERM', () => server.close());
process.on('SIGINT', () => server.close());
