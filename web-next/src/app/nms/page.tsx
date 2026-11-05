'use client';

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Activity, RefreshCw, CheckCircle, XCircle, Clock, ChevronRight,
  Search, Radio, Layers, Bell, BarChart3, CheckSquare, Square, X,
  GitMerge, BookOpen, ShieldAlert, Pencil, Trash2, Plus, Network, Tag,
} from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';
import { NmsKpiCards, SeverityTrend, TopSourcesBar,
  IncidentKpiStrip, IncidentSeverityDonut, IncidentTimeline,
  IncidentScopeJudgement, IncidentSegmentBars, IncidentTypeBars,
  IncidentDimensionMatrix, SCOPE_META,
  parseNmsTime, fmtDuration } from '@/components/NmsCharts';
import type { ScopeAnalysis } from '@/components/NmsCharts';
import NmsSslInvestigation from '@/components/NmsSslInvestigation';
import type { SslPanelTarget } from '@/components/NmsSslInvestigation';

// ─── Types ────────────────────────────────────────────────────────────────────

interface NmsAlarm {
  alarm_id: string;
  group?: string;
  groups?: string[];
  view_group?: string;
  severity: string;
  summary: string;
  source: string;
  node?: string;
  time: string;
  first_time?: string;
  status: string;
  state?: string;
  node_alias?: string;
  last_time?: string;
  acknowledged?: string;
  alert_key?: string;
  event_key?: string;
  event_group?: string;
  case_id?: string;
  state_change?: string;
  create_time?: string;
  flash?: string;
  owner_gid?: string;
  manager?: string;
  agent?: string;
  identifier?: string;
  aclass?: string;
  atype?: string;
  grade?: string;
  location?: string;
  owner?: string;
  count?: string;
  expire_time?: string;
  server_name?: string;
  server_serial?: string;
  alarm_type?: string;
  serial?: string;
  event_id?: string;
  checklist?: string;
  occurrences?: number;  // 历史事件累计发生次数 (内容键去重, Serial 换手合并)
  fields?: Record<string, string>;
  raw?: string;
}

interface NmsLatest {
  timestamp: string;
  total: number;
  alarms: NmsAlarm[];
  returned?: number;
  filtered_total?: number;
  page?: number;
  page_size?: number;
  has_more?: boolean;
  severity_counts?: Record<string, number>;
  group_counts?: Record<string, number>;
  group_counts_active?: Record<string, number>;
  active_total?: number;
  cleared_total?: number;
  // SSL 调查创建时用于绑定快照（服务端重算校验，客户端不可指定 base）
  snapshot_ref?: string;
}

interface NmsAnalysis {
  timestamp: string;
  new_count: number;
  total_count: number;
  analysis: string;
  severity_counts?: Record<string, number>;
}

interface NmsData {
  latest: NmsLatest | null;
  analysis: NmsAnalysis | null;
  seen_count: number;
  history_count: number;
  configured: boolean;
  pending?: boolean;
  message?: string;
  error?: string;
}

interface NmsHistoryData {
  count: number;
  alarms: NmsAlarm[];
  source?: 'zset' | 'list';
  has_more?: boolean;
  page?: number;
  error?: string;
}

interface NmsIncident {
  incident_id: string;
  title: string;
  severity: string;
  category: 'object' | 'systemic';
  member_count: number;
  source_count: number;
  sources: string[];
  groups: string[];
  first_time: string;
  last_time: string;
  acked_count: number;
  unacked_count: number;
  new_count: number;
  priority: number;
  alarm_ids: string[];
  // 维度筛选（后端新增，旧数据可能缺失）
  segments?: { cidr: string; count: number }[];
  types?: { name: string; count: number }[];
  scope?: 'cross' | 'site' | 'object';
}

interface NmsIncidentsData {
  incidents: NmsIncident[];
  incident_count: number;
  total_alarms?: number;
  scope_analysis?: ScopeAnalysis | null;
  scope_analysis_scope?: string;
  timestamp?: string | null;
  configured: boolean;
  is_personal?: boolean;
  pending?: boolean;
  message?: string;
  error?: string;
}

// Checklist 预案匹配（以告警为核心的确定性匹配，/api/nms/checklist）
interface ChecklistDevice {
  key: string;
  name?: string;
  hostname?: string;
  ip?: string;
  sn?: string;
  vendor?: string;
  model?: string;
  firmware?: string;
  site?: string;
  status: string;
}

// 工具实测项（抓取时写入快照，处理分析"初步检查"面板结构化展示）
interface ToolCheckItem {
  status: string;        // ok | fail | no_record
  label?: string;
  summary: string;
  // DNS 实测
  domain?: string;
  server?: string;
  record_type?: string;
  // SSL 证书实测
  host?: string;
  port?: number;
  days_left?: number;
  expires?: string;
  subject?: string;
  issuer?: string;
  // TCP 端口探测
  ip?: string;
  // 备份作业实测（BRR/M365）
  total?: number;
  ok?: number;
  failed?: number;
  warning?: number;
  latest_rp?: string;
  servers?: string[];
  is_o365?: boolean;
}

interface ChecklistMatch {
  title: string;
  sheet: string;
  steps: string;
  variants?: { sheet: string; steps: string; applies?: boolean }[];
  group: string;
  group_name: string;
  tenant: 'platform' | 'single' | 'both' | 'unknown';
  ips: string[];
  count: number;
  unacked: number;
  severities: Record<string, number>;
  sources: { name: string; count: number }[];
  source_total: number;
  samples: { summary: string; source: string; ips: string[]; severity: string; time: string; serial: string }[];
  analysis?: string[];
  tool_checks?: { dns?: ToolCheckItem[]; ssl?: ToolCheckItem[]; tcp?: ToolCheckItem[] } | null;
  hardware?: boolean;
  devices?: ChecklistDevice[];
}

interface ChecklistGroup {
  key: string;
  name: string;
  desc: string;
  count: number;
  matches: ChecklistMatch[];
}

interface ChecklistData {
  matches: ChecklistMatch[];
  groups?: ChecklistGroup[];
  matched_count: number;
  active_count: number;
  unmatched_count: number;
  unmatched: { category: string; count: number }[];
  timestamp?: string | null;
  pending?: boolean;
  error?: string;
}

// ─── Checklist 预案清单（仅管理员） ───
interface ChecklistPlan {
  title: string;
  sheet: string;
  steps: string;
  variants: { sheet: string; steps: string }[];
  rules: string[];
  active_hits: number;
  origin?: 'xlsx' | 'custom';
}

interface PlanFormData {
  mode: 'add' | 'edit';
  title: string;          // edit 时锁定
  origin?: 'xlsx' | 'custom';
  steps_customer: string;
  steps_platform: string;
}

interface ChecklistPlansData {
  plans: ChecklistPlan[];
  total: number;
  active_hit_total: number;
  orphan_rules: string[];
  source?: string;
  error?: string;
}

// ─── Severity config (professional, no emoji) ───────────────────────────────────

const SEV_ORDER = ['critical', 'major', 'minor', 'warning', 'indeterminate', 'clear', 'cleared', 'unknown'] as const;

const SEV: Record<string, { label: string; color: string; bg: string; dot: string; border: string; rowBg: string; hoverBg: string }> = {
  critical:      { label: '严重', color: '#DC2626', bg: '#FEF2F2', dot: '#EF4444', border: '#FECACA', rowBg: 'rgba(239,68,68,0.15)', hoverBg: 'rgba(239,68,68,0.22)' },
  major:         { label: '重要', color: '#EA580C', bg: '#FFF7ED', dot: '#F97316', border: '#FED7AA', rowBg: 'rgba(249,115,22,0.13)', hoverBg: 'rgba(249,115,22,0.20)' },
  minor:         { label: '次要', color: '#CA8A04', bg: '#FEFCE8', dot: '#EAB308', border: '#FEF08A', rowBg: 'rgba(234,179,8,0.11)', hoverBg: 'rgba(234,179,8,0.17)' },
  warning:       { label: '警告', color: '#0284C7', bg: '#F0F9FF', dot: '#0EA5E9', border: '#BAE6FD', rowBg: 'rgba(14,165,233,0.10)', hoverBg: 'rgba(14,165,233,0.16)' },
  indeterminate: { label: '未知', color: '#6B7280', bg: '#F9FAFB', dot: '#9CA3AF', border: '#E5E7EB', rowBg: 'transparent', hoverBg: 'rgba(148,163,184,0.08)' },
  clear:         { label: '清除', color: '#16A34A', bg: '#F0FDF4', dot: '#22C55E', border: '#BBF7D0', rowBg: 'transparent', hoverBg: 'rgba(148,163,184,0.08)' },
  cleared:       { label: '已清除', color: '#16A34A', bg: '#F0FDF4', dot: '#22C55E', border: '#BBF7D0', rowBg: 'transparent', hoverBg: 'rgba(148,163,184,0.08)' },
  unknown:       { label: '未知', color: '#6B7280', bg: '#F9FAFB', dot: '#9CA3AF', border: '#E5E7EB', rowBg: 'transparent', hoverBg: 'rgba(148,163,184,0.08)' },
};

const sev = (s: string) => SEV[s] || SEV.unknown;

// 现存(活跃) 级别集合 (历史库事件含全部级别, 由后端按活跃级别记录)
const ACTIVE_SEV = ['critical', 'major', 'minor', 'warning', 'indeterminate'];

// 单条告警的分组归属：groups[] 优先，回退到单 group
const alarmGroups = (a: NmsAlarm): string[] =>
  (a.groups && a.groups.length) ? a.groups : (a.group ? [a.group] : []);

// 是否已 ACK 确认（Acknowledged=1 为已确认，0/空 为未确认）
const isAcked = (a: NmsAlarm): boolean =>
  String(a.acknowledged || a.status || '') === '1';

// 与后端 _ssl_is_ssl_alarm 同口径：仅这类告警显示「调查」入口（服务端仍会复核）
const isSslAlarmRow = (a: NmsAlarm): boolean => {
  if ((a.checklist || '') === 'SSL证书过期') return true;
  const low = `${a.summary || ''} ${(a.fields?.Summary || '')}`.toLowerCase();
  return (low.includes('ssl') || low.includes('证书')) && low.includes('position');
};

// ─── Small components ───────────────────────────────────────────────────────────

function SevDot({ severity, withLabel = true }: { severity: string; withLabel?: boolean }) {
  const s = sev(severity);
  const pulse = severity === 'critical' || severity === 'major';
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="relative flex h-2 w-2">
        {pulse && (
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-40"
                style={{ background: s.dot }} />
        )}
        <span className="relative inline-flex rounded-full h-2 w-2" style={{ background: s.dot }} />
      </span>
      {withLabel && <span className="text-xs font-medium" style={{ color: s.color }}>{s.label}</span>}
    </span>
  );
}

function SeverityBar({ counts }: { counts: Record<string, number> }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (total === 0) return null;
  const segments = SEV_ORDER.filter(s => (counts[s] || 0) > 0)
    .map(s => ({ key: s, count: counts[s] || 0, pct: ((counts[s] || 0) / total) * 100 }));
  return (
    <div className="flex h-1.5 rounded-full overflow-hidden bg-[#F1F5F9]">
      {segments.map(seg => (
        <div key={seg.key} className="transition-all duration-500"
             style={{ width: `${seg.pct}%`, background: sev(seg.key).dot }}
             title={`${sev(seg.key).label}: ${seg.count}`} />
      ))}
    </div>
  );
}

// 简易 markdown -> React 节点 (段落/粗体/表格/列表/引用/分隔线)
function renderSimpleMarkdown(text: string) {
  const lines = String(text || '').split('\n');
  const nodes: React.ReactNode[] = [];
  let i = 0;
  const inline = (s: string): React.ReactNode => {
    const parts: React.ReactNode[] = [];
    // 组合匹配：**粗体** / `代码` / [文本](链接) / 裸 URL
    const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s)]+)/g;
    let last = 0, m;
    const linkCls = "text-[#7C3AED] underline decoration-dotted underline-offset-2 hover:text-[#6D28D9] break-all";
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parts.push(s.slice(last, m.index));
      if (m[1] !== undefined) {
        const pv = m[1].match(/^P([0-3])$/);
        if (pv) {
          const pCls: Record<string, string> = {
            P0: 'bg-red-50 border-red-200 text-red-700',
            P1: 'bg-amber-50 border-amber-200 text-amber-700',
            P2: 'bg-sky-50 border-sky-200 text-sky-700',
            P3: 'bg-slate-100 border-slate-200 text-slate-600',
          };
          parts.push(<span key={`b${m.index}`} className={`inline-block align-middle px-1.5 py-px rounded-md border text-[11px] font-semibold mr-1 ${pCls[m[1]]}`}>{m[1]}</span>);
        } else {
          parts.push(<strong key={`b${m.index}`}>{m[1]}</strong>);
        }
      } else if (m[2] !== undefined) {
        parts.push(<code key={`c${m.index}`} className="px-1 py-0.5 rounded bg-[#F1F5F9] text-[#DB2777] font-mono text-[12px]">{m[2]}</code>);
      } else if (m[3] !== undefined) {
        parts.push(<a key={`l${m.index}`} href={m[4]} target="_blank" rel="noopener noreferrer" className={linkCls}>{m[3]}</a>);
      } else if (m[5] !== undefined) {
        parts.push(<a key={`u${m.index}`} href={m[5]} target="_blank" rel="noopener noreferrer" className={linkCls}>{m[5]}</a>);
      }
      last = m.index + m[0].length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts.length ? parts : s;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^---+$/.test(line.trim())) {
      nodes.push(<hr key={i} className="my-4 border-[#E5E7EB]" />);
      i++; continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const lvl = h[1].length, txt = h[2];
      if (lvl === 1) nodes.push(<h1 key={i} className="text-base font-bold text-[#0F172A] mt-5 mb-2">{inline(txt)}</h1>);
      else if (lvl === 2) nodes.push(<h2 key={i} className="text-sm font-bold text-[#0F172A] mt-4 mb-2 pb-1 border-b border-[#E5E7EB]">{inline(txt)}</h2>);
      else if (lvl === 3) nodes.push(<h3 key={i} className="text-sm font-semibold text-[#334155] mt-3 mb-1.5">{inline(txt)}</h3>);
      else nodes.push(<h4 key={i} className="text-xs font-semibold text-[#475569] mt-2 mb-1">{inline(txt)}</h4>);
      i++; continue;
    }
    if (/^\s*\|.+\|\s*$/.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        if (!/^\s*\|[\s\-|:]+\|\s*$/.test(lines[i]))
          rows.push(lines[i].split('|').slice(1, -1).map(c => c.trim()));
        i++;
      }
      if (rows.length) {
        const head = rows[0], body = rows.slice(1);
        nodes.push(
          <div key={`t${i}`} className="my-3 overflow-x-auto border border-[#E5E7EB] rounded-lg">
            <table className="min-w-full text-xs">
              <thead className="bg-[#EDE9FE]">
                <tr>
                  {head.map((c, j) => (<th key={j} className="px-3 py-2 text-left font-semibold text-[#5B21B6]">{inline(c)}</th>))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F1F5F9]">
                {body.map((r, ri) => (
                  <tr key={ri} className={`${ri % 2 === 1 ? 'bg-[#F8FAFC]' : 'bg-white'} hover:bg-[#F1F5F9]`}>
                    {r.map((c, j) => (<td key={j} className="px-3 py-2 text-[#475569]">{inline(c)}</td>))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*[-*]\s+/, ''))}</li>);
        i++;
      }
      nodes.push(<ul key={`ul${i}`} className="list-disc pl-5 my-2 space-y-1 text-[#334155]">{items}</ul>);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*\d+\.\s+/, ''))}</li>);
        i++;
      }
      nodes.push(<ol key={`ol${i}`} className="list-decimal pl-5 my-2 space-y-1 text-[#334155]">{items}</ol>);
      continue;
    }
    if (/^\s*>\s*/.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && /^\s*>\s*/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s*/, ''));
        i++;
      }
      nodes.push(
        <div key={`q${i}`} className="my-3 border-l-2 border-[#94A3B8] bg-[#F8FAFC] px-3 py-2 text-[#475569] text-sm rounded-r">
          {inline(quoted.join(' '))}
        </div>
      );
      continue;
    }
    if (!line.trim()) { i++; continue; }
    nodes.push(<p key={i} className="my-1.5 leading-relaxed">{inline(line)}</p>);
    i++;
  }
  return nodes;
}

// 详情字段：有值才渲染
function Field({ label, value, mono }: { label: string; value?: string; mono?: boolean }) {
  if (!value) return null;
  return <div className="truncate" title={`${label}: ${value}`}><span className="text-[#94A3B8]">{label}：</span><span className={`${mono ? 'font-mono ' : ''}text-[#475569]`}>{value}</span></div>;
}

// ─── Alarm row (compact, group-aware) ────────────────────────────────────────────

function AlarmRow({ alarm, idx, groupRank, onInvestigate }: {
  alarm: NmsAlarm; idx: number; groupRank?: (g: string) => number;
  onInvestigate?: (alarm: NmsAlarm) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [hover, setHover] = useState(false);
  const [showAllFields, setShowAllFields] = useState(false);
  const s = sev(alarm.severity);
  // 分组按业务优先级排序展示（避免 Default/AllEvents 等兜底视图排首位）
  const gsRaw = alarmGroups(alarm);
  const gs = groupRank ? [...gsRaw].sort((a, b) => groupRank(a) - groupRank(b) || a.localeCompare(b)) : gsRaw;
  const firstT = alarm.first_time || '';
  const lastT = alarm.last_time || alarm.time || '';
  const src = alarm.source || alarm.node || '';
  return (
    <>
      <tr className="group transition-colors cursor-pointer"
          style={{ background: hover ? s.hoverBg : s.rowBg }}
          onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
          onClick={() => setExpanded(!expanded)}>
        <td className="w-1 p-0"><div className="h-full w-1 group-hover:w-1.5 transition-all" style={{ background: s.dot }} /></td>
        <td className="px-2 py-1 w-8 text-[11px] text-[#94A3B8] font-mono">{idx + 1}</td>
        <td className="px-2 py-1 w-[70px]"><SevDot severity={alarm.severity} /></td>
        <td className="px-2 py-1 w-[64px]">
          {isAcked(alarm) ? (
            <span className="inline-flex items-center gap-1 text-[11px] font-medium text-[#16A34A]" title="已确认 (ACK=1)">
              <CheckCircle size={12} />已确认
            </span>
          ) : (
            <span className="text-[11px] text-[#94A3B8]" title="未确认 (ACK=0)">未确认</span>
          )}
        </td>
        <td className="px-2 py-1 w-[64px]">
          {onInvestigate && isSslAlarmRow(alarm) ? (
            <button onClick={e => { e.stopPropagation(); onInvestigate(alarm); }}
              title="发起只读取证：校验证书链、域名与剩余有效期"
              className="inline-flex items-center gap-0.5 whitespace-nowrap text-[10px] text-[#475569] border border-[#E2E8F0] rounded px-1.5 py-px hover:border-[#CBD5E1] hover:text-[#0F172A] transition-colors">
              <ShieldAlert size={9} /> 调查
            </button>
          ) : null}
        </td>
        <td className="px-2 py-1 max-w-0">
          <div className="text-[12px] text-[#0F172A] truncate leading-snug" title={alarm.summary || alarm.raw}>
            {alarm.checklist && (
              <span className="inline-block align-middle text-[9px] font-medium px-1 py-px mr-1 rounded bg-amber-50 border border-amber-200 text-amber-700"
                    title={`Checklist 预案：${alarm.checklist}`}>预案</span>
            )}
            {alarm.occurrences && alarm.occurrences > 1 && (
              <span className="inline-block align-middle text-[9px] font-medium px-1 py-px mr-1 rounded bg-sky-50 border border-sky-200 text-sky-700"
                    title={`同一事件累计发生 ${alarm.occurrences} 次（Serial 换手已合并，occurrences 计次）`}>×{alarm.occurrences}</span>
            )}
            {alarm.summary || alarm.raw || '—'}
          </div>
        </td>
        <td className="px-2 py-1 w-[112px]">
          {gs.length ? (
            <div className="flex items-center gap-1" title={gs.join(', ')}>
              <span className="inline-block text-[10px] font-medium px-1 py-px rounded bg-[#F1F5F9] text-[#475569] max-w-[76px] truncate">{gs[0]}</span>
              {gs.length > 1 && <span className="text-[9px] text-[#94A3B8]">+{gs.length - 1}</span>}
            </div>
          ) : <span className="text-[#CBD5E1]">—</span>}
        </td>
        <td className="px-2 py-1 w-[120px] text-[11px] text-[#64748B] font-mono truncate" title={src}>{src || '—'}</td>
        <td className="px-2 py-1 w-[150px] whitespace-nowrap">
          <div className="text-[10px] leading-[13px] font-mono text-[#94A3B8] truncate" title={`首次: ${firstT || '—'}`}>首 {firstT || '—'}</div>
          <div className="text-[10px] leading-[13px] font-mono text-[#64748B] truncate" title={`最后: ${lastT || '—'}`}>末 {lastT || '—'}</div>
        </td>
        <td className="px-2 py-1 w-6">
          <ChevronRight size={13} className={`text-[#CBD5E1] transition-transform ${expanded ? 'rotate-90 text-[#64748B]' : ''}`} />
        </td>
      </tr>
      {expanded && (
        <tr style={{ background: 'rgba(248,250,252,0.9)' }}>
          <td colSpan={10} className="px-6 py-3">
            <div className="grid grid-cols-4 gap-x-6 gap-y-1.5 text-[11px]">
              <div className="truncate"><span className="text-[#94A3B8]">首次出现：</span><span className="font-mono text-[#475569]">{firstT || '—'}</span></div>
              <div className="truncate"><span className="text-[#94A3B8]">最后出现：</span><span className="font-mono text-[#475569]">{lastT || '—'}</span></div>
              <div><span className="text-[#94A3B8]">严重级别：</span><span className="font-medium" style={{ color: s.color }}>{s.label}</span></div>
              <Field label="次数" value={alarm.count} />
              <Field label="Node" value={alarm.node} mono />
              <Field label="NodeAlias" value={alarm.node_alias} mono />
              <Field label="来源" value={src} mono />
              <Field label="Manager" value={alarm.manager} mono />
              <Field label="Agent" value={alarm.agent} mono />
              <Field label="Class" value={alarm.aclass} mono />
              <Field label="告警类型" value={alarm.alarm_type} mono />
              <Field label="EventGroup" value={alarm.event_group} mono />
              <Field label="EventKey" value={alarm.event_key} mono />
              <Field label="CaseId" value={alarm.case_id} mono />
              <Field label="Type" value={alarm.atype} mono />
              <Field label="Owner" value={alarm.owner} mono />
              <Field label="状态变更" value={alarm.state_change} mono />
              <Field label="创建时间" value={alarm.create_time} mono />
              <div className="truncate"><span className="text-[#94A3B8]">确认状态：</span>{isAcked(alarm) ? <span className="font-medium text-[#16A34A]">已确认</span> : <span className="text-[#EA580C]">未确认</span>}</div>
              <Field label="Serial" value={alarm.serial || alarm.event_id} mono />
              <div className="col-span-2 truncate"><span className="text-[#94A3B8]">alarm_id：</span><span className="font-mono text-[#475569]">{alarm.alarm_id || '—'}</span></div>
              <div className="col-span-4">
                <span className="text-[#94A3B8]">处置预案：</span>
                {alarm.checklist
                  ? <span className="inline-block text-[10px] font-medium px-1.5 py-px rounded bg-amber-50 border border-amber-200 text-amber-700">{alarm.checklist}</span>
                  : <span className="text-[#CBD5E1]">Checklist 无匹配</span>}
              </div>
              <div className="col-span-4">
                <span className="text-[#94A3B8]">分组：</span>
                <span className="inline-flex flex-wrap gap-1 align-middle">
                  {gs.length ? gs.map(g => (<span key={g} className="inline-block text-[10px] px-1.5 py-px rounded bg-[#F1F5F9] text-[#475569]">{g}</span>)) : <span className="text-[#CBD5E1]">—</span>}
                </span>
              </div>
              {alarm.fields && Object.keys(alarm.fields).length > 0 && (
                <div className="col-span-4 mt-0.5">
                  <button onClick={() => setShowAllFields(v => !v)}
                    className="text-[10px] text-[#64748B] hover:text-[#DC2626] transition-colors">
                    {showAllFields ? '▾' : '▸'} 全部字段 ({Object.keys(alarm.fields).length})
                  </button>
                  {showAllFields && (
                    <div className="grid grid-cols-3 gap-x-6 gap-y-1 mt-1.5 p-2 bg-white rounded border border-[#F1F5F9] max-h-48 overflow-y-auto">
                      {Object.entries(alarm.fields).map(([k, v]) => (
                        <div key={k} className="text-[10px] truncate" title={`${k}: ${v}`}>
                          <span className="text-[#94A3B8]">{k}:</span> <span className="font-mono text-[#475569]">{v}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {alarm.raw && (
                <div className="col-span-4 mt-1">
                  <span className="text-[#94A3B8]">raw:</span>
                  <div className="font-mono text-[#64748B] mt-1 p-2 bg-white rounded border border-[#F1F5F9] text-[10px] leading-relaxed break-all max-h-32 overflow-y-auto">{alarm.raw}</div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ─── 事件簇卡片（事件视图主列表单元，可展开成员告警）────────────────────────────

function IncidentCard({ inc, expanded, onToggle, alarmMap, groupRank, onInvestigate }: {
  inc: NmsIncident;
  expanded: boolean;
  onToggle: () => void;
  alarmMap: Record<string, NmsAlarm>;
  groupRank: (g: string) => number;
  onInvestigate?: (alarm: NmsAlarm, incidentId: string) => void;
}) {
  const meta = sev(inc.severity);
  const members = inc.alarm_ids.map(id => alarmMap[id]).filter(Boolean);
  const firstMs = parseNmsTime(inc.first_time);
  const lastMs = parseNmsTime(inc.last_time) ?? firstMs;
  const duration = firstMs != null && lastMs != null ? fmtDuration(Math.max(0, lastMs - firstMs)) : '—';
  const ackPct = inc.member_count > 0
    ? Math.max(0, Math.min(100, Math.round((inc.acked_count / inc.member_count) * 100)))
    : 0;
  const gs = [...(inc.groups || [])].sort((a, b) => groupRank(a) - groupRank(b) || a.localeCompare(b));
  const srcs = inc.sources || [];

  return (
    <div className="rounded-xl border bg-white overflow-hidden transition-colors"
      style={{ borderColor: expanded ? meta.dot : '#E2E8F0' }}>
      <div className="flex items-stretch cursor-pointer hover:bg-[#F8FAFC] transition-colors"
        onClick={onToggle}>
        {/* 级别色条：一眼识别严重程度 */}
        <div className="w-[3px] shrink-0" style={{ background: meta.dot }} />
        <div className="flex-1 min-w-0 px-3.5 py-2.5">
          {/* 标题行：优先级 / 级别 / 类别 / 标题 / 新增 */}
          <div className="flex items-center gap-2 min-w-0">
            <span className="inline-flex items-center justify-center min-w-[30px] px-1.5 py-0.5 rounded-md text-[11px] font-bold shrink-0"
              style={{ background: meta.bg, color: meta.color }} title={`优先级分 ${inc.priority}`}>P{inc.priority}</span>
            <span className="inline-flex items-center gap-1 shrink-0">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: meta.dot }} />
              <span className="text-[11px] font-medium" style={{ color: meta.color }}>{meta.label}</span>
            </span>
            {inc.category === 'systemic' ? (
              <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-[#EEF2FF] text-[#4F46E5] border border-[#C7D2FE] shrink-0">系统级</span>
            ) : (
              <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-[#F1F5F9] text-[#475569] border border-[#E2E8F0] shrink-0">对象级</span>
            )}
            {inc.scope && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-md border shrink-0"
                style={{
                  background: SCOPE_META[inc.scope].bg,
                  color: SCOPE_META[inc.scope].color,
                  borderColor: SCOPE_META[inc.scope].border,
                }}>
                {SCOPE_META[inc.scope].label}
              </span>
            )}
            <span className="text-[13px] font-medium text-[#0F172A] truncate flex-1" title={inc.title}>{inc.title}</span>
            {inc.new_count > 0 && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-[#FEF2F2] text-[#DC2626] border border-[#FECACA]">新增 {inc.new_count}</span>
            )}
            <ChevronRight size={14} className={`shrink-0 text-[#CBD5E1] transition-transform ${expanded ? 'rotate-90 text-[#64748B]' : ''}`} />
          </div>

          {/* 元信息行：来源 / 分组 / 时间跨度 */}
          <div className="mt-1.5 flex items-center gap-x-3 gap-y-1 flex-wrap text-[11px] text-[#64748B]">
            <span className="inline-flex items-center gap-1 min-w-0 max-w-[300px]" title={srcs.join('、')}>
              <Radio size={11} className="text-[#94A3B8] shrink-0" />
              <span className="truncate">{srcs.length ? srcs.join('、') : '未知来源'}</span>
              {inc.source_count > srcs.length && <span className="shrink-0 text-[#94A3B8]">+{inc.source_count - srcs.length}</span>}
            </span>
            <span className="inline-flex items-center gap-1 min-w-0 max-w-[240px]" title={gs.join('、')}>
              <Layers size={11} className="text-[#94A3B8] shrink-0" />
              <span className="truncate">{gs.length ? gs.join('、') : '未分组'}</span>
            </span>
            <span className="inline-flex items-center gap-1 font-mono text-[10px] text-[#94A3B8]" title={`首: ${inc.first_time || '-'} / 末: ${inc.last_time || '-'}`}>
              <Clock size={11} className="shrink-0" />
              {inc.first_time || '—'} → {inc.last_time || '—'}
              <span className="text-[#64748B]">· {duration}</span>
            </span>
          </div>

          {/* 维度行：主网段 / 主告警类型 */}
          {(inc.segments?.length || inc.types?.length) ? (
            <div className="mt-1 flex items-center gap-x-3 gap-y-1 flex-wrap text-[10px] text-[#64748B]">
              {inc.segments?.[0] && (
                <span className="inline-flex items-center gap-1 min-w-0 max-w-[240px]"
                  title={(inc.segments || []).map(s => `${s.cidr} (${s.count})`).join('、')}>
                  <Network size={11} className="text-[#94A3B8] shrink-0" />
                  <span className="font-mono truncate">{inc.segments[0].cidr}</span>
                  {(inc.segments?.length || 0) > 1 && (
                    <span className="shrink-0 text-[#94A3B8]">+{(inc.segments?.length || 0) - 1}</span>
                  )}
                </span>
              )}
              {inc.types?.[0] && (
                <span className="inline-flex items-center gap-1 min-w-0 max-w-[260px]"
                  title={(inc.types || []).map(t => `${t.name} (${t.count})`).join('、')}>
                  <Tag size={11} className="text-[#94A3B8] shrink-0" />
                  <span className="truncate">{inc.types[0].name}</span>
                  {(inc.types?.length || 0) > 1 && (
                    <span className="shrink-0 text-[#94A3B8]">+{(inc.types?.length || 0) - 1}</span>
                  )}
                </span>
              )}
            </div>
          ) : null}

          {/* 规模 + 确认进度（仅反映 NMS 确认状态，不代表故障已恢复） */}
          <div className="mt-2 flex items-center gap-4 flex-wrap">
            <span className="inline-flex items-baseline gap-1">
              <span className="text-[14px] font-semibold font-mono tabular-nums text-[#0F172A]">{inc.member_count}</span>
              <span className="text-[10px] text-[#94A3B8]">成员告警</span>
            </span>
            <span className="inline-flex items-baseline gap-1">
              <span className="text-[14px] font-semibold font-mono tabular-nums text-[#0F172A]">{inc.source_count}</span>
              <span className="text-[10px] text-[#94A3B8]">来源</span>
            </span>
            <span className="inline-flex items-baseline gap-1">
              <span className="text-[14px] font-semibold font-mono tabular-nums"
                style={{ color: inc.unacked_count > 0 ? '#EA580C' : '#16A34A' }}>{inc.unacked_count}</span>
              <span className="text-[10px] text-[#94A3B8]">未确认</span>
            </span>
            <div className="ml-auto flex items-center gap-2 shrink-0 min-w-[170px] max-w-[220px] flex-1">
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between text-[10px] text-[#94A3B8] mb-0.5">
                  <span>已确认</span>
                  <span className="font-mono tabular-nums">{inc.acked_count}/{inc.member_count}</span>
                </div>
                <div className="flex h-1.5 rounded-full overflow-hidden bg-[#F1F5F9]"
                  title={`已确认 ${inc.acked_count} · 未确认 ${inc.unacked_count}`}>
                  <div className="transition-all" style={{ width: `${ackPct}%`, background: '#16A34A' }} />
                  <div className="transition-all" style={{ width: `${100 - ackPct}%`, background: '#FDBA74' }} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 展开：成员告警明细 */}
      {expanded && (
        <div className="border-t border-[#F1F5F9] bg-[#F8FAFC] px-3.5 py-3">
          <div className="text-[11px] text-[#64748B] mb-2 flex items-center gap-3 flex-wrap">
            <span>成员告警 {inc.member_count} 条 · 来源 {inc.source_count} 个</span>
            <span>已确认 {inc.acked_count} / 未确认 {inc.unacked_count}</span>
            <span>分组: {gs.join('、') || '-'}</span>
          </div>
          {members.length > 0 ? (
            <div className="border border-[#E2E8F0] rounded-xl overflow-hidden bg-white">
              <table className="w-full table-fixed">
                <tbody className="divide-y divide-[#F1F5F9]">
                  {members.slice(0, 50).map((m, mi) => (
                    <AlarmRow key={`${m.alarm_id}-${mi}`} alarm={m} idx={mi} groupRank={groupRank}
                      onInvestigate={onInvestigate ? (a) => onInvestigate(a, inc.incident_id) : undefined} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-[11px] text-[#94A3B8]">
              成员告警不在当前已加载快照中（共 {inc.member_count} 条，可到「当前告警」页加载更多）。
            </p>
          )}
          {members.length > 50 && (
            <p className="text-[11px] text-[#94A3B8] mt-2">仅显示前 50 条成员（共 {inc.member_count} 条）</p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Checklist 预案匹配卡片（智能分析页，以告警为核心的结构化展示）──────────────────
const TC_STATUS_TEXT: Record<string, string> = {
  ok: '正常', fail: '异常/不可达', no_record: '无记录', attention: '存在失败作业',
};

const TC_META: Record<string, { name: string; badge: string }> = {
  dns: { name: 'DNS API 实测', badge: 'bg-indigo-50 border-indigo-200 text-indigo-700' },
  ssl: { name: 'SSL 证书实测', badge: 'bg-amber-50 border-amber-200 text-amber-700' },
  tcp: { name: 'TCP 端口探测', badge: 'bg-cyan-50 border-cyan-200 text-cyan-700' },
  backup: { name: '备份作业实测', badge: 'bg-teal-50 border-teal-200 text-teal-700' },
};

// 初步检查（工具实测）面板：可展开详情、逐项/全部复制
function ToolChecksPanel({ checks }: { checks: Record<string, ToolCheckItem[]> }) {
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  const [copiedKey, setCopiedKey] = useState('');
  const copy = (key: string, text: string) => {
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => {
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(''), 1500);
    }).catch(() => {});
  };
  const fields = (t: ToolCheckItem): [string, string][] => {
    if (t.domain) return [['域名', t.domain], ['DNS服务器', t.server || '默认'], ['记录类型', t.record_type || '—'], ['状态', TC_STATUS_TEXT[t.status] || t.status]];
    if (t.host) return [['地址', `${t.host}:${t.port ?? 443}`], ['剩余天数', t.days_left != null ? `${t.days_left} 天` : '—'], ['到期时间', t.expires || '—'], ['证书主体', t.subject || '—'], ['颁发者', t.issuer || '—']];
    if (t.total != null) return [
      ['备份服务器', t.ip || '—'],
      ['平台', t.is_o365 ? 'Veeam M365' : 'BRR（Veeam/NetBackup）'],
      ['相关作业', `${t.total} 个`],
      ['最近结果', `Success ${t.ok ?? 0}${t.failed ? `、Failed ${t.failed}` : ''}${t.warning ? `、Warning ${t.warning}` : ''}`],
      ['最近还原点', t.latest_rp || '—'],
      ['状态', TC_STATUS_TEXT[t.status] || t.status],
    ];
    if (t.ip) return [['地址', t.port ? `${t.ip}:${t.port}` : t.ip], ['状态', TC_STATUS_TEXT[t.status] || t.status]];
    return [['结果', t.summary || '—']];
  };
  const entries = Object.entries(checks).filter(([, v]) => v && v.length > 0) as [string, ToolCheckItem[]][];
  const allText = entries.flatMap(([type, items]) => items.map(t => `[${TC_META[type]?.name || type}] ${t.summary}`)).join('\n');
  if (!entries.length) return null;
  return (
    <div className="mt-2 pt-2 border-t border-sky-100">
      <div className="flex items-center justify-between mb-1.5">
        <div className="text-[10px] font-semibold text-[#334155]">初步检查（工具实测）</div>
        <button onClick={() => copy('__all__', allText)}
          className="text-[9px] px-1.5 py-px rounded border border-sky-200 text-sky-600 hover:bg-sky-50 bg-white transition-colors">
          {copiedKey === '__all__' ? '已复制' : '复制全部'}
        </button>
      </div>
      <div className="space-y-1.5">
        {entries.map(([type, items]) => (
          <div key={type}>
            <span className={`text-[9px] font-medium px-1.5 py-px rounded border ${TC_META[type]?.badge || ''}`}>{TC_META[type]?.name || type}</span>
            <div className="mt-1 space-y-1">
              {items.map((t, i) => {
                const key = `${type}-${i}`;
                const isOpen = !!openMap[key];
                const dot = t.status === 'ok' ? 'bg-emerald-500'
                  : t.status === 'fail' ? 'bg-red-500'
                    : t.status === 'no_record' ? 'bg-slate-400' : 'bg-slate-300';
                return (
                  <div key={key} className="border border-[#E2E8F0] rounded bg-white/90 overflow-hidden">
                    <div className="flex items-center gap-1.5 px-2 py-1">
                      <button onClick={() => setOpenMap(s => ({ ...s, [key]: !isOpen }))}
                        className="flex items-center gap-1.5 flex-1 min-w-0 text-left" title="展开详情">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot}`} />
                        <span className="text-[10px] text-[#334155] truncate">{t.summary}</span>
                        <span className="text-[9px] text-[#94A3B8] shrink-0">{isOpen ? '▾' : '▸'}</span>
                      </button>
                      <button onClick={() => copy(key, t.summary)}
                        className="shrink-0 text-[9px] px-1 py-px rounded border border-[#E2E8F0] text-[#64748B] hover:border-sky-300 hover:text-sky-600 bg-white transition-colors">
                        {copiedKey === key ? '已复制' : '复制'}
                      </button>
                    </div>
                    {isOpen && (
                      <div className="px-2 pb-1.5 pt-1 border-t border-[#F1F5F9] grid grid-cols-2 gap-x-3 gap-y-0.5 bg-[#F8FAFC]/60">
                        {fields(t).map(([k, v]) => (
                          <div key={k} className="flex gap-1 text-[10px] min-w-0">
                            <span className="text-[#94A3B8] shrink-0">{k}</span>
                            <span className="text-[#334155] break-all">{v}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ChecklistCard({ m }: { m: ChecklistMatch }) {
  const [open, setOpen] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const topSev = SEV_ORDER.find(s => (m.severities[s] || 0) > 0) || 'unknown';
  const s = sev(topSev);
  // 处理建议：单客户/平台两侧各一份时并排展示（旧数据无 variants 时回退主份）
  const variants = (m.variants && m.variants.length > 0)
    ? m.variants
    : (m.steps ? [{ sheet: m.sheet, steps: m.steps }] : []);
  // 影响范围徽标：平台(多客户共享)/单客户/两者
  const tenantLabel: Record<string, { t: string; cls: string }> = {
    platform: { t: '平台·多客户', cls: 'bg-sky-50 border-sky-200 text-sky-700' },
    single: { t: '单客户', cls: 'bg-emerald-50 border-emerald-200 text-emerald-700' },
    both: { t: '单客户/平台混合', cls: 'bg-violet-50 border-violet-200 text-violet-700' },
    unknown: { t: '范围未知', cls: 'bg-[#F1F5F9] border-[#E2E8F0] text-[#64748B]' },
  };
  const tl = tenantLabel[m.tenant] || tenantLabel.unknown;
  return (
    <div className="border border-[#E2E8F0] rounded-xl bg-white overflow-hidden">
      {/* 头部：预案名 + 告警规模 + 级别分布 */}
      <button onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-[#F8FAFC] transition-colors">
        <span className="w-1.5 h-8 rounded-full shrink-0" style={{ background: s.dot }} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px] font-semibold text-[#0F172A] truncate">{m.title}</span>
            <span className="text-[10px] px-1.5 py-px rounded bg-amber-50 border border-amber-200 text-amber-700">Checklist</span>
            {m.group_name && m.group_name !== '其他' && (
              <span className="text-[10px] px-1.5 py-px rounded bg-[#F1F5F9] border border-[#E2E8F0] text-[#475569]">{m.group_name}</span>
            )}
            <span className={`text-[10px] px-1.5 py-px rounded border ${tl.cls}`}>{tl.t}</span>
          </div>
          <div className="flex items-center gap-3 mt-1 text-[11px] text-[#64748B] flex-wrap">
            <span>命中 <strong className="text-[#0F172A]">{m.count}</strong> 条告警</span>
            {m.unacked > 0 && <span className="text-[#EA580C]">未确认 {m.unacked}</span>}
            <span>影响对象 {m.source_total}</span>
            {m.ips.length > 0 && (
              <span className="text-[#475569]">IP {m.ips.slice(0, 4).join(' · ')}{m.ips.length > 4 ? ` 等 ${m.ips.length} 个` : ''}</span>
            )}
            <span className="inline-flex items-center gap-1.5">
              {SEV_ORDER.filter(sv => (m.severities[sv] || 0) > 0).map(sv => (
                <span key={sv} className="inline-flex items-center gap-0.5">
                  <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: sev(sv).dot }} />
                  <span style={{ color: sev(sv).color }}>{m.severities[sv]}</span>
                </span>
              ))}
            </span>
          </div>
        </div>
        <ChevronRight size={14} className={`text-[#CBD5E1] shrink-0 transition-transform ${open ? 'rotate-90 text-[#64748B]' : ''}`} />
      </button>
      {open && (
        <div className="px-4 pb-3 border-t border-[#F1F5F9] pt-3 space-y-3">
          {/* 受影响对象（IP 优先展示，无 IP 时退回 source） */}
          {(m.sources.length > 0 || m.ips.length > 0) && (
            <div>
              <div className="text-[11px] text-[#94A3B8] mb-1">受影响对象 TOP{m.sources.length}{m.source_total > m.sources.length ? `（共 ${m.source_total} 个）` : ''}{m.ips.length > 0 ? ` · IP ${m.ips.length} 个` : ''}</div>
              <div className="flex flex-wrap gap-1">
                {m.sources.slice(0, 6).map(sc => (
                  <span key={sc.name} className="text-[10px] font-mono px-1.5 py-px rounded bg-[#F1F5F9] text-[#475569]">
                    {sc.name} ×{sc.count}
                  </span>
                ))}
              </div>
            </div>
          )}
          {/* 样本告警 */}
          {m.samples.length > 0 && (
            <div>
              <div className="text-[11px] text-[#94A3B8] mb-1">告警样本（前 {m.samples.length} 条）</div>
              <div className="space-y-1">
                {m.samples.map((sm, i) => (
                  <div key={sm.serial || i} className="flex items-start gap-2 text-[11px] leading-snug">
                    <span className="w-1.5 h-1.5 rounded-full inline-block mt-1.5 shrink-0" style={{ background: sev(sm.severity).dot }} />
                    <span className="text-[#334155] break-all">{sm.summary}</span>
                    <span className="font-mono text-[#94A3B8] whitespace-nowrap ml-auto shrink-0">{(sm.ips && sm.ips[0]) || sm.source}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {/* 处理建议方向 + 处理分析（并排双卡片：左侧单客户/平台预案，右侧基于真实告警数据的分析） */}
          {(variants.length > 0 || (m.analysis && m.analysis.length > 0)) && (
            <div>
              <button onClick={() => setShowSteps(v => !v)}
                className="text-[11px] font-medium text-[#7C3AED] hover:text-[#6D28D9] transition-colors">
                {showSteps ? '▾' : '▸'} 处理建议方向{variants.length > 1 ? `（${variants.map(v => v.sheet).join(' / ')}）` : (variants.length === 1 && variants[0].sheet ? `（${variants[0].sheet}）` : '')} · 处理分析
              </button>
              {showSteps && (
                <div className={`mt-1.5 grid gap-2 ${variants.length > 0 && m.analysis && m.analysis.length > 0 ? 'md:grid-cols-2' : ''}`}>
                  {/* 左：处理建议方向（单客户/平台两侧各有预案时并排，适用侧高亮） */}
                  {variants.length > 0 && (
                    <div className={`grid gap-2 ${variants.length > 1 ? 'md:grid-cols-2' : ''}`}>
                      {variants.map(v => (
                        <div key={v.sheet}
                             className={`p-3 border rounded-lg ${v.applies === false
                               ? 'bg-[#F8FAFC] border-[#E2E8F0] opacity-70'
                               : 'bg-amber-50/50 border-amber-100'}`}>
                          <div className="flex items-center gap-1.5 mb-1.5">
                            <span className={`text-[10px] font-medium px-1.5 py-px rounded border ${
                              v.applies === false
                                ? 'bg-[#F1F5F9] border-[#E2E8F0] text-[#94A3B8]'
                                : 'bg-amber-100/80 border-amber-200 text-amber-700'}`}>
                              {v.sheet || '通用'}
                            </span>
                            {v.applies === false
                              ? <span className="text-[9px] text-[#94A3B8]">当前影响面不适用，仅供参考</span>
                              : <span className="text-[9px] font-medium text-amber-700">✓ 适用当前影响面</span>}
                          </div>
                          <div className="text-[11px] text-[#475569] leading-relaxed whitespace-pre-wrap max-h-64 overflow-y-auto">
                            {v.steps}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  {/* 右：处理分析（按预案步骤组织的真实数据分析 + 硬件设备 SN 资产） */}
                  {m.analysis && m.analysis.length > 0 && (
                    <div className="p-3 bg-sky-50/40 border border-sky-100 rounded-lg">
                      <div className="flex items-center gap-1.5 mb-1.5">
                        <span className="text-[10px] font-medium px-1.5 py-px rounded bg-sky-100/80 border border-sky-200 text-sky-700">处理分析</span>
                        <span className="text-[9px] text-[#64748B]">按预案步骤核对真实告警数据（IP 去重）</span>
                      </div>
                      <ul className="space-y-1">
                        {m.analysis.map((p, i) => {
                          const isHeader = p.startsWith('处置核对');
                          const isVerdict = p.startsWith('　工具初步判断');
                          const isStep = p.startsWith('　') && !isVerdict;
                          const kv = p.match(/^(告警概况|级别\/确认|时间|影响面)：(.*)$/);
                          if (isHeader) {
                            return (
                              <li key={i} className="pt-1.5 mt-1 border-t border-sky-100 flex items-center gap-1.5">
                                <span className="w-2 h-[2px] rounded-full bg-sky-500 shrink-0" />
                                <span className="text-[11px] font-semibold text-[#0F172A]">处置核对（按预案步骤）</span>
                              </li>
                            );
                          }
                          if (kv) {
                            const labelMap: Record<string, string> = {
                              '告警概况': '概况', '级别/确认': '级别', '时间': '时间', '影响面': '影响面',
                            };
                            return (
                              <li key={i} className="flex items-start gap-1.5">
                                <span className="shrink-0 mt-px text-[9px] font-medium px-1 py-px rounded bg-sky-100 border border-sky-200 text-sky-700 w-[44px] text-center">
                                  {labelMap[kv[1]] || kv[1]}
                                </span>
                                <span className="text-[11px] text-[#334155] leading-relaxed min-w-0">{kv[2]}</span>
                              </li>
                            );
                          }
                          if (isStep) {
                            const body = p.replace(/^　/, '');
                            const sep = body.indexOf(' → ');
                            const stepText = sep >= 0 ? body.slice(0, sep) : body;
                            const ann = sep >= 0 ? body.slice(sep + 3) : '';
                            return (
                              <li key={i} className="flex items-start gap-1.5">
                                <span className="w-1 h-1 rounded-full bg-sky-300 mt-[6px] shrink-0" />
                                <div className="min-w-0 leading-relaxed">
                                  <span className="text-[11px] text-[#475569]">{stepText}</span>
                                  {ann && (
                                    <span className="block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-amber-50 border border-amber-200 text-amber-800 break-all">
                                      {ann}
                                    </span>
                                  )}
                                </div>
                              </li>
                            );
                          }
                          if (isVerdict) {
                            return (
                              <li key={i} className="flex items-start gap-1.5 mt-1">
                                <span className="w-2 h-[2px] rounded-full bg-emerald-500 mt-[7px] shrink-0" />
                                <span className="text-[11px] font-medium text-[#0F172A] leading-relaxed">{p.replace(/^　/, '')}</span>
                              </li>
                            );
                          }
                          return (
                            <li key={i} className="flex items-start gap-1.5">
                              <span className="w-1 h-1 rounded-full bg-sky-400 mt-[6px] shrink-0" />
                              <span className="text-[11px] text-[#334155] leading-relaxed">{p}</span>
                            </li>
                          );
                        })}
                      </ul>
                      {m.tool_checks && <ToolChecksPanel checks={m.tool_checks} />}
                      {/* 硬件告警: 设备资产（SN/固件来自 OPS-CORE资源管理平台） */}
                      {m.hardware && m.devices && m.devices.length > 0 && (
                        <div className="mt-2 pt-2 border-t border-sky-100">
                          <div className="text-[10px] font-medium text-sky-700 mb-1">设备资产（OPS-CORE资源管理平台）</div>
                          <div className="overflow-x-auto rounded border border-sky-100 bg-white">
                            <table className="min-w-full text-[10px]">
                              <thead className="bg-sky-50/60">
                                <tr>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">对象</th>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">SN</th>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">厂商/型号</th>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">固件</th>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">位置</th>
                                  <th className="px-2 py-1 text-left font-medium text-[#475569]">匹配状态</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-sky-50">
                                {m.devices.map(d => (
                                  <tr key={d.key}>
                                    <td className="px-2 py-1 font-mono text-[#334155]">
                                      {d.hostname || d.key}
                                      {d.ip && d.ip !== d.key ? <span className="text-[#94A3B8]"> ({d.ip})</span> : ''}
                                    </td>
                                    <td className="px-2 py-1 font-mono font-medium text-[#0F172A]">{d.sn || '—'}</td>
                                    <td className="px-2 py-1 text-[#475569]">{[d.vendor, d.model].filter(Boolean).join(' ') || '—'}</td>
                                    <td className="px-2 py-1 font-mono text-[#475569]">{d.firmware || '—'}</td>
                                    <td className="px-2 py-1 text-[#475569]">{d.site || '—'}</td>
                                    <td className="px-2 py-1">
                                      {d.status === 'ok'
                                        ? <span className="text-emerald-600">已匹配</span>
                                        : d.status === 'not_found'
                                          ? <span className="text-[#94A3B8]">资产表未收录</span>
                                          : <span className="text-[#EA580C]" title={d.status}>{d.status}</span>}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── 右侧告警详情面板：展示当前网段/类型条件下的关联告警与命中事件 ─────────────
// 与主区并排布局（非覆盖）：展开时主区动态回缩让出宽度，关闭时归零。

function DrillSidePanel(props: {
  open: boolean;
  cidr: string | null;
  typeName: string | null;
  // cidr/typeName 为「其他」时，传入矩阵已列出的 Top7 作为排除集
  cidrExclude: string[];
  typeExclude: string[];
  groups: string;
  token: string;
  incidents: NmsIncident[];
  // 快照时间戳：抓取完成后变化，通知面板立即静默刷新
  refreshKey?: string;
  // 打开轮次：每次打开都算一次新查询，不复用上一轮缓存的告警
  sessionKey?: number;
  onClose: () => void;
  onClear: () => void;
  onOpenIncident: (id: string) => void;
}) {
  const { open, cidr, typeName, cidrExclude, typeExclude, groups, token, incidents,
    refreshKey, sessionKey = 0, onClose, onClear, onOpenIncident } = props;
  const PAGE_SIZE = 50;
  const [tab, setTab] = useState<'alarms' | 'events'>('alarms');
  const [rows, setRows] = useState<NmsAlarm[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  // 静默刷新：不遮挡当前列表，只在底部提示
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [syncedAt, setSyncedAt] = useState('');
  // 当前 rows 对应的查询签名：不匹配即视为旧结果，一律不渲染，避免换条件后残留上一次的告警
  const [rowsKey, setRowsKey] = useState('');
  const rowsRef = useRef<NmsAlarm[]>([]);
  const reqRef = useRef(0);
  const busyRef = useRef(false);
  const active = !!(cidr || typeName);

  // 条件变化/重新打开立即换签名（不等 effect 清空），旧 rows 在当帧就已失效
  const key = `${sessionKey}|${cidr || ''}|${typeName || ''}|${cidrExclude.join(',')}|${typeExclude.join(',')}|${groups}`;
  const stale = rowsKey !== key;
  const shownRows = stale ? [] : rows;
  const shownTotal = stale ? 0 : total;
  const shownHasMore = stale ? false : hasMore;

  const query = useCallback((p: number) => {
    const qs = new URLSearchParams({ page: String(p), page_size: '50', status: 'active' });
    if (groups) qs.set('groups', groups);
    // 「其他」桶：交给后端用 __other__ + 排除集，与矩阵同一提取函数口径一致
    if (cidr === '其他') {
      qs.set('cidr', '__other__');
      if (cidrExclude.length) qs.set('cidr_exclude', cidrExclude.join(','));
    } else if (cidr) qs.set('cidr', cidr);
    if (typeName === '其他') {
      qs.set('alarm_type', '__other__');
      if (typeExclude.length) qs.set('type_exclude', typeExclude.join(','));
    } else if (typeName) qs.set('alarm_type', typeName);
    return qs.toString();
  }, [cidr, typeName, cidrExclude, typeExclude, groups]);

  useEffect(() => { rowsRef.current = rows; }, [rows]);

  const fetchPage = useCallback(async (p: number) => {
    const r = await fetch(`/api/nms/alarms?${query(p)}`,
      { headers: { Authorization: `Bearer ${token || ''}` } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d: NmsData = await r.json();
    return {
      alarms: d.latest?.alarms || [],
      total: d.latest?.filtered_total ?? 0,
      hasMore: !!d.latest?.has_more,
    };
  }, [query, token]);

  const load = useCallback(async (silent: boolean) => {
    // 静默刷新与首屏加载/加载更多互斥；前台加载不可跳过
    if (silent && busyRef.current) return;
    busyRef.current = true;
    const rid = ++reqRef.current;
    if (silent) setRefreshing(true); else { setLoading(true); setRefreshing(false); }
    if (!silent) setError('');
    try {
      // 静默刷新按已加载页数补齐，保持「加载更多」的偏移不变
      const pages = silent ? Math.max(1, Math.ceil(rowsRef.current.length / PAGE_SIZE)) : 1;
      const acc: NmsAlarm[] = [];
      let res: { alarms: NmsAlarm[]; total: number; hasMore: boolean } =
        { alarms: [], total: 0, hasMore: false };
      for (let p = 1; p <= pages; p += 1) {
        res = await fetchPage(p);
        acc.push(...res.alarms);
        if (!res.hasMore) break;
      }
      if (rid !== reqRef.current) return;
      setRows(acc);
      setTotal(res.total);
      setPage(Math.max(1, Math.ceil(acc.length / PAGE_SIZE)));
      setHasMore(res.hasMore);
      setRowsKey(key);
      setSyncedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      setError('');
    } catch (e) {
      if (rid !== reqRef.current) return;
      // 静默刷新失败时保留当前列表，下个周期重试
      if (!silent) {
        setRows([]); setTotal(0); setHasMore(false);
        setError(e instanceof Error ? e.message : '加载失败');
      }
    } finally {
      if (rid === reqRef.current) {
        setLoading(false); setRefreshing(false); busyRef.current = false;
      }
    }
  }, [fetchPage, key]);

  // 打开/换条件：旧结果已失效，重新加载；打开期间每 15s 静默刷新
  useEffect(() => {
    if (!open || !active) return;
    // 首个请求放到宏任务发起，避免在 effect 体内同步 setState
    const kick = setTimeout(() => { void load(false); }, 0);
    const timer = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void load(true);
    }, 15000);
    return () => {
      clearTimeout(kick); clearInterval(timer);
      reqRef.current += 1; busyRef.current = false;
    };
  }, [open, active, key, load]);

  // 快照更新（手动抓取/后台采集完成）：立即静默刷新，不等轮询
  useEffect(() => {
    if (!open || !active || !refreshKey) return;
    const kick = setTimeout(() => { void load(true); }, 0);
    return () => clearTimeout(kick);
  }, [refreshKey, open, active, load]);

  const loadMore = async () => {
    if (loading || refreshing || busyRef.current) return;
    busyRef.current = true;
    const rid = reqRef.current;
    const next = page + 1;
    setLoading(true);
    try {
      const d = await fetchPage(next);
      if (rid !== reqRef.current) return;
      setRows(prev => [...prev, ...d.alarms]);
      setHasMore(d.hasMore);
      setTotal(d.total);
      setPage(next);
    } catch { /* 保留已加载数据 */ } finally {
      if (rid === reqRef.current) { setLoading(false); busyRef.current = false; }
    }
  };

  const fmtTs = (a: NmsAlarm) => {
    const ts = a.last_time || a.time || a.first_time || '';
    return ts ? ts.replace('T', ' ').slice(5, 16) : '—';
  };

  return (
    <aside aria-hidden={!open} inert={!open}
      className="h-full w-full bg-white border-l border-[#E2E8F0] flex flex-col">
      {/* 头部：当前筛选条件 */}
      <div className="px-4 py-3 border-b border-[#E2E8F0] flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-[#0F172A]">告警详情</div>
          <div className="flex items-center gap-1.5 flex-wrap mt-1.5">
            {cidr && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border border-[#FDBA74] bg-[#FFF7ED] text-[#C2410C]">
                <Network size={9} /><span className="font-mono">{cidr === '其他' ? '其他网段' : cidr}</span>
              </span>
            )}
            {typeName && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border border-[#C4B5FD] bg-[#F5F3FF] text-[#6D28D9] max-w-[260px]">
                <Tag size={9} /><span className="truncate">{typeName === '其他' ? '其他类型' : typeName}</span>
              </span>
            )}
            {!active && <span className="text-[11px] text-[#94A3B8]">点击矩阵格子或网段/类型行</span>}
          </div>
        </div>
        <button onClick={onClose} title="关闭"
          className="p-1 rounded-md text-[#94A3B8] hover:text-[#334155] hover:bg-[#F1F5F9] transition-colors shrink-0">
          <X size={14} />
        </button>
      </div>

      {/* Tab */}
      <div className="px-4 flex items-center gap-4 border-b border-[#F1F5F9] shrink-0">
        {([
          { k: 'alarms' as const, label: '相关告警', n: !error && stale ? -1 : shownTotal },
          { k: 'events' as const, label: '命中事件', n: incidents.length },
        ]).map(t => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`pb-2 pt-2 text-[12px] border-b-2 -mb-px transition-colors ${
              tab === t.k ? 'border-[#DC2626] text-[#0F172A] font-medium'
                : 'border-transparent text-[#64748B] hover:text-[#334155]'}`}>
            {t.label} <span className="tabular-nums opacity-60">{t.n < 0 ? '…' : t.n}</span>
          </button>
        ))}
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto">
        {tab === 'alarms' ? (
          error ? (
            <div className="py-14 text-center px-6">
              <p className="text-[11px] text-[#DC2626]">告警加载失败：{error}</p>
              <button onClick={() => load(false)}
                className="mt-2 px-2.5 py-1 rounded-md border border-[#E2E8F0] bg-white text-[11px] text-[#475569] hover:border-[#CBD5E1] transition-colors">
                重试
              </button>
            </div>
          ) : stale || (loading && shownRows.length === 0) ? (
            // 旧条件的数据一律不渲染：换条件后直接过淘到骨架屏，不等请求返回
            <div className="px-4 py-4 space-y-3">
              {[0, 1, 2, 3].map(i => (
                <div key={i} className="animate-pulse space-y-1.5">
                  <div className="h-2 w-1/3 rounded bg-[#F1F5F9]" />
                  <div className="h-2 w-full rounded bg-[#F8FAFC]" />
                  <div className="h-2 w-2/3 rounded bg-[#F8FAFC]" />
                </div>
              ))}
              <p className="text-[10px] text-[#94A3B8] text-center pt-1">加载中…</p>
            </div>
          ) : shownRows.length === 0 ? (
            <div className="py-14 text-center px-6">
              <p className="text-[11px] text-[#64748B]">当前条件下没有告警</p>
              <p className="text-[10px] text-[#94A3B8] mt-1">矩阵计数基于全量快照，可能含已清除告警</p>
            </div>
          ) : (
            <>
              <div className="divide-y divide-[#F1F5F9]">
                {shownRows.map(a => {
                  const m = sev(a.severity);
                  return (
                    <div key={a.alarm_id} className="px-4 py-2.5 hover:bg-[#F8FAFC] transition-colors">
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: m.dot }} />
                        <span className="text-[10px] shrink-0" style={{ color: m.color }}>{m.label}</span>
                        <span className="text-[10px] font-mono text-[#64748B] truncate">{a.source || a.node || '—'}</span>
                        <span className="ml-auto text-[10px] text-[#94A3B8] tabular-nums shrink-0">{fmtTs(a)}</span>
                      </div>
                      <div className="text-[11px] text-[#334155] leading-4 line-clamp-2">{a.summary || '(无摘要)'}</div>
                      <div className="mt-0.5 text-[10px] text-[#94A3B8] truncate">
                        {alarmGroups(a).join('、') || '未分组'}
                        {a.acknowledged === '1' && <span className="ml-1.5 text-[#16A34A]">已确认</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
              {shownHasMore && (
                <div className="py-2.5 text-center">
                  <button onClick={loadMore} disabled={loading}
                    className="px-3 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[11px] text-[#475569] hover:border-[#CBD5E1] disabled:opacity-60 transition-colors">
                    {loading ? '加载中…' : `加载更多（已显示 ${shownRows.length}/${shownTotal}）`}
                  </button>
                </div>
              )}
            </>
          )
        ) : (
          incidents.length === 0 ? (
            <div className="py-14 text-center px-6 text-[11px] text-[#64748B]">当前条件下没有匹配事件</div>
          ) : (
            <div className="divide-y divide-[#F1F5F9]">
              {incidents.map(inc => {
                const sm = sev(inc.severity);
                return (
                  <button key={inc.incident_id} onClick={() => onOpenIncident(inc.incident_id)}
                    className="w-full text-left px-4 py-2.5 hover:bg-[#F8FAFC] transition-colors">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono" style={{ color: sm.color }}>P{inc.priority}</span>
                      <span className="text-[10px] text-[#64748B]">{inc.category === 'systemic' ? '系统级' : '对象级'}</span>
                      {inc.scope && (
                        <span className="text-[10px]" style={{ color: SCOPE_META[inc.scope].color }}>
                          {SCOPE_META[inc.scope].label}
                        </span>
                      )}
                      <span className="ml-auto text-[10px] text-[#94A3B8] tabular-nums shrink-0">{inc.member_count} 条</span>
                    </div>
                    <div className="text-[11px] text-[#334155] leading-4 line-clamp-2 mt-0.5">{inc.title}</div>
                  </button>
                );
              })}
            </div>
          )
        )}
      </div>

      {/* 底部：清除与同步状态 */}
      <div className="px-4 py-2 border-t border-[#F1F5F9] flex items-center justify-between shrink-0">
        <button onClick={onClear} className="text-[11px] text-[#94A3B8] hover:text-[#DC2626] transition-colors">
          清除筛选
        </button>
        <span className="text-[10px] text-[#94A3B8] flex items-center gap-1">
          {active && <RefreshCw size={9} className={refreshing ? 'animate-spin' : ''} />}
          {active ? `活跃告警 · ${stale || !syncedAt ? '加载中' : `更新于 ${syncedAt}`}` : '口径：当前分组 · 活跃告警'}
        </span>
      </div>
    </aside>
  );
}

// ─── Main component ─────────────────────────────────────────────────────────────

function NmsContent() {
  const [data, setData] = useState<NmsData | null>(null);
  const [historyData, setHistoryData] = useState<NmsHistoryData | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<'incidents' | 'alarms' | 'history' | 'analysis' | 'checklist'>('incidents');
  const [alarmStatus, setAlarmStatus] = useState<'active' | 'cleared'>('active');
  const [selectedSeverities, setSelectedSeverities] = useState<Set<string>>(new Set());
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [groupsTouched, setGroupsTouched] = useState(false);
  const [groupSearch, setGroupSearch] = useState('');
  const [searchText, setSearchText] = useState('');
  // Phase 2.1: 时间范围筛选 (ISO-8601, 空 = 不限)
  const [timeFrom, setTimeFrom] = useState<string>('');
  const [timeTo, setTimeTo] = useState<string>('');
  const [ackFilter, setAckFilter] = useState<'' | 'unacked' | 'acked'>('');
  const [refreshMsg, setRefreshMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [filteredTotal, setFilteredTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);
  // 翻页期间探测到的新采集快照：不打断当前列表，提示用户一键回第一页刷新
  const [newSnapshot, setNewSnapshot] = useState<{ ts: string; total?: number } | null>(null);
  const [collectInterval, setCollectInterval] = useState(0);  // 当前生效采集间隔（秒）
  const [defaultGroups, setDefaultGroups] = useState<string[]>([]);
  const [showAllGroups, setShowAllGroups] = useState(false);
  const [groupPanelOpen, setGroupPanelOpen] = useState(false);
  // ── 事件视图（关联降噪）──
  const [incidentsData, setIncidentsData] = useState<NmsIncidentsData | null>(null);
  // 首屏总会拉一次事件，初始置 true：避免默认分组请求返回前闪现「NMS 凭据未配置」空态
  const [incidentsLoading, setIncidentsLoading] = useState(true);
  // 事件拉取失败原因：区分「请求失败」与「后端凭据未配置」，避免把网络异常误报成未配置
  const [incidentsError, setIncidentsError] = useState('');
  const [expandedIncident, setExpandedIncident] = useState<string | null>(null);
  // 事件视图筛选/排序（与分组筛选叠加，仅前端过滤）
  const [incSev, setIncSev] = useState<Set<string>>(new Set());
  const [incCategory, setIncCategory] = useState<'all' | 'systemic' | 'object'>('all');
  const [incScope, setIncScope] = useState<'all' | 'cross' | 'site' | 'object'>('all');
  // 维度筛选：从网段 Top / 类型 Top / 矩阵格子联动筛选事件列表（与上方条件叠加）
  const [incSeg, setIncSeg] = useState<string | null>(null);
  const [incType, setIncType] = useState<string | null>(null);
  // 「其他」桶的排除集（矩阵已列出的 Top7），仅在 incSeg/incType === '其他' 时非空
  const [incSegEx, setIncSegEx] = useState<string[]>([]);
  const [incTypeEx, setIncTypeEx] = useState<string[]>([]);
  // 右侧筛选面板（按需滑出，不常驻占宽）
  const [drillOpen, setDrillOpen] = useState(false);
  // 打开轮次：每次打开递增，面板据此丢弃上一轮缓存的告警（关闭后重新打开也要重拉）
  const [drillSeq, setDrillSeq] = useState(0);
  // ── SSL 证书调查（与告警详情共用右侧停靠区，互斥展示）──
  const [sslOpen, setSslOpen] = useState(false);
  const [sslTarget, setSslTarget] = useState<SslPanelTarget | null>(null);
  const [sslSeq, setSslSeq] = useState(0);
  // 从抽屉点事件后的定位：主列表只保留该事件，避免扩容渲染上千张卡片
  const [incFocus, setIncFocus] = useState<string | null>(null);
  const [incSearch, setIncSearch] = useState('');
  const [incSort, setIncSort] = useState<'priority' | 'recent' | 'members'>('priority');
  const [incLimit, setIncLimit] = useState(30);
  // ── 智能分析（按分组，手动触发 + 弹窗确认） ──
  const [analysisGroup, setAnalysisGroup] = useState('');
  const [analysisData, setAnalysisData] = useState<{ analysis: NmsAnalysis | null; group: string; precomputed: boolean; message?: string } | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirmAnalyze, setConfirmAnalyze] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // A4: 分析 outcome (采纳/已解决/未解决/误报) — 反哺 KB
  const [outcomeMsg, setOutcomeMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // ── Checklist 预案匹配（确定性，随分析分组联动）──
  const [checklistData, setChecklistData] = useState<ChecklistData | null>(null);
  const [checklistLoading, setChecklistLoading] = useState(false);
  // ── Checklist 预案清单（仅管理员可访问，后端 require_admin 双重拦截）──
  const [isAdmin, setIsAdmin] = useState(false);
  const [plansData, setPlansData] = useState<ChecklistPlansData | null>(null);
  const [plansLoading, setPlansLoading] = useState(false);
  const [expandedPlan, setExpandedPlan] = useState<string | null>(null);
  const [planForm, setPlanForm] = useState<PlanFormData | null>(null);
  const [planSaving, setPlanSaving] = useState(false);
  const [deletingPlan, setDeletingPlan] = useState<ChecklistPlan | null>(null);
  const [planMsg, setPlanMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const token = typeof window !== 'undefined' ? localStorage.getItem('zops_token') : '';
  const headers = useMemo(() => ({ 'Authorization': `Bearer ${token || ''}` }), [token]);
  // 当前列表的基线快照时间戳（判断后台是否已完成新一轮采集）
  const snapshotTsRef = useRef('');
  // 用户已点了「暂不刷新」的快照：同一快照不再重复提示
  const dismissedTsRef = useRef('');

  const fetchData = useCallback(async (groupsParam?: string, page = 1, append = false, statusParam?: 'active' | 'cleared') => {
    if (!append) setLoading(true);
    try {
      const gp = groupsParam !== undefined ? groupsParam : [...selectedGroups].join(',');
      const st = statusParam || alarmStatus;
      const timeParams = (timeFrom || timeTo)
        ? `&from_ts=${encodeURIComponent(timeFrom || '')}&to_ts=${encodeURIComponent(timeTo || '')}`
        : '';
      const url = `/api/nms/alarms?page=${page}&page_size=500&status=${st}${gp ? `&groups=${encodeURIComponent(gp)}` : ''}${timeParams}`;
      const alarmsRes = await fetch(url, { headers });
      if (alarmsRes.ok) {
        const d: NmsData = await alarmsRes.json();
        if (append && d.latest) {
          setData(prev => {
            if (!prev?.latest || !d.latest) return d;
            return { ...d, latest: { ...d.latest, alarms: [...prev.latest.alarms, ...d.latest.alarms] } };
          });
        } else {
          setData(d);
        }
        setHasMore(d.latest?.has_more || false);
        setFilteredTotal(d.latest?.filtered_total ?? d.latest?.alarms?.length ?? 0);
        setCurrentPage(d.latest?.page || page);
        // 第一页即最新快照：重置基线并清除「有新数据」提示
        if ((d.latest?.page || page) === 1 && !append) {
          snapshotTsRef.current = d.latest?.timestamp || '';
          dismissedTsRef.current = '';
          setNewSnapshot(null);
        }
      }
    } catch (e) {
      console.error('NMS fetch error:', e);
    }
    if (!append) setLoading(false);
  }, [headers, selectedGroups, alarmStatus, timeFrom, timeTo]);

  // 拉取历史告警库 (内容键去重事件, 30 天保留; 分组/时间过滤+分页在服务端做)
  const fetchHistory = useCallback(async (page = 1, append = false, loadingMore = false) => {
    if (!loadingMore) setHistoryLoading(true);
    try {
      const gp = [...selectedGroups].join(',');
      const timeParams = (timeFrom || timeTo)
        ? `&from_ts=${encodeURIComponent(timeFrom || '')}&to_ts=${encodeURIComponent(timeTo || '')}`
        : '';
      const url = `/api/nms/history?limit=200&page=${page}${gp ? `&groups=${encodeURIComponent(gp)}` : ''}${timeParams}`;
      const res = await fetch(url, { headers });
      if (res.ok) {
        const d: NmsHistoryData = await res.json();
        setHistoryData(prev => (append && prev
          ? { ...d, alarms: [...prev.alarms, ...d.alarms] }
          : d));
        setHistoryPage(d.page || page);
        setHistoryHasMore(!!d.has_more);
        // 第一页已是最新历史：清空基线，由下次探测静默采纳当前快照
        if ((d.page || page) === 1 && !append) {
          snapshotTsRef.current = '';
          setNewSnapshot(null);
        }
      }
    } catch (e) {
      console.error('NMS history fetch error:', e);
    }
    if (!loadingMore) setHistoryLoading(false);
  }, [headers, selectedGroups, timeFrom, timeTo]);

  // 拉取关联降噪事件簇（按选中分组过滤）
  // 最近一次成功拉取时间：面板打开时据此判断是否需要补拉，避免展示旧事件
  const lastIncFetchRef = useRef(0);
  const fetchIncidents = useCallback(async (groupsParam?: string, silent = false) => {
    // silent：后台刷新，不驱动「刷新」按钮的旋转态，避免每次轮询都闪一下
    if (!silent) setIncidentsLoading(true);
    setIncidentsError('');
    try {
      const gp = groupsParam !== undefined ? groupsParam : [...selectedGroups].join(',');
      const url = `/api/nms/incidents${gp ? `?groups=${encodeURIComponent(gp)}` : ''}`;
      const res = await fetch(url, { headers });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d: NmsIncidentsData = await res.json();
      setIncidentsData(d);
      lastIncFetchRef.current = Date.now();
      // 后端异常会被包装成 {error} 返回，需原样暴露而不是当成「无数据」
      if (d?.error) setIncidentsError(String(d.error));
    } catch (e) {
      console.error('NMS incidents fetch error:', e);
      const msg = e instanceof Error ? e.message : '网络异常';
      setIncidentsError(msg === 'Failed to fetch' ? '无法连接后端服务' : msg);
    }
    if (!silent) setIncidentsLoading(false);
  }, [headers, selectedGroups]);

  // 打开告警详情面板时：事件快照超过 15s 未更新就先后台补拉，同时在面板打开期间每 20s 静默刷新一次，
  // 保证「命中事件」与告警列表同步；事件接口 payload 仅 ~30KB，代价可控
  useEffect(() => {
    if (!drillOpen) return;
    if (Date.now() - lastIncFetchRef.current >= 15000) {
      void fetchIncidents([...selectedGroups].join(','), true);
    }
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void fetchIncidents([...selectedGroups].join(','), true);
    }, 20000);
    return () => clearInterval(id);
  }, [drillOpen, fetchIncidents, selectedGroups]);

  // 轻量探测后台是否已完成新一轮采集（只读快照头部，不做过滤/排序）
  // adopt=true: 把当前快照作为新基线（用于首屏/刚刷新完第一页的场景）
  const probeSnapshot = useCallback(async (adopt = false) => {
    try {
      const res = await fetch('/api/nms/snapshot-meta', { headers });
      if (!res.ok) return;
      const d = await res.json();
      if (d?.check_interval) setCollectInterval(d.check_interval);
      const ts: string = d?.timestamp || '';
      if (!ts) return;
      if (adopt || !snapshotTsRef.current) {
        snapshotTsRef.current = ts;
        dismissedTsRef.current = '';
        setNewSnapshot(null);
        return;
      }
      if (ts !== snapshotTsRef.current && ts !== dismissedTsRef.current) {
        setNewSnapshot({ ts, total: d.total });
      }
    } catch { /* 单次探测失败忽略 */ }
  }, [headers]);

  // 首屏探测一次，拿到当前快照基线与生效采集间隔
  useEffect(() => { probeSnapshot(true); }, [probeSnapshot]);

  // ── Phase 2.3: 筛选状态持久化 (localStorage) ──
  // 保存: 分组 / 级别 / ACK / 分析分组 / 时间范围; 恢复失败静默回退默认
  const NMS_VIEW_KEY = 'zops_nms_view_v1';
  useEffect(() => {
    try {
      const raw = localStorage.getItem(NMS_VIEW_KEY);
      if (!raw) return;
      const v = JSON.parse(raw);
      if (Array.isArray(v.groups) && v.groups.length) setSelectedGroups(new Set(v.groups));
      if (Array.isArray(v.severities)) setSelectedSeverities(new Set(v.severities));
      if (typeof v.ackFilter === 'string') setAckFilter(v.ackFilter as '' | 'acked' | 'unacked');
      if (typeof v.analysisGroup === 'string' && v.analysisGroup) setAnalysisGroup(v.analysisGroup);
      if (typeof v.timeFrom === 'string') setTimeFrom(v.timeFrom);
      if (typeof v.timeTo === 'string') setTimeTo(v.timeTo);
    } catch { /* 损坏的存储直接忽略 */ }
  }, []);
  // 任一筛选变化 → 写回 (try/catch 保护隐私模式)
  useEffect(() => {
    try {
      localStorage.setItem(NMS_VIEW_KEY, JSON.stringify({
        groups: [...selectedGroups],
        severities: [...selectedSeverities],
        ackFilter,
        analysisGroup,
        timeFrom,
        timeTo,
      }));
    } catch { /* 存储不可用时忽略 */ }
  }, [selectedGroups, selectedSeverities, ackFilter, analysisGroup, timeFrom, timeTo]);

  // 首次加载：取用户默认分组，仅拉取该范围告警（避免 6 万条重叠视图的噪声与错误统计）
  useEffect(() => {
    (async () => {
      let dg: string[] = [];
      try {
        const res = await fetch('/api/nms/default-groups', { headers });
        if (res.ok) { const j = await res.json(); dg = Array.isArray(j.groups) ? j.groups.filter(Boolean) : []; }
      } catch { /* 忽略，回退全部分组 */ }
      setDefaultGroups(dg);
      // Phase 2.3: 已恢复的持久化分组优先于默认分组 (用户上次的选择更权威)
      const savedRaw = typeof localStorage !== 'undefined' ? localStorage.getItem(NMS_VIEW_KEY) : null;
      let savedGroups: string[] = [];
      try { savedGroups = savedRaw ? (JSON.parse(savedRaw).groups || []) : []; } catch { /* ignore */ }
      const useGroups = savedGroups.length ? savedGroups : dg;
      if (useGroups.length) {
        setSelectedGroups(new Set(useGroups));
        await fetchData(useGroups.join(','), 1);
      } else {
        await fetchData('', 1);
      }
      // 预拉一次事件簇，使页签徽标计数与事件视图初始数据就绪
      fetchIncidents(useGroups.length ? useGroups.join(',') : '');
    })();
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  // 定时自动刷新：每 60s 拉取当前页签数据，反映 nms_agent 后台更新（页面隐藏时暂停）
  // 翻页/加载更多时不再静默跳过：改为探测新快照并提示，由用户决定何时回第一页
  useEffect(() => {
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      if (activeTab === 'incidents') {
        fetchIncidents([...selectedGroups].join(','), true);
      } else if (activeTab === 'history') {
        if (historyPage <= 1 && !loadingMore) fetchHistory(1, false);
        else probeSnapshot();
      } else if (activeTab === 'alarms') {
        if (currentPage <= 1 && !loadingMore) {
          fetchData([...selectedGroups].join(','), 1, false, alarmStatus);
        } else {
          probeSnapshot();
        }
      }
    }, 60000);
    return () => clearInterval(id);
  }, [activeTab, alarmStatus, selectedGroups, currentPage, historyPage, loadingMore, fetchData, fetchIncidents, fetchHistory, probeSnapshot]);

  // 用户手动改动分组后按新范围重新加载
  useEffect(() => {
    if (!groupsTouched) return;
    setNewSnapshot(null);
    snapshotTsRef.current = '';
    fetchData([...selectedGroups].join(','), 1);
    if (activeTab === 'incidents') fetchIncidents([...selectedGroups].join(','));
    if (activeTab === 'history') fetchHistory(1);
  }, [selectedGroups, groupsTouched]);  // eslint-disable-line react-hooks/exhaustive-deps

  const loadMore = () => {
    setLoadingMore(true);
    const gp = [...selectedGroups].join(',');
    fetch(`/api/nms/alarms?page=${currentPage + 1}&page_size=500&status=${alarmStatus}${gp ? `&groups=${encodeURIComponent(gp)}` : ''}`, { headers })
      .then(r => r.ok ? r.json() : null)
      .then((d: NmsData | null) => {
        const latest = d?.latest;
        if (!d || !latest) return;
        setData(prev => {
          if (!prev?.latest) return d;
          return { ...d, latest: { ...latest, alarms: [...prev.latest.alarms, ...latest.alarms] } };
        });
        setHasMore(latest.has_more || false);
        setCurrentPage(latest.page || currentPage + 1);
      })
      .catch(() => {})
      .finally(() => setLoadingMore(false));
  };

  const loadMoreHistory = () => {
    setLoadingMore(true);
    fetchHistory(historyPage + 1, true, true)
      .finally(() => setLoadingMore(false));
  };

  // 一键回到第一页并刷新（丢弃当前分页追加的旧数据，切到最新快照）
  const backToFirstPage = async () => {
    setNewSnapshot(null);
    if (activeTab === 'history') {
      setHistoryPage(1);
      await fetchHistory(1, false);
    } else {
      setCurrentPage(1);
      await fetchData([...selectedGroups].join(','), 1, false, alarmStatus);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await fetch('/api/nms/refresh', { method: 'POST', headers });
      const d = await res.json();
      setRefreshMsg({ ok: d.success, text: d.message || d.error || '未知结果' });
      setTimeout(() => setRefreshMsg(null), 5000);
      if (!d.success) { setRefreshing(false); return; }
      let tries = 0;
      const poll = async () => {
        tries += 1;
        try {
          const aRes = await fetch(`/api/nms/alarms?status=${alarmStatus}&groups=${encodeURIComponent([...selectedGroups].join(','))}`, { headers });
          if (aRes.ok) {
            const ad: NmsData = await aRes.json();
            if (ad.latest && !ad.pending) {
              setData(ad);
              // 手动抓取已完成：基线同步到新快照，避免下一轮探测误报「有新数据」
              snapshotTsRef.current = ad.latest.timestamp || '';
              setNewSnapshot(null);
              // 同步刷新事件视图（关联降噪结果随快照一同更新）
              fetchIncidents([...selectedGroups].join(','));
              setRefreshing(false);
              return;
            }
          }
        } catch { /* 忽略单次轮询错误 */ }
        if (tries >= 6) { setRefreshing(false); return; }
        setTimeout(poll, 5000);
      };
      setTimeout(poll, 5000);
    } catch {
      setRefreshMsg({ ok: false, text: '请求失败' });
      setTimeout(() => setRefreshMsg(null), 5000);
      setRefreshing(false);
    }
  };

  // ── 数据统计 ──
  const alarms = useMemo(() => data?.latest?.alarms || [], [data]);
  // 当前页签的数据源: 历史告警=历史库事件, 其余=快照告警
  const sourceAlarms = useMemo(
    () => (activeTab === 'history' ? (historyData?.alarms || []) : alarms),
    [activeTab, historyData, alarms]);
  // 事件簇与成员查找（基于当前已加载快照）
  const incidents = useMemo(() => incidentsData?.incidents || [], [incidentsData]);
  // 当前范围内事件所含告警总数（供事件视图概览显示，随分组过滤联动）
  const shownAlarmCount = useMemo(
    () => incidents.reduce((s, i) => s + (i.member_count || 0), 0), [incidents]);

  // ── 事件视图：级别分布 + 前端筛选/排序（分组已在服务端过滤）──
  const scopeAnalysis = incidentsData?.scope_analysis || null;
  const incidentSeverityCounts = useMemo(() => {
    const acc: Record<string, number> = {};
    incidents.forEach(i => { const s = i.severity || 'unknown'; acc[s] = (acc[s] || 0) + 1; });
    return acc;
  }, [incidents]);

  const incidentScopeCounts = useMemo(() => {
    const acc = { cross: 0, site: 0, object: 0 };
    incidents.forEach(i => { if (i.scope) acc[i.scope] += 1; });
    return acc;
  }, [incidents]);

  const filteredIncidents = useMemo(() => {
    let list = incidents;
    if (incSev.size > 0) list = list.filter(i => incSev.has(i.severity));
    if (incCategory !== 'all') list = list.filter(i => i.category === incCategory);
    if (incScope !== 'all') list = list.filter(i => i.scope === incScope);
    if (incSeg === '其他') {
      // 事件含任一「未进入 Top7」的非空网段即命中，与矩阵其他行口径一致
      list = list.filter(i => (i.segments || []).some(s => s.cidr && !incSegEx.includes(s.cidr)));
    } else if (incSeg) {
      list = list.filter(i => (i.segments || []).some(s => s.cidr === incSeg));
    }
    if (incType === '其他') {
      list = list.filter(i => (i.types || []).some(t => !incTypeEx.includes(t.name)));
    } else if (incType) {
      list = list.filter(i => (i.types || []).some(t => t.name === incType));
    }
    if (incFocus) list = list.filter(i => i.incident_id === incFocus);
    const q = incSearch.trim().toLowerCase();
    if (q) {
      list = list.filter(i =>
        (i.title || '').toLowerCase().includes(q) ||
        (i.sources || []).join(',').toLowerCase().includes(q) ||
        (i.groups || []).join(',').toLowerCase().includes(q) ||
        (i.segments || []).map(s => s.cidr).join(',').toLowerCase().includes(q) ||
        (i.types || []).map(t => t.name).join(',').toLowerCase().includes(q));
    }
    const lastTs = (i: NmsIncident) => parseNmsTime(i.last_time) ?? parseNmsTime(i.first_time) ?? 0;
    if (incSort === 'recent') return [...list].sort((a, b) => lastTs(b) - lastTs(a));
    if (incSort === 'members') return [...list].sort((a, b) => (b.member_count || 0) - (a.member_count || 0));
    return [...list].sort((a, b) => (b.priority || 0) - (a.priority || 0));
  }, [incidents, incSev, incCategory, incScope, incSeg, incSegEx, incType, incTypeEx, incFocus, incSearch, incSort]);

  // 筛选开关：同值点击取消；网段与类型可独立叠加
  const drillSeg = useCallback((cidr: string) => {
    setIncLimit(30);
    setSslOpen(false);
    setDrillOpen(true);
    setDrillSeq(s => s + 1);
    setIncSeg(prev => (prev === cidr ? null : cidr));
    setIncSegEx([]);
  }, []);
  const drillType = useCallback((name: string) => {
    setIncLimit(30);
    setSslOpen(false);
    setDrillOpen(true);
    setDrillSeq(s => s + 1);
    setIncType(prev => (prev === name ? null : name));
    setIncTypeEx([]);
  }, []);
  const drillCell = useCallback((cidr: string, type: string) => {
    setIncLimit(30);
    setSslOpen(false);
    const same = incSeg === cidr && incType === type;
    // 点「其他」时记录当前矩阵已列出的 Top7，用于构造排除集
    const listedSegs = (scopeAnalysis?.matrix?.segments || []).filter(s => s !== '其他');
    const listedTypes = (scopeAnalysis?.matrix?.types || []).filter(t => t !== '其他');
    setIncSeg(same ? null : cidr);
    setIncSegEx(!same && cidr === '其他' ? listedSegs : []);
    setIncType(same ? null : type);
    setIncTypeEx(!same && type === '其他' ? listedTypes : []);
    setDrillOpen(!same);
    if (!same) setDrillSeq(s => s + 1);
  }, [incSeg, incType, scopeAnalysis]);
  const clearDrill = useCallback(() => {
    setIncSeg(null); setIncType(null); setIncFocus(null);
    setIncSegEx([]); setIncTypeEx([]);
    setIncLimit(30); setDrillOpen(false);
  }, []);
  // 从侧栏点事件 → 主列表聚焦该事件并展开，同时收起抽屉
  const openIncidentFromPanel = useCallback((id: string) => {
    setIncFocus(id);
    setExpandedIncident(id);
    setIncLimit(30);
    setDrillOpen(false);
    setTimeout(() => {
      document.getElementById(`incident-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 180);
  }, []);

  // ── SSL 调查：从具体原告警发起（事件内也逐条选择，不用整簇成员推断健康）──
  const openSslForAlarm = useCallback((alarm: NmsAlarm, incidentId?: string) => {
    const ts = data?.latest?.timestamp || '';
    const ref = data?.latest?.snapshot_ref || '';
    if (!ts || !ref) {
      setRefreshMsg({ ok: false, text: '暂无可用告警快照，请等本轮采集完成后再发起调查' });
      return;
    }
    setDrillOpen(false);
    setSslTarget({
      alarmId: alarm.alarm_id,
      snapshotTimestamp: ts,
      snapshotRef: ref,
      incidentId: incidentId || undefined,
      summary: alarm.summary,
      source: alarm.source,
    });
    setSslOpen(true);
    setSslSeq(s => s + 1);
  }, [data]);

  const openSslRecords = useCallback(() => {
    setDrillOpen(false);
    setSslTarget(null);
    setSslOpen(true);
    setSslSeq(s => s + 1);
  }, []);

  // 快照已更新：用页面最新快照重新校验，不自动重放其他动作
  const retrySslWithLatest = useCallback(() => {
    setSslTarget(prev => {
      if (!prev) return prev;
      const ts = data?.latest?.timestamp;
      const ref = data?.latest?.snapshot_ref;
      if (!ts || !ref) return prev;
      return { ...prev, snapshotTimestamp: ts, snapshotRef: ref };
    });
    setSslSeq(s => s + 1);
  }, [data]);

  const toggleIncSev = useCallback((s: string) => {
    setIncLimit(30);
    setIncSev(prev => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
  }, []);
  const alarmMap = useMemo(() => {
    const m: Record<string, NmsAlarm> = {};
    alarms.forEach(a => { if (a.alarm_id) m[a.alarm_id] = a; });
    return m;
  }, [alarms]);

  const groupStats = useMemo(() => {
    // 面板计数用活跃口径（与表格默认展示一致），无则回退全量口径
    const gc = data?.latest?.group_counts_active || data?.latest?.group_counts;
    if (gc && Object.keys(gc).length > 0) {
      const m: Record<string, { count: number; crit: number }> = {};
      Object.entries(gc).forEach(([g, count]) => { m[g] = { count: count as number, crit: 0 }; });
      alarms.forEach(a => {
        if (a.severity !== 'critical' && a.severity !== 'major') return;
        alarmGroups(a).forEach(g => { if (m[g]) m[g].crit += 1; });
      });
      return m;
    }
    const m: Record<string, { count: number; crit: number }> = {};
    alarms.forEach(a => {
      const g = a.group || '未分组';
      if (!m[g]) m[g] = { count: 0, crit: 0 };
      m[g].count += 1;
      if (a.severity === 'critical' || a.severity === 'major') m[g].crit += 1;
    });
    return m;
  }, [alarms, data?.latest?.group_counts_active, data?.latest?.group_counts]);

  const availableGroups = useMemo(
    () => Object.keys(groupStats).sort((a, b) =>
      (groupStats[b].crit - groupStats[a].crit) || (groupStats[b].count - groupStats[a].count) || a.localeCompare(b)),
    [groupStats]
  );

  // 行内分组徽标显示优先级：当前筛选 > 用户默认分组 > 具体业务视图 > 宽状态视图（覆盖≥半数，如 Escalated/Unacknowledged）> 兜底视图
  // 返回复合分：rank*1e9 + 分组规模，同 rank 内成员数少（更具业务区分度）的视图优先展示
  const groupRank = useCallback((g: string) => {
    const gc = data?.latest?.group_counts;
    const total = data?.latest?.total || 0;
    const cnt = (gc && gc[g]) || 0;
    if (selectedGroups.has(g)) return cnt;
    if (defaultGroups.includes(g)) return 1e9 + cnt;
    if ((total > 0 && cnt >= total) || g === 'Default' || g === 'AllEvents') return 4e9 + cnt;
    if (total > 0 && cnt >= total * 0.5) return 3e9 + cnt;
    return 2e9 + cnt;
  }, [selectedGroups, defaultGroups, data?.latest?.group_counts, data?.latest?.total]);

  // 面板默认只展示「用户默认分组」，可切换查看全部分组
  const panelGroups = useMemo(
    () => (showAllGroups || defaultGroups.length === 0) ? availableGroups : defaultGroups,
    [showAllGroups, defaultGroups, availableGroups]
  );

  // 无默认分组时，回退为全选全部可用分组
  useEffect(() => {
    if (!groupsTouched && defaultGroups.length === 0 && availableGroups.length)
      setSelectedGroups(new Set(availableGroups));
  }, [availableGroups, groupsTouched, defaultGroups]);

  const allSelected = panelGroups.length > 0 && panelGroups.every(g => selectedGroups.has(g));

  // 切换分组后矩阵 Top7 会变，「其他」排除集失效 → 统一清空筛选条件
  const resetIncDrill = useCallback(() => {
    setIncFocus(null);
    setIncSeg(null); setIncSegEx([]);
    setIncType(null); setIncTypeEx([]);
    setDrillOpen(false);
  }, []);

  const toggleGroup = (g: string) => {
    setGroupsTouched(true);
    resetIncDrill();
    setSelectedGroups(prev => {
      const next = new Set(prev);
      if (next.has(g)) next.delete(g); else next.add(g);
      return next;
    });
  };
  const selectAllGroups = () => { setGroupsTouched(true); resetIncDrill(); setSelectedGroups(new Set(panelGroups)); };
  const clearGroups = () => { setGroupsTouched(true); resetIncDrill(); setSelectedGroups(new Set()); };
  const onlyGroup = (g: string) => { setGroupsTouched(true); resetIncDrill(); setSelectedGroups(new Set([g])); };

  // 智能分析可选分组：面板分组优先，当前分析分组不在列表时置顶保留
  const analysisOptions = useMemo(() => {
    const base = panelGroups.length ? panelGroups : availableGroups;
    return analysisGroup && !base.includes(analysisGroup) ? [analysisGroup, ...base] : base;
  }, [panelGroups, availableGroups, analysisGroup]);

  // 拉取指定分组的 LLM 分析（/api/nms/analysis?group=）
  const fetchAnalysis = useCallback(async (group: string) => {
    if (!group) return;
    setAnalysisLoading(true);
    try {
      const res = await fetch(`/api/nms/analysis?group=${encodeURIComponent(group)}`, { headers });
      if (res.ok) setAnalysisData(await res.json());
    } catch { /* ignore */ } finally {
      setAnalysisLoading(false);
    }
  }, [headers]);

  // 拉取指定分组的 Checklist 预案匹配聚合（确定性，实时读快照，不依赖 LLM）
  const fetchChecklist = useCallback(async (group: string) => {
    setChecklistLoading(true);
    try {
      const res = await fetch(`/api/nms/checklist?groups=${encodeURIComponent(group || '')}`, { headers });
      if (res.ok) setChecklistData(await res.json());
    } catch { /* ignore */ } finally {
      setChecklistLoading(false);
    }
  }, [headers]);

  // 预案清单（仅管理员；后端 require_admin 为真正的安全边界，前端仅隐藏入口）
  const fetchPlans = useCallback(async () => {
    setPlansLoading(true);
    try {
      const res = await fetch('/api/nms/checklist-plans', { headers });
      if (res.ok) {
        setPlansData(await res.json());
      } else {
        setPlansData({ plans: [], total: 0, active_hit_total: 0, orphan_rules: [],
                       error: `无权限或加载失败（HTTP ${res.status}）` });
      }
    } catch (e) {
      setPlansData({ plans: [], total: 0, active_hit_total: 0, orphan_rules: [],
                     error: '加载失败：' + String(e) });
    } finally {
      setPlansLoading(false);
    }
  }, [headers]);

  // 管理员判定（zops_user 由 AppShell 登录时写入；tab 仅管理员渲染）
  useEffect(() => {
    try {
      const raw = localStorage.getItem('zops_user');
      if (raw) {
        const u = JSON.parse(raw);
        if (u?.role === 'admin') {
          setIsAdmin(true);
          fetchPlans();
        }
      }
    } catch { /* ignore */ }
  }, [fetchPlans]);

  // ── 预案编辑/新增/删除（均走后端 require_admin，前端仅交互层）──
  const openAddPlan = () => setPlanForm({
    mode: 'add', title: '', steps_customer: '', steps_platform: '' });

  const openEditPlan = (p: ChecklistPlan) => {
    const bySheet = (s: string) =>
      p.variants.find(v => v.sheet.includes(s))?.steps || '';
    setPlanForm({
      mode: 'edit', title: p.title, origin: p.origin,
      steps_customer: bySheet('单客户'), steps_platform: bySheet('平台'),
    });
  };

  const savePlan = async () => {
    if (!planForm) return;
    setPlanSaving(true);
    setPlanMsg(null);
    try {
      const isAdd = planForm.mode === 'add';
      const url = isAdd
        ? '/api/nms/checklist-plans'
        : `/api/nms/checklist-plans/${encodeURIComponent(planForm.title)}`;
      const res = await fetch(url, {
        method: isAdd ? 'POST' : 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: planForm.title,
          steps_customer: planForm.steps_customer,
          steps_platform: planForm.steps_platform,
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.ok) {
        setPlanForm(null);
        setPlanMsg({ ok: true, text: `${isAdd ? '新增' : '保存'}预案「${planForm.title}」成功，已即时生效` });
        fetchPlans();
      } else {
        setPlanMsg({ ok: false, text: typeof d.detail === 'string' ? d.detail : `保存失败（HTTP ${res.status}）` });
      }
    } catch (e) {
      setPlanMsg({ ok: false, text: '保存失败：' + String(e) });
    } finally {
      setPlanSaving(false);
    }
  };

  const deletePlan = async () => {
    if (!deletingPlan) return;
    setPlanSaving(true);
    setPlanMsg(null);
    try {
      const res = await fetch(
        `/api/nms/checklist-plans/${encodeURIComponent(deletingPlan.title)}`,
        { method: 'DELETE', headers });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.ok) {
        setPlanMsg({ ok: true, text: `已移除预案「${deletingPlan.title}」（不再参与匹配）` });
        setDeletingPlan(null);
        fetchPlans();
      } else {
        setPlanMsg({ ok: false, text: typeof d.detail === 'string' ? d.detail : `移除失败（HTTP ${res.status}）` });
      }
    } catch (e) {
      setPlanMsg({ ok: false, text: '移除失败：' + String(e) });
    } finally {
      setPlanSaving(false);
    }
  };

  // 确认后按需触发指定分组分析，并轮询结果（约 1-2 分钟完成）
  const triggerAnalyze = useCallback(async () => {
    const g = analysisGroup;
    if (!g) return;
    setConfirmAnalyze(false);
    setAnalyzing(true);
    setAnalyzeMsg(null);
    try {
      const res = await fetch('/api/nms/analyze', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ group: g }),
      });
      const d = await res.json();
      setAnalyzeMsg({ ok: !!d.success, text: d.message || (d.success ? '已触发分析' : '触发失败') });
    } catch {
      setAnalyzeMsg({ ok: false, text: '触发分析失败' });
      setAnalyzing(false);
      return;
    }
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const r2 = await fetch(`/api/nms/analysis?group=${encodeURIComponent(g)}`, { headers });
        if (r2.ok) {
          const d2 = await r2.json();
          if (d2.analysis) {
            setAnalysisData(d2);
            setAnalyzing(false);
            setAnalyzeMsg({ ok: true, text: `分组「${g}」分析已完成` });
            clearInterval(timer);
            return;
          }
        }
      } catch { /* ignore */ }
      if (tries >= 24) { clearInterval(timer); setAnalyzing(false); }  // 最多轮询约 2 分钟
    }, 5000);
  }, [analysisGroup, headers]);

  // A4: 分析 outcome 反馈 (采纳 / 已解决 / 未解决 / 误报)
  const submitOutcome = useCallback(async (outcome: string) => {
    if (!analysisGroup || !analysisData?.analysis) return;
    setOutcomeMsg(null);
    try {
      const note = window.prompt(
        `${outcome} - 可选备注 (留空取消):`, '') || '';
      if (note === null) return;  // 用户取消
      const res = await fetch('/api/nms/analysis/outcome', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ group: analysisGroup, outcome, note }),
      });
      const d = await res.json();
      if (res.ok && d.success) {
        setOutcomeMsg({ ok: true, text: `已记录: ${outcome}` });
        fetchAnalysis(analysisGroup);  // 刷新本地分析数据,反映新 outcome
      } else {
        setOutcomeMsg({ ok: false, text: d.error || d.message || '提交失败' });
      }
    } catch (e) {
      setOutcomeMsg({ ok: false, text: String(e) });
    }
  }, [analysisGroup, analysisData, headers, fetchAnalysis]);

  // 切换页签：当前告警=现存快照 / 历史告警=历史库(内容键去重事件) / 智能分析=拉取所选分组分析
  const changeTab = (tab: 'incidents' | 'alarms' | 'history' | 'analysis' | 'checklist') => {
    setActiveTab(tab);
    setNewSnapshot(null);
    // 切换页签立即隐藏上一页的调查内容（不保留旧面板）
    setSslOpen(false);
    setDrillOpen(false);
    if (tab === 'alarms') {
      setAlarmStatus('active');
      setSelectedSeverities(new Set());
      setSearchText('');
      fetchData([...selectedGroups].join(','), 1, false, 'active');
    } else if (tab === 'history') {
      setSelectedSeverities(new Set());
      setSearchText('');
      fetchHistory();
    } else if (tab === 'analysis') {
      const g = [...selectedGroups][0] || analysisGroup || panelGroups[0] || availableGroups[0] || '';
      if (g) { setAnalysisGroup(g); fetchAnalysis(g); fetchChecklist(g); }
      // Phase 2.2: 图表需要事件簇数据 (KPI 卡 + 饼图); 无则拉取, 不阻塞分析展示
      if (!incidentsData?.incidents?.length) fetchIncidents([...selectedGroups].join(','));
    } else if (tab === 'incidents') {
      fetchIncidents([...selectedGroups].join(','));
    } else if (tab === 'checklist') {
      fetchPlans();
    }
  };

  // 严重级别多选：空集合表示显示全部级别，选中一个或多个则只显示所选级别
  const toggleSeverity = (s: string) => {
    setSelectedSeverities(prev => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
  };

  // 按选中分组 → 严重级别 → 搜索 过滤
  // 历史页签: 分组过滤+分页已在服务端完成, 客户端仅做级别/ACK/搜索; 保持接口时间倒序
  const filteredAlarms = useMemo(() => {
    let result = sourceAlarms.filter(a => {
      if (activeTab === 'history') return true;  // 服务端已按分组过滤
      const gs = alarmGroups(a);
      return gs.length === 0 || gs.some(g => selectedGroups.has(g));
    });
    // 现存范围隔离（快照页签才有"现存/已清除"之分）
    if (activeTab !== 'history') {
      result = result.filter(a => ACTIVE_SEV.includes(a.severity));
    }
    if (selectedSeverities.size > 0) result = result.filter(a => selectedSeverities.has(a.severity));
    // ACK 确认状态过滤：未确认 / 已确认
    if (ackFilter === 'acked') result = result.filter(isAcked);
    else if (ackFilter === 'unacked') result = result.filter(a => !isAcked(a));
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      result = result.filter(a =>
        (a.summary || '').toLowerCase().includes(q) ||
        (a.source || '').toLowerCase().includes(q) ||
        alarmGroups(a).join(',').toLowerCase().includes(q) ||
        (a.alarm_type || '').toLowerCase().includes(q));
    }
    if (activeTab === 'history') return result;  // 历史保持接口时间倒序
    const order: Record<string, number> = {};
    SEV_ORDER.forEach((s, i) => { order[s] = i; });
    return [...result].sort((a, b) => (order[a.severity] ?? 99) - (order[b.severity] ?? 99));
  }, [sourceAlarms, selectedGroups, selectedSeverities, searchText, activeTab, ackFilter]);

  // 选中分组范围内的严重级别统计
  // 严重级别统计：快照页签优先服务端全量统计; 历史页签按历史事件本地统计
  const severityCounts = useMemo(() => {
    if (activeTab !== 'history') {
      const sc = data?.latest?.severity_counts;
      if (sc && Object.keys(sc).length > 0) return sc;
    }
    const acc: Record<string, number> = {};
    sourceAlarms.filter(a => { const gs = alarmGroups(a); return gs.length === 0 || gs.some(g => selectedGroups.has(g)); })
      .forEach(a => { const s = a.severity || 'unknown'; acc[s] = (acc[s] || 0) + 1; });
    return acc;
  }, [sourceAlarms, selectedGroups, data?.latest?.severity_counts, activeTab]);

  // 选中分组范围内的 ACK 确认状态统计（未确认 / 已确认）
  const ackCounts = useMemo(() => {
    let acked = 0, unacked = 0;
    sourceAlarms.filter(a => { const gs = alarmGroups(a); return gs.length === 0 || gs.some(g => selectedGroups.has(g)); })
      .forEach(a => { if (isAcked(a)) acked += 1; else unacked += 1; });
    return { acked, unacked };
  }, [sourceAlarms, selectedGroups]);
  const scopeTotal = activeTab === 'history'
    ? sourceAlarms.length
    : Object.values(severityCounts).reduce((a, b) => a + b, 0);
  const criticalCount = (severityCounts.critical || 0) + (severityCounts.major || 0);
  const activeTotal = data?.latest?.active_total ?? alarms.filter(a => ACTIVE_SEV.includes(a.severity)).length;
  const shownGroups = panelGroups.filter(g => !groupSearch.trim() || g.toLowerCase().includes(groupSearch.toLowerCase()));
  const lastUpdate = data?.latest?.timestamp
    ? new Date(data.latest.timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
    : null;

  return (
    <div className="h-full flex flex-col bg-[#F8FAFC]">
      {/* ── Header ── */}
      <PageHeader
        icon={Activity}
        title="NMS 告警中心"
        accent="#0F172A"
        iconBg="rgba(15,23,42,0.08)"
        subtitle={
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <span className={`w-1.5 h-1.5 rounded-full ${data?.latest ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300'}`} />
              {data?.configured ? (data?.latest ? '监控中' : '等待抓取') : '未配置'}
            </span>
            {lastUpdate && <span>更新于 {lastUpdate}</span>}
            {collectInterval > 0 && <span className="text-[#94A3B8]">每 {Math.round(collectInterval / 60)} 分钟采集</span>}
            <span className="text-[#CBD5E1]">IBM Netcool</span>
          </span>
        }
        actions={
          <>
            <button onClick={() => fetchData()} disabled={loading}
              className="p-2 hover:bg-[#F1F5F9] rounded-lg text-[#64748B] transition-colors" title="刷新数据">
              <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
            </button>
            <button onClick={handleRefresh} disabled={refreshing || !data?.configured}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-semibold text-white bg-[#DC2626] hover:bg-[#B91C1C] transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
              <Radio size={13} className={refreshing ? 'animate-pulse' : ''} />
              {refreshing ? '抓取中...' : '立即抓取'}
            </button>
          </>
        }
      />

      {/* Toast */}
      {refreshMsg && (
        <div className={`mx-6 mt-3 px-4 py-2.5 rounded-lg text-xs flex items-center gap-2 shrink-0 ${
          refreshMsg.ok ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
          {refreshMsg.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
          {refreshMsg.text}
        </div>
      )}

      {/* 翻页期间的新数据提示：不打断当前列表，一键回第一页刷新 */}
      {newSnapshot && (activeTab === 'alarms' || activeTab === 'history') && (
        <div className="mx-6 mt-3 px-4 py-2 rounded-lg bg-[#EFF6FF] border border-[#BFDBFE] text-[12px] text-[#1E40AF] flex items-center gap-2 shrink-0">
          <Bell size={13} className="shrink-0 text-[#2563EB]" />
          <span>
            后台已完成新一轮采集（{new Date(newSnapshot.ts).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}）
            {typeof newSnapshot.total === 'number' ? `，快照共 ${newSnapshot.total} 条告警` : ''}
            ，当前停留在第 {activeTab === 'history' ? historyPage : currentPage} 页
          </span>
          <button onClick={backToFirstPage}
            className="ml-auto inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-[#2563EB] hover:bg-[#1D4ED8] text-white text-[11px] font-medium transition-colors">
            <RefreshCw size={11} /> 回到第一页刷新
          </button>
          <button onClick={() => { dismissedTsRef.current = newSnapshot.ts; setNewSnapshot(null); }} title="暂不刷新"
            className="p-0.5 rounded text-[#93C5FD] hover:text-[#2563EB] hover:bg-[#DBEAFE] transition-colors">
            <X size={13} />
          </button>
        </div>
      )}

      {/* ── Tabs ── */}
      <div className="flex items-center border-b border-[#E2E8F0] px-6 bg-white shrink-0">
        {([
          { key: 'incidents' as const, label: '事件视图', count: incidentsData?.incident_count || 0, icon: GitMerge },
          { key: 'alarms' as const, label: '当前告警', count: activeTotal, icon: Bell },
          { key: 'history' as const, label: '历史告警', count: data?.history_count ?? historyData?.count ?? 0, icon: Clock },
          { key: 'analysis' as const, label: '智能分析', count: analysisData?.analysis ? 1 : 0, icon: BarChart3 },
          ...(isAdmin ? [{ key: 'checklist' as const, label: '预案清单', count: plansData?.total || 0, icon: BookOpen }] : []),
        ]).map(tab => {
          const Icon = tab.icon;
          return (
            <button key={tab.key} onClick={() => changeTab(tab.key)}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-[13px] font-medium border-b-2 transition-all -mb-px ${
                activeTab === tab.key ? 'border-[#DC2626] text-[#DC2626]' : 'border-transparent text-[#64748B] hover:text-[#334155]'}`}>
              <Icon size={14} />
              {tab.label}
              {tab.count > 0 && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${
                  activeTab === tab.key ? 'bg-red-100 text-red-600' : 'bg-slate-100 text-slate-500'}`}>{tab.count}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* ── Content ── */}
      <div className="flex-1 overflow-hidden flex">
        {/* 主区：告警详情面板展开时自动回缩让出宽度 */}
        <div className="flex-1 min-w-0 overflow-hidden">

        {/* ══ 事件视图（关联降噪：把海量告警归并为事件簇）══ */}
        {activeTab === 'incidents' && (
          <div className="h-full overflow-y-auto p-5">
            <div className="flex items-center gap-3 mb-4 flex-wrap">
              <div className="flex items-center gap-2">
                <GitMerge size={15} className="text-[#0F172A]" />
                <span className="text-[13px] font-semibold text-[#0F172A]">关联降噪</span>
              </div>
              {incidentsData && !incidentsData.pending && (
                <span className="text-[12px] text-[#64748B]">
                  <strong className="text-[#0F172A]">{incidentsData.incident_count}</strong> 个事件（约 {shownAlarmCount} 条告警）
                </span>
              )}
              <button onClick={openSslRecords}
                className="ml-auto inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] transition-colors"
                title="我的调查与案例审核（告警消失后仍可查看）">
                <BookOpen size={13} /> 调查记录
              </button>
              <button onClick={() => fetchIncidents([...selectedGroups].join(','))} disabled={incidentsLoading}
                className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] disabled:opacity-50 transition-colors">
                <RefreshCw size={13} className={incidentsLoading ? 'animate-spin' : ''} /> 刷新
              </button>
            </div>

            {/* 分组筛选：事件视图按分组过滤（与「当前告警」共用 selectedGroups，切换即重新拉取） */}
            <div className="flex items-center gap-1.5 mb-4 flex-wrap">
              <Layers size={13} className="text-[#94A3B8] shrink-0" />
              <span className="text-[11px] text-[#94A3B8] shrink-0 mr-0.5">分组</span>
              {panelGroups.map(g => {
                const checked = selectedGroups.has(g);
                return (
                  <button key={g} onClick={() => toggleGroup(g)} title={g}
                    className={`inline-flex items-center max-w-[180px] truncate px-2 py-0.5 rounded-full text-[11px] border transition-colors ${
                      checked ? 'bg-red-50 border-red-200 text-[#DC2626]' : 'bg-white border-[#E2E8F0] text-[#64748B] hover:border-[#CBD5E1]'}`}>
                    {g}
                  </button>
                );
              })}
              {defaultGroups.length > 0 && (
                <button onClick={() => setShowAllGroups(v => !v)}
                  className="text-[11px] text-[#64748B] hover:text-[#DC2626] transition-colors">
                  {showAllGroups ? '仅默认分组' : `全部分组 (${availableGroups.length})`}
                </button>
              )}
              {selectedGroups.size > 0 && (
                <button onClick={clearGroups} className="text-[11px] text-[#94A3B8] hover:text-[#DC2626] transition-colors">清空</button>
              )}
            </div>

            {incidentsLoading && !incidentsData ? (
              <div className="text-center py-16">
                <RefreshCw size={22} className="animate-spin text-[#CBD5E1] mx-auto" />
                <p className="text-[#94A3B8] text-sm mt-3">加载事件中…</p>
              </div>
            ) : incidents.length > 0 ? (
              <div className="space-y-3">
                {/* 刷新失败但保留旧数据：顶部提示，不遮挡当前结果 */}
                {incidentsError && (
                  <div className="border border-[#FCA5A5] bg-[#FEF2F2] rounded-lg px-3 py-2 text-[11px] text-[#B91C1C] flex items-center gap-2">
                    <XCircle size={13} className="shrink-0" />
                    <span className="min-w-0">事件刷新失败：{incidentsError}（当前显示上次成功结果）</span>
                    <button onClick={() => fetchIncidents()}
                      className="ml-auto shrink-0 underline underline-offset-2 hover:text-[#7F1D1D]">重试</button>
                  </div>
                )}
                {/* 概览指标 */}
                <IncidentKpiStrip incidents={incidents} />

                {/* 站点 / 服务关联分析（旧数据无 scope_analysis 时降级为原有布局） */}
                {scopeAnalysis ? (
                  <>
                    <IncidentScopeJudgement analysis={scopeAnalysis} incidents={incidents}
                      scopeLabel={incidentsData?.scope_analysis_scope} />
                    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                      <div className="lg:col-span-2 min-w-0">
                        <IncidentDimensionMatrix analysis={scopeAnalysis}
                          active={incSeg && incType ? { cidr: incSeg, type: incType } : null}
                          onSelect={drillCell} />
                      </div>
                      <IncidentSeverityDonut incidents={incidents} active={incSev} onToggle={toggleIncSev} />
                    </div>
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                      <IncidentSegmentBars analysis={scopeAnalysis} active={incSeg} onSelect={drillSeg} />
                      <IncidentTypeBars analysis={scopeAnalysis} active={incType} onSelect={drillType} />
                    </div>
                    <IncidentTimeline incidents={incidents} maxRows={8} />
                  </>
                ) : (
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                    <IncidentSeverityDonut incidents={incidents} active={incSev} onToggle={toggleIncSev} />
                    <div className="lg:col-span-2 min-w-0">
                      <IncidentTimeline incidents={incidents} maxRows={8} />
                    </div>
                  </div>
                )}

                {/* 列表筛选：级别 / 类别 / 范围 / 维度筛选 / 搜索 / 排序 */}
                <div className="space-y-1.5 pt-0.5">
                  {(incSeg || incType || incFocus) && (
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">筛选条件</span>
                      {incSeg && (
                        <span className="inline-flex items-center gap-1 text-[11px] pl-1.5 pr-1 py-0.5 rounded-md border border-[#FDBA74] bg-[#FFF7ED] text-[#C2410C]">
                          <Network size={10} />
                          网段 <span className="font-mono">{incSeg === '其他' ? '其他网段' : incSeg}</span>
                          <button onClick={() => { setIncSeg(null); setIncSegEx([]); setIncLimit(30); if (!incType) setDrillOpen(false); }}
                            className="ml-0.5 p-0.5 rounded hover:bg-[#FFEDD5] transition-colors" title="移除网段条件">
                            <X size={10} />
                          </button>
                        </span>
                      )}
                      {incType && (
                        <span className="inline-flex items-center gap-1 text-[11px] pl-1.5 pr-1 py-0.5 rounded-md border border-[#C4B5FD] bg-[#F5F3FF] text-[#6D28D9]">
                          <Tag size={10} />
                          类型 <span className="max-w-[220px] truncate">{incType === '其他' ? '其他类型' : incType}</span>
                          <button onClick={() => { setIncType(null); setIncTypeEx([]); setIncLimit(30); if (!incSeg) setDrillOpen(false); }}
                            className="ml-0.5 p-0.5 rounded hover:bg-[#EDE9FE] transition-colors" title="移除类型条件">
                            <X size={10} />
                          </button>
                        </span>
                      )}
                      {incFocus && (
                        <span className="inline-flex items-center gap-1 text-[11px] pl-1.5 pr-1 py-0.5 rounded-md border border-[#CBD5E1] bg-[#F1F5F9] text-[#475569]">
                          <GitMerge size={10} />
                          已定位 1 个事件
                          <button onClick={() => { setIncFocus(null); setExpandedIncident(null); }}
                            className="ml-0.5 p-0.5 rounded hover:bg-[#E2E8F0] transition-colors" title="取消定位">
                            <X size={10} />
                          </button>
                        </span>
                      )}
                      <span className="text-[10px] text-[#94A3B8] tabular-nums">命中 {filteredIncidents.length} 个事件</span>
                      <button onClick={clearDrill}
                        className="text-[10px] text-[#94A3B8] hover:text-[#DC2626] transition-colors">清除筛选</button>
                    </div>
                  )}
                  <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">级别</span>
                  <button onClick={() => { setIncSev(new Set()); setIncLimit(30); }}
                    className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border ${
                      incSev.size === 0 ? 'bg-[#F1F5F9] text-[#0F172A] border-[#94A3B8]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                    全部 <span className="opacity-60">{incidents.length}</span>
                  </button>
                  {SEV_ORDER.filter(s => s !== 'cleared' && (incidentSeverityCounts[s] || 0) > 0).map(s => {
                    const meta = sev(s);
                    const active = incSev.has(s);
                    return (
                      <button key={s} onClick={() => toggleIncSev(s)}
                        className="text-xs px-2.5 py-1 rounded-md font-medium transition-all border inline-flex items-center gap-1.5"
                        style={active
                          ? { background: meta.bg, color: meta.color, borderColor: meta.dot }
                          : { background: 'white', color: '#475569', borderColor: '#E2E8F0' }}>
                        {active
                          ? <CheckSquare size={12} style={{ color: meta.color }} />
                          : <span className="w-1.5 h-1.5 rounded-full" style={{ background: meta.dot }} />}
                        {meta.label} <span className="opacity-60">{incidentSeverityCounts[s] || 0}</span>
                      </button>
                    );
                  })}

                  <span className="w-px h-4 bg-[#E2E8F0] mx-1 self-center" />

                  {([
                    { key: 'all' as const, label: '全部类别' },
                    { key: 'systemic' as const, label: '系统级' },
                    { key: 'object' as const, label: '对象级' },
                  ]).map(c => (
                    <button key={c.key} onClick={() => { setIncCategory(c.key); setIncLimit(30); }}
                      className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border ${
                        incCategory === c.key ? 'bg-[#F1F5F9] text-[#0F172A] border-[#94A3B8]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                      {c.label}
                    </button>
                  ))}

                  <span className="w-px h-4 bg-[#E2E8F0] mx-1 self-center" />

                  <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">范围</span>
                  {([
                    { key: 'all' as const, label: '全部' },
                    { key: 'cross' as const, label: '跨站点' },
                    { key: 'site' as const, label: '同站点' },
                    { key: 'object' as const, label: '单对象' },
                  ]).map(s => (
                    <button key={s.key} onClick={() => { setIncScope(s.key); setIncLimit(30); }}
                      className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border ${
                        incScope === s.key ? 'bg-[#F1F5F9] text-[#0F172A] border-[#94A3B8]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                      {s.label}
                      {s.key !== 'all' && (
                        <span className="opacity-60"> {incidentScopeCounts[s.key]}</span>
                      )}
                    </button>
                  ))}

                  <div className="relative ml-auto">
                    <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#94A3B8]" />
                    <input type="text" value={incSearch}
                      onChange={e => { setIncSearch(e.target.value); setIncLimit(30); }}
                      placeholder="搜索事件 / 来源 / 分组…"
                      className="text-xs pl-8 pr-3 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] w-52 focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300" />
                  </div>
                  <select value={incSort}
                    onChange={e => { setIncSort(e.target.value as 'priority' | 'recent' | 'members'); setIncLimit(30); }}
                    className="text-xs px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#475569] focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300">
                    <option value="priority">按优先级</option>
                    <option value="recent">按最近发生</option>
                    <option value="members">按成员数</option>
                  </select>
                  <span className="text-[11px] text-[#94A3B8] tabular-nums">
                    {filteredIncidents.length === incidents.length
                      ? `${incidents.length} 个事件`
                      : `${filteredIncidents.length} / ${incidents.length} 个事件`}
                  </span>
                  </div>
                </div>

                {/* 事件卡片列表 */}
                <div className="space-y-2">
                  {filteredIncidents.slice(0, incLimit).map(inc => (
                    <div key={inc.incident_id} id={`incident-${inc.incident_id}`}>
                      <IncidentCard inc={inc}
                        expanded={expandedIncident === inc.incident_id}
                        onToggle={() => setExpandedIncident(expandedIncident === inc.incident_id ? null : inc.incident_id)}
                        alarmMap={alarmMap} groupRank={groupRank}
                        onInvestigate={openSslForAlarm} />
                    </div>
                  ))}
                  {filteredIncidents.length === 0 && (
                    <div className="border border-[#E2E8F0] rounded-xl bg-white text-center py-12">
                      <p className="text-[13px] text-[#475569]">没有符合筛选条件的事件</p>
                      <button onClick={() => { setIncSev(new Set()); setIncCategory('all'); setIncScope('all'); setIncSeg(null); setIncSegEx([]); setIncType(null); setIncTypeEx([]); setIncFocus(null); setIncSearch(''); setIncLimit(30); }}
                        className="mt-2 text-[12px] text-[#DC2626] hover:underline">清除筛选</button>
                    </div>
                  )}
                  {filteredIncidents.length > incLimit && (
                    <div className="text-center pt-1">
                      <button onClick={() => setIncLimit(v => v + 30)}
                        className="px-4 py-2 rounded-lg border border-[#E2E8F0] bg-white text-[12px] text-[#475569] hover:border-[#CBD5E1] transition-colors">
                        加载更多事件（剩余 {filteredIncidents.length - incLimit}）
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ) : incidentsError ? (
              <div className="border border-[#E2E8F0] rounded-xl bg-white text-center py-16">
                <div className="w-14 h-14 rounded-2xl flex items-center justify-center mx-auto mb-3 bg-[#FEF2F2]">
                  <XCircle size={26} className="text-[#FCA5A5]" />
                </div>
                <p className="text-[#475569] font-medium text-sm">事件数据加载失败</p>
                <p className="text-xs mt-1 text-[#94A3B8]">{incidentsError}</p>
                <button onClick={() => fetchIncidents()}
                  className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[12px] text-[#475569] hover:border-[#CBD5E1] transition-colors">
                  <RefreshCw size={12} /> 重试
                </button>
              </div>
            ) : (
              <div className="border border-[#E2E8F0] rounded-xl bg-white text-center py-16">
                <div className="w-14 h-14 rounded-2xl flex items-center justify-center mx-auto mb-3 bg-[#F1F5F9]">
                  <GitMerge size={26} className="text-[#CBD5E1]" />
                </div>
                <p className="text-[#475569] font-medium text-sm">
                  {incidentsData?.configured
                    ? (incidentsData?.pending ? '监控任务尚未完成首次抓取' : '当前范围暂无事件')
                    : (incidentsData?.message || 'NMS 凭据未配置')}
                </p>
                <p className="text-xs mt-1 text-[#94A3B8]">
                  {incidentsData?.message
                    || (incidentsData?.configured
                      ? '事件由采集时基于当前告警快照自动归并，稍后重试'
                      : '请在系统设置中配置 NMS 采集账号')}
                </p>
              </div>
            )}
          </div>
        )}

        {/* ══ 当前告警(现存) / 历史告警(已清除)：左分组面板 + 右告警表 ══ */}
        {(activeTab === 'alarms' || activeTab === 'history') && (
          <div className="h-full flex">
            {/* 左：分组勾选面板（展开时显示；收起时右侧顶部显示横向分组条） */}
            {groupPanelOpen && (
            <aside className="w-56 shrink-0 border-r border-[#E2E8F0] bg-white flex flex-col">
              <div className="px-3 py-2.5 border-b border-[#F1F5F9] flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-[12px] font-semibold text-[#334155] min-w-0" title={[...selectedGroups].join(', ')}>
                  <Layers size={13} className="text-[#94A3B8] shrink-0" /> 分组
                  <span className="text-[11px] font-normal text-[#64748B] truncate">
                    {selectedGroups.size ? [...selectedGroups].join(', ') : '未选择'}
                  </span>
                </span>
                <div className="flex items-center gap-2">
                  <button onClick={allSelected ? clearGroups : selectAllGroups}
                    className="text-[11px] text-[#64748B] hover:text-[#DC2626] transition-colors">
                    {allSelected ? '清空' : '全选'}
                  </button>
                  <button onClick={() => setGroupPanelOpen(false)}
                    className="p-0.5 rounded hover:bg-[#F1F5F9] text-[#94A3B8] hover:text-[#64748B] transition-colors" title="收起">
                    <ChevronRight size={14} className="rotate-180" />
                  </button>
                </div>
              </div>
              <div className="px-3 py-2 border-b border-[#F1F5F9]">
                <div className="relative">
                  <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#94A3B8]" />
                  <input type="text" value={groupSearch} onChange={e => setGroupSearch(e.target.value)}
                    placeholder="搜索分组…"
                    className="w-full text-[12px] pl-7 pr-6 py-1.5 rounded-md border border-[#E2E8F0] bg-[#F8FAFC] text-[#334155] focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300" />
                  {groupSearch && (
                    <button onClick={() => setGroupSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-[#CBD5E1] hover:text-[#64748B]">
                      <X size={12} />
                    </button>
                  )}
                </div>
              </div>
              {defaultGroups.length > 0 && (
                <button onClick={() => setShowAllGroups(v => !v)}
                  className="w-full text-left px-3 py-1.5 text-[11px] text-[#64748B] hover:text-[#DC2626] border-b border-[#F1F5F9] transition-colors">
                  {showAllGroups ? '← 仅看我的默认分组' : `显示全部分组 (${availableGroups.length}) →`}
                </button>
              )}
              <div className="flex-1 overflow-y-auto py-1">
                {shownGroups.map(g => {
                  const st = groupStats[g] || { count: 0, crit: 0 };
                  const checked = selectedGroups.has(g);
                  return (
                    <div key={g}
                      className={`group flex items-center gap-2 px-3 py-1.5 cursor-pointer transition-colors ${checked ? 'bg-red-50/40' : 'hover:bg-[#F8FAFC]'}`}
                      onClick={() => toggleGroup(g)}>
                      {checked
                        ? <CheckSquare size={15} className="text-[#DC2626] shrink-0" />
                        : <Square size={15} className="text-[#CBD5E1] shrink-0" />}
                      {st.crit > 0 && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: sev('critical').dot }} />}
                      <span className={`flex-1 text-[12px] truncate ${checked ? 'text-[#0F172A] font-medium' : 'text-[#475569]'}`} title={g}>{g}</span>
                      <button onClick={e => { e.stopPropagation(); onlyGroup(g); }}
                        className="hidden group-hover:inline text-[10px] text-[#94A3B8] hover:text-[#DC2626] shrink-0" title="仅看此分组">仅此</button>
                      <span className="text-[10px] text-[#94A3B8] font-mono shrink-0">{st.count}</span>
                    </div>
                  );
                })}
                {shownGroups.length === 0 && (
                  <div className="px-3 py-6 text-center text-[11px] text-[#94A3B8]">无匹配分组</div>
                )}
              </div>
            </aside>
            )}

            {/* 右：分组条（收起时） + 统计 + 过滤 + 表格 */}
            <div className="flex-1 flex flex-col min-w-0">
              {!groupPanelOpen && (
                <div className="shrink-0 border-b border-[#E2E8F0] bg-white px-4 py-2 flex items-center gap-2 flex-wrap">
                  <button onClick={() => setGroupPanelOpen(true)}
                    className="flex items-center gap-1.5 text-[12px] font-medium text-[#64748B] hover:text-[#DC2626] transition-colors" title="展开分组筛选">
                    <Layers size={14} /> 分组筛选
                  </button>
                  <span className="text-[#E2E8F0] select-none">|</span>
                  {selectedGroups.size === 0 ? (
                    <span className="text-[12px] text-[#94A3B8]">未选择分组</span>
                  ) : (
                    <>
                      {[...selectedGroups].slice(0, 5).map(g => (
                        <span key={g} className="inline-flex items-center gap-0.5 pl-2 pr-0.5 py-0.5 rounded-full bg-red-50 border border-red-200 text-[12px] text-[#DC2626]">
                          <button onClick={() => setGroupPanelOpen(true)} title={g} className="hover:underline max-w-[150px] truncate">{g}</button>
                          <button onClick={() => toggleGroup(g)} title={`移除 ${g}`} className="p-0.5 rounded-full hover:bg-red-100 text-[#F87171] hover:text-[#DC2626] transition-colors"><X size={11} /></button>
                        </span>
                      ))}
                      {selectedGroups.size > 5 && (
                        <button onClick={() => setGroupPanelOpen(true)}
                          className="text-[12px] text-[#64748B] hover:text-[#DC2626] px-1.5 py-0.5 rounded-full border border-dashed border-[#CBD5E1] hover:border-[#DC2626] transition-colors">+{selectedGroups.size - 5} 个</button>
                      )}
                    </>
                  )}
                  <button onClick={() => setGroupPanelOpen(true)}
                    className="ml-auto flex items-center gap-0.5 text-[11px] text-[#94A3B8] hover:text-[#DC2626] transition-colors">
                    展开 <ChevronRight size={12} />
                  </button>
                </div>
              )}
              <div className="flex-1 overflow-y-auto">
              <div className="p-5">
                {/* 严重级别汇总条 */}
                {scopeTotal > 0 && (
                  <div className="mb-4">
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">级别</span>
                      <button onClick={() => setSelectedSeverities(new Set())}
                        className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border ${
                          selectedSeverities.size === 0 ? 'bg-[#F1F5F9] text-[#0F172A] border-[#94A3B8]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                        全部 <span className="opacity-60">{scopeTotal}</span>
                      </button>
                      {SEV_ORDER.filter(s => s !== 'cleared' && (severityCounts[s] || 0) > 0).map(s => {
                        const meta = sev(s); const active = selectedSeverities.has(s);
                        return (
                          <button key={s} onClick={() => toggleSeverity(s)}
                            className="text-xs px-2.5 py-1 rounded-md font-medium transition-all border inline-flex items-center gap-1.5"
                            style={active ? { background: meta.bg, color: meta.color, borderColor: meta.dot } : { background: 'white', color: '#475569', borderColor: '#E2E8F0' }}>
                            {active
                              ? <CheckSquare size={12} style={{ color: meta.color }} />
                              : <span className="w-1.5 h-1.5 rounded-full" style={{ background: meta.dot }} />}
                            {meta.label} <span className="opacity-60">{severityCounts[s] || 0}</span>
                          </button>
                        );
                      })}
                      <div className="relative ml-auto">
                        <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#94A3B8]" />
                        <input type="text" value={searchText} onChange={e => setSearchText(e.target.value)}
                          placeholder="搜索告警…"
                          className="text-xs pl-8 pr-3 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] w-48 focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300" />
                      </div>
                    </div>
                    {/* Phase 2.1: 时间范围筛选 (从/到, ISO 本地时间; 空 = 不限) */}
                    <div className="flex items-center gap-2 mt-2 flex-wrap">
                      <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">时间范围</span>
                      <input type="datetime-local" value={timeFrom} onChange={e => setTimeFrom(e.target.value ? `${e.target.value}:00+08:00` : '')}
                        className="text-xs px-2 py-1 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300" />
                      <span className="text-[#94A3B8] text-xs">→</span>
                      <input type="datetime-local" value={timeTo.replace(/\+08:00$/, '')} onChange={e => setTimeTo(e.target.value ? `${e.target.value}:00+08:00` : '')}
                        className="text-xs px-2 py-1 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300" />
                      {(timeFrom || timeTo) && (
                        <button onClick={() => { setTimeFrom(''); setTimeTo(''); }}
                          className="text-[11px] px-2 py-1 rounded-md text-[#475569] hover:bg-[#F1F5F9] inline-flex items-center gap-1">
                          <X size={10} /> 清除
                        </button>
                      )}
                      <button onClick={() => activeTab === 'history'
                        ? fetchHistory(1)
                        : fetchData([...selectedGroups].join(','), 1)}
                        className="text-[11px] px-2.5 py-1 rounded-md bg-[#0F172A] text-white hover:bg-[#1E293B]">
                        应用
                      </button>
                      {/* 快捷预设 */}
                      <div className="ml-auto flex items-center gap-1">
                        {[
                          { label: '1h', ms: 3600_000 },
                          { label: '6h', ms: 6 * 3600_000 },
                          { label: '24h', ms: 24 * 3600_000 },
                          { label: '7d', ms: 7 * 24 * 3600_000 },
                        ].map(p => (
                          <button key={p.label} onClick={() => {
                            const now = new Date();
                            const from = new Date(now.getTime() - p.ms);
                            setTimeFrom(from.toISOString().slice(0, 19) + '+08:00');
                            setTimeTo(now.toISOString().slice(0, 19) + '+08:00');
                          }}
                            className="text-[10px] px-2 py-0.5 rounded border border-[#E2E8F0] text-[#475569] hover:border-[#94A3B8]">
                            {p.label}
                          </button>
                        ))}
                      </div>
                    </div>
                    <SeverityBar counts={severityCounts} />
                    {/* 维度二：确认状态（ACK，独立于严重级别） */}
                    <div className="flex items-center gap-2 flex-wrap mt-2.5">
                      <span className="text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">确认状态</span>
                      <button onClick={() => setAckFilter('')}
                        className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border ${
                          ackFilter === '' ? 'bg-[#F1F5F9] text-[#0F172A] border-[#94A3B8]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                        全部 <span className="opacity-60">{ackCounts.acked + ackCounts.unacked}</span>
                      </button>
                      <button onClick={() => setAckFilter(ackFilter === 'unacked' ? '' : 'unacked')}
                        className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border inline-flex items-center gap-1.5 ${
                          ackFilter === 'unacked' ? 'bg-[#FFF7ED] text-[#EA580C] border-[#FED7AA]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                        未确认 <span className="opacity-60">{ackCounts.unacked}</span>
                      </button>
                      <button onClick={() => setAckFilter(ackFilter === 'acked' ? '' : 'acked')}
                        className={`text-xs px-2.5 py-1 rounded-md font-medium transition-all border inline-flex items-center gap-1.5 ${
                          ackFilter === 'acked' ? 'bg-[#F0FDF4] text-[#16A34A] border-[#BBF7D0]' : 'bg-white text-[#475569] border-[#E2E8F0] hover:border-[#CBD5E1]'}`}>
                        <CheckCircle size={12} />
                        已确认 <span className="opacity-60">{ackCounts.acked}</span>
                      </button>
                    </div>
                  </div>
                )}

                {/* 表格 */}
                <div className="border border-[#E2E8F0] rounded-xl overflow-hidden bg-white">
                  <table className="w-full table-fixed">
                    <thead>
                      <tr className="bg-[#F8FAFC] border-b border-[#E2E8F0]">
                        <th className="w-1" />
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-8">#</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[70px]">级别</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[64px]">确认</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[64px]">证书</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider">告警摘要</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[112px]">分组</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[120px]">来源</th>
                        <th className="text-left px-2 py-1.5 text-[10px] font-semibold text-[#94A3B8] uppercase tracking-wider w-[150px]">时间（首/末）</th>
                        <th className="w-6" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#F1F5F9]">
                      {filteredAlarms.map((alarm, idx) => (
                        <AlarmRow key={`${alarm.alarm_id}-${idx}`} alarm={alarm} idx={idx} groupRank={groupRank}
                          onInvestigate={activeTab === 'alarms' ? openSslForAlarm : undefined} />
                      ))}
                    </tbody>
                  </table>

                  {(activeTab === 'history'
                    ? (filteredAlarms.length === 0 && !historyLoading)
                    : (scopeTotal === 0 && !loading)) && (
                    <div className="text-center py-16">
                      <div className="w-14 h-14 rounded-2xl flex items-center justify-center mx-auto mb-3 bg-[#F1F5F9]">
                        <Activity size={26} className="text-[#CBD5E1]" />
                      </div>
                      <p className="text-[#475569] font-medium text-sm">
                        {activeTab === 'history'
                          ? (historyData ? '历史库暂无事件' : '暂无历史数据')
                          : (data?.configured
                            ? (selectedGroups.size === 0 ? '未选择任何分组' : '暂无现存告警')
                            : 'NMS 凭据未配置')}
                      </p>
                      <p className="text-xs mt-1 text-[#94A3B8]">
                        {activeTab === 'history'
                          ? ((timeFrom || timeTo)
                            ? '所选时间窗口内无历史事件（历史保留 30 天，更早的已按保留期老化）'
                            : '历史事件由抓取轮次持续写入（内容键去重，保留 30 天）；若刚部署请等待一轮抓取')
                          : (data?.configured
                            ? (selectedGroups.size === 0 ? '请在左侧勾选要查看的分组' : (data?.message || '将用您的 AD 账号登录 NMS 抓取，仅显示您权限范围内的告警'))
                            : '请在 zops.env 中配置 NMS_USERNAME 和 NMS_PASSWORD')}
                      </p>
                      {data?.configured && selectedGroups.size !== 0 && activeTab === 'alarms' && (
                        <button onClick={handleRefresh} disabled={refreshing}
                          className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-[#DC2626] text-white text-sm font-medium hover:bg-[#B91C1C] disabled:opacity-60 transition-colors">
                          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
                          {refreshing ? '正在用您的 AD 身份登录 NMS 抓取…' : '用我的 AD 账号抓取'}
                        </button>
                      )}
                    </div>
                  )}
                  {((activeTab === 'history' ? historyLoading : loading) &&
                    (activeTab === 'history' ? !historyData : !data)) && (
                    <div className="text-center py-16">
                      <RefreshCw size={22} className="animate-spin text-[#CBD5E1] mx-auto" />
                      <p className="text-[#94A3B8] text-sm mt-3">加载中...</p>
                    </div>
                  )}
                </div>

                {filteredAlarms.length > 0 && (
                  <div className="flex items-center justify-between mt-3 px-1 text-[11px] text-[#94A3B8]">
                    <span>{activeTab === 'history'
                      ? `历史事件 ${filteredAlarms.length} / ${scopeTotal} 条（去重后，同事件换手已合并）`
                      : `现存 ${filteredAlarms.length} / ${filteredTotal || scopeTotal} 条（${selectedGroups.size} 个分组）`}</span>
                    <div className="flex items-center gap-3">
                      {activeTab === 'alarms' && <span>严重/重要 {criticalCount} 条需关注</span>}
                      {(activeTab === 'history' ? historyHasMore : hasMore) && (
                        <button onClick={activeTab === 'history' ? loadMoreHistory : loadMore} disabled={loadingMore}
                          className="px-2.5 py-1 rounded-md border border-[#E2E8F0] bg-white text-[#475569] hover:border-[#CBD5E1] transition-colors disabled:opacity-50">
                          {loadingMore ? '加载中…' : '加载更多'}
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
              </div>
            </div>
          </div>
        )}

        {/* ══ 智能分析 ══ */}
        {activeTab === 'analysis' && (
          <div className="h-full overflow-y-auto p-5">
            {/* Phase 2.2: KPI 卡 + 图表 (数据来自已加载快照, 不发新请求) */}
            <div className="mb-4 space-y-3">
              <NmsKpiCards alarms={data?.latest?.alarms || []} incidents={incidentsData?.incidents || []} />
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                <div className="lg:col-span-2"><SeverityTrend alarms={data?.latest?.alarms || []} /></div>
                <TopSourcesBar alarms={data?.latest?.alarms || []} />
              </div>
            </div>

            {/* 分组选择 + 触发分析 */}
            <div className="flex items-center gap-3 mb-4 flex-wrap">
              <div className="flex items-center gap-2">
                <Layers size={15} className="text-[#94A3B8]" />
                <span className="text-[13px] text-[#475569]">分析分组</span>
                <select value={analysisGroup}
                  onChange={e => { setAnalysisGroup(e.target.value); fetchAnalysis(e.target.value); fetchChecklist(e.target.value); }}
                  className="text-[13px] px-2.5 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#0F172A] focus:outline-none focus:ring-2 focus:ring-red-500/15 focus:border-red-300 max-w-[300px]">
                  {analysisOptions.map(g => <option key={g} value={g}>{g}</option>)}
                </select>
              </div>
              <button onClick={() => setConfirmAnalyze(true)} disabled={analyzing || !analysisGroup}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-[#7C3AED] text-white text-[13px] font-medium hover:bg-[#6D28D9] disabled:opacity-50 transition-colors">
                <BarChart3 size={14} className={analyzing ? 'animate-pulse' : ''} />
                {analyzing ? '分析中…' : '生成分析'}
              </button>
              <button onClick={() => { fetchAnalysis(analysisGroup); fetchChecklist(analysisGroup); }} disabled={analysisLoading || !analysisGroup}
                className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] disabled:opacity-50 transition-colors">
                <RefreshCw size={13} className={analysisLoading ? 'animate-spin' : ''} /> 刷新
              </button>
              {analyzeMsg && (
                <span className={`text-[12px] ${analyzeMsg.ok ? 'text-[#16A34A]' : 'text-[#DC2626]'}`}>{analyzeMsg.text}</span>
              )}
            </div>

            {/* ── Checklist 预案匹配（以告警为核心的确定性匹配，与快照实时同步）── */}
            <div className="mb-5">
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <CheckSquare size={15} className="text-amber-600" />
                <span className="font-semibold text-[#0F172A] text-sm">Checklist 预案匹配</span>
                {checklistData && !checklistData.pending && (
                  <span className="text-[11px] text-[#64748B]">
                    活跃 <strong className="text-[#0F172A]">{checklistData.active_count}</strong> 条，命中
                    <strong className="text-amber-700"> {checklistData.matched_count}</strong> 条 ·
                    <strong className="text-amber-700"> {checklistData.matches.length}</strong> 个预案，未匹配 {checklistData.unmatched_count} 条
                  </span>
                )}
                {checklistLoading && <RefreshCw size={12} className="animate-spin text-[#CBD5E1]" />}
              </div>
              {checklistData && checklistData.matches.length > 0 ? (
                <div className="space-y-2">
                  {/* 按预案群分组：群标题 + 关联分析描述 + 组内预案卡片 */}
                  {checklistData.groups && checklistData.groups.length > 0
                    ? checklistData.groups.map(g => (
                        <div key={g.key} className="rounded-xl border border-[#F1F5F9] bg-[#FBFCFE] p-3 space-y-2">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[12px] font-semibold text-[#0F172A]">{g.name}</span>
                            <span className="text-[10px] px-1.5 py-px rounded bg-amber-50 border border-amber-200 text-amber-700">{g.count} 条告警</span>
                          </div>
                          {g.desc && (
                            <p className="text-[11px] text-[#64748B] leading-relaxed">{g.desc}</p>
                          )}
                          <div className="space-y-2">
                            {g.matches.map(m => <ChecklistCard key={m.title} m={m} />)}
                          </div>
                        </div>
                      ))
                    : checklistData.matches.map(m => <ChecklistCard key={m.title} m={m} />)}
                </div>
              ) : (
                <div className="border border-dashed border-[#E2E8F0] rounded-xl bg-white px-4 py-5 text-center text-[12px] text-[#94A3B8]">
                  {checklistLoading ? '匹配中…' : '当前分组活跃告警未命中任何 Checklist 预案'}
                </div>
              )}
              {/* 未匹配告警分类摘要：提示规则盲区/人工处置面 */}
              {checklistData && checklistData.unmatched.length > 0 && (
                <div className="mt-2 flex items-center gap-1.5 flex-wrap text-[11px]">
                  <span className="text-[#94A3B8]">未匹配告警分类：</span>
                  {checklistData.unmatched.map(u => (
                    <span key={u.category} className="px-1.5 py-px rounded bg-[#F1F5F9] text-[#64748B]">
                      {u.category} ×{u.count}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {analysisLoading && !analysisData ? (
              <div className="text-center py-16">
                <RefreshCw size={22} className="animate-spin text-[#CBD5E1] mx-auto" />
                <p className="text-[#94A3B8] text-sm mt-3">加载分析中…</p>
              </div>
            ) : analysisData?.analysis ? (
              <div className="border border-[#E2E8F0] rounded-xl overflow-hidden bg-white">
                <div className="px-5 py-3.5 border-b border-[#F1F5F9] bg-gradient-to-r from-[#F5F3FF] to-[#F8FAFC]">
                  <div className="flex items-center gap-2 mb-3">
                    <div className="w-6 h-6 rounded-lg bg-violet-100 flex items-center justify-center">
                      <BarChart3 size={14} className="text-[#7C3AED]" />
                    </div>
                    <span className="font-semibold text-[#0F172A] text-sm">智能分析</span>
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-violet-50 border border-violet-200 text-[#7C3AED]">{analysisData.group}</span>
                    {analysisData.analysis?.severity_counts && (
                      <span className="text-[11px] text-[#64748B]">
                        {['critical', 'major', 'minor', 'warning'].map(s => analysisData.analysis?.severity_counts?.[s] ? (
                          <span key={s} className="mr-1.5">
                            <span className={`inline-block w-1.5 h-1.5 rounded-full mr-0.5 ${s === 'critical' ? 'bg-red-500' : s === 'major' ? 'bg-orange-500' : s === 'minor' ? 'bg-amber-400' : 'bg-sky-400'}`} />
                            {analysisData.analysis?.severity_counts?.[s]}
                          </span>
                        ) : null)}
                      </span>
                    )}
                  </div>
                  <div className="grid grid-cols-3 max-w-[420px] gap-2">
                    <div className="rounded-lg bg-white border border-violet-100 px-3 py-2">
                      <div className="text-[10px] text-[#94A3B8]">本次新增告警</div>
                      <div className="text-lg font-bold text-[#7C3AED] leading-tight">{analysisData.analysis.new_count}</div>
                    </div>
                    <div className="rounded-lg bg-white border border-[#E2E8F0] px-3 py-2">
                      <div className="text-[10px] text-[#94A3B8]">累计告警</div>
                      <div className="text-lg font-bold text-[#0F172A] leading-tight">{analysisData.analysis.total_count}</div>
                    </div>
                    <div className="rounded-lg bg-white border border-[#E2E8F0] px-3 py-2">
                      <div className="text-[10px] text-[#94A3B8]">分析时间</div>
                      <div className="text-[12px] font-semibold text-[#334155] leading-snug mt-0.5">{new Date(analysisData.analysis.timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</div>
                    </div>
                  </div>
                  {/* A4: outcome 反馈按钮 — 反哺 KB, 让系统越来越懂运维 */}
                  <div className="flex items-center gap-1.5 mt-3 flex-wrap">
                    <span className="text-[10px] text-[#94A3B8] uppercase tracking-wider mr-1">采纳反馈</span>
                    {[
                      { v: '采纳', color: '#16A34A' },
                      { v: '已解决', color: '#0EA5E9' },
                      { v: '未解决', color: '#EA580C' },
                      { v: '误报', color: '#94A3B8' },
                    ].map(o => {
                      const active = (analysisData.analysis as any)?.outcome === o.v;
                      return (
                        <button key={o.v} onClick={() => submitOutcome(o.v)}
                          disabled={active}
                          className={`text-[11px] px-2 py-1 rounded-md font-medium transition-colors ${
                            active ? 'opacity-50 cursor-default'
                                   : 'border hover:brightness-95'
                          }`}
                          style={active
                            ? { background: o.color, color: 'white', borderColor: o.color }
                            : { color: o.color, background: 'white', borderColor: o.color }}>
                          {o.v}{active && ' ✓'}
                        </button>
                      );
                    })}
                    {(analysisData.analysis as any)?.outcome && (
                      <span className="text-[10px] text-[#64748B] ml-2">
                        {(analysisData.analysis as any).outcome} ·
                        {(analysisData.analysis as any).outcome_by}
                      </span>
                    )}
                    {outcomeMsg && (
                      <span className={`text-[11px] ml-2 ${outcomeMsg.ok ? 'text-[#16A34A]' : 'text-[#DC2626]'}`}>
                        {outcomeMsg.text}
                      </span>
                    )}
                  </div>
                </div>
                <div className="px-5 py-4 text-sm text-[#334155] leading-relaxed">
                  {analysisData.analysis.analysis
                    ? renderSimpleMarkdown(analysisData.analysis.analysis)
                    : <span className="text-[#94A3B8]">（该分组暂无分析内容）</span>}
                </div>
              </div>
            ) : (
              <div className="border border-[#E2E8F0] rounded-xl bg-white text-center py-16">
                <div className="w-14 h-14 rounded-2xl flex items-center justify-center mx-auto mb-3 bg-[#F1F5F9]">
                  <BarChart3 size={26} className="text-[#CBD5E1]" />
                </div>
                <p className="text-[#475569] font-medium text-sm">{analysisGroup ? `分组「${analysisGroup}」暂无分析报告` : '请选择要分析的分组'}</p>
                <p className="text-xs mt-1 text-[#94A3B8]">点击“生成分析”对所选分组进行告警智能分析</p>
                {analysisGroup && (
                  <button onClick={() => setConfirmAnalyze(true)} disabled={analyzing}
                    className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-[#7C3AED] text-white text-sm font-medium hover:bg-[#6D28D9] disabled:opacity-60 transition-colors">
                    <BarChart3 size={14} /> 生成分析
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {/* ══ Checklist 预案清单（仅管理员可见） ══ */}
        {activeTab === 'checklist' && (
          <div className="h-full overflow-y-auto p-5">
            {plansLoading && !plansData ? (
              <div className="text-center py-16">
                <RefreshCw size={22} className="animate-spin text-[#CBD5E1] mx-auto" />
                <p className="text-[#94A3B8] text-sm mt-3">加载预案清单中…</p>
              </div>
            ) : plansData?.error ? (
              <div className="border border-red-200 rounded-xl bg-red-50 px-4 py-5 flex items-center gap-2 text-[13px] text-red-700">
                <ShieldAlert size={16} /> {plansData.error}
              </div>
            ) : (
              <>
                {/* 头部统计 + 新增入口 */}
                <div className="flex items-center gap-3 mb-4 flex-wrap">
                  <div className="flex items-center gap-2">
                    <BookOpen size={15} className="text-[#0F172A]" />
                    <span className="text-[13px] font-semibold text-[#0F172A]">Checklist 预案清单</span>
                  </div>
                  {plansData && (
                    <span className="text-[12px] text-[#64748B]">
                      <strong className="text-[#0F172A]">{plansData.total}</strong> 条预案
                      · <strong className="text-[#0F172A]">{plansData.active_hit_total}</strong> 条活跃告警命中
                      {plansData.source && (
                        <span className="text-[#94A3B8]"> · 数据源：{plansData.source.split('/').pop()}</span>
                      )}
                    </span>
                  )}
                  <button onClick={openAddPlan}
                    className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#0F172A] text-white text-[12px] font-medium hover:bg-[#334155] transition-colors">
                    <Plus size={13} /> 新增预案
                  </button>
                </div>
                {/* 操作反馈 */}
                {planMsg && (
                  <div className={`mb-4 px-3.5 py-2.5 rounded-lg text-[12px] flex items-center gap-2 border ${
                    planMsg.ok
                      ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                      : 'bg-red-50 border-red-200 text-red-700'}`}>
                    {planMsg.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}{planMsg.text}
                  </div>
                )}
                {/* 孤儿规则提示 */}
                {plansData && plansData.orphan_rules.length > 0 && (
                  <div className="mb-4 border border-amber-200 rounded-lg bg-amber-50 px-3.5 py-2.5 text-[12px] text-amber-700">
                    以下匹配规则在预案清单中无对应条目（建议同步维护）：
                    {plansData.orphan_rules.map(r => (
                      <code key={r} className="mx-1 px-1 py-px rounded bg-amber-100 text-[11px] break-all">{r}</code>
                    ))}
                  </div>
                )}
                {/* 预案列表 */}
                <div className="space-y-2">
                  {(plansData?.plans || []).map(p => {
                    const open = expandedPlan === p.title;
                    return (
                      <div key={p.title} className="border border-[#E2E8F0] rounded-xl bg-white overflow-hidden group">
                        <div className="flex items-center gap-1 px-4 py-3 hover:bg-[#F8FAFC] transition-colors">
                        <button
                          onClick={() => setExpandedPlan(open ? null : p.title)}
                          className="flex-1 min-w-0 flex items-center gap-3 text-left">
                          <ChevronRight size={14} className={`text-[#94A3B8] transition-transform shrink-0 ${open ? 'rotate-90' : ''}`} />
                          <span className="font-medium text-[13px] text-[#0F172A] truncate">{p.title}</span>
                          {p.origin === 'custom' && (
                            <span className="text-[10px] px-1.5 py-px rounded-full bg-cyan-50 border border-cyan-200 text-[#0E7490] shrink-0">自定义</span>
                          )}
                          <span className="text-[11px] px-2 py-0.5 rounded-full bg-slate-100 text-[#64748B] shrink-0">
                            {p.variants.map(v => v.sheet).filter(Boolean).join(' / ') || p.sheet || '未分域'}
                          </span>
                          <span className="text-[11px] px-2 py-0.5 rounded-full bg-violet-50 border border-violet-200 text-[#7C3AED] shrink-0">
                            规则 ×{p.rules.length}
                          </span>
                          {p.active_hits > 0 && (
                            <span className="text-[11px] px-2 py-0.5 rounded-full bg-red-50 border border-red-200 text-[#DC2626] shrink-0">
                              活跃命中 {p.active_hits}
                            </span>
                          )}
                        </button>
                        <span className="flex items-center gap-1 shrink-0">
                          <button onClick={e => { e.stopPropagation(); openEditPlan(p); }}
                            className="p-1.5 rounded-lg text-[#64748B] hover:bg-[#F1F5F9] hover:text-[#0F172A] transition-colors" title="编辑预案">
                            <Pencil size={13} />
                          </button>
                          <button onClick={e => { e.stopPropagation(); setDeletingPlan(p); }}
                            className="p-1.5 rounded-lg text-[#94A3B8] hover:bg-red-50 hover:text-[#DC2626] transition-colors" title="移除预案">
                            <Trash2 size={13} />
                          </button>
                        </span>
                        </div>
                        {open && (
                          <div className="border-t border-[#F1F5F9] px-4 py-3.5 space-y-3">
                            {/* 匹配规则 */}
                            <div>
                              <div className="text-[11px] font-semibold text-[#94A3B8] mb-1.5">确定性匹配规则（命中即绑定预案）</div>
                              {p.rules.length ? (
                                <div className="space-y-1">
                                  {p.rules.map(r => (
                                    <code key={r} className="block px-2 py-1 rounded bg-[#F8FAFC] border border-[#E2E8F0] text-[11px] text-[#475569] break-all">{r}</code>
                                  ))}
                                </div>
                              ) : (
                                <span className="text-[12px] text-amber-600">⚠ 无匹配规则（当前依赖 LLM 自由关联，建议补充）</span>
                              )}
                            </div>
                            {/* 处置步骤 */}
                            <div>
                              <div className="text-[11px] font-semibold text-[#94A3B8] mb-1.5">处置步骤</div>
                              <div className="text-[12px] text-[#334155] leading-relaxed whitespace-pre-wrap">{p.steps}</div>
                            </div>
                            {/* 分域变体 */}
                            {p.variants.length > 1 && (
                              <div className="space-y-1.5">
                                {p.variants.map(v => (
                                  <div key={v.sheet} className="rounded-lg bg-[#F8FAFC] border border-[#F1F5F9] px-3 py-2">
                                    <div className="text-[10px] font-semibold text-[#64748B] mb-1">{v.sheet}</div>
                                    <div className="text-[12px] text-[#475569] leading-relaxed whitespace-pre-wrap">{v.steps}</div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        )}

        {/* ── 预案编辑/新增弹窗 ── */}
        {planForm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => !planSaving && setPlanForm(null)}>
            <div className="bg-white rounded-xl shadow-xl w-[560px] max-h-[85vh] flex flex-col overflow-hidden" onClick={e => e.stopPropagation()}>
              <div className="px-5 py-4 border-b border-[#F1F5F9] flex items-center gap-2 shrink-0">
                <Pencil size={15} className="text-[#0F172A]" />
                <span className="font-semibold text-[#0F172A] text-sm">{planForm.mode === 'add' ? '新增预案' : `编辑预案${planForm.origin === 'custom' ? '（自定义）' : '（覆盖文件源）'}`}</span>
              </div>
              <div className="px-5 py-4 space-y-4 overflow-y-auto">
                <div>
                  <label className="block text-[12px] font-medium text-[#475569] mb-1.5">预案名称</label>
                  {planForm.mode === 'add' ? (
                    <input
                      value={planForm.title}
                      onChange={e => setPlanForm({ ...planForm, title: e.target.value })}
                      placeholder="如：存储磁盘故障、DNS解析异常"
                      className="w-full px-3 py-2 rounded-lg border border-[#E2E8F0] text-[13px] text-[#0F172A] focus:outline-none focus:border-[#94A3B8]" />
                  ) : (
                    <div className="px-3 py-2 rounded-lg bg-[#F8FAFC] border border-[#F1F5F9] text-[13px] font-medium text-[#0F172A]">{planForm.title}</div>
                  )}
                </div>
                <div>
                  <label className="block text-[12px] font-medium text-[#475569] mb-1.5">单客户侧处置步骤 <span className="text-[#94A3B8] font-normal">（可选）</span></label>
                  <textarea
                    value={planForm.steps_customer}
                    onChange={e => setPlanForm({ ...planForm, steps_customer: e.target.value })}
                    rows={6}
                    placeholder="1. 第一步…&#10;2. 第二步…"
                    className="w-full px-3 py-2 rounded-lg border border-[#E2E8F0] text-[12px] text-[#334155] leading-relaxed focus:outline-none focus:border-[#94A3B8] font-mono" />
                </div>
                <div>
                  <label className="block text-[12px] font-medium text-[#475569] mb-1.5">平台侧处置步骤 <span className="text-[#94A3B8] font-normal">（可选，至少填一侧）</span></label>
                  <textarea
                    value={planForm.steps_platform}
                    onChange={e => setPlanForm({ ...planForm, steps_platform: e.target.value })}
                    rows={6}
                    placeholder="1. 第一步…&#10;2. 第二步…"
                    className="w-full px-3 py-2 rounded-lg border border-[#E2E8F0] text-[12px] text-[#334155] leading-relaxed focus:outline-none focus:border-[#94A3B8] font-mono" />
                </div>
                <p className="text-[11px] text-[#94A3B8] leading-relaxed">
                  保存后即时生效：告警匹配、智能分析、预案清单同步使用新内容。匹配规则仍需在代码 _NMS_CHECKLIST_RULES 维护（需回放验证防误配）。
                </p>
              </div>
              <div className="px-5 py-3 bg-[#F8FAFC] flex justify-end gap-2 shrink-0">
                <button onClick={() => setPlanForm(null)} disabled={planSaving}
                  className="px-3.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] transition-colors disabled:opacity-50">取消</button>
                <button onClick={savePlan}
                  disabled={planSaving || (planForm.mode === 'add' ? !planForm.title.trim() : false) || (!planForm.steps_customer.trim() && !planForm.steps_platform.trim())}
                  className="px-3.5 py-1.5 rounded-lg bg-[#0F172A] text-white text-[13px] font-medium hover:bg-[#334155] transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                  {planSaving ? '保存中…' : '保存并生效'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ── 预案移除确认弹窗 ── */}
        {deletingPlan && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => !planSaving && setDeletingPlan(null)}>
            <div className="bg-white rounded-xl shadow-xl w-[420px] overflow-hidden" onClick={e => e.stopPropagation()}>
              <div className="px-5 py-4 border-b border-[#F1F5F9] flex items-center gap-2">
                <Trash2 size={15} className="text-[#DC2626]" />
                <span className="font-semibold text-[#0F172A] text-sm">确认移除预案</span>
              </div>
              <div className="px-5 py-4 text-sm text-[#475569]">
                确定移除预案 <strong className="text-[#0F172A]">“{deletingPlan.title}”</strong>？
                <p className="text-xs mt-2 leading-relaxed" style={{ color: deletingPlan.origin === 'custom' ? '#DC2626' : '#94A3B8' }}>
                  {deletingPlan.origin === 'custom'
                    ? '自定义预案将被物理删除，此操作不可恢复。'
                    : '文件源预案仅从匹配/分析中移除（xlsx 原文保留），重新编辑同名预案即可恢复。'}
                </p>
              </div>
              <div className="px-5 py-3 bg-[#F8FAFC] flex justify-end gap-2">
                <button onClick={() => setDeletingPlan(null)} disabled={planSaving}
                  className="px-3.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] transition-colors disabled:opacity-50">取消</button>
                <button onClick={deletePlan} disabled={planSaving}
                  className="px-3.5 py-1.5 rounded-lg bg-[#DC2626] text-white text-[13px] font-medium hover:bg-[#B91C1C] transition-colors disabled:opacity-50">
                  {planSaving ? '移除中…' : '确认移除'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ── 分析确认弹窗 ── */}
        {confirmAnalyze && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setConfirmAnalyze(false)}>
            <div className="bg-white rounded-xl shadow-xl w-[400px] overflow-hidden" onClick={e => e.stopPropagation()}>
              <div className="px-5 py-4 border-b border-[#F1F5F9] flex items-center gap-2">
                <BarChart3 size={16} className="text-[#7C3AED]" />
                <span className="font-semibold text-[#0F172A] text-sm">确认告警分析</span>
              </div>
              <div className="px-5 py-4 text-sm text-[#475569]">
                是否对分组 <strong className="text-[#0F172A]">“{analysisGroup}”</strong> 进行告警分析？
                <p className="text-xs text-[#94A3B8] mt-2">系统将基于该分组当前告警快照进行 LLM 智能分析，约需 1-2 分钟完成。</p>
              </div>
              <div className="px-5 py-3 bg-[#F8FAFC] flex justify-end gap-2">
                <button onClick={() => setConfirmAnalyze(false)} className="px-3.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[13px] text-[#475569] hover:border-[#CBD5E1] transition-colors">取消</button>
                <button onClick={triggerAnalyze} className="px-3.5 py-1.5 rounded-lg bg-[#7C3AED] text-white text-[13px] font-medium hover:bg-[#6D28D9] transition-colors">确认分析</button>
              </div>
            </div>
          </div>
        )}
        </div>{/* /主区 */}

        {/* 右侧告警详情面板：点矩阵/网段/类型后展开，主区同步回缩（不遮挡内容） */}
        {/* refreshKey = 快照时间戳：抓取完成后变化，面板会立即静默刷新，不等 15s 轮询 */}
        {activeTab === 'incidents' && (
          <div className={`shrink-0 overflow-hidden transition-[width] duration-200 ${drillOpen ? 'w-[440px] max-w-[45vw]' : 'w-0'}`}>
            <DrillSidePanel
              open={drillOpen}
              cidr={incSeg}
              typeName={incType}
              cidrExclude={incSegEx}
              typeExclude={incTypeEx}
              groups={[...selectedGroups].join(',')}
              token={token || ''}
              incidents={filteredIncidents}
              refreshKey={data?.latest?.timestamp || ''}
              sessionKey={drillSeq}
              onClose={() => setDrillOpen(false)}
              onClear={clearDrill}
              onOpenIncident={openIncidentFromPanel} />
          </div>
        )}
        {/* 右侧证书调查面板：与告警详情共用停靠区，主区同步回缩 */}
        <div data-testid="ssl-dock" className={`shrink-0 overflow-hidden transition-[width] duration-200 ${sslOpen ? 'w-[440px] max-w-[45vw]' : 'w-0'}`}>
          <NmsSslInvestigation
            open={sslOpen}
            token={token || ''}
            isAdmin={isAdmin}
            target={sslTarget}
            sessionKey={sslSeq}
            onSnapshotRetry={retrySslWithLatest}
            onClose={() => setSslOpen(false)} />
        </div>
      </div>
    </div>
  );
}

export default function NmsPage() {
  return <NmsContent />;
}
