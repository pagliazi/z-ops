'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  AlertTriangle, Bug, Clock, RefreshCw, ChevronRight, Lightbulb,
  ShieldCheck, Timer, Layers, Target, Loader2, Check,
} from 'lucide-react';
import {
  fetchObs, postObs, formatTs, formatSec, StatCard, LoadingPlaceholder, EmptyState,
  CategoryBadge, SeverityBadge, ExecDetailDrawer,
  type ErrorGroup, type ExecRecord, type LatencyBlock,
} from './shared';

interface Diagnostics {
  summary: {
    window_days: number;
    window_executions: number;
    window_failures: number;
    failure_rate: number;
    failure_pool: number;
    error_groups: number;
    active_groups: number;
    affected_sessions: number;
    slow_calls: number;
    p95_latency_ms: number;
    max_latency_ms: number;
  };
  error_groups: ErrorGroup[];
  by_category: { category: string; label: string; count: number; groups: number }[];
  latency: LatencyBlock;
  error?: string;
}

export function ErrorDiagnosticsTab() {
  const [data, setData] = useState<Diagnostics | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(7);
  const [detail, setDetail] = useState<ExecRecord | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchObs(`diagnostics?days=${days}`));
    } catch (e) { console.error(e); }
    setLoading(false);
  }, [days]);

  useEffect(() => { load(); }, [load]);

  if (!data) return <LoadingPlaceholder text="正在聚合失败记录..." />;

  const s = data.summary || ({} as Diagnostics['summary']);
  const groups = data.error_groups || [];
  const latency = data.latency || ({ agents: [], outliers: [], directions: [], tools: [], slow_threshold_ms: 30000 } as LatencyBlock);

  return (
    <div className="p-6 space-y-5">
      {/* 概览 */}
      <div className="grid grid-cols-5 gap-4">
        <StatCard icon={Bug} label="错误类型" value={s.error_groups ?? 0} color="red"
          hint={`失败池累计 ${s.failure_pool ?? 0} 条记录`} />
        <StatCard icon={AlertTriangle} label="活跃错误(7天内)" value={s.active_groups ?? 0} color="yellow"
          hint={s.active_groups ? '仍在发生，需优先处理' : '近 7 天无新增失败'} />
        <StatCard icon={Layers} label="受影响会话" value={s.affected_sessions ?? 0} color="purple"
          hint="有失败记录的独立会话数" />
        <StatCard icon={Timer} label="慢调用" value={s.slow_calls ?? 0} color="indigo"
          hint={`≥ ${Math.round((latency.slow_threshold_ms || 30000) / 1000)}s 的执行次数`} />
        <StatCard icon={Clock} label="P95 耗时" value={formatSec(s.p95_latency_ms)} color="blue"
          hint={`最慢单次 ${formatSec(s.max_latency_ms)}`} />
      </div>

      {/* 窗口与分类 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 shadow-sm flex flex-wrap items-center gap-3">
        <span className="text-xs text-[#6B7280]">观测窗口</span>
        <div className="flex gap-1">
          {[1, 3, 7, 30].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`px-3 py-1 rounded-full text-xs border transition-colors ${
                days === d
                  ? 'bg-[#2563EB] border-[#2563EB] text-white'
                  : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
              }`}>
              {d === 1 ? '今天' : `${d} 天`}
            </button>
          ))}
        </div>
        <span className="text-xs text-[#9CA3AF]">
          窗口内执行 {s.window_executions ?? 0} 次 · 失败 {s.window_failures ?? 0} 次 · 失败率 {s.failure_rate ?? 0}%
        </span>
        {(data.by_category || []).map(c => (
          <span key={c.category} className="text-xs text-[#6B7280] flex items-center gap-1">
            <CategoryBadge category={c.category} label={c.label} />
            {c.count} 次 / {c.groups} 类
          </span>
        ))}
        <button onClick={load} className="ml-auto p-2 rounded-lg hover:bg-[#F3F4F6] text-[#6B7280]">
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* 错误组 */}
      <div>
        <h3 className="text-sm font-medium text-[#374151] mb-3 flex items-center gap-2">
          <Bug size={15} className="text-red-500" />
          错误分组（按根因指纹聚类，含优化方向）
        </h3>
        <div className="space-y-3">
          {groups.length === 0 && (
            <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm">
              <EmptyState icon={ShieldCheck} title="失败池为空" hint="Agent 执行失败后会自动进入这里并聚类分析" />
            </div>
          )}
          {groups.map(g => (
            <ErrorGroupCard key={g.signature} group={g} onOpenRecord={setDetail} onChanged={load} />
          ))}
        </div>
      </div>

      {/* 延迟优化方向 */}
      {latency.directions.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-[#374151] mb-3 flex items-center gap-2">
            <Lightbulb size={15} className="text-amber-500" />
            延迟优化方向（基于真实分位数触发）
          </h3>
          <div className="grid grid-cols-2 gap-4">
            {latency.directions.map(d => (
              <div key={d.agent_id} className="bg-white border border-[#E5E7EB] rounded-xl p-4 shadow-sm">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-[#111827]">{d.title}</span>
                  <SeverityBadge severity={d.severity} />
                </div>
                <div className="text-[11px] text-[#9CA3AF] mb-2">{d.evidence}</div>
                <ul className="space-y-1">
                  {d.actions.map((a, i) => (
                    <li key={i} className="text-[12px] text-[#4B5563] flex gap-1.5 leading-relaxed">
                      <ChevronRight size={13} className="mt-0.5 text-[#9CA3AF] flex-shrink-0" />
                      <span>{a}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Agent 延迟分位数 */}
      {latency.agents.length > 0 && (
        <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151]">
            Agent 延迟分位数（窗口内 {s.window_executions ?? 0} 次执行）
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-[#6B7280] bg-[#F9FAFB]">
                <th className="text-left px-5 py-2 font-medium">Agent</th>
                <th className="text-right px-3 py-2 font-medium">调用</th>
                <th className="text-right px-3 py-2 font-medium">平均</th>
                <th className="text-right px-3 py-2 font-medium">P50</th>
                <th className="text-right px-3 py-2 font-medium">P95</th>
                <th className="text-right px-3 py-2 font-medium">P99</th>
                <th className="text-right px-3 py-2 font-medium">最慢</th>
                <th className="text-right px-5 py-2 font-medium">慢调用</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F3F4F6]">
              {latency.agents.map(a => (
                <tr key={a.agent_id} className="hover:bg-[#F9FAFB]">
                  <td className="px-5 py-2.5 text-[#111827] font-medium text-[13px]">{a.agent_id}</td>
                  <td className="px-3 py-2.5 text-right text-[#6B7280] text-[13px]">{a.calls}</td>
                  <td className="px-3 py-2.5 text-right text-[#374151] text-[13px]">{formatSec(a.avg_ms)}</td>
                  <td className="px-3 py-2.5 text-right text-[#374151] text-[13px]">{formatSec(a.p50_ms)}</td>
                  <td className={`px-3 py-2.5 text-right text-[13px] font-medium ${
                    a.p95_ms >= 120000 ? 'text-red-600' : a.p95_ms >= 30000 ? 'text-amber-600' : 'text-[#374151]'
                  }`}>{formatSec(a.p95_ms)}</td>
                  <td className="px-3 py-2.5 text-right text-[#374151] text-[13px]">{formatSec(a.p99_ms)}</td>
                  <td className="px-3 py-2.5 text-right text-[#6B7280] text-[13px]">{formatSec(a.max_ms)}</td>
                  <td className="px-5 py-2.5 text-right text-[13px]">
                    <span className={a.slow_calls > 0 ? 'text-amber-700' : 'text-[#9CA3AF]'}>
                      {a.slow_calls} 次 / {a.slow_ratio}%
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 慢调用离群点 */}
      {latency.outliers.length > 0 && (
        <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-3 border-b border-[#F3F4F6] text-sm font-medium text-[#374151] flex items-center gap-2">
            <Timer size={14} className="text-amber-500" />
            慢调用明细（≥ {Math.round((latency.slow_threshold_ms || 30000) / 1000)}s，点击查看完整上下文）
            <span className="ml-auto text-xs text-[#9CA3AF]">{latency.outliers.length} 条</span>
          </div>
          <div className="divide-y divide-[#F3F4F6] max-h-[40vh] overflow-y-auto">
            {latency.outliers.map((o, i) => (
              <button key={i} onClick={() => setDetail(o)}
                className="w-full px-5 py-2.5 text-left flex items-center gap-3 hover:bg-[#F9FAFB] transition-colors">
                <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${o.success === false ? 'bg-red-400' : 'bg-amber-400'}`} />
                <span className="text-[13px] font-medium text-[#111827] w-32 truncate">{o.agent_id}</span>
                <span className="text-xs text-[#6B7280] w-28 truncate">{o.tool_name}</span>
                <span className="text-xs text-[#4B5563] flex-1 truncate">{o.input_summary || '-'}</span>
                <span className="text-[13px] font-medium text-amber-700 w-16 text-right">{formatSec(o.latency_ms)}</span>
                <span className="text-xs text-[#9CA3AF] w-28 text-right">{formatTs(o.started_at)}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <ExecDetailDrawer record={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// 单个错误组卡片
// ═══════════════════════════════════════════════════════
function ErrorGroupCard({ group, onOpenRecord, onChanged }:
  { group: ErrorGroup; onOpenRecord: (r: ExecRecord) => void; onChanged: () => void }) {
  const [open, setOpen] = useState(group.state === 'active');
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState(false);

  const createOptimization = async () => {
    setSaving(true);
    try {
      await postObs('optimizations', {
        title: `${group.agents[0] || 'agent'}: ${group.signature.slice(0, 60)}`,
        category: group.category,
        severity: group.severity,
        status: 'planned',
        owner: group.agents.join(' / '),
        problem: `错误「${group.sample_error.slice(0, 160)}」累计发生 ${group.count} 次，`
          + `影响 ${group.session_count} 个会话，最近一次 ${formatTs(group.last_seen)}。`,
        root_cause: '待定位（由可观测性大盘自动立项）',
        direction: group.direction.title,
        actions: group.direction.actions,
        metric: {
          type: 'error_signature',
          match: group.sample_error.slice(0, 60),
          agent_id: group.agents[0] || '',
        },
      });
      setCreated(true);
      onChanged();
    } catch (e) {
      console.error(e);
      alert('立项失败，请确认当前账号为管理员');
    }
    setSaving(false);
  };

  return (
    <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-3.5 flex items-start gap-3">
        <span className={`w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${group.state === 'active' ? 'bg-red-500' : 'bg-[#D1D5DB]'}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <CategoryBadge category={group.category} label={group.category_label} />
            <SeverityBadge severity={group.severity} />
            <span className="text-[11px] px-1.5 py-0.5 rounded border border-[#E5E7EB] text-[#6B7280]">
              {group.state === 'active' ? `7 天内 ${group.recent_7d} 次` : `已静默 ${group.silent_days} 天`}
            </span>
            <span className="text-xs text-[#9CA3AF]">
              累计 {group.count} 次 · {group.session_count} 个会话 · {group.agents.join(', ')}
            </span>
          </div>
          <div className="mt-1.5 font-mono text-[13px] text-[#111827] break-all leading-relaxed">
            {group.signature}
          </div>
          <div className="mt-1 text-[11px] text-[#9CA3AF]">
            首次 {formatTs(group.first_seen)} · 最近 {formatTs(group.last_seen)}
            {group.tools.length > 0 && ` · 工具 ${group.tools.join(', ')}`}
          </div>
        </div>
        <button onClick={() => setOpen(!open)}
          className="px-2.5 py-1 rounded-full border border-[#E5E7EB] text-xs text-[#6B7280] hover:bg-[#F9FAFB] flex-shrink-0">
          {open ? '收起' : '展开详情'}
        </button>
      </div>

      {open && (
        <div className="border-t border-[#F3F4F6] px-5 py-4 space-y-4 bg-[#FCFCFD]">
          {/* 优化方向 */}
          <div className="rounded-xl border border-[#E5E7EB] bg-white p-4">
            <div className="flex items-center gap-2 mb-2">
              <Lightbulb size={14} className="text-amber-500" />
              <span className="text-[13px] font-medium text-[#111827]">优化方向：{group.direction.title}</span>
              {created ? (
                <span className="ml-auto text-xs text-green-700 flex items-center gap-1">
                  <Check size={13} /> 已立项到优化闭环
                </span>
              ) : (
                <button onClick={createOptimization} disabled={saving}
                  className="ml-auto px-3 py-1 rounded-full bg-[#2563EB] text-white text-xs hover:bg-[#1D4ED8] disabled:opacity-60 flex items-center gap-1">
                  {saving ? <Loader2 size={12} className="animate-spin" /> : <Target size={12} />}
                  立项到优化闭环
                </button>
              )}
            </div>
            <ol className="space-y-1.5">
              {group.direction.actions.map((a, i) => (
                <li key={i} className="text-[12px] text-[#4B5563] flex gap-2 leading-relaxed">
                  <span className="w-4 h-4 rounded-full bg-[#F3F4F6] text-[#6B7280] text-[10px] flex items-center justify-center flex-shrink-0 mt-0.5">
                    {i + 1}
                  </span>
                  <span>{a}</span>
                </li>
              ))}
            </ol>
          </div>

          {/* 原始错误 */}
          <div className="rounded-xl border border-red-100 bg-red-50/60 p-3">
            <div className="text-[11px] text-red-700 font-medium mb-1">原始错误信息</div>
            <pre className="text-[12px] text-red-800 whitespace-pre-wrap break-all font-mono leading-relaxed">
              {group.sample_error}
            </pre>
          </div>

          {/* 失败样本 */}
          <div className="rounded-xl border border-[#E5E7EB] bg-white overflow-hidden">
            <div className="px-4 py-2.5 border-b border-[#F3F4F6] text-[12px] font-medium text-[#374151]">
              失败样本（{group.samples.length} / {group.count}，点击查看完整上下文）
            </div>
            <div className="divide-y divide-[#F3F4F6]">
              {group.samples.map((sp, i) => (
                <button key={i} onClick={() => onOpenRecord({ ...sp, success: false })}
                  className="w-full px-4 py-2.5 text-left flex items-center gap-3 hover:bg-[#F9FAFB]">
                  <span className="text-xs text-[#6B7280] w-24 truncate">{sp.agent_id}</span>
                  <span className="text-xs text-[#9CA3AF] w-20 truncate">{sp.tool_name}</span>
                  <span className="text-[12px] text-[#374151] flex-1 truncate">{sp.input_summary || '(无输入摘要)'}</span>
                  <span className="text-xs text-[#6B7280] w-14 text-right">{formatSec(sp.latency_ms)}</span>
                  <span className="text-xs text-[#9CA3AF] w-28 text-right">{formatTs(sp.started_at)}</span>
                  <ChevronRight size={14} className="text-[#D1D5DB]" />
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
