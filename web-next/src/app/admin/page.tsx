'use client';

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { KeyRound, 
  Shield, Users, Key, FileText, History, Brain,
  Bot, ShieldCheck, Server, Info, Save, RefreshCw, Trash2, Plus, Eye, EyeOff, Settings, Activity,
  Bell, BellRing, Mail, MessageSquare, TestTube, CheckCircle, XCircle, Loader2, Settings2, ChevronDown,
  LayoutDashboard, Search, Copy, Pencil, Send, Globe, Clock, Monitor,
} from 'lucide-react';
import { getApprovalMeta, getApprovalCustomers } from '@/lib/api';
import { ComboPanel } from '@/components/ComboPanel';
import { PageHeader } from '@/components/PageHeader';

function getInitials(name: string): string {
  if (!name) return 'U';
  if (/[\u4e00-\u9fa5]/.test(name)) return name.charAt(0).toUpperCase();
  const words = name.split(/\s+/);
  if (words.length >= 2) return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
  return name.charAt(0).toUpperCase();
}

interface User {
  username: string;
  display_name?: string;
  role: 'admin' | 'user';
  group?: string;
  biz_perms?: string[];        // 个人业务权限点 (与系统角色分离)
  inherited_perms?: string[];  // 从所属组继承的业务权限 (只读, 编辑走组配置)
  scopes?: BizScope;           // 个人范围约束 (site/customer; 空=不限; 与组 scope 合并生效)
  biz_perms_ttl?: Record<string, number>;  // 临时授权 {perm: expire_ts 秒}; 到期自动失效
  auth_source?: string;
  source?: string;
  last_login?: number;
}

// 范围约束 (L4): 空 = 不限; 有效范围 = 个人 ∪ 组
type BizScope = { sites: string[]; customers: string[] };
const parseScopeList = (v: string) => v.split(/[,，、;；]/).map(x => x.trim()).filter(Boolean).slice(0, 50);
// 组权限配置 (biz_perms/scope: 组级授权, 全体成员继承; 有效 = 个人 ∪ 组)
type GroupPerm = { tabs: string[]; nms_groups: string[]; biz_perms: string[]; scope?: BizScope };

// 当前登录用户名（用于禁止删除/降级自己）
function getMyUsername(): string {
  try { return JSON.parse(localStorage.getItem('zops_user') || '{}').username || ''; } catch { return ''; }
}

function formatLastLogin(ts?: number): string {
  if (!ts) return '从未登录';
  const d = new Date(ts * 1000);
  const diff = Date.now() - d.getTime();
  if (diff < 3600e3) return `${Math.max(1, Math.floor(diff / 60e3))} 分钟前`;
  if (diff < 86400e3) return `${Math.floor(diff / 3600e3)} 小时前`;
  if (diff < 7 * 86400e3) return `${Math.floor(diff / 86400e3)} 天前`;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const USER_GROUPS = ['GCOC', 'GNOC', 'PSC', 'COPS', 'NQA', 'SOC', 'NSC', 'CNS', 'PROV'];

interface Overview {
  version?: string;
  app?: string;
  description?: string;
  agents?: Record<string, any>;
  channels?: Record<string, any>;
  agent_count?: number;
  total_skills?: number;
  llm_usage_count?: number;
  llm_provider?: string;
  llm_model?: string;
  llm_real_model?: string;
  llm_model_alias?: boolean;
}

const adminTabs = [
  { id: 'overview', icon: LayoutDashboard, label: '总览', color: 'bg-slate-600' },
  { id: 'perm', icon: ShieldCheck, label: '权限管理', color: 'bg-teal-500' },
  { id: 'oneapi_keys', icon: Key, label: 'API Keys', color: 'bg-amber-500' },
  { id: 'audit', icon: FileText, label: '审计日志', color: 'bg-slate-500' },
  { id: 'llm', icon: Brain, label: '模型配置', color: 'bg-purple-500' },
  { id: 'agent_models', icon: Bot, label: 'Agent 模型', color: 'bg-cyan-500' },
  { id: 'notify', icon: Bell, label: '通知配置', color: 'bg-pink-500' },
  { id: 'sanitize', icon: ShieldCheck, label: '数据脱敏', color: 'bg-emerald-500' },
  { id: 'ldap', icon: Server, label: 'LDAP/AD', color: 'bg-indigo-500' },
  { id: 'system', icon: Info, label: '系统信息', color: 'bg-gray-500' },
  { id: 'skills', icon: Bot, label: '技能生命周期', color: 'bg-rose-500' },
  { id: 'memories', icon: Brain, label: '记忆管理', color: 'bg-violet-500' },
  { id: 'computer_use', icon: Monitor, label: 'Computer Use', color: 'bg-indigo-500' },
  { id: 'plugins', icon: Bot, label: '插件', color: 'bg-fuchsia-500' },
];


// ═══ Computer Use 配置管理 ═══
function ComputerUseConfig() {
  const [cfg, setCfg] = useState<any>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');
  const [testUrl, setTestUrl] = useState('');
  const [testResult, setTestResult] = useState<any>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch('/api/admin/computer-use/config',
        { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (r.ok) setCfg(await r.json());
    } catch { /* 网络异常 */ }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true); setMsg('');
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch('/api/admin/computer-use/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify(cfg),
      });
      const d = await r.json();
      if (r.ok) { setMsg('✅ 配置已保存 (运行时生效, 无需重启)'); setCfg(d.config); }
      else setMsg(`❌ ${d.detail || r.status}`);
    } catch (e: any) { setMsg('❌ ' + e.message); }
    setSaving(false);
    setTimeout(() => setMsg(''), 4000);
  };

  const testDomain = async () => {
    if (!testUrl.trim()) return;
    setTestResult(null);
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch('/api/admin/computer-use/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ url: testUrl.trim() }),
      });
      setTestResult(await r.json());
    } catch { /* 网络异常 */ }
  };

  const clearSessions = async () => {
    if (!confirm('清除所有持久浏览器会话 (登录态)?')) return;
    const token = localStorage.getItem('zops_token');
    await fetch('/api/admin/computer-use/sessions/clear', {
      method: 'POST', headers: { 'Authorization': `Bearer ${token || ''}` },
    });
    setMsg('✅ 浏览器会话已清除');
    setTimeout(() => setMsg(''), 3000);
  };

  const card = 'bg-white border border-gray-200 rounded-xl p-4 mb-4';
  const label = 'block text-[13px] font-medium text-gray-900 mb-1';
  const input = 'w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50 focus:bg-white focus:ring-1 focus:ring-indigo-300 outline-none transition-all';

  return (
    <div className="max-w-3xl">
      <SectionHeader icon={Monitor} title="Computer Use 配置" subtitle="浏览器自动化引擎参数 · 域名白名单 · 会话管理 (运行时生效)" color="#4F46E5" />
      {msg && <div className="mb-3 text-sm px-3 py-2 rounded-lg bg-gray-50 border border-gray-100 text-gray-700">{msg}</div>}
      {loading ? <div className="text-center text-gray-400 text-sm py-8">加载中…</div> : (
        <>
          {/* 域名白名单 */}
          <div className={card}>
            <div className="flex items-center gap-2 mb-2">
              <Globe size={15} className="text-indigo-500" />
              <span className={label}>域名白名单</span>
            </div>
            <p className="text-[11px] text-gray-400 mb-2 leading-relaxed">
              逗号分隔的域名列表 (如 remedy.example.com, itsm.xxx.com)。留空 = 允许所有公网域名 (内网地址始终拦截)。
              配置后 Computer Use 只能访问白名单内的域名。
            </p>
            <textarea
              value={cfg.allowed_domains || ''}
              onChange={e => setCfg({ ...cfg, allowed_domains: e.target.value })}
              rows={3} placeholder="remedy.example.com, itsm.example.com"
              className={`${input} font-mono text-xs`} />
            <div className="flex gap-2 mt-2">
              <input
                value={testUrl}
                onChange={e => setTestUrl(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && testDomain()}
                placeholder="输入 URL 测试是否允许访问…"
                className={`${input} flex-1`} />
              <button onClick={testDomain}
                className="px-4 rounded-lg text-xs font-medium bg-indigo-50 text-indigo-600 border border-indigo-200 hover:bg-indigo-100 whitespace-nowrap">
                测试
              </button>
            </div>
            {testResult && (
              <div className={`mt-2 px-3 py-2 rounded-lg text-xs ${testResult.allowed ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>
                {testResult.allowed ? '✅ 允许访问' : `❌ 被拦截: ${testResult.reason}`}
              </div>
            )}
          </div>

          {/* 站点凭据保管 (登录免问询) */}
          <div className={card}>
            <div className="flex items-center gap-2 mb-2">
              <KeyRound size={15} className="text-indigo-500" />
              <span className={label}>站点凭据保管</span>
            </div>
            <p className="text-[11px] text-gray-400 mb-2 leading-relaxed">
              每行一条: <code className="text-indigo-500">域名 用户名 密码</code> (空格分隔; 密码含空格时取第三段起的全部)。
              仅在服务端无头模式 (用户浏览器扩展未连接) 下自动登录时使用; 密码不进入对话与模型输入。
              用户扩展模式不需要此项 — 浏览器自身登录态完成认证。
            </p>
            <textarea
              value={cfg.site_credentials || ''}
              onChange={e => setCfg({ ...cfg, site_credentials: e.target.value })}
              rows={3} placeholder="remedy.example.com myuser P@ssw0rd"
              className={`${input} font-mono text-xs`} />
          </div>

          {/* 预算配置 */}
          <div className={card}>
            <div className="flex items-center gap-2 mb-3">
              <Clock size={15} className="text-indigo-500" />
              <span className={label}>执行预算</span>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-gray-500">最大步数 (1-100)</label>
                <input type="number" min={1} max={100}
                  value={cfg.max_steps ?? 40}
                  onChange={e => setCfg({ ...cfg, max_steps: parseInt(e.target.value) || 40 })}
                  className={input} />
              </div>
              <div>
                <label className="text-xs text-gray-500">单步超时 (秒, 5-120)</label>
                <input type="number" min={5} max={120}
                  value={cfg.step_timeout_s ?? 30}
                  onChange={e => setCfg({ ...cfg, step_timeout_s: parseInt(e.target.value) || 30 })}
                  className={input} />
              </div>
              <div>
                <label className="text-xs text-gray-500">总时间预算 (秒, 30-3600)</label>
                <input type="number" min={30} max={3600}
                  value={cfg.total_budget_s ?? 900}
                  onChange={e => setCfg({ ...cfg, total_budget_s: parseInt(e.target.value) || 900 })}
                  className={input} />
              </div>
              <div>
                <label className="text-xs text-gray-500">截图上限 (张)</label>
                <input type="number" min={1} max={100}
                  value={cfg.max_screenshots ?? 20}
                  onChange={e => setCfg({ ...cfg, max_screenshots: parseInt(e.target.value) || 20 })}
                  className={input} />
              </div>
            </div>
          </div>

          {/* 会话管理 */}
          <div className={card}>
            <div className="flex items-center gap-2 mb-2">
              <Users size={15} className="text-indigo-500" />
              <span className={label}>浏览器会话</span>
            </div>
            <p className="text-[11px] text-gray-400 mb-3">
              持久会话可跨任务复用登录态 (对话中传 params.browser_session="key")。
              会话超时时间: {Math.round((cfg.session_ttl_s ?? 1800) / 60)} 分钟。
            </p>
            <button onClick={clearSessions}
              className="px-4 py-2 rounded-lg text-xs font-medium border border-red-200 text-red-600 hover:bg-red-50">
              清除所有浏览器会话
            </button>
          </div>

          <button onClick={save} disabled={saving}
            className="w-full py-3 rounded-xl text-sm font-medium bg-gray-900 text-white hover:bg-gray-700 disabled:opacity-30 transition-colors">
            {saving ? '保存中…' : '保存配置'}
          </button>
        </>
      )}
    </div>
  );
}

// ═══ 插件管理 (dsh "一切皆插件": 启停写 patch 层, 副作用可逆) ═══
function PluginsPanel() {
  const [data, setData] = useState<any>({ plugins: [], events: [] });
  const [recap, setRecap] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');

  const authHeaders = () => ({
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}`,
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [pr, cr] = await Promise.all([
        fetch('/api/admin/plugins', { headers: authHeaders() }),
        fetch('/api/admin/plugins/recap', { headers: authHeaders() }),
      ]);
      if (pr.ok) setData(await pr.json());
      if (cr.ok) setRecap((await cr.json()).recap || []);
    } catch { /* 网络异常 */ }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (pid: string, action: 'enable' | 'disable' | 'reload') => {
    setBusy(pid + action); setMsg('');
    try {
      const r = await fetch(`/api/admin/plugins/${action === 'reload' ? 'reload' : pid + '/' + action}`, {
        method: 'POST',
        headers: authHeaders(),
        body: action === 'reload' ? JSON.stringify(pid ? { id: pid } : {}) : undefined,
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      setMsg(`✅ ${pid || '全部插件'} ${action === 'enable' ? '已启用' : action === 'disable' ? '已停用 (副作用已撤销)' : '已重载'}`);
      await load();
    } catch (e: any) { setMsg('❌ ' + e.message); }
    setBusy('');
    setTimeout(() => setMsg(''), 4000);
  };

  const stateBadge = (s: string) => {
    const map: any = {
      enabled: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      disabled: 'bg-gray-50 text-gray-500 border-gray-200',
      error: 'bg-red-50 text-red-600 border-red-200',
      discovered: 'bg-amber-50 text-amber-700 border-amber-200',
    };
    const label: any = { enabled: '运行中', disabled: '已停用', error: '错误', discovered: '未启用' };
    return <span className={`px-2 py-0.5 rounded-md text-[10px] font-medium border ${map[s] || map.discovered}`}>
      {label[s] || s}</span>;
  };

  const modeLabel: any = { emit: '观察', bail: '守卫', waterfall: '环绕', parallel: '并行', serial: '顺序' };

  return (
    <div className="max-w-4xl">
      <SectionHeader icon={Bot} title="插件管理" subtitle="一切皆插件 · 事件驱动 · 启停即时生效且副作用可逆 (参考 DeepSeek Harness 架构)" color="#A21CAF" />
      {msg && <div className="mb-3 text-sm px-3 py-2 rounded-lg bg-gray-50 border border-gray-100 text-gray-700">{msg}</div>}
      {loading ? <div className="text-center text-gray-400 text-sm py-8">加载中…</div> : (
        <>
          {/* 插件列表 */}
          {data.plugins.map((p: any) => (
            <div key={p.id} className="bg-white border border-gray-200 rounded-xl p-4 mb-3">
              <div className="flex items-center gap-2.5 mb-1">
                <span className="text-[14px] font-semibold text-gray-900">{p.id}</span>
                <span className="text-[11px] text-gray-400">v{p.version}</span>
                {stateBadge(p.state)}
                <div className="flex-1" />
                <button onClick={() => act(p.id, 'reload')} disabled={busy === p.id + 'reload'}
                  className="px-2.5 py-1 rounded-md text-[11px] text-gray-500 border border-gray-200 hover:bg-gray-50 disabled:opacity-40">
                  {busy === p.id + 'reload' ? '…' : '重载'}
                </button>
                {p.state === 'enabled' ? (
                  <button onClick={() => act(p.id, 'disable')} disabled={busy === p.id + 'disable'}
                    className="px-3 py-1.5 rounded-lg text-[11px] font-medium border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40">
                    {busy === p.id + 'disable' ? '…' : '停用'}
                  </button>
                ) : p.state !== 'error' ? (
                  <button onClick={() => act(p.id, 'enable')} disabled={busy === p.id + 'enable'}
                    className="px-3 py-1.5 rounded-lg text-[11px] font-medium bg-gray-900 text-white hover:bg-gray-700 disabled:opacity-40">
                    {busy === p.id + 'enable' ? '…' : '启用'}
                  </button>
                ) : null}
              </div>
              <p className="text-[12px] text-gray-500 mb-2">{p.description}</p>
              {p.error && <div className="text-[11px] text-red-500 mb-2">⚠ {p.error}</div>}
              {p.contributions?.length > 0 && (
                <div className="text-[11px] text-gray-400 space-y-0.5">
                  {p.contributions.map((c: any, i: number) => (
                    <div key={i} className="flex gap-1.5">
                      <span className="text-fuchsia-500 font-mono flex-none">{c.kind}</span>
                      <span className="font-mono text-gray-500 flex-none">{c.id}</span>
                      <span className="truncate">{c.detail}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}

          {/* 事件缝 */}
          <div className="bg-white border border-gray-200 rounded-xl p-4 mb-3">
            <span className="block text-[13px] font-medium text-gray-900 mb-2">事件缝 (扩展点)</span>
            <div className="divide-y divide-gray-50">
              {data.events.map((e: any) => (
                <div key={e.event} className="flex items-center gap-3 py-1.5 text-[12px]">
                  <span className="font-mono text-indigo-500 w-44 flex-none">{e.event}</span>
                  <span className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 text-[10px]">{modeLabel[e.mode] || e.mode}</span>
                  <span className="text-gray-400 flex-1">{e.listeners} 个监听器</span>
                </div>
              ))}
            </div>
          </div>

          {/* 最近动态 (session_recap) */}
          {recap.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-xl p-4">
              <span className="block text-[13px] font-medium text-gray-900 mb-2">会话动态 (session_recap 插件)</span>
              <div className="space-y-1 max-h-56 overflow-y-auto">
                {recap.slice(0, 15).map((r: any, i: number) => (
                  <div key={i} className="text-[11px] flex gap-2">
                    <span className="text-gray-300 font-mono flex-none">
                      {new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
                    </span>
                    <span className="text-gray-600 font-medium flex-none">{r.user}</span>
                    <span className="text-gray-400 truncate">{r.topics}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ═══ Computer Use 凭据管理 ═══
function CUCredentialsPanel() {
  const [creds, setCreds] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState('');
  const [form, setForm] = useState({ name: '', username: '', password: '', url_pattern: '' });
  const [showPw, setShowPw] = useState(false);
  const [editing, setEditing] = useState<string | null>(null); // 正在编辑的凭据名

  const authHeaders = () => ({
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}`,
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch('/api/admin/computer-use/credentials',
        { headers: authHeaders() });
      if (r.ok) { const d = await r.json(); setCreds(d.credentials || []); }
    } catch { /* 网络异常 */ }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const flash = (t: string, ok = true) => {
    setMsg(`${ok ? '✅' : '❌'} ${t}`);
    setTimeout(() => setMsg(''), 4000);
  };

  const save = async () => {
    const { name, username, password, url_pattern } = form;
    if (!name.trim() || !username.trim() || (!password.trim() && !editing)) {
      flash('名称、用户名、密码均不能为空 (编辑已有凭据时密码留空 = 保持原密码)', false);
      return;
    }
    try {
      const r = await fetch(`/api/admin/computer-use/credentials/${encodeURIComponent(name.trim())}`, {
        method: 'PUT', headers: authHeaders(),
        body: JSON.stringify({
          username: username.trim(),
          ...(password.trim() ? { password: password } : {}),
          url_pattern: url_pattern.trim(),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      flash(`凭据 '${name}' 已保存 (AES 加密存储)`);
      setForm({ name: '', username: '', password: '', url_pattern: '' });
      setEditing(null);
      load();
    } catch (e: any) { flash('保存失败: ' + e.message, false); }
  };

  const edit = (c: any) => {
    setEditing(c.name);
    setForm({ name: c.name, username: c.username, password: '', url_pattern: c.url_pattern || '' });
    setShowPw(false);
  };

  const del = async (name: string) => {
    if (!confirm(`删除凭据 '${name}'?`)) return;
    await fetch(`/api/admin/computer-use/credentials/${encodeURIComponent(name)}`,
      { method: 'DELETE', headers: authHeaders() });
    flash(`凭据 '${name}' 已删除`);
    load();
  };

  const card = 'bg-white border border-gray-200 rounded-xl p-4 mb-4';
  const label = 'block text-[13px] font-medium text-gray-900 mb-1';
  const input = 'w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50 focus:bg-white focus:ring-1 focus:ring-indigo-300 outline-none transition-all';

  return (
    <div className="max-w-3xl">
      <SectionHeader icon={KeyRound} title="站点凭据库" subtitle="服务端无头模式的回退凭据 · 加密存储 · URL 模式自动匹配 (扩展模式下不需要)" color="#4F46E5" />
      {msg && <div className="mb-3 text-sm px-3 py-2 rounded-lg bg-gray-50 border border-gray-100 text-gray-700">{msg}</div>}

      {/* 浏览器扩展引导 (推荐方式: 凭据零经过) */}
      <div className="bg-indigo-50/60 border border-indigo-100 rounded-xl p-4 mb-4">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-2">
            <ShieldCheck size={15} className="text-indigo-500" />
            <span className="text-[13px] font-medium text-indigo-900">
              推荐方式: 用户本机浏览器扩展 (凭据零经过)
            </span>
          </div>
          <a href="/api/cu/extension/download" download
             className="px-3 py-1.5 rounded-lg text-[11px] font-medium bg-indigo-600 text-white hover:bg-indigo-700 whitespace-nowrap">
            下载扩展 zip
          </a>
        </div>
        <p className="text-[11px] text-indigo-700/80 leading-relaxed">
          用户在自己 Chrome 安装「Z-OPS Browser Bridge」并登录后, Computer Use 直接操作用户本机浏览器:
          登录态与密码由浏览器自身管理 (密码管理器/Session), Z-OPS 全程不接触任何密码;
          每步操作实时显示在用户 Z-OPS 页面的浏览器小窗中。下方命名凭据库仅作为
          服务端无头模式的回退 (扩展未连接时)。
        </p>
      </div>

      {/* 已保管凭据列表 */}
      <div className={card}>
        <div className="flex items-center justify-between mb-2">
          <span className={label}>已保管凭据 ({creds.length})</span>
          <button onClick={load} className="text-gray-400 hover:text-gray-600"><RefreshCw size={13} /></button>
        </div>
        {loading ? <div className="text-gray-400 text-sm py-4 text-center">加载中…</div> : (
          creds.length === 0
            ? <div className="text-gray-400 text-xs py-4 text-center">暂无命名凭据 — AD 会话凭据之外可在此补充保管其他站点账号</div>
            : <div className="divide-y divide-gray-100">
              {creds.map((c) => (
                <div key={c.name} className="flex items-center gap-3 py-2.5">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-gray-900 truncate">{c.name}</div>
                    <div className="text-[11px] text-gray-400 truncate">
                      {c.username} · 匹配 <code className="text-indigo-500">{c.url_pattern || '所有 URL'}</code>
                      {c.updated_at ? ` · 更新于 ${new Date(c.updated_at * 1000).toLocaleString()}` : ''}
                    </div>
                  </div>
                  <button onClick={() => edit(c)}
                    className="px-2.5 py-1 rounded-md text-[11px] text-gray-500 border border-gray-200 hover:bg-gray-50">编辑</button>
                  <button onClick={() => del(c.name)}
                    className="p-1.5 rounded-md text-red-400 hover:bg-red-50 hover:text-red-600"><Trash2 size={13} /></button>
                </div>
              ))}
            </div>
        )}
      </div>

      {/* 新增/编辑表单 */}
      <div className={card}>
        <span className={label}>{editing ? `编辑凭据: ${editing}` : '添加凭据'}</span>
        <p className="text-[11px] text-gray-400 mb-3 leading-relaxed">
          密码加密存储 (密钥由服务端派生), 列表与 API 永不回显明文。URL 模式为正则 (如 <code className="text-indigo-500">remedy\\.</code>),
          自动登录按 <span className="font-medium">AD 会话凭据 → 命名凭据</span> 的优先级取用。
        </p>
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={form.name}
              onChange={e => setForm({ ...form, name: e.target.value })}
              disabled={!!editing}
              placeholder="remedy"
              className={`${input} disabled:bg-gray-100 disabled:text-gray-400`} />
          </div>
          <div>
            <label className="text-xs text-gray-500">用户名</label>
            <input value={form.username}
              onChange={e => setForm({ ...form, username: e.target.value })}
              placeholder="svc-gcoc"
              className={input} />
          </div>
          <div className="relative">
            <label className="text-xs text-gray-500">密码 {editing && <span className="text-gray-300">(留空 = 保持原密码)</span>}</label>
            <input type={showPw ? 'text' : 'password'} value={form.password}
              onChange={e => setForm({ ...form, password: e.target.value })}
              placeholder="••••••••"
              className={`${input} pr-8`} />
            <button onClick={() => setShowPw(!showPw)}
              className="absolute right-2 top-[26px] text-gray-300 hover:text-gray-500">
              {showPw ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <div>
            <label className="text-xs text-gray-500">URL 模式 (正则, 可空)</label>
            <input value={form.url_pattern}
              onChange={e => setForm({ ...form, url_pattern: e.target.value })}
              placeholder="remedy\\.example"
              className={`${input} font-mono text-xs`} />
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={save}
            className="px-4 py-2 rounded-lg text-xs font-medium bg-gray-900 text-white hover:bg-gray-700">
            {editing ? '更新凭据' : '保存凭据'}
          </button>
          {editing && (
            <button onClick={() => { setEditing(null); setForm({ name: '', username: '', password: '', url_pattern: '' }); }}
              className="px-4 py-2 rounded-lg text-xs font-medium border border-gray-200 text-gray-500 hover:bg-gray-50">
              取消编辑
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══ 记忆后台管理 (admin: 全量用户记忆可见可管 — 完整记忆不只用户自己看) ═══
function MemoriesAdminPanel() {
  const [q, setQ] = useState('');
  const [user, setUser] = useState('');
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch(`/api/admin/memories?q=${encodeURIComponent(q)}&username=${encodeURIComponent(user)}&limit=200`,
        { headers: { 'Authorization': `Bearer ${token || ''}` } });
      const d = await r.json();
      setItems(d.memories || []);
    } catch (e: any) { setMsg('加载失败: ' + e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const del = async (sid: string, uname: string) => {
    if (!confirm(`删除 ${uname} 的这条记忆?`)) return;
    await fetch(`/api/admin/memories/item?session_id=${encodeURIComponent(sid)}&username=${encodeURIComponent(uname)}`,
      { method: 'DELETE', headers: { 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` } });
    load();
  };
  const clearUser = async () => {
    if (!user.trim() || !confirm(`清除用户 ${user} 的全部记忆? 不可恢复`)) return;
    const r = await fetch(`/api/admin/memories/user?username=${encodeURIComponent(user.trim())}`,
      { method: 'DELETE', headers: { 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` } });
    const d = await r.json();
    setMsg(`已清除 ${d.deleted || 0} 条`);
    setTimeout(() => setMsg(''), 3000);
    load();
  };

  return (
    <div className="max-w-5xl">
      <SectionHeader icon={Brain} title="记忆管理" subtitle="全部用户的跨会话记忆 · 完整内容 · 可删可清 (后台审计)" color="#7C3AED" />
      {msg && <div className="mb-3 text-sm text-gray-700 bg-gray-50 border rounded-lg px-3 py-2">{msg}</div>}
      <div className="flex gap-2 mb-4">
        <input value={user} onChange={e => setUser(e.target.value)} placeholder="按用户名过滤 (如 svc-gcoc)"
          className="flex-1 px-3 py-2 rounded-lg border text-sm" style={{ borderColor: '#E5E7EB' }} />
        <input value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === 'Enter' && load()}
          placeholder="关键词…" className="flex-1 px-3 py-2 rounded-lg border text-sm" style={{ borderColor: '#E5E7EB' }} />
        <button onClick={load} className="px-4 rounded-lg text-sm font-medium text-white" style={{ background: '#7C3AED' }}>搜索</button>
        <button onClick={clearUser} className="px-4 rounded-lg text-sm font-medium border hover:bg-red-50" style={{ borderColor: 'rgba(239,68,68,0.3)', color: '#DC2626' }}>清除该用户</button>
      </div>
      {loading ? <div className="text-center text-gray-400 text-sm py-8">加载中…</div> : items.length === 0 ? (
        <div className="text-center text-gray-400 text-sm py-8 bg-white rounded-xl border">暂无记忆</div>
      ) : (
        <div className="space-y-2">
          {items.map((m, i) => (
            <div key={m.session_id + i} className="group flex items-start gap-3 p-3.5 bg-white rounded-xl border">
              <div className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold text-white shrink-0"
                   style={{ background: 'linear-gradient(135deg,#7C3AED,#4F46E5)' }}>
                {(m.username || '?').slice(0, 1).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 text-[11px] text-gray-400 mb-0.5">
                  <span className="font-medium text-gray-600">{m.username}</span>
                  <span>{m.created_at?.slice(0, 16).replace('T', ' ')}</span>
                </div>
                <div className="text-xs leading-relaxed break-words text-gray-800">{m.summary}</div>
                {m.topics?.length > 0 && (
                  <div className="flex gap-1.5 mt-1.5 flex-wrap">
                    {m.topics.slice(0, 6).map((t: string, j: number) => (
                      <span key={j} className="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-600">{t}</span>
                    ))}
                  </div>
                )}
              </div>
              <button onClick={() => del(m.session_id, m.username)}
                className="opacity-0 group-hover:opacity-100 p-1.5 rounded-lg hover:bg-red-50 shrink-0">
                <Trash2 size={13} color="#DC2626" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══ P2 (task-c17): 技能生命周期管理 (candidate→evaluated→approved→published→retired) ═══
function SkillLifecyclePanel() {
  const [skills, setSkills] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch('/api/skills/lifecycle', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      const d = await r.json();
      setSkills(d.skills || []);
    } catch (e: any) { setMsg('加载失败: ' + e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const act = async (name: string, action: string, version?: string) => {
    if (action === 'retire' && !confirm(`停用技能 ${name}? 线上注入将移除 (历史保留)`)) return;
    setMsg('');
    try {
      const token = localStorage.getItem('zops_token');
      const r = await fetch(`/api/skills/lifecycle/${name}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify(version ? { version } : {}),
      });
      const d = await r.json();
      setMsg(r.ok ? `✅ ${name} ${action} ${version || ''} 成功` : `❌ ${d.detail || r.status}`);
      load();
    } catch (e: any) { setMsg('❌ ' + e.message); }
  };

  const stateBadge = (s: string) => {
    const map: Record<string, string> = {
      candidate: 'bg-amber-50 text-amber-700 border-amber-200',
      evaluated: 'bg-blue-50 text-blue-700 border-blue-200',
      approved: 'bg-violet-50 text-violet-700 border-violet-200',
      published: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      retired: 'bg-gray-100 text-gray-500 border-gray-200',
    };
    return <span className={`px-2 py-0.5 rounded-full text-[11px] border ${map[s] || map.retired}`}>{s}</span>;
  };

  return (
    <div className="max-w-5xl">
      <SectionHeader icon={Bot} title="技能生命周期" subtitle="candidate → evaluated → approved → published · 复盘只产候选, 发布需管理员" color="#E11D48" />
      {msg && <div className="mb-3 text-sm text-gray-700 bg-gray-50 border rounded-lg px-3 py-2">{msg}</div>}
      {loading ? <div className="text-gray-400 text-sm py-8 text-center">加载中...</div> : skills.length === 0 ? (
        <div className="text-gray-400 text-sm py-8 text-center bg-white rounded-xl border">暂无技能 (复盘产生候选后在此审核发布)</div>
      ) : (
        <div className="space-y-3">
          {skills.map((s: any) => (
            <div key={s.name} className="bg-white rounded-xl border p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-gray-800 text-sm">{s.name}</span>
                  {stateBadge('published')}
                  <span className="text-xs text-gray-400">发布指针: {s.published || '-'}</span>
                  <span className="text-xs text-gray-400" title="使用次数 (verified 证据数)">使用: {s.usage ?? 0} 次{typeof s.verified === 'number' && s.verified > 0 ? ` (核验 ${s.verified})` : ''}</span>
                  <span className="text-xs text-gray-400">成功率: {s.success_rate === null || s.success_rate === undefined ? '未验证(null)' : `${Math.round(s.success_rate * 100)}%`}</span>
                </div>
                <div className="flex gap-2">
                  {s.published && <button onClick={() => act(s.name, 'retire')} className="px-2.5 py-1 rounded-lg text-xs bg-gray-100 hover:bg-gray-200 text-gray-600">停用</button>}
                </div>
              </div>
              <div className="space-y-1.5">
                {Object.entries(s.versions || {}).map(([ver, v]: any) => (
                  <div key={ver} className="flex items-center gap-2 text-xs bg-gray-50 rounded-lg px-3 py-1.5">
                    <span className="font-mono text-gray-600 w-20">{ver}</span>
                    {stateBadge(v.state)}
                    <span className="text-gray-400 truncate flex-1">hash {v.hash}</span>
                    {v.state === 'evaluated' && <button onClick={() => act(s.name, 'approve', ver)} className="px-2 py-0.5 rounded bg-violet-50 hover:bg-violet-100 text-violet-700 border border-violet-200">批准</button>}
                    {v.state === 'approved' && <button onClick={() => act(s.name, 'publish', ver)} className="px-2 py-0.5 rounded bg-emerald-50 hover:bg-emerald-100 text-emerald-700 border border-emerald-200">发布</button>}
                    {(v.state === 'approved' || v.state === 'retired') && s.published !== ver && (
                      <button onClick={() => act(s.name, 'rollback', ver)} className="px-2 py-0.5 rounded bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200">回退到此版</button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══ 通用 Section Header 组件 ═══
function SectionHeader({ icon: Icon, title, subtitle, color = '#2563EB' }: { icon: any; title: string; subtitle?: string; color?: string }) {
  return (
    <div className="flex items-center gap-3 mb-5">
      <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ backgroundColor: color + '15' }}>
        <Icon size={16} style={{ color }} />
      </div>
      <div>
        <h3 className="text-[#111827] font-semibold text-lg">{title}</h3>
        {subtitle && <p className="text-xs text-[#9CA3AF]">{subtitle}</p>}
      </div>
    </div>
  );
}

function AdminContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [activeTab, setActiveTab] = useState(() => {
    const t = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '').get('tab');
    return t || 'overview';
  });
  const switchTab = (id: string) => {
    setActiveTab(id);
    router.replace(`/admin?tab=${id}`, { scroll: false });
  };
  const [overview, setOverview] = useState<Overview | null>(null);
  const [permGroup, setPermGroup] = useState('');

  // 支持 /admin?tab=xxx 直达 (如通知配置 /admin?tab=notify)
  useEffect(() => {
    const tab = new URLSearchParams(window.location.search).get('tab');
    if (tab && adminTabs.some(t => t.id === tab)) setActiveTab(tab);
  }, []);
  useEffect(() => { fetchOverview(); }, []);

  const fetchOverview = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const response = await fetch('/api/overview', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (response.ok) { const data = await response.json(); setOverview(data); }
    } catch (error) { console.error('Failed to fetch overview:', error); }
  };

  return (
    <div className="h-full flex flex-col bg-gray-50">
      <PageHeader icon={Settings} title="系统设置" subtitle="系统配置与用户管理" />
      <div className="bg-white border-b border-[#E5E7EB]/80 px-4 pt-3 flex gap-1 overflow-x-auto">
        {adminTabs.map(t => {
          const Icon = t.icon;
          const active = activeTab === t.id;
          return (
            <button key={t.id} onClick={() => switchTab(t.id)}
              className={`flex items-center gap-2 px-3.5 pb-2.5 pt-1.5 text-sm font-medium whitespace-nowrap border-b-2 transition-all ${
                active
                  ? 'border-[#2563EB] text-[#2563EB]'
                  : 'border-transparent text-[#4B5563] hover:text-[#111827]'
              }`}>
              <Icon size={15} />
              {t.label}
            </button>
          );
        })}
      </div>
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'overview' && <OverviewDashboard onJumpToGroup={(g) => { setPermGroup(g); setActiveTab('perm'); }} />}
        {activeTab === 'perm' && <PermissionCenter initialGroup={permGroup} />}
        {activeTab === 'oneapi_keys' && <APIKeysManagement />}
        {activeTab === 'audit' && <AuditLogs />}
        {activeTab === 'llm' && <LLMConfig />}
        {activeTab === 'agent_models' && <AgentModels />}
        {activeTab === 'notify' && <NotifyConfig />}
        {activeTab === 'sanitize' && <SanitizeConfig />}
        {activeTab === 'ldap' && <LDAPConfig />}
        {activeTab === 'system' && <SystemInfo overview={overview} />}
        {activeTab === 'skills' && <SkillLifecyclePanel />}
        {activeTab === 'memories' && <MemoriesAdminPanel />}
        {activeTab === 'plugins' && <PluginsPanel />}
        {activeTab === 'computer_use' && (
          <>
            <ComputerUseConfig />
            <CUCredentialsPanel />
          </>
        )}
      </div>
    </div>
  );
}

// ═══ 总览仪表盘 ═══
function OverviewDashboard({ onJumpToGroup }: { onJumpToGroup: (g: string) => void }) {
  const [overview, setOverview] = useState<any>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [perms, setPerms] = useState<Record<string, GroupPerm>>({});
  const [allGroups, setAllGroups] = useState<string[]>(USER_GROUPS);

  useEffect(() => {
    const token = localStorage.getItem('zops_token');
    const headers = { 'Authorization': `Bearer ${token || ''}` };
    Promise.all([
      fetch('/api/overview', { headers }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/auth/users', { headers }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/admin/group-permissions', { headers }).then(r => r.ok ? r.json() : null).catch(() => null),
    ]).then(([ov, us, gp]) => {
      setOverview(ov);
      setUsers(us?.users || []);
      setPerms(gp?.groups || {});
      if (gp?.available_groups?.length) setAllGroups(gp.available_groups);
    });
  }, []);

  const adminCount = users.filter(u => u.role === 'admin').length;
  const adCount = users.filter(u => u.auth_source === 'ldap').length;
  const agents: Record<string, any> = overview?.agents || {};
  const agentNames = Object.keys(agents);
  const onlineAgents = agentNames.filter(n => !['timeout', 'unknown'].includes(agents[n]?.status));
  const configuredGroups = allGroups.filter(g => { const p = perms[g]; return p && (p.tabs.length > 0 || p.nms_groups.length > 0 || p.biz_perms.length > 0); });
  const memberCount = (g: string) => users.filter(u => u.group === g).length;

  const stats = [
    { label: '用户总数', value: users.length, sub: `${adminCount} 管理员 · ${adCount} AD域`, icon: Users, color: '#2563EB' },
    { label: '用户组', value: allGroups.length, sub: `${configuredGroups.length} 组已配置`, icon: ShieldCheck, color: '#0D9488' },
    { label: 'Agents', value: agentNames.length, sub: `${onlineAgents.length} 在线`, icon: Bot, color: '#7C3AED' },
    { label: 'LLM 调用', value: overview?.llm_usage_count || 0, sub: `${overview?.llm_provider || '-'} · ${overview?.llm_real_model || overview?.llm_model || '-'}`, icon: Brain, color: '#EA580C' },
  ];

  return (
    <div className="max-w-full">
      <SectionHeader icon={LayoutDashboard} title="总览" subtitle="系统运行状态与权限概况" color="#2563EB" />
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-4 mb-5">
        {stats.map(s => {
          const Icon = s.icon;
          return (
            <div key={s.label} className="bg-white border border-[#E5E7EB] rounded-xl p-4 flex items-center gap-3.5">
              <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0" style={{ backgroundColor: s.color + '15' }}>
                <Icon size={18} style={{ color: s.color }} />
              </div>
              <div className="min-w-0">
                <div className="text-2xl font-bold text-[#111827] leading-tight">{s.value}</div>
                <div className="text-xs font-medium text-[#6B7280]">{s.label}</div>
                <div className="text-[11px] text-[#9CA3AF] truncate">{s.sub}</div>
              </div>
            </div>
          );
        })}
      </div>
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 mb-5">
        <div className="flex items-center justify-between mb-2.5">
          <h4 className="text-sm font-semibold text-[#111827]">用户组权限概况</h4>
          <span className="text-xs text-[#9CA3AF]">点击进入组权限配置</span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-1.5">
          {allGroups.map(g => {
            const p = perms[g] || { tabs: [], nms_groups: [], biz_perms: [] };
            const configured = p.tabs.length > 0 || p.nms_groups.length > 0 || p.biz_perms.length > 0;
            return (
              <button key={g} onClick={() => onJumpToGroup(g)}
                className="flex items-center gap-2 border border-[#E5E7EB] rounded-lg px-3 py-1.5 hover:border-teal-300 hover:bg-teal-50/40 transition-colors text-left">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${configured ? 'bg-teal-500' : 'bg-[#D1D5DB]'}`} title={configured ? '已配置' : '默认'} />
                <span className="text-xs font-medium text-[#111827] truncate">{g}</span>
                <span className="ml-auto text-[11px] text-[#9CA3AF] shrink-0">{memberCount(g)} 人</span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
          <h4 className="text-sm font-semibold text-[#111827] mb-3 flex items-center gap-2"><Activity size={15} className="text-[#16A34A]" /> Agents 状态</h4>
          <div className="space-y-2">
            {agentNames.map(n => {
              const st = agents[n]?.status;
              const online = !['timeout', 'unknown'].includes(st);
              return (
                <div key={n} className="flex items-center gap-2.5 text-sm">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${online ? 'bg-[#16A34A]' : 'bg-[#D1D5DB]'}`} />
                  <span className="text-[#374151]">{n}</span>
                  <span className={`ml-auto text-[11px] ${online ? 'text-[#16A34A]' : 'text-[#9CA3AF]'}`}>{online ? '在线' : (st === 'timeout' ? '超时' : '未知')}</span>
                </div>
              );
            })}
            {agentNames.length === 0 && <div className="text-xs text-[#9CA3AF] py-2">暂无 Agent 心跳数据</div>}
          </div>
        </div>
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
          <h4 className="text-sm font-semibold text-[#111827] mb-3 flex items-center gap-2"><Info size={15} className="text-[#2563EB]" /> 系统信息</h4>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-[#6B7280]">应用</span><span className="font-medium text-[#111827]">{overview?.app || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">LLM 提供商</span><span className="font-medium text-[#111827]">{overview?.llm_provider || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">当前模型</span><span className="font-mono text-xs text-[#111827]">{overview?.llm_real_model || overview?.llm_model || '-'}</span></div>
            {overview?.llm_model_alias && overview?.llm_model && (
              <div className="flex justify-between"><span className="text-[#6B7280]">调用别名</span><span className="font-mono text-xs text-[#9CA3AF]">{overview.llm_model}</span></div>
            )}
            <div className="flex justify-between"><span className="text-[#6B7280]">描述</span><span className="text-xs text-[#4B5563] truncate max-w-[220px]">{overview?.description || '-'}</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══ 权限管理中心：统一管理用户与组 (增删改查) ═══
function PermissionCenter({ initialGroup }: { initialGroup?: string }) {
  const [view, setView] = useState<'users' | 'groups'>(initialGroup ? 'groups' : 'users');
  const [groupInfo, setGroupInfo] = useState<{ groups: string[]; builtin: string[]; custom: string[]; member_counts: Record<string, number> } | null>(null);

  const fetchGroups = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/admin/groups', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) setGroupInfo(await res.json());
    } catch (e) { console.error(e); }
  };
  useEffect(() => { fetchGroups(); }, []);

  const groups = groupInfo?.groups || USER_GROUPS;
  const custom = groupInfo?.custom || [];

  return (
    <div className="max-w-full">
      <SectionHeader icon={ShieldCheck} title="权限管理" subtitle="统一管理用户、用户组与组权限" color="#0D9488" />
      <div className="flex items-center gap-1 mb-5 bg-gray-100 rounded-lg p-1 w-fit">
        <button onClick={() => setView('users')}
          className={`flex items-center gap-1.5 px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
            view === 'users' ? 'bg-white text-[#111827] shadow-sm' : 'text-[#6B7280] hover:text-[#111827]'}`}>
          <Users size={14} /> 用户管理
        </button>
        <button onClick={() => setView('groups')}
          className={`flex items-center gap-1.5 px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
            view === 'groups' ? 'bg-white text-[#111827] shadow-sm' : 'text-[#6B7280] hover:text-[#111827]'}`}>
          <ShieldCheck size={14} /> 组与权限
        </button>
      </div>
      {view === 'users'
        ? <UsersManagement groups={groups} />
        : <GroupPermissions initialGroup={initialGroup} groups={groups} customGroups={custom} onGroupsChanged={fetchGroups} />}
    </div>
  );
}

// 用户管理
function UsersManagement({ groups = USER_GROUPS }: { groups?: string[] }) {
  const [users, setUsers] = useState<User[]>([]);
  const [newUser, setNewUser] = useState({ username: '', password: '', display_name: '', role: 'user', group: '' });
  const [adUser, setAdUser] = useState({ username: '', display_name: '', role: 'user', group: '' });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkGroup, setBulkGroup] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [groupFilter, setGroupFilter] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  // 业务权限点目录 (后端 BIZ_PERMS 下发; 新增业务域自动出现, 无需改前端)
  const [bizCatalog, setBizCatalog] = useState<{ key: string; label: string; desc: string; group: string }[]>([]);
  const [bizRoles, setBizRoles] = useState<Record<string, string[]>>({});  // 业务角色模板 (一键套用)
  const [permUser, setPermUser] = useState<string | null>(null);      // 权限编辑弹层目标用户
  const [permDraft, setPermDraft] = useState<Set<string>>(new Set()); // 弹层草稿
  const [scopeDraft, setScopeDraft] = useState<BizScope>({ sites: [], customers: [] }); // 范围约束草稿 (L4)
  const { siteOptions, custOptions, loading: scopeLoading } = useScopeOptions();  // GCOC 站点/客户字典
  const [ttlDraft, setTtlDraft] = useState<Record<string, number>>({}); // 临时授权草稿 {perm: 天数; 0/缺省=永久}
  const [permMetrics, setPermMetrics] = useState<{ key: string; label: string; group: string; holders: number; used: number; denied: number; advice: string }[]>([]);
  const [metricsOpen, setMetricsOpen] = useState(false);
  const loadPermMetrics = () => {
    const token = localStorage.getItem('zops_token');
    fetch('/api/admin/perm-metrics', { headers: { 'Authorization': `Bearer ${token || ''}` } })
      .then(r => r.ok ? r.json() : { metrics: [] })
      .then(d => setPermMetrics(d.metrics || []))
      .catch(() => {});
  };
  const me = useMemo(() => getMyUsername(), []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(t);
  }, [toast]);

  const fetchUsers = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const response = await fetch('/api/auth/users', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (response.ok) { const data = await response.json(); setUsers(data.users || []); }
    } catch (error) { console.error('Failed to fetch users:', error); }
  };
  useEffect(() => { fetchUsers(); }, []);

  // 拉取业务权限点目录 (登录可读; 目录随 BIZ_PERMS 注册自动扩展)
  const fetchBizCatalog = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/auth/biz-perms', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) { const d = await res.json(); setBizCatalog(d.perms || []); setBizRoles(d.roles || {}); }
    } catch (error) { console.error('Failed to fetch biz-perm catalog:', error); }
  };
  useEffect(() => { fetchBizCatalog(); }, []);

  // 目录按业务分组聚合 (保持后端注册顺序)
  const bizGroups = useMemo(() => {
    const m = new Map<string, { key: string; label: string; desc: string; group: string }[]>();
    for (const p of bizCatalog) {
      if (!m.has(p.group)) m.set(p.group, []);
      m.get(p.group)!.push(p);
    }
    return Array.from(m.entries());
  }, [bizCatalog]);

  const filteredUsers = useMemo(() => users.filter(u => {
    const q = search.trim().toLowerCase();
    if (q && !(u.username.toLowerCase().includes(q) || (u.display_name || '').toLowerCase().includes(q))) return false;
    if (groupFilter === 'none' && u.group) return false;
    if (groupFilter && groupFilter !== 'none' && u.group !== groupFilter) return false;
    if (roleFilter && u.role !== roleFilter) return false;
    return true;
  }), [users, search, groupFilter, roleFilter]);

  // 统一提取后端错误详情
  const errDetail = async (res: Response, fallback: string) => {
    try { const d = await res.json(); if (d.detail) return typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
    return `${fallback} (HTTP ${res.status})`;
  };

  const addUser = async () => {
    if (!newUser.username.trim() || !newUser.password.trim()) return;
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify(newUser) });
      if (res.ok) {
        setToast({ ok: true, text: `用户 ${newUser.username} 已创建` });
        setNewUser({ username: '', password: '', display_name: '', role: 'user', group: '' }); fetchUsers();
      } else {
        setToast({ ok: false, text: await errDetail(res, '创建失败') });
      }
    } catch (error) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
  };

  const deleteUser = async (username: string) => {
    if (!window.confirm(`确认删除用户 ${username}？此操作不可恢复`)) return;
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch(`/api/auth/users/${username}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) setToast({ ok: true, text: `用户 ${username} 已删除` });
      else setToast({ ok: false, text: await errDetail(res, '删除失败') });
      fetchUsers();
    } catch (error) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
  };

  const updateUserField = async (username: string, patch: Record<string, any>, okText: string) => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch(`/api/auth/users/${username}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify(patch) });
      if (res.ok) setToast({ ok: true, text: okText });
      else setToast({ ok: false, text: await errDetail(res, '更新失败') });
      fetchUsers();
    } catch (error) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
  };

  const updateGroup = (username: string, group: string) =>
    updateUserField(username, { group }, group ? `${username} 已设为 ${group} 组` : `已清除 ${username} 的组别`);

  const updateRole = (username: string, role: string) =>
    updateUserField(username, { role }, `${username} 已设为${role === 'admin' ? '管理员' : '普通用户'}`);

  // 业务权限编辑 (与系统角色分离; admin 默认仅审计查看, 业务操作权单独授予)
  const openPermModal = (username: string) => {
    const u = users.find(x => x.username === username);
    setPermDraft(new Set((u?.biz_perms as string[]) || []));
    setScopeDraft({ sites: [...(u?.scopes?.sites || [])], customers: [...(u?.scopes?.customers || [])] });
    const ttlInit: Record<string, number> = {};
    Object.entries(u?.biz_perms_ttl || {}).forEach(([pk, exp]) => {
      const days = Math.max(1, Math.ceil(((exp as number) * 1000 - Date.now()) / 86400000));
      if (days > 0) ttlInit[pk] = days;
    });
    setTtlDraft(ttlInit);
    setPermUser(username);
  };
  const togglePermDraft = (key: string) => {
    setPermDraft(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };
  const saveBizPerms = () => {
    if (!permUser) return;
    const labels = bizCatalog.filter(p => permDraft.has(p.key)).map(p => p.label);
    const ttlPayload: Record<string, number> = {};
    permDraft.forEach(pk => {
      const days = ttlDraft[pk] || 0;
      if (days > 0) ttlPayload[pk] = Math.floor(Date.now() / 1000) + days * 86400;
    });
    updateUserField(permUser, { biz_perms: Array.from(permDraft), scopes: scopeDraft, biz_perms_ttl: ttlPayload },
      `${permUser} 业务权限已更新: ${labels.length ? labels.join('、') : '无'}${(scopeDraft.sites.length || scopeDraft.customers.length) ? `; 范围: ${scopeDraft.sites.join('、') || '站点不限'} / ${scopeDraft.customers.join('、') || '客户不限'}` : ''}`);
    setPermUser(null);
  };

  const resetPassword = async (username: string) => {
    const pw = window.prompt(`为 ${username} 设置新密码（至少4位）:`);
    if (pw === null) return;
    if (!pw || pw.length < 4) { setToast({ ok: false, text: '密码至少4位，未重置' }); return; }
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch(`/api/auth/users/${username}/reset-password`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ password: pw })
      });
      if (res.ok) setToast({ ok: true, text: `${username} 的密码已重置` });
      else setToast({ ok: false, text: await errDetail(res, '重置失败') });
    } catch (error) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
  };

  const toggleSelect = (uname: string) => {
    setSelected(prev => { const next = new Set(prev); if (next.has(uname)) next.delete(uname); else next.add(uname); return next; });
  };

  const bulkSetGroup = async () => {
    if (selected.size === 0) return;
    setBulkBusy(true);
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/admin/group-members', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ group: bulkGroup, usernames: Array.from(selected), action: 'add' })
      });
      if (res.ok) {
        const d = await res.json();
        setToast({ ok: true, text: bulkGroup ? `已将 ${d.updated.length} 名用户设置为 ${bulkGroup} 组` : `已清除 ${d.updated.length} 名用户的组别` });
        setSelected(new Set());
        fetchUsers();
      } else {
        let detail = `批量设置失败 (HTTP ${res.status})`;
        try { const d = await res.json(); if (d.detail) detail = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
        setToast({ ok: false, text: detail });
      }
    } catch (e) {
      setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' });
    }
    setBulkBusy(false);
  };

  return (
    <div className="max-w-full relative">
      {toast && (
        <div className={`fixed top-6 right-6 z-50 flex items-center gap-2.5 px-4 py-3 rounded-lg shadow-lg border text-sm font-medium transition-all duration-300 ${
          toast.ok ? 'bg-blue-600 text-white border-blue-500' : 'bg-red-600 text-white border-red-500'}`}>
          {toast.ok ? <CheckCircle size={16} /> : <XCircle size={16} />}
          <span>{toast.text}</span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 min-w-[200px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
          <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索用户名 / 显示名..."
            className="w-full pl-9 pr-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent" />
        </div>
        <select value={groupFilter} onChange={e => setGroupFilter(e.target.value)} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#2563EB]">
          <option value="">全部组别</option>
          {groups.map(g => <option key={g} value={g}>{g}</option>)}
          <option value="none">无组别</option>
        </select>
        <select value={roleFilter} onChange={e => setRoleFilter(e.target.value)} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#2563EB]">
          <option value="">全部角色</option>
          <option value="admin">管理员</option>
          <option value="user">用户</option>
        </select>
      </div>
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden mb-6 ">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] text-sm font-semibold text-[#374151] flex items-center gap-2">
          <input type="checkbox" checked={filteredUsers.length > 0 && selected.size === filteredUsers.length} onChange={() => setSelected(prev => (prev.size === filteredUsers.length && filteredUsers.length > 0) ? new Set() : new Set(filteredUsers.map(u => u.username)))}
            className="w-3.5 h-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500" title="全选" />
          <Users size={14} className="text-[#9CA3AF]" /> 用户列表
          <span className="ml-auto text-xs text-[#9CA3AF] font-normal">{(search || groupFilter || roleFilter) ? `筛选出 ${filteredUsers.length} / 共 ${users.length}` : `${users.length} 人`}{selected.size > 0 && ` · 已选 ${selected.size}`}</span>
        </div>
        {selected.size > 0 && (
          <div className="px-4 py-2.5 border-b border-blue-100 bg-blue-50/60 flex items-center gap-2">
            <span className="text-xs font-medium text-[#2563EB]">已选 {selected.size} 名用户</span>
            <select value={bulkGroup} onChange={e => setBulkGroup(e.target.value)}
              className="text-xs border border-[#D1D5DB] rounded-lg px-2 py-1 bg-white text-[#374151] focus:ring-1 focus:ring-[#2563EB]">
              <option value="">设为无组别</option>
              {groups.map(g => <option key={g} value={g}>{g}</option>)}
            </select>
            <button onClick={bulkSetGroup} disabled={bulkBusy}
              className="bg-[#2563EB] hover:bg-[#1D4ED8] disabled:bg-[#E5E7EB] text-white text-xs px-3 py-1.5 rounded-lg font-medium transition-colors">
              {bulkBusy ? '设置中...' : '批量设置组别'}
            </button>
            <button onClick={() => setSelected(new Set())} className="text-xs text-[#6B7280] hover:text-[#111827] px-2 py-1.5 rounded-lg transition-colors">取消选择</button>
          </div>
        )}
        <div className="divide-y divide-[#F3F4F6]">
          {filteredUsers.map(u => (
            <div key={u.username} className="px-4 py-3.5 flex items-center justify-between hover:bg-[#F9FAFB] transition-colors">
              <div className="flex items-center gap-3">
                <input type="checkbox" checked={selected.has(u.username)} onChange={() => toggleSelect(u.username)}
                  className="w-3.5 h-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                <div className="w-9 h-9 rounded-full bg-[#F3F4F6] flex items-center justify-center text-sm font-bold text-[#4B5563]">
                  {getInitials(u.display_name || u.username)}
                </div>
                <div>
                  <div className="font-semibold text-[#111827] text-sm flex items-center gap-1.5">
                    {u.display_name || u.username}
                    {u.username === me && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-blue-50 text-[#2563EB] border border-blue-100 font-medium">我</span>}
                  </div>
                  <div className="text-xs text-[#6B7280] mt-0.5">@{u.username} · {u.auth_source === 'ldap' ? 'AD域' : '本地'} · 最近登录 {formatLastLogin(u.last_login)}</div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <select value={u.group || ''} onChange={e => updateGroup(u.username, e.target.value)}
                  className="text-xs border border-[#D1D5DB] rounded-lg px-2 py-1 bg-white text-[#374151] focus:ring-1 focus:ring-[#2563EB]" title="组别">
                  <option value="">无组别</option>
                  {groups.map(g => <option key={g} value={g}>{g}</option>)}
                </select>
                <select value={u.role} onChange={e => updateRole(u.username, e.target.value)} disabled={u.username === me}
                  className={`text-xs border rounded-lg px-2 py-1 focus:ring-1 focus:ring-[#2563EB] ${u.role === 'admin' ? 'border-blue-200 bg-blue-50 text-[#2563EB] font-medium' : 'border-[#D1D5DB] bg-white text-[#374151]'} disabled:opacity-60 disabled:cursor-not-allowed`}
                  title={u.username === me ? '不能修改自己的角色' : '角色'}>
                  <option value="user">用户</option>
                  <option value="admin">管理员</option>
                </select>
                <button onClick={() => openPermModal(u.username)}
                  className={`text-xs border rounded-lg px-2 py-1 transition-colors ${((u.biz_perms || []).length)
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700 font-medium hover:bg-emerald-100'
                    : 'border-[#D1D5DB] bg-white text-[#9CA3AF] hover:text-[#374151]'}`}
                  title="业务权限 (与系统角色分离): 授予后可执行对应业务操作; 系统管理员默认仅审计查看">
                  业务权限{((u.biz_perms || []).length) ? ` ${u.biz_perms!.length}项` : ''}
                </button>
                {u.auth_source !== 'ldap' && (
                  <button onClick={() => resetPassword(u.username)}
                    className="text-[#9CA3AF] hover:text-[#2563EB] hover:bg-blue-50 text-xs px-2.5 py-1.5 rounded-lg transition-colors border border-transparent hover:border-blue-100" title="重置密码">重置密码</button>
                )}
                <button onClick={() => deleteUser(u.username)} disabled={u.username === me}
                  className="text-[#9CA3AF] hover:text-[#DC2626] hover:bg-[#FEF2F2] text-xs px-2.5 py-1.5 rounded-lg transition-colors border border-transparent hover:border-[#FECACA] disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-[#9CA3AF] disabled:hover:bg-transparent"
                  title={u.username === me ? '不能删除自己' : '删除用户'}>删除</button>
              </div>
            </div>
          ))}
          {filteredUsers.length === 0 && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">{users.length === 0 ? '暂无用户' : '无匹配用户'}</div>}
        </div>
      </div>
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5  mb-4">
        <h4 className="text-sm font-semibold text-[#111827] mb-3 flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-[#EFF6FF] flex items-center justify-center"><Users size={13} className="text-[#2563EB]" /></div>
          添加本地用户
        </h4>
        <div className="grid grid-cols-2 gap-2 mb-3">
          <input type="text" placeholder="用户名" value={newUser.username} onChange={e => setNewUser(p => ({ ...p, username: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent" />
          <input type="password" placeholder="密码" value={newUser.password} onChange={e => setNewUser(p => ({ ...p, password: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent" />
          <input type="text" placeholder="显示名 (可选)" value={newUser.display_name} onChange={e => setNewUser(p => ({ ...p, display_name: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent" />
          <select value={newUser.role} onChange={e => setNewUser(p => ({ ...p, role: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent">
            <option value="user">普通用户</option><option value="admin">管理员</option>
          </select>
          <select value={newUser.group} onChange={e => setNewUser(p => ({ ...p, group: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB] focus:border-transparent">
            <option value="">无组别</option>
            {groups.map(g => <option key={g} value={g}>{g}</option>)}
          </select>
        </div>
        <button onClick={addUser} disabled={!newUser.username.trim() || !newUser.password.trim()}
          className="bg-[#2563EB] hover:bg-[#1D4ED8] disabled:bg-[#E5E7EB] disabled:text-[#9CA3AF] text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors">添加用户</button>
      </div>
      <div className="bg-white border border-green-200 rounded-lg p-5 ">
        <h4 className="text-sm font-semibold text-[#111827] mb-1 flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-[#F0FDF4] flex items-center justify-center"><ShieldCheck size={13} className="text-[#16A34A]" /></div>
          授权 AD 域用户
        </h4>
        <p className="text-xs text-[#6B7280] mb-3 ml-8">输入 AD 域账号名称即可授权，用户之后可用 AD 密码登录</p>
        <div className="grid grid-cols-3 gap-2 mb-3">
          <input type="text" placeholder="AD 域账号 (如 zhangsan)" value={adUser.username} onChange={e => setAdUser(p => ({ ...p, username: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A] focus:border-transparent" />
          <input type="text" placeholder="显示名 (可选)" value={adUser.display_name} onChange={e => setAdUser(p => ({ ...p, display_name: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A] focus:border-transparent" />
          <select value={adUser.role} onChange={e => setAdUser(p => ({ ...p, role: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A] focus:border-transparent">
            <option value="user">普通用户</option><option value="admin">管理员</option>
          </select>
          <select value={adUser.group} onChange={e => setAdUser(p => ({ ...p, group: e.target.value }))} className="px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A] focus:border-transparent">
            <option value="">无组别</option>
            {groups.map(g => <option key={g} value={g}>{g}</option>)}
          </select>
        </div>
        <button disabled={!adUser.username.trim()} onClick={async () => {
          try {
            const token = localStorage.getItem('zops_token');
            const response = await fetch('/api/auth/authorize-ad', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify(adUser) });
            if (response.ok) {
              setToast({ ok: true, text: `AD 用户 ${adUser.username} 已授权` });
              setAdUser({ username: '', display_name: '', role: 'user', group: '' }); fetchUsers();
            } else {
              setToast({ ok: false, text: await errDetail(response, '授权失败') });
            }
          } catch (error) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
        }} className="bg-[#16A34A] hover:bg-[#15803D] disabled:bg-gray-200 disabled:text-[#4B5563] text-white px-4 py-2 rounded-lg text-sm font-medium  transition-colors">授权</button>
      </div>

      {/* 权限使用度量 (定期复核依据): 通过/拒绝累计 + 持有人数 + 治理建议 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] flex items-center justify-between cursor-pointer" onClick={() => { setMetricsOpen(o => !o); if (!metricsOpen && permMetrics.length === 0) loadPermMetrics(); }}>
          <div className="text-sm font-semibold text-[#374151] flex items-center gap-2">
            <Activity size={14} className="text-[#9CA3AF]" /> 权限使用度量
            <span className="text-xs font-normal text-[#9CA3AF]">used=权限门通过 · denied=403 拒绝 (越权尝试) · 定期复核依据</span>
          </div>
          <button className="text-xs text-[#2563EB] hover:underline">{metricsOpen ? '收起' : '刷新/展开'}</button>
        </div>
        {metricsOpen && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-[#F9FAFB] text-[#6B7280]">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">权限点</th>
                  <th className="px-3 py-2 text-left font-medium">域</th>
                  <th className="px-3 py-2 text-right font-medium">持有人</th>
                  <th className="px-3 py-2 text-right font-medium">通过</th>
                  <th className="px-3 py-2 text-right font-medium">拒绝</th>
                  <th className="px-3 py-2 text-left font-medium">治理建议</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F3F4F6]">
                {permMetrics.map(m => (
                  <tr key={m.key} className="hover:bg-[#F9FAFB]">
                    <td className="px-3 py-2 text-[#111827] font-medium">{m.label}<span className="ml-1.5 text-[9px] text-[#9CA3AF] font-mono">{m.key}</span></td>
                    <td className="px-3 py-2 text-[#6B7280]">{m.group}</td>
                    <td className="px-3 py-2 text-right font-mono text-[#111827]">{m.holders}</td>
                    <td className="px-3 py-2 text-right font-mono text-[#16A34A]">{m.used}</td>
                    <td className={`px-3 py-2 text-right font-mono ${m.denied > 0 ? 'text-[#DC2626] font-semibold' : 'text-[#9CA3AF]'}`}>{m.denied}</td>
                    <td className="px-3 py-2 text-[#6B7280]">{m.advice || (m.used > 0 && m.denied > 0 ? '使用正常, 拒绝可关注' : '使用正常')}</td>
                  </tr>
                ))}
                {permMetrics.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-[#9CA3AF]">暂无度量数据 (权限门通过/拒绝时累计)</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 业务权限编辑弹层: 目录由后端 BIZ_PERMS 下发, 新增业务域自动出现 */}
      {permUser && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setPermUser(null)}>
          <div className="bg-white rounded-xl shadow-xl max-w-2xl w-full mx-4 max-h-[85vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="px-5 py-4 border-b border-[#E5E7EB] flex items-center justify-between">
              <div>
                <div className="text-sm font-semibold text-[#111827]">业务权限 · {permUser}</div>
                <div className="text-xs text-[#9CA3AF] mt-0.5">与系统角色分离; 系统管理员默认仅审计查看</div>
              </div>
              <button onClick={() => setPermUser(null)} className="text-[#9CA3AF] hover:text-[#111827] text-lg leading-none px-1">✕</button>
            </div>
            <div className="px-5 py-4 overflow-y-auto space-y-4 flex-1 min-h-0">
              {Object.keys(bizRoles).length > 0 && (
                <div className="px-3 py-2.5 rounded-lg bg-violet-50 border border-violet-100">
                  <div className="text-[10px] font-semibold uppercase tracking-widest text-violet-600 mb-1.5">角色模板 (点击一键勾选, 可再微调)</div>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(bizRoles).map(([name, perms]) => (
                      <button key={name} onClick={() => setPermDraft(new Set(perms))}
                        className="text-[11px] px-2 py-1 rounded-full bg-white border border-violet-200 text-violet-700 hover:border-violet-400 transition-colors"
                        title={`包含 ${perms.length} 项: ${perms.join(', ')}`}>
                        {name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {(users.find(u => u.username === permUser)?.inherited_perms || []).length > 0 && (
                <div className="px-3 py-2.5 rounded-lg bg-sky-50 border border-sky-100">
                  <div className="text-[10px] font-semibold uppercase tracking-widest text-sky-600 mb-1.5">从组 {(users.find(u => u.username === permUser)?.group) || ''} 继承（只读，编辑走组配置）</div>
                  <div className="flex flex-wrap gap-1.5">
                    {(users.find(u => u.username === permUser)?.inherited_perms || []).map(k => (
                      <span key={k} className="text-[10px] px-2 py-0.5 rounded-full bg-white border border-sky-200 text-sky-700">
                        {(bizCatalog.find(c => c.key === k)?.label) || k} ⟵ 组
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {bizGroups.map(([group, perms]) => (
                <div key={group}>
                  <div className="text-[10px] font-semibold uppercase tracking-widest text-[#9CA3AF] mb-2">{group}</div>
                  <div className="space-y-1.5">
                    {perms.map(p => (
                      <label key={p.key} className="flex items-start gap-2.5 px-3 py-2 rounded-lg border border-[#E5E7EB] hover:bg-[#F9FAFB] cursor-pointer">
                        <input type="checkbox" checked={permDraft.has(p.key)} onChange={() => togglePermDraft(p.key)}
                          className="mt-0.5 w-3.5 h-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                        <div className="min-w-0">
                          <div className="text-xs font-medium text-[#111827]">{p.label}</div>
                          <div className="text-[10px] text-[#9CA3AF] mt-0.5">{p.desc}</div>
                        </div>
                        {permDraft.has(p.key) && (
                          <select value={ttlDraft[p.key] || 0} onClick={e => e.stopPropagation()}
                            onChange={e => setTtlDraft(d => ({ ...d, [p.key]: Number(e.target.value) }))}
                            className="ml-auto mt-0.5 text-[10px] border border-[#D1D5DB] rounded px-1 py-0.5 shrink-0 text-[#6B7280] bg-white" title="临时授权有效期 (到期自动失效)">
                            <option value={0}>永久</option><option value={7}>7天</option><option value={30}>30天</option><option value={90}>90天</option>
                            {!!ttlDraft[p.key] && ![7, 30, 90].includes(ttlDraft[p.key]) && <option value={ttlDraft[p.key]}>{ttlDraft[p.key]}天</option>}
                          </select>
                        )}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
              {bizCatalog.length === 0 && <div className="text-xs text-[#9CA3AF] text-center py-4">权限点目录加载中...</div>}
            </div>

            {/* 范围约束 (L4): GCOC 字典候选选择, 存三字码/GID; 空=不限; 与组 scope 取更严 (交集) */}
            <div className="px-5 pt-3 border-t border-[#F3F4F6]">
              <div className="text-[10px] font-semibold uppercase tracking-widest text-[#6B7280] mb-2">范围约束（留空 = 不限 · 与组范围取更严）</div>
              <div className="grid grid-cols-2 gap-3 pb-1">
                <ScopeTags label="站点 — 审批申请与决策按此裁剪" hint="输入站点/机房名，如香港/HKG、CTT"
                  options={siteOptions} values={scopeDraft.sites} loading={scopeLoading} tone="blue"
                  onChange={v => setScopeDraft(d => ({ ...d, sites: v }))} />
                <ScopeTags label="客户 — 名称或 GID 均可拦截" hint="搜索 GID 或客户名, 回车手输"
                  options={custOptions} values={scopeDraft.customers} loading={scopeLoading} tone="blue"
                  onChange={v => setScopeDraft(d => ({ ...d, customers: v }))} />
              </div>
            </div>
            <div className="px-5 py-3.5 border-t border-[#E5E7EB] flex justify-end gap-2">
              <button onClick={() => setPermUser(null)} className="px-3 py-1.5 rounded-lg text-xs border border-[#D1D5DB] text-[#6B7280] hover:bg-[#F9FAFB] transition-colors">取消</button>
              <button onClick={saveBizPerms} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-[#2563EB] hover:bg-[#1D4ED8] text-white transition-colors">保存</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ═══ 范围约束标签编辑器: GCOC 字典候选下拉 + 回车手输自定义 (站点存三字码/客户存 GID) ═══
function ScopeTags({ label, hint, options, values, onChange, loading, tone }: {
  label: string; hint: string; options: { value: string; label: string }[];
  values: string[]; onChange: (v: string[]) => void; loading?: boolean; tone: 'blue' | 'teal';
}) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const chipCls = tone === 'blue' ? 'bg-blue-50 border-blue-200 text-blue-700' : 'bg-teal-50 border-teal-200 text-teal-700';
  const filtered = options.filter(o => !values.includes(o.value)
    && (!q.trim() || o.label.toLowerCase().includes(q.trim().toLowerCase()) || o.value.toLowerCase().includes(q.trim().toLowerCase()))).slice(0, 12);
  const add = (v: string) => { const t = v.trim(); if (t && !values.includes(t)) onChange([...values, t]); setQ(''); setOpen(false); };
  return (
    <div className="relative" ref={boxRef}>
      <div className="text-[10px] text-[#9CA3AF] mb-1">{label}{loading && <span className="ml-1 opacity-70">(字典加载中, 可手输)</span>}</div>
      <div className="border border-[#D1D5DB] rounded-lg px-2 py-1.5 min-h-[38px] flex flex-wrap items-center gap-1.5 bg-white focus-within:border-blue-400">
        {values.map(v => (
          <span key={v} className={`inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded-full border ${chipCls}`}
            title={options.find(o => o.value === v)?.label || v}>
            <span className="max-w-[130px] truncate">{v}</span>
            <button onClick={() => onChange(values.filter(x => x !== v))} className="hover:text-red-500 leading-none">×</button>
          </span>
        ))}
        <input value={q}
          onChange={e => { setQ(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 180)}
          onKeyDown={e => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            const hit = filtered.find(o => o.label.toLowerCase().includes(q.trim().toLowerCase()) || o.value.toLowerCase() === q.trim().toLowerCase());
            if (q.trim()) add(hit ? hit.value : q);
          }}
          placeholder={values.length ? '' : hint}
          className="flex-1 min-w-[110px] text-xs outline-none bg-transparent" />
      </div>
      {/* ComboPanel: portal 到 body, 不被弹窗/滚动容器裁剪; 下方空间不足自动向上翻 */}
      {open && filtered.length > 0 && (
        <ComboPanel anchor={boxRef.current}>
          {filtered.map(o => (
            <button key={o.value} type="button" onMouseDown={e => { e.preventDefault(); add(o.value); }}
              className="w-full text-left px-3 py-1.5 text-[11px] hover:bg-gray-50 flex items-center justify-between gap-2">
              <span className="truncate">{o.label}</span>
              <span className="font-mono text-[9px] text-[#9CA3AF] shrink-0">{o.value}</span>
            </button>
          ))}
        </ComboPanel>
      )}
    </div>
  );
}

// GCOC 字典候选懒加载 hook (站点=机房中文名/三字码; 客户=GID/名称; 后端有缓存, 两个组件各自拉取代价低)
function useScopeOptions() {
  const [siteOptions, setSiteOptions] = useState<{ value: string; label: string }[]>([]);
  const [custOptions, setCustOptions] = useState<{ value: string; label: string }[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [m, cs] = await Promise.all([getApprovalMeta().catch(() => null), getApprovalCustomers().catch(() => null)]);
        if (!alive) return;
        if (m?.site_items?.length) setSiteOptions(m.site_items);
        if (cs?.customers?.length) setCustOptions(cs.customers.map(c => ({ value: c.gid, label: c.name })));
      } finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, []);
  return { siteOptions, custOptions, loading };
}

// ═══ 组权限配置 ═══
const ALL_TABS = [
  { href: '/', label: '智能对话' },
  { href: '/nms', label: 'NMS 告警' },
  { href: '/kb', label: '知识库' },
  // 审批中心为全员默认入口 (fixed: 勾选锁定, 后端 /api/auth/me 强制包含)
  { href: '/approvals', label: '审批中心', fixed: true },
  { href: '/agents', label: '系统架构' },
  { href: '/llm-wiki', label: 'LLM-Wiki' },
  { href: '/observability', label: '监控大盘' },
  { href: '/logs', label: '运维日志' },
  { href: '/n8n', label: '工作流' },
  { href: '/admin', label: '系统设置' },
];

function GroupPermissions({ initialGroup, groups = USER_GROUPS, customGroups = [], onGroupsChanged }: { initialGroup?: string; groups?: string[]; customGroups?: string[]; onGroupsChanged?: () => void }) {
  const [selectedGroup, setSelectedGroup] = useState(initialGroup || groups[0]);
  const { siteOptions, custOptions, loading: scopeLoading } = useScopeOptions();  // GCOC 站点/客户字典
  const [perms, setPerms] = useState<Record<string, GroupPerm>>({});
  const [savedPerms, setSavedPerms] = useState<Record<string, GroupPerm>>({});
  const [saving, setSaving] = useState(false);
  const [nmsAvailable, setNmsAvailable] = useState<string[]>([]);
  const [nmsCounts, setNmsCounts] = useState<Record<string, number>>({});
  const [nmsDropdownOpen, setNmsDropdownOpen] = useState(false);
  const [nmsSearch, setNmsSearch] = useState('');
  const nmsDropRef = useRef<HTMLDivElement>(null);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [addSelected, setAddSelected] = useState<Set<string>>(new Set());
  const [memberBusy, setMemberBusy] = useState(false);
  const [memberSearch, setMemberSearch] = useState('');
  const [copyOpen, setCopyOpen] = useState(false);
  const copyDropRef = useRef<HTMLDivElement>(null);
  const [newGroupName, setNewGroupName] = useState('');
  const [groupBusy, setGroupBusy] = useState(false);

  // 组列表变化后，若当前选中组已不存在则回退到第一个组
  useEffect(() => {
    if (groups.length > 0 && !groups.includes(selectedGroup)) setSelectedGroup(groups[0]);
  }, [groups, selectedGroup]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(t);
  }, [toast]);

  const fetchPerms = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/admin/group-permissions', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) {
        const d = await res.json();
        setPerms(d.groups || {});
        setSavedPerms(JSON.parse(JSON.stringify(d.groups || {})));
      }
    } catch (e) { console.error(e); }
  };
  const fetchUsers = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/auth/users', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) { const d = await res.json(); setUsers(d.users || []); }
    } catch (e) { console.error(e); }
  };
  const fetchNmsGroups = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/nms/groups', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (res.ok) { const d = await res.json(); setNmsAvailable(d.groups || []); setNmsCounts(d.group_counts || {}); }
    } catch (e) { console.error(e); }
  };
  useEffect(() => { fetchPerms(); fetchUsers(); fetchNmsGroups(); }, []);

  const current = perms[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };

  // 未保存修改跟踪：对比当前编辑态与已保存快照（忽略数组顺序）
  const normalizePerm = (p?: GroupPerm) =>
    JSON.stringify({ t: [...(p?.tabs || [])].sort(), n: [...(p?.nms_groups || [])].sort(), b: [...(p?.biz_perms || [])].sort(), s: { sites: [...(p?.scope?.sites || [])].sort(), customers: [...(p?.scope?.customers || [])].sort() } });
  const dirtyGroups = useMemo(
    () => groups.filter(g => normalizePerm(perms[g]) !== normalizePerm(savedPerms[g])),
    [perms, savedPerms, groups]
  );
  const isDirty = (g: string) => dirtyGroups.includes(g);

  const members = useMemo(() => users.filter(u => u.group === selectedGroup), [users, selectedGroup]);
  const nonMembers = useMemo(() => users.filter(u => u.group !== selectedGroup), [users, selectedGroup]);
  const memberCountOf = (g: string) => users.filter(u => u.group === g).length;

  const filteredNonMembers = useMemo(
    () => nonMembers.filter(u => {
      const q = memberSearch.trim().toLowerCase();
      if (!q) return true;
      return u.username.toLowerCase().includes(q) || (u.display_name || '').toLowerCase().includes(q);
    }),
    [nonMembers, memberSearch]
  );

  const selectGroup = (g: string) => { setSelectedGroup(g); setAddSelected(new Set()); setNmsDropdownOpen(false); setNmsSearch(''); setMemberSearch(''); };

  const toggleAddUser = (uname: string) => {
    setAddSelected(prev => { const next = new Set(prev); if (next.has(uname)) next.delete(uname); else next.add(uname); return next; });
  };

  const toggleAddAll = () => {
    setAddSelected(prev => (prev.size === filteredNonMembers.length && filteredNonMembers.length > 0) ? new Set() : new Set(filteredNonMembers.map(u => u.username)));
  };

  const copyFrom = (src: string) => {
    const sp = perms[src] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
    setPerms(prev => ({ ...prev, [selectedGroup]: { tabs: [...sp.tabs], nms_groups: [...sp.nms_groups], biz_perms: [...sp.biz_perms], scope: { sites: [...(sp.scope?.sites || [])], customers: [...(sp.scope?.customers || [])] } } }));
    setCopyOpen(false);
    setToast({ ok: true, text: `已从 ${src} 组复制配置到 ${selectedGroup} 组（需保存生效）` });
  };

  const resetCurrent = () => {
    setPerms(prev => ({ ...prev, [selectedGroup]: { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } } }));
    setToast({ ok: true, text: `已将 ${selectedGroup} 组重置为默认（菜单全可见、NMS全选，需保存生效）` });
  };

  const discardChanges = () => {
    setPerms(JSON.parse(JSON.stringify(savedPerms)));
    setToast({ ok: true, text: '已放弃全部未保存修改' });
  };

  useEffect(() => {
    if (!copyOpen) return;
    const handler = (e: MouseEvent) => {
      const _t = e.target as HTMLElement;
      if (copyDropRef.current && !copyDropRef.current.contains(_t) && !_t.closest('[data-combo-panel]')) setCopyOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [copyOpen]);

  // 统一提取后端错误详情
  const errDetail = async (res: Response, fallback: string) => {
    try { const d = await res.json(); if (d.detail) return typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
    return `${fallback} (HTTP ${res.status})`;
  };

  const createGroup = async () => {
    const name = newGroupName.trim();
    if (!name || groupBusy) return;
    setGroupBusy(true);
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/admin/groups', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ name })
      });
      if (res.ok) {
        setToast({ ok: true, text: `组 ${name} 已创建` });
        setNewGroupName('');
        onGroupsChanged?.();
        setSelectedGroup(name);
        fetchPerms();
      } else {
        setToast({ ok: false, text: await errDetail(res, '创建组失败') });
      }
    } catch (e) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
    setGroupBusy(false);
  };

  const deleteGroup = async (g: string) => {
    if (!window.confirm(`确认删除组 ${g}？删除后组内成员将置为无组别，其权限配置一并清除`)) return;
    setGroupBusy(true);
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch(`/api/admin/groups/${encodeURIComponent(g)}`, {
        method: 'DELETE', headers: { 'Authorization': `Bearer ${token || ''}` }
      });
      if (res.ok) {
        const d = await res.json();
        setToast({ ok: true, text: `组 ${g} 已删除${d.moved_members?.length ? `，${d.moved_members.length} 名成员已置为无组别` : ''}` });
        onGroupsChanged?.();
        fetchUsers();
        fetchPerms();
      } else {
        setToast({ ok: false, text: await errDetail(res, '删除组失败') });
      }
    } catch (e) { setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' }); }
    setGroupBusy(false);
  };

  const applyMembers = async (action: 'add' | 'remove', usernames: string[]) => {
    if (!usernames.length) return;
    setMemberBusy(true);
    try {
      const token = localStorage.getItem('zops_token');
      const res = await fetch('/api/admin/group-members', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ group: selectedGroup, usernames, action })
      });
      if (res.ok) {
        const d = await res.json();
        setToast({ ok: true, text: action === 'add' ? `已将 ${d.updated.length} 名用户加入 ${selectedGroup} 组` : `已将 ${d.updated.length} 名用户移出 ${selectedGroup} 组` });
        setAddSelected(new Set());
        fetchUsers();
      } else {
        let detail = `操作失败 (HTTP ${res.status})`;
        try { const d = await res.json(); if (d.detail) detail = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
        setToast({ ok: false, text: detail });
      }
    } catch (e) {
      setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' });
    }
    setMemberBusy(false);
  };

  const toggleTab = (href: string) => {
    setPerms(prev => {
      const cur = prev[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
      const tabs = cur.tabs.includes(href) ? cur.tabs.filter(t => t !== href) : [...cur.tabs, href];
      return { ...prev, [selectedGroup]: { ...cur, tabs } };
    });
  };

  const setAllTabs = (all: boolean) => {
    setPerms(prev => {
      const cur = prev[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
      return { ...prev, [selectedGroup]: { ...cur, tabs: all ? ALL_TABS.map(t => t.href) : [] } };
    });
  };

  const toggleNmsGroup = (g: string) => {
    setPerms(prev => {
      const cur = prev[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
      const nms_groups = cur.nms_groups.includes(g) ? cur.nms_groups.filter(x => x !== g) : [...cur.nms_groups, g];
      return { ...prev, [selectedGroup]: { ...cur, nms_groups } };
    });
  };

  const clearNmsGroups = () => {
    setPerms(prev => {
      const cur = prev[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
      return { ...prev, [selectedGroup]: { ...cur, nms_groups: [] } };
    });
  };

  // 组级业务权限切换 (全体成员继承; 有效权限 = 个人 ∪ 组)
  const toggleGroupBizPerm = (key: string) => {
    setPerms(prev => {
      const cur = prev[selectedGroup] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
      const biz_perms = cur.biz_perms.includes(key) ? cur.biz_perms.filter(x => x !== key) : [...cur.biz_perms, key];
      return { ...prev, [selectedGroup]: { ...cur, biz_perms } };
    });
  };

  // 业务权限点目录 (后端 BIZ_PERMS 下发, 新增业务域自动出现)
  const [bizCatalog, setBizCatalog] = useState<{ key: string; label: string; desc: string; group: string }[]>([]);
  const [bizRoles, setBizRoles] = useState<Record<string, string[]>>({});  // 业务角色模板 (一键套用)
  useEffect(() => {
    const token = localStorage.getItem('zops_token');
    fetch('/api/auth/biz-perms', { headers: { 'Authorization': `Bearer ${token || ''}` } })
      .then(r => r.ok ? r.json() : { perms: [] })
      .then(d => { setBizCatalog(d.perms || []); setBizRoles(d.roles || {}); })
      .catch(() => {});
  }, []);

  const filteredNmsAvailable = useMemo(
    () => nmsAvailable.filter(g => g.toLowerCase().includes(nmsSearch.trim().toLowerCase())),
    [nmsAvailable, nmsSearch]
  );

  useEffect(() => {
    if (!nmsDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      const _t = e.target as HTMLElement;
      if (nmsDropRef.current && !nmsDropRef.current.contains(_t) && !_t.closest('[data-combo-panel]')) setNmsDropdownOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [nmsDropdownOpen]);

  // 保存全部有修改的组（避免切组后未保存修改静默丢失）
  const save = async () => {
    const targets = dirtyGroups.length ? dirtyGroups : [selectedGroup];
    setSaving(true);
    try {
      const token = localStorage.getItem('zops_token');
      const failed: string[] = [];
      for (const g of targets) {
        const p = perms[g] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
        const res = await fetch('/api/admin/group-permissions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
          body: JSON.stringify({ group: g, tabs: p.tabs, nms_groups: p.nms_groups, biz_perms: p.biz_perms, scope: p.scope || { sites: [], customers: [] } })
        });
        if (!res.ok) failed.push(g);
      }
      if (failed.length === 0) {
        setSavedPerms(JSON.parse(JSON.stringify(perms)));
        setToast({ ok: true, text: targets.length > 1 ? `已保存 ${targets.join('、')} 共 ${targets.length} 个组的配置` : `${targets[0]} 组权限已保存` });
      } else {
        setToast({ ok: false, text: `以下组保存失败: ${failed.join('、')}` });
      }
    } catch (e) {
      setToast({ ok: false, text: '网络请求失败，请确认后端服务已启动' });
    }
    setSaving(false);
  };

  return (
    <div className="max-w-full relative">
      {/* Toast 提示 */}
      {toast && (
        <div className={`fixed top-6 right-6 z-50 flex items-center gap-2.5 px-4 py-3 rounded-lg shadow-lg border text-sm font-medium transition-all duration-300 ${
          toast.ok ? 'bg-teal-600 text-white border-teal-500' : 'bg-red-600 text-white border-red-500'}`}>
          {toast.ok ? <CheckCircle size={16} /> : <XCircle size={16} />}
          <span>{toast.text}</span>
        </div>
      )}
      <div className="flex gap-5">
        {/* 左侧组别选择 */}
        <div className="w-44 shrink-0">
          <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
            <div className="px-3 py-2.5 border-b border-[#F3F4F6] bg-[#F9FAFB] text-xs font-semibold text-[#374151] flex items-center justify-between">
              <span>用户组</span>
              <span className="text-[10px] text-[#9CA3AF] font-normal">{groups.length} 个</span>
            </div>
            <div className="divide-y divide-[#F3F4F6]">
              {groups.map(g => {
                const isCustom = customGroups.includes(g);
                return (
                  <div key={g} onClick={() => selectGroup(g)}
                    className={`group/row w-full text-left px-3.5 py-2.5 text-sm font-medium transition-colors flex items-center justify-between gap-1 cursor-pointer ${
                      selectedGroup === g ? 'bg-teal-50 text-teal-700 border-r-2 border-teal-500' : 'text-[#4B5563] hover:bg-[#F9FAFB]'}`}>
                    <span className="flex items-center gap-1.5 min-w-0">
                      <span className="truncate">{g}</span>
                      {isCustom && <span className="text-[9px] px-1 py-px rounded bg-purple-50 text-purple-600 border border-purple-100 shrink-0">自定义</span>}
                      {isDirty(g) && <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" title="有未保存修改" />}
                    </span>
                    <span className="flex items-center gap-1 shrink-0">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${selectedGroup === g ? 'bg-teal-100 text-teal-700' : 'bg-gray-100 text-[#9CA3AF]'}`}>{memberCountOf(g)}</span>
                      {isCustom && (
                        <span onClick={e => { e.stopPropagation(); deleteGroup(g); }} title="删除该组"
                          className="hidden group-hover/row:inline-flex text-[#9CA3AF] hover:text-[#DC2626] cursor-pointer">
                          <Trash2 size={12} />
                        </span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
            {/* 新建组 */}
            <div className="border-t border-[#F3F4F6] p-2 space-y-1.5">
              <input type="text" value={newGroupName} onChange={e => setNewGroupName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') createGroup(); }}
                placeholder="新组名称..." maxLength={20}
                className="w-full px-2.5 py-1.5 border border-[#E5E7EB] rounded-md text-xs focus:outline-none focus:border-teal-400" />
              <button onClick={createGroup} disabled={!newGroupName.trim() || groupBusy}
                className="w-full flex items-center justify-center gap-1 bg-teal-600 hover:bg-teal-700 disabled:bg-[#E5E7EB] disabled:text-[#9CA3AF] text-white px-2 py-1.5 rounded-md text-xs font-medium transition-colors">
                <Plus size={12} />{groupBusy ? '处理中...' : '新建组'}
              </button>
            </div>
          </div>
        </div>
        {/* 右侧配置区 */}
        <div className="flex-1">
          {/* 权限概览表 */}
          <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden mb-5">
            <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] text-sm font-semibold text-[#374151] flex items-center gap-2">
              <ShieldCheck size={14} className="text-[#9CA3AF]" /> 权限概览
              <span className="ml-auto text-xs text-[#9CA3AF] font-normal">点击行切换组</span>
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[#9CA3AF] border-b border-[#F3F4F6]">
                  <th className="px-4 py-2.5 font-medium">组</th>
                  <th className="px-4 py-2.5 font-medium">成员</th>
                  <th className="px-4 py-2.5 font-medium">可见菜单</th>
                  <th className="px-4 py-2.5 font-medium">NMS 分组</th>
                  <th className="px-4 py-2.5 font-medium">业务权限</th>
                  <th className="px-4 py-2.5 font-medium">状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F3F4F6]">
                {groups.map(g => {
                  const p = perms[g] || { tabs: [], nms_groups: [], biz_perms: [], scope: { sites: [], customers: [] } };
                  const configured = p.tabs.length > 0 || p.nms_groups.length > 0 || p.biz_perms.length > 0;
                  const active = selectedGroup === g;
                  return (
                    <tr key={g} onClick={() => selectGroup(g)} className={`cursor-pointer transition-colors ${active ? 'bg-teal-50/60' : 'hover:bg-[#F9FAFB]'}`}>
                      <td className={`px-4 py-2.5 font-semibold ${active ? 'text-teal-700' : 'text-[#111827]'}`}>{g}</td>
                      <td className="px-4 py-2.5 text-[#4B5563]">{memberCountOf(g)} 人</td>
                      <td className="px-4 py-2.5 text-[#4B5563]">{p.tabs.length > 0 ? `${p.tabs.length}/${ALL_TABS.length}` : '全部（默认）'}</td>
                      <td className="px-4 py-2.5 text-[#4B5563]">{p.nms_groups.length > 0 ? `${p.nms_groups.length} 个` : '全选（默认）'}</td>
                      <td className="px-4 py-2.5 text-[#4B5563]">{p.biz_perms.length > 0 ? `${p.biz_perms.length} 项` : '—'}</td>
                      <td className="px-4 py-2.5">
                        <span className="inline-flex items-center gap-1.5">
                          <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${configured ? 'bg-teal-50 text-teal-700 border border-teal-100' : 'bg-gray-100 text-[#6B7280]'}`}>{configured ? '已配置' : '默认'}</span>
                          {isDirty(g) && <span className="text-[11px] px-2 py-0.5 rounded-full font-medium bg-amber-50 text-amber-700 border border-amber-200">未保存</span>}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {/* 复制配置 / 重置 */}
          <div className="flex items-center gap-2 mb-5">
            <div className="relative" ref={copyDropRef}>
              <button type="button" onClick={() => setCopyOpen(o => !o)}
                className="flex items-center gap-1.5 px-3 py-2 border border-[#D1D5DB] rounded-lg bg-white text-xs font-medium text-[#4B5563] hover:border-[#9CA3AF] hover:text-[#111827] transition-colors">
                <Copy size={13} /> 从其他组复制配置
                <ChevronDown size={13} className={`transition-transform ${copyOpen ? 'rotate-180' : ''}`} />
              </button>
              {copyOpen && (
                <div className="absolute z-20 mt-1.5 w-44 bg-white border border-[#E5E7EB] rounded-xl shadow-lg overflow-hidden">
                  <div className="p-1.5">
                    {groups.filter(g => g !== selectedGroup).map(g => (
                      <button key={g} onClick={() => copyFrom(g)} className="w-full text-left px-3 py-2 text-sm text-[#374151] rounded-md hover:bg-[#F9FAFB] transition-colors">{g}</button>
                    ))}
                  </div>
                </div>
              )}
            </div>
            <button type="button" onClick={resetCurrent}
              className="flex items-center gap-1.5 px-3 py-2 border border-[#D1D5DB] rounded-lg bg-white text-xs font-medium text-[#4B5563] hover:border-[#9CA3AF] hover:text-[#111827] transition-colors">
              <RefreshCw size={13} /> 重置 {selectedGroup} 为默认
            </button>
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-sm font-semibold text-[#111827]">可见菜单 (TAB)
                <span className="ml-1.5 text-xs font-normal text-[#9CA3AF]">{current.tabs.length > 0 ? `已选 ${current.tabs.length}/${ALL_TABS.length}` : '默认全部可见'}</span>
              </h4>
              <div className="flex items-center gap-2 text-[11px]">
                <button type="button" onClick={() => setAllTabs(true)} className="text-teal-600 hover:text-teal-700 font-medium">全选</button>
                <span className="text-[#E5E7EB]">|</span>
                <button type="button" onClick={() => setAllTabs(false)} className="text-[#6B7280] hover:text-[#111827] font-medium">清空</button>
              </div>
            </div>
            <p className="text-xs text-[#9CA3AF] mb-3">勾选 {selectedGroup} 组用户可见的侧边栏菜单项，未配置则默认全部可见</p>
            <div className="grid grid-cols-2 gap-2">
              {ALL_TABS.map(tab => {
                const fixed = (tab as { fixed?: boolean }).fixed;
                return (
                <label key={tab.href} className={`flex items-center gap-2 px-3 py-2.5 rounded-lg border transition-all ${
                  fixed ? 'border-teal-200 bg-teal-50/60 cursor-default'
                  : current.tabs.includes(tab.href) ? 'border-teal-300 bg-teal-50' : 'border-[#E5E7EB] hover:border-[#D1D5DB]'} ${fixed ? '' : 'cursor-pointer'}`}>
                  <input type="checkbox" checked={fixed ? true : current.tabs.includes(tab.href)}
                    onChange={() => { if (!fixed) toggleTab(tab.href); }} disabled={fixed}
                    className="w-3.5 h-3.5 rounded border-gray-300 text-teal-600 focus:ring-teal-500" />
                  <span className="text-sm text-[#374151]">{tab.label}</span>
                  {fixed && <span className="ml-auto text-[10px] text-teal-600 shrink-0">全员默认</span>}
                </label>
                );
              })}
            </div>
          </div>
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
            <h4 className="text-sm font-semibold text-[#111827] mb-3">NMS 告警默认预选分组</h4>
            <p className="text-xs text-[#9CA3AF] mb-3">勾选 {selectedGroup} 组用户进入NMS页面时自动选中的告警分组，未配置则默认全选</p>
            <div className="relative" ref={nmsDropRef}>
              <button type="button" onClick={() => setNmsDropdownOpen(o => !o)} disabled={nmsAvailable.length === 0}
                className="w-full flex items-center justify-between gap-2 px-3 py-2.5 border border-[#D1D5DB] rounded-lg bg-white text-sm hover:border-[#9CA3AF] disabled:bg-[#F9FAFB] disabled:cursor-not-allowed transition-colors">
                <span className={current.nms_groups.length ? 'text-[#111827]' : 'text-[#9CA3AF]'}>
                  {nmsAvailable.length === 0 ? '暂无可用分组（NMS 未抓取到数据）' : current.nms_groups.length ? `已选 ${current.nms_groups.length} 个分组` : '未配置（默认全选）'}
                </span>
                <ChevronDown size={15} className={`text-[#9CA3AF] shrink-0 transition-transform ${nmsDropdownOpen ? 'rotate-180' : ''}`} />
              </button>
              {nmsDropdownOpen && nmsAvailable.length > 0 && (
                <ComboPanel anchor={nmsDropRef.current} maxHeight={360}>
                  <div className="p-2 border-b border-[#F3F4F6]">
                    <input type="text" value={nmsSearch} onChange={e => setNmsSearch(e.target.value)}
                      placeholder="搜索分组..." className="w-full px-2.5 py-1.5 border border-[#E5E7EB] rounded-md text-xs focus:outline-none focus:border-teal-400" />
                  </div>
                  <div className="max-h-56 overflow-y-auto p-1.5">
                    {filteredNmsAvailable.map(g => (
                      <label key={g} className="flex items-center gap-2 px-2.5 py-2 rounded-md cursor-pointer hover:bg-[#F9FAFB] transition-colors">
                        <input type="checkbox" checked={current.nms_groups.includes(g)} onChange={() => toggleNmsGroup(g)}
                          className="w-3.5 h-3.5 rounded border-gray-300 text-teal-600 focus:ring-teal-500" />
                        <span className="text-sm text-[#374151] truncate">{g}</span>
                        {nmsCounts[g] !== undefined && <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-[#9CA3AF] shrink-0">{nmsCounts[g]}</span>}
                      </label>
                    ))}
                    {filteredNmsAvailable.length === 0 && <div className="text-xs text-[#9CA3AF] text-center py-3">无匹配分组</div>}
                  </div>
                  <div className="border-t border-[#F3F4F6] px-3 py-2 flex items-center justify-between">
                    <span className="text-[11px] text-[#9CA3AF]">已选 {current.nms_groups.length} 个</span>
                    <div className="flex items-center gap-2.5">
                      <button type="button" onClick={clearNmsGroups} className="text-[11px] text-[#6B7280] hover:text-[#111827] font-medium">清空</button>
                      <button type="button" onClick={() => setNmsDropdownOpen(false)} className="text-[11px] text-teal-600 hover:text-teal-700 font-medium">完成</button>
                    </div>
                  </div>
                </ComboPanel>
              )}
            </div>
            {/* 已选分组徽章：无需展开下拉即可查看/移除 */}
            {current.nms_groups.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {current.nms_groups.map(g => (
                  <span key={g} className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-teal-50 text-teal-700 border border-teal-200">
                    {g}
                    <button onClick={() => toggleNmsGroup(g)} className="text-teal-400 hover:text-red-600" title="移除">×</button>
                  </span>
                ))}
              </div>
            )}
          </div>
          </div>
          {/* 组级业务权限: 全体成员继承 (有效权限 = 个人 ∪ 组) */}
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 mt-5">
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-sm font-semibold text-[#111827]">业务权限（成员继承）
                <span className="ml-1.5 text-xs font-normal text-[#9CA3AF]">{current.biz_perms.length > 0 ? `已选 ${current.biz_perms.length} 项` : '未配置（成员仅个人权限）'}</span>
              </h4>
            </div>
            <p className="text-xs text-[#9CA3AF] mb-2">勾选 {selectedGroup} 组全体成员继承的业务权限（与个人权限取并集）；系统管理员不受影响，默认仅审计查看</p>
            {Object.keys(bizRoles).length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap mb-3">
                <span className="text-[10px] text-[#9CA3AF]">角色模板:</span>
                {Object.entries(bizRoles).map(([name, perms]) => (
                  <button key={name} onClick={() => {
                    setPerms(prev => ({ ...prev, [selectedGroup]: { ...current, biz_perms: [...perms] } }));
                    setToast({ ok: true, text: `已套用「${name}」模板 (${perms.length} 项), 可继续勾选微调` });
                  }} className="text-[11px] px-2 py-1 rounded-full bg-teal-50 border border-teal-200 text-teal-700 hover:border-teal-400 transition-colors"
                    title={`替换为 ${perms.length} 项: ${perms.join(', ')}`}>
                    {name}
                  </button>
                ))}
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-2">
              {bizCatalog.map(p => (
                <label key={p.key} className={`flex items-start gap-2 px-3 py-2.5 rounded-lg border cursor-pointer transition-all ${
                  current.biz_perms.includes(p.key) ? 'border-teal-300 bg-teal-50' : 'border-[#E5E7EB] hover:border-[#D1D5DB]'}`}>
                  <input type="checkbox" checked={current.biz_perms.includes(p.key)} onChange={() => toggleGroupBizPerm(p.key)}
                    className="mt-0.5 w-3.5 h-3.5 rounded border-gray-300 text-teal-600 focus:ring-teal-500" />
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-[#111827]">{p.label}</div>
                    <div className="text-[10px] text-[#9CA3AF] mt-0.5">{p.group}</div>
                  </div>
                </label>
              ))}
              {bizCatalog.length === 0 && <div className="text-xs text-[#9CA3AF] col-span-5 py-2">权限点目录加载中...</div>}
            </div>
            {/* 组级范围约束: GCOC 字典候选选择 (站点存三字码/客户存 GID); 空=不限; 与个人 scope 取更严 (交集) */}
            <div className="mt-4 pt-3 border-t border-[#F3F4F6]">
              <div className="text-[10px] font-semibold uppercase tracking-widest text-teal-700 mb-2">范围约束（成员继承 · 留空 = 不限 · 与个人范围取更严）</div>
              <div className="grid grid-cols-2 gap-3">
                <ScopeTags label="站点 — 成员可申请/可审批的站点" hint="输入站点/机房名，如香港/HKG、CTT"
                  options={siteOptions} values={current.scope?.sites || []} loading={scopeLoading} tone="teal"
                  onChange={v => setPerms(prev => ({ ...prev, [selectedGroup]: { ...current, scope: { ...(current.scope || { sites: [], customers: [] }), sites: v } } }))} />
                <ScopeTags label="客户 — 名称或 GID 均可拦截" hint="搜索 GID 或客户名, 回车手输"
                  options={custOptions} values={current.scope?.customers || []} loading={scopeLoading} tone="teal"
                  onChange={v => setPerms(prev => ({ ...prev, [selectedGroup]: { ...current, scope: { ...(current.scope || { sites: [], customers: [] }), customers: v } } }))} />
              </div>
            </div>
          </div>
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 mt-5">
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-sm font-semibold text-[#111827]">组成员管理</h4>
              <span className="text-xs text-[#9CA3AF]">{selectedGroup} 组当前 {members.length} 人</span>
            </div>
            <p className="text-xs text-[#9CA3AF] mb-3">管理 {selectedGroup} 组成员，勾选下方用户可批量加入，调整后即时生效</p>
            <div className="flex flex-wrap gap-2 mb-4">
              {members.map(m => (
                <span key={m.username} className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-full bg-teal-50 text-teal-700 border border-teal-200">
                  <span className="w-4 h-4 rounded-full bg-teal-600 text-white flex items-center justify-center text-[10px] font-bold">{getInitials(m.display_name || m.username)}</span>
                  {m.display_name || m.username}
                  <button onClick={() => applyMembers('remove', [m.username])} disabled={memberBusy} className="text-teal-400 hover:text-red-600 ml-0.5" title="移出该组">×</button>
                </span>
              ))}
              {members.length === 0 && <span className="text-xs text-[#9CA3AF]">暂无成员，请从下方批量添加</span>}
            </div>
            <div className="border-t border-[#F3F4F6] pt-3">
              <div className="relative mb-2">
                <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
                <input type="text" value={memberSearch} onChange={e => setMemberSearch(e.target.value)}
                  placeholder="搜索可加入用户..." className="w-full pl-8 pr-3 py-1.5 border border-[#D1D5DB] rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-teal-500 focus:border-transparent" />
              </div>
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-2 text-xs font-medium text-[#374151] cursor-pointer">
                  <input type="checkbox" checked={filteredNonMembers.length > 0 && addSelected.size === filteredNonMembers.length} onChange={toggleAddAll}
                    className="w-3.5 h-3.5 rounded border-gray-300 text-teal-600 focus:ring-teal-500" />
                  全选可加入用户 ({filteredNonMembers.length})
                </label>
                <button onClick={() => applyMembers('add', Array.from(addSelected))} disabled={memberBusy || addSelected.size === 0}
                  className="bg-teal-600 hover:bg-teal-700 disabled:bg-[#E5E7EB] disabled:text-[#9CA3AF] text-white px-3.5 py-1.5 rounded-lg text-xs font-medium transition-colors flex items-center gap-1">
                  <Plus size={13} />{memberBusy ? '处理中...' : `批量加入 ${selectedGroup} 组${addSelected.size ? ` (${addSelected.size})` : ''}`}
                </button>
              </div>
              <div className="grid grid-cols-3 gap-2 max-h-44 overflow-y-auto">
                {filteredNonMembers.map(u => (
                  <label key={u.username} className={`flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer transition-all ${
                    addSelected.has(u.username) ? 'border-teal-300 bg-teal-50' : 'border-[#E5E7EB] hover:border-[#D1D5DB]'}`}>
                    <input type="checkbox" checked={addSelected.has(u.username)} onChange={() => toggleAddUser(u.username)}
                      className="w-3.5 h-3.5 rounded border-gray-300 text-teal-600 focus:ring-teal-500" />
                    <span className="text-sm text-[#374151] truncate">{u.display_name || u.username}</span>
                    {u.group && <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-[#9CA3AF] shrink-0">{u.group}</span>}
                  </label>
                ))}
                {filteredNonMembers.length === 0 && <div className="col-span-3 text-center text-xs text-[#9CA3AF] py-3">{nonMembers.length === 0 ? '所有用户均已在该组' : '无匹配用户'}</div>}
              </div>
            </div>
          </div>
          <div className="mt-5 flex items-center gap-3">
            <button onClick={save} disabled={saving || (dirtyGroups.length === 0)}
              className="bg-[#0D9488] hover:bg-[#0F766E] disabled:bg-[#E5E7EB] disabled:text-[#9CA3AF] text-white px-5 py-2.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
              <Save size={14} />{saving ? '保存中...' : dirtyGroups.length > 1 ? `保存全部修改 (${dirtyGroups.length} 个组)` : dirtyGroups.length === 1 ? `保存 ${dirtyGroups[0]} 组配置` : '无未保存修改'}
            </button>
            {dirtyGroups.length > 0 && !saving && (
              <button onClick={discardChanges}
                className="px-4 py-2.5 rounded-lg text-sm font-medium text-[#6B7280] border border-[#D1D5DB] bg-white hover:border-[#9CA3AF] hover:text-[#111827] transition-colors">
                放弃修改
              </button>
            )}
            {dirtyGroups.length > 0 && (
              <span className="text-xs text-amber-600 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                {dirtyGroups.join('、')} 有未保存修改
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// API Keys 管理
function APIKeysManagement() {
  const [keys, setKeys] = useState<any[]>([]);
  const [newKeyName, setNewKeyName] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchKeys = async () => {
    try {
      const token = localStorage.getItem('zops_token');
      const response = await fetch('/api/oneapi/keys', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (response.ok) { const data = await response.json(); setKeys(data.keys || []); }
    } catch (error) { console.error('Failed to fetch keys:', error); }
  };
  const createKey = async () => {
    if (!newKeyName.trim()) return;
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      const response = await fetch('/api/oneapi/keys', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify({ name: newKeyName }) });
      if (response.ok) { const data = await response.json(); alert(`API Key 创建成功: ${data.key}\n请妥善保存，关闭后将无法查看!`); setNewKeyName(''); fetchKeys(); }
    } catch (error) { console.error('Failed to create key:', error); }
    setLoading(false);
  };
  const deleteKey = async (keyId: string) => {
    if (!confirm('确定要删除这个 API Key 吗?')) return;
    try {
      const token = localStorage.getItem('zops_token');
      await fetch(`/api/oneapi/keys/${keyId}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token || ''}` } });
      fetchKeys();
    } catch (error) { console.error('Failed to delete key:', error); }
  };
  useEffect(() => { fetchKeys(); }, []);

  return (
    <div className="max-w-full">
      <SectionHeader icon={Key} title="API Keys" subtitle="管理外部 API 访问密钥" color="#EA580C" />
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 mb-4 ">
        <h4 className="text-sm font-semibold text-[#111827] mb-3 flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-[#FFF7ED] flex items-center justify-center"><Plus size={13} className="text-[#EA580C]" /></div>
          创建新 API Key
        </h4>
        <div className="flex gap-2">
          <input type="text" placeholder="输入名称 (如 chatbox)" value={newKeyName} onChange={e => setNewKeyName(e.target.value)} className="flex-1 px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#2563EB]" />
          <button onClick={createKey} disabled={loading || !newKeyName.trim()} className="bg-[#2563EB] hover:bg-[#1D4ED8] disabled:bg-gray-200 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors">{loading ? '创建中...' : '创建'}</button>
        </div>
      </div>
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden ">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] text-sm font-semibold text-[#374151] flex items-center gap-2">
          <Key size={14} className="text-[#9CA3AF]" /> 已创建的 Keys <span className="ml-auto text-xs text-[#9CA3AF] font-normal">{keys.length} 个</span>
        </div>
        <div className="divide-y divide-[#F3F4F6]">
          {keys.map(k => (
            <div key={k.id} className="px-4 py-3.5 flex items-center justify-between hover:bg-[#F9FAFB] transition-colors">
              <div>
                <div className="font-medium text-[#111827] text-sm">{k.label}</div>
                <div className="text-xs text-[#6B7280] mt-0.5 font-mono">{k.key} · {k.created_by} · {k.created_at}</div>
              </div>
              <button onClick={() => deleteKey(k.id)} className="text-[#F87171] hover:text-[#DC2626] p-2 hover:bg-[#FEF2F2] rounded-lg transition-colors"><Trash2 size={16} /></button>
            </div>
          ))}
          {keys.length === 0 && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">暂无 API Keys</div>}
        </div>
      </div>
    </div>
  );
}

// 审计日志
function AuditLogs() {
  const today = new Date().toISOString().split('T')[0];
  const fmtDate = (d: Date) => d.toISOString().split('T')[0];
  const daysAgo = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return fmtDate(d); };
  // 视图模式: 'agent' = Agent 操作 ; 'user' = 用户审计明细 (登录/查询/配置) ; 'summary' = 用户汇总 (登录/查询/token)
  const [view, setView] = useState<'agent' | 'user' | 'summary'>('agent');
  // 共享: 时间范围 (起始 + 结束, 默认今天)
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  const [logs, setLogs] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<any>(null);
  const [search, setSearch] = useState('');
  const [agentFilter, setAgentFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  // 用户审计专用过滤
  const [userFilter, setUserFilter] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  // 用户汇总专用
  const [summary, setSummary] = useState<{ rows: any[]; totals: any; excluded_count?: number; excluded_patterns?: string[] } | null>(null);
  const [summarySort, setSummarySort] = useState<{ key: string; dir: 'asc' | 'desc' }>({ key: 'total_tokens', dir: 'desc' });
  const [backfilling, setBackfilling] = useState(false);
  const [backfillResult, setBackfillResult] = useState<any>(null);
  // 用户档案: username -> {display_name, role, group, auth_source} (从 /api/auth/users 取)
  // 用于在审计明细行/汇总表中展示"显示名 + 组别 + 角色", 替代只显示原始 username
  const [userProfiles, setUserProfiles] = useState<Record<string, { display_name: string; role: string; group: string; auth_source: string }>>({});

  // 加载用户档案 (组别/显示名/角色), 后续在审计日志明细行 / 汇总表中展示
  useEffect(() => {
    const token = localStorage.getItem('zops_token');
    fetch('/api/auth/users', { headers: { 'Authorization': `Bearer ${token || ''}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const m: Record<string, any> = {};
        (d?.users || []).forEach((u: any) => {
          m[u.username] = {
            display_name: u.display_name || u.username,
            role: u.role || 'user',
            group: u.group || '',
            auth_source: u.auth_source || 'local',
          };
        });
        setUserProfiles(m);
      })
      .catch(() => {});
  }, []);

  const preset = (n: number) => {
    if (n === 0) { setStartDate(today); setEndDate(today); }
    else { setStartDate(daysAgo(n - 1)); setEndDate(today); }
  };

  const buildRange = () => {
    const p = new URLSearchParams({ start_date: startDate, end_date: endDate, count: '200' });
    if (view === 'agent' && agentFilter) p.set('agent', agentFilter);
    if (view === 'user' && userFilter) p.set('username', userFilter);
    if (view === 'user' && actionFilter) p.set('action', actionFilter);
    return p.toString();
  };

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      if (view === 'summary') {
        const response = await fetch(`/api/audit/user-stats?start_date=${startDate}&end_date=${endDate}`,
          { headers: { 'Authorization': `Bearer ${token || ''}` } });
        if (response.ok) {
          const data = await response.json();
          setSummary({ rows: data.rows || [], totals: data.totals || {} });
        }
        setLoading(false);
        return;
      }
      const url = view === 'agent'
        ? `/api/audit/recent?${buildRange()}`
        : `/api/audit/user-events?${buildRange()}`;
      const response = await fetch(url, { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (response.ok) {
        const data = await response.json();
        setLogs(data.entries || []);
        if (view === 'agent') {
          setStats({ count: data.count, errors: data.errors, success_rate: data.success_rate, agents: data.agents });
        } else {
          setStats({ count: data.count, usernames: data.filters?.usernames || [], actions: data.filters?.actions || [] });
        }
      }
    } catch (error) { console.error('Failed to fetch audit logs:', error); }
    setLoading(false);
  };
  useEffect(() => { fetchLogs(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [view, startDate, endDate]);
  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN');

  const agentList = useMemo(() => (Array.isArray(stats?.agents) ? stats.agents : []), [stats]);
  const userList = useMemo(() => (Array.isArray(stats?.usernames) ? stats.usernames : []), [stats]);
  const actionList = useMemo(() => (Array.isArray(stats?.actions) ? stats.actions : []), [stats]);

  const filteredLogs = useMemo(() => logs.filter(l => {
    const q = search.trim().toLowerCase();
    if (view === 'agent') {
      if (q && !((l.action || '').toLowerCase().includes(q) || (l.detail || '').toLowerCase().includes(q) || (l.agent_id || '').toLowerCase().includes(q))) return false;
      if (agentFilter && l.agent_id !== agentFilter) return false;
      if (statusFilter === 'success' && !l.success) return false;
      if (statusFilter === 'fail' && l.success) return false;
    } else {
      if (q && !((l.username || '').toLowerCase().includes(q) || (l.action || '').toLowerCase().includes(q) || (l.detail || '').toLowerCase().includes(q))) return false;
      if (userFilter && l.username !== userFilter) return false;
      if (actionFilter && l.action !== actionFilter) return false;
    }
    return true;
  }), [logs, search, agentFilter, statusFilter, userFilter, actionFilter, view]);

  // 用户汇总排序
  const sortedSummary = useMemo(() => {
    const rows = (summary?.rows || []).slice();
    const { key, dir } = summarySort;
    rows.sort((a: any, b: any) => {
      const av = a[key] ?? 0; const bv = b[key] ?? 0;
      if (typeof av === 'string') return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      return dir === 'asc' ? av - bv : bv - av;
    });
    return rows;
  }, [summary, summarySort]);

  const sortBy = (key: string) => setSummarySort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'desc' });
  const sortArrow = (key: string) => summarySort.key === key ? (summarySort.dir === 'asc' ? ' ↑' : ' ↓') : '';

  const fmtNum = (n: number) => (n || 0).toLocaleString();

  // 用户审计动作 → 展示标签与色板
  const actionMeta = (a: string): { label: string; tone: string } => {
    const m: Record<string, { label: string; tone: string }> = {
      login:           { label: '登录',   tone: 'bg-blue-50 text-[#2563EB] border-blue-100' },
      logout:          { label: '登出',   tone: 'bg-slate-50 text-[#475569] border-slate-200' },
      query:           { label: '查询',   tone: 'bg-violet-50 text-[#7C3AED] border-violet-100' },
      biz_perms:       { label: '业务权限', tone: 'bg-amber-50 text-[#D97706] border-amber-100' },
      biz_perms_ttl:   { label: '临时授权', tone: 'bg-orange-50 text-[#EA580C] border-orange-100' },
      biz_scope:       { label: '范围变更', tone: 'bg-cyan-50 text-[#0891B2] border-cyan-100' },
      group_biz_perms: { label: '组权限', tone: 'bg-teal-50 text-[#0D9488] border-teal-100' },
      create_api_key:  { label: '新建Key', tone: 'bg-emerald-50 text-[#16A34A] border-emerald-100' },
      delete_api_key:  { label: '删除Key', tone: 'bg-red-50 text-[#DC2626] border-red-100' },
      llm_config:      { label: '模型配置', tone: 'bg-indigo-50 text-[#4F46E5] border-indigo-100' },
      add_provider:    { label: '新增Provider', tone: 'bg-emerald-50 text-[#16A34A] border-emerald-100' },
      delete_provider: { label: '删除Provider', tone: 'bg-red-50 text-[#DC2626] border-red-100' },
    };
    return m[a] || { label: a || '-', tone: 'bg-gray-50 text-[#4B5563] border-gray-200' };
  };

  return (
    <div className="max-w-full">
      <SectionHeader icon={FileText} title="审计日志" subtitle="Agent 操作 + 用户审计 (登录/查询/配置变更) + 用户用量汇总" color="#7C3AED" />

      {/* 视图切换 + 时间范围选择器 */}
      <div className="flex flex-wrap items-center gap-3 mb-4 bg-white border border-[#E5E7EB] rounded-xl px-4 py-3 ">
        <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-0.5">
          <button onClick={() => setView('agent')}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${view === 'agent' ? 'bg-white text-[#7C3AED] shadow-sm' : 'text-[#6B7280] hover:text-[#374151]'}`}>
            Agent 操作
          </button>
          <button onClick={() => setView('user')}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${view === 'user' ? 'bg-white text-[#7C3AED] shadow-sm' : 'text-[#6B7280] hover:text-[#374151]'}`}>
            用户审计
          </button>
          <button onClick={() => setView('summary')}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${view === 'summary' ? 'bg-white text-[#7C3AED] shadow-sm' : 'text-[#6B7280] hover:text-[#374151]'}`}>
            用户汇总
          </button>
        </div>

        {/* 时间范围: 起始 + 结束 (替代原单点日期) */}
        <div className="flex items-center gap-1.5 text-xs text-[#6B7280]">
          <span>时间范围</span>
          <input type="date" value={startDate} max={endDate} onChange={e => setStartDate(e.target.value)}
            className="px-2 py-1.5 border border-[#D1D5DB] rounded-lg text-sm" title="起始日期" />
          <span>→</span>
          <input type="date" value={endDate} min={startDate} max={today} onChange={e => setEndDate(e.target.value)}
            className="px-2 py-1.5 border border-[#D1D5DB] rounded-lg text-sm" title="结束日期" />
        </div>
        {/* 快捷区间 */}
        <div className="flex items-center gap-1">
          {[{ k: 0, l: '今天' }, { k: 1, l: '近1天' }, { k: 7, l: '近7天' }, { k: 30, l: '近30天' }].map(p => (
            <button key={p.k} onClick={() => preset(p.k)}
              className={`text-[11px] px-2 py-1 rounded-md border transition-colors ${
                (p.k === 0 && startDate === today && endDate === today) ||
                (p.k > 0 && startDate === daysAgo(p.k - 1) && endDate === today)
                  ? 'border-[#7C3AED] bg-violet-50 text-[#7C3AED] font-medium'
                  : 'border-[#E5E7EB] text-[#6B7280] hover:text-[#374151] hover:border-[#D1D5DB]'
              }`}>{p.l}</button>
          ))}
        </div>

        <button onClick={fetchLogs} disabled={loading} className="p-2 hover:bg-gray-100 rounded-lg transition-colors" title="刷新"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} /></button>
        {view !== 'summary' && (
          <div className="relative flex-1 min-w-[160px]">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
            <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder={view === 'agent' ? '搜索操作内容...' : '搜索用户/动作/详情...'}
              className="w-full pl-9 pr-3 py-1.5 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#7C3AED] focus:border-transparent" />
          </div>
        )}
        {view === 'agent' && (
          <>
            <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)} className="px-3 py-1.5 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#7C3AED]">
              <option value="">全部 Agent</option>
              {agentList.map((a: string) => <option key={a} value={a}>{a}</option>)}
            </select>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="px-3 py-1.5 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#7C3AED]">
              <option value="">全部状态</option>
              <option value="success">成功</option>
              <option value="fail">失败</option>
            </select>
          </>
        )}
        {view === 'user' && (
          <>
            <select value={userFilter} onChange={e => setUserFilter(e.target.value)} className="px-3 py-1.5 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#7C3AED]">
              <option value="">全部用户</option>
              {userList.map((u: string) => <option key={u} value={u}>{u}</option>)}
            </select>
            <select value={actionFilter} onChange={e => setActionFilter(e.target.value)} className="px-3 py-1.5 border border-[#D1D5DB] rounded-lg text-sm bg-white text-[#374151] focus:ring-1 focus:ring-[#7C3AED]">
              <option value="">全部动作</option>
              {actionList.map((a: string) => <option key={a} value={a}>{actionMeta(a).label} ({a})</option>)}
            </select>
          </>
        )}
        {stats && view === 'agent' && (<div className="flex gap-4 text-xs ml-auto">
          <span className="px-2.5 py-1 bg-gray-100 rounded-full text-[#4B5563] font-medium">总计 {stats.count}</span>
          <span className="px-2.5 py-1 bg-red-50 rounded-full text-[#DC2626] font-medium">错误 {stats.errors}</span>
          <span className="px-2.5 py-1 bg-green-50 rounded-full text-[#16A34A] font-medium">成功率 {stats.success_rate}%</span>
        </div>)}
      </div>

      {/* 用户汇总视图 */}
      {view === 'summary' && (
        <>
          {/* 顶部汇总条 */}
          {summary?.totals && (
            <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 mb-4">
              {[
                { label: '活跃用户', value: fmtNum(summary.totals.users || 0), color: '#7C3AED' },
                { label: '真实登录', value: fmtNum(summary.totals.logins || 0), color: '#2563EB' },
                { label: '提问调用', value: fmtNum(summary.totals.queries || 0), color: '#0D9488' },
                { label: 'LLM 调用', value: fmtNum(summary.totals.llm_calls || 0), color: '#EA580C' },
                { label: '总 Token 消耗', value: fmtNum(summary.totals.total_tokens || 0), color: '#DC2626' },
              ].map(s => (
                <div key={s.label} className="bg-white border border-[#E5E7EB] rounded-xl px-4 py-3 flex items-center gap-3">
                  <div className="w-1.5 h-10 rounded-full" style={{ backgroundColor: s.color }} />
                  <div>
                    <div className="text-xl font-bold text-[#111827] leading-tight">{s.value}</div>
                    <div className="text-[11px] text-[#6B7280] mt-0.5">{s.label}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
          {/* 过滤提示 + 回填按钮 */}
          <div className="flex flex-wrap items-center gap-2 mb-3">
            {typeof summary?.excluded_count === 'number' && summary.excluded_count > 0 && (
              <div className="flex items-center gap-1.5 text-[11px] px-2.5 py-1 bg-amber-50 text-[#B45309] border border-amber-200 rounded-full font-medium" title={`已隐藏以下规则的临时账号: ${(summary.excluded_patterns || []).join(', ')}`}>
                <Eye size={11} /> 已隐藏 {summary.excluded_count} 条临时账号记录
              </div>
            )}
            <button onClick={async () => {
              if (loading || backfilling) return;
              if (!window.confirm(`确认回填 ${startDate} ~ ${endDate} 范围的历史用户审计数据？\n\n- 从 daily_log 提取历史提问 → user_ops (query)\n- 从 daily_log 提取历史 LLM 调用 → user_llm (token)\n\n此操作幂等, 可重复执行`)) return;
              setBackfilling(true);
              try {
                const token = localStorage.getItem('zops_token');
                const res = await fetch(`/api/audit/backfill?start_date=${startDate}&end_date=${endDate}`, {
                  method: 'POST', headers: { 'Authorization': `Bearer ${token || ''}` }
                });
                if (res.ok) {
                  const d = await res.json();
                  setBackfillResult(d);
                  fetchLogs();  // 刷新统计
                } else {
                  setBackfillResult({ error: `HTTP ${res.status}` });
                }
              } catch (e) { setBackfillResult({ error: String(e) }); }
              setBackfilling(false);
            }} disabled={loading || backfilling}
              className="flex items-center gap-1 text-[11px] px-2.5 py-1 bg-white border border-[#D1D5DB] rounded-full text-[#374151] hover:bg-gray-50 hover:border-[#9CA3AF] transition-colors disabled:opacity-50"
              title="从 daily_log 历史日志回填 user_ops(query) 与 user_llm(token)">
              {backfilling ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
              回填历史数据
            </button>
            {backfillResult && !backfillResult.error && (
              <div className="text-[11px] px-2.5 py-1 bg-emerald-50 text-[#16A34A] border border-emerald-200 rounded-full font-medium">
                ✓ 扫描 {fmtNum(backfillResult.scanned)} 条 → 新增 {fmtNum(backfillResult.queries_added)} 提问 / {fmtNum(backfillResult.llm_added)} LLM 调用 (跳过 {fmtNum(backfillResult.skipped_duplicates)} 重复)
              </div>
            )}
            {backfillResult?.error && (
              <div className="text-[11px] px-2.5 py-1 bg-red-50 text-[#DC2626] border border-red-200 rounded-full font-medium">
                回填失败: {backfillResult.error}
              </div>
            )}
          </div>
          <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-[#F9FAFB] text-[#6B7280]">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">用户</th>
                    <th className="px-3 py-2 text-left font-medium">显示名 / 组别</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('logins')}>真实登录{sortArrow('logins')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('queries')}>提问次数{sortArrow('queries')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('days_active')}>活跃天数{sortArrow('days_active')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('llm_calls')}>LLM 调用{sortArrow('llm_calls')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('input_tokens')}>输入 Token{sortArrow('input_tokens')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('output_tokens')}>输出 Token{sortArrow('output_tokens')}</th>
                    <th className="px-3 py-2 text-right font-medium cursor-pointer hover:text-[#7C3AED]" onClick={() => sortBy('total_tokens')}>总 Token{sortArrow('total_tokens')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F3F4F6]">
                  {sortedSummary.map(r => {
                    const profile = userProfiles[r.username];
                    const displayName = profile?.display_name || r.username;
                    const isAdmin = profile?.role === 'admin';
                    const group = profile?.group;
                    return (
                    <tr key={r.username} className="hover:bg-[#F9FAFB]">
                      <td className="px-3 py-2">
                        <div className="font-medium text-[#111827]">
                          {displayName}
                          {displayName !== r.username && (
                            <span className="ml-1 text-[10px] text-[#9CA3AF] font-mono">@{r.username}</span>
                          )}
                        </div>
                        {!profile && (
                          <div className="text-[10px] text-[#F59E0B] mt-0.5">⚠ 用户档案不存在 (已删除?)</div>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1 flex-wrap">
                          {isAdmin && (
                            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-violet-50 text-[#7C3AED] border border-violet-100">管理员</span>
                          )}
                          {group && (
                            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-blue-50 text-[#2563EB] border border-blue-100">组 {group}</span>
                          )}
                          {!isAdmin && !group && (
                            <span className="text-[10px] text-[#9CA3AF]">-</span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-[#2563EB]">{fmtNum(r.logins)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#0D9488]">{fmtNum(r.queries)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#6B7280]">{fmtNum(r.days_active)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#EA580C]">{fmtNum(r.llm_calls)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#374151]">{fmtNum(r.input_tokens)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#374151]">{fmtNum(r.output_tokens)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[#111827] font-semibold">{fmtNum(r.total_tokens)}</td>
                    </tr>
                  );
                  })}
                  {sortedSummary.length === 0 && (
                    <tr><td colSpan={8} className="px-3 py-8 text-center text-[#9CA3AF]">{loading ? '加载中...' : '该时间范围内无用户审计数据'}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {/* 明细视图 (Agent 操作 / 用户审计) */}
      {view !== 'summary' && (
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden ">
        <div className="divide-y divide-[#F3F4F6] max-h-[60vh] overflow-y-auto">
          {filteredLogs.map((log, i) => (
            <div key={`${view}-${i}-${log.timestamp || 0}`} className="px-4 py-3 hover:bg-[#F9FAFB] transition-colors">
              {view === 'agent' ? (
                <>
                  <div className="flex items-center gap-2 text-xs">
                    <span className="font-semibold text-[#111827]">{log.agent_id}</span>
                    {log.username && (
                      <span className="inline-flex items-center gap-1.5">
                        <span className="text-[#374151]">{(userProfiles[log.username]?.display_name) || log.username}</span>
                        {(userProfiles[log.username]?.display_name) && (
                          <span className="text-[10px] text-[#9CA3AF] font-mono">@{log.username}</span>
                        )}
                        {userProfiles[log.username]?.role === 'admin' && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-violet-50 text-[#7C3AED] border border-violet-100">管理员</span>
                        )}
                        {userProfiles[log.username]?.group && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-blue-50 text-[#2563EB] border border-blue-100">组 {userProfiles[log.username].group}</span>
                        )}
                      </span>
                    )}
                    <span className="text-[#9CA3AF]">{formatTime(log.timestamp)}</span>
                    <span className={`px-1.5 py-0.5 rounded-full text-[10px] font-medium ${log.success ? 'bg-green-50 text-[#16A34A] border border-green-100' : 'bg-[#FEF2F2] text-[#DC2626] border border-[#FECACA]'}`}>{log.success ? '成功' : '失败'}</span>
                  </div>
                  <div className="text-sm text-[#4B5563] mt-1">{log.action || log.detail || '-'}</div>
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2 text-xs flex-wrap">
                    {log.username ? (
                      <span className="inline-flex items-center gap-1.5">
                        <span className="font-semibold text-[#111827]">{(userProfiles[log.username]?.display_name) || log.username}</span>
                        {(userProfiles[log.username]?.display_name) && (
                          <span className="text-[10px] text-[#9CA3AF] font-mono">@{log.username}</span>
                        )}
                        {!userProfiles[log.username] && (
                          <span className="text-[10px] text-[#F59E0B] font-mono">@未注册</span>
                        )}
                        {userProfiles[log.username]?.role === 'admin' && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-violet-50 text-[#7C3AED] border border-violet-100">管理员</span>
                        )}
                        {userProfiles[log.username]?.group && (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-blue-50 text-[#2563EB] border border-blue-100">组 {userProfiles[log.username].group}</span>
                        )}
                      </span>
                    ) : <span className="font-semibold text-[#9CA3AF]">-</span>}
                    <span className="text-[#9CA3AF]">{formatTime(log.timestamp)}</span>
                    {(() => { const m = actionMeta(log.action); return (<span className={`px-1.5 py-0.5 rounded-full text-[10px] font-medium border ${m.tone}`}>{m.label}</span>); })()}
                    {log.ip && <span className="text-[#9CA3AF] font-mono text-[10px]">IP {log.ip}</span>}
                    {log.session_id && <span className="text-[#9CA3AF] font-mono text-[10px]">会话 {log.session_id}</span>}
                  </div>
                  <div className="text-sm text-[#4B5563] mt-1 break-all">{log.detail || '-'}</div>
                </>
              )}
            </div>
          ))}
          {filteredLogs.length === 0 && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">{logs.length === 0 ? '暂无审计记录' : '无匹配记录'}</div>}
        </div>
      </div>
      )}
    </div>
  );
}

// LLM 配置 (保留原有完整逻辑)
function LLMConfig() {
  const [cfg, setCfg] = useState<any>({});
  const [providers, setProviders] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showAddProvider, setShowAddProvider] = useState(false);
  const [newProvider, setNewProvider] = useState({ id: '', name: '', base_url: '', models: '' });
  const [addingProvider, setAddingProvider] = useState(false);
  const [editingProvider, setEditingProvider] = useState('');   // 编辑中的自定义 provider id (空=新建)
  const [modelsMsg, setModelsMsg] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [showKey, setShowKey] = useState<Record<string, boolean>>({});
  // 各 provider 已存凭据 (GET provider_creds): 切换时恢复 URL/记住的模型; Key 不回显只标状态
  const [credsMap, setCredsMap] = useState<Record<string, any>>({});
  const [hasOneapiKey, setHasOneapiKey] = useState(false);
  // 网关验证过的真实模型名 (请求名 → 网关返回名): 该是什么模型就显示什么模型
  const [modelNames, setModelNames] = useState<Record<string, string>>({});
  const [fetchedModel, setFetchedModel] = useState('');
  const [fetchedReal, setFetchedReal] = useState('');

  const fetchConfig = async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem('zops_token');
      const response = await fetch('/api/llm/config', { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (response.ok) {
        const data = await response.json();
        setCfg({ provider: data.provider, model: data.model, real_model: data.real_model || '', api_key: '', base_url: data.base_url || '', oneapi_key: '', oneapi_base_url: data.oneapi_base_url || '', sanitize_enabled: data.sanitize_enabled ?? true });
        setProviders(data.providers || []);
        setHasOneapiKey(!!data.has_oneapi_key);
        setModelNames(data.model_names || {});
        setFetchedModel(data.model || '');
        setFetchedReal(data.real_model || '');
        const creds: Record<string, any> = {};
        for (const [pid, slot] of Object.entries(data.provider_creds || {})) {
          const s = slot as any;
          creds[pid] = { base_url: s.base_url || '', model: s.model || '', hasKey: !!s.has_api_key };
        }
        creds[data.provider] = creds[data.provider] || { base_url: data.base_url || '', model: data.model || '', hasKey: !!data.has_api_key };
        setCredsMap(creds);
      }
    } catch (error) { console.error('Failed to fetch LLM config:', error); }
    setLoading(false);
  };

  // 切换 provider: 暂存当前输入, 恢复目标 provider 上次的 URL/模型; Key 永不回显 (留空=用已存 Key)
  const switchProvider = (p: any) => {
    if (!cfg.provider || p.id === cfg.provider) return;
    const stashed = { ...(credsMap[cfg.provider] || {}), base_url: cfg.base_url || '', model: cfg.model || '' };
    const mem = credsMap[p.id] || {};
    setCredsMap({ ...credsMap, [cfg.provider]: stashed });
    setCfg({ ...cfg, provider: p.id, base_url: mem.base_url || '', api_key: '', model: mem.model || (p.models || [])[0] || '' });
  };

  // 显式清除已保存的 Key (空输入 = 保留, 清除必须走 clear_keys)
  const clearSavedKey = async () => {
    const field = cfg.provider === 'oneapi' ? 'oneapi_key' : 'api_key';
    if (!confirm(`确定清除已保存的${cfg.provider === 'oneapi' ? ' OneAPI' : ''} API Key？清除后调用将失败，需重新输入。`)) return;
    try {
      const token = localStorage.getItem('zops_token');
      const resp = await fetch('/api/llm/config', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify({ provider: cfg.provider, model: cfg.model, clear_keys: [field] }) });
      if (resp.ok) fetchConfig(); else alert('清除失败');
    } catch { alert('清除失败'); }
  };
  const keyConfigured = cfg.provider === 'oneapi' ? hasOneapiKey : !!(credsMap[cfg.provider]?.hasKey);
  const saveConfig = async () => {
    setSaving(true);
    try {
      const token = localStorage.getItem('zops_token');
      const resp = await fetch('/api/llm/config', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify(cfg) });
      if (resp.ok) alert('配置保存成功'); else alert('保存失败: ' + (await resp.json()).error);
    } catch (error) { alert('保存失败'); }
    setSaving(false);
  };
  const testModel = async () => {
    setTesting(true); setTestResult(null);
    try {
      const token = localStorage.getItem('zops_token');
      const resp = await fetch('/api/llm/test', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` }, body: JSON.stringify(cfg) });
      const data = await resp.json(); setTestResult(data);
    } catch (e) { setTestResult({ status: 'error', error: '网络错误' }); }
    setTesting(false);
  };
  const refreshModels = async () => {
    if (!cfg.provider) return;
    setRefreshingModels(true); setModelsMsg('');
    try {
      const token = localStorage.getItem('zops_token');
      const resp = await fetch(`/api/llm/models/${cfg.provider}`, { headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (resp.ok) {
        const data = await resp.json();
        setProviders(prev => prev.map(p => p.id === cfg.provider ? { ...p, models: data.models || [] } : p));
        const n = (data.models || []).length;
        setModelsMsg(data.source === 'gateway' ? `✓ 已从网关拉取 ${n} 个可用模型` : `仅预设 ${n} 个 (网关拉取需先保存该 Provider 的 Key)`);
        setTimeout(() => setModelsMsg(''), 4000);
      }
    } catch (e) { console.error('Refresh models failed:', e); }
    setRefreshingModels(false);
  };
  const startEditProvider = (p: any) => {
    setEditingProvider(p.id);
    setNewProvider({ id: p.id, name: p.name || '', base_url: p.base_url || '', models: (p.models || []).join(', ') });
    setShowAddProvider(true);
  };
  const saveProvider = async () => {
    if (!newProvider.id || !newProvider.name) { alert('ID 和名称必填'); return; }
    setAddingProvider(true);
    try {
      const token = localStorage.getItem('zops_token');
      const models = newProvider.models ? newProvider.models.split(',').map(s => s.trim()).filter(Boolean) : [];
      const isEdit = !!editingProvider;
      const resp = await fetch(isEdit ? `/api/llm/providers/${encodeURIComponent(editingProvider)}` : '/api/llm/providers', {
        method: isEdit ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token || ''}` },
        body: JSON.stringify({ ...newProvider, models }),
      });
      if (resp.ok) {
        alert(isEdit ? 'Provider 已更新' : '添加成功');
        setShowAddProvider(false); setEditingProvider(''); setNewProvider({ id: '', name: '', base_url: '', models: '' }); fetchConfig();
      }
      else alert((isEdit ? '更新失败: ' : '添加失败: ') + (await resp.json()).error);
    } catch (error) { alert('保存失败'); }
    setAddingProvider(false);
  };
  const deleteProvider = async (pid: string) => {
    if (!confirm(`确定删除 Provider "${pid}"？`)) return;
    try {
      const token = localStorage.getItem('zops_token');
      const resp = await fetch(`/api/llm/providers/${pid}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token || ''}` } });
      if (resp.ok) fetchConfig(); else alert('删除失败: ' + (await resp.json()).error);
    } catch (error) { alert('删除失败'); }
  };
  useEffect(() => { fetchConfig(); }, []);
  const currentProvider = providers.find(p => p.id === cfg.provider);
  const modelSuggestions = currentProvider?.models || [];

  return (
    <div className="max-w-full">
      <div className="flex items-center justify-between mb-5">
        <SectionHeader icon={Brain} title="模型配置" subtitle="配置 LLM 提供商、模型和 API 密钥" color="#4F46E5" />
        <button onClick={fetchConfig} className="text-xs text-[#6B7280] hover:text-[#374151] flex items-center gap-1 px-3 py-1.5 rounded-lg hover:bg-gray-100 transition-colors"><RefreshCw size={12} /> 刷新</button>
      </div>
      {loading ? (
        <div className="text-[#6B7280] text-sm flex items-center gap-2"><RefreshCw className="animate-spin" size={14} /> 加载中...</div>
      ) : (
        <div className="space-y-5">
          <div className="bg-white border border-[#E5E7EB] rounded-xl ">
            <div className="p-4 border-b border-[#F3F4F6]">
              <label className="block text-sm font-semibold text-[#111827] mb-2">LLM 提供商</label>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {providers.map(p => (
                  <button key={p.id} onClick={() => switchProvider(p)}
                    className={`text-left px-3 py-2.5 rounded-lg border text-sm transition-all ${cfg.provider === p.id ? 'border-[#2563EB] bg-[#EFF6FF] ring-1 ring-[#2563EB]/20' : 'border-[#E5E7EB] hover:border-[#D1D5DB] hover:bg-gray-50'}`}>
                    <div className={`font-medium ${cfg.provider === p.id ? 'text-[#1D4ED8]' : 'text-[#111827]'}`}>{p.name}{!p.builtin && ' ✨'}</div>
                    {p.desc && <div className="text-[10px] text-[#9CA3AF] mt-0.5 truncate">{p.desc}</div>}
                    <div className="text-[10px] text-[#9CA3AF] mt-0.5">{(p.models || []).length} 个模型</div>
                  </button>
                ))}
              </div>
            </div>
            <div className="p-4 border-b border-[#F3F4F6]">
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm font-semibold text-[#111827]">模型名称</label>
                <button onClick={refreshModels} disabled={refreshingModels} className="text-[11px] text-[#2563EB] hover:text-[#1D4ED8] flex items-center gap-1"><RefreshCw size={11} className={refreshingModels ? 'animate-spin' : ''} />{refreshingModels ? '加载中...' : '刷新模型列表'}</button>
              </div>
              {modelSuggestions.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {modelSuggestions.map((m: string) => (
                    <button key={m} onClick={() => setCfg({ ...cfg, model: m })} title={modelNames[m] && modelNames[m] !== m ? `实际模型: ${modelNames[m]}` : undefined} className={`px-2 py-1 rounded-md text-xs border transition-all ${cfg.model === m ? 'bg-[#2563EB] text-white border-[#2563EB] font-medium' : 'bg-[#F9FAFB] text-[#4B5563] border-[#E5E7EB] hover:border-blue-300 hover:text-[#2563EB]'}`}>{m}{modelNames[m] && modelNames[m] !== m && <span className="ml-1 opacity-60" title={`实际模型: ${modelNames[m]}`}>ⓘ</span>}</button>
                  ))}
                </div>
              )}
              <input type="text" value={cfg.model || ''} onChange={e => setCfg({ ...cfg, model: e.target.value })} placeholder="输入或点击上方标签选择模型" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent" />
              {(modelNames[cfg.model] || (cfg.model === fetchedModel && fetchedReal)) && ((modelNames[cfg.model] || fetchedReal) !== cfg.model) && (
                <p className="text-[11px] text-[#6B7280] mt-1">实际模型: <span className="font-mono font-medium text-[#111827]">{modelNames[cfg.model] || fetchedReal}</span><span className="text-[#9CA3AF]">（网关验证：「{cfg.model}」实际路由到此模型）</span></p>
              )}
              <p className="text-[11px] text-[#9CA3AF] mt-1">{modelsMsg || '点击标签快速选择；输入任意模型名保存后会自动记住，也可「刷新模型列表」从网关拉取'}</p>
            </div>
            <div className="p-4 border-b border-[#F3F4F6] space-y-3">
              {cfg.provider === 'oneapi' ? (
                <><div><label className="block text-sm font-medium text-[#374151] mb-1">OneAPI 网关地址</label><input type="text" value={cfg.oneapi_base_url || ''} onChange={e => setCfg({ ...cfg, oneapi_base_url: e.target.value })} placeholder="https://aigw-noc.example.com/v1" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /></div>
                <div><label className="block text-sm font-medium text-[#374151] mb-1">OneAPI Key</label><div className="relative"><input type={showKey['oneapi'] ? 'text' : 'password'} value={cfg.oneapi_key || ''} onChange={e => setCfg({ ...cfg, oneapi_key: e.target.value })} placeholder={hasOneapiKey ? '已保存，无需重填（输入新值可覆盖）' : 'sk-xxx'} className="w-full px-3 py-2 pr-10 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /><button type="button" onClick={() => setShowKey(prev => ({ ...prev, oneapi: !prev.oneapi }))} className="absolute right-2 top-1/2 -translate-y-1/2 text-[#9CA3AF] hover:text-[#4B5563]">{showKey['oneapi'] ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>{!cfg.oneapi_key && hasOneapiKey && <p className="text-[11px] text-[#16A34A] mt-1">✓ 已保存 — 留空保存/测试即自动使用，无需重填 <button onClick={clearSavedKey} className="text-[#F87171] hover:underline ml-1">清除</button></p>}</div></>
              ) : (
                <><div><label className="block text-sm font-medium text-[#374151] mb-1">API Base URL</label><input type="text" value={cfg.base_url || ''} onChange={e => setCfg({ ...cfg, base_url: e.target.value })} placeholder={currentProvider?.base_url || 'https://api.openai.com/v1'} className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /><p className="text-[11px] text-[#9CA3AF] mt-1">留空则使用默认地址: {currentProvider?.base_url || '-'}</p></div>
                <div><label className="block text-sm font-medium text-[#374151] mb-1">API Key</label><div className="relative"><input type={showKey['api'] ? 'text' : 'password'} value={cfg.api_key || ''} onChange={e => setCfg({ ...cfg, api_key: e.target.value })} placeholder={keyConfigured ? '已保存，无需重填（输入新值可覆盖）' : 'sk-xxx'} className="w-full px-3 py-2 pr-10 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /><button type="button" onClick={() => setShowKey(prev => ({ ...prev, api: !prev.api }))} className="absolute right-2 top-1/2 -translate-y-1/2 text-[#9CA3AF] hover:text-[#4B5563]">{showKey['api'] ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>{!cfg.api_key && keyConfigured && <p className="text-[11px] text-[#16A34A] mt-1">✓ 已保存 — 留空保存/测试即自动使用，无需重填 <button onClick={clearSavedKey} className="text-[#F87171] hover:underline ml-1">清除</button></p>}</div></>
              )}
            </div>
            <div className="p-4 flex items-center gap-3">
              <button onClick={saveConfig} disabled={saving} className="bg-[#2563EB] hover:bg-[#1D4ED8] disabled:bg-[#E5E7EB] text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5"><Save size={14} />{saving ? '保存中...' : '保存配置'}</button>
              <button onClick={testModel} disabled={testing || !cfg.model} className="border border-[#D1D5DB] hover:border-blue-400 hover:text-[#2563EB] disabled:border-[#E5E7EB] disabled:text-gray-300 text-[#4B5563] px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">{testing ? (<><RefreshCw className="animate-spin" size={14} /> 测试中...</>) : (<>⚡ 测试连接</>)}</button>
            </div>
            {testResult && (<div className={`mx-4 mb-4 p-3 rounded-lg text-sm border ${testResult.status === 'ok' ? 'bg-[#F0FDF4] border-[#BBF7D0] text-[#166534]' : 'bg-[#FEF2F2] border-[#FECACA] text-[#991B1B]'}`}>
              {testResult.status === 'ok' ? (<div><div className="font-medium text-[#166534]">✅ 连接成功</div><div className="mt-1 space-y-0.5 text-xs text-[#16A34A]"><div>模型回复: {testResult.reply}</div><div>实际模型: {testResult.model_returned}</div><div>响应时间: {testResult.elapsed_seconds}s</div>{testResult.usage && <div>Token: {testResult.usage.prompt_tokens || testResult.usage.input_tokens || 0} → {testResult.usage.completion_tokens || testResult.usage.output_tokens || 0}</div>}</div></div>)
              : (<div><div className="font-medium text-[#991B1B]">❌ 连接失败</div><div className="mt-1 text-xs text-[#DC2626]">{testResult.error}{testResult.status_code && <span className="ml-1">(HTTP {testResult.status_code})</span>}{testResult.elapsed_seconds != null && <span className="ml-1">({testResult.elapsed_seconds}s)</span>}</div></div>)}
            </div>)}
          </div>
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 ">
            <div className="flex items-center justify-between">
              <div><h4 className="text-sm font-medium text-[#111827]">数据脱敏</h4><p className="text-[11px] text-[#9CA3AF] mt-0.5">发送到 LLM 前自动脱敏敏感信息 (IP/GID 等)</p></div>
              <button onClick={() => setCfg({ ...cfg, sanitize_enabled: !cfg.sanitize_enabled })} className={`relative w-11 h-6 rounded-full transition-colors ${cfg.sanitize_enabled ? 'bg-[#2563EB]' : 'bg-[#D1D5DB]'}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${cfg.sanitize_enabled ? 'translate-x-5' : ''}`} />
              </button>
            </div>
          </div>
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 ">
            <div className="flex items-center justify-between mb-3">
              <div><h4 className="text-sm font-medium text-[#111827]">自定义提供商</h4><p className="text-[11px] text-[#9CA3AF] mt-0.5">添加任何 OpenAI 兼容的 API 提供商；一个网关可挂多个模型</p></div>
              <button onClick={() => { setShowAddProvider(!showAddProvider); if (showAddProvider) { setEditingProvider(''); setNewProvider({ id: '', name: '', base_url: '', models: '' }); } }} className="flex items-center gap-1 text-xs text-[#2563EB] hover:text-blue-800 font-medium">{showAddProvider && editingProvider ? <Pencil size={14} /> : <Plus size={14} />} {showAddProvider && editingProvider ? '编辑中' : '添加'}</button>
            </div>
            {providers.filter(p => !p.builtin).length === 0 ? <p className="text-xs text-[#9CA3AF]">暂无自定义提供商</p> : (
              <div className="space-y-2 mb-3">{providers.filter(p => !p.builtin).map(p => (
                <div key={p.id} className="flex items-center justify-between bg-[#F9FAFB] px-3 py-2.5 rounded-lg text-sm border border-[#F3F4F6]">
              <div><span className="font-medium text-[#111827]">{p.name}</span><span className="text-[#9CA3AF] ml-2 text-xs">({p.id})</span>{p.base_url && <span className="text-[#9CA3AF] ml-2 text-xs">{p.base_url}</span>}<span className="text-[#9CA3AF] ml-2 text-xs">{(p.models || []).length} 模型</span></div>
              <div className="flex items-center gap-3">
                <button onClick={() => startEditProvider(p)} title="编辑 (Base URL / 模型列表)" className="text-[#6B7280] hover:text-[#2563EB] transition-colors"><Pencil size={14} /></button>
                <button onClick={() => deleteProvider(p.id)} className="text-[#F87171] hover:text-[#DC2626] transition-colors"><Trash2 size={14} /></button>
              </div>
                </div>
              ))}</div>
            )}
            {showAddProvider && (
              <div className="border-t border-[#F3F4F6] pt-3 mt-3 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div><label className="block text-xs font-medium text-[#4B5563] mb-1 font-medium">ID (英文, 唯一)</label><input type="text" disabled={!!editingProvider} value={newProvider.id} onChange={e => setNewProvider({ ...newProvider, id: e.target.value })} placeholder="my-provider" className={`w-full px-2.5 py-1.5 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB] ${editingProvider ? 'bg-gray-100 text-[#9CA3AF]' : ''}`} /></div>
                  <div><label className="block text-xs font-medium text-[#4B5563] mb-1 font-medium">显示名称</label><input type="text" value={newProvider.name} onChange={e => setNewProvider({ ...newProvider, name: e.target.value })} placeholder="My LLM Provider" className="w-full px-2.5 py-1.5 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /></div>
                </div>
                <div><label className="block text-xs font-medium text-[#4B5563] mb-1 font-medium">API Base URL</label><input type="text" value={newProvider.base_url} onChange={e => setNewProvider({ ...newProvider, base_url: e.target.value })} placeholder="https://api.example.com/v1" className="w-full px-2.5 py-1.5 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /></div>
                <div><label className="block text-xs font-medium text-[#4B5563] mb-1 font-medium">预设模型 (逗号分隔, 可留空; 也可之后「刷新模型列表」从网关拉取)</label><input type="text" value={newProvider.models} onChange={e => setNewProvider({ ...newProvider, models: e.target.value })} placeholder="model-a, model-b" className="w-full px-2.5 py-1.5 border border-[#D1D5DB] rounded-lg text-sm text-[#111827] bg-white placeholder:text-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#2563EB]" /></div>
                <div className="flex gap-2">
                  <button onClick={saveProvider} disabled={addingProvider} className="bg-[#16A34A] hover:bg-[#15803D] disabled:bg-[#E5E7EB] text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-colors">{addingProvider ? '保存中...' : editingProvider ? '保存修改' : '确认添加'}</button>
                  <button onClick={() => { setShowAddProvider(false); setEditingProvider(''); setNewProvider({ id: '', name: '', base_url: '', models: '' }); }} className="text-[#6B7280] hover:text-[#374151] px-3 py-1.5 rounded-lg text-sm border border-[#D1D5DB] transition-colors">取消</button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ═══ Agent 模型配置 ═══
function AgentModels() {
  const [models, setModels] = useState<any[]>([]);
  const [globalRealModel, setGlobalRealModel] = useState('');
  useEffect(() => {
    (async () => {
      try {
        const token = localStorage.getItem('zops_token');
        const res = await fetch('/api/agents/detail', { headers: { 'Authorization': `Bearer ${token}` } });
        if (res.ok) {
          const d = await res.json();
          setModels(Object.values(d.agents || {}));
          setGlobalRealModel(d.global_real_model || d.global_model || '');
        }
      } catch (e) { console.error(e); }
    })();
  }, []);
  const sourceLabel = (s: string) => s === 'override' ? '自定义' : s === 'yaml' ? 'YAML' : '全局';
  const sourceCls = (s: string) => s === 'override' ? 'bg-blue-50 text-[#2563EB] border border-blue-100' : s === 'yaml' ? 'bg-purple-50 text-[#7C3AED] border border-purple-100' : 'bg-gray-100 text-[#6B7280]';
  return (
    <div className="max-w-4xl">
      <SectionHeader icon={Bot} title="Agent 模型配置" subtitle={`各 Agent 使用的 LLM 模型${globalRealModel ? `（全局实际模型: ${globalRealModel}）` : ''}`} color="#0891B2" />
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden ">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] text-sm font-semibold text-[#374151] flex items-center gap-2">
          <Bot size={14} className="text-[#9CA3AF]" /> 模型分配 <span className="ml-auto text-xs text-[#9CA3AF]">{models.length} 个</span>
        </div>
        <div className="divide-y divide-[#F3F4F6]">
          {models.map((m: any, i: number) => (
            <div key={m.id || i} className="px-4 py-3.5 flex items-center justify-between hover:bg-[#F9FAFB] transition-colors">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-lg bg-[#EFF6FF] flex items-center justify-center"><Bot size={14} className="text-[#2563EB]" /></div>
                <div><div className="font-medium text-[#111827] text-sm">{m.name || m.id || `Agent ${i}`}</div><div className="text-xs text-[#374151] font-mono">{m.real_model || m.model || '默认'}{m.real_model && m.real_model !== m.model && <span className="text-[#9CA3AF]">（别名 {m.model}）</span>}</div></div>
              </div>
              <span className={`text-xs px-2 py-1 rounded-full ${sourceCls(m.model_source)}`}>{sourceLabel(m.model_source)}</span>
            </div>
          ))}
          {models.length === 0 && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">暂无配置</div>}
        </div>
      </div>
    </div>
  );
}

// ═══ 通知配置 + 通知用户组 ═══
interface NotifyMember { username: string; display_name?: string; mail?: string; source?: string }
interface NotifyGroup { name: string; description?: string; members: NotifyMember[]; created_at?: string; updated_at?: string; updated_by?: string }

function NotifyConfig() {
  const [cfg, setCfg] = useState<any>(null);
  const [groups, setGroups] = useState<NotifyGroup[]>([]);
  const [expanded, setExpanded] = useState<string>('');
  const [editing, setEditing] = useState<NotifyGroup | null>(null);   // 正在编辑/新建的组
  const [q, setQ] = useState('');                                     // 成员搜索关键词
  const [results, setResults] = useState<NotifyMember[]>([]);
  const [searching, setSearching] = useState(false);
  const [msg, setMsg] = useState('');
  // NMS/飞书动态配置（前端可控, /api/nms/config）
  const [nmsCfg, setNmsCfg] = useState<any>(null);
  const [nmsForm, setNmsForm] = useState<any>(null);
  const [nmsIntervalMin, setNmsIntervalMin] = useState('');  // 采集间隔（分钟，提交时换算为秒）
  const [nmsSaving, setNmsSaving] = useState(false);
  const [nmsTesting, setNmsTesting] = useState(false);
  // NMS 飞书多源推送通道
  const [nmsChannels, setNmsChannels] = useState<any[]>([]);
  const [nmsChats, setNmsChats] = useState<any[]>([]);
  const [nmsGroupOptions, setNmsGroupOptions] = useState<string[]>([]);
  const [nmsSevOptions, setNmsSevOptions] = useState<any[]>([]);
  const [chEditing, setChEditing] = useState<any>(null);   // 编辑/新建通道
  const [chSaving, setChSaving] = useState(false);
  const [chTesting, setChTesting] = useState('');
  const [chGroupInput, setChGroupInput] = useState('');    // 分组手输补录

  const auth = () => ({ 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` });
  const toast = (s: string) => { setMsg(s); setTimeout(() => setMsg(''), 3000); };

  const fetchCfg = async () => { try { const r = await fetch('/api/notify/config', { headers: auth() }); if (r.ok) setCfg(await r.json()); } catch {} };
  const fetchGroups = async () => { try { const r = await fetch('/api/notify/groups', { headers: auth() }); if (r.ok) { const d = await r.json(); setGroups(d.groups || []); } } catch {} };
  const fetchNmsCfg = async () => {
    try {
      const r = await fetch('/api/nms/config', { headers: auth() });
      if (r.ok) {
        const d = await r.json();
        setNmsCfg(d);
        setNmsForm((prev: any) => prev || {
          feishu_webhook_url: d.effective?.feishu_webhook_url || '',
          feishu_app_id: d.effective?.feishu_app_id || '',
          feishu_app_secret: '',
          feishu_verification_token: d.saved?.feishu_verification_token || '',
          push_enabled: d.effective?.push_enabled,
          push_only_on_new: d.effective?.push_only_on_new,
          snapshot_groups: d.effective?.snapshot_groups || '',
          snapshot_severities: (d.effective?.snapshot_severities || '').split(',').filter(Boolean),
          snapshot_unack_only: d.effective?.snapshot_unack_only ?? true,
          push_groups: d.effective?.push_groups || '',
          check_interval: d.effective?.check_interval || 600,
        });
        setNmsIntervalMin(prev => prev || String(Math.round((d.effective?.check_interval || 600) / 60)));
      }
    } catch {}
  };
  // 采集压力估算：每轮约 25 万行，15 分钟 ≈ 50% Netcool 查询占用
  const nmsIntervalMins = Number(nmsIntervalMin) || 0;
  const nmsLoadPct = nmsIntervalMins > 0 ? Math.round(750 / nmsIntervalMins) : 0;
  useEffect(() => { fetchCfg(); fetchGroups(); fetchNmsCfg(); fetchNmsChannels(); }, []);

  const fetchNmsChannels = async () => {
    try {
      const r = await fetch('/api/nms/channels', { headers: auth() });
      if (r.ok) {
        const d = await r.json();
        setNmsChannels(d.channels || []);
        setNmsChats(d.chats || []);
        setNmsGroupOptions(d.group_options || []);
        setNmsSevOptions(d.severity_options || []);
      }
    } catch {}
  };

  const startNewChannel = () => setChEditing({ id: '', name: '', type: 'webhook', webhook_url: '', chat_id: '', groups: [], severities: [], enabled: true, is_default: false });

  const toggleChGroup = (g: string) => {
    if (!chEditing) return;
    const cur = new Set(chEditing.groups || []);
    if (cur.has(g)) cur.delete(g); else cur.add(g);
    setChEditing({ ...chEditing, groups: Array.from(cur) });
  };

  const addChGroupInput = () => {
    if (!chEditing) return;
    const adds = chGroupInput.split(',').map((g: string) => g.trim()).filter(Boolean);
    if (!adds.length) return;
    const cur = new Set(chEditing.groups || []);
    adds.forEach((g: string) => cur.add(g));
    setChEditing({ ...chEditing, groups: Array.from(cur) });
    setChGroupInput('');
  };

  const saveChannel = async () => {
    if (!chEditing) return;
    if (!chEditing.name.trim()) { toast('请填写通道名称'); return; }
    setChSaving(true);
    try {
      const body = {
        id: chEditing.id || '', name: chEditing.name, type: chEditing.type,
        webhook_url: chEditing.webhook_url || '', chat_id: chEditing.chat_id || '',
        groups: chEditing.groups || [], severities: chEditing.severities || [],
        enabled: chEditing.enabled ?? true, is_default: chEditing.is_default ?? false,
      };
      const r = await fetch('/api/nms/channels', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify(body) });
      if (r.ok) { toast('通道已保存，下轮推送即生效'); setChEditing(null); fetchNmsChannels(); }
      else { const d = await r.json().catch(() => ({})); toast(d.detail || '保存失败'); }
    } catch { toast('保存失败'); }
    setChSaving(false);
  };

  const removeChannel = async (id: string, name: string) => {
    if (!confirm(`确定删除通道「${name}」？`)) return;
    try {
      const r = await fetch(`/api/nms/channels/${encodeURIComponent(id)}`, { method: 'DELETE', headers: auth() });
      if (r.ok) { toast('通道已删除'); fetchNmsChannels(); } else toast('删除失败');
    } catch { toast('删除失败'); }
  };

  const testChannel = async (id: string) => {
    setChTesting(id);
    try {
      const r = await fetch('/api/nms/channels/test', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify({ id }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) toast(d.message || '测试已发送'); else toast(d.detail || '测试失败');
    } catch { toast('测试失败'); }
    setChTesting('');
  };

  const saveNms = async () => {
    if (!nmsForm) return;
    setNmsSaving(true);
    try {
      const body: any = { ...nmsForm };
      body.snapshot_severities = (nmsForm.snapshot_severities || []).join(',');
      const mins = Number(nmsIntervalMin);
      if (Number.isFinite(mins) && mins > 0) body.check_interval = Math.max(60, Math.round(mins * 60));
      const r = await fetch('/api/nms/config', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify(body) });
      if (r.ok) { toast('NMS/飞书配置已保存，采集间隔热生效（无需重启）'); fetchNmsCfg(); setNmsForm((f: any) => ({ ...f, feishu_app_secret: '' })); }
      else { const d = await r.json().catch(() => ({})); toast(d.detail || '保存失败'); }
    } catch { toast('保存失败'); }
    setNmsSaving(false);
  };

  const testFeishu = async () => {
    setNmsTesting(true);
    try {
      const r = await fetch('/api/nms/config/test-feishu', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify({}) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) toast(d.message || '测试消息已发送');
      else toast(d.detail || '测试失败');
    } catch { toast('测试失败'); }
    setNmsTesting(false);
  };

  const searchUsers = async () => {
    setSearching(true);
    try {
      const r = await fetch(`/api/notify/ldap/users?keyword=${encodeURIComponent(q)}&limit=15`, { headers: auth() });
      if (r.ok) { const d = await r.json(); setResults(d.users || []); if (!d.users?.length) toast('未搜索到匹配用户'); }
      else toast('搜索失败');
    } catch { toast('搜索失败'); }
    setSearching(false);
  };

  const startNew = () => { setEditing({ name: '', description: '', members: [] }); setQ(''); setResults([]); setExpanded(''); };
  const startEdit = (g: NotifyGroup) => { setEditing({ ...g, members: [...(g.members || [])] }); setQ(''); setResults([]); setExpanded(''); };

  const addMember = (u: NotifyMember) => {
    if (!editing) return;
    if (editing.members.some(m => m.username === u.username)) { toast('该成员已在组内'); return; }
    setEditing({ ...editing, members: [...editing.members, u] });
  };
  const removeMember = (uname: string) => { if (!editing) return; setEditing({ ...editing, members: editing.members.filter(m => m.username !== uname) }); };

  const save = async () => {
    if (!editing || !editing.name.trim()) { toast('请填写用户组名称'); return; }
    try {
      const r = await fetch('/api/notify/groups', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify(editing) });
      if (r.ok) { toast(`用户组 '${editing.name}' 已保存`); setEditing(null); fetchGroups(); }
      else { const d = await r.json().catch(() => ({})); toast(d.detail || '保存失败'); }
    } catch { toast('保存失败'); }
  };

  const del = async (name: string) => {
    if (!confirm(`确定删除用户组 '${name}'？`)) return;
    try {
      const r = await fetch(`/api/notify/groups/${encodeURIComponent(name)}`, { method: 'DELETE', headers: auth() });
      if (r.ok) { toast(`用户组 '${name}' 已删除`); fetchGroups(); } else toast('删除失败');
    } catch { toast('删除失败'); }
  };

  const email = cfg?.email || {};
  const feishu = cfg?.feishu || {};

  return (
    <div className="max-w-full">
      <div className="flex items-start justify-between">
        <SectionHeader icon={Bell} title="通知配置" subtitle="通知渠道状态与通知人（用户组）管理" color="#EA580C" />
        <Link href="/notify" className="text-xs text-[#2563EB] hover:text-[#1D4ED8] flex items-center gap-1 px-3 py-1.5 rounded-lg hover:bg-gray-100 transition-colors mt-1"><Send size={12} /> 通知测试 →</Link>
      </div>
      {msg && <div className="fixed top-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg border bg-white text-sm font-medium text-[#111827]">{msg}</div>}

      {/* 渠道状态 */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-4">
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-semibold text-[#111827] flex items-center gap-2"><Mail size={15} className="text-[#2563EB]" /> 邮件通知</h4>
            <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${email.enabled ? 'bg-green-50 text-[#16A34A] border border-green-100' : 'bg-gray-100 text-[#6B7280]'}`}>{email.enabled ? '已启用' : '未启用'}</span>
          </div>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-[#6B7280]">SMTP 服务器</span><span className="font-mono text-xs text-[#111827]">{email.smtp_host || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">端口</span><span className="font-mono text-xs text-[#111827]">{email.smtp_port || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">发件人</span><span className="font-mono text-xs text-[#111827]">{email.smtp_from || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">管理员邮箱</span><span className="font-mono text-xs text-[#111827]">{email.admin_email || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">MX 直发</span><span className="text-xs text-[#111827]">{email.mx_direct ? '是' : '否'}</span></div>
          </div>
        </div>
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-semibold text-[#111827] flex items-center gap-2"><MessageSquare size={15} className="text-[#16A34A]" /> 飞书通知</h4>
            <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${nmsCfg?.feishu_enabled ? 'bg-green-50 text-[#16A34A] border border-green-100' : 'bg-gray-100 text-[#6B7280]'}`}>{nmsCfg?.feishu_enabled ? '已启用' : '未启用'}</span>
          </div>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">Webhook URL</label>
              <input value={nmsForm?.feishu_webhook_url || ''} onChange={e => setNmsForm({ ...nmsForm, feishu_webhook_url: e.target.value })} placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">App ID</label>
                <input value={nmsForm?.feishu_app_id || ''} onChange={e => setNmsForm({ ...nmsForm, feishu_app_id: e.target.value })} placeholder="cli_xxx" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
              </div>
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">App Secret {nmsCfg?.saved?.feishu_app_secret_set ? <span className="text-[#16A34A]">(已配置)</span> : <span className="text-[#9CA3AF]">(未配置)</span>}</label>
                <input type="password" value={nmsForm?.feishu_app_secret || ''} onChange={e => setNmsForm({ ...nmsForm, feishu_app_secret: e.target.value })} placeholder={nmsCfg?.saved?.feishu_app_secret_set ? '留空保持原值' : 'App Secret'} className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
              </div>
            </div>
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">Verification Token</label>
              <input value={nmsForm?.feishu_verification_token || ''} onChange={e => setNmsForm({ ...nmsForm, feishu_verification_token: e.target.value })} placeholder="事件订阅校验 token（可选）" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button onClick={saveNms} disabled={nmsSaving} className="flex items-center gap-1.5 text-xs font-medium bg-[#16A34A] hover:bg-[#15803D] text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50">{nmsSaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存飞书配置</button>
              <button onClick={testFeishu} disabled={nmsTesting} className="flex items-center gap-1.5 text-xs font-medium bg-white border border-[#D1D5DB] hover:border-[#16A34A] text-[#374151] px-4 py-2 rounded-lg transition-colors disabled:opacity-50">{nmsTesting ? <Loader2 size={13} className="animate-spin" /> : <MessageSquare size={13} />} 测试推送</button>
              <span className="ml-auto text-[11px] text-[#9CA3AF]">事件回调端口 {nmsCfg?.event_port || '-'}</span>
            </div>
            <p className="text-[11px] text-[#9CA3AF] leading-relaxed">Webhook 用于自定义机器人告警推送；App ID/Secret 用于应用机器人（群内 @机器人 交互命令）。配置保存后 30 秒内生效，无需重启服务。</p>
          </div>
        </div>
      </div>

      {/* NMS 告警通知/截图配置 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 mb-4">
        <div className="flex items-center justify-between mb-4">
          <h4 className="text-sm font-semibold text-[#111827] flex items-center gap-2"><Activity size={15} className="text-[#EA580C]" /> NMS 告警通知与截图配置</h4>
          <span className="text-[11px] text-[#9CA3AF]">采集间隔热生效；其余配置 30 秒内生效</span>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* 推送设置 */}
          <div className="space-y-3">
            <label className="flex items-center gap-2 text-sm text-[#374151] cursor-pointer">
              <input type="checkbox" checked={!!nmsForm?.push_enabled} onChange={e => setNmsForm({ ...nmsForm, push_enabled: e.target.checked })} className="w-4 h-4 rounded accent-[#EA580C]" />
              启用飞书告警推送
            </label>
            <label className="flex items-center gap-2 text-sm text-[#374151] cursor-pointer">
              <input type="checkbox" checked={!!nmsForm?.push_only_on_new} onChange={e => setNmsForm({ ...nmsForm, push_only_on_new: e.target.checked })} className="w-4 h-4 rounded accent-[#EA580C]" />
              仅在新告警出现时推送
            </label>
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">采集间隔（分钟，热生效无需重启）</label>
              <div className="flex items-center gap-2 flex-wrap">
                <div className="flex items-center gap-1.5">
                  <input type="number" min={1} step={1} value={nmsIntervalMin} onChange={e => setNmsIntervalMin(e.target.value)} className="w-20 px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#EA580C]/30 focus:border-[#EA580C]" />
                  <span className="text-xs text-[#6B7280]">分钟</span>
                </div>
                <div className="flex items-center gap-1">
                  {[10, 15, 30, 60].map(m => (
                    <button key={m} onClick={() => setNmsIntervalMin(String(m))} className={`text-[11px] px-2 py-1 rounded-md border transition-colors ${nmsIntervalMins === m ? 'bg-[#EA580C] text-white border-[#EA580C]' : 'bg-white text-[#374151] border-[#D1D5DB] hover:border-[#EA580C]'}`}>{m} 分</button>
                  ))}
                </div>
              </div>
              {nmsIntervalMins > 0 && (
                <p className={`text-[11px] mt-1.5 leading-relaxed ${nmsLoadPct >= 60 ? 'text-[#DC2626]' : nmsLoadPct >= 40 ? 'text-[#B45309]' : 'text-[#6B7280]'}`}>
                  每轮约 25 万行；当前间隔 Netcool 查询占用约 <b>{nmsLoadPct}%</b>（15 分钟≈50%）。建议 15 分钟起步，低于 10 分钟压力显著上升。
                </p>
              )}
              <p className="text-[11px] text-[#9CA3AF] mt-1">保存后下一轮即按新间隔调度，进行中的等待 30 秒内感知新配置。</p>
            </div>
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">推送分组（逗号分隔，留空=推送全部分组；只推这些组的告警）</label>
              <input value={nmsForm?.push_groups || ''} onChange={e => setNmsForm({ ...nmsForm, push_groups: e.target.value })} placeholder="GCOC_use" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm font-mono focus:ring-2 focus:ring-[#EA580C]/30 focus:border-[#EA580C]" />
            </div>
          </div>
          {/* 截图设置 */}
          <div className="space-y-3">
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">截图分组（逗号分隔，留空=关闭分组截图）</label>
              <input value={nmsForm?.snapshot_groups || ''} onChange={e => setNmsForm({ ...nmsForm, snapshot_groups: e.target.value })} placeholder="GCOC_use,GCOC_core" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm font-mono focus:ring-2 focus:ring-[#EA580C]/30 focus:border-[#EA580C]" />
            </div>
            <div>
              <label className="text-xs text-[#6B7280] mb-1 block">告警级别筛选（每个级别单独截图；留空=只截整组图）</label>
              <div className="flex flex-wrap gap-3">
                {(nmsCfg?.severity_options || []).map((s: any) => (
                  <label key={s.value} className="flex items-center gap-1.5 text-sm text-[#374151] cursor-pointer">
                    <input type="checkbox" checked={(nmsForm?.snapshot_severities || []).includes(s.value)} onChange={e => { const cur = new Set(nmsForm?.snapshot_severities || []); if (e.target.checked) cur.add(s.value); else cur.delete(s.value); setNmsForm({ ...nmsForm, snapshot_severities: Array.from(cur) }); }} className="w-4 h-4 rounded accent-[#EA580C]" />
                    {s.label}
                  </label>
                ))}
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-[#374151] cursor-pointer">
              <input type="checkbox" checked={nmsForm?.snapshot_unack_only ?? true} onChange={e => setNmsForm({ ...nmsForm, snapshot_unack_only: e.target.checked })} className="w-4 h-4 rounded accent-[#EA580C]" />
              仅截未确认告警（所有截图默认排除已 ACK 确认的告警）
            </label>
          </div>
        </div>
        <div className="flex items-center gap-2 mt-4 pt-3 border-t border-[#F3F4F6]">
          <button onClick={saveNms} disabled={nmsSaving} className="flex items-center gap-1.5 text-xs font-medium bg-[#EA580C] hover:bg-[#C2410C] text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50">{nmsSaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存 NMS 配置</button>
          <span className="text-[11px] text-[#9CA3AF]">飞书群命令: <span className="font-mono">{nmsCfg?.cmd_help || 'nms 状态 / nms 分组 / nms 截图'}</span></span>
        </div>
        {/* 飞书多源推送通道 */}
        <div className="mt-4 pt-4 border-t border-[#F3F4F6]">
          <div className="flex items-center gap-2">
            <h4 className="text-sm font-semibold text-[#111827] flex items-center gap-2"><BellRing size={15} className="text-[#16A34A]" /> 飞书推送通道（多源分发）</h4>
            {!chEditing && <button onClick={startNewChannel} className="ml-auto flex items-center gap-1 text-xs font-medium bg-[#16A34A] hover:bg-[#15803D] text-white px-3 py-1.5 rounded-lg transition-colors"><Plus size={12} /> 新建通道</button>}
          </div>
          <span className="text-[11px] text-[#9CA3AF]">每个通道=一个飞书群接收源（Webhook 群机器人 / 应用机器人群），按分组+级别独立过滤推送；所有常规通道无匹配时推「默认兜底」通道；无通道配置则走上方默认 Webhook</span>
          <div className="mt-2 space-y-2">
            {nmsChannels.map((c: any) => (
              <div key={c.id} className="flex items-center gap-2 bg-gray-50 border border-[#E5E7EB] rounded-lg px-3 py-2">
                <span className="text-xs font-medium text-[#111827] w-28 truncate" title={c.name}>{c.name}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium whitespace-nowrap ${c.type === 'webhook' ? 'bg-orange-50 text-[#EA580C] border border-orange-100' : 'bg-green-50 text-[#16A34A] border border-green-100'}`}>{c.type === 'webhook' ? 'Webhook' : '应用群'}</span>
                <span className="text-xs text-[#6B7280] font-mono flex-1 truncate">
                  {c.type === 'webhook' ? (c.webhook_url || '').replace(/^https?:\/\//, '').slice(0, 40) : (nmsChats.find((x: any) => x.chat_id === c.chat_id)?.name || c.chat_id)}
                </span>
                <span className="text-xs text-[#16A34A] font-mono max-w-[200px] truncate" title={(c.groups || []).join(', ')}>{(c.groups || []).join(', ') || '全部分组'}</span>
                <span className="text-xs text-[#9CA3AF] font-mono max-w-[140px] truncate" title={(c.severities || []).join(', ')}>{(c.severities || []).length ? (c.severities || []).join(',') : '全部级别'}</span>
                {!c.enabled && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-[#6B7280] font-medium whitespace-nowrap">停用</span>}
                {c.is_default && <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-[#2563EB] border border-blue-100 font-medium whitespace-nowrap">默认兜底</span>}
                <button onClick={() => testChannel(c.id)} disabled={chTesting === c.id} className="text-xs text-[#2563EB] hover:underline disabled:opacity-50 whitespace-nowrap">{chTesting === c.id ? '发送中...' : '测试'}</button>
                <button onClick={() => setChEditing({ ...c, groups: [...(c.groups || [])], severities: [...(c.severities || [])] })} className="text-xs text-[#374151] hover:underline">编辑</button>
                <button onClick={() => removeChannel(c.id, c.name)} className="text-xs text-[#DC2626] hover:underline">删除</button>
              </div>
            ))}
            {!nmsChannels.length && <div className="text-xs text-[#9CA3AF]">暂无通道。可新建通道让不同飞书群按分组/级别接收告警，也可在群内发「nms 订阅 分组名」自动创建</div>}
          </div>
          {/* 新建/编辑通道表单 */}
          {chEditing && (
            <div className="mt-3 border border-[#E5E7EB] rounded-lg bg-[#F0FDF4]/40 px-4 py-4 space-y-3">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div>
                  <label className="text-xs text-[#6B7280] mb-1 block">通道名称 *</label>
                  <input value={chEditing.name} onChange={e => setChEditing({ ...chEditing, name: e.target.value })} placeholder="如：GCOC 值班群" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
                </div>
                <div>
                  <label className="text-xs text-[#6B7280] mb-1 block">类型</label>
                  <div className="flex gap-2">
                    {([['webhook', 'Webhook 群机器人'], ['app', '应用机器人群']] as const).map(([v, label]) => (
                      <button key={v} onClick={() => setChEditing({ ...chEditing, type: v })} className={`px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${chEditing.type === v ? 'bg-[#16A34A] text-white border-[#16A34A]' : 'bg-white text-[#374151] border-[#D1D5DB] hover:border-[#16A34A]'}`}>{label}</button>
                    ))}
                  </div>
                </div>
                {chEditing.type === 'webhook' ? (
                  <div>
                    <label className="text-xs text-[#6B7280] mb-1 block">Webhook URL *</label>
                    <input value={chEditing.webhook_url || ''} onChange={e => setChEditing({ ...chEditing, webhook_url: e.target.value })} placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
                  </div>
                ) : (
                  <div>
                    <label className="text-xs text-[#6B7280] mb-1 block">目标群 *</label>
                    {nmsChats.length > 0 ? (
                      <select value={chEditing.chat_id || ''} onChange={e => setChEditing({ ...chEditing, chat_id: e.target.value })} className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]">
                        <option value="">选择机器人所在群...</option>
                        {nmsChats.map((c: any) => (
                          <option key={c.chat_id} value={c.chat_id}>{c.name} ({c.chat_id.slice(0, 12)}...)</option>
                        ))}
                      </select>
                    ) : (
                      <input value={chEditing.chat_id || ''} onChange={e => setChEditing({ ...chEditing, chat_id: e.target.value })} placeholder="chat_id（oc_ 开头，需把机器人拉入群）" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
                    )}
                  </div>
                )}
              </div>
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">订阅分组（点击选择，不选=全部分组）</label>
                <div className="flex flex-wrap gap-1.5">
                  {nmsGroupOptions.map((g: string) => (
                    <button key={g} onClick={() => toggleChGroup(g)} className={`px-2.5 py-1 rounded-full text-xs font-mono border transition-colors ${(chEditing.groups || []).includes(g) ? 'bg-[#16A34A] text-white border-[#16A34A]' : 'bg-white text-[#374151] border-[#E5E7EB] hover:border-[#16A34A]'}`}>{g}</button>
                  ))}
                  {(chEditing.groups || []).filter((g: string) => !nmsGroupOptions.includes(g)).map((g: string) => (
                    <button key={g} onClick={() => toggleChGroup(g)} className="px-2.5 py-1 rounded-full text-xs font-mono border bg-[#16A34A] text-white border-[#16A34A]">{g}</button>
                  ))}
                </div>
                <div className="flex items-center gap-2 mt-1.5">
                  <input value={chGroupInput} onChange={e => setChGroupInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addChGroupInput(); } }} placeholder="手输分组名补录（逗号分隔，回车添加）" className="flex-1 px-3 py-1.5 border border-[#D1D5DB] rounded-lg text-xs font-mono focus:ring-2 focus:ring-[#16A34A]/30 focus:border-[#16A34A]" />
                  <button onClick={addChGroupInput} className="text-xs text-[#16A34A] hover:underline whitespace-nowrap">添加</button>
                </div>
              </div>
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">告警级别过滤（不选=全部级别）</label>
                <div className="flex flex-wrap gap-3">
                  {(nmsSevOptions.length ? nmsSevOptions : (nmsCfg?.severity_options || [])).map((s: any) => (
                    <label key={s.value} className="flex items-center gap-1.5 text-sm text-[#374151] cursor-pointer">
                      <input type="checkbox" checked={(chEditing.severities || []).includes(s.value)} onChange={e => { const cur = new Set(chEditing.severities || []); if (e.target.checked) cur.add(s.value); else cur.delete(s.value); setChEditing({ ...chEditing, severities: Array.from(cur) }); }} className="w-4 h-4 rounded accent-[#16A34A]" />
                      {s.label}
                    </label>
                  ))}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-4">
                <label className="flex items-center gap-2 text-sm text-[#374151] cursor-pointer">
                  <input type="checkbox" checked={chEditing.enabled ?? true} onChange={e => setChEditing({ ...chEditing, enabled: e.target.checked })} className="w-4 h-4 rounded accent-[#16A34A]" />
                  启用通道
                </label>
                <label className="flex items-center gap-2 text-sm text-[#374151] cursor-pointer">
                  <input type="checkbox" checked={!!chEditing.is_default} onChange={e => setChEditing({ ...chEditing, is_default: e.target.checked })} className="w-4 h-4 rounded accent-[#16A34A]" />
                  默认兜底通道（所有常规通道无匹配时推送，全局唯一）
                </label>
                <div className="ml-auto flex items-center gap-2">
                  <button onClick={() => setChEditing(null)} className="text-xs font-medium bg-white border border-[#D1D5DB] hover:border-[#6B7280] text-[#374151] px-4 py-2 rounded-lg transition-colors">取消</button>
                  <button onClick={saveChannel} disabled={chSaving} className="flex items-center gap-1.5 text-xs font-medium bg-[#16A34A] hover:bg-[#15803D] text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50">{chSaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存通道</button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 通知用户组 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] flex items-center gap-2">
          <Users size={14} className="text-[#EA580C]" />
          <span className="text-sm font-semibold text-[#374151]">通知用户组</span>
          <span className="text-xs text-[#9CA3AF]">成员从 LDAP/AD 搜索添加，发送通知时可指定用户组</span>
          <span className="ml-auto text-xs text-[#9CA3AF]">{groups.length} 个组</span>
          {!editing && <button onClick={startNew} className="flex items-center gap-1 text-xs font-medium bg-[#EA580C] hover:bg-[#C2410C] text-white px-3 py-1.5 rounded-lg transition-colors"><Plus size={12} /> 新建用户组</button>}
        </div>

        {/* 编辑/新建表单 */}
        {editing && (
          <div className="border-b border-[#F3F4F6] bg-[#FFF7ED]/50 px-4 py-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">组名称 *</label>
                <input value={editing.name} onChange={e => setEditing({ ...editing, name: e.target.value })} placeholder="如：NMS值班组" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#EA580C]/30 focus:border-[#EA580C]" />
              </div>
              <div>
                <label className="text-xs text-[#6B7280] mb-1 block">描述</label>
                <input value={editing.description || ''} onChange={e => setEditing({ ...editing, description: e.target.value })} placeholder="用途说明（可选）" className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#EA580C]/30 focus:border-[#EA580C]" />
              </div>
            </div>
            {/* LDAP 成员搜索 */}
            <div className="flex items-center gap-2 mb-2">
              <div className="flex items-center gap-2 flex-1 bg-white border border-[#D1D5DB] rounded-lg px-3 py-2 focus-within:border-[#EA580C] focus-within:ring-1 focus-within:ring-[#EA580C]/20">
                <Server size={13} className="text-[#9CA3AF]" />
                <input value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') searchUsers(); }} placeholder="从 LDAP/AD 搜索用户（用户名/姓名/邮箱）..." className="flex-1 text-sm focus:outline-none bg-transparent" />
              </div>
              <button onClick={searchUsers} disabled={searching} className="flex items-center gap-1.5 text-xs font-medium bg-white border border-[#D1D5DB] hover:border-[#EA580C] text-[#374151] px-3 py-2 rounded-lg transition-colors">
                {searching ? <Loader2 size={13} className="animate-spin" /> : <Search size={13} />} 搜索
              </button>
            </div>
            {results.length > 0 && (
              <div className="border border-[#E5E7EB] rounded-lg bg-white max-h-40 overflow-y-auto divide-y divide-[#F3F4F6] mb-3">
                {results.map(u => (
                  <div key={u.username} onClick={() => addMember(u)} className="px-3 py-2 flex items-center gap-3 hover:bg-[#FFF7ED] cursor-pointer transition-colors">
                    <div className="w-7 h-7 rounded-full bg-[#FEE2E2] flex items-center justify-center text-[11px] font-bold text-[#EA580C]">{getInitials(u.display_name || u.username)}</div>
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-medium text-[#111827]">{u.display_name || u.username} <span className="text-[#9CA3AF] font-mono">@{u.username}</span></div>
                      <div className="text-[11px] text-[#6B7280] font-mono">{u.mail || '无邮箱'}</div>
                    </div>
                    <span className="text-[10px] text-[#9CA3AF]">{u.source === 'ldap' ? 'LDAP' : '本地'}</span>
                    <Plus size={13} className="text-[#EA580C]" />
                  </div>
                ))}
              </div>
            )}
            {/* 已选成员 */}
            <div className="flex flex-wrap gap-2 mb-3">
              {editing.members.map(m => (
                <div key={m.username} className="flex items-center gap-1.5 bg-white border border-[#E5E7EB] rounded-full pl-1 pr-2 py-1">
                  <div className="w-5 h-5 rounded-full bg-[#FEE2E2] flex items-center justify-center text-[9px] font-bold text-[#EA580C]">{getInitials(m.display_name || m.username)}</div>
                  <span className="text-xs text-[#374151]">{m.display_name || m.username}</span>
                  {m.mail && <span className="text-[10px] text-[#9CA3AF] font-mono">{m.mail}</span>}
                  <button onClick={() => removeMember(m.username)} className="text-[#9CA3AF] hover:text-red-500 transition-colors"><XCircle size={13} /></button>
                </div>
              ))}
              {editing.members.length === 0 && <span className="text-xs text-[#9CA3AF] py-1">尚未添加成员 — 请在上方搜索并点击添加</span>}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={save} className="flex items-center gap-1.5 text-xs font-medium bg-[#EA580C] hover:bg-[#C2410C] text-white px-4 py-2 rounded-lg transition-colors"><Save size={13} /> 保存用户组</button>
              <button onClick={() => setEditing(null)} className="text-xs text-[#6B7280] hover:text-[#111827] px-3 py-2 transition-colors">取消</button>
              <span className="ml-auto text-[11px] text-[#9CA3AF]">{editing.members.length} 名成员</span>
            </div>
          </div>
        )}

        {/* 组列表 */}
        <div className="divide-y divide-[#F3F4F6]">
          {groups.map(g => (
            <div key={g.name}>
              <div className="px-4 py-3 flex items-center gap-3 hover:bg-[#F9FAFB] transition-colors">
                <div className="w-8 h-8 rounded-lg bg-[#FFEDD5] flex items-center justify-center"><Users size={15} className="text-[#EA580C]" /></div>
                <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setExpanded(expanded === g.name ? '' : g.name)}>
                  <div className="text-sm font-medium text-[#111827]">{g.name} <span className="text-[11px] text-[#9CA3AF] font-normal">{(g.members || []).length} 名成员</span></div>
                  <div className="text-xs text-[#6B7280] truncate">{g.description || '无描述'}</div>
                </div>
                {g.updated_at && <span className="text-[10px] text-[#9CA3AF]">更新于 {g.updated_at}</span>}
                <button onClick={() => setExpanded(expanded === g.name ? '' : g.name)} className="text-[#9CA3AF] hover:text-[#374151] transition-colors"><ChevronDown size={15} className={`transition-transform ${expanded === g.name ? 'rotate-180' : ''}`} /></button>
                <button onClick={() => startEdit(g)} className="text-[#6B7280] hover:text-[#EA580C] transition-colors"><Settings2 size={14} /></button>
                <button onClick={() => del(g.name)} className="text-[#6B7280] hover:text-red-500 transition-colors"><Trash2 size={14} /></button>
              </div>
              {expanded === g.name && (
                <div className="px-4 pb-3 bg-[#F9FAFB]">
                  <div className="border border-[#E5E7EB] rounded-lg bg-white divide-y divide-[#F3F4F6]">
                    {(g.members || []).map(m => (
                      <div key={m.username} className="px-3 py-2 flex items-center gap-3">
                        <div className="w-7 h-7 rounded-full bg-[#FEE2E2] flex items-center justify-center text-[11px] font-bold text-[#EA580C]">{getInitials(m.display_name || m.username)}</div>
                        <div className="text-xs text-[#111827] font-medium">{m.display_name || m.username} <span className="text-[#9CA3AF] font-mono">@{m.username}</span></div>
                        <div className="ml-auto text-xs text-[#6B7280] font-mono flex items-center gap-1"><Mail size={11} className="text-[#9CA3AF]" />{m.mail || '无邮箱'}</div>
                      </div>
                    ))}
                    {(g.members || []).length === 0 && <div className="px-3 py-4 text-center text-xs text-[#9CA3AF]">暂无成员</div>}
                  </div>
                </div>
              )}
            </div>
          ))}
          {groups.length === 0 && !editing && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">暂无通知用户组，点击右上角「新建用户组」创建</div>}
        </div>
      </div>
    </div>
  );
}

// ═══ 数据脱敏 ═══
function SanitizeConfig() {
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [testInput, setTestInput] = useState('');
  const [testOutput, setTestOutput] = useState('');
  useEffect(() => { (async () => { try { const t = localStorage.getItem('zops_token'); const r = await fetch('/api/sanitize/mappings', { headers: { 'Authorization': `Bearer ${t}` } }); if (r.ok) setMappings((await r.json()).mappings || {}); } catch (e) { console.error(e); } })(); }, []);
  const entries = Object.entries(mappings);
  const testSanitize = async () => { if (!testInput.trim()) return; try { const t = localStorage.getItem('zops_token'); const r = await fetch('/api/sanitize/test', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${t}` }, body: JSON.stringify({ text: testInput }) }); if (r.ok) { const d = await r.json(); setTestOutput(d.masked_text || ''); } } catch { setTestOutput('测试失败'); } };
  return (
    <div className="max-w-full">
      <SectionHeader icon={ShieldCheck} title="数据脱敏" subtitle="配置敏感信息过滤规则" color="#16A34A" />
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden  mb-4">
        <div className="px-4 py-3 border-b border-[#F3F4F6] bg-[#F9FAFB] text-sm font-semibold text-[#374151] flex items-center gap-2"><ShieldCheck size={14} className="text-[#9CA3AF]" /> 自定义脱敏映射 <span className="ml-auto text-xs text-[#9CA3AF]">{entries.length} 条</span></div>
        <div className="divide-y divide-[#F3F4F6]">
          {entries.map(([real, alias], i) => (<div key={i} className="px-4 py-3 flex items-center justify-between hover:bg-[#F9FAFB]"><div className="flex items-center gap-3"><div className="w-8 h-8 rounded-lg bg-[#F0FDF4] flex items-center justify-center"><ShieldCheck size={14} className="text-[#16A34A]" /></div><div><div className="font-medium text-[#111827] text-sm font-mono">{real}</div><div className="text-xs text-[#6B7280]">替换为 <span className="font-mono">{alias}</span></div></div></div><span className="text-xs px-2 py-1 rounded-full bg-green-50 text-[#16A34A] border border-green-100">启用</span></div>))}
          {entries.length === 0 && <div className="px-4 py-8 text-center text-[#9CA3AF] text-sm">暂无自定义脱敏映射（内置规则仍生效）</div>}
        </div>
      </div>
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
        <h4 className="text-sm font-semibold text-[#111827] mb-3 flex items-center gap-2"><div className="w-6 h-6 rounded-md bg-[#F0FDF4] flex items-center justify-center"><TestTube size={13} className="text-[#16A34A]" /></div>脱敏测试</h4>
        <textarea value={testInput} onChange={e => setTestInput(e.target.value)} placeholder="输入测试文本..." rows={3} className="w-full px-3 py-2 border border-[#D1D5DB] rounded-lg text-sm focus:ring-2 focus:ring-[#16A34A] focus:border-transparent mb-2" />
        <button onClick={testSanitize} className="bg-[#16A34A] hover:bg-[#15803D] text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors mb-2">测试脱敏</button>
        {testOutput && <div className="bg-[#F9FAFB] border border-[#E5E7EB] rounded-lg p-3 text-sm font-mono text-[#374151] whitespace-pre-wrap">{testOutput}</div>}
      </div>
    </div>
  );
}

// ═══ LDAP 配置 ═══
function LDAPConfig() {
  const [cfg, setCfg] = useState<any>({});
  useEffect(() => { (async () => { try { const t = localStorage.getItem('zops_token'); const r = await fetch('/api/admin/ldap-config', { headers: { 'Authorization': `Bearer ${t}` } }); if (r.ok) setCfg(await r.json()); } catch (e) { console.error(e); } })(); }, []);
  return (
    <div className="max-w-full">
      <SectionHeader icon={Server} title="LDAP/AD 配置" subtitle="Active Directory 域认证设置" color="#475569" />
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
        <div className="flex items-center gap-3 mb-4">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${cfg.ldap_enabled ? 'bg-[#F0FDF4]' : 'bg-gray-100'}`}><Server size={20} className={cfg.ldap_enabled ? 'text-[#16A34A]' : 'text-[#9CA3AF]'} /></div>
          <div><div className="font-semibold text-[#111827]">状态: {cfg.ldap_enabled ? '已启用' : '未启用'}</div><div className="text-xs text-[#6B7280]">{cfg.server_uri || '未配置服务器'}</div></div>
        </div>
        <div className="space-y-3">
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">服务器地址</span><div className="text-sm font-medium text-[#111827] font-mono">{cfg.server_uri || '-'}</div></div>
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">Bind DN</span><div className="text-sm font-medium text-[#111827] font-mono">{cfg.bind_dn || '-'}</div></div>
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">Bind 密码</span><div className="text-sm font-medium text-[#111827] font-mono">{cfg.bind_password || '-'}</div></div>
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">搜索 Base DN</span><div className="text-sm font-medium text-[#111827] font-mono">{cfg.search_base || '-'}</div></div>
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">用户过滤器</span><div className="text-sm font-medium text-[#111827] font-mono">{cfg.search_filter || '-'}</div></div>
          <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">默认角色</span><div className="text-sm font-medium text-[#111827]">{cfg.default_role || '-'}</div></div>
        </div>
      </div>
    </div>
  );
}

// ═══ 系统信息 ═══
function SystemInfo({ overview }: { overview: any }) {
  return (
    <div className="max-w-full">
      <SectionHeader icon={Info} title="系统信息" subtitle="版本和运行状态" color="#2563EB" />
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 ">
          <div className="flex items-center gap-2 mb-3"><Info size={16} className="text-blue-500" /><span className="text-sm font-semibold text-[#111827]">基本信息</span></div>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-[#6B7280]">版本</span><span className="font-mono font-medium text-[#111827]">{overview?.version || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">应用</span><span className="font-medium text-[#111827]">{overview?.app || '-'}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">描述</span><span className="text-[#4B5563] text-xs truncate max-w-[180px]">{overview?.description || '-'}</span></div>
          </div>
        </div>
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 ">
          <div className="flex items-center gap-2 mb-3"><Activity size={16} className="text-green-500" /><span className="text-sm font-semibold text-[#111827]">运行统计</span></div>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-[#6B7280]">Agent 数</span><span className="font-bold text-[#2563EB]">{overview?.agent_count || 0}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">技能数</span><span className="font-bold text-[#7C3AED]">{overview?.total_skills || 0}</span></div>
            <div className="flex justify-between"><span className="text-[#6B7280]">LLM 调用</span><span className="font-bold text-[#16A34A]">{overview?.llm_usage_count || 0}</span></div>
          </div>
        </div>
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-4  col-span-2">
          <div className="flex items-center gap-2 mb-3"><Brain size={16} className="text-indigo-500" /><span className="text-sm font-semibold text-[#111827]">LLM 配置</span></div>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">Provider</span><div className="font-medium text-[#111827]">{overview?.llm_provider || '-'}</div></div>
            <div className="bg-[#F9FAFB] rounded-lg px-3 py-2"><span className="text-xs text-[#6B7280]">Model（实际）</span><div className="font-mono font-medium text-[#111827]">{overview?.llm_real_model || overview?.llm_model || '-'}</div>{overview?.llm_model_alias && overview?.llm_model && <div className="text-[10px] text-[#9CA3AF] font-mono mt-0.5">调用别名: {overview.llm_model}</div>}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AdminPage() {
  return <AdminContent />;
}
