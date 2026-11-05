'use client';

import { useState, useEffect } from 'react';
import { Workflow, ExternalLink, Play, RefreshCw, Clock, CheckCircle, XCircle, Shield, AlertTriangle, Zap, ChevronDown, ChevronRight, Activity } from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';

interface RegistryWorkflow {
  id: string;
  workflow_id: string;
  name: string;
  description: string;
  category: string;
  params: string[];
  require_approval: boolean;
  webhook_path: string;
  schedule?: string;
}

interface N8nExecution {
  id: string;
  workflowId: string;
  status: string;
  startedAt: string;
  stoppedAt: string;
  mode: string;
}

const CATEGORY_ICONS: Record<string, string> = {
  vm: '🖥️', opscore: '📦', ad: '👤', notification: '🔔',
  report: '📊', ops: '🔧', approval: '🔐', event: '⚡',
};

const CATEGORY_COLORS: Record<string, string> = {
  vm: 'bg-blue-50 text-blue-700 border-blue-200',
  opscore: 'bg-green-50 text-[#16A34A] border-[#BBF7D0]',
  ad: 'bg-purple-50 text-purple-700 border-purple-200',
  notification: 'bg-yellow-50 text-yellow-700 border-yellow-200',
  report: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  ops: 'bg-orange-50 text-orange-700 border-orange-200',
  approval: 'bg-red-50 text-red-700 border-red-200',
  event: 'bg-cyan-50 text-cyan-700 border-cyan-200',
};

function N8nContent() {
  const [registry, setRegistry] = useState<RegistryWorkflow[]>([]);
  const [categories, setCategories] = useState<Record<string, string>>({});
  const [executions, setExecutions] = useState<N8nExecution[]>([]);
  const [n8nStatus, setN8nStatus] = useState<{ status: string; url: string }>({ status: 'unknown', url: '' });
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<'registry' | 'executions'>('registry');
  const [expandedCats, setExpandedCats] = useState<Set<string>>(new Set());
  const [triggerResult, setTriggerResult] = useState<{ wf: string; msg: string; ok: boolean } | null>(null);

  const token = typeof window !== 'undefined' ? localStorage.getItem('zops_token') : '';
  const headers = { 'Authorization': `Bearer ${token || ''}` };

  const fetchAll = async () => {
    setLoading(true);
    try {
      const [regRes, statusRes, execRes] = await Promise.all([
        fetch('/api/n8n/registry', { headers }),
        fetch('/api/n8n/status', { headers }),
        fetch('/api/n8n/executions?limit=30', { headers }),
      ]);
      if (regRes.ok) {
        const d = await regRes.json();
        setRegistry(d.workflows || []);
        setCategories(d.categories || {});
        setExpandedCats(new Set(Object.keys(d.categories || {})));
      }
      if (statusRes.ok) setN8nStatus(await statusRes.json());
      if (execRes.ok) { const d = await execRes.json(); setExecutions(d.executions || []); }
    } catch (e) { console.error('N8N fetch error:', e); }
    setLoading(false);
  };

  useEffect(() => { fetchAll(); }, []);

  const triggerWorkflow = async (wfId: string, wfName: string) => {
    try {
      const res = await fetch(`/api/n8n/trigger/${wfId}`, {
        method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const d = await res.json();
      setTriggerResult({ wf: wfName, msg: d.message || (d.success ? '触发成功' : '触发失败'), ok: d.success });
      setTimeout(() => setTriggerResult(null), 5000);
    } catch (e) {
      setTriggerResult({ wf: wfName, msg: '请求失败', ok: false });
      setTimeout(() => setTriggerResult(null), 5000);
    }
  };

  const toggleCat = (cat: string) => {
    setExpandedCats(prev => { const next = new Set(prev); next.has(cat) ? next.delete(cat) : next.add(cat); return next; });
  };

  const grouped = registry.reduce((acc, wf) => {
    const cat = wf.category || 'other';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(wf);
    return acc;
  }, {} as Record<string, RegistryWorkflow[]>);

  const isOnline = n8nStatus.status === 'online';
  const successExecs = executions.filter(e => e.status === 'success').length;

  return (
    <div className="h-full flex flex-col bg-[#F9FAFB]">
      {/* Header */}
      <PageHeader
        icon={Workflow}
        title="N8N 工作流管理"
        accent="#0891B2"
        iconBg="rgba(8,145,178,0.1)"
        subtitle={
          <span className="flex items-center gap-3">
            <span className={`flex items-center gap-1.5 ${isOnline ? 'text-green-600' : n8nStatus.status === 'offline' ? 'text-red-500' : 'text-[#9CA3AF]'}`}>
              <span className={`w-2 h-2 rounded-full ${isOnline ? 'bg-green-500 animate-pulse' : n8nStatus.status === 'offline' ? 'bg-red-500' : 'bg-gray-400'}`} />
              {isOnline ? '在线' : n8nStatus.status === 'offline' ? '离线' : '未知'}
            </span>
            <span>{registry.length} 个工作流</span>
            <span>{registry.filter(w => w.require_approval).length} 个需审批</span>
          </span>
        }
        actions={
          <>
            <button onClick={fetchAll} disabled={loading} className="p-2 hover:bg-[#F3F4F6] rounded-lg text-[#6B7280] transition-colors">
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <a href={n8nStatus.url ? `/n8n-ui/` : 'http://localhost:5678'} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-2 px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-full text-sm font-semibold transition-all">
              <ExternalLink size={14} />打开 N8N
            </a>
          </>
        }
      />

      {/* Toast */}
      {triggerResult && (
        <div className={`mx-6 mt-3 px-4 py-3 rounded-xl text-sm flex items-center gap-2  ${triggerResult.ok ? 'bg-green-50 text-[#16A34A] border border-[#BBF7D0]' : 'bg-red-50 text-red-700 border border-red-200'}`}>
          {triggerResult.ok ? <CheckCircle size={16} /> : <XCircle size={16} />}
          <span className="font-semibold">{triggerResult.wf}</span>: {triggerResult.msg}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-[#E5E7EB] px-6 bg-white">
        <button onClick={() => setActiveTab('registry')}
          className={`px-4 py-3 text-sm font-medium border-b-2 transition-all ${activeTab === 'registry' ? 'border-cyan-500 text-cyan-600 bg-cyan-50/50 ' : 'border-transparent text-[#6B7280] hover:text-[#374151] hover:bg-[#F9FAFB]'} rounded-t-lg`}>
          工作流注册表 ({registry.length})
        </button>
        <button onClick={() => setActiveTab('executions')}
          className={`px-4 py-3 text-sm font-medium border-b-2 transition-all ${activeTab === 'executions' ? 'border-cyan-500 text-cyan-600 bg-cyan-50/50 ' : 'border-transparent text-[#6B7280] hover:text-[#374151] hover:bg-[#F9FAFB]'} rounded-t-lg`}>
          执行历史 ({executions.length})
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'registry' && (
          <div className="space-y-3">
            {Object.entries(grouped).map(([cat, wfs]) => (
              <div key={cat} className="border border-[#E5E7EB] rounded-xl overflow-hidden bg-white shadow-sm hover:shadow-md transition-all">
                <button onClick={() => toggleCat(cat)}
                  className="w-full flex items-center justify-between px-5 py-4 bg-gray-50 hover:bg-blue-50 transition-all">
                  <div className="flex items-center gap-2.5">
                    <span className="text-lg">{CATEGORY_ICONS[cat] || '📋'}</span>
                    <span className="font-semibold text-[#111827]">{categories[cat] || cat}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${CATEGORY_COLORS[cat] || 'bg-[#F9FAFB] text-[#4B5563] border-[#E5E7EB]'}`}>{wfs.length}</span>
                  </div>
                  {expandedCats.has(cat) ? <ChevronDown size={16} className="text-[#9CA3AF]" /> : <ChevronRight size={16} className="text-[#9CA3AF]" />}
                </button>
                {expandedCats.has(cat) && (
                  <div className="divide-y divide-[#F3F4F6]">
                    {wfs.map(wf => (
                      <div key={wf.id} className="px-5 py-4 flex items-start justify-between hover:bg-[#F9FAFB]/50 transition-colors">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-[#111827] text-sm">{wf.name}</span>
                            {wf.require_approval && (
                              <span className="flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 bg-[#FFFBEB] text-[#D97706] rounded-full border border-amber-200/60 font-medium"><Shield size={9} /> 审批</span>
                            )}
                            {wf.schedule && (
                              <span className="flex items-center gap-0.5 text-xs px-1.5 py-0.5 bg-[#EFF6FF] text-[#2563EB] rounded border border-blue-200"><Clock size={10} /> {wf.schedule}</span>
                            )}
                          </div>
                          <p className="text-xs text-[#6B7280] mt-1">{wf.description}</p>
                          {wf.params.length > 0 && (
                            <div className="flex gap-1 mt-1.5 flex-wrap">
                              {wf.params.map(p => (<span key={p} className="text-xs px-1.5 py-0.5 bg-gray-100 text-[#4B5563] rounded font-mono">{p}</span>))}
                            </div>
                          )}
                        </div>
                        <button onClick={() => triggerWorkflow(wf.id, wf.name)}
                          className="ml-3 flex items-center gap-1.5 px-4 py-2 bg-[#F0FDF4] hover:bg-[#DCFCE7] text-[#16A34A] rounded-full text-xs font-bold transition-all border border-[#BBF7D0]  hover: shrink-0">
                          <Play size={12} /> 触发
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
            {registry.length === 0 && (
              <div className="text-center py-20">
                <div className="w-20 h-20 rounded-2xl bg-[#F3F4F6] flex items-center justify-center mx-auto mb-4"><Workflow size={36} className="text-[#9CA3AF]" /></div>
                <p className="text-[#6B7280] font-medium">暂无工作流</p>
                <p className="text-xs mt-1 text-[#9CA3AF]">请检查后端 WORKFLOW_REGISTRY 配置</p>
              </div>
            )}
          </div>
        )}

        {activeTab === 'executions' && (
          <div className="border border-[#E5E7EB] rounded-xl overflow-hidden bg-white shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-[#6B7280] uppercase tracking-wider">执行ID</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-[#6B7280] uppercase tracking-wider">状态</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-[#6B7280] uppercase tracking-wider">模式</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-[#6B7280] uppercase tracking-wider">开始时间</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-[#6B7280] uppercase tracking-wider">结束时间</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F3F4F6]">
                {executions.map(ex => (
                  <tr key={ex.id} className="hover:bg-[#F9FAFB] transition-colors">
                    <td className="px-4 py-3 font-mono text-xs text-[#374151]">{String(ex.id).slice(0, 12)}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-full ${
                        ex.status === 'success' ? 'bg-green-50 text-[#16A34A] border border-green-100' :
                        ex.status === 'error' ? 'bg-red-50 text-red-700 border border-red-100' :
                        ex.status === 'running' ? 'bg-blue-50 text-blue-700 border border-blue-100' :
                        'bg-gray-100 text-[#4B5563]'
                      }`}>
                        {ex.status === 'success' ? <CheckCircle size={10} /> :
                         ex.status === 'error' ? <XCircle size={10} /> :
                         ex.status === 'running' ? <Zap size={10} /> :
                         <AlertTriangle size={10} />}
                        {ex.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-[#4B5563]">{ex.mode || '-'}</td>
                    <td className="px-4 py-3 text-xs text-[#4B5563]">{ex.startedAt ? new Date(ex.startedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '-'}</td>
                    <td className="px-4 py-3 text-xs text-[#4B5563]">{ex.stoppedAt ? new Date(ex.stoppedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {executions.length === 0 && (
              <div className="text-center py-20">
                <div className="w-20 h-20 rounded-2xl bg-[#F3F4F6] flex items-center justify-center mx-auto mb-4"><Clock size={36} className="text-[#9CA3AF]" /></div>
                <p className="text-[#6B7280] font-medium">暂无执行记录</p>
                <p className="text-xs mt-1 text-[#9CA3AF]">{n8nStatus.status !== 'online' ? 'N8N 服务未连接' : '工作流尚未执行'}</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function N8nPage() {
  return <N8nContent />;
}
