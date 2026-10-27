'use client';

/**
 * NMS 智能分析图表组件 (Phase 2.2)
 *
 * 数据来源: /api/nms/alarms (NmsLatest + NmsAlarm[]) 与 /api/nms/incidents (NmsIncident[])
 * 不发起新请求, 由父组件传入已加载的数据 — 保持「单数据源」与现有刷新节奏一致。
 *
 * 图表:
 *   - NmsKpiCards       4 张 KPI 卡 (活跃 / 未确认 / P0 / 事件簇, 事件簇卡含系统级/对象级拆分)
 *   - SeverityTrend     24h 严重度趋势 (按 first_time 小时聚合, LineChart)
 *   - TopSourcesBar     Top 8 受影响来源 (BarChart, 按 severity 堆叠)
 */

import { useMemo } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line, Legend, PieChart, Pie, Cell,
} from 'recharts';
import { AlertTriangle, BellRing, GitMerge, Radio, ShieldAlert, Network, Tag, Radar } from 'lucide-react';

export const SEV_COLORS: Record<string, string> = {
  critical: '#DC2626',
  major: '#EA580C',
  minor: '#D97706',
  warning: '#CA8A04',
  indeterminate: '#64748B',
  clear: '#16A34A',
  cleared: '#16A34A',
  unknown: '#94A3B8',
};

export const SEV_CN: Record<string, string> = {
  critical: '严重', major: '重要', minor: '次要',
  warning: '警告', indeterminate: '未知', cleared: '已清除', unknown: '未知',
};

const ACTIVE_SEV = ['critical', 'major', 'minor', 'warning', 'indeterminate'];

interface AlarmLike {
  severity: string;
  source?: string;
  first_time?: string;
  last_time?: string;
  time?: string;
  acknowledged?: string;
  status?: string;
}

export function isAcked(a: AlarmLike): boolean {
  return String(a.acknowledged ?? a.status ?? '') === '1';
}

// ─── KPI 卡 ───────────────────────────────────────────────────────────────────

export function NmsKpiCards(props: {
  alarms: AlarmLike[];
  incidents?: { category: 'object' | 'systemic'; member_count: number }[];
}) {
  const { alarms, incidents = [] } = props;
  const active = alarms.filter(a => ACTIVE_SEV.includes(a.severity));
  const unacked = active.filter(a => !isAcked(a));
  const p0 = active.filter(a => a.severity === 'critical' || a.severity === 'major');
  const sysCount = incidents.filter(i => i.category === 'systemic').length;

  const cards = [
    { label: '活跃告警', value: active.length, icon: BellRing, color: '#0F172A',
      hint: `共 ${alarms.length} 条 (含已清除)` },
    { label: '未确认', value: unacked.length, icon: AlertTriangle,
      color: unacked.length > 0 ? '#EA580C' : '#16A34A',
      hint: `已确认 ${active.length - unacked.length} 条` },
    { label: 'P0 (严重+重要)', value: p0.length, icon: ShieldAlert,
      color: p0.length > 0 ? '#DC2626' : '#16A34A',
      hint: 'deterministic 引擎定级' },
    { label: '事件簇', value: incidents.length, icon: GitMerge, color: '#7C3AED',
      hint: incidents.length
        ? `系统级 ${sysCount} / 对象级 ${incidents.length - sysCount}`
        : '关联降噪归并结果' },
  ];
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {cards.map(c => {
        const Icon = c.icon;
        return (
          <div key={c.label}
            className="rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: `${c.color}12` }}>
              <Icon size={17} style={{ color: c.color }} />
            </div>
            <div className="min-w-0">
              <div className="text-[22px] font-bold leading-none" style={{ color: c.color }}>
                {c.value}
              </div>
              <div className="text-[11px] text-[#64748B] mt-1 truncate">{c.label}</div>
              <div className="text-[10px] text-[#94A3B8] truncate">{c.hint}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── 严重度趋势 (24h, 按 first_time 小时聚合) ─────────────────────────────────

export function SeverityTrend(props: { alarms: AlarmLike[]; hours?: number }) {
  const { alarms, hours = 24 } = props;
  const data = useMemo(() => {
    const now = new Date();
    const start = new Date(now.getTime() - hours * 3600_000);
    // 初始化每个小时的桶 (保证空小时也有数据点)
    const buckets: Record<string, Record<string, number>> = {};
    for (let i = 0; i < hours; i++) {
      const h = new Date(start.getTime() + i * 3600_000);
      const key = `${String(h.getHours()).padStart(2, '0')}:00`;
      buckets[key] = { critical: 0, major: 0, minor: 0, warning: 0 };
    }
    const keys = Object.keys(buckets);
    for (const a of alarms) {
      const ts = a.first_time || a.last_time || a.time || '';
      if (!ts) continue;
      const t = new Date(ts);
      if (isNaN(t.getTime()) || t < start || t > now) continue;
      const key = `${String(t.getHours()).padStart(2, '0')}:00`;
      if (buckets[key] && a.severity in buckets[key]) buckets[key][a.severity]++;
    }
    return keys.map(k => ({ hour: k, ...buckets[k] }));
  }, [alarms, hours]);

  const sevs = ['critical', 'major', 'minor', 'warning'];
  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4">
      <div className="text-[13px] font-semibold text-[#0F172A] mb-1">
        严重度趋势 <span className="text-[11px] font-normal text-[#94A3B8]">近 {hours} 小时新增</span>
      </div>
      <div className="h-[180px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#F1F5F9" />
            <XAxis dataKey="hour" tick={{ fontSize: 10, fill: '#94A3B8' }}
              interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: '#94A3B8' }} allowDecimals={false} />
            <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #E2E8F0' }} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {sevs.map(s => (
              <Line key={s} type="monotone" dataKey={s} name={SEV_CN[s] || s}
                stroke={SEV_COLORS[s]} strokeWidth={1.6} dot={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ─── Top 受影响来源 (堆叠柱) ──────────────────────────────────────────────────

export function TopSourcesBar(props: { alarms: AlarmLike[]; topN?: number }) {
  const { alarms, topN = 8 } = props;
  const data = useMemo(() => {
    const bySrc: Record<string, Record<string, number>> = {};
    for (const a of alarms) {
      if (!ACTIVE_SEV.includes(a.severity)) continue;
      const src = (a.source || '').trim() || '(未知来源)';
      if (!bySrc[src]) bySrc[src] = {};
      bySrc[src][a.severity] = (bySrc[src][a.severity] || 0) + 1;
    }
    const total = (m: Record<string, number>) =>
      Object.values(m).reduce((s, n) => s + n, 0);
    return Object.entries(bySrc)
      .map(([src, m]) => ({ src, ...m, _total: total(m) }))
      .sort((a, b) => b._total - a._total)
      .slice(0, topN);
  }, [alarms, topN]);

  const sevs = ['critical', 'major', 'minor', 'warning', 'indeterminate'];
  // 长源名 (BGP Peer 字段 + 乱码) 截断在 Y 轴显示, 完整内容在 tooltip 呈现
  const truncateSrc = (s: string, max = 22) =>
    s.length > max ? s.slice(0, max - 1) + '…' : s;

  // 自定义 tooltip: 显示完整源名 + 总数 + 分级别明细 (按严重度倒序, 0 给最大)
  const CustomTooltip = ({ active, payload }: any) => {
    if (!active || !payload?.length) return null;
    const row = payload[0].payload as Record<string, any>;
    const activeSevs = sevs
      .filter(s => (row[s] || 0) > 0)
      .sort((a, b) => (row[b] || 0) - (row[a] || 0));
    return (
      <div className="bg-white border border-slate-200 shadow-xl rounded-lg p-3 text-xs min-w-[200px] max-w-[360px]">
        <div className="font-semibold text-slate-900 mb-0.5 break-all text-[13px]">
          {row.src}
        </div>
        <div className="text-slate-500 text-[11px] mb-2">
          共 <strong className="text-slate-900">{row._total}</strong> 条活跃告警
        </div>
        <div className="space-y-1">
          {activeSevs.map(s => (
            <div key={s} className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0"
                style={{ background: SEV_COLORS[s] }} />
              <span className="text-slate-600">{SEV_CN[s]}</span>
              <span className="font-mono font-medium text-slate-900 ml-auto tabular-nums">
                {row[s]}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4">
      <div className="text-[13px] font-semibold text-[#0F172A] mb-1">
        Top {topN} 受影响来源
      </div>
      {data.length === 0
        ? <div className="h-[180px] flex items-center justify-center text-xs text-[#94A3B8]">暂无活跃告警</div>
        : <div className="h-[180px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} layout="vertical" barCategoryGap="15%"
              margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="src" width={140} interval={0}
                tickLine={false} axisLine={false}
                tick={{ fontSize: 10, fill: '#475569' }}
                tickFormatter={(v: string) => truncateSrc(v)} />
              <Tooltip cursor={{ fill: 'rgba(124, 58, 237, 0.06)' }} content={<CustomTooltip />} />
              {sevs.map(s => (
                <Bar key={s} dataKey={s} name={SEV_CN[s] || s} stackId="a"
                  fill={SEV_COLORS[s]} radius={2} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>}
    </div>
  );
}

// ─── 事件簇可视化 (事件视图专用) ──────────────────────────────────────────────
// 数据来源: /api/nms/incidents 返回的 incident 列表, 只做展示, 不额外请求。

export interface IncidentLike {
  incident_id: string;
  title: string;
  severity: string;
  category: 'object' | 'systemic';
  member_count: number;
  source_count: number;
  sources?: string[];
  groups?: string[];
  first_time?: string;
  last_time?: string;
  acked_count?: number;
  unacked_count?: number;
  new_count?: number;
  priority?: number;
  segments?: { cidr: string; count: number }[];
  types?: { name: string; count: number }[];
  scope?: IncidentScope;
}

export type IncidentScope = 'cross' | 'site' | 'object';

/** 事件范围口径：跨站点 / 同站点多对象 / 单对象 */
export const SCOPE_META: Record<IncidentScope, { label: string; color: string; bg: string; border: string }> = {
  cross: { label: '跨站点', color: '#6D28D9', bg: '#F5F3FF', border: '#DDD6FE' },
  site: { label: '同站点', color: '#C2410C', bg: '#FFF7ED', border: '#FED7AA' },
  object: { label: '单对象', color: '#475569', bg: '#F8FAFC', border: '#E2E8F0' },
};

const INC_SEV_ORDER = ['critical', 'major', 'minor', 'warning', 'indeterminate'];

/** NMS 时间字符串 → epoch ms。兼容 'YYYY-MM-DD HH:mm:ss' / ISO(+08:00) / 'M/D/YY, h:MM AM/PM'。 */
export function parseNmsTime(s?: string | null): number | null {
  if (!s) return null;
  let t = String(s).trim();
  if (!t) return null;
  // 'YYYY-MM-DD HH:mm:ss' → ISO 本地时间 (浏览器按本地时区解析)
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(t)) t = t.replace(' ', 'T');
  t = t.replace(/\.\d+/, '');
  const ms = Date.parse(t);
  if (!isNaN(ms)) return ms;
  const m = t.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?$/i);
  if (m) {
    let hour = parseInt(m[4], 10);
    const ap = (m[7] || '').toUpperCase();
    if (ap === 'PM' && hour < 12) hour += 12;
    if (ap === 'AM' && hour === 12) hour = 0;
    let year = parseInt(m[3], 10);
    if (year < 100) year += 2000;
    const d = new Date(year, parseInt(m[1], 10) - 1, parseInt(m[2], 10),
      hour, parseInt(m[5], 10), m[6] ? parseInt(m[6], 10) : 0);
    return isNaN(d.getTime()) ? null : d.getTime();
  }
  return null;
}

/** 毫秒时长 → 紧凑文案 (45m / 3h20m / 2d4h) */
export function fmtDuration(ms: number): string {
  if (!isFinite(ms) || ms <= 0) return '—';
  if (ms < 60_000) return '<1m';
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60), rm = mins % 60;
  if (h < 24) return rm ? `${h}h${rm}m` : `${h}h`;
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d}d${rh}h` : `${d}d`;
}

// ─── 事件概览指标条 ─────────────────────────────────────────────────────────

export function IncidentKpiStrip({ incidents }: { incidents: IncidentLike[] }) {
  const total = incidents.length;
  const sysCount = incidents.filter(i => i.category === 'systemic').length;
  const objCount = total - sysCount;
  const members = incidents.reduce((s, i) => s + (i.member_count || 0), 0);
  const acked = incidents.reduce((s, i) => s + (i.acked_count || 0), 0);
  const unacked = incidents.reduce((s, i) => s + (i.unacked_count || 0), 0);
  const newCount = incidents.reduce((s, i) => s + (i.new_count || 0), 0);
  const srcSet = new Set<string>();
  incidents.forEach(i => (i.sources || []).forEach(s => { if (s) srcSet.add(s); }));
  const ackPct = members > 0 ? Math.round((acked / members) * 100) : 0;

  type Tile = {
    label: string; value: number; icon: typeof GitMerge; color: string;
    hint: string; bar?: { n: number; color: string }[];
  };
  const tiles: Tile[] = [
    { label: '事件簇', value: total, icon: GitMerge, color: '#7C3AED',
      hint: `系统级 ${sysCount} · 对象级 ${objCount}`,
      bar: total ? [{ n: sysCount, color: '#4F46E5' }, { n: objCount, color: '#CBD5E1' }] : undefined },
    { label: '涉及告警', value: members, icon: BellRing, color: '#0F172A',
      hint: `已确认 ${acked} · 未确认 ${unacked}` },
    { label: '未确认告警', value: unacked, icon: AlertTriangle,
      color: unacked > 0 ? '#EA580C' : '#16A34A',
      hint: `占全部告警 ${100 - ackPct}%` },
    { label: '受影响来源', value: srcSet.size, icon: Radio, color: '#0284C7',
      hint: total ? `平均每个事件 ${(incidents.reduce((s, i) => s + (i.source_count || 0), 0) / total).toFixed(1)} 个来源` : '暂无数据' },
  ];

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {tiles.map(c => {
        const Icon = c.icon;
        return (
          <div key={c.label}
            className="rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: `${c.color}12` }}>
              <Icon size={17} style={{ color: c.color }} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-[22px] font-bold leading-none" style={{ color: c.color }}>
                {c.value}
              </div>
              <div className="text-[11px] text-[#64748B] mt-1 truncate">
                {c.label}
                {c.label === '未确认告警' && newCount > 0 && (
                  <span className="ml-1 text-[10px] text-[#DC2626]">· 本轮新增 {newCount}</span>
                )}
              </div>
              <div className="text-[10px] text-[#94A3B8] truncate">{c.hint}</div>
            </div>
            {c.bar && c.bar.some(b => b.n > 0) && (
              <div className="w-10 shrink-0">
                <div className="flex h-1 rounded-full overflow-hidden bg-[#F1F5F9]">
                  {c.bar.map((b, bi) => (
                    <div key={bi} style={{ width: `${(b.n / Math.max(1, c.value)) * 100}%`, background: b.color }} />
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── 事件级别分布 (环图 + 可点击图例) ─────────────────────────────────────────

export function IncidentSeverityDonut(props: {
  incidents: IncidentLike[];
  active?: Set<string>;
  onToggle?: (severity: string) => void;
}) {
  const { incidents, active, onToggle } = props;
  const rows = useMemo(() => {
    const acc: Record<string, number> = {};
    incidents.forEach(i => {
      const s = i.severity || 'unknown';
      acc[s] = (acc[s] || 0) + 1;
    });
    return INC_SEV_ORDER.filter(s => (acc[s] || 0) > 0)
      .map(s => ({ key: s, name: SEV_CN[s] || s, value: acc[s], color: SEV_COLORS[s] }));
  }, [incidents]);
  const total = rows.reduce((a, b) => a + b.value, 0);

  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 flex flex-col min-w-0">
      <div className="text-[13px] font-semibold text-[#0F172A]">事件级别分布</div>
      <div className="text-[11px] text-[#94A3B8] mb-1">按事件簇最高级别统计 · 点击图例可筛选</div>
      {total === 0 ? (
        <div className="flex-1 min-h-[150px] flex items-center justify-center text-xs text-[#94A3B8]">暂无事件</div>
      ) : (
        <div className="flex items-center gap-2 flex-1 min-h-0">
          <div className="relative w-[148px] h-[148px] shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={rows} dataKey="value" nameKey="name" startAngle={90} endAngle={-270}
                  innerRadius={48} outerRadius={68} paddingAngle={2} stroke="none" isAnimationActive={false}>
                  {rows.map(r => <Cell key={r.key} fill={r.color}
                    opacity={!active || active.size === 0 || active.has(r.key) ? 1 : 0.28} />)}
                </Pie>
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #E2E8F0' }}
                  formatter={(value) => `${value} 个事件`} />
              </PieChart>
            </ResponsiveContainer>
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
              <span className="text-[24px] font-bold leading-none text-[#0F172A]">{total}</span>
              <span className="text-[10px] text-[#94A3B8] mt-1">事件</span>
            </div>
          </div>
          <div className="flex-1 min-w-0 space-y-0.5">
            {rows.map(r => {
              const on = !!active?.has(r.key);
              const pct = Math.round((r.value / total) * 100);
              return (
                <button key={r.key} onClick={() => onToggle?.(r.key)} title={`筛选 ${r.name}`}
                  className={`w-full flex items-center gap-2 px-1.5 py-1 rounded-md text-left transition-colors ${
                    on ? 'bg-[#F1F5F9]' : 'hover:bg-[#F8FAFC]'}`}>
                  <span className="w-2 h-2 rounded-sm shrink-0" style={{ background: r.color }} />
                  <span className={`text-[11px] truncate ${on ? 'text-[#0F172A] font-medium' : 'text-[#475569]'}`}>{r.name}</span>
                  <span className="ml-auto text-[11px] font-mono font-medium text-[#0F172A] tabular-nums">{r.value}</span>
                  <span className="text-[10px] text-[#94A3B8] w-8 text-right tabular-nums">{pct}%</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── 事件时间轴 (首次→最后出现的跨度条) ───────────────────────────────────────

export function IncidentTimeline(props: { incidents: IncidentLike[]; maxRows?: number }) {
  const { incidents, maxRows = 8 } = props;
  const view = useMemo(() => {
    const parsed = incidents.map(i => {
      const s = parseNmsTime(i.first_time) ?? parseNmsTime(i.last_time);
      if (s == null) return null;
      const e = parseNmsTime(i.last_time) ?? s;
      return { inc: i, s, e: Math.max(e, s) };
    }).filter((x): x is { inc: IncidentLike; s: number; e: number } => !!x);
    // 取优先级最高的 N 个事件, 再按首次出现时间排成时间线
    const picked = [...parsed]
      .sort((a, b) => (b.inc.priority || 0) - (a.inc.priority || 0))
      .slice(0, maxRows)
      .sort((a, b) => a.s - b.s);
    if (!picked.length) return null;
    const lo0 = Math.min(...picked.map(r => r.s));
    const hi0 = Math.max(...picked.map(r => r.e));
    const span = Math.max(60_000, hi0 - lo0);
    const pad = span * 0.02;
    const lo = lo0 - pad, hi = hi0 + pad;
    return { lo, hi, rows: picked, showDate: hi - lo > 36 * 3600_000 };
  }, [incidents, maxRows]);

  const fmtTick = (t: number, withDate: boolean) => {
    const d = new Date(t);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    if (!withDate) return `${hh}:${mm}`;
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hh}:${mm}`;
  };

  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 flex flex-col min-w-0">
      <div className="text-[13px] font-semibold text-[#0F172A]">事件时间轴</div>
      <div className="text-[11px] text-[#94A3B8] mb-2">
        横轴为首次 → 最后出现时间 · 展示优先级最高 {maxRows} 个事件
      </div>
      {!view ? (
        <div className="flex-1 min-h-[150px] flex items-center justify-center text-xs text-[#94A3B8]">暂无可解析的事件时间</div>
      ) : (
        <>
          <div className="space-y-1.5 flex-1">
            {view.rows.map(({ inc, s, e }) => {
              const left = Math.max(0, ((s - view.lo) / (view.hi - view.lo)) * 100);
              const width = Math.max(1.2, ((e - s) / (view.hi - view.lo)) * 100);
              const color = SEV_COLORS[inc.severity] || '#94A3B8';
              return (
                <div key={inc.incident_id} className="flex items-center gap-2">
                  <div className="w-5 shrink-0 text-[10px] font-mono text-[#94A3B8] text-right tabular-nums"
                    title={`priority ${inc.priority ?? '-'}`}>{inc.priority ?? '-'}</div>
                  <div className="w-[132px] shrink-0 text-[11px] text-[#475569] truncate" title={inc.title}>
                    {inc.title}
                  </div>
                  <div className="relative flex-1 h-4 rounded bg-[#F8FAFC]">
                    <div className="absolute top-1 bottom-1 rounded-[3px] hover:brightness-110 transition-[filter] cursor-default"
                      style={{ left: `${left}%`, width: `${width}%`, background: color }}
                      title={`${inc.title}\n首: ${inc.first_time || '-'}\n末: ${inc.last_time || '-'}\n持续: ${fmtDuration(e - s)} · ${inc.member_count} 条告警 · ${inc.source_count} 个来源`} />
                  </div>
                  <div className="w-[42px] shrink-0 text-right text-[10px] font-mono text-[#94A3B8] tabular-nums">
                    {fmtDuration(e - s)}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <div className="w-5 shrink-0" />
            <div className="w-[132px] shrink-0" />
            <div className="relative flex-1 h-3">
              {[0, 1, 2, 3, 4].map(i => {
                const t = view.lo + ((view.hi - view.lo) * i) / 4;
                return (
                  <span key={i}
                    className={`absolute top-0 text-[9px] font-mono text-[#94A3B8] ${i === 0 ? '' : i === 4 ? '-translate-x-full' : '-translate-x-1/2'}`}
                    style={{ left: `${i * 25}%` }}>
                    {fmtTick(t, view.showDate)}
                  </span>
                );
              })}
            </div>
           <div className="w-[42px] shrink-0" />
          </div>
        </>
      )}
    </div>
  );
}

// ─── 站点 / 服务关联分析（网段 × 类型，纯规则定性）───────────────────────────

export interface ScopeAnalysis {
  active_total: number;
  segment_total: number;
  type_total: number;
  no_ip: number;
  segments: { cidr: string; count: number; share: number; types: number; sources: number }[];
  types: { name: string; count: number; share: number; segments: number; sources: number }[];
  matrix: {
    segments: string[];
    types: string[];
    counts: number[][];
    row_totals: number[];
    col_totals: number[];
  } | null;
  judgement: {
    primary: 'site' | 'service' | 'mixed' | 'scattered' | 'none';
    site_score: number;
    service_score: number;
    text: string;
  };
}

const JUDGE_META: Record<string, { label: string; color: string; bg: string; border: string }> = {
  site: { label: '站点异常主导', color: '#C2410C', bg: '#FFF7ED', border: '#FED7AA' },
  service: { label: '服务异常主导', color: '#6D28D9', bg: '#F5F3FF', border: '#DDD6FE' },
  mixed: { label: '站点聚集 + 服务扩散', color: '#B45309', bg: '#FFFBEB', border: '#FDE68A' },
  scattered: { label: '告警分散', color: '#475569', bg: '#F8FAFC', border: '#E2E8F0' },
  none: { label: '暂无数据', color: '#94A3B8', bg: '#F8FAFC', border: '#E2E8F0' },
};

/** 关联定性条：站点聚集度 / 服务扩散度 + 结论 + 事件范围计数 */
export function IncidentScopeJudgement(props: {
  analysis: ScopeAnalysis;
  incidents: IncidentLike[];
  scopeLabel?: string;
}) {
  const { analysis, incidents, scopeLabel } = props;
  const j = analysis.judgement;
  const meta = JUDGE_META[j.primary] || JUDGE_META.none;
  const scopeCount = { cross: 0, site: 0, object: 0 };
  incidents.forEach(i => { if (i.scope) scopeCount[i.scope] += 1; });
  const fmt = (n: number) => n.toLocaleString('zh-CN');

  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4">
      <div className="flex items-start gap-6 flex-wrap">
        <div className="min-w-[260px] flex-1">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold text-[#0F172A]">
            <Radar size={14} className="text-[#DC2626]" /> 站点 / 服务关联定性
          </div>
          <div className="text-[11px] text-[#94A3B8] mt-0.5 mb-2 flex items-center gap-1.5 flex-wrap">
            <span>按 IP 网段 × 告警类型统计活跃告警 · 纯规则判定，不依赖模型</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#F8FAFC] border border-[#E2E8F0] text-[#64748B] max-w-[220px] truncate"
              title={`分析口径：${scopeLabel || '全量快照'}`}>
              口径：{scopeLabel || '全量快照'}
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded-md border font-medium"
              style={{ color: meta.color, background: meta.bg, borderColor: meta.border }}>
              {meta.label}
            </span>
            <span className="text-[12px] text-[#334155] leading-5">{j.text}</span>
          </div>
        </div>
        <div className="w-[228px] shrink-0 space-y-2">
          {[
            { label: '站点聚集度', score: j.site_score, color: '#EA580C' },
            { label: '服务扩散度', score: j.service_score, color: '#7C3AED' },
          ].map(r => (
            <div key={r.label}>
              <div className="flex items-center justify-between text-[10px] mb-0.5">
                <span className="text-[#64748B]">{r.label}</span>
                <span className="font-mono font-medium tabular-nums" style={{ color: r.color }}>{r.score}</span>
              </div>
              <div className="h-1.5 rounded-full bg-[#F1F5F9] overflow-hidden">
                <div className="h-full rounded-full"
                  style={{ width: `${Math.min(100, Math.max(0, r.score))}%`, background: r.color }} />
              </div>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-x-5 gap-y-1.5 shrink-0">
          {[
            { label: '活跃告警', value: analysis.active_total },
            { label: '来源网段', value: analysis.segment_total },
            { label: '告警类型', value: analysis.type_total },
            { label: '未定位 IP', value: analysis.no_ip },
          ].map(s => (
            <div key={s.label} className="flex items-baseline gap-1.5">
              <span className="text-[15px] font-semibold font-mono tabular-nums text-[#0F172A]">{fmt(s.value)}</span>
              <span className="text-[10px] text-[#94A3B8]">{s.label}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 pt-2.5 border-t border-[#F1F5F9] flex items-center gap-3 flex-wrap text-[10px] text-[#64748B]">
        <span className="text-[#94A3B8]">事件范围</span>
        {(['cross', 'site', 'object'] as IncidentScope[]).map(k => (
          <span key={k} className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-sm" style={{ background: SCOPE_META[k].color }} />
            {SCOPE_META[k].label}
            <strong className="font-mono tabular-nums text-[#334155]">{scopeCount[k]}</strong>
          </span>
        ))}
        <span className="ml-auto text-[#94A3B8]">跨站点 = 事件涉及 ≥2 个 /24 网段</span>
      </div>
    </div>
  );
}

/** IP 网段 Top：条长=告警量，类型数越多越像站点性故障；点击行筛选事件 */
export function IncidentSegmentBars({ analysis, active, onSelect }: {
  analysis: ScopeAnalysis;
  active?: string | null;
  onSelect?: (cidr: string) => void;
}) {
  const rows = analysis.segments;
  const max = Math.max(1, ...rows.map(r => r.count));
  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 min-w-0">
      <div className="flex items-center gap-1.5 text-[13px] font-semibold text-[#0F172A]">
        <Network size={14} className="text-[#EA580C]" /> IP 网段 Top {rows.length || 10}
        {onSelect && <span className="ml-auto text-[10px] font-normal text-[#94A3B8]">点击行筛选</span>}
      </div>
      <div className="text-[11px] text-[#94A3B8] mt-0.5 mb-2.5">
        按设备/来源 IP 归并 /24 · 单网段类型越多越像站点性故障
      </div>
      {rows.length === 0 ? (
        <div className="h-[160px] flex items-center justify-center text-xs text-[#94A3B8]">暂无网段数据</div>
      ) : (
        <div className="space-y-0.5">
          {rows.map(r => {
            const siteLike = r.types >= 3;
            const on = active === r.cidr;
            return (
              <button key={r.cidr} type="button" onClick={() => onSelect?.(r.cidr)}
                className={`w-full flex items-center gap-2 rounded-md px-1 py-[3px] -mx-1 text-left transition-colors ${
                  on ? 'bg-[#FFF7ED] ring-1 ring-inset ring-[#FDBA74]' : 'hover:bg-[#F8FAFC]'}`}
                title={`${r.cidr} · ${r.count} 条 · ${r.share}% · ${r.types} 类告警 · ${r.sources} 个来源${onSelect ? ' · 点击筛选该网段事件' : ''}`}>
                <span className={`w-[102px] shrink-0 font-mono text-[10px] truncate ${on ? 'text-[#C2410C] font-semibold' : 'text-[#334155]'}`}>{r.cidr}</span>
                <div className="flex-1 min-w-0 h-2.5 rounded-full bg-[#F8FAFC] overflow-hidden">
                  <div className="h-full rounded-full"
                    style={{ width: `${Math.max(2, (r.count / max) * 100)}%`, background: siteLike ? '#EA580C' : '#94A3B8' }} />
                </div>
                <span className="w-11 shrink-0 text-right text-[10px] font-mono tabular-nums text-[#0F172A]">{r.count.toLocaleString('zh-CN')}</span>
                <span className={`w-[74px] shrink-0 text-right text-[9px] tabular-nums ${siteLike ? 'text-[#C2410C]' : 'text-[#94A3B8]'}`}>
                  {r.types} 类 · {r.sources} 源
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** 告警类型 Top：跨网段越多越像服务/协议面故障；点击行筛选事件 */
export function IncidentTypeBars({ analysis, active, onSelect }: {
  analysis: ScopeAnalysis;
  active?: string | null;
  onSelect?: (name: string) => void;
}) {
  const rows = analysis.types;
  const max = Math.max(1, ...rows.map(r => r.count));
  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 min-w-0">
      <div className="flex items-center gap-1.5 text-[13px] font-semibold text-[#0F172A]">
        <Tag size={14} className="text-[#7C3AED]" /> 告警类型 Top {rows.length || 10}
        {onSelect && <span className="ml-auto text-[10px] font-normal text-[#94A3B8]">点击行筛选</span>}
      </div>
      <div className="text-[11px] text-[#94A3B8] mt-0.5 mb-2.5">
        按 EventGroup 归并 · 跨网段越广越像服务/协议面故障
      </div>
      {rows.length === 0 ? (
        <div className="h-[160px] flex items-center justify-center text-xs text-[#94A3B8]">暂无类型数据</div>
      ) : (
        <div className="space-y-0.5">
          {rows.map(r => {
            const spread = r.segments >= 3;
            const on = active === r.name;
            return (
              <button key={r.name} type="button" onClick={() => onSelect?.(r.name)}
                className={`w-full flex items-center gap-2 rounded-md px-1 py-[3px] -mx-1 text-left transition-colors ${
                  on ? 'bg-[#F5F3FF] ring-1 ring-inset ring-[#C4B5FD]' : 'hover:bg-[#F8FAFC]'}`}
                title={`${r.name} · ${r.count} 条 · 跨 ${r.segments} 个网段 · ${r.sources} 个来源${onSelect ? ' · 点击筛选该类型事件' : ''}`}>
                <span className={`w-[122px] shrink-0 text-[10px] truncate ${on ? 'text-[#6D28D9] font-semibold' : 'text-[#334155]'}`}>{r.name}</span>
                <div className="flex-1 min-w-0 h-2.5 rounded-full bg-[#F8FAFC] overflow-hidden">
                  <div className="h-full rounded-full"
                    style={{ width: `${Math.max(2, (r.count / max) * 100)}%`, background: spread ? '#7C3AED' : '#94A3B8' }} />
                </div>
                <span className="w-11 shrink-0 text-right text-[10px] font-mono tabular-nums text-[#0F172A]">{r.count.toLocaleString('zh-CN')}</span>
                <span className={`w-[74px] shrink-0 text-right text-[9px] tabular-nums ${spread ? 'text-[#6D28D9]' : 'text-[#94A3B8]'}`}>
                  {r.segments} 段 · {r.sources} 源
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** 网段 × 类型 热力矩阵：横向连片=服务扩散，纵向连片=站点聚集；点击格子筛选「网段+类型」 */
export function IncidentDimensionMatrix({ analysis, active, onSelect }: {
  analysis: ScopeAnalysis;
  active?: { cidr: string; type: string } | null;
  onSelect?: (cidr: string, type: string) => void;
}) {
  const m = analysis.matrix;
  if (!m || !m.segments.length) {
    return (
      <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 min-w-0">
        <div className="text-[13px] font-semibold text-[#0F172A]">网段 × 类型 关联矩阵</div>
        <div className="h-[160px] flex items-center justify-center text-xs text-[#94A3B8]">暂无关联数据</div>
      </div>
    );
  }
  const max = Math.max(1, ...m.counts.flat());

  return (
    <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 min-w-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold text-[#0F172A]">
            网段 × 类型 关联矩阵
            {onSelect && <span className="text-[10px] font-normal text-[#94A3B8]">点击格子筛选</span>}
          </div>
          <div className="text-[11px] text-[#94A3B8] mt-0.5">
            横向连片 = 同类告警跨站点（服务扩散）· 纵向连片 = 同站点多类型（站点聚集）
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0 pt-0.5">
          <span className="text-[9px] text-[#94A3B8]">少</span>
          {[0.08, 0.18, 0.3, 0.42, 0.56].map(a => (
            <span key={a} className="w-2.5 h-2.5 rounded-[2px]" style={{ background: `rgba(220,38,38,${a})` }} />
          ))}
          <span className="text-[9px] text-[#94A3B8]">多</span>
        </div>
      </div>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[560px] table-fixed border-separate" style={{ borderSpacing: '2px' }}>
          <thead>
            <tr>
              <th className="w-[104px]" />
              {m.types.map(t => {
                const colOn = active?.type === t;
                return (
                  <th key={t} className="align-bottom pb-0.5" title={t}>
                    <div className={`text-[9px] leading-3 truncate px-0.5 ${colOn ? 'text-[#6D28D9] font-semibold' : 'text-[#64748B]'}`}>{t}</div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {m.segments.map((s, i) => {
              const rowOn = active?.cidr === s;
              return (
                <tr key={s}>
                  <td className="pr-1">
                    <div className={`text-[10px] font-mono truncate ${s === '其他' ? 'text-[#94A3B8]' : rowOn ? 'text-[#C2410C] font-semibold' : 'text-[#334155]'}`}
                      title={s}>{s}</div>
                  </td>
                  {m.counts[i].map((v, j) => {
                    const t = m.types[j];
                    const alpha = v === 0 ? 0 : 0.06 + 0.54 * (v / max);
                    const clickable = v > 0 && !!onSelect;
                    const selected = active?.cidr === s && active?.type === t;
                    return (
                      <td key={j} className="p-0">
                        <button type="button" disabled={!clickable}
                          onClick={() => onSelect?.(s, t)}
                          className={`w-full h-full rounded-[2px] text-center transition-[filter] ${
                            clickable ? 'cursor-pointer hover:brightness-95' : 'cursor-default'}`}
                          style={{
                            background: v === 0 ? '#F8FAFC' : `rgba(220,38,38,${alpha})`,
                            color: alpha > 0.36 ? '#FFFFFF' : '#475569',
                            boxShadow: selected ? 'inset 0 0 0 2px #0F172A' : undefined,
                          }}
                          title={`${s} × ${t}：${v} 条${clickable ? ' · 点击筛选该组合事件' : ''}`}>
                          <div className="text-[9px] font-mono tabular-nums leading-[22px]">{v === 0 ? '' : v}</div>
                        </button>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            <tr>
              <td className="text-[9px] text-[#94A3B8] pt-1">合计</td>
              {m.col_totals.map((v, j) => (
                <td key={j} className="text-center pt-1">
                  <span className="text-[9px] font-mono tabular-nums text-[#64748B]">{v}</span>
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 flex items-center justify-between gap-3 text-[9px] text-[#94A3B8]">
        <span>「其他」= 未进入 Top7 的网段/类型，同样可点击筛选</span>
        {analysis.no_ip > 0 && (
          <span className="shrink-0">未定位 IP 的 {analysis.no_ip} 条未计入矩阵</span>
        )}
      </div>
    </div>
  );
}
