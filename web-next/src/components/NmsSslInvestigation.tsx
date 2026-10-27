'use client';

/**
 * SSL 证书调查侧栏（右侧停靠，不遮挡主区）。
 *
 * - 从告警行发起：创建/复用调查（服务端校验快照引用），展示证据、人工处置、
 *   自动恢复跟踪、案例提交与引用。
 * - 调查记录：我的调查 + 有权限的案例（管理员可审核）。
 * - 打开且页面可见时 5 秒轮询；隐藏暂停。AbortController + 请求序号防旧响应覆盖。
 * - 只读取证：不调用 LLM、不 ACK、不改动生产配置。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ShieldAlert, X, RefreshCw, CheckCircle, AlertTriangle, Clock,
  ChevronLeft, BookOpen, Pause, Play, FileCheck,
} from 'lucide-react';

// ─── Types ────────────────────────────────────────────────────────────────────

export interface SslPanelTarget {
  alarmId: string;
  snapshotTimestamp: string;
  snapshotRef: string;
  incidentId?: string;
  summary?: string;
  source?: string;
}

export interface SslPanelProps {
  open: boolean;
  token: string;
  isAdmin: boolean;
  /** null = 仅打开「调查记录」 */
  target: SslPanelTarget | null;
  /** 每次打开递增：丢弃上一轮内容 */
  sessionKey: number;
  /** 快照过期时用页面最新快照重试 */
  onSnapshotRetry?: () => void;
  onClose: () => void;
}

interface SslCert {
  ip?: string;
  fingerprint?: string;
  remaining_seconds?: number;
  verified?: boolean;
  not_after?: string;
  subject?: string;
  issuer?: string;
  error?: string;
  defect_type?: string;
}

interface SslHealth {
  diagnosis?: string;
  defect_type?: string;
  error?: string;
  observed_at?: string;
  min_days?: number;
  certificates?: SslCert[];
}

interface SslTimelineItem {
  at: string;
  event: string;
  actor: string;
  detail: string;
}

interface SslRecovery {
  mode?: 'tls' | 'manual';
  tls_ok?: boolean | null;
  nms_clear_ok?: boolean;
  clear_evidence?: Record<string, unknown> | null;
  manual_completed_at?: string | null;
  manual_clear_first_at?: string | null;
  manual_quiet_s?: number;
}

interface SslInv {
  id: string;
  status: string;
  owner: string;
  scope: string;
  target?: { host: string; port: number };
  verification?: 'tls' | 'manual';
  verification_note?: string;
  verification_result?: string;
  alarm?: {
    alarm_id?: string; serial?: string; source?: string; summary?: string;
    severity?: string; event_key?: string; event_group?: string;
    first_time?: string; last_time?: string;
  };
  manual_completed_at?: string | null;
  manual_action?: { category?: string; note?: string } | null;
  healthy_streak?: number;
  health?: SslHealth;
  diagnosis?: Record<string, unknown> | null;
  last_check_at?: string;
  nms_last_observed_at?: string;
  nms_last_result?: string;
  nms_last_error?: string;
  nms_clear_note?: string;
  clear_evidence?: Record<string, unknown> | null;
  next_check_at?: string;
  pause_reason?: string;
  paused_at?: string;
  recovered_at?: string;
  created_at?: string;
  updated_at?: string;
  version: number;
  timeline?: SslTimelineItem[];
  timeline_total?: number;
  checks?: Record<string, unknown>[];
  recovery?: SslRecovery;
  cases?: SslCaseRef[];
  feedback?: { at: string; helpful: boolean; note?: string }[];
}

interface SslListItem {
  id: string;
  status: string;
  target?: { host: string; port: number };
  verification?: 'tls' | 'manual';
  created_at?: string;
  updated_at?: string;
  healthy_streak?: number;
  diagnosis?: string;
  defect_type?: string;
  last_check_at?: string;
  nms_last_observed_at?: string;
  manual_completed_at?: string;
  recovered_at?: string;
  pause_reason?: string;
  alarm?: { summary?: string; source?: string; severity?: string };
}

interface SslCaseRef {
  id: string;
  investigation_id?: string;
  version?: number;
  status?: string;
  owner?: string;
  scope?: string;
  target?: { host?: string; port?: number };
  root_cause?: string;
  action?: string;
  conditions?: string;
  created_at?: string;
  reviewed_by?: string;
  reviewed_at?: string;
  review_note?: string;
  fault_type?: string;
}

interface ApiFailure {
  status: number;
  code: string;
  message: string;
  retryable: boolean;
}

class SslApiError extends Error implements ApiFailure {
  status: number;
  code: string;
  retryable: boolean;
  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

async function request<T>(path: string, opts: {
  method?: string;
  body?: unknown;
  token: string;
  signal?: AbortSignal;
}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: {
      Authorization: `Bearer ${opts.token || ''}`,
      ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
    cache: 'no-store',
  });
  let payload: unknown = null;
  try { payload = await res.json(); } catch { /* 非 JSON 响应 */ }
  if (!res.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;
    if (typeof detail === 'string') {
      throw new SslApiError(res.status, `http_${res.status}`, detail, false);
    }
    const d = (detail || {}) as { code?: string; message?: string; retryable?: boolean };
    throw new SslApiError(res.status, d.code || `http_${res.status}`,
      d.message || `请求失败（HTTP ${res.status}）`, !!d.retryable);
  }
  return payload as T;
}

function asApiError(e: unknown): ApiFailure {
  if (e instanceof SslApiError) {
    return { status: e.status, code: e.code, message: e.message, retryable: e.retryable };
  }
  if (e instanceof Error && e.name === 'AbortError') {
    return { status: 0, code: 'aborted', message: '请求已取消', retryable: true };
  }
  return { status: 0, code: 'network_error',
    message: e instanceof Error ? e.message : '网络异常，请稍后重试', retryable: true };
}

// ─── Labels ───────────────────────────────────────────────────────────────────

const STATUS_META: Record<string, { label: string; color: string; bg: string; border: string }> = {
  queued:         { label: '排队核验', color: '#475569', bg: '#F8FAFC', border: '#E2E8F0' },
  investigating:  { label: '正在核验', color: '#2563EB', bg: '#EFF6FF', border: '#BFDBFE' },
  awaiting_action:{ label: '待人工处置', color: '#C2410C', bg: '#FFF7ED', border: '#FED7AA' },
  observing:      { label: '自动跟踪中', color: '#0369A1', bg: '#F0F9FF', border: '#BAE6FD' },
  waiting_nms:    { label: '等待 NMS 确认', color: '#6D28D9', bg: '#F5F3FF', border: '#DDD6FE' },
  recovered:      { label: '已恢复', color: '#15803D', bg: '#F0FDF4', border: '#BBF7D0' },
  paused:         { label: '已暂停', color: '#64748B', bg: '#F8FAFC', border: '#E2E8F0' },
};

const DIAG_META: Record<string, { label: string; color: string; bg: string; border: string }> = {
  healthy:   { label: '严格校验通过', color: '#15803D', bg: '#F0FDF4', border: '#BBF7D0' },
  unhealthy: { label: '证书异常', color: '#B91C1C', bg: '#FEF2F2', border: '#FECACA' },
  unknown:   { label: '证据不足', color: '#B45309', bg: '#FFFBEB', border: '#FDE68A' },
};

const DEFECT_LABEL: Record<string, string> = {
  ok: '无缺陷',
  expired: '证书已过期',
  expiring_soon: '剩余有效期不足门槛',
  hostname_mismatch: '域名与证书不匹配',
  untrusted: '证书链不可信',
  not_yet_valid: '证书尚未生效',
  connect_failed: '连接失败',
  timeout: '连接超时',
  resolve_failed: 'DNS 解析失败',
  address_blocked: '地址被安全策略拒绝',
  too_many_addresses: '解析地址超出上限',
  inconsistent_certificates: '多地址证书不一致',
  incomplete: '证书证据不完整',
  probe_error: '探测异常',
};

const CATEGORY_LABEL: Record<string, string> = {
  renewed: '已续期/更换证书',
  replaced: '替换设备或证书',
  chain_fixed: '修复证书链',
  config_changed: '调整服务配置',
  human_checked_no_change: '人工核查，未变更',
  other: '其他处置',
};

const EVENT_LABEL: Record<string, string> = {
  created: '创建调查',
  diagnosed: '首次取证',
  manual_completed: '人工处置完成',
  recheck_requested: '请求复核',
  recheck: '单次复核',
  waiting_nms: 'TLS 达标',
  regressed: 'TLS 异常回退',
  clear_observed: 'NMS 清除证据',
  recovered: '恢复终结',
  paused: '暂停',
  resumed: '恢复跟踪',
  case_submitted: '提交案例',
  feedback: '反馈',
};

const SEV_LABEL: Record<string, string> = {
  critical: '严重', major: '重要', minor: '次要', warning: '警告',
  indeterminate: '不确定', cleared: '已清除', clear: '已清除', unknown: '未知',
};

function fmtTs(value?: string | null): string {
  if (!value) return '—';
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return value;
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtRemaining(seconds?: number): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  if (seconds <= 0) return '已过期';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  return days > 0 ? `${days} 天 ${hours} 小时` : `${hours} 小时`;
}

function isEvidenceStale(observedAt?: string | null): boolean {
  if (!observedAt) return true;
  const ms = Date.parse(observedAt);
  if (!Number.isFinite(ms)) return true;
  return Date.now() - ms > 5 * 60 * 1000;
}

function statusMeta(status?: string) {
  return STATUS_META[status || ''] || STATUS_META.queued;
}

function diagMeta(diagnosis?: string) {
  return DIAG_META[diagnosis || ''] || { label: '尚未核验', color: '#64748B', bg: '#F8FAFC', border: '#E2E8F0' };
}

function Chip({ text, color, bg, border, title }: {
  text: string; color: string; bg: string; border: string; title?: string;
}) {
  return (
    <span className="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded border whitespace-nowrap"
      style={{ color, background: bg, borderColor: border }} title={title}>{text}</span>
  );
}

function StatusChip({ status }: { status?: string }) {
  const m = statusMeta(status);
  return <Chip text={m.label} color={m.color} bg={m.bg} border={m.border} />;
}

function CaseStatusChip({ status }: { status?: string }) {
  const map: Record<string, { t: string; c: string; bg: string; b: string }> = {
    pending: { t: '待审核', c: '#C2410C', bg: '#FFF7ED', b: '#FED7AA' },
    approved: { t: '已批准', c: '#15803D', bg: '#F0FDF4', b: '#BBF7D0' },
    returned: { t: '已退回', c: '#B91C1C', bg: '#FEF2F2', b: '#FECACA' },
    revoked: { t: '已撤回', c: '#64748B', bg: '#F8FAFC', b: '#E2E8F0' },
  };
  const m = map[status || ''] || { t: status || '—', c: '#64748B', bg: '#F8FAFC', b: '#E2E8F0' };
  return <Chip text={m.t} color={m.c} bg={m.bg} border={m.b} />;
}

function Banner({ kind, children }: { kind: 'error' | 'warn' | 'info' | 'ok'; children: React.ReactNode }) {
  const style = kind === 'error' ? 'border-[#FECACA] bg-[#FEF2F2] text-[#B91C1C]'
    : kind === 'warn' ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#B45309]'
      : kind === 'ok' ? 'border-[#BBF7D0] bg-[#F0FDF4] text-[#15803D]'
        : 'border-[#BAE6FD] bg-[#F0F9FF] text-[#0369A1]';
  return (
    <div className={`border rounded-lg px-2.5 py-2 text-[11px] leading-4 flex items-start gap-1.5 ${style}`}>
      {kind === 'ok' ? <CheckCircle size={12} className="mt-0.5 shrink-0" />
        : kind === 'info' ? <Clock size={12} className="mt-0.5 shrink-0" />
          : <AlertTriangle size={12} className="mt-0.5 shrink-0" />}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function SectionTitle({ icon, text, extra }: { icon: React.ReactNode; text: string; extra?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5 mb-1.5">
      <span className="text-[#94A3B8]">{icon}</span>
      <span className="text-[11px] font-semibold text-[#334155]">{text}</span>
      {extra && <span className="ml-auto">{extra}</span>}
    </div>
  );
}

// ─── Detail view ──────────────────────────────────────────────────────────────

function DetailView(props: {
  inv: SslInv;
  acting: boolean;
  myCase: SslCaseRef | null;
  onRecheck: () => void;
  onPause: () => void;
  onResume: () => void;
  onManual: (category: string, note: string) => Promise<boolean>;
  onSubmitCase: (rootCause: string, action: string, conditions: string) => Promise<boolean>;
  onFeedback: (helpful: boolean, note: string) => void;
}) {
  const { inv, acting, myCase } = props;
  const [manualOpen, setManualOpen] = useState(false);
  const [category, setCategory] = useState('renewed');
  const [note, setNote] = useState('');
  const [caseOpen, setCaseOpen] = useState(false);
  const [rootCause, setRootCause] = useState('');
  const [actionText, setActionText] = useState('');
  const [conditions, setConditions] = useState('');
  const [fbNote, setFbNote] = useState('');

  const health = inv.health || {};
  const diagnosis = health.diagnosis;
  const dm = diagMeta(diagnosis);
  const recovery = inv.recovery || {};
  const streak = inv.healthy_streak || 0;
  const certs = health.certificates || [];
  const stale = (inv.status === 'observing' || inv.status === 'waiting_nms') && isEvidenceStale(health.observed_at);
  const nmsEmpty = inv.nms_last_result === 'empty';
  const nmsError = inv.nms_last_result === 'error';
  const nmsUnknown = !inv.nms_last_observed_at;

  const canManual = inv.status !== 'recovered';
  const canSubmitCase = inv.status === 'recovered';
  const isManual = (inv.verification || recovery.mode || 'tls') === 'manual';
  const quietS = Number(recovery.manual_quiet_s || 1800);
  const clearFirst = recovery.manual_clear_first_at
    ? new Date(String(recovery.manual_clear_first_at)).getTime() : 0;
  const quietElapsed = clearFirst ? Math.max(0, Math.floor((Date.now() - clearFirst) / 1000)) : 0;

  return (
    <div className="flex-1 overflow-y-auto">
      {/* 流程状态 */}
      <div className="px-4 py-3 border-b border-[#F1F5F9]">
        <div className="flex items-center gap-1.5 flex-wrap">
          <StatusChip status={inv.status} />
          <Chip text={dm.label} color={dm.color} bg={dm.bg} border={dm.border} />
          {isManual && (
            <Chip text="人工核验" color="#B45309" bg="#FFFBEB" border="#FDE68A" />
          )}
          {inv.pause_reason && (
            <Chip text={inv.pause_reason === 'track_window_expired' ? '24 小时窗口到期' : '手动暂停'}
              color="#64748B" bg="#F8FAFC" border="#E2E8F0" />
          )}
          <span className="ml-auto text-[10px] text-[#94A3B8] font-mono">v{inv.version}</span>
        </div>
        <div className="mt-2 text-[12px] text-[#0F172A] font-medium truncate"
          title={inv.alarm?.summary || ''}>
          {inv.alarm?.summary || 'SSL 证书告警'}
        </div>
        <div className="mt-1 flex items-center gap-2 flex-wrap text-[10px] text-[#64748B]">
          <span className="font-mono">{inv.target?.host}:{inv.target?.port}</span>
          <span className="font-mono">{inv.alarm?.source || '—'}</span>
          {inv.alarm?.severity && <span>{SEV_LABEL[inv.alarm.severity] || inv.alarm.severity}</span>}
          {inv.alarm?.serial && <span className="font-mono">serial {inv.alarm.serial}</span>}
          <span className="ml-auto">{inv.scope === 'personal' ? '个人范围' : '全局范围'}</span>
        </div>
      </div>

      <div className="px-4 py-3 space-y-3">
        {isManual && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 leading-relaxed">
            该目标不做 TLS 自动取证{inv.verification_note ? `（${inv.verification_note}）` : ''}。
            闭环口径：人工处置 + NMS 明确清除持续 {Math.round(quietS / 60)} 分钟（无同身份活跃替代）才判定恢复，
            不会以证书健康作为证据。
          </div>
        )}
        {/* 提示：状态语义分开表达 */}
        {inv.status === 'waiting_nms' && (
          <Banner kind="info">
            {isManual
              ? `人工核验模式：已完成处置，等待 NMS 清除持续 ${Math.round(quietS / 60)} 分钟（无同身份活跃替代）才算恢复。`
              : '实测恢复，等待 NMS 确认：证书链、域名与剩余有效期已达标，仍需采集到原告警的明确清除记录才算恢复。'}
          </Banner>
        )}
        {inv.status === 'recovered' && (
          <Banner kind="ok">
            {isManual
              ? '已恢复：人工处置后 NMS 清除持续达标，未做 TLS 验证（不计为证书健康）。'
              : '已恢复：TLS 严格实测健康且 NMS 已明确清除原告警。'}
            消息表述为“操作后观察到恢复”，不代表根因已被因果证明。
          </Banner>
        )}
        {stale && (
          <Banner kind="warn">证据已过期：最近一次 TLS 实测超过 5 分钟，不计入连续健康次数。</Banner>
        )}
        {(nmsUnknown || nmsEmpty || nmsError) && inv.status !== 'recovered' && (
          <Banner kind="warn">
            {nmsError
              ? `采集异常：${inv.nms_last_error || '本轮采集失败'}，不能作为清除证据`
              : nmsUnknown ? '暂无 NMS 采集观测记录' : '本轮采集为空，不能作为清除证据'}；
            告警消失、ACK 或采集失败都不算清除。
          </Banner>
        )}
        {health.error && diagnosis !== 'healthy' && (
          <Banner kind="error">
            核验说明：{health.error}
            {health.defect_type ? `（${DEFECT_LABEL[health.defect_type] || health.defect_type}）` : ''}
          </Banner>
        )}

        {/* 证据：TLS 实测（人工核验模式不探测，整块隐藏） */}
        <section className={`rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5${isManual ? ' hidden' : ''}`}>
          <SectionTitle icon={<ShieldAlert size={12} />} text="TLS 严格实测"
            extra={<span className="text-[10px] text-[#94A3B8]">{fmtTs(health.observed_at)}</span>} />
          <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
            <div className="truncate"><span className="text-[#94A3B8]">诊断：</span>
              <span style={{ color: dm.color }}>{dm.label}</span></div>
            <div className="truncate"><span className="text-[#94A3B8]">缺陷：</span>
              <span className="text-[#475569]">{health.defect_type ? (DEFECT_LABEL[health.defect_type] || health.defect_type) : '—'}</span></div>
            <div className="truncate"><span className="text-[#94A3B8]">有效期门槛：</span>
              <span className="text-[#475569]">剩余 ≥ {health.min_days ?? 30} 天</span></div>
            <div className="truncate"><span className="text-[#94A3B8]">连续健康：</span>
              <span className="text-[#475569] font-mono">{streak} / 3 次</span></div>
          </div>
          {certs.length > 0 && (
            <div className="mt-2 border border-[#F1F5F9] rounded-lg overflow-hidden">
              <table className="w-full table-fixed">
                <thead>
                  <tr className="bg-[#F8FAFC] text-[9px] text-[#94A3B8] uppercase tracking-wider">
                    <th className="text-left px-2 py-1 font-medium">地址</th>
                    <th className="text-left px-2 py-1 font-medium w-[76px]">校验</th>
                    <th className="text-left px-2 py-1 font-medium w-[86px]">剩余</th>
                    <th className="text-left px-2 py-1 font-medium w-[92px]">指纹</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F1F5F9]">
                  {certs.slice(0, 4).map((c, i) => (
                    <tr key={`${c.ip || i}`} className="text-[10px]">
                      <td className="px-2 py-1 font-mono text-[#475569] truncate" title={c.ip || ''}>{c.ip || '—'}</td>
                      <td className="px-2 py-1">
                        <span style={{ color: c.verified ? '#15803D' : '#B91C1C' }}>
                          {c.verified ? '通过' : (DEFECT_LABEL[c.defect_type || ''] || '未通过')}
                        </span>
                      </td>
                      <td className="px-2 py-1 text-[#475569] tabular-nums" title={c.not_after ? `到期 ${c.not_after}` : ''}>
                        {fmtRemaining(c.remaining_seconds)}
                      </td>
                      <td className="px-2 py-1 font-mono text-[#94A3B8] truncate" title={c.fingerprint || ''}>
                        {(c.fingerprint || '—').slice(0, 12)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {certs.length === 0 && (
            <p className="mt-1.5 text-[10px] text-[#94A3B8]">本轮未取得证书证据（解析失败或地址被阻断时不会判定健康）。</p>
          )}
        </section>

        {/* 证据：NMS 观测 */}
        <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
          <SectionTitle icon={<RefreshCw size={12} />} text="NMS 采集观测"
            extra={<span className="text-[10px] text-[#94A3B8]">{fmtTs(inv.nms_last_observed_at)}</span>} />
          <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
            <div className="truncate"><span className="text-[#94A3B8]">本轮结果：</span>
              <span className="text-[#475569]">
                {inv.nms_last_observed_at
                  ? nmsError ? '采集异常（不算清除）'
                    : nmsEmpty ? '空快照（不算清除）' : '已读取告警行'
                  : '—'}
              </span></div>
            <div className="truncate"><span className="text-[#94A3B8]">明确清除：</span>
              <span style={{ color: recovery.nms_clear_ok ? '#15803D' : '#B45309' }}>
                {recovery.nms_clear_ok ? '已取得' : '待确认'}
              </span></div>
          </div>
          {!recovery.nms_clear_ok && inv.nms_clear_note && (
            <p className="mt-1 text-[10px] text-[#94A3B8]">{inv.nms_clear_note}</p>
          )}
          {recovery.clear_evidence && (
            <p className="mt-1 text-[10px] text-[#64748B] font-mono break-all">
              serial={String(recovery.clear_evidence.serial || '—')} ·
              {' '}{String(recovery.clear_evidence.severity || '')} ·
              {' '}{fmtTs(String(recovery.clear_evidence.observed_at || ''))}
            </p>
          )}
        </section>

        {/* 恢复条件 */}
        <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
          <SectionTitle icon={<CheckCircle size={12} />} text="恢复条件（需同时满足）" />
          <div className="space-y-1 text-[11px]">
            {isManual ? (
              <>
                <div className="flex items-center gap-1.5">
                  {inv.manual_completed_at ? <CheckCircle size={12} className="text-[#16A34A]" /> : <Clock size={12} className="text-[#CBD5E1]" />}
                  <span className="text-[#475569]">人工处置已完成（记录根因与动作）</span>
                </div>
                <div className="flex items-center gap-1.5">
                  {quietElapsed >= quietS ? <CheckCircle size={12} className="text-[#16A34A]" /> : <Clock size={12} className="text-[#CBD5E1]" />}
                  <span className="text-[#475569]">NMS 清除持续 ≥ {Math.round(quietS / 60)} 分钟，且无同身份活跃替代</span>
                  <span className="ml-auto font-mono text-[10px] text-[#94A3B8]">
                    {clearFirst ? `${Math.min(quietElapsed, quietS)}/${quietS}s` : '未开始计时'}
                  </span>
                </div>
              </>
            ) : (
              <>
                <div className="flex items-center gap-1.5">
                  {recovery.tls_ok ? <CheckCircle size={12} className="text-[#16A34A]" /> : <Clock size={12} className="text-[#CBD5E1]" />}
                  <span className="text-[#475569]">TLS 连续 3 次严格校验健康（相邻 ≥60 秒、跨度 ≥120 秒）</span>
                  <span className="ml-auto font-mono text-[10px] text-[#94A3B8]">{streak}/3</span>
                </div>
                <div className="flex items-center gap-1.5">
                  {recovery.nms_clear_ok ? <CheckCircle size={12} className="text-[#16A34A]" /> : <Clock size={12} className="text-[#CBD5E1]" />}
                  <span className="text-[#475569]">NMS 明确清除原告警（晚于人工完成、无同身份活跃替代）</span>
                </div>
              </>
            )}
          </div>
          {inv.manual_action?.note && (
            <p className="mt-1.5 text-[10px] text-[#64748B] truncate"
              title={inv.manual_action.note}>
              人工处置：{CATEGORY_LABEL[inv.manual_action.category || ''] || inv.manual_action.category}
              （{fmtTs(inv.manual_completed_at)}）· {inv.manual_action.note}
            </p>
          )}
        </section>

        {/* 操作 */}
        <div className="flex items-center gap-2 flex-wrap">
          {!isManual && (
            <button onClick={props.onRecheck} disabled={acting || inv.status === 'recovered'}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[11px] text-[#475569] hover:border-[#CBD5E1] disabled:opacity-50 transition-colors">
              <RefreshCw size={11} className={acting ? 'animate-spin' : ''} /> 立即复核
            </button>
          )}
          {inv.status === 'paused' ? (
            <button onClick={props.onResume} disabled={acting}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[11px] text-[#475569] hover:border-[#CBD5E1] disabled:opacity-50 transition-colors">
              <Play size={11} /> 恢复跟踪
            </button>
          ) : (
            <button onClick={props.onPause} disabled={acting || inv.status === 'recovered'}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[11px] text-[#64748B] hover:border-[#CBD5E1] disabled:opacity-50 transition-colors">
              <Pause size={11} /> 暂停跟踪
            </button>
          )}
          {canManual && (
            <button onClick={() => setManualOpen(v => !v)}
              className="ml-auto inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-[#0F172A] text-white text-[11px] font-medium hover:bg-[#1E293B] transition-colors">
              <FileCheck size={11} /> 标记处置完成
            </button>
          )}
        </div>

        {/* 人工处置表单 */}
        {canManual && manualOpen && (
          <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
            <SectionTitle icon={<FileCheck size={12} />} text="人工处置记录"
              extra={<span className="text-[10px] text-[#94A3B8]">标记后开始自动复核</span>} />
            <div className="space-y-2">
              <select value={category} onChange={e => setCategory(e.target.value)}
                className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]">
                {Object.entries(CATEGORY_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
              <textarea value={note} onChange={e => setNote(e.target.value)} rows={3}
                placeholder="填写实际处置说明（必填），例如：已更换证书并重载服务"
                className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] resize-none focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
              <div className="flex items-center gap-2">
                <button disabled={acting || !note.trim()}
                  onClick={async () => {
                    const ok = await props.onManual(category, note);
                    if (ok) { setNote(''); setManualOpen(false); }
                  }}
                  className="px-2.5 py-1.5 rounded-lg bg-[#0F172A] text-white text-[11px] font-medium hover:bg-[#1E293B] disabled:opacity-40 transition-colors">
                  {acting ? '提交中…' : '确认已处置'}
                </button>
                <span className="text-[10px] text-[#94A3B8]">提交后人工完成时间与跟踪窗口重新计时</span>
              </div>
            </div>
          </section>
        )}

        {/* 案例提交 */}
        {canSubmitCase && (
          <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
            <SectionTitle icon={<BookOpen size={12} />} text="成功案例"
              extra={myCase ? <CaseStatusChip status={myCase.status} /> : <span className="text-[10px] text-[#94A3B8]">待提交</span>} />
            {myCase ? (
              <div className="space-y-1 text-[11px]">
                <div className="text-[#475569]"><span className="text-[#94A3B8]">实际原因：</span>{myCase.root_cause}</div>
                <div className="text-[#475569]"><span className="text-[#94A3B8]">实际操作：</span>{myCase.action}</div>
                {myCase.conditions && <div className="text-[#475569]"><span className="text-[#94A3B8]">适用条件：</span>{myCase.conditions}</div>}
                {myCase.review_note && <div className="text-[10px] text-[#64748B]">审核说明：{myCase.review_note}</div>}
                {!myCase.status || myCase.status === 'returned' ? (
                  <button onClick={() => setCaseOpen(v => !v)}
                    className="text-[10px] text-[#2563EB] hover:underline">重新提交新版本</button>
                ) : null}
              </div>
            ) : (
              <button onClick={() => setCaseOpen(v => !v)}
                className="text-[11px] text-[#2563EB] hover:underline">提交成功案例（需管理员审核）</button>
            )}
            {caseOpen && (
              <div className="mt-2 space-y-2">
                <input value={rootCause} onChange={e => setRootCause(e.target.value)}
                  placeholder="实际原因（必填）"
                  className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
                <input value={actionText} onChange={e => setActionText(e.target.value)}
                  placeholder="实际操作（必填）"
                  className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
                <input value={conditions} onChange={e => setConditions(e.target.value)}
                  placeholder="适用条件（可选）"
                  className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
                <button disabled={acting || !rootCause.trim() || !actionText.trim()}
                  onClick={async () => {
                    const ok = await props.onSubmitCase(rootCause, actionText, conditions);
                    if (ok) { setRootCause(''); setActionText(''); setConditions(''); setCaseOpen(false); }
                  }}
                  className="px-2.5 py-1.5 rounded-lg bg-[#0F172A] text-white text-[11px] font-medium hover:bg-[#1E293B] disabled:opacity-40 transition-colors">
                  {acting ? '提交中…' : '提交审核'}
                </button>
              </div>
            )}
          </section>
        )}

        {/* 已批准案例引用 */}
        {(inv.cases || []).length > 0 && (
          <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
            <SectionTitle icon={<BookOpen size={12} />} text="同目标已批准案例"
              extra={<span className="text-[10px] text-[#94A3B8]">最多 3 条</span>} />
            <div className="divide-y divide-[#F1F5F9]">
              {(inv.cases || []).map(c => (
                <div key={c.id} className="py-1.5 first:pt-0 last:pb-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-[#64748B] font-mono">{c.target?.host}:{c.target?.port}</span>
                    <Chip text={`v${c.version ?? '—'}`} color="#475569" bg="#F8FAFC" border="#E2E8F0" />
                    <span className="ml-auto text-[10px] text-[#94A3B8]">
                      {c.reviewed_by ? `批准人 ${c.reviewed_by}` : '已批准'}
                    </span>
                  </div>
                  <div className="text-[11px] text-[#475569] leading-4 mt-0.5 line-clamp-2">{c.root_cause} → {c.action}</div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 反馈 */}
        <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
          <SectionTitle icon={<Clock size={12} />} text="反馈（永久记录，不进入成功案例库）" />
          <input value={fbNote} onChange={e => setFbNote(e.target.value)}
            placeholder="补充说明（可选）"
            className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
          <div className="flex items-center gap-2 mt-2">
            <button onClick={() => props.onFeedback(true, fbNote)}
              className="px-2.5 py-1.5 rounded-lg border border-[#BBF7D0] bg-[#F0FDF4] text-[11px] text-[#15803D] hover:border-[#86EFAC] transition-colors">
              有帮助
            </button>
            <button onClick={() => props.onFeedback(false, fbNote)}
              className="px-2.5 py-1.5 rounded-lg border border-[#FECACA] bg-[#FEF2F2] text-[11px] text-[#B91C1C] hover:border-[#FCA5A5] transition-colors">
              未解决 / 建议有误
            </button>
            {(inv.feedback || []).length > 0 && (
              <span className="ml-auto text-[10px] text-[#94A3B8]">已记录 {inv.feedback?.length} 条</span>
            )}
          </div>
        </section>

        {/* 时间线 */}
        <section className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5">
          <SectionTitle icon={<Clock size={12} />} text="时间线"
            extra={<span className="text-[10px] text-[#94A3B8]">
              {(inv.timeline || []).length}/{inv.timeline_total ?? (inv.timeline || []).length}
            </span>} />
          <div className="space-y-1">
            {(inv.timeline || []).slice(-20).reverse().map((t, i) => (
              <div key={`${t.at}-${i}`} className="text-[10px] leading-4">
                <span className="font-mono text-[#94A3B8]">{fmtTs(t.at)}</span>
                <span className="ml-1 text-[#334155]">{EVENT_LABEL[t.event] || t.event}</span>
                <span className="ml-1 text-[#94A3B8]">{t.detail}</span>
              </div>
            ))}
            {(inv.timeline || []).length === 0 && (
              <p className="text-[10px] text-[#94A3B8]">暂无记录</p>
            )}
          </div>
        </section>

        <p className="text-[10px] text-[#94A3B8] leading-4">
          仅读取证书做严格校验，不 ACK 告警、不改动生产配置、不调用模型。
          人工完成不等于恢复，恢复需 TLS 实测与 NMS 明确清除同时满足。
        </p>
      </div>
    </div>
  );
}

// ─── Records view ─────────────────────────────────────────────────────────────

function RecordsView(props: {
  records: SslListItem[];
  recordsLoading: boolean;
  cases: SslCaseRef[];
  casesLoading: boolean;
  isAdmin: boolean;
  me: string;
  acting: boolean;
  onRefresh: () => void;
  onOpen: (id: string) => void;
  onReview: (caseId: string, action: 'approve' | 'return', note: string) => Promise<boolean>;
  onRevoke: (caseId: string, note: string) => Promise<boolean>;
}) {
  const { records, cases, isAdmin, me } = props;
  const [tab, setTab] = useState<'investigations' | 'cases'>('investigations');
  const [caseFilter, setCaseFilter] = useState('');
  const [openCase, setOpenCase] = useState<string | null>(null);
  const [reviewNote, setReviewNote] = useState('');
  const shownCases = caseFilter ? cases.filter(c => c.status === caseFilter) : cases;

  return (
    <>
      <div className="px-4 flex items-center gap-4 border-b border-[#F1F5F9] shrink-0">
        {([
          { k: 'investigations' as const, label: '我的调查', n: records.length },
          { k: 'cases' as const, label: '案例', n: cases.length },
        ]).map(t => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`pb-2 pt-2 text-[12px] border-b-2 -mb-px transition-colors ${
              tab === t.k ? 'border-[#DC2626] text-[#0F172A] font-medium'
                : 'border-transparent text-[#64748B] hover:text-[#334155]'}`}>
            {t.label} <span className="tabular-nums opacity-60">{t.n}</span>
          </button>
        ))}
        <button onClick={props.onRefresh}
          className="ml-auto p-1 rounded-md text-[#94A3B8] hover:text-[#334155] hover:bg-[#F1F5F9] transition-colors"
          title="刷新记录">
          <RefreshCw size={12} className={props.recordsLoading || props.casesLoading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {tab === 'investigations' ? (
          props.recordsLoading && records.length === 0 ? (
            <div className="py-10 text-center text-[11px] text-[#94A3B8]">加载中…</div>
          ) : records.length === 0 ? (
            <div className="py-12 text-center px-6">
              <p className="text-[11px] text-[#64748B]">暂无调查记录</p>
              <p className="text-[10px] text-[#94A3B8] mt-1">在 SSL 告警行点「调查」即可发起只读取证</p>
            </div>
          ) : (
            <div className="divide-y divide-[#F1F5F9]">
              {records.map(r => (
                <button key={r.id} onClick={() => props.onOpen(r.id)}
                  className="w-full text-left px-4 py-2.5 hover:bg-[#F8FAFC] transition-colors">
                  <div className="flex items-center gap-1.5">
                    <StatusChip status={r.status} />
                    {r.verification === 'manual' && (
                      <Chip text="人工核验" color="#B45309" bg="#FFFBEB" border="#FDE68A" />
                    )}
                    <span className="text-[10px] font-mono text-[#64748B] truncate">
                      {r.target?.host}:{r.target?.port}
                    </span>
                    <span className="ml-auto text-[10px] text-[#94A3B8] tabular-nums shrink-0">
                      {fmtTs(r.updated_at || r.created_at)}
                    </span>
                  </div>
                  <div className="text-[11px] text-[#334155] leading-4 line-clamp-2 mt-0.5">
                    {r.alarm?.summary || 'SSL 证书调查'}
                  </div>
                  <div className="mt-0.5 text-[10px] text-[#94A3B8] flex items-center gap-2">
                    <span>{r.alarm?.source || '—'}</span>
                    {typeof r.healthy_streak === 'number' && <span>连续健康 {r.healthy_streak}/3</span>}
                    {r.nms_last_observed_at && <span>NMS 观测 {fmtTs(r.nms_last_observed_at)}</span>}
                  </div>
                </button>
              ))}
            </div>
          )
        ) : (
          <>
            <div className="px-4 py-2 border-b border-[#F1F5F9] flex items-center gap-1 flex-wrap">
              {[
                { k: '', label: '全部' },
                { k: 'pending', label: '待审核' },
                { k: 'approved', label: '已批准' },
                { k: 'returned', label: '已退回' },
                { k: 'revoked', label: '已撤回' },
              ].map(f => (
                <button key={f.k} onClick={() => setCaseFilter(f.k)}
                  className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                    caseFilter === f.k ? 'border-[#CBD5E1] bg-[#F1F5F9] text-[#0F172A]'
                      : 'border-[#E2E8F0] bg-white text-[#64748B] hover:border-[#CBD5E1]'}`}>
                  {f.label}
                </button>
              ))}
              {!isAdmin && <span className="ml-auto text-[10px] text-[#94A3B8]">仅显示我的案例</span>}
              {isAdmin && <span className="ml-auto text-[10px] text-[#94A3B8]">管理员可审核他人案例</span>}
            </div>
            {props.casesLoading && cases.length === 0 ? (
              <div className="py-10 text-center text-[11px] text-[#94A3B8]">加载中…</div>
            ) : shownCases.length === 0 ? (
              <div className="py-12 text-center px-6">
                <p className="text-[11px] text-[#64748B]">暂无案例</p>
                <p className="text-[10px] text-[#94A3B8] mt-1">调查恢复后可提交成功案例，经管理员审核后供后续调查引用</p>
              </div>
            ) : (
              <div className="divide-y divide-[#F1F5F9]">
                {shownCases.map(c => {
                  const canReview = isAdmin && c.status === 'pending' && c.owner !== me;
                  const canRevoke = (c.owner === me || isAdmin) && c.status !== 'revoked';
                  return (
                    <div key={c.id} className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <CaseStatusChip status={c.status} />
                        <span className="text-[10px] font-mono text-[#64748B] truncate">
                          {c.target?.host}:{c.target?.port}
                        </span>
                        <Chip text={`v${c.version ?? '—'}`} color="#475569" bg="#F8FAFC" border="#E2E8F0" />
                        <span className="ml-auto text-[10px] text-[#94A3B8]">{fmtTs(c.created_at)}</span>
                      </div>
                      <div className="text-[11px] text-[#334155] leading-4 mt-0.5 line-clamp-2">
                        <span className="text-[#94A3B8]">原因：</span>{c.root_cause}
                      </div>
                      <div className="text-[11px] text-[#475569] leading-4 line-clamp-2">
                        <span className="text-[#94A3B8]">操作：</span>{c.action}
                      </div>
                      <div className="mt-0.5 text-[10px] text-[#94A3B8] flex items-center gap-2 flex-wrap">
                        <span>提交人 {c.owner || '—'}</span>
                        {c.reviewed_by && <span>审核 {c.reviewed_by} · {fmtTs(c.reviewed_at)}</span>}
                        {c.review_note && <span className="truncate max-w-[180px]" title={c.review_note}>说明：{c.review_note}</span>}
                        {(canReview || canRevoke) && (
                          <button onClick={() => {
                            setOpenCase(openCase === c.id ? null : c.id);
                            setReviewNote('');
                          }}
                            className="ml-auto text-[10px] text-[#2563EB] hover:underline">
                            {openCase === c.id ? '收起' : (canReview ? '审核' : '撤回')}
                          </button>
                        )}
                      </div>
                      {openCase === c.id && (
                        <div className="mt-1.5 p-2 rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] space-y-1.5">
                          <input value={reviewNote} onChange={e => setReviewNote(e.target.value)}
                            placeholder={canReview ? '审核说明（退回必填）' : '撤回原因（必填）'}
                            className="w-full text-[11px] px-2 py-1.5 rounded-md border border-[#E2E8F0] bg-white text-[#334155] focus:outline-none focus:ring-2 focus:ring-slate-500/10 focus:border-[#CBD5E1]" />
                          <div className="flex items-center gap-1.5 flex-wrap">
                            {canReview && (
                              <>
                                <button disabled={props.acting}
                                  onClick={async () => {
                                    const ok = await props.onReview(c.id, 'approve', reviewNote);
                                    if (ok) { setOpenCase(null); setReviewNote(''); }
                                  }}
                                  className="px-2 py-1 rounded-md bg-[#15803D] text-white text-[10px] font-medium hover:bg-[#166534] disabled:opacity-40 transition-colors">
                                  批准
                                </button>
                                <button disabled={props.acting || !reviewNote.trim()}
                                  onClick={async () => {
                                    const ok = await props.onReview(c.id, 'return', reviewNote);
                                    if (ok) { setOpenCase(null); setReviewNote(''); }
                                  }}
                                  className="px-2 py-1 rounded-md bg-white border border-[#FECACA] text-[#B91C1C] text-[10px] font-medium hover:border-[#FCA5A5] disabled:opacity-40 transition-colors">
                                  退回
                                </button>
                              </>
                            )}
                            {canRevoke && (
                              <button disabled={props.acting || !reviewNote.trim()}
                                onClick={async () => {
                                  const ok = await props.onRevoke(c.id, reviewNote);
                                  if (ok) { setOpenCase(null); setReviewNote(''); }
                                }}
                                className="px-2 py-1 rounded-md bg-white border border-[#E2E8F0] text-[#64748B] text-[10px] font-medium hover:border-[#CBD5E1] disabled:opacity-40 transition-colors">
                                撤回案例
                              </button>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function NmsSslInvestigation(props: SslPanelProps) {
  const { open, token, isAdmin, target, sessionKey, onSnapshotRetry, onClose } = props;
  const [view, setView] = useState<'detail' | 'records'>('detail');
  const [inv, setInv] = useState<SslInv | null>(null);
  const [invId, setInvId] = useState('');
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<ApiFailure | null>(null);
  const [banner, setBanner] = useState<{ kind: 'error' | 'warn' | 'ok' | 'info'; text: string } | null>(null);
  const [acting, setActing] = useState(false);
  const [records, setRecords] = useState<SslListItem[]>([]);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [cases, setCases] = useState<SslCaseRef[]>([]);
  const [casesLoading, setCasesLoading] = useState(false);
  const [myCase, setMyCase] = useState<SslCaseRef | null>(null);
  const [caseNonce, setCaseNonce] = useState(0);
  const [me, setMe] = useState('');

  const detailSeq = useRef(0);
  const detailAbort = useRef<AbortController | null>(null);
  const recordsSeq = useRef(0);

  useEffect(() => {
    const kick = setTimeout(() => {
      try {
        setMe(JSON.parse(localStorage.getItem('zops_user') || '{}').username || '');
      } catch { setMe(''); }
    }, 0);
    return () => clearTimeout(kick);
  }, []);

  // 打开/切换目标：下一帧清空旧内容，避免上一轮残留（不在 effect 体内同步 setState）
  useEffect(() => {
    if (!open) return;
    const kick = setTimeout(() => {
      setBanner(null);
      setCreateError(null);
      setMyCase(null);
      if (!target) {
        setView('records');
        setInv(null);
        setInvId('');
        setCreateBusy(false);
        return;
      }
      setView('detail');
      setInv(null);
      setInvId('');
    }, 0);
    return () => clearTimeout(kick);
  }, [open, target, sessionKey]);

  // 创建（或复用）调查
  useEffect(() => {
    if (!open || !target) return;
    let cancelled = false;
    const ac = new AbortController();
    const kick = setTimeout(() => {
      if (cancelled) return;
      setCreateBusy(true);
      setCreateError(null);
      (async () => {
      try {
        const d = await request<{ investigation: SslInv; created: boolean }>(
          '/api/nms/ssl-investigations', {
            method: 'POST', token, signal: ac.signal,
            body: {
              alarm_id: target.alarmId,
              snapshot_timestamp: target.snapshotTimestamp,
              snapshot_ref: target.snapshotRef,
              incident_id: target.incidentId || '',
              idempotency_key: `ui-${sessionKey}-${target.alarmId}`,
            },
          });
        if (cancelled) return;
        setInv(d.investigation);
        setInvId(d.investigation.id);
      } catch (e) {
        if (cancelled) return;
        setCreateError(asApiError(e));
      } finally {
        if (!cancelled) setCreateBusy(false);
      }
      })();
    }, 0);
    return () => { cancelled = true; clearTimeout(kick); ac.abort(); };
  }, [open, target, sessionKey, token]);

  const loadDetail = useCallback(async (id: string, silent: boolean) => {
    const seq = ++detailSeq.current;
    detailAbort.current?.abort();
    const ac = new AbortController();
    detailAbort.current = ac;
    try {
      const d = await request<SslInv>(`/api/nms/ssl-investigations/${id}`,
        { token, signal: ac.signal });
      if (seq !== detailSeq.current) return;
      setInv(d);
      setInvId(d.id);
      setBanner(null);
    } catch (e) {
      if (seq !== detailSeq.current) return;
      const err = asApiError(e);
      if (err.code === 'aborted') return;
      if (!silent) {
        setBanner({ kind: 'error', text: `${err.message}${err.code ? `（${err.code}）` : ''}` });
      }
    }
  }, [token]);

  // 详情轮询：可见时 5 秒（暂停中 15 秒），页面隐藏跳过
  useEffect(() => {
    if (!open || view !== 'detail' || !invId) return;
    if (inv?.status === 'recovered') return;
    const interval = inv?.status === 'paused' ? 15000 : 5000;
    const timer = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void loadDetail(invId, true);
    }, interval);
    return () => clearInterval(timer);
  }, [open, view, invId, inv?.status, loadDetail]);

  useEffect(() => () => {
    detailAbort.current?.abort();
    detailSeq.current += 1;
  }, []);

  const loadRecords = useCallback(async () => {
    const seq = ++recordsSeq.current;
    setRecordsLoading(true);
    setCasesLoading(true);
    try {
      const [list, caseList] = await Promise.all([
        request<{ items: SslListItem[] }>('/api/nms/ssl-investigations?limit=50', { token }),
        request<{ items: SslCaseRef[] }>('/api/nms/ssl-cases', { token }),
      ]);
      if (seq !== recordsSeq.current) return;
      setRecords(list.items || []);
      setCases(caseList.items || []);
      setBanner(null);
    } catch (e) {
      if (seq !== recordsSeq.current) return;
      const err = asApiError(e);
      setBanner({ kind: 'error', text: `${err.message}${err.code ? `（${err.code}）` : ''}` });
    } finally {
      if (seq === recordsSeq.current) {
        setRecordsLoading(false);
        setCasesLoading(false);
      }
    }
  }, [token]);

  useEffect(() => {
    if (!open || view !== 'records') return;
    const kick = setTimeout(() => { void loadRecords(); }, 0);
    return () => clearTimeout(kick);
  }, [open, view, loadRecords]);

  // 已恢复调查的案例状态（提交/退回后可重新提交）
  useEffect(() => {
    if (!open || !inv || inv.status !== 'recovered') return;
    let cancelled = false;
    (async () => {
      try {
        const d = await request<{ items: SslCaseRef[] }>('/api/nms/ssl-cases', { token });
        if (cancelled) return;
        setMyCase((d.items || []).find(c => c.investigation_id === inv.id) || null);
      } catch { /* 案例列表失败不遮挡调查详情 */ }
    })();
    return () => { cancelled = true; };
  }, [open, token, inv, caseNonce]);

  const handleFailure = useCallback((e: unknown) => {
    const err = asApiError(e);
    if (err.status === 401) {
      setBanner({ kind: 'error', text: '登录状态已失效，请重新登录后再操作' });
      return;
    }
    setBanner({ kind: 'error', text: `${err.message}${err.code ? `（${err.code}）` : ''}` });
    // 409：重取详情，但不清空未提交表单
    if (err.status === 409 && invId) void loadDetail(invId, true);
  }, [invId, loadDetail]);

  const act = useCallback(async (path: string, body: Record<string, unknown> = {}) => {
    if (!invId) return false;
    setActing(true);
    setBanner(null);
    try {
      const d = await request<SslInv>(`/api/nms/ssl-investigations/${invId}${path}`,
        { method: 'POST', token, body });
      setInv(prev => (prev ? { ...prev, ...d } : d));
      void loadDetail(invId, true);
      return true;
    } catch (e) {
      handleFailure(e);
      return false;
    } finally {
      setActing(false);
    }
  }, [invId, token, handleFailure, loadDetail]);

  const submitCase = useCallback(async (rootCause: string, action: string, conditions: string) => {
    if (!invId) return false;
    setActing(true);
    setBanner(null);
    try {
      await request(`/api/nms/ssl-investigations/${invId}/case`, {
        method: 'POST', token, body: { root_cause: rootCause, action, conditions },
      });
      setCaseNonce(n => n + 1);
      void loadDetail(invId, true);
      setBanner({ kind: 'ok', text: '案例已提交，等待管理员审核（创建人不得自审）' });
      return true;
    } catch (e) {
      handleFailure(e);
      return false;
    } finally {
      setActing(false);
    }
  }, [invId, token, handleFailure, loadDetail]);

  const reviewCase = useCallback(async (caseId: string, action: 'approve' | 'return', note: string) => {
    setActing(true);
    setBanner(null);
    try {
      await request(`/api/nms/ssl-cases/${caseId}/review`, {
        method: 'POST', token, body: { action, note },
      });
      await loadRecords();
      if (invId) void loadDetail(invId, true);
      setBanner({ kind: 'ok', text: action === 'approve' ? '案例已批准' : '案例已退回' });
      return true;
    } catch (e) {
      handleFailure(e);
      return false;
    } finally {
      setActing(false);
    }
  }, [token, loadRecords, invId, loadDetail, handleFailure]);

  const revokeCase = useCallback(async (caseId: string, note: string) => {
    setActing(true);
    setBanner(null);
    try {
      await request(`/api/nms/ssl-cases/${caseId}/revoke`, {
        method: 'POST', token, body: { note },
      });
      await loadRecords();
      setCaseNonce(n => n + 1);
      setBanner({ kind: 'ok', text: '案例已撤回，历史版本保留' });
      return true;
    } catch (e) {
      handleFailure(e);
      return false;
    } finally {
      setActing(false);
    }
  }, [token, loadRecords, handleFailure]);

  const retrySnapshot = createError && (createError.code === 'snapshot_stale'
    || createError.code === 'snapshot_mismatch' || createError.code === 'snapshot_missing');

  return (
    <aside aria-hidden={!open} inert={!open} data-testid="ssl-panel"
      className="h-full w-full bg-white border-l border-[#E2E8F0] flex flex-col">
      {/* 头部 */}
      <div className="px-4 py-3 border-b border-[#E2E8F0] flex items-start gap-2 shrink-0">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <ShieldAlert size={14} className="text-[#0F172A] shrink-0" />
            <span className="text-[13px] font-semibold text-[#0F172A]">
              {view === 'detail' ? '证书调查' : '调查记录'}
            </span>
            {view === 'detail' && inv && <StatusChip status={inv.status} />}
          </div>
          <div className="text-[10px] text-[#94A3B8] mt-0.5 truncate">
            {view === 'detail' && inv
              ? `${inv.target?.host}:${inv.target?.port} · ${inv.alarm?.source || '—'}`
              : '告警消失后仍可查看调查与案例'}
          </div>
        </div>
        {view === 'detail' && (
          <button onClick={() => setView('records')} title="调查记录"
            className="p-1 rounded-md text-[#94A3B8] hover:text-[#334155] hover:bg-[#F1F5F9] transition-colors shrink-0">
            <BookOpen size={13} />
          </button>
        )}
        {view === 'records' && target && invId && (
          <button onClick={() => setView('detail')} title="返回调查"
            className="p-1 rounded-md text-[#94A3B8] hover:text-[#334155] hover:bg-[#F1F5F9] transition-colors shrink-0">
            <ChevronLeft size={13} />
          </button>
        )}
        <button onClick={onClose} title="关闭"
          className="p-1 rounded-md text-[#94A3B8] hover:text-[#334155] hover:bg-[#F1F5F9] transition-colors shrink-0">
          <X size={14} />
        </button>
      </div>

      {banner && (
        <div className="px-4 pt-2 shrink-0">
          <Banner kind={banner.kind}>{banner.text}</Banner>
        </div>
      )}

      {/* 内容 */}
      {view === 'records' ? (
        <RecordsView
          records={records}
          recordsLoading={recordsLoading}
          cases={cases}
          casesLoading={casesLoading}
          isAdmin={isAdmin}
          me={me}
          acting={acting}
          onRefresh={loadRecords}
          onOpen={(id) => {
            setView('detail');
            setInv(null);
            setInvId(id);
            setBanner(null);
            void loadDetail(id, false);
          }}
          onReview={reviewCase}
          onRevoke={revokeCase} />
      ) : createBusy && !inv ? (
        <div className="flex-1 px-4 py-4 space-y-3">
          {[0, 1, 2, 3].map(i => (
            <div key={i} className="animate-pulse space-y-1.5">
              <div className="h-2 w-1/3 rounded bg-[#F1F5F9]" />
              <div className="h-2 w-full rounded bg-[#F8FAFC]" />
              <div className="h-2 w-2/3 rounded bg-[#F8FAFC]" />
            </div>
          ))}
          <p className="text-[10px] text-[#94A3B8] text-center pt-1">正在创建调查并核验证书…</p>
        </div>
      ) : createError && !inv ? (
        <div className="flex-1 px-4 py-6">
          <Banner kind="error">
            {createError.message}
            {createError.code ? `（${createError.code}）` : ''}
          </Banner>
          <div className="mt-3 flex items-center gap-2">
            {retrySnapshot && onSnapshotRetry && (
              <button onClick={onSnapshotRetry}
                className="px-2.5 py-1.5 rounded-lg bg-[#0F172A] text-white text-[11px] font-medium hover:bg-[#1E293B] transition-colors">
                使用最新快照重试
              </button>
            )}
            <button onClick={onClose}
              className="px-2.5 py-1.5 rounded-lg border border-[#E2E8F0] bg-white text-[11px] text-[#475569] hover:border-[#CBD5E1] transition-colors">
              关闭
            </button>
          </div>
        </div>
      ) : inv ? (
        <DetailView
          inv={inv}
          acting={acting}
          myCase={myCase}
          onRecheck={() => { void act('/recheck'); }}
          onPause={() => { void act('/pause', { reason: 'user_paused' }); }}
          onResume={() => { void act('/resume'); }}
          onManual={(category, note) => act('/manual-completed', {
            category, note, expected_version: inv.version,
          })}
          onSubmitCase={submitCase}
          onFeedback={(helpful, note) => { void act('/feedback', { helpful, note }); }} />
      ) : (
        <div className="flex-1 py-14 text-center px-6">
          <p className="text-[11px] text-[#64748B]">未选择调查</p>
          <p className="text-[10px] text-[#94A3B8] mt-1">在 SSL 告警行点「调查」发起只读取证</p>
        </div>
      )}

      {/* 底部 */}
      <div className="px-4 py-2 border-t border-[#F1F5F9] flex items-center justify-between shrink-0">
        <span className="text-[10px] text-[#94A3B8]">只读取证 · 人工处置后自动复核</span>
        <span className="text-[10px] text-[#94A3B8]">
          {view === 'detail' && inv ? `更新于 ${fmtTs(inv.updated_at)}` : '记录永久保存'}
        </span>
      </div>
    </aside>
  );
}
