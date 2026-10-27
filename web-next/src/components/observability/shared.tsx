'use client';

import { useEffect } from 'react';
import { X, RefreshCw, AlertTriangle, CheckCircle2, Clock } from 'lucide-react';

// ─── 共享类型 ───
export interface ExecRecord {
  exec_id?: string;
  agent_id: string;
  tool_name: string;
  exec_type?: string;
  success?: boolean;
  latency_ms: number;
  started_at: number;
  completed_at?: number;
  session_id?: string;
  username?: string;
  error?: string;
  input_summary?: string;
  output_summary?: string;
  model?: string;
  provider?: string;
  input_tokens?: number;
  output_tokens?: number;
}

export interface ErrorGroup {
  signature: string;
  sample_error: string;
  category: string;
  category_label: string;
  severity: string;
  direction: { title: string; actions: string[] };
  count: number;
  agents: string[];
  tools: string[];
  sessions: string[];
  session_count: number;
  first_seen: number;
  last_seen: number;
  samples: ExecRecord[];
  recent_24h: number;
  recent_7d: number;
  state: 'active' | 'dormant';
  silent_days: number;
}

export interface LatencyAgent {
  agent_id: string;
  calls: number;
  avg_ms: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  max_ms: number;
  slow_calls: number;
  slow_ratio: number;
}

export interface LatencyBlock {
  slow_threshold_ms: number;
  agents: LatencyAgent[];
  tools: { target: string; calls: number; avg_ms: number; p95_ms: number; max_ms: number }[];
  outliers: (ExecRecord & { success?: boolean })[];
  directions: { agent_id: string; severity: string; title: string; evidence: string; actions: string[] }[];
}

// ─── 请求辅助 ───
export function getToken() {
  return typeof window !== 'undefined' ? localStorage.getItem('zops_token') || '' : '';
}

export async function fetchObs(endpoint: string) {
  const res = await fetch(`/api/observability/${endpoint}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function postObs(endpoint: string, body: any) {
  const res = await fetch(`/api/observability/${endpoint}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status} ${txt}`);
  }
  return res.json();
}

// ─── 时间/数值格式化 ───
export function toDate(ts: any): Date | null {
  if (!ts && ts !== 0) return null;
  if (typeof ts === 'string') {
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof ts === 'number') {
    const d = new Date(ts > 1e12 ? ts : ts * 1000);
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

export function formatTs(ts: any) {
  const d = toDate(ts);
  if (!d) return '-';
  return d.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}

export function formatSec(ms: any) {
  const v = Number(ms || 0);
  if (!v) return '0s';
  if (v < 1000) return `${Math.round(v)}ms`;
  return `${(v / 1000).toFixed(1)}s`;
}

export const AGENT_COLORS: Record<string, string> = {
  orchestrator: '#6366f1',
  gcoc_agent: '#06b6d4',
  kb_agent: '#f59e0b',
  general_agent: '#10b981',
  nms_agent: '#ef4444',
  browser_agent: '#8b5cf6',
};

export const PIE_COLORS = ['#6366f1', '#06b6d4', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6'];

// ─── 徽标 ───
const SEVERITY_STYLE: Record<string, string> = {
  high: 'bg-red-50 text-red-700 border-red-100',
  medium: 'bg-amber-50 text-amber-700 border-amber-100',
  low: 'bg-slate-50 text-slate-600 border-slate-200',
};
const SEVERITY_LABEL: Record<string, string> = { high: '高', medium: '中', low: '低' };

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span className={`px-1.5 py-0.5 rounded border text-[10px] font-medium ${SEVERITY_STYLE[severity] || SEVERITY_STYLE.low}`}>
      {SEVERITY_LABEL[severity] || severity}
    </span>
  );
}

const CATEGORY_STYLE: Record<string, string> = {
  code_defect: 'bg-red-50 text-red-700 border-red-100',
  timeout: 'bg-orange-50 text-orange-700 border-orange-100',
  network: 'bg-purple-50 text-purple-700 border-purple-100',
  auth: 'bg-blue-50 text-blue-700 border-blue-100',
  rate_limit: 'bg-amber-50 text-amber-700 border-amber-100',
  llm_output: 'bg-indigo-50 text-indigo-700 border-indigo-100',
  data_missing: 'bg-slate-50 text-slate-600 border-slate-200',
  unknown: 'bg-slate-50 text-slate-600 border-slate-200',
};

export function CategoryBadge({ category, label }: { category: string; label: string }) {
  return (
    <span className={`px-2 py-0.5 rounded border text-[11px] font-medium ${CATEGORY_STYLE[category] || CATEGORY_STYLE.unknown}`}>
      {label}
    </span>
  );
}

// ─── 指标卡 ───
export function StatCard({ icon: Icon, label, value, color, hint }:
  { icon: any; label: string; value: string | number; color: string; hint?: string }) {
  const colorMap: Record<string, { text: string; solid: string }> = {
    indigo: { text: 'text-indigo-600', solid: 'bg-indigo-500' },
    blue: { text: 'text-blue-600', solid: 'bg-blue-500' },
    green: { text: 'text-green-600', solid: 'bg-green-500' },
    red: { text: 'text-red-600', solid: 'bg-red-500' },
    purple: { text: 'text-purple-600', solid: 'bg-purple-500' },
    yellow: { text: 'text-yellow-600', solid: 'bg-yellow-500' },
    slate: { text: 'text-slate-700', solid: 'bg-slate-500' },
  };
  const c = colorMap[color] || colorMap.blue;
  return (
    <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <div className={`w-9 h-9 rounded-xl ${c.solid} flex items-center justify-center flex-shrink-0`}>
          <Icon size={18} className="text-white" />
        </div>
        <div className="min-w-0">
          <div className={`text-2xl font-semibold ${c.text} leading-tight tracking-tight`}>{value}</div>
          <div className="text-xs text-[#6B7280] truncate mt-0.5">{label}</div>
        </div>
      </div>
      {hint && <div className="text-[11px] text-[#9CA3AF] mt-2 leading-relaxed">{hint}</div>}
    </div>
  );
}

export function LoadingPlaceholder({ text = '加载中...' }: { text?: string }) {
  return (
    <div className="flex items-center justify-center h-64 text-[#9CA3AF]">
      <RefreshCw size={20} className="animate-spin mr-2" />
      <span className="text-sm">{text}</span>
    </div>
  );
}

export function EmptyState({ icon: Icon, title, hint }: { icon: any; title: string; hint?: string }) {
  return (
    <div className="text-center text-[#9CA3AF] py-14">
      <Icon size={36} className="mx-auto mb-3 text-[#D1D5DB]" />
      <p className="text-sm text-[#6B7280]">{title}</p>
      {hint && <p className="text-xs mt-1">{hint}</p>}
    </div>
  );
}

// ─── 执行详情抽屉 ───
export function ExecDetailDrawer({ record, onClose }: { record: ExecRecord | null; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!record) return null;
  const failed = record.success === false;
  const rows: [string, any][] = [
    ['执行 ID', record.exec_id || '-'],
    ['Agent', record.agent_id || '-'],
    ['工具 / 类型', record.tool_name || record.exec_type || '-'],
    ['耗时', formatSec(record.latency_ms)],
    ['开始时间', formatTs(record.started_at)],
    ['会话 ID', record.session_id || '-'],
    ['用户', record.username || '-'],
    ['模型', record.model ? `${record.model}${record.provider ? ` · ${record.provider}` : ''}` : '-'],
    ['Token', (record.input_tokens || record.output_tokens)
      ? `in ${record.input_tokens || 0} / out ${record.output_tokens || 0}` : '-'],
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/20" onClick={onClose} />
      <div className="relative w-full max-w-[520px] h-full bg-white border-l border-[#E5E7EB] shadow-lg flex flex-col">
        <div className="px-5 py-4 border-b border-[#E5E7EB] flex items-center gap-2">
          {failed
            ? <AlertTriangle size={16} className="text-red-500" />
            : <CheckCircle2 size={16} className="text-green-500" />}
          <span className="text-sm font-medium text-[#111827]">
            执行详情 · {failed ? '失败' : '成功'}
          </span>
          <button onClick={onClose} className="ml-auto p-1.5 rounded-lg hover:bg-[#F3F4F6] text-[#6B7280]">
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          <div className="grid grid-cols-2 gap-x-4 gap-y-3">
            {rows.map(([k, v]) => (
              <div key={k} className="min-w-0">
                <div className="text-[11px] text-[#9CA3AF]">{k}</div>
                <div className="text-[13px] text-[#111827] break-all">{String(v)}</div>
              </div>
            ))}
          </div>

          {record.error && (
            <div className="rounded-xl border border-red-100 bg-red-50/60 p-3">
              <div className="text-[11px] text-red-700 font-medium mb-1 flex items-center gap-1">
                <AlertTriangle size={12} /> 错误信息
              </div>
              <pre className="text-[12px] text-red-800 whitespace-pre-wrap break-all font-mono leading-relaxed">
                {record.error}
              </pre>
            </div>
          )}

          <DetailBlock title="输入摘要" content={record.input_summary} />
          <DetailBlock title="输出摘要" content={record.output_summary} />

          {Number(record.latency_ms) >= 30000 && (
            <div className="rounded-xl border border-amber-100 bg-amber-50/60 p-3 text-[12px] text-amber-800 flex gap-2">
              <Clock size={14} className="mt-0.5 flex-shrink-0" />
              <span>
                本次耗时 {formatSec(record.latency_ms)}，已超过 30s 慢调用阈值。
                可在「错误诊断 → 延迟优化方向」查看该 Agent 的收敛建议。
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function DetailBlock({ title, content }: { title: string; content?: string }) {
  if (!content) return null;
  return (
    <div className="rounded-xl border border-[#E5E7EB] bg-[#F9FAFB] p-3">
      <div className="text-[11px] text-[#6B7280] font-medium mb-1">{title}</div>
      <pre className="text-[12px] text-[#374151] whitespace-pre-wrap break-all leading-relaxed">
        {content}
      </pre>
    </div>
  );
}
