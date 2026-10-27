'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Wrench, CheckCircle2, AlertTriangle, TrendingDown, TrendingUp, Minus,
  RefreshCw, ChevronDown, ChevronRight, FileCode2, Target, Lightbulb,
  ClipboardCheck, Loader2, Clock, Sparkles, Shield, Undo2,
} from 'lucide-react';
import {
  fetchObs, postObs, formatTs, formatSec, StatCard, LoadingPlaceholder, EmptyState,
  CategoryBadge, SeverityBadge,
} from './shared';

interface GuardrailSignal {
  label?: string;
  bad?: string;
  before_n: number;
  after_n: number;
  before_pct: number | null;
  after_pct: number | null;
  delta_pp: number | null;
  den_before: number;
  den_after: number;
}

interface GuardrailResult {
  signals: Record<string, GuardrailSignal>;
  regressions?: string[];
  sample_ok?: boolean;
  baseline_days?: number;
}

interface AutoSuggest {
  id: string;
  title: string;
  days: number;
  reason: string;
}

interface OptResult {
  metric_type: string;
  guardrail?: GuardrailResult;
  verdict: string;
  verdict_label: string;
  observed_days: number;
  detail: string;
  before: number | null;
  after: number | null;
  unit: string;
  data_available: boolean;
  delta_pct?: number;
  p95_after_ms?: number;
  sample_calls?: number;
  covered_calls?: number;
  last_hit?: number;
}

interface OptItem {
  id: string;
  title: string;
  category: string;
  severity: string;
  status: string;
  status_label: string;
  owner?: string;
  problem?: string;
  root_cause?: string;
  direction?: string;
  actions?: string[];
  files?: string[];
  landed_at?: number;
  landed_at_basis?: string;
  verification?: string;
  metric?: Record<string, any>;
  rollback_reason?: string;
  rolled_back_at?: number;
  rolled_back_by?: string;
  drift?: { changed: string[]; missing: string[]; anchored_at?: number; git?: string };
  result: OptResult;
}

interface OptBoard {
  items: OptItem[];
  board: Record<string, number>;
  board_labels: Record<string, string>;
  total: number;
  effective: number;
  updated_at: number;
  auto_suggest?: AutoSuggest[];
  error?: string;
}

const STATUS_ORDER = ['planned', 'in_progress', 'landed', 'verified', 'rejected', 'rolled_back'];

const STATUS_STYLE: Record<string, string> = {
  planned: 'bg-slate-50 text-slate-600 border-slate-200',
  in_progress: 'bg-blue-50 text-blue-700 border-blue-100',
  landed: 'bg-indigo-50 text-indigo-700 border-indigo-100',
  verified: 'bg-green-50 text-green-700 border-green-100',
  rejected: 'bg-slate-50 text-slate-400 border-slate-200',
  rolled_back: 'bg-red-50 text-red-700 border-red-100',
};

const VERDICT_META: Record<string, { style: string; icon: any }> = {
  resolved: { style: 'bg-green-50 text-green-700 border-green-100', icon: CheckCircle2 },
  improved: { style: 'bg-green-50 text-green-700 border-green-100', icon: TrendingDown },
  clean: { style: 'bg-emerald-50 text-emerald-700 border-emerald-100', icon: CheckCircle2 },
  recurring: { style: 'bg-red-50 text-red-700 border-red-100', icon: AlertTriangle },
  degraded: { style: 'bg-red-50 text-red-700 border-red-100', icon: TrendingUp },
  regression: { style: 'bg-red-50 text-red-700 border-red-200', icon: Shield },
  flat: { style: 'bg-slate-50 text-slate-600 border-slate-200', icon: Minus },
  pending: { style: 'bg-amber-50 text-amber-700 border-amber-100', icon: Clock },
  baseline_missing: { style: 'bg-amber-50 text-amber-700 border-amber-100', icon: Target },
  evidence_only: { style: 'bg-blue-50 text-blue-700 border-blue-100', icon: ClipboardCheck },
  no_data: { style: 'bg-slate-50 text-slate-500 border-slate-200', icon: Minus },
};

const METRIC_LABEL: Record<string, string> = {
  error_signature: '错误指纹前后对比',
  error_category: '错误分类前后对比',
  latency: '延迟基线对比',
  manual: '代码与验证证据',
};

export function OptimizationLoopTab() {
  const [data, setData] = useState<OptBoard | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>('all');
  const [busySuggest, setBusySuggest] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchObs('optimizations?days=30'));
    } catch (e) { console.error(e); }
    setLoading(false);
  }, []);

  // 自动建议回滚 → 人工确认后一键归档 (仅台账, 不执行任何代码操作)
  const archiveRollback = async (s: AutoSuggest) => {
    if (!window.confirm(`确认归档「${s.title}」为已回滚？\n(仅台账记录，不会执行任何代码操作)`)) return;
    setBusySuggest(s.id);
    try {
      await postObs('optimizations', {
        id: s.id, status: 'rolled_back',
        rollback_reason: `${s.reason}（自动建议·人工确认归档）`,
      });
      load();
    } catch (e) { console.error(e); }
    setBusySuggest('');
  };

  useEffect(() => { load(); }, [load]);

  if (!data) return <LoadingPlaceholder text="正在核算优化结果..." />;

  const items = data.items || [];
  const board = data.board || {};
  const labels = data.board_labels || {};
  const shown = filter === 'all' ? items : items.filter(i => i.status === filter);
  const verifiedResolved = items.filter(
    i => i.result?.verdict === 'resolved' || i.result?.verdict === 'improved'
  ).length;
  const recurring = items.filter(
    i => i.result?.verdict === 'recurring' || i.result?.verdict === 'degraded'
  ).length;

  return (
    <div className="p-6 space-y-5">
      {/* 概览 */}
      <div className="grid grid-cols-4 gap-4">
        <StatCard icon={Wrench} label="优化台账" value={data.total ?? 0} color="indigo"
          hint={`待优化 ${board.planned ?? 0} · 已落地 ${board.landed ?? 0} · 已验证 ${board.verified ?? 0} · 已回滚 ${board.rolled_back ?? 0}`} />
        <StatCard icon={CheckCircle2} label="真实生效" value={verifiedResolved} color="green"
          hint="用执行数据核算为已消除 / 已改善" />
        <StatCard icon={AlertTriangle} label="仍需跟进" value={recurring} color="red"
          hint={recurring ? '落地后仍复现或指标退化' : '无复现与退化项'} />
        <StatCard icon={Sparkles} label="待优化方向" value={board.planned ?? 0} color="yellow"
          hint="已立项、等待落地" />
      </div>

      {/* 自动建议回滚 (OPT_AUTO_ROLLBACK_SUGGEST 开启且有持续回归时才出现) */}
      {(data.auto_suggest || []).length > 0 && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 shadow-sm space-y-2">
          <div className="flex items-center gap-2 text-[13px] font-medium text-red-700">
            <Undo2 size={15} /> 建议回滚 ({(data.auto_suggest || []).length}) — 护栏指标持续回归
          </div>
          {(data.auto_suggest || []).map(s => (
            <div key={s.id} className="flex flex-wrap items-center gap-2 text-[12px] text-red-800">
              <span className="font-medium">{s.title}</span>
              <span className="opacity-80">{s.reason}</span>
              <button onClick={() => archiveRollback(s)} disabled={busySuggest === s.id}
                className="ml-auto px-3 py-1 rounded-lg bg-red-600 text-white text-[12px] hover:bg-red-700
                           disabled:opacity-50 flex items-center gap-1.5">
                {busySuggest === s.id ? <Loader2 size={12} className="animate-spin" /> : null} 归档为已回滚
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 看板筛选 */}
      <div className="bg-white border border-[#E5E7EB] rounded-xl p-4 shadow-sm flex flex-wrap items-center gap-2">
        <span className="text-xs text-[#6B7280] mr-1">状态看板</span>
        <button onClick={() => setFilter('all')}
          className={`px-3 py-1 rounded-full text-xs border transition-colors ${
            filter === 'all'
              ? 'bg-[#2563EB] border-[#2563EB] text-white'
              : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
          }`}>
          全部 {data.total ?? 0}
        </button>
        {STATUS_ORDER.filter(s => (board[s] ?? 0) > 0).map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1 rounded-full text-xs border transition-colors ${
              filter === s
                ? 'bg-[#2563EB] border-[#2563EB] text-white'
                : 'bg-white border-[#E5E7EB] text-[#6B7280] hover:bg-[#F9FAFB]'
            }`}>
            {labels[s] || s} {board[s]}
          </button>
        ))}
        <span className="text-[11px] text-[#9CA3AF] ml-2">
          结果由 Redis 真实执行记录实时核算 · 更新于 {formatTs(data.updated_at)}
        </span>
        <button onClick={load} className="ml-auto p-2 rounded-lg hover:bg-[#F3F4F6] text-[#6B7280]">
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* 台账列表 */}
      <div className="space-y-3">
        {shown.length === 0 && (
          <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm">
            <EmptyState icon={Wrench} title="暂无优化记录" hint="可在「错误诊断」页把错误组一键立项" />
          </div>
        )}
        {shown.map(item => (
          <OptCard key={item.id} item={item} onChanged={load} />
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// 单条优化卡片
// ═══════════════════════════════════════════════════════
function OptCard({ item, onChanged }: { item: OptItem; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState('');
  const r = item.result || ({} as OptResult);
  const meta = VERDICT_META[r.verdict] || VERDICT_META.no_data;
  const VIcon = meta.icon;

  const advance = async (status: string) => {
    let extra: Record<string, any> = {};
    if (status === 'rolled_back') {
      const reason = window.prompt('回滚原因（将记录到台账，方便复盘）:', '') ?? '';
      if (!reason.trim() && !window.confirm('未填写原因，仍要标记为已回滚？')) return;
      extra.rollback_reason = reason.trim() || '未填写原因';
    }
    setBusy(status);
    try {
      await postObs('optimizations', { id: item.id, status, ...extra });
      onChanged();
    } catch (e) { console.error(e); }
    setBusy('');
  };

  const nextStatus =
    item.status === 'planned' ? [['in_progress', '开始处理'], ['rejected', '否决(未落地)']] :
    item.status === 'in_progress' ? [['landed', '标记已落地'], ['planned', '重开(仅台账)']] :
    item.status === 'landed' ? [['verified', '确认已验证'], ['rolled_back', '标记已回滚']] :
    item.status === 'verified' ? [['rolled_back', '标记已回滚']] :
    [];

  // 推进按钮悬浮提示: 消歧“改台账 ≠ 改代码”
  const STATUS_TIP: Record<string, string> = {
    planned: '仅重置台账状态，不改动任何代码',
    rolled_back: '归档记录：代码已人工撤销（不会执行任何代码操作）',
    rejected: '归档记录：该方案未落地即被否决',
  };

  return (
    <div className="bg-white border border-[#E5E7EB] rounded-xl shadow-sm overflow-hidden">
      {/* 头部 */}
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <button onClick={() => setOpen(!open)}
            className="mt-0.5 p-1 rounded-lg hover:bg-[#F3F4F6] text-[#9CA3AF] flex-shrink-0">
            {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`px-2 py-0.5 rounded border text-[11px] font-medium ${
                STATUS_STYLE[item.status] || STATUS_STYLE.planned}`}>
                {item.status_label || item.status}
              </span>
              <span className="text-sm font-medium text-[#111827]">{item.title}</span>
              <SeverityBadge severity={item.severity} />
              {item.category && <CategoryBadge category={item.category} label={item.category} />}
            </div>
            <div className="text-[11px] text-[#9CA3AF] mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
              <span>负责模块 {item.owner || '-'}</span>
              <span>核算口径 {METRIC_LABEL[r.metric_type] || r.metric_type || '-'}</span>
              {item.landed_at ? <span>观测起点 {formatTs(item.landed_at)}</span> : null}
              {r.observed_days ? <span>已观测 {r.observed_days} 天</span> : null}
            </div>
          </div>
        </div>

        {/* 真实结果条 */}
        <div className={`mt-3 ml-9 rounded-xl border p-3 ${meta.style}`}>
          <div className="flex items-center gap-2 flex-wrap">
            <VIcon size={14} className="flex-shrink-0" />
            <span className="text-[13px] font-medium">{r.verdict_label || '数据不足'}</span>
            {r.data_available && r.before !== null && r.after !== null && (
              <span className="text-[12px] font-mono">
                {fmtValue(r.before, r.unit)} → {fmtValue(r.after, r.unit)}
                {typeof r.delta_pct === 'number' && (
                  <span className="ml-1">({r.delta_pct > 0 ? '+' : ''}{r.delta_pct}%)</span>
                )}
              </span>
            )}
            {typeof r.p95_after_ms === 'number' && r.p95_after_ms > 0 && (
              <span className="text-[11px] opacity-80">P95 {formatSec(r.p95_after_ms)}</span>
            )}
            {typeof r.sample_calls === 'number' && (
              <span className="text-[11px] opacity-80">样本 {r.sample_calls} 次</span>
            )}
            {typeof r.covered_calls === 'number' && r.covered_calls > 0 && (
              <span className="text-[11px] opacity-80">覆盖 {r.covered_calls} 次执行</span>
            )}
          </div>
          {r.detail && <div className="text-[12px] mt-1.5 leading-relaxed opacity-90">{r.detail}</div>}
        </div>

        {/* 代码漂移提示: 落地锚定后文件又变过 → 台账可能滞后 */}
        {item.drift && (
          <div className="mt-2 ml-9 rounded-lg bg-amber-50 border border-amber-200 px-3 py-1.5 text-[11px] text-amber-800 flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="flex items-center gap-1 font-medium">
              <AlertTriangle size={12} /> 代码漂移：涉及文件在标记落地后已再变更
            </span>
            <span className="font-mono">
              {[...(item.drift.changed || []), ...(item.drift.missing || []).map(m => `${m}(已删除)`)].join('、')}
            </span>
            {item.drift.git && <span className="text-amber-600">锚定 {item.drift.git}</span>}
            <span className="text-amber-600">台账可能与代码脱节，请核对后更新状态</span>
          </div>
        )}
      </div>

      {/* 详情 */}
      {open && (
        <div className="px-5 pb-4 pt-1 ml-9 border-t border-[#F3F4F6] space-y-3">
          <Section icon={AlertTriangle} color="text-red-500" title="问题现象" text={item.problem} />
          <Section icon={Target} color="text-purple-500" title="根因定位" text={item.root_cause} />
          <Section icon={Lightbulb} color="text-amber-500" title="优化方向" text={item.direction} />

          {(item.actions || []).length > 0 && (
            <div>
              <div className="text-[12px] font-medium text-[#374151] mb-1.5 flex items-center gap-1.5">
                <Wrench size={13} className="text-indigo-500" /> 落地动作
              </div>
              <ul className="space-y-1">
                {(item.actions || []).map((a, i) => (
                  <li key={i} className="text-[12px] text-[#4B5563] flex gap-1.5 leading-relaxed">
                    <span className="text-[#9CA3AF]">{i + 1}.</span>
                    <span>{a}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(item.files || []).length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-medium text-[#374151] flex items-center gap-1.5">
                <FileCode2 size={13} className="text-[#6B7280]" /> 涉及文件
              </span>
              {(item.files || []).map(f => (
                <code key={f} className="px-2 py-0.5 rounded bg-[#F3F4F6] text-[11px] text-[#374151] font-mono">
                  {f}
                </code>
              ))}
            </div>
          )}

          {item.verification && (
            <div className="rounded-xl border border-[#E5E7EB] bg-[#F9FAFB] p-3">
              <div className="text-[11px] font-medium text-[#6B7280] mb-1 flex items-center gap-1.5">
                <ClipboardCheck size={12} /> 验证证据
              </div>
              <div className="text-[12px] text-[#374151] leading-relaxed">{item.verification}</div>
            </div>
          )}

          {/* 回滚归档记录 */}
          {item.rollback_reason && (
            <div className="rounded-xl border border-red-100 bg-red-50 p-3">
              <div className="text-[11px] font-medium text-red-700 mb-1 flex items-center gap-1.5">
                <Undo2 size={12} /> 回滚归档
              </div>
              <div className="text-[12px] text-red-800 leading-relaxed">{item.rollback_reason}</div>
              {item.rolled_back_at && (
                <div className="text-[11px] text-red-400 mt-1">
                  {formatTs(item.rolled_back_at)}{item.rolled_back_by ? ` · ${item.rolled_back_by}` : ''}
                </div>
              )}
            </div>
          )}

          {/* 护栏观测: 落地前基线 vs 落地后的质量指标 (回归自动标红) */}
          {r.guardrail?.signals && Object.keys(r.guardrail.signals).length > 0 && (
            <div className="rounded-xl border border-[#E5E7EB] bg-[#F9FAFB] p-3">
              <div className="text-[11px] font-medium text-[#6B7280] mb-1.5 flex items-center gap-1.5">
                <Shield size={12} /> 护栏观测 (落地前 {r.guardrail.baseline_days ?? '-'} 天基线 vs 落地后)
              </div>
              <div className="space-y-1">
                {Object.entries(r.guardrail.signals).map(([sig, s]) => {
                  const bad = (r.guardrail?.regressions || []).includes(sig);
                  return (
                    <div key={sig}
                      className={`text-[12px] flex flex-wrap items-center gap-2 ${bad ? 'text-red-600 font-medium' : 'text-[#374151]'}`}>
                      <span>{s.label || sig}</span>
                      {s.before_pct !== null && s.after_pct !== null ? (
                        <span className="font-mono">{s.before_pct}% → {s.after_pct}%
                          {typeof s.delta_pp === 'number' && (
                            <span className={s.delta_pp > 0 ? 'text-red-500' : 'text-green-600'}>
                              {' '}({s.delta_pp > 0 ? '+' : ''}{s.delta_pp}pp)
                            </span>
                          )}
                        </span>
                      ) : (
                        <span className="text-[#9CA3AF]">样本不足</span>
                      )}
                      {bad && (
                        <span className="text-[10px] bg-red-50 border border-red-200 px-1.5 py-0.5 rounded">疑似回归</span>
                      )}
                      <span className="text-[10px] text-[#9CA3AF]">样本 {s.before_n} → {s.after_n}</span>
                    </div>
                  );
                })}
              </div>
              {!r.guardrail.sample_ok && (
                <div className="text-[11px] text-[#9CA3AF] mt-1.5">前后样本各需 ≥20 次才做回归判定</div>
              )}
            </div>
          )}

          {item.landed_at_basis && (
            <div className="text-[11px] text-[#9CA3AF] leading-relaxed">
              观测起点依据：{item.landed_at_basis}
            </div>
          )}

          {nextStatus.length > 0 && (
            <div className="flex items-center gap-2 pt-1">
              <span className="text-[11px] text-[#9CA3AF]">推进状态</span>
              {nextStatus.map(([s, label]) => (
                <button key={s} onClick={() => advance(s)} disabled={!!busy}
                  title={STATUS_TIP[s]}
                  className={`px-3 py-1.5 rounded-lg border text-[12px]
                              disabled:opacity-50 flex items-center gap-1.5 ${
                    s === 'rolled_back'
                      ? 'border-red-200 text-red-700 hover:bg-red-50'
                      : 'border-[#E5E7EB] text-[#374151] hover:bg-[#F9FAFB]'}`}>
                  {busy === s ? <Loader2 size={12} className="animate-spin" /> : null}
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Section({ icon: Icon, color, title, text }:
  { icon: any; color: string; title: string; text?: string }) {
  if (!text) return null;
  return (
    <div>
      <div className="text-[12px] font-medium text-[#374151] mb-1 flex items-center gap-1.5">
        <Icon size={13} className={color} /> {title}
      </div>
      <div className="text-[12px] text-[#4B5563] leading-relaxed">{text}</div>
    </div>
  );
}

function fmtValue(v: number, unit: string) {
  if (unit === 'ms') return formatSec(v);
  return `${v}${unit || ''}`;
}
