'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Activity, Cpu, Zap, AlertTriangle, RefreshCw, Clock, CheckCircle2,
  XCircle, TrendingUp, Box, Wrench, Brain, Layers, Eye, ChevronRight,
  ArrowUpRight, ArrowDownRight, Shield, GitBranch, Bug, Timer, SearchCode, X,
} from 'lucide-react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line, Legend, Area, AreaChart
} from 'recharts';
import { PageHeader } from '@/components/PageHeader';
import {
  fetchObs, formatTs, formatSec, AGENT_COLORS, PIE_COLORS, getToken, toDate,
  StatCard, LoadingPlaceholder, ExecDetailDrawer,
  type ExecRecord, type LatencyAgent,
} from './observability/shared';
import { ErrorDiagnosticsTab } from './observability/ErrorDiagnosticsTab';
import { OptimizationLoopTab } from './observability/OptimizationLoopTab';

// ─── 类型定义 ───
interface AgentStat {
  total_calls: number;
  success_calls: number;
  failed_calls: number;
  success_rate: number;
  avg_latency_ms: number;
  total_input_tokens: number;
  total_output_tokens: number;
  p50_latency_ms?: number;
  p95_latency_ms?: number;
  p99_latency_ms?: number;
  max_latency_ms?: number;
  slow_calls?: number;
  slow_ratio?: number;
  window_calls?: number;
  msg_calls?: number;   // 消息级口径: Agent 处理的用户消息数
  tool_calls?: number;  // 工具级口径: 真实工具/LLM 调用数
}

interface HarnessData {
  failures: { count: number; recent: any[] };
  improvements: { count: number; recent: any[]; pending: number; applied: number; verified: number; rejected: number };
  experiences: { count: number; recent: any[] };
  skills: { count: number; names: string[] };
  harness_enabled: boolean;
  auto_apply_enabled: boolean;
  last_auto_apply?: string;
}

interface ToolPoolAgent {
  agent_id: string;
  description: string;
  capabilities: string[];
  tools: { name: string; description: string }[];
  tool_count: number;
  status: string;
  registered_at: number;
  usage?: { total_calls?: number; success_rate?: number; msg_calls?: number; tool_calls?: number; failed_calls?: number };
  tool_usage?: Record<string, { tool_name: string; calls: number; fails: number; fail_rate: number }>;
}

const TABS = [
  { key: 'timeline', label: '执行追踪', icon: Activity },
  { key: 'errors', label: '错误诊断', icon: Bug },
  { key: 'performance', label: 'Agent 性能', icon: TrendingUp },
  { key: 'optimize', label: '优化闭环', icon: Wrench },
  { key: 'harness', label: 'Harness 进化', icon: Brain },
  { key: 'toolpool', label: '工具池', icon: Layers },
] as const;

type TabKey = typeof TABS[number]['key'];

// ═══════════════════════════════════════════════════════
// 主组件
// ═══════════════════════════════════════════════════════
export function ObservabilityDashboard() {
  const [tab, setTab] = useState<TabKey>('timeline');
  const [refreshKey, setRefreshKey] = useState(0);
  const [autoRefresh, setAutoRefresh] = useState(false);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => setRefreshKey(k => k + 1), 30000);
    return () => clearInterval(timer);
  }, [autoRefresh]);

  const handleRefresh = () => setRefreshKey(k => k + 1);

  return (
    <div className="h-full flex flex-col bg-[#F9FAFB]">
      {/* Header */}
      <PageHeader
        icon={Eye}
        title="可观测性大盘"
        subtitle="执行追踪 · 错误诊断 · Agent 性能 · 优化闭环 · 自我进化 · 工具池"
        accent="#7C3AED"
        iconBg="rgba(124,58,237,0.1)"
        actions={
          <>
            <button
              onClick={() => setAutoRefresh(!autoRefresh)}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-all ${
                autoRefresh
                  ? 'bg-green-50 border-green-200 text-green-700'
                  : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
              }`}
            >
              {autoRefresh ? '⏸ 自动刷新' : '▶ 自动刷新'}
            </button>
            <button
              onClick={handleRefresh}
              className="p-2 hover:bg-[#F3F4F6] rounded-lg text-[#6B7280] transition-all"
              title="刷新数据"
            >
              <RefreshCw size={18} />
            </button>
          </>
        }
      />

      {/* Tabs */}
      <div className="flex border-b border-[#E5E7EB] px-6 gap-1 bg-white">
        {TABS.map(t => {
          const Icon = t.icon;
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-all ${
                active
                  ? 'border-indigo-500 text-indigo-600 bg-indigo-50/80 '
                  : 'border-transparent text-[#6B7280] hover:text-[#374151] hover:border-gray-300 hover:bg-[#F9FAFB]'
              } rounded-t-lg`}
            >
              <Icon size={15} />
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-y-auto">
        {tab === 'timeline' && <TimelineTab key={`timeline-${refreshKey}`} />}
        {tab === 'errors' && <ErrorDiagnosticsTab key={`errors-${refreshKey}`} />}
        {tab === 'performance' && <PerformanceTab key={`perf-${refreshKey}`} />}
        {tab === 'optimize' && <OptimizationLoopTab key={`optimize-${refreshKey}`} />}
        {tab === 'harness' && <HarnessTab key={`harness-${refreshKey}`} />}
        {tab === 'toolpool' && <ToolPoolTab key={`toolpool-${refreshKey}`} />}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// 调用热力图: GitHub 风格色块 (近 N 天 × 每 2 小时格, 深浅 = 调用密度)
// ═══════════════════════════════════════════════════════
function heatColor(n: number): string {
  if (n <= 0) return '#F3F4F6';
  if (n <= 1) return '#E0E7FF';
  if (n <= 3) return '#C7D2FE';
  if (n <= 6) return '#A5B4FC';
  if (n <= 12) return '#818CF8';
  if (n <= 24) return '#6366F1';
  return '#4F46E5';
}
const HEAT_LEGEND = [0, 1, 4, 7, 13, 25];
const BUCKET_H = 2;   // 时间粒度: 每 2 小时一个色块
const BUCKETS = 24 / BUCKET_H; // 12 行

function CallHeatmap() {
  const [days, setDays] = useState(14);
  const [cells, setCells] = useState<{ date: string; hour: number; count: number; fail: number }[]>([]);
  const [total, setTotal] = useState(0);
  const [peak, setPeak] = useState<string>('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchObs(`timeline?limit=20000&days=${days}`);
      const recs: ExecRecord[] = data.timeline || [];
      const map: Record<string, { count: number; fail: number }> = {};
      for (const r of recs) {
        const d = toDate(r.started_at);
        if (!d) continue;
        const cst = new Date(d.getTime() + 8 * 3600 * 1000); // Asia/Shanghai
        const key = `${cst.toISOString().slice(0, 10)}-${Math.floor(cst.getUTCHours() / BUCKET_H)}`;
        const cell = (map[key] ||= { count: 0, fail: 0 });
        cell.count += 1;
        if (r.success === false) cell.fail += 1;
      }
      const grid: { date: string; hour: number; count: number; fail: number }[] = [];
      let c = 0;
      let peakKey = ''; let peakCount = -1;
      const nowCst = new Date(Date.now() + 8 * 3600 * 1000);
      for (let i = days - 1; i >= 0; i--) {
        const day = new Date(nowCst.getTime() - i * 86400 * 1000);
        const dateStr = day.toISOString().slice(0, 10);
        for (let b = 0; b < BUCKETS; b++) {
          const v = map[`${dateStr}-${b}`];
          grid.push({ date: dateStr, hour: b, count: v?.count || 0, fail: v?.fail || 0 });
          if (v) {
            c += v.count;
            if (v.count > peakCount) { peakCount = v.count; peakKey = `${dateStr} ${String(b * BUCKET_H).padStart(2, '0')}:00`; }
          }
        }
      }
      setCells(grid);
      setTotal(c);
      setPeak(peakKey);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, [days]);

  useEffect(() => { load(); }, [load]);

  // 坐标翻转: 行 = 时间桶 (每 2 小时, 12 行), 列 = 日期 (N 列)
  const rows = Array.from({ length: BUCKETS }, (_, b) => ({
    label: `${String(b * BUCKET_H).padStart(2, '0')}:00`,
    cells: Array.from({ length: days }, (_, di) => cells[di * BUCKETS + b] || { date: '', hour: b, count: 0, fail: 0 }),
  }));
  const dayLabels = Array.from({ length: days }, (_, di) => cells[di * BUCKETS]?.date || '');
  const dayStep = days <= 7 ? 1 : days <= 14 ? 2 : 3;

  return (
    <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 shadow-sm">
      <div className="flex items-center justify-between mb-4 gap-2">
        <h3 className="text-sm font-medium text-[#374151] flex items-center gap-2 whitespace-nowrap">
          <Zap size={14} className="text-indigo-500" />
          调用热力图（近 {days} 天 · 每 {BUCKET_H} 小时）
        </h3>
        <div className="flex items-center gap-2 flex-shrink-0">
          <div className="flex gap-1 flex-shrink-0">
            {[7, 14, 30].map(d => (
              <button key={d} onClick={() => setDays(d)}
                className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                  days === d
                    ? 'bg-[#EEF2FF] border-[#C7D2FE] text-[#4F46E5]'
                    : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
                }`}>
                {d} 天
              </button>
            ))}
          </div>
          <span className="text-[11px] text-[#9CA3AF] whitespace-nowrap">共 {total} 次调用{peak && ` · 峰值 ${peak}`}</span>
          <button onClick={load} className="p-1 hover:bg-[#F3F4F6] rounded text-[#9CA3AF]">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <div className="inline-block w-full pr-1">
          {/* 列头: 日期刻度 */}
          <div className="flex mb-1">
            <div className="w-14 flex-shrink-0" />
            {dayLabels.map((ds, di) => (
              <div key={di} className="flex-1 text-center text-[9px] text-[#9CA3AF] font-mono pb-1">
                {di % dayStep === 0 ? ds.slice(5) : ''}
              </div>
            ))}
          </div>
          {/* 行: 小时 × N 天 */}
          {rows.map((row, ri) => (
            <div key={ri} className="flex items-center gap-0.5 mb-0.5">
              <div className="w-14 flex-shrink-0 text-[9px] text-[#9CA3AF] font-mono text-right pr-2 leading-none">
                {row.label}
              </div>
              {row.cells.map((cell, ci) => (
                <div key={ci} className="flex-1" title={`${cell.date} ${String(cell.hour * BUCKET_H).padStart(2, '0')}:00-${String(cell.hour * BUCKET_H + BUCKET_H - 1).padStart(2, '0')}:59 · 调用 ${cell.count} 次${cell.fail > 0 ? ` · 失败 ${cell.fail} 次` : ''}`}>
                  <div className="h-[12px] rounded-[2px] transition-colors" style={{ background: heatColor(cell.count) }}>
                    {cell.fail > 0 && (
                      <span className="block w-1 h-1 rounded-full bg-red-500 ml-auto mr-0.5 mt-0.5" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          ))}
          {/* 图例 */}
          <div className="flex items-center justify-end gap-1.5 mt-3 text-[10px] text-[#9CA3AF]">
            <span>少</span>
            {HEAT_LEGEND.map((v, i) => (
              <span key={i} className="w-[10px] h-[10px] rounded-[2px]" style={{ background: heatColor(v) }} />
            ))}
            <span>多</span>
            <span className="ml-3 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-red-500" /> 含失败
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// Tab 1: 执行追踪
// ═══════════════════════════════════════════════════════
function TimelineTab() {
  const [records, setRecords] = useState<ExecRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('');
  const [days, setDays] = useState(3);
  const [mode, setMode] = useState<'all' | 'failed' | 'slow'>('all');
  const [detail, setDetail] = useState<ExecRecord | null>(null);
  // v4.0: trace_id 精确查询 (路由 → Agent → 工具全链)
  const [traceQ, setTraceQ] = useState('');
  const [traceResult, setTraceResult] = useState<{ trace_id: string; records: ExecRecord[] } | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchObs(`timeline?limit=300&days=${days}`);
      setRecords(data.timeline || []);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, [days]);

  useEffect(() => { load(); }, [load]);

  // 按 trace_id 查询全链执行记录 (来自消息 msg_id 的链路追踪)
  const searchTrace = async () => {
    const q = traceQ.trim();
    if (!q) return;
    setTraceLoading(true); setTraceError('');
    try {
      const res = await fetch(`/api/observability/executions?trace_id=${encodeURIComponent(q)}&limit=50`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setTraceResult({ trace_id: data.trace_id, records: data.records || [] });
      if (!(data.records || []).length) setTraceError(`trace ${q} 无执行记录 (可能已过 7 天保留期)`);
    } catch (e: any) {
      setTraceError(`查询失败: ${e.message}`);
      setTraceResult(null);
    }
    setTraceLoading(false);
  };

  const SLOW_MS = 30000;
  // trace 模式: 列表与统计切换为该 trace_id 的全链记录; 否则为时间线数据
  const base: ExecRecord[] = traceResult ? traceResult.records : records;
  const filtered = base.filter(r => {
    if (mode === 'failed' && r.success !== false) return false;
    if (mode === 'slow' && (r.latency_ms || 0) < SLOW_MS) return false;
    if (!filter) return true;
    const kw = filter.toLowerCase();
    return (r.agent_id || '').toLowerCase().includes(kw)
      || (r.tool_name || '').toLowerCase().includes(kw)
      || (r.input_summary || '').toLowerCase().includes(kw)
      || (r.error || '').toLowerCase().includes(kw);
  });

  // 按小时聚合的 mini chart
  const hourlyData = (() => {
    const map: Record<string, { count: number; fail: number }> = {};
    records.forEach(r => {
      const h = new Date((r.started_at || 0) * 1000).getHours();
      const key = `${String(h).padStart(2, '0')}:00`;
      if (!map[key]) map[key] = { count: 0, fail: 0 };
      map[key].count += 1;
      if (r.success === false) map[key].fail += 1;
    });
    return Object.entries(map)
      .map(([hour, v]) => ({ hour, 执行: v.count, 失败: v.fail }))
      .sort((a, b) => a.hour.localeCompare(b.hour));
  })();

  const successCount = base.filter(r => r.success !== false).length;
  const failCount = base.filter(r => r.success === false).length;
  const slowCount = base.filter(r => (r.latency_ms || 0) >= SLOW_MS).length;
  const avgElapsed = base.length > 0
    ? ((base.reduce((s, r) => s + (r.latency_ms || 0), 0) / base.length) / 1000).toFixed(1)
    : '0';
  const maxElapsed = base.reduce((m, r) => Math.max(m, r.latency_ms || 0), 0);

  return (
    <div className="p-6 space-y-5">
      {/* Summary Cards */}
      <div className="grid grid-cols-5 gap-4">
        <StatCard icon={Activity} label={`近 ${days} 天执行`} value={records.length} color="indigo"
          hint={`当前列表 ${filtered.length} 条`} />
        <StatCard icon={CheckCircle2} label="成功" value={successCount} color="green"
          hint={records.length ? `成功率 ${((successCount / records.length) * 100).toFixed(1)}%` : '暂无数据'} />
        <StatCard icon={XCircle} label="失败" value={failCount} color="red"
          hint={failCount ? '点击下方「仅失败」查看详情' : '窗口内无失败'} />
        <StatCard icon={Timer} label="慢调用" value={slowCount} color="yellow"
          hint={`≥ 30s，最慢 ${formatSec(maxElapsed)}`} />
        <StatCard icon={Clock} label="平均耗时" value={`${avgElapsed}s`} color="blue"
          hint="窗口内全部执行均值" />
      </div>

      {/* 左右分布: 24 小时执行分布 + 调用热力图 */}
      <div className="grid grid-cols-2 gap-6">
        {hourlyData.length > 0 && (
          <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 shadow-sm flex flex-col">
            <h3 className="text-sm font-medium text-[#374151] mb-3 flex items-center justify-between">
              24 小时执行分布（含失败叠加）
              <span className="text-[10px] text-[#9CA3AF] font-normal">全窗口按小时聚合</span>
            </h3>
            <div className="flex-1 min-h-[180px]">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={hourlyData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="hour" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Area type="monotone" dataKey="执行" stroke="#6366f1" fill="#eef2ff" strokeWidth={2} />
                  <Area type="monotone" dataKey="失败" stroke="#ef4444" fill="#fee2e2" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Call Heatmap: 色块深浅 = 调用密度 */}
        <CallHeatmap />
      </div>

      {/* Filter + Refresh */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 shadow-sm flex flex-wrap items-center gap-3">
        <div className="flex gap-1">
          {([['all', '全部'], ['failed', `仅失败 ${failCount}`], ['slow', `慢调用 ${slowCount}`]] as const).map(([m, label]) => (
            <button key={m} onClick={() => setMode(m as any)}
              className={`px-3 py-1 rounded-full text-xs border transition-colors ${
                mode === m
                  ? 'bg-[#2563EB] border-[#2563EB] text-white'
                  : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
              }`}>
              {label}
            </button>
          ))}
        </div>
        <div className="flex gap-1">
          {[1, 3, 7].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`px-3 py-1 rounded-full text-xs border transition-colors ${
                days === d
                  ? 'bg-[#EEF2FF] border-[#C7D2FE] text-[#4F46E5]'
                  : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
              }`}>
              {d === 1 ? '今天' : `${d} 天`}
            </button>
          ))}
        </div>
        <input
          type="text"
          placeholder="按 Agent / 工具 / 输入 / 错误关键字筛选..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          className="flex-1 min-w-[200px] px-3 py-2 border border-[#E5E7EB] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-200"
        />
        <button onClick={load} className="p-2 hover:bg-[#F3F4F6] rounded-lg text-[#6B7280]">
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* v4.0: trace_id 精确查询 (全链路追踪) */}
      <div className="flex items-center gap-2">
        <SearchCode size={15} className="text-[#9CA3AF] flex-shrink-0" />
        <input
          type="text"
          placeholder="输入 trace_id / msg_id 查询全链路执行 (路由 → Agent → 工具)..."
          value={traceQ}
          onChange={e => setTraceQ(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') searchTrace(); }}
          className="flex-1 px-3 py-2 border border-[#E5E7EB] rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-200"
        />
        <button onClick={searchTrace} disabled={traceLoading || !traceQ.trim()}
          className="px-3 py-2 rounded-lg text-xs font-medium bg-[#4F46E5] text-white hover:bg-indigo-700 disabled:opacity-40 transition-colors flex items-center gap-1.5">
          {traceLoading ? <RefreshCw size={13} className="animate-spin" /> : <SearchCode size={13} />}
          追踪
        </button>
      </div>
      {traceError && !traceResult && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">{traceError}</div>
      )}

      {/* Timeline List */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden shadow-sm">
        <div className="px-5 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex justify-between">
          {traceResult
            ? <span className="flex items-center gap-2">
                <span className="font-mono text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded">trace {traceResult.trace_id}</span>
                <span className="text-xs text-[#6B7280]">命中 {traceResult.records.length} 条全链执行记录</span>
                <button onClick={() => { setTraceResult(null); setTraceError(''); }}
                  className="text-xs text-[#6B7280] hover:text-[#111827] flex items-center gap-0.5 ml-1">
                  <X size={13} />返回时间线
                </button>
              </span>
            : <span>执行时间线（点击任意一行查看完整上下文）</span>}
          <span className="text-xs text-[#9CA3AF]">{filtered.length} 条</span>
        </div>
        <div className="divide-y divide-[#F3F4F6] max-h-[50vh] overflow-y-auto">
          {[...filtered].reverse().map((r, i) => {
            const failed = r.success === false;
            const elapsed = (r.latency_ms || 0) / 1000;
            return (
              <button key={r.exec_id || i} onClick={() => setDetail(r)}
                className={`w-full px-5 py-3 text-sm text-left flex items-center gap-3 transition-colors ${
                  failed ? 'bg-red-50/60 hover:bg-red-50' : 'hover:bg-[#F9FAFB]'
                }`}>
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${failed ? 'bg-red-400' : 'bg-green-400'}`} />
                <span className="font-medium text-[#111827] w-28 truncate">{r.agent_id}</span>
                <span className="text-[#4B5563] w-36 truncate text-xs">{r.tool_name || r.exec_type || '-'}</span>
                <span className={`text-xs w-16 flex-shrink-0 ${
                  elapsed >= 30 ? 'text-red-500 font-medium' : elapsed >= 10 ? 'text-yellow-600' : 'text-green-600'
                }`}>
                  {elapsed.toFixed(1)}s
                </span>
                {failed && r.error
                  ? <span className="text-xs text-red-600 flex-1 truncate font-mono" title={r.error}>{r.error}</span>
                  : <span className="text-xs text-[#6B7280] flex-1 truncate" title={r.input_summary}>{r.input_summary || '-'}</span>}
                <span className="text-xs text-[#9CA3AF] w-28 text-right flex-shrink-0">{formatTs(r.started_at)}</span>
                <ChevronRight size={14} className="text-[#D1D5DB] flex-shrink-0" />
              </button>
            );
          })}
          {filtered.length === 0 && (
            <div className="text-center text-[#9CA3AF] py-12">
              <Activity size={40} className="mx-auto mb-2 opacity-30" />
              <p className="text-sm">{mode === 'all' ? '暂无执行记录' : '当前筛选条件下无匹配记录'}</p>
              <p className="text-xs mt-1">Agent 执行任务后会自动记录</p>
            </div>
          )}
        </div>
      </div>

      <ExecDetailDrawer record={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// Tab 2: Agent 性能
// ═══════════════════════════════════════════════════════
function PerformanceTab() {
  const [stats, setStats] = useState<Record<string, AgentStat>>({});
  const [todayCount, setTodayCount] = useState(0);
  const [failCount, setFailCount] = useState(0);
  const [slowThreshold, setSlowThreshold] = useState(30000);
  const [windowDays, setWindowDays] = useState(7);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchObs(`stats?days=${windowDays}`);
      setStats(data.agents || {});
      setTodayCount(data.today_executions || 0);
      setFailCount(data.total_failures || 0);
      setSlowThreshold(data.slow_threshold_ms || 30000);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, [windowDays]);

  useEffect(() => { load(); }, [load]);

  // BarChart data
  const barData = Object.entries(stats).map(([id, s]) => ({
    agent: id.replace('_agent', ''),
    调用数: s.total_calls || 0,
    成功: s.success_calls || 0,
    失败: s.failed_calls || 0,
  }));

  // Pie data
  const pieData = Object.entries(stats)
    .filter(([, s]) => (s.total_calls || 0) > 0)
    .map(([id, s]) => ({ name: id.replace('_agent', ''), value: s.total_calls || 0 }));

  // Latency data (ms → s)
  const latencyData = Object.entries(stats).map(([id, s]) => ({
    agent: id.replace('_agent', ''),
    平均耗时: Number(((s.avg_latency_ms || 0) / 1000).toFixed(1)),
  }));

  const totalCalls = Object.values(stats).reduce((s, v) => s + (v.total_calls || 0), 0);
  const totalSuccess = Object.values(stats).reduce((s, v) => s + (v.success_calls || 0), 0);
  const successRate = totalCalls > 0 ? ((totalSuccess / totalCalls) * 100).toFixed(1) : '0';
  const totalSlow = Object.values(stats).reduce((s, v) => s + (v.slow_calls || 0), 0);
  const worstP95 = Object.values(stats).reduce((m, v) => Math.max(m, v.p95_latency_ms || 0), 0);
  const quantileRows = Object.entries(stats)
    .filter(([, s]) => (s.window_calls || 0) > 0)
    .sort((a, b) => (b[1].p95_latency_ms || 0) - (a[1].p95_latency_ms || 0));

  return (
    <div className="p-6 space-y-5">
      {/* Summary */}
      <div className="grid grid-cols-5 gap-4">
        <StatCard icon={Cpu} label="今日执行" value={todayCount} color="indigo"
          hint={`在统计 Agent ${Object.keys(stats).length} 个`} />
        <StatCard icon={Zap} label="累计调用" value={totalCalls} color="blue"
          hint="自统计开启以来的全量" />
        <StatCard icon={CheckCircle2} label="成功率" value={`${successRate}%`} color="green"
          hint={`成功 ${totalSuccess} / 失败 ${totalCalls - totalSuccess}`} />
        <StatCard icon={AlertTriangle} label="失败池" value={failCount} color="red"
          hint="详情见「错误诊断」Tab" />
        <StatCard icon={Timer} label={`慢调用(${windowDays}天)`} value={totalSlow} color="yellow"
          hint={`≥ ${Math.round(slowThreshold / 1000)}s，最差 P95 ${formatSec(worstP95)}`} />
      </div>

      <div className="grid grid-cols-2 gap-6">
        {/* 调用量柱状图 */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-[#374151]">Agent 调用量</h3>
            <button onClick={load} className="p-1 hover:bg-[#F3F4F6] rounded text-[#9CA3AF]">
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={barData} barCategoryGap="20%">
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="agent" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="成功" stackId="a" fill="#10b981" radius={[0, 0, 0, 0]} />
              <Bar dataKey="失败" stackId="a" fill="#ef4444" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* 调用占比饼图 */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
          <h3 className="text-sm font-medium text-[#374151] mb-3">调用占比</h3>
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={55} outerRadius={85} paddingAngle={3} dataKey="value" label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}>
                {pieData.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
              </Pie>
              <Tooltip />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* 延迟分布 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
        <h3 className="text-sm font-medium text-[#374151] mb-3">Agent 平均响应延迟 (秒)</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={latencyData} layout="vertical" barSize={20}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis type="number" tick={{ fontSize: 11 }} unit="s" />
            <YAxis type="category" dataKey="agent" tick={{ fontSize: 12 }} width={80} />
            <Tooltip />
            <Bar dataKey="平均耗时" fill="#818cf8" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* 延迟分位数明细 */}
      {quantileRows.length > 0 && (
        <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex items-center gap-2">
            <Timer size={14} className="text-indigo-500" />
            尾延迟分位数（近 {windowDays} 天真实执行记录，均值会掩盖尾延迟）
            <span className="ml-auto text-xs text-[#9CA3AF]">P95 降序</span>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-[#6B7280] bg-[#F9FAFB]">
                <th className="text-left px-5 py-2 font-medium">Agent</th>
                <th className="text-right px-3 py-2 font-medium">窗口调用</th>
                <th className="text-right px-3 py-2 font-medium">P50</th>
                <th className="text-right px-3 py-2 font-medium">P95</th>
                <th className="text-right px-3 py-2 font-medium">P99</th>
                <th className="text-right px-3 py-2 font-medium">最慢</th>
                <th className="text-right px-5 py-2 font-medium">慢调用</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F3F4F6]">
              {quantileRows.map(([id, s]) => (
                <tr key={id} className="hover:bg-[#F9FAFB]">
                  <td className="px-5 py-2.5">
                    <span className="inline-flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[id] || '#6b7280' }} />
                      <span className="text-[13px] font-medium text-[#111827]">{id}</span>
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-right text-[13px] text-[#6B7280]">{s.window_calls}</td>
                  <td className="px-3 py-2.5 text-right text-[13px] text-[#374151]">{formatSec(s.p50_latency_ms)}</td>
                  <td className={`px-3 py-2.5 text-right text-[13px] font-medium ${
                    (s.p95_latency_ms || 0) >= 120000 ? 'text-red-600'
                      : (s.p95_latency_ms || 0) >= 30000 ? 'text-amber-600' : 'text-[#374151]'
                  }`}>{formatSec(s.p95_latency_ms)}</td>
                  <td className="px-3 py-2.5 text-right text-[13px] text-[#374151]">{formatSec(s.p99_latency_ms)}</td>
                  <td className="px-3 py-2.5 text-right text-[13px] text-[#6B7280]">{formatSec(s.max_latency_ms)}</td>
                  <td className="px-5 py-2.5 text-right text-[13px]">
                    <span className={(s.slow_calls || 0) > 0 ? 'text-amber-700' : 'text-[#9CA3AF]'}>
                      {s.slow_calls || 0} 次 / {s.slow_ratio || 0}%
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Agent Detail Cards */}
      <div className="grid grid-cols-2 gap-4">
        {Object.entries(stats).map(([id, s]) => (
          <div key={id} className="bg-white border border-[#E5E7EB] rounded-xl p-5 shadow-sm">
            <div className="flex items-center gap-2 mb-3">
              <div className="w-3 h-3 rounded-full" style={{ backgroundColor: AGENT_COLORS[id] || '#6b7280' }} />
              <span className="font-medium text-[#111827] text-sm">{id}</span>
              <span className="ml-auto text-xs text-[#9CA3AF]">
                失败 {s.failed_calls || 0} 次
              </span>
            </div>
            <div className="grid grid-cols-4 gap-3 text-center">
              <div>
                <div className="text-lg font-semibold text-[#111827]">{s.total_calls || 0}</div>
                <div className="text-[11px] text-[#6B7280]">总调用</div>
                {((s.msg_calls || 0) + (s.tool_calls || 0)) > 0 && (
                  <div className="text-[10px] text-[#9CA3AF] mt-0.5">消息 {s.msg_calls || 0} · 工具 {s.tool_calls || 0}</div>
                )}
              </div>
              <div>
                <div className="text-lg font-semibold text-green-600">
                  {(s.success_rate || 0).toFixed(0)}%
                </div>
                <div className="text-[11px] text-[#6B7280]">成功率</div>
              </div>
              <div>
                <div className="text-lg font-semibold text-blue-600">{((s.avg_latency_ms || 0) / 1000).toFixed(1)}s</div>
                <div className="text-[11px] text-[#6B7280]">平均耗时</div>
              </div>
              <div>
                <div className={`text-lg font-semibold ${
                  (s.p95_latency_ms || 0) >= 30000 ? 'text-amber-600' : 'text-[#374151]'
                }`}>{s.p95_latency_ms ? formatSec(s.p95_latency_ms) : '-'}</div>
                <div className="text-[11px] text-[#6B7280]">P95</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// Tab 3: Harness 进化
// ═══════════════════════════════════════════════════════
function HarnessTab() {
  const [data, setData] = useState<HarnessData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await fetchObs('harness');
      setData(d);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  if (!data) return <LoadingPlaceholder />;

  // 进化漏斗阶段: 按自我改进闭环顺序排列，数值逐级收口 (失败→方案→应用→验证)
  const stages = [
    { label: '失败捕获', sub: '执行失败入池', value: data.failures.count, color: '#EF4444', icon: XCircle },
    { label: '改进生成', sub: '根因分析产出方案', value: data.improvements.count, color: '#F59E0B', icon: GitBranch },
    { label: '已应用', sub: '方案写入技能', value: data.improvements.applied || 0, color: '#3B82F6', icon: Zap },
    { label: '已验证', sub: 'A/B 对比确认效果', value: data.improvements.verified || 0, color: '#10B981', icon: CheckCircle2 },
  ];
  const stageMax = Math.max(...stages.map(s => s.value), 1);

  return (
    <div className="p-6 space-y-5">
      {/* Status Banner */}
      <div className={`rounded-lg p-4 flex items-center gap-3 ${data.harness_enabled ? 'bg-green-50 border border-green-200' : 'bg-yellow-50 border border-yellow-200'}`}>
        <Brain size={24} className={data.harness_enabled ? 'text-green-600' : 'text-yellow-600'} />
        <div>
          <div className="font-medium text-[#111827] text-sm">
            Harness 自我进化引擎 — {data.harness_enabled ? '已启用' : '已禁用'}
          </div>
          <div className="text-xs text-[#6B7280] mt-0.5">
            失败 → 根因分析 → 技能生成 → <span className={data.auto_apply_enabled ? 'text-green-600 font-medium' : ''}>自动应用</span> → A/B 验证 → 经验沉淀
          </div>
          {data.auto_apply_enabled && (
            <div className="text-xs text-green-600 mt-1 flex items-center gap-1">
              <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
              自动应用循环运行中 (每 5 分钟检查)
            </div>
          )}
        </div>
        <button onClick={load} className="ml-auto p-2 hover:bg-white/50 rounded-lg text-[#6B7280]">
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-5 gap-4">
        <StatCard icon={AlertTriangle} label="失败记录" value={data.failures.count} color="red" />
        <StatCard icon={GitBranch} label="待处理" value={data.improvements.pending || 0} color="yellow" />
        <StatCard icon={CheckCircle2} label="已应用" value={data.improvements.applied || 0} color="green" />
        <StatCard icon={TrendingUp} label="已验证" value={data.improvements.verified || 0} color="blue" />
        <StatCard icon={Box} label="技能总数" value={data.skills.count} color="purple" />
      </div>

      {/* Evolution Funnel — 阶段化流水线漏斗 (转化率 + 闭环反哺) */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-medium text-[#374151]">进化漏斗 · 自我改进闭环</h3>
          <span className="text-[10px] text-[#9CA3AF]">箭头上方为阶段转化率，迷你条表示相对量</span>
        </div>
        <div className="flex items-stretch">
          {stages.map((s, i) => {
            const Icon = s.icon;
            const prev = i > 0 ? stages[i - 1].value : 0;
            const conv = prev > 0 && s.value > 0 ? Math.min(Math.round((s.value / prev) * 100), 100) : null;
            return (
              <div key={s.label} className="flex items-center">
                {/* 阶段卡片 */}
                <div className="w-[150px] rounded-lg border border-[#E5E7EB] p-3 bg-[#FAFAFB] hover:border-[#D1D5DB] transition-colors">
                  <div className="flex items-center gap-1.5 mb-1.5">
                    <span className="w-5 h-5 rounded flex items-center justify-center" style={{ backgroundColor: `${s.color}1A` }}>
                      <Icon size={12} style={{ color: s.color }} />
                    </span>
                    <span className="text-[10px] text-[#9CA3AF] font-mono">0{i + 1}</span>
                  </div>
                  <div className="text-xs font-medium text-[#111827]">{s.label}</div>
                  <div className="flex items-baseline gap-1 mt-0.5">
                    <span className="text-xl font-semibold tabular-nums" style={{ color: s.color }}>{s.value}</span>
                    <span className="text-[10px] text-[#9CA3AF]">条</span>
                  </div>
                  <div className="text-[10px] text-[#9CA3AF] mb-2">{s.sub}</div>
                  {/* 相对量迷你条: 视觉漏斗收口 */}
                  <div className="h-1.5 rounded-full bg-[#F3F4F6] overflow-hidden">
                    <div className="h-full rounded-full transition-all" style={{ width: `${s.value > 0 ? Math.max((s.value / stageMax) * 100, 4) : 0}%`, backgroundColor: s.color }} />
                  </div>
                </div>
                {/* 转化箭头 */}
                {i < stages.length - 1 && (
                  <div className="flex-1 min-w-[52px] flex flex-col items-center justify-center px-1">
                    <span className={`text-[10px] font-medium tabular-nums ${conv !== null ? 'text-[#6B7280]' : 'text-[#D1D5DB]'}`}>
                      {conv !== null ? `${conv}%` : '—'}
                    </span>
                    <div className="w-full flex items-center">
                      <div className="flex-1 h-px bg-[#D1D5DB]" />
                      <ChevronRight size={12} className="text-[#9CA3AF] flex-shrink-0" />
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {/* 沉淀产出 + 反哺闭环 */}
        <div className="mt-3 rounded-lg border border-dashed border-[#D1D5DB] bg-[#F9FAFB] px-4 py-2.5 flex items-center gap-3">
          <RefreshCw size={13} className="text-[#6B7280] flex-shrink-0" />
          <span className="text-[11px] text-[#374151]">
            沉淀产出：
            <span className="mx-1 font-medium text-emerald-600">经验 {data.experiences.count} 条</span>·
            <span className="mx-1 font-medium text-purple-600">技能 {data.skills.count} 个</span>
            —— 反哺后续执行决策与路由，形成自我进化闭环
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-6">
        {/* 最近失败记录 */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex items-center gap-2">
            <XCircle size={14} className="text-red-500" />
            最近失败 ({data.failures.count})
          </div>
          <div className="divide-y divide-[#F3F4F6] max-h-[35vh] overflow-y-auto">
            {data.failures.recent.length === 0 ? (
              <div className="text-center text-[#9CA3AF] py-8 text-sm">暂无失败记录</div>
            ) : (
              [...data.failures.recent].reverse().map((f, i) => (
                <div key={i} className="px-4 py-3 text-xs hover:bg-[#F9FAFB]">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-[#111827]">{f.agent_id || f.agent}</span>
                    <span className="text-[#9CA3AF]">{f.tool_name || f.tool || ''}</span>
                    <span className="ml-auto text-[#9CA3AF]">{formatTs(f.started_at || f.timestamp)}</span>
                  </div>
                  {f.error && <div className="text-red-500 mt-1 truncate" title={f.error}>{f.error}</div>}
                </div>
              ))
            )}
          </div>
        </div>

        {/* 最近改进记录 */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex items-center gap-2">
            <GitBranch size={14} className="text-indigo-500" />
            改进记录 ({data.improvements.count})
          </div>
          <div className="divide-y divide-[#F3F4F6] max-h-[35vh] overflow-y-auto">
            {data.improvements.recent.length === 0 ? (
              <div className="text-center text-[#9CA3AF] py-8 text-sm">暂无改进记录</div>
            ) : (
              [...data.improvements.recent].reverse().map((imp, i) => {
                const statusColors: Record<string, string> = {
                  pending: 'bg-yellow-100 text-yellow-700',
                  applied: 'bg-green-100 text-green-700',
                  verified: 'bg-blue-100 text-blue-700',
                  rejected: 'bg-gray-100 text-gray-500',
                  rolled_back: 'bg-red-100 text-red-700',
                };
                const statusLabels: Record<string, string> = {
                  pending: '待应用',
                  applied: '已应用',
                  verified: '已验证',
                  rejected: '已拒绝',
                  rolled_back: '已回滚',
                };
                return (
                  <div key={i} className="px-4 py-3 text-xs hover:bg-[#F9FAFB]">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-[#111827]">{imp.skill_name || imp.skill || imp.name || `改进 #${i + 1}`}</span>
                      <span className={`px-1.5 py-0.5 rounded text-[10px] ${statusColors[imp.status] || 'bg-gray-100 text-gray-500'}`}>
                        {statusLabels[imp.status] || imp.status || 'pending'}
                      </span>
                      {imp.confidence && (
                        <span className="text-[10px] text-[#9CA3AF]">置信度: {(imp.confidence * 100).toFixed(0)}%</span>
                      )}
                      <span className="ml-auto text-[#9CA3AF]">{formatTs(imp.timestamp || imp.created_at)}</span>
                    </div>
                    {imp.rationale && <div className="text-[#6B7280] mt-1 truncate">{imp.rationale}</div>}
                    {imp.verification_status && (
                      <div className={`mt-1 text-[10px] ${imp.verification_status === 'improved' ? 'text-green-600' : imp.verification_status === 'degraded' ? 'text-red-600' : 'text-gray-500'}`}>
                        验证结果: {imp.verification_status === 'improved' ? '✅ 改善' : imp.verification_status === 'degraded' ? '❌ 退化' : '➖ 中性'}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* 经验沉淀 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex items-center gap-2">
          <TrendingUp size={14} className="text-green-500" />
          经验沉淀 ({data.experiences.count})
        </div>
        <div className="divide-y divide-[#F3F4F6] max-h-[30vh] overflow-y-auto">
          {data.experiences.recent.length === 0 ? (
            <div className="text-center text-[#9CA3AF] py-8 text-sm">暂无经验记录</div>
          ) : (
            [...data.experiences.recent].reverse().map((exp, i) => (
              <div key={i} className="px-4 py-3 text-xs hover:bg-[#F9FAFB]">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-[#111827]">{exp.category || exp.type || '通用'}</span>
                  <span className="text-[#6B7280] truncate max-w-md">{exp.insight || exp.content || exp.description || ''}</span>
                  <span className="ml-auto text-[#9CA3AF] flex-shrink-0">{formatTs(exp.timestamp || exp.created_at)}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 技能列表 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
        <h3 className="text-sm font-medium text-[#374151] mb-3 flex items-center gap-2">
          <Box size={14} className="text-yellow-500" />
          已注册技能 ({data.skills.count})
        </h3>
        <div className="flex flex-wrap gap-2">
          {data.skills.names.map((name, i) => (
            <span key={i} className="px-2.5 py-1 bg-gray-100 text-[#374151] rounded-lg text-xs font-mono">
              {name}
            </span>
          ))}
          {data.skills.names.length === 0 && (
            <span className="text-[#9CA3AF] text-sm">暂无技能</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// Tab 4: 工具池
// ═══════════════════════════════════════════════════════
function ToolPoolTab() {
  const [agents, setAgents] = useState<ToolPoolAgent[]>([]);
  const [totalTools, setTotalTools] = useState(0);
  const [totalCaps, setTotalCaps] = useState(0);
  const [totalCalls, setTotalCalls] = useState(0);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchObs('toolpool');
      setAgents(data.agents || []);
      setTotalTools(data.total_tools || 0);
      setTotalCaps(data.total_capabilities || 0);
      setTotalCalls(data.total_calls || 0);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  // Tool distribution pie (注册面: 各 agent 注册工具数)
  // 口径说明: 仅统计注册了可执行工具 (ToolDef) 的 Agent —— 当前工具执行面集中在 opscore_agent
  // (数据查询/CMDB/备份等 21 个工具); kb/general/nms/orchestrator 等为对话/动作型 Agent,
  // 能力经消息路由委派 (Agents-as-Tools), 本身无本地工具 → tool_count=0 属正常, 不计入分布
  const toolDist = agents
    .map(a => ({ name: a.agent_id.replace('_agent', ''), value: a.tool_count }))
    .filter(d => d.value > 0);
  // Real call distribution (调用面: 各 agent 真实执行次数, 与性能页同源)
  const callDist = agents
    .map(a => ({ name: a.agent_id.replace('_agent', ''), value: a.usage?.total_calls || 0 }))
    .filter(d => d.value > 0);

  return (
    <div className="p-6 space-y-5">
      {/* Summary */}
      <div className="grid grid-cols-4 gap-5">
        <StatCard icon={Cpu} label="在线 Agent" value={`${agents.filter(a => a.status === 'online').length}/${agents.length}`} color="indigo" />
        <StatCard icon={Wrench} label="总工具数" value={totalTools} color="blue" />
        <StatCard icon={Layers} label="总能力标签" value={totalCaps} color="green" />
        <StatCard icon={Zap} label="累计调用" value={totalCalls} color="purple" />
      </div>

      <div className="grid grid-cols-3 gap-5">
        {/* Tool Distribution Pie (注册面) */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
          <div className="flex items-start justify-between mb-3">
            <div>
              <h3 className="text-sm font-medium text-[#374151]">内置工具分布（注册面）</h3>
              <p className="text-[10px] text-[#9CA3AF] mt-0.5">仅含注册可执行工具的 Agent；对话/编排型 Agent 经消息路由委派，无本地工具</p>
            </div>
            <button onClick={load} className="p-1 hover:bg-[#F3F4F6] rounded text-[#9CA3AF]">
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
          {toolDist.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={toolDist} cx="50%" cy="50%" innerRadius={45} outerRadius={75} paddingAngle={3} dataKey="value"
                  label={({ name, value }) => `${name}: ${value}`}>
                  {toolDist.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[220px] flex flex-col items-center justify-center text-[#9CA3AF]">
              <Wrench size={32} className="mb-2 opacity-30" />
              <p className="text-xs">暂无注册工具</p>
            </div>
          )}
        </div>

        {/* Real Call Distribution Pie (调用面) */}
        <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-[#374151]">真实调用分布（调用面 · 与 Agent 性能页同源）</h3>
            <span className="text-[11px] text-[#9CA3AF]">工具徽标右侧 ×N = 该工具真实调用次数</span>
          </div>
          {callDist.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={callDist} cx="32%" cy="50%" innerRadius={45} outerRadius={75} paddingAngle={3} dataKey="value"
                  label={({ name, value }) => `${name}: ${value} 次`}>
                  {callDist.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[220px] flex flex-col items-center justify-center text-[#9CA3AF]">
              <Zap size={32} className="mb-2 opacity-30" />
              <p className="text-xs">暂无真实调用记录（重启后产生新调用即累计）</p>
            </div>
          )}
        </div>
      </div>

        {/* Agent Cards (span 2) */}
        <div className="col-span-2 space-y-4">
          {agents.map((a, idx) => (
            <div key={a.agent_id} className="bg-white border border-[#E5E7EB] rounded-xl p-5 ">
              <div className="flex items-center gap-2 mb-3">
                <div className="w-3 h-3 rounded-full" style={{ backgroundColor: AGENT_COLORS[a.agent_id] || PIE_COLORS[idx] }} />
                <span className="font-medium text-[#111827] text-sm">{a.agent_id}</span>
                <span className={`px-1.5 py-0.5 rounded text-[10px] ${a.status === 'online' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-[#6B7280]'}`}>
                  {a.status}
                </span>
                <span className="text-xs text-[#9CA3AF]">{a.tool_count} 工具</span>
                {/* 真实调用统计: 消息级/工具级分口径，与 Agent 性能页同源 */}
                <span className="ml-auto text-xs">
                  {(a.usage?.total_calls || 0) > 0 ? (
                    <span className="text-[#374151]">
                      <span className="font-medium">{a.usage?.total_calls}</span> 次调用
                      {((a.usage?.msg_calls || 0) + (a.usage?.tool_calls || 0)) > 0 && (
                        <span className="text-[#9CA3AF] ml-1.5">（消息 {a.usage?.msg_calls || 0} · 工具 {a.usage?.tool_calls || 0}）</span>
                      )}
                      <span className={`ml-1.5 ${(a.usage?.success_rate || 0) >= 95 ? 'text-green-600' : 'text-amber-600'}`}>
                        {a.usage?.success_rate}%
                      </span>
                    </span>
                  ) : (
                    <span className="text-[#9CA3AF]">暂无调用</span>
                  )}
                </span>
              </div>
              <div className="text-xs text-[#6B7280] mb-2">{a.description}</div>

              {/* Capabilities */}
              {a.capabilities && a.capabilities.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {a.capabilities.map((cap, i) => (
                    <span key={i} className="px-2 py-0.5 bg-[#EEF2FF] text-[#4F46E5] rounded text-[10px]">
                      {cap}
                    </span>
                  ))}
                </div>
              )}

              {/* Tools: ×N = 真实调用次数，零调用灰色、高频霆蓝 */}
              <div className="flex flex-wrap gap-1.5">
                {(a.tools || []).map((t, i) => {
                  const tu = a.tool_usage?.[t.name];
                  const calls = tu?.calls || 0;
                  return (
                    <span key={i}
                      title={`${t.description}${calls > 0 ? ` · 真实调用 ${calls} 次 (失败 ${tu!.fails} 次)` : ' · 暂无真实调用'}`}
                      className={`px-2 py-0.5 rounded text-[10px] font-mono ${
                        calls > 0 ? 'bg-indigo-50 text-[#4F46E5] ring-1 ring-indigo-100' : 'bg-gray-100 text-[#9CA3AF]'
                      }`}>
                      {t.name}{calls > 0 && <span className="ml-1 font-sans font-medium">×{calls}</span>}
                    </span>
                  );
                })}
              </div>
            </div>
          ))}

          {agents.length === 0 && (
            <div className="text-center text-[#9CA3AF] py-12">
              <Layers size={40} className="mx-auto mb-2 opacity-30" />
              <p className="text-sm">暂无注册 Agent</p>
              <p className="text-xs mt-1">Agent 启动后会自动注册到 ToolPool</p>
            </div>
          )}
        </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// 共享小组件已抽取到 ./observability/shared（StatCard / LoadingPlaceholder / ExecDetailDrawer）
// ═══════════════════════════════════════════════════════

