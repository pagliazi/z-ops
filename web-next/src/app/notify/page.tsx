'use client';
import { useState, useEffect, useCallback } from 'react';
import {
  Bell, Mail, Send, MessageSquare, Activity, Loader2, CheckCircle, XCircle, Users,
  Workflow as WorkflowIcon, Plus, Trash2, Search, UserPlus, X, Pencil,
} from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';

/* ═══ 通知中心: 渠道测试 · 用户组管理(CRUD+LDAP选人) · NMS 通道 · n8n 工作流 ═══ */

interface GroupMember { username: string; display_name: string; mail: string; }
interface NotifyGroup { name: string; description: string; members: GroupMember[]; updated_at?: string; }

export default function NotifyPage() {
  const auth = () => ({ 'Content-Type': 'application/json', 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` });
  const [tab, setTab] = useState<'test' | 'groups' | 'channels'>('test');

  return (
    <div className="max-w-5xl mx-auto px-6 py-6">
      <PageHeader icon={Bell} title="通知中心" subtitle="渠道连通性测试 · 通知用户组管理 · NMS 推送通道 · n8n 通知工作流" />
      <div className="flex gap-1 mt-5 border-b border-[#E5E7EB]">
        {([['test', '渠道测试', Send], ['groups', '用户组', Users], ['channels', '推送通道', Activity]] as const).map(([id, label, Icon]) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex items-center gap-1.5 px-4 pb-2.5 pt-1.5 text-sm font-medium border-b-2 transition-all
              ${tab === id ? 'border-[#2563EB] text-[#2563EB]' : 'border-transparent text-[#6B7280] hover:text-[#111827]'}`}>
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>
      <div className="mt-5">
        {tab === 'test' && <TestTab auth={auth} />}
        {tab === 'groups' && <GroupsTab auth={auth} />}
        {tab === 'channels' && <ChannelsTab auth={auth} />}
      </div>
    </div>
  );
}

/* ═══ Tab 1: 渠道测试 ═══ */
function TestTab({ auth }: { auth: () => any }) {
  const [cfg, setCfg] = useState<any>(null);
  const [mailTo, setMailTo] = useState('');
  const [testing, setTesting] = useState('');
  const [results, setResults] = useState<Record<string, any>>({});
  const [subject, setSubject] = useState('Z-OPS 通知测试');
  const [body, setBody] = useState('');
  const [sendTo, setSendTo] = useState('');
  const [sendGroup, setSendGroup] = useState('');
  const [sending, setSending] = useState(false);
  const [sendResult, setSendResult] = useState<any>(null);
  const [groups, setGroups] = useState<NotifyGroup[]>([]);

  useEffect(() => {
    (async () => {
      try { const r = await fetch('/api/notify/config', { headers: auth() }); if (r.ok) { const d = await r.json(); setCfg(d); setMailTo(d?.email?.admin_email || ''); } } catch {}
      try { const r = await fetch('/api/notify/groups', { headers: auth() }); if (r.ok) setGroups((await r.json()).groups || []); } catch {}
    })();
  }, []);

  const runTest = async (channel: string) => {
    setTesting(channel); setResults(p => ({ ...p, [channel]: null }));
    try {
      const r = await fetch(`/api/notify/test/${channel}`, {
        method: 'POST', headers: auth(),
        body: JSON.stringify(channel === 'email' ? { to: mailTo } : {}),
      });
      const d = await r.json();
      setResults(p => ({ ...p, [channel]: d }));
    } catch { setResults(p => ({ ...p, [channel]: { success: false, error: '网络错误' } })); }
    setTesting('');
  };

  const sendCustom = async () => {
    if (!body.trim()) { alert('请填写邮件内容'); return; }
    setSending(true); setSendResult(null);
    try {
      const r = await fetch('/api/notify/send', {
        method: 'POST', headers: auth(),
        body: JSON.stringify({ subject, body, to: sendTo, group: sendGroup }),
      });
      setSendResult({ ok: r.ok, data: await r.json() });
    } catch { setSendResult({ ok: false, data: { detail: '网络错误' } }); }
    setSending(false);
  };

  const ResultBox = ({ k }: { k: string }) => {
    const res = results[k];
    if (res === null) return <div className="text-xs text-[#6B7280] flex items-center gap-1 mt-2"><Loader2 size={12} className="animate-spin" /> 测试中...</div>;
    if (!res) return null;
    const ok = res.success !== false && (res.status !== 'error');
    return <div className={`mt-2 p-2.5 rounded-lg text-xs border ${ok ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-red-50 border-red-200 text-red-800'}`}>
      {ok ? <span className="flex items-center gap-1 font-medium"><CheckCircle size={13} /> 发送成功{res.channel ? ` (${res.channel})` : ''}{res.to ? ` → ${res.to}` : ''}</span>
        : <span className="flex items-center gap-1 font-medium"><XCircle size={13} /> 失败: {res.error || res.detail || JSON.stringify(res).slice(0, 120)}</span>}
    </div>;
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5">
          <div className="flex items-center gap-2.5 mb-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-orange-50"><Mail size={15} className="text-orange-600" /></div>
            <h3 className="text-sm font-semibold text-[#111827]">邮件通知</h3>
          </div>
          <input type="text" value={mailTo} onChange={e => setMailTo(e.target.value)}
            placeholder={cfg?.email?.admin_email || 'admin@example.com'}
            className="w-full px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
          <button onClick={() => runTest('email')} disabled={testing === 'email'}
            className="mt-2 w-full bg-gray-900 hover:bg-gray-700 disabled:opacity-30 text-white px-4 py-2 rounded-lg text-sm font-medium flex items-center justify-center gap-1.5 transition-colors">
            {testing === 'email' ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} 发送测试邮件
          </button>
          {cfg?.email && <p className="text-[11px] text-gray-400 mt-2">SMTP: {cfg.email.smtp_host}:{cfg.email.smtp_port} · MX 直投{cfg.email.mx_direct ? '开启' : '关闭'}</p>}
          <ResultBox k="email" />
        </div>

        <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5">
          <div className="flex items-center gap-2.5 mb-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-cyan-50"><MessageSquare size={15} className="text-cyan-600" /></div>
            <h3 className="text-sm font-semibold text-[#111827]">飞书通知</h3>
          </div>
          <p className="text-xs text-[#6B7280] mb-3">向默认飞书渠道发送测试消息，验证应用凭据与推送链路。</p>
          <button onClick={() => runTest('feishu')} disabled={testing === 'feishu'}
            className="w-full bg-gray-900 hover:bg-gray-700 disabled:opacity-30 text-white px-4 py-2 rounded-lg text-sm font-medium flex items-center justify-center gap-1.5 transition-colors">
            {testing === 'feishu' ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} 发送飞书测试
          </button>
          <ResultBox k="feishu" />
        </div>
      </div>

      <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5">
        <div className="flex items-center gap-2.5 mb-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-violet-50"><Users size={15} className="text-violet-600" /></div>
          <h3 className="text-sm font-semibold text-[#111827]">自定义邮件</h3>
        </div>
        <div className="space-y-3">
          <input type="text" value={subject} onChange={e => setSubject(e.target.value)} placeholder="邮件主题"
            className="w-full px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
          <textarea value={body} onChange={e => setBody(e.target.value)} rows={4} placeholder="邮件正文 (支持 HTML)"
            className="w-full px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
          <div className="grid grid-cols-2 gap-3">
            <input type="text" value={sendTo} onChange={e => setSendTo(e.target.value)} placeholder="收件人 (可留空, 多个分号分隔)"
              className="px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
            <select value={sendGroup} onChange={e => setSendGroup(e.target.value)}
              className="px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300">
              <option value="">或选择用户组</option>
              {groups.map(g => <option key={g.name} value={g.name}>{g.name} ({g.members?.length || 0}人)</option>)}
            </select>
          </div>
          <button onClick={sendCustom} disabled={sending}
            className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-30 text-white px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-1.5">
            {sending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} 发送
          </button>
          {sendResult && (
            <div className={`p-2.5 rounded-lg text-xs border ${sendResult.ok ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-red-50 border-red-200 text-red-800'}`}>
              {sendResult.ok ? `✅ 已发送 → ${sendResult.data?.to || sendGroup || '默认管理员'}` : `❌ ${sendResult.data?.detail || '发送失败'}`}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ═══ Tab 2: 用户组管理 (CRUD + LDAP 选人) ═══ */
function GroupsTab({ auth }: { auth: () => any }) {
  const [groups, setGroups] = useState<NotifyGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<NotifyGroup | null>(null);
  const [searchKw, setSearchKw] = useState('');
  const [ldapUsers, setLdapUsers] = useState<GroupMember[]>([]);
  const [searching, setSearching] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch('/api/notify/groups', { headers: auth() });
      if (r.ok) setGroups((await r.json()).groups || []);
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const searchLdap = async (kw: string) => {
    setSearching(true);
    try {
      const r = await fetch(`/api/notify/ldap/users?keyword=${encodeURIComponent(kw)}&limit=30`, { headers: auth() });
      if (r.ok) setLdapUsers((await r.json()).users || []);
    } catch {}
    setSearching(false);
  };

  const saveGroup = async () => {
    if (!editing || !editing.name.trim()) { setMsg('组名不能为空'); return; }
    try {
      const r = await fetch('/api/notify/groups', {
        method: 'POST', headers: auth(),
        body: JSON.stringify(editing),
      });
      if (r.ok) { setMsg(`✅ 组「${editing.name}」已保存`); setEditing(null); load(); }
      else setMsg(`❌ ${(await r.json()).detail || r.status}`);
    } catch (e: any) { setMsg('❌ ' + e.message); }
    setTimeout(() => setMsg(''), 3000);
  };

  const deleteGroup = async (name: string) => {
    if (!confirm(`删除用户组「${name}」?`)) return;
    await fetch(`/api/notify/groups/${encodeURIComponent(name)}`, { method: 'DELETE', headers: auth() });
    load();
  };

  const addMember = (u: GroupMember) => {
    if (!editing) return;
    if (editing.members.some(m => m.username === u.username)) return;
    setEditing({ ...editing, members: [...editing.members, u] });
  };
  const removeMember = (uname: string) => {
    if (!editing) return;
    setEditing({ ...editing, members: editing.members.filter(m => m.username !== uname) });
  };

  return (
    <div className="space-y-4">
      {msg && <div className="text-sm px-3 py-2 rounded-lg bg-gray-50 border border-gray-100 text-gray-700">{msg}</div>}

      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-500">{groups.length} 个用户组</span>
        <button onClick={() => setEditing({ name: '', description: '', members: [] })}
          className="flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-gray-900 text-white text-sm font-medium hover:bg-gray-700 transition-colors">
          <Plus size={14} /> 新建用户组
        </button>
      </div>

      {/* 编辑面板 */}
      {editing && (
        <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-[#111827]">{editing.updated_at ? '编辑' : '新建'}用户组</h3>
            <button onClick={() => setEditing(null)} className="p-1.5 rounded-lg text-gray-400 hover:bg-gray-100"><X size={15} /></button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <input value={editing.name} onChange={e => setEditing({ ...editing, name: e.target.value })} placeholder="组名 (如: 值班一组)"
              className="px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
            <input value={editing.description} onChange={e => setEditing({ ...editing, description: e.target.value })} placeholder="描述 (可选)"
              className="px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
          </div>

          {/* 已选成员 */}
          <div>
            <div className="text-xs font-medium text-gray-500 mb-2">成员 ({editing.members.length})</div>
            {editing.members.length === 0 ? (
              <p className="text-xs text-gray-400">尚未添加成员 — 在下方搜索并添加</p>
            ) : (
              <div className="flex flex-wrap gap-1.5 mb-3">
                {editing.members.map(m => (
                  <span key={m.username} className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-violet-50 text-violet-700 text-xs">
                    {m.display_name || m.username}
                    <button onClick={() => removeMember(m.username)} className="hover:text-red-500"><X size={11} /></button>
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* LDAP 搜索添加 */}
          <div>
            <div className="flex items-center gap-2 text-xs font-medium text-gray-500 mb-2">
              <Search size={12} /> 搜索用户 (LDAP/AD · 支持姓名/账号/邮箱)
            </div>
            <div className="flex gap-2 mb-2">
              <input value={searchKw} onChange={e => setSearchKw(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && searchLdap(searchKw)}
                placeholder="输入关键词后回车…"
                className="flex-1 px-3 py-2 rounded-lg bg-gray-50 border-0 text-sm outline-none focus:bg-white focus:ring-1 focus:ring-blue-300" />
              <button onClick={() => searchLdap(searchKw)} disabled={searching}
                className="px-4 rounded-lg bg-gray-100 text-gray-700 text-sm hover:bg-gray-200 transition-colors flex items-center gap-1.5">
                {searching ? <Loader2 size={13} className="animate-spin" /> : <UserPlus size={13} />} 搜索
              </button>
            </div>
            {ldapUsers.length > 0 && (
              <div className="border border-gray-100 rounded-xl divide-y divide-gray-50 max-h-48 overflow-y-auto">
                {ldapUsers.map(u => {
                  const added = editing.members.some(m => m.username === u.username);
                  return (
                    <button key={u.username} onClick={() => addMember(u)} disabled={added}
                      className="w-full flex items-center justify-between px-3.5 py-2.5 text-left hover:bg-gray-50 disabled:opacity-40">
                      <div>
                        <span className="text-sm text-gray-900">{u.display_name || u.username}</span>
                        <span className="text-[11px] text-gray-400 ml-2 font-mono">{u.username}</span>
                        {u.mail && <span className="text-[11px] text-gray-400 ml-2">{u.mail}</span>}
                      </div>
                      {added ? <CheckCircle size={14} className="text-emerald-500" /> : <Plus size={14} className="text-gray-400" />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 pt-2 border-t border-gray-100">
            <button onClick={() => setEditing(null)} className="px-4 py-2 rounded-lg text-sm text-gray-500 hover:bg-gray-100">取消</button>
            <button onClick={saveGroup} className="px-4 py-2 rounded-lg bg-gray-900 text-white text-sm font-medium hover:bg-gray-700">保存</button>
          </div>
        </div>
      )}

      {/* 组列表 */}
      {loading ? <div className="text-center text-sm text-gray-400 py-8">加载中…</div> : groups.length === 0 && !editing ? (
        <div className="text-center py-12 bg-white rounded-2xl border border-[#E5E7EB]">
          <Users size={28} className="mx-auto text-gray-300 mb-2" />
          <p className="text-sm text-gray-400">暂无用户组 — 点击「新建用户组」创建</p>
        </div>
      ) : (
        <div className="space-y-2">
          {groups.map(g => (
            <div key={g.name} className="group bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-4 flex items-center gap-4">
              <div className="w-9 h-9 rounded-xl flex items-center justify-center bg-violet-50 shrink-0">
                <Users size={16} className="text-violet-600" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-[#111827]">{g.name} <span className="text-[11px] text-gray-400 font-normal ml-1">{g.members?.length || 0} 名成员</span></div>
                {g.description && <div className="text-xs text-gray-400 mt-0.5 truncate">{g.description}</div>}
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {(g.members || []).slice(0, 6).map(m => (
                    <span key={m.username} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">{m.display_name || m.username}</span>
                  ))}
                  {(g.members?.length || 0) > 6 && <span className="text-[10px] text-gray-400">+{g.members.length - 6}</span>}
                </div>
              </div>
              <div className="flex gap-1 shrink-0">
                <button onClick={() => { setEditing({ ...g }); setSearchKw(''); setLdapUsers([]); }}
                  className="p-2 rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700"><Pencil size={14} /></button>
                <button onClick={() => deleteGroup(g.name)}
                  className="p-2 rounded-lg text-gray-400 hover:bg-red-50 hover:text-red-600"><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ═══ Tab 3: NMS 推送通道 + n8n 工作流 ═══ */
function ChannelsTab({ auth }: { auth: () => any }) {
  const [channels, setChannels] = useState<any[]>([]);
  const [n8nFlows, setN8nFlows] = useState<any[]>([]);
  const [chTesting, setChTesting] = useState('');
  const [flowTesting, setFlowTesting] = useState('');
  const [flowResult, setFlowResult] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try { const r = await fetch('/api/nms/channels', { headers: auth() }); if (r.ok) setChannels((await r.json()).channels || []); } catch {}
      try {
        const r = await fetch('/api/n8n/registry', { headers: auth() });
        if (r.ok) setN8nFlows(((await r.json()).workflows || []).filter((w: any) => w.category === 'notification'));
      } catch {}
      setLoading(false);
    })();
  }, []);

  const testChannel = async (id: string, name: string) => {
    setChTesting(id);
    try {
      const r = await fetch('/api/nms/channels/test', { method: 'POST', headers: auth(), body: JSON.stringify({ id }) });
      if (r.ok) alert(`✅ 通道「${name}」测试卡片已发送`);
      else alert(`❌ ${(await r.json()).detail || r.status}`);
    } catch { alert('网络错误'); }
    setChTesting('');
  };

  const triggerFlow = async (id: string) => {
    setFlowTesting(id); setFlowResult(null);
    try {
      const r = await fetch(`/api/n8n/trigger/${id}`, {
        method: 'POST', headers: auth(),
        body: JSON.stringify({ trigger: 'manual', title: 'Z-OPS 通知链路测试', message: '来自通知中心的手动触发' }),
      });
      const d = await r.json();
      setFlowResult({ id, ok: r.ok && d.success, msg: r.status === 403 ? '需要「工作流编排」权限' : (d.message || d.detail || (r.ok ? '已触发' : '失败')) });
    } catch { setFlowResult({ id, ok: false, msg: '网络错误' }); }
    setFlowTesting('');
  };

  if (loading) return <div className="text-center text-sm text-gray-400 py-8">加载中…</div>;

  return (
    <div className="space-y-5">
      <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5">
        <div className="flex items-center gap-2.5 mb-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-red-50"><Activity size={15} className="text-red-600" /></div>
          <h3 className="text-sm font-semibold text-[#111827]">NMS 告警推送通道</h3>
        </div>
        <p className="text-xs text-gray-400 mb-3">向指定飞书群发送测试卡片 (通道在「系统设置 → 通知配置」管理)</p>
        {channels.length === 0 ? <p className="text-xs text-gray-400">暂无通道</p> : (
          <div className="space-y-2">
            {channels.map((ch: any) => (
              <div key={ch.id} className="flex items-center justify-between bg-gray-50 rounded-lg px-3.5 py-2.5">
                <div>
                  <div className="text-sm font-medium text-gray-900">{ch.name}
                    <span className="text-[11px] text-gray-400 font-normal ml-1.5">({ch.type === 'webhook' ? 'Webhook' : '应用群'}{ch.enabled === false ? ' · 已停用' : ''})</span>
                  </div>
                  {ch.groups?.length ? <div className="text-[11px] text-gray-400">分组: {ch.groups.join('、')}</div> : null}
                </div>
                <button onClick={() => testChannel(ch.id, ch.name)} disabled={chTesting === ch.id}
                  className="text-xs text-blue-600 hover:text-blue-800 flex items-center gap-1 disabled:text-gray-300">
                  {chTesting === ch.id ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />} 测试
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="bg-white border border-[#E5E7EB] rounded-2xl shadow-sm p-5">
        <div className="flex items-center gap-2.5 mb-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-cyan-50"><WorkflowIcon size={15} className="text-cyan-600" /></div>
          <h3 className="text-sm font-semibold text-[#111827]">n8n 通知推送工作流</h3>
        </div>
        <p className="text-xs text-gray-400 mb-3">手动触发 n8n 通知类工作流 (需「工作流编排」权限)</p>
        {n8nFlows.length === 0 ? <p className="text-xs text-gray-400">注册表中暂无通知类工作流</p> : (
          <div className="space-y-2">
            {n8nFlows.map((w: any) => (
              <div key={w.workflow_id || w.id} className="flex items-center justify-between bg-gray-50 rounded-lg px-3.5 py-2.5">
                <div>
                  <div className="text-sm font-medium text-gray-900">{w.name || w.workflow_id}</div>
                  <div className="text-[11px] text-gray-400">参数: {(w.params || []).join('、') || '-'}{w.require_approval ? ' · 需审批' : ''}</div>
                </div>
                <div className="flex items-center gap-2">
                  {flowResult?.id === (w.workflow_id || w.id) && (
                    <span className={`text-[11px] ${flowResult.ok ? 'text-emerald-600' : 'text-red-600'}`}>{flowResult.ok ? '✓' : '✗'} {String(flowResult.msg).slice(0, 30)}</span>
                  )}
                  <button onClick={() => triggerFlow(w.workflow_id || w.id)} disabled={flowTesting === (w.workflow_id || w.id)}
                    className="text-xs text-blue-600 hover:text-blue-800 flex items-center gap-1 disabled:text-gray-300">
                    {flowTesting === (w.workflow_id || w.id) ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />} 触发
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
