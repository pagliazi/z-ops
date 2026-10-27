'use client';

/**
 * 设置弹窗 — Codex 风格重写 (task-c17)
 * 设计语言: 扁平行式设置 (hairline 分隔而非卡片堆叠) · 黑色系控件 ·
 * 分段选择器 · mono 技术值 · 克制留白
 */
import { useState, useEffect, useCallback } from 'react';
import {
  X, Brain, Trash2, Search, ShieldCheck,
  User, Database, AlertTriangle, LogOut, KeyRound, ChevronDown, Monitor, Cpu, Activity, MessageSquare, Sparkles,
} from 'lucide-react';
import { initialsOf } from '@/components/chat/GlobalSidebar';
import { ComputerUseSettings } from '@/components/ComputerUseSettings';

interface MemoryHit { session_id: string; created_at: string; summary: string; topics: string[]; }
type SettingsTab = 'general' | 'memory' | 'web' | 'data';

const ASSISTANTS = [
  { id: '', label: '智能', icon: Sparkles },
  { id: 'gcoc_agent', label: '数据', icon: Cpu },
  { id: 'ops_agent', label: '运维', icon: Activity },
  { id: 'kb_agent', label: '知识', icon: MessageSquare },
  { id: 'computer_agent', label: '电脑操作', icon: Monitor },
  { id: 'general_agent', label: '通用', icon: User },
];

/* 开关 (Codex 黑色系) */
function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!on)}
      className={`relative w-9 h-[20px] rounded-full transition-colors duration-200 shrink-0 ${on ? 'bg-gray-900' : 'bg-gray-200'}`}>
      <span className={`absolute top-[2px] w-4 h-4 rounded-full bg-white shadow-sm transition-all duration-200 ${on ? 'left-[18px]' : 'left-[2px]'}`} />
    </button>
  );
}

/* 设置行 */
function Row({ label, hint, children }: { label: string; hint?: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-6 py-3.5 border-b border-gray-100 last:border-0">
      <div className="min-w-0">
        <div className="text-[13px] text-gray-900">{label}</div>
        {hint && <div className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{hint}</div>}
      </div>
      <div className="shrink-0 flex items-center gap-2">{children}</div>
    </div>
  );
}

const inputCls = 'bg-gray-50 border-0 rounded-lg px-3 py-2 text-[13px] outline-none focus:bg-white focus:ring-1 focus:ring-gray-900/15 transition-all placeholder:text-gray-400 text-gray-900';

export function SettingsModal({ open, onClose, initialTab = 'memory', username, displayName: displayNameProp, role, onLogout }: {
  open: boolean; onClose: () => void; initialTab?: SettingsTab; username?: string;
  displayName?: string; role?: string; onLogout?: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<MemoryHit[]>([]);
  const [memEnabled, setMemEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState('');

  const [displayName, setDisplayName] = useState(displayNameProp || '');
  const [pwdOpen, setPwdOpen] = useState(false);
  const [pwd, setPwd] = useState({ old: '', new1: '', new2: '' });
  const [pwdMsg, setPwdMsg] = useState('');
  const [defaultAgent, setDefaultAgent] = useState('');
  const [showThinking, setShowThinking] = useState(true);

  const authHeaders = () => ({ 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` });

  const loadMem = useCallback(async (query: string) => {
    setLoading(true);
    try {
      const r = await fetch(`/api/memory/search?q=${encodeURIComponent(query)}&limit=50`, { headers: authHeaders() });
      if (r.ok) setHits((await r.json()).results || []);
    } catch { /* 网络异常 */ }
    setLoading(false);
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const r = await fetch('/api/memory/settings', { headers: authHeaders() });
      if (r.ok) setMemEnabled((await r.json()).memory_enabled !== false);
    } catch { /* 默认开 */ }
  }, []);

  useEffect(() => {
    if (open) { setTab(initialTab); loadMem(''); loadSettings(); }
  }, [open, initialTab, loadMem, loadSettings]);

  useEffect(() => {
    setDisplayName(displayNameProp || '');
    setDefaultAgent(localStorage.getItem('zops_pref_default_agent') || '');
    setShowThinking(localStorage.getItem('zops_pref_show_thinking') !== '0');
  }, [displayNameProp, open]);

  const flash = (m: string) => { setNote(m); setTimeout(() => setNote(''), 3000); };

  const saveProfile = async () => {
    try {
      const r = await fetch('/api/auth/profile', {
        method: 'PUT', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ display_name: displayName }),
      });
      if (r.ok) {
        localStorage.setItem('zops_user', JSON.stringify({ ...JSON.parse(localStorage.getItem('zops_user') || '{}'), display_name: displayName }));
        flash('已保存');
      } else flash(`保存失败: ${(await r.json()).detail || r.status}`);
    } catch (e: any) { flash(e.message); }
  };

  const changePassword = async () => {
    if (pwd.new1 !== pwd.new2) { setPwdMsg('两次输入不一致'); return; }
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ old_password: pwd.old, new_password: pwd.new1 }),
      });
      const d = await r.json();
      if (r.ok) { setPwdMsg('✓ 已更新'); setPwd({ old: '', new1: '', new2: '' }); setTimeout(() => { setPwdMsg(''); setPwdOpen(false); }, 1500); }
      else setPwdMsg(d.detail || `失败 (${r.status})`);
    } catch (e: any) { setPwdMsg(e.message); }
  };

  const toggleMemory = async () => {
    const next = !memEnabled;
    setMemEnabled(next);
    await fetch('/api/memory/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ memory_enabled: next }),
    });
  };

  const deleteItem = async (sid: string) => {
    await fetch(`/api/memory/item?session_id=${encodeURIComponent(sid)}`, { method: 'DELETE', headers: authHeaders() });
    loadMem(q);
  };

  const clearAll = async () => {
    if (!confirm('清除你的全部记忆? 不可恢复。')) return;
    const r = await fetch('/api/memory/all', { method: 'DELETE', headers: authHeaders() });
    flash(`已清除 ${(await r.json()).deleted || 0} 条`);
    loadMem('');
  };

  if (!open) return null;

  const TABS: { id: SettingsTab; icon: any; label: string }[] = [
    { id: 'general', icon: User, label: '通用' },
    { id: 'memory', icon: Brain, label: '记忆' },
    { id: 'web', icon: Monitor, label: '电脑操作' },
    { id: 'data', icon: Database, label: '数据控制' },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[2px]"
         onClick={onClose}>
      <div className="w-full max-w-3xl h-[580px] max-h-[88vh] rounded-2xl bg-white shadow-2xl flex overflow-hidden"
           onClick={e => e.stopPropagation()}>

        {/* 左栏 */}
        <div className="w-[180px] shrink-0 bg-gray-50 border-r border-gray-100 flex flex-col py-4 px-2.5">
          <div className="px-2.5 pb-4 text-[11px] font-medium text-gray-400 tracking-wide">设置</div>
          {TABS.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[13px] font-medium text-left transition-colors mb-0.5
                ${tab === t.id ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-900 hover:bg-gray-100/60'}`}>
              <t.icon size={15} className={tab === t.id ? 'text-gray-900' : 'text-gray-400'} />
              {t.label}
            </button>
          ))}
          <div className="mt-auto px-2.5">
            <div className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold text-white"
                 style={{ background: '#6b7280' }}>
              {initialsOf(displayNameProp || username)}
            </div>
            <div className="text-[10px] text-gray-400 mt-1.5 font-mono truncate">{username || 'me'}</div>
          </div>
        </div>

        {/* 右侧内容 */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="h-14 shrink-0 flex items-center justify-between px-8 border-b border-gray-100">
            <h2 className="text-[15px] font-semibold text-gray-900">
              {tab === 'general' ? '通用' : tab === 'memory' ? '记忆' : tab === 'web' ? '电脑操作' : '数据控制'}
            </h2>
            <button onClick={onClose} aria-label="关闭设置"
              className="p-2 -mr-2 rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700 transition-colors">
              <X size={16} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-8 py-5">
            {note && (
              <div className="mb-4 text-[12px] px-3 py-2 rounded-lg bg-gray-50 text-gray-600 border border-gray-100">{note}</div>
            )}

            {/* ═══ 通用 ═══ */}
            {tab === 'general' && (
              <div>
                {/* 账户 */}
                <div className="flex items-center gap-4 pb-5 border-b border-gray-100">
                  <div className="w-12 h-12 rounded-full flex items-center justify-center text-sm font-bold text-white shrink-0"
                       style={{ background: '#6b7280' }}>
                    {initialsOf(displayNameProp || username)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-[15px] font-semibold text-gray-900 truncate">{displayNameProp || username || '用户'}</div>
                    <div className="text-[11px] text-gray-500 mt-0.5 flex items-center gap-2">
                      <span className="font-mono">@{username || '-'}</span>
                      <span className="px-1.5 py-px rounded text-[10px] font-medium"
                            style={{ background: role === 'admin' ? 'rgba(124,58,237,0.08)' : 'rgba(16,185,129,0.08)',
                                     color: role === 'admin' ? '#7C3AED' : '#059669' }}>
                        {role === 'admin' ? '管理员' : '成员'}
                      </span>
                    </div>
                  </div>
                  {onLogout && (
                    <button onClick={onLogout}
                      className="text-[12px] text-gray-500 hover:text-red-600 transition-colors flex items-center gap-1.5 shrink-0">
                      <LogOut size={13} /> 退出
                    </button>
                  )}
                </div>

                {/* 显示名 */}
                <Row label="显示名" hint="用于头像缩写与展示">
                  <input value={displayName} onChange={e => setDisplayName(e.target.value)}
                    className={`${inputCls} w-44`} placeholder="姓名" />
                  <button onClick={saveProfile} disabled={displayName === (displayNameProp || '')}
                    className="text-[12px] font-medium px-3 py-1.5 rounded-lg bg-gray-900 text-white disabled:opacity-20 hover:bg-gray-700 transition-colors">
                    保存
                  </button>
                </Row>

                {/* 默认助手 (分段选择器) */}
                <Row label="默认助手" hint="新对话默认使用的专家, 对话中可随时切换">
                  <div className="inline-flex bg-gray-100 rounded-lg p-0.5 gap-0.5 flex-wrap">
                    {ASSISTANTS.map(a => (
                      <button key={a.id} onClick={() => { setDefaultAgent(a.id); localStorage.setItem('zops_pref_default_agent', a.id); }}
                        className={`px-2.5 py-1 rounded-md text-[12px] font-medium transition-all flex items-center gap-1
                          ${defaultAgent === a.id ? 'bg-white shadow-sm text-gray-900' : 'text-gray-500 hover:text-gray-800'}`}>
                        <a.icon size={11} /> {a.label}
                      </button>
                    ))}
                  </div>
                </Row>

                {/* 思考链 */}
                <Row label="显示思考过程" hint="推理模型思考链默认展开; 关闭则折叠为「已深度思考」">
                  <Switch on={showThinking} onChange={v => { setShowThinking(v); localStorage.setItem('zops_pref_show_thinking', v ? '1' : '0'); }} />
                </Row>

                {/* 密码 (折叠) */}
                <Row label="密码" hint="本地账号可自助修改; LDAP/AD 域账号请在 AD 修改">
                  <button onClick={() => setPwdOpen(v => !v)}
                    className="text-[12px] font-medium text-gray-600 hover:text-gray-900 flex items-center gap-1 transition-colors">
                    <KeyRound size={13} /> 修改密码
                    <ChevronDown size={13} className={`transition-transform ${pwdOpen ? 'rotate-180' : ''}`} />
                  </button>
                </Row>
                {pwdOpen && (
                  <div className="py-4 border-b border-gray-100 space-y-2">
                    <input type="password" value={pwd.old} onChange={e => setPwd(p => ({ ...p, old: e.target.value }))}
                      placeholder="当前密码" className={`${inputCls} w-full`} />
                    <div className="flex gap-2">
                      <input type="password" value={pwd.new1} onChange={e => setPwd(p => ({ ...p, new1: e.target.value }))}
                        placeholder="新密码 (≥8 位)" className={`${inputCls} flex-1`} />
                      <input type="password" value={pwd.new2} onChange={e => setPwd(p => ({ ...p, new2: e.target.value }))}
                        placeholder="确认" className={`${inputCls} flex-1`} />
                    </div>
                    <div className="flex items-center gap-3">
                      <button onClick={changePassword} disabled={!pwd.old || !pwd.new1}
                        className="text-[12px] font-medium px-3.5 py-2 rounded-lg bg-gray-900 text-white disabled:opacity-20 hover:bg-gray-700 transition-colors">
                        更新密码
                      </button>
                      {pwdMsg && <span className="text-[11px] text-gray-500">{pwdMsg}</span>}
                    </div>
                  </div>
                )}

                {/* 快捷键 */}
                <Row label="命令面板" hint="任意页面唤起, ↑↓ 选择, Enter 执行">
                  <kbd className="px-2 py-1 rounded-md bg-gray-50 border border-gray-200 text-[11px] font-mono text-gray-500">⌘K</kbd>
                </Row>
              </div>
            )}

            {/* ═══ 记忆 ═══ */}
            {tab === 'memory' && (
              <div>
                <Row label="记忆" hint="跨会话记住对话要点; 回答带「记忆」徽章表示参考了这些内容">
                  <Switch on={memEnabled} onChange={toggleMemory} />
                </Row>

                <div className="flex items-start gap-2 py-3.5 border-b border-gray-100">
                  <ShieldCheck size={13} className="text-emerald-600 mt-0.5 shrink-0" />
                  <span className="text-[11px] text-gray-500 leading-relaxed">
                    <b className="text-gray-700">仅你本人可见</b> — 记忆与账号绑定; 管理员可在后台审计管理。
                  </span>
                </div>

                <div className="flex items-center gap-2 py-3">
                  <div className="flex-1 flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-50 focus-within:bg-white focus-within:ring-1 focus-within:ring-gray-900/15 transition-all">
                    <Search size={13} className="text-gray-400 shrink-0" />
                    <input value={q} onChange={e => setQ(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') loadMem(q); }}
                      placeholder="搜索记忆…" className="flex-1 bg-transparent outline-none text-[12px] placeholder:text-gray-400" />
                  </div>
                  <span className="text-[11px] text-gray-400 tabular-nums shrink-0">{loading ? '…' : `${hits.length} 条`}</span>
                </div>

                <div>
                  {loading ? (
                    <div className="text-center text-[12px] text-gray-400 py-10">检索中…</div>
                  ) : hits.length === 0 ? (
                    <div className="text-center text-[12px] text-gray-400 py-10">暂无记忆 — 对话几句后会自动沉淀</div>
                  ) : hits.map(h => (
                    <div key={h.session_id + h.created_at} className="group flex items-start gap-3 py-3 border-b border-gray-100 last:border-0">
                      <Brain size={12} className="mt-1 shrink-0 text-gray-300 group-hover:text-violet-400 transition-colors" />
                      <div className="flex-1 min-w-0">
                        <div className="text-[10px] text-gray-400 font-mono mb-0.5">{h.created_at?.slice(0, 16).replace('T', ' ')}</div>
                        <div className="text-[12px] leading-relaxed text-gray-800 break-words">{h.summary}</div>
                        {h.topics?.length > 0 && (
                          <div className="flex gap-1 mt-1.5 flex-wrap">
                            {h.topics.slice(0, 5).map((t, i) => (
                              <span key={i} className="text-[10px] px-1.5 py-px rounded bg-gray-100 text-gray-500">{t}</span>
                            ))}
                          </div>
                        )}
                      </div>
                      <button onClick={() => deleteItem(h.session_id)}
                        className="opacity-0 group-hover:opacity-100 p-1.5 -m-1 rounded-md text-gray-300 hover:text-red-500 hover:bg-red-50 transition-all shrink-0" title="忘记这条">
                        <Trash2 size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {tab === 'web' && <ComputerUseSettings />}

            {/* ═══ 数据控制 ═══ */}
            {tab === 'data' && (
              <div>
                <div className="rounded-xl border border-red-200/70 p-4">
                  <div className="flex items-center gap-2 text-[13px] font-medium text-red-600">
                    <AlertTriangle size={14} /> 清除全部记忆
                  </div>
                  <p className="text-[11px] text-gray-500 mt-1.5 leading-relaxed">
                    删除你的全部会话记忆及对应私有知识页, <b>不可恢复</b>。对话原文与审计记录按规范永久保留, 不受此操作影响。
                  </p>
                  <button onClick={clearAll}
                    className="mt-3 text-[12px] font-medium px-3.5 py-2 rounded-lg border border-red-200 text-red-600 hover:bg-red-50 transition-colors flex items-center gap-1.5">
                    <Trash2 size={13} /> 清除全部
                  </button>
                </div>
                <div className="mt-5 text-[11px] text-gray-400 leading-relaxed">
                  记忆来源: 对话摘要 (Q→A) 与显式指令「记住: …」。检索时注入你自己的提问上下文辅助回答。
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
