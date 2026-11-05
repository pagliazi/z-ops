'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import {
  ShieldAlert, Plus, X, FileText, Send, ClipboardList, Eye, Undo2, RotateCcw, Sparkles, Server, BellRing, Wrench, FileCheck2, Settings2, Save, Pencil, ChevronDown, History, Wand2, Box, Users, FileSearch, CheckCircle2, Clock } from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';
import { ComboPanel } from '@/components/ComboPanel';
import {
  createApproval, updateApproval, getMyApprovals, getApprovalMeta, getPendingApprovals, decideApproval,
  getApprovalCustomers, lookupCaseNo, cancelApproval, analyzeApprovalImpact, getApprovalHistory, autoFillApproval,
  getRelatedAlerts, generateApprovalCitp, matchApprovalDevice, citpExportUrl,
  getApprovalDraft, saveApprovalDraft, clearApprovalDraft, searchCitpDevices, runAcceptanceTest, confirmExecution, getApprovalStats,
  type ApprovalMeta, type CaseLookupResult, type RelatedAlert, type CitpSnapshot,
  type CitpDevice, type CitpPlanStep, type CitpCheckItem, type ApprovalHistoryEvent, type AcceptanceResult,
} from '@/lib/api';

// ═══ 审批中心 (统一入口) ═══
// 我的申请 + 审批管理 同页 tab 切换; 左列表右详情; 新建按钮展开表单。
// 语义: destructive 一律被拦截 (系统绝不自动执行), 批准/拒绝仅为流程留痕,
//       实际执行由运维人员按变更流程线下完成。

// 操作类型参数建议 (选项清单来自 meta.operation_groups 按平台分组; 此处仅参数提示)
const OP_HINTS: Record<string, string> = {
  vm_power_off: 'vm_name, vm_id', vm_power_on: 'vm_name, vm_id',
  vm_power_reboot: 'vm_name, vm_id', vm_shutdown: 'vm_name, vm_id',
  vm_remove: 'vm_name, vm_id, reason', vm_migrate: 'vm_name, target_host',
  vm_resource_resize: 'vm_name, cpu, memory_mb', vm_disk_expand: 'vm_name, disk, new_size_gb',
  vm_snapshot_create: 'vm_name, snapshot_name', vm_snapshot_delete: 'snapshot_name, storage',
  vm_snapshot_revert: 'vm_name, snapshot_name',
  backup_job_run: 'job_name, target', backup_job_stop: 'job_name',
  backup_restore: 'backup_id, target, point_in_time', backup_repo_cleanup: 'repo, retention',
  storage_volume_expand: 'volume, new_size_gb', storage_volume_offline: 'volume, reason',
  storage_snapshot_clone: 'volume, snapshot, clone_name',
  dns_record_change: 'domain, record_type, value', firewall_rule_change: 'policy, src, dst, action',
  network_route_change: 'network, gateway', loadbalancer_change: 'vip, pool, member',
  ip_address_change: 'host, old_ip, new_ip',
  monitor_maintenance_window: 'scope, start, end', monitor_threshold_change: 'metric, old, new',
  db_restart: 'instance, mode', db_failover_switch: 'instance, target_node',
  db_backup_manual: 'instance, type', db_restore: 'instance, backup_id, point_in_time',
  db_schema_change: 'instance, ddl_summary',
  cert_renew: 'domain, cert_id', account_permission_change: 'account, change_detail',
  security_scan: 'target, scan_type',
  service_restart: 'service, host', batch_exec: 'command, hosts', emergency_reboot: 'hosts, reason',
  // 硬件维护 (CITP 联动; 目标设备主要填「目标主机/服务器」字段)
  hw_nic_replace: 'target_host, 故障端口', hw_bios_battery_replace: 'target_host',
  hw_disk_replace: 'target_host, slot(槽位)', hw_memory_replace: 'target_host, slot(DIMM槽位)',
  hw_psu_replace: 'target_host, slot(电源槽位)', hw_fan_replace: 'target_host, slot(风扇槽位)',
  hw_motherboard_replace: 'target_host', hw_sfp_replace: 'target_host, vmnic(端口), 对端设备/端口',
  hw_cable_replace: 'target_host, port(端口)',
  hw_reseat: 'target_host, part(部件), slot(槽位/端口)',
};
const CUSTOM_TYPE = '__custom__';

// NMS 告警级别徽标色 (联动卡片/详情共用)
const SEV_CLS: Record<string, string> = {
  critical: 'bg-red-100 text-red-700', major: 'bg-orange-100 text-orange-700',
  minor: 'bg-amber-100 text-amber-700', warning: 'bg-yellow-50 text-yellow-700',
  indeterminate: 'bg-gray-100 text-gray-600',
};

const STATUS_META: Record<string, { label: string; cls: string; dot: string }> = {
  pending: { label: '待决策', cls: 'bg-amber-50 text-amber-700', dot: 'bg-amber-500' },
  approved: { label: '已批准', cls: 'bg-emerald-50 text-emerald-700', dot: 'bg-emerald-500' },
  rejected: { label: '已拒绝', cls: 'bg-red-50 text-red-700', dot: 'bg-red-500' },
  cancelled: { label: '已撤销', cls: 'bg-gray-100 text-gray-500', dot: 'bg-gray-400' },
  timeout: { label: '已超时', cls: 'bg-gray-100 text-gray-500', dot: 'bg-gray-400' },
  executed: { label: '已执行', cls: 'bg-emerald-50 text-emerald-700', dot: 'bg-emerald-500' },
};

// ApplyForm 预填 (回退申请入口: 自动带出原单业务字段并关联)
interface ApplyPrefill {
  tool_name?: string;
  summary?: string;
  site?: string;
  customer?: string;
  customer_gid?: string;
  case_no?: string;
  original_approval_id?: string;
}

const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  urgent: { label: '紧急', cls: 'bg-red-50 text-red-600' },
  high: { label: '高', cls: 'bg-orange-50 text-orange-600' },
  medium: { label: '中', cls: 'bg-amber-50 text-amber-700' },
  low: { label: '低', cls: 'bg-gray-100 text-gray-500' },
};

// 三级操作目录类型: 产品类型 → 服务类型 → 操作类型
type OpCatalogEntry = { product: string; services: { service: string; ops: { value: string; label: string }[] }[] };
type OpCatalogMap = Record<string, OpCatalogEntry[]>;

// 审批分类: 日常配置审批 / 维护审批 (维护单需 CITP 自动生成)
const CATEGORY_META: Record<string, { label: string; cls: string }> = {
  daily: { label: '日常配置', cls: 'bg-sky-50 text-sky-700 border-sky-100' },
  maintenance: { label: '维护', cls: 'bg-amber-50 text-amber-700 border-amber-200' },
};
const catLabel = (c?: string) => CATEGORY_META[c || 'daily']?.label || '日常配置';

const fmtTime = (ts?: string | number) => {
  const t = parseFloat(String(ts || 0));
  if (!t) return '-';
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const fmtPlanned = (ts?: string) => (ts ? String(ts).replace('T', ' ') : '-');

const inputCls = 'w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm text-[#111827] bg-white focus:outline-none focus:border-[#E11D48]';
const labelCls = 'text-xs text-[#6B7280] mb-1.5 block';

// ═══ 申请模板 ═══
interface ApprovalTemplate {
  id: string;
  name: string;
  tool_name: string;
  service_type: string;
  priority: string;
  params: { k: string; v: string }[];
  summary: string;
  customer: string;
  approvers: string;
  risk_note: string;
  impact_scope: string;
  rollback_plan: string;
  change_window: string;
}

// 预置模板 (常用变更场景, 一键填充表单)
const PRESET_TEMPLATES: ApprovalTemplate[] = [
  // ── 硬件维护 (按真实 CITP 模板类型配置; 选中自动切维护单并联动 CITP 故障类型) ──
  {
    id: 'preset_hw_disk', name: '硬盘更换', tool_name: 'hw_disk_replace',
    service_type: '硬件/服务器/存储设备', priority: 'high',
    params: [{ k: 'slot', v: '' }],
    summary: '故障 HDD 磁盘更换，槽位与告警定位一致，核对 SN 后更换',
    customer: '',
    approvers: '',
    risk_note: '更换期间集群/阵列冗余降低；新盘无法识别则收集日志交厂商二线',
    impact_scope: '单盘冗余消化，业务不中断',
    rollback_plan: '新盘无法识别暂停维护，收集日志交厂商二线分析并安排换模块',
    change_window: '',
  },
  {
    id: 'preset_hw_nic', name: '板载网卡更换', tool_name: 'hw_nic_replace',
    service_type: '硬件/服务器/存储设备', priority: 'high',
    params: [{ k: 'slot', v: '' }],
    summary: '主机故障网卡更换，核对设备标签与 SN 后关机更换',
    customer: '',
    approvers: '',
    risk_note: '需关机更换；更换后需验证 bond/网络无丢包',
    impact_scope: '主机临时下线，集群冗余消化',
    rollback_plan: '异常时工程师当场升级二线远程协助',
    change_window: '',
  },
  {
    id: 'preset_hw_sfp', name: '光模块更换', tool_name: 'hw_sfp_replace',
    service_type: '硬件/服务器/存储设备', priority: 'high',
    params: [{ k: 'vmnic', v: '' }],
    summary: '主机端光模块(SFP)故障更换，GNOC 配合交换机端口关闭/开启',
    customer: '',
    approvers: '',
    risk_note: '告警未恢复需检查/更换楼内尾纤',
    impact_scope: '单链路切换，冗余链路承载',
    rollback_plan: '告警未恢复则更换楼内尾纤或升级 GNOC 处理',
    change_window: '',
  },
  {
    id: 'preset_hw_mem', name: '内存更换', tool_name: 'hw_memory_replace',
    service_type: '硬件/服务器/存储设备', priority: 'high',
    params: [{ k: 'slot', v: '' }],
    summary: '故障内存(DIMM)更换，断电重启修复，记录故障槽位',
    customer: '',
    approvers: '',
    risk_note: '需关机断电更换；BMC 定位故障 DIMM 槽位',
    impact_scope: '主机临时下线',
    rollback_plan: '异常时工程师当场升级二线远程协助',
    change_window: '',
  },
  {
    id: 'preset_hw_mb', name: '主板更换', tool_name: 'hw_motherboard_replace',
    service_type: '硬件/服务器/存储设备', priority: 'urgent',
    params: [],
    summary: '主板/主板模块故障更换，记录接线与 BIOS 配置后断电抽出更换',
    customer: '',
    approvers: '',
    risk_note: '更换后需恢复 BIOS/RAID/BMC 配置；检查同机柜其他设备防误触',
    impact_scope: '主机停机窗口较长',
    rollback_plan: '配置不符对照记录修正；观察 15 分钟无告警后离场',
    change_window: '',
  },
  {
    id: 'preset_hw_bios', name: 'BIOS 电池更换', tool_name: 'hw_bios_battery_replace',
    service_type: '硬件/服务器/存储设备', priority: 'medium',
    params: [],
    summary: '主板 BIOS 电池(CR2032)更换，核对 SN 后静电操作更换',
    customer: '',
    approvers: '',
    risk_note: '更换后确认 BIOS 配置(启动顺序/RAID)未丢失、系统时间正常',
    impact_scope: '主机临时下线',
    rollback_plan: '异常时工程师当场升级二线远程协助',
    change_window: '',
  },
  // ── 日常配置 (真实高频场景) ──
  {
    id: 'preset_vm_remove', name: '客户 VM 移除', tool_name: 'vm_remove',
    service_type: 'SCC/主机', priority: 'high',
    params: [{ k: 'vm_name', v: '' }, { k: 'vm_id', v: '' }],
    summary: '客户请求移除这两个vm，已经提供SCCRF，检查已经关机，请审批',
    customer: '',
    approvers: '',
    risk_note: '移除后 VM 不可恢复，请确认客户已提供 SCCRF 且 VM 已关机',
    impact_scope: '客户名下该 2 台 VM',
    rollback_plan: '移除前保留备份/镜像；客户确认后执行',
    change_window: '',
  },
];

// 我的模板 (localStorage 持久化, 各用户独立)
const MY_TPL_KEY = 'zops_approval_templates';
const loadMyTemplates = (): ApprovalTemplate[] => {
  try { return JSON.parse(localStorage.getItem(MY_TPL_KEY) || '[]'); } catch { return []; }
};
const saveMyTemplates = (tpls: ApprovalTemplate[]) => {
  try { localStorage.setItem(MY_TPL_KEY, JSON.stringify(tpls)); } catch { /* ignore */ }
};

// ═══ 申请草稿 (服务端存储: 每用户一份, 跨设备; 打开拉取恢复, 防抖自动存, 提交后清除) ═══
type DraftData = Record<string, any>;

function Toast({ toast }: { toast: { ok: boolean; text: string } | null }) {
  if (!toast) return null;
  return (
    <div className={`text-sm rounded-lg px-4 py-2.5 ${toast.ok ? 'bg-emerald-50 text-emerald-700 border border-emerald-100' : 'bg-red-50 text-red-700 border border-red-100'}`}>
      {toast.text}
    </div>
  );
}

// 表单分区卡片 (弹窗内逻辑分组: 申请内容/关联对象/计划与审批流/影响分析等)
function FormSection({ icon: Icon, title, hint, action, tone, step, children }: {
  icon: React.ElementType; title: string; hint?: string; action?: React.ReactNode;
  tone?: 'amber' | 'default'; step?: number; children: React.ReactNode;
}) {
  const amber = tone === 'amber';
  return (
    <div className={`border rounded-xl overflow-hidden ${amber ? 'border-amber-200 bg-amber-50/20' : 'border-[#E5E7EB] bg-white'}`}>
      <div className={`flex items-center gap-2 px-4 py-2.5 border-b ${amber ? 'bg-amber-50 border-amber-200' : 'bg-gray-50/70 border-[#F3F4F6]'}`}>
        {step !== undefined && (
          <span className={`w-[18px] h-[18px] rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 ${
            amber ? 'bg-amber-500 text-white' : 'bg-[#111827] text-white'}`}>{step}</span>
        )}
        <Icon size={13} className={amber ? 'text-amber-600' : 'text-[#6B7280]'} />
        <span className="text-xs font-semibold text-[#374151]">{title}</span>
        {hint && <span className="text-[11px] text-[#9CA3AF] truncate hidden sm:inline">{hint}</span>}
        {action && <span className="ml-auto shrink-0">{action}</span>}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

// ═══ 发起/编辑申请 (弹窗模式: 模板 + 双单据表单; editRecord 传入即编辑模式) ═══
function ApplyForm({ onSubmitted, onClose, prefill, editRecord }: {
  onSubmitted: (approvalId?: string) => void;
  onClose: () => void;
  prefill?: ApplyPrefill | null;
  editRecord?: any | null;   // 编辑模式: 待审批单原始记录 (预填全字段, PUT 提交)
}) {
  const [summary, setSummary] = useState(prefill?.summary || '');
  const [toolName, setToolName] = useState(prefill?.tool_name || '');  // 默认「请选择」
  const [customType, setCustomType] = useState('');
  const [customer, setCustomer] = useState(prefill?.customer || '');
  const [customerGid, setCustomerGid] = useState(prefill?.customer_gid || '');  // OPS-CORE CRM 联动带出
  const [custList, setCustList] = useState<{ gid: string; name: string; site_ids?: string[]; order_no?: string }[]>([]);
  const [customerOrder, setCustomerOrder] = useState('');  // OPS-CORE-MGT CRM 订单号 (选客户自动带出)
  const [customerSiteIds, setCustomerSiteIds] = useState('');  // OPS-CORE-MGT CRM 资源池 Site ID (选中客户自动带出)
  const [custOpen, setCustOpen] = useState(false);
  const [approvers, setApprovers] = useState('');
  // 站点 combobox: 输入匹配 OPS-CORE 结构化项 (label=机房中文名, value=城市三字码)
  const [site, setSite] = useState(prefill?.site || '');
  const [siteText, setSiteText] = useState(prefill?.site || '');
  const [siteOpen, setSiteOpen] = useState(false);
  const siteBoxRef = useRef<HTMLDivElement>(null);
  const custBoxRef = useRef<HTMLDivElement>(null);
  // 分级审批流: DS 配置复核 → 技术审批 → 行政审批 (逐级解锁; 空则跳过该级)
  const [approverDs, setApproverDs] = useState('');
  const [approverTech, setApproverTech] = useState('');
  const [approverAdmin, setApproverAdmin] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [serviceType, setServiceType] = useState('');
  const [productType, setProductType] = useState('');  // 三级层级第一级: 产品类型 (SCC/VONE/SF/硬件/平台服务)
  // 目标主机/服务器 (逗号分隔) + NMS 告警联动 (勾选关联活动告警)
  const [targetHost, setTargetHost] = useState('');
  const [relAlerts, setRelAlerts] = useState<RelatedAlert[]>([]);
  const [relLoading, setRelLoading] = useState(false);
  const [relNote, setRelNote] = useState('');
  const [selAlerts, setSelAlerts] = useState<Set<string>>(new Set());
  const selAlertsRef = useRef<Set<string>>(new Set());   // 最新勾选 (供防抖定时器闭包读取, 避免旧值覆盖恢复项)
  useEffect(() => { selAlertsRef.current = selAlerts; }, [selAlerts]);
  const [caseNo, setCaseNo] = useState(prefill?.case_no || '');
  const [caseCheck, setCaseCheck] = useState<{ state: 'idle' | 'checking' | 'ok' | 'miss' | 'unavail'; text: string; hit?: CaseLookupResult['cases'][0] }>({ state: 'idle', text: '' });
  const [priority, setPriority] = useState('medium');
  // 审批分类 (daily=日常配置 / maintenance=维护); 选操作类型按建议表联动, 人工改过后不再联动
  const [category, setCategory] = useState('daily');
  const [catTouched, setCatTouched] = useState(false);
  // CITP (维护审批, 按真实 CITP 附件格式): 故障类型模板 + 设备手选匹配 + 时间窗手选
  const [citp, setCitp] = useState<CitpSnapshot | null>(null);
  const [citpBusy, setCitpBusy] = useState(false);
  const [faultType, setFaultType] = useState('');   // 故障类型: 强制手选, 不预设默认值
  const [citpTarget, setCitpTarget] = useState('');
  const [citpMatch, setCitpMatch] = useState<{ device: CitpDevice; customers: { name: string; gid?: string; vms?: number }[]; vms_count: number } | null>(null);
  const [matchBusy, setMatchBusy] = useState(false);
  const [devCands, setDevCands] = useState<{ name: string; kind: string; vendor: string; model: string; ip: string }[]>([]);
  const [devOpen, setDevOpen] = useState(false);
  const [devNote, setDevNote] = useState('');
  const devBoxRef = useRef<HTMLDivElement>(null);
  const [matchNote, setMatchNote] = useState('');
  const [citpWStart, setCitpWStart] = useState('');
  const [citpWEnd, setCitpWEnd] = useState('');
  const [citpTz, setCitpTz] = useState('UTC+8');
  const [citpPlatform, setCitpPlatform] = useState('');   // 平台检查单: ''=自动识别, 可手动纠正 (CML 等特殊存储)
  const [plannedTs, setPlannedTs] = useState('');
  const [changeWindow, setChangeWindow] = useState('');
  const [cwStart, setCwStart] = useState('');
  const [cwEnd, setCwEnd] = useState('');
  const [meta, setMeta] = useState<ApprovalMeta>({ apply_types: [], service_types: [], sites: [], approvers: [] });
  const [kvRows, setKvRows] = useState<{ k: string; v: string }[]>([{ k: '', v: '' }]);
  const [impactScope, setImpactScope] = useState('');
  const [rollbackPlan, setRollbackPlan] = useState('');
  const [riskNote, setRiskNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [myTpls, setMyTpls] = useState<ApprovalTemplate[]>([]);

  useEffect(() => { setMyTpls(loadMyTemplates()); }, []);
  useEffect(() => { getApprovalMeta().then(setMeta).catch(() => {}); }, []);
  // 弹窗: Esc 关闭 + 打开期间锁定背景滚动
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  // ═══ 草稿: 打开时从服务端拉取恢复 (回退申请预填优先, 不恢复) ═══
  const [restoredAt, setRestoredAt] = useState(0);
  const [draftSavedAt, setDraftSavedAt] = useState(0);  // 手动保存时间戳 (按钮反馈)
  const hydratedRef = useRef(false);  // 恢复尝试完成前不触发自动保存 (避免覆盖服务端草稿)
  const applyDraft = (d: DraftData) => {
    try {
      if (typeof d.category === 'string') { setCategory(d.category); setCatTouched(!!d.catTouched); }
      if (typeof d.summary === 'string') setSummary(d.summary);
      if (typeof d.toolName === 'string') setToolName(d.toolName);
      if (typeof d.customType === 'string') setCustomType(d.customType);
      if (typeof d.customer === 'string') setCustomer(d.customer);
      if (typeof d.customerGid === 'string') setCustomerGid(d.customerGid);
      if (typeof d.customerSiteIds === 'string') setCustomerSiteIds(d.customerSiteIds);
      if (typeof d.customerOrder === 'string') setCustomerOrder(d.customerOrder);
      if (typeof d.approvers === 'string') setApprovers(d.approvers);
      if (typeof d.approverDs === 'string') setApproverDs(d.approverDs);
      if (typeof d.approverTech === 'string') setApproverTech(d.approverTech);
      if (typeof d.approverAdmin === 'string') setApproverAdmin(d.approverAdmin);
      if (typeof d.site === 'string') { setSite(d.site); setSiteText(d.siteText || d.site); }
      if (typeof d.serviceType === 'string') setServiceType(d.serviceType);
      if (typeof d.targetHost === 'string') setTargetHost(d.targetHost);
      if (typeof d.caseNo === 'string') setCaseNo(d.caseNo);
      if (typeof d.priority === 'string') setPriority(d.priority);
      if (typeof d.plannedTs === 'string') setPlannedTs(d.plannedTs);
      if (typeof d.cwStart === 'string') setCwStart(d.cwStart);
      if (typeof d.cwEnd === 'string') setCwEnd(d.cwEnd);
      if (Array.isArray(d.kvRows)) setKvRows(d.kvRows.length ? d.kvRows : [{ k: '', v: '' }]);
      if (typeof d.impactScope === 'string') setImpactScope(d.impactScope);
      if (typeof d.rollbackPlan === 'string') setRollbackPlan(d.rollbackPlan);
      if (typeof d.riskNote === 'string') setRiskNote(d.riskNote);
      if (typeof d.faultType === 'string') setFaultType(d.faultType);
      if (typeof d.citpTarget === 'string') setCitpTarget(d.citpTarget);
      if (d.citp && typeof d.citp === 'object') setCitp(d.citp);
      if (d.citpMatch && typeof d.citpMatch === 'object') setCitpMatch(d.citpMatch);
      if (typeof d.citpWStart === 'string') setCitpWStart(d.citpWStart);
      if (typeof d.citpWEnd === 'string') setCitpWEnd(d.citpWEnd);
      if (typeof d.citpTz === 'string') setCitpTz(d.citpTz);
      if (typeof d.citpPlatform === 'string') setCitpPlatform(d.citpPlatform);
      setRestoredAt(d.savedAt || Date.now());
    } catch { /* 草稿损坏则忽略 */ }
  };
  useEffect(() => {
    if (prefill?.original_approval_id || editRecord) { hydratedRef.current = true; return; }
    let alive = true;
    getApprovalDraft()
      .then(d => { if (alive && d.draft && typeof d.draft === 'object') applyDraft(d.draft as DraftData); })
      .catch(() => {})
      .finally(() => { if (alive) setTimeout(() => { hydratedRef.current = true; }, 50); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ═══ 编辑模式: 预填待审批单全字段 (params/CITP/变更窗口 反解析回表单) ═══
  useEffect(() => {
    if (!editRecord) return;
    try {
      const rec = editRecord;
      setCategory(rec.category === 'maintenance' ? 'maintenance' : 'daily');
      setCatTouched(true);
      setSummary(rec.summary || '');
      const registered = (meta.operation_groups || []).some(g => g.ops.some(o => o.value === rec.tool_name));
      setToolName(rec.tool_name || '');
      setCustomType(registered ? '' : (rec.tool_name || ''));
      if (rec.tool_name && !registered) setToolName(CUSTOM_TYPE);
      setCustomer(rec.customer || ''); setCustomerGid(rec.customer_gid || '');
      setCustomerSiteIds(rec.customer_site_ids || '');
      setCustomerOrder(rec.customer_order_no || '');
      setChangeWindow(rec.change_window || '');   // 窗口文本回退 (cwStart/End 解析失败时提交原文本, 防丢失)
      // 已关联告警恢复: 原单勾选快照 → 列表 + 全选 (提交时原样带回)
      try {
        const relSaved = JSON.parse(rec.related_alerts || '[]');
        if (Array.isArray(relSaved) && relSaved.length) {
          setRelAlerts(relSaved.map((x: any) => ({ ...x, matched: x.matched || ['单据'] })));
          setSelAlerts(new Set(relSaved.map((x: any) => x.alarm_id).filter(Boolean)));
        }
      } catch { /* ignore */ }
      setApprovers(rec.approvers || '');
      setApproverDs(rec.approver_ds || ''); setApproverTech(rec.approver_tech || '');
      setApproverAdmin(rec.approver_admin || '');
      setSite(rec.site || ''); setSiteText(rec.site || '');
      setServiceType(rec.service_type || '');
      setTargetHost(rec.target_host || '');
      setCaseNo(rec.case_no || '');
      setPriority(rec.priority || 'medium');
      setPlannedTs((rec.planned_ts || '').replace(' ', 'T').slice(0, 16));
      setImpactScope(rec.impact_scope || ''); setRollbackPlan(rec.rollback_plan || '');
      setRiskNote(rec.risk_note || '');
      try { const p = JSON.parse(rec.params || '{}'); setKvRows(Object.keys(p).length ? Object.entries(p).map(([k, v]) => ({ k, v: String(v) })) : [{ k: '', v: '' }]); } catch { /* ignore */ }
      // 变更窗口文本反解析: "2026-09-25 02:00 ~ 04:00" / "2026-09-25 02:00 ~ 2026-09-26 04:00"
      const w = rec.change_window || '';
      const wm = w.match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s*~\s*(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{2}:\d{2})?/);
      if (wm) {
        setCwStart(`${wm[1]}T${wm[2]}`);
        const endDate = wm[3] || wm[1];
        const endTime = wm[4] || wm[2];
        if (endTime) setCwEnd(`${endDate}T${endTime}`);
      }
      // CITP 快照反解析 (维护单)
      if (rec.citp) {
        try {
          const citp2 = JSON.parse(rec.citp);
          setCitp(citp2);
          if (citp2.fault_type_key) setFaultType(citp2.fault_type_key);
          if (citp2.device) {
            setCitpTarget(citp2.device.target || '');
            setCitpMatch({ device: citp2.device, customers: citp2.customers || [], vms_count: citp2.vms_count || 0 });
            setMatchNote(citp2.device.matched ? '已匹配 (单据原快照)' : `未匹配: ${citp2.device.reason || '未匹配'}`);
          }
          const cwin = citp2.window || '';
          const cm = cwin.match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?:-(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{2}:\d{2}))?/);
          if (cm) {
            setCitpWStart(`${cm[1]}T${cm[2]}`);
            if (cm[4]) setCitpWEnd(`${cm[3] || cm[1]}T${cm[4]}`);
          }
          const tzm = cwin.match(/(UTC[+-]\d+|HKT|UTC)$/);
          if (tzm) setCitpTz(tzm[1]);
          if (citp2.platform) setCitpPlatform(citp2.platform);
        } catch { /* ignore */ }
      }
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editRecord]);

  // 草稿内容收集 + 自动暂存 (1.2s 防抖; 服务端存储, 有实质内容才写)
  const collectDraft = (): DraftData => ({
    category, catTouched, summary, toolName, customType, customer, customerGid,
    approvers, approverDs, approverTech, approverAdmin, site, siteText, serviceType,
    targetHost, caseNo, priority, plannedTs, cwStart, cwEnd,
    kvRows, impactScope, rollbackPlan, riskNote,
    faultType, citpTarget, citp, citpMatch, citpWStart, citpWEnd, citpTz, citpPlatform,
    savedAt: Date.now(),
  });
  const hasDraftContent = () => Boolean(
    summary.trim() || toolName || customType.trim() || citpTarget.trim() || caseNo.trim()
    || targetHost.trim() || customer.trim() || site.trim() || approverDs
    || kvRows.some(r => r.k.trim() || r.v.trim()));
  useEffect(() => {
    if (!hydratedRef.current || editRecord || !hasDraftContent()) return;
    const t = setTimeout(() => { saveApprovalDraft(collectDraft()).catch(() => {}); }, 1200);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, catTouched, summary, toolName, customType, customer, customerGid,
      approvers, approverDs, approverTech, approverAdmin, site, siteText, serviceType,
      targetHost, caseNo, priority, plannedTs, cwStart, cwEnd, kvRows,
      impactScope, rollbackPlan, riskNote, faultType, citpTarget, citp, citpMatch,
      citpWStart, citpWEnd, citpTz, citpPlatform]);

  // 三层层级联动: 产品类型 ▸ 服务类型 ▸ 操作类型 (按审批分类目录)
  const byCat = (meta as any).operation_groups_by_category as OpCatalogMap | undefined;
  const OP_PRODUCTS: OpCatalogEntry[] = (byCat?.[category]?.length ? byCat[category] : []);
  const curProduct = OP_PRODUCTS.find(p => p.product === productType);
  const allOps = OP_PRODUCTS.flatMap(p => p.services.flatMap(sv => sv.ops));
  const opsForStype = serviceType
    ? ((curProduct?.services || []).find(sv => sv.service === serviceType)?.ops || [])
    : allOps;
  // 选中操作 → 自动推导产品/服务归属 (用户只选操作, 不逐级选)
  const onOpChange = (v: string) => {
    setToolName(v);
    for (const p of OP_PRODUCTS) {
      for (const sv of p.services) {
        if (sv.ops.some(o => o.value === v)) { setProductType(p.product); setServiceType(sv.service); return; }
      }
    }
    if (v === CUSTOM_TYPE) { setProductType(''); setServiceType(''); }
  };

  // 其他参数区: 默认折叠 (可选项, 不打扰主流程)
  const [paramsOpen, setParamsOpen] = useState(false);
  const params_toggle = (
    <button type="button" onClick={() => setParamsOpen(v => !v)}
      className="flex items-center gap-1 text-[11px] text-[#6B7280] hover:text-[#111827] font-medium">
      <ChevronDown size={13} className={`transition-transform ${paramsOpen ? '' : '-rotate-90'}`} />
      {paramsOpen ? '收起' : '展开'}
    </button>
  );
  // 平台/分类切换: 当前操作类型不属于新组则清空重选 (联动一致性)
  useEffect(() => {
    if (serviceType && toolName && toolName !== CUSTOM_TYPE &&
        !opsForStype.some(o => o.value === toolName)) {
      setToolName('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceType, category]);

  // 操作类型 → 审批分类联动 (按建议表预选; 用户手动改过分类/编辑模式不再联动 —
  // 编辑时本 effect 闭包里 toolName/catTouched 还是旧值, 会把编辑初始化的 category 覆盖回 daily)
  useEffect(() => {
    if (catTouched || editRecord) return;
    const finalType = toolName === CUSTOM_TYPE ? customType.trim() : toolName;
    const hint = finalType ? (meta.category_hints?.[finalType] || 'daily') : 'daily';
    setCategory(hint);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolName, customType, meta.category_hints]);

  // ═══ 申请内容自动分析 → 空字段自动填充 (规则 + AI; 不覆盖已填字段) ═══
  const [autofilling, setAutofilling] = useState(false);
  const appliedRef = useRef('');   // 已分析过的事由文本 (避免重复触发)
  const runAutofill = async (manual = false) => {
    const text = summary.trim();
    if (text.length < 6) { if (manual) setToast({ ok: false, text: '请先填写申请内容 (至少 6 字) 再分析' }); return; }
    setAutofilling(true);
    try {
      const d = await autoFillApproval({ summary: text, category });
      const fills = d.fills || {};
      const applied: string[] = [];
      // 只填空字段 (用户已填/已选的不覆盖)
      if (fills.case_no && !caseNo.trim()) { setCaseNo(fills.case_no); applied.push('Case 号'); }
      if (fills.target_host && !targetHost.trim()) { setTargetHost(fills.target_host); applied.push('目标设备'); }
      if (fills.site && !site.trim()) {
        const it = (meta.site_items || []).find(x => x.value === fills.site);
        setSite(fills.site); setSiteText(it ? it.label : fills.site); applied.push('站点');
      }
      if (fills.customer && !customer.trim()) { setCustomer(fills.customer); applied.push('客户'); }
      if (fills.product && !productType) {
        setProductType(fills.product);
        if (fills.service) { setServiceType(fills.service); applied.push(`产品/服务 (${fills.product}/${fills.service})`); }
        else applied.push('产品类型');
      }
      if (fills.tool_name && !toolName) { setToolName(fills.tool_name); applied.push('操作类型'); }
      if (manual || applied.length) {
        setToast(applied.length
          ? { ok: true, text: `已根据申请内容自动填充: ${applied.join('、')}` }
          : { ok: true, text: '分析完成, 无新增可填充字段 (已填字段不覆盖)' });
      }
    } catch { if (manual) setToast({ ok: false, text: '自动分析失败, 请手动填写' }); }
    setAutofilling(false);
  };
  // 事由输入停止 1.5s 后自动分析 (同一段文本只触发一次; 编辑模式不自动)
  useEffect(() => {
    if (editRecord) return;
    const text = summary.trim();
    if (text.length < 8 || appliedRef.current === text) return;
    const t = setTimeout(() => { appliedRef.current = text; runAutofill(false); }, 1500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary]);

  // 硬件维护操作 (hw_*) → CITP 故障类型自动联动 (hw_disk_replace → disk_replace)
  useEffect(() => {
    if (toolName.startsWith('hw_')) {
      const ftKey = toolName.slice(3);
      if ((meta.citp_fault_types || []).some(t => t.value === ftKey)) setFaultType(ftKey);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolName, meta.citp_fault_types]);

  // NMS 告警联动: 站点/客户/case/主机任一填写后 800ms 防抖自动查询活动告警 (可勾选关联)
  useEffect(() => {
    const sv = site.trim(), cv = customer.trim(), nv = caseNo.trim(), hv = targetHost.trim();
    if (!sv && !cv && !nv && !hv) { setRelAlerts([]); setRelNote(''); return; }
    const t = setTimeout(async () => {
      setRelLoading(true);
      try {
        const d = await getRelatedAlerts({ site: sv, customer: cv, case_no: nv, host: hv });
        // 合并: 已勾选的告警 (含原单恢复项) 始终保留, 与新查询结果按 alarm_id 去重; 不清空已选
        setRelAlerts(prevList => {
          const kept = prevList.filter(a => selAlertsRef.current.has(a.alarm_id));
          const fresh = (d.alerts || []).filter(a => !prevList.some(k => k.alarm_id === a.alarm_id));
          return [...kept, ...fresh];
        });
        setRelNote(d.available
          ? ((d.alerts || []).length
              ? `匹配 ${d.matched ?? (d.alerts || []).length} 条 (当前活动告警共 ${d.total_active ?? 0} 条)`
              : '无匹配活动告警')
          : (d.note || 'NMS 暂不可用'));
      } catch { setRelNote('查询失败, 可继续提交'); }
      setRelLoading(false);
    }, 800);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site, customer, caseNo, targetHost]);
  // OPS-CORE CRM 客户字典异步加载 (独立接口, 首次数秒; 加载完成前可手输兜底)
  useEffect(() => { getApprovalCustomers().then(d => setCustList(d.customers || [])).catch(() => {}); }, []);

  const reset = () => {
    setSummary(''); setCustomType(''); setCustomer(''); setCustomerGid(''); setCustomerSiteIds(''); setCustomerOrder(''); setApprovers('');
    setSite(''); setSiteText(''); setSiteOpen(false);
    setApproverDs(''); setApproverTech(''); setApproverAdmin('');
    setServiceType(''); setCaseNo(''); setCaseCheck({ state: 'idle', text: '' });
    setTargetHost(''); setRelAlerts([]); setSelAlerts(new Set()); setRelNote('');
    setPriority('medium'); setPlannedTs(''); setChangeWindow(''); setCwStart(''); setCwEnd('');
    setKvRows([{ k: '', v: '' }]); setImpactScope(''); setRollbackPlan(''); setRiskNote('');
    setCategory('daily'); setCatTouched(false); setCitp(null);
    setFaultType(''); setCitpTarget(''); setCitpMatch(null); setMatchNote('');
    setCitpWStart(''); setCitpWEnd(''); setCitpTz('UTC+8'); setCitpPlatform('');
    setRestoredAt(0); setDraftSavedAt(0);
  };

  // 应用模板: 填充表单字段; 模板操作类型归属哪张单 (hint) 就自动切到哪张
  const applyTemplate = (tpl: ApprovalTemplate) => {
    if ((meta.operation_groups || []).some(g => g.ops.some(o => o.value === tpl.tool_name))) {
      setToolName(tpl.tool_name); setCustomType('');
    } else {
      setToolName(CUSTOM_TYPE); setCustomType(tpl.tool_name);
    }
    const hint = (meta.category_hints?.[tpl.tool_name] || 'daily');
    setCategory(hint);
    setCatTouched(true);
    setSummary(tpl.summary);
    setCustomer(tpl.customer);
    setApprovers(tpl.approvers);
    // 三级层级定位: 包含此操作的产品/服务 (保证操作类型下拉能命中模板值)
    const tProds = (((meta as any).operation_groups_by_category as OpCatalogMap | undefined)?.[hint] || []);
    const tSvc = tProds.flatMap(p => (p.services || []).map(sv => ({ product: p.product, service: sv.service, ops: sv.ops })))
      .find(x => x.ops.some(o => o.value === tpl.tool_name));
    if (tSvc) { setProductType(tSvc.product); setServiceType(tSvc.service); }
    else if (tpl.service_type) setServiceType(tpl.service_type);
    setPriority(tpl.priority);
    setKvRows(tpl.params.length ? tpl.params.map(p => ({ ...p })) : [{ k: '', v: '' }]);
    setRiskNote(tpl.risk_note);
    setImpactScope(tpl.impact_scope);
    setRollbackPlan(tpl.rollback_plan);
    setChangeWindow(tpl.change_window);
    // 变更窗口模板值尝试解析到范围选择器 (格式 2026-08-31 02:00-04:00 / 含 ~)
    const m1 = (tpl.change_window || '').match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
    if (m1) setCwStart(`${m1[1]}T${m1[2]}`);
    const tail = (tpl.change_window || '').split(/[~\-]/).pop()?.trim().match(/^(\d{2}:\d{2})$/);
    if (m1 && tail) setCwEnd(`${m1[1]}T${tail[1]}`);
    setToast({ ok: true, text: `已应用模板「${tpl.name}」，请补充站点 / case 号等必填信息后提交` });
  };

  // 存为我的模板: 把当前表单内容保存到 localStorage
  const saveAsTemplate = () => {
    const name = window.prompt('模板名称 (保存当前表单内容):');
    if (!name || !name.trim()) return;
    const finalType = toolName === CUSTOM_TYPE ? customType.trim() : toolName;
    if (!finalType) { setToast({ ok: false, text: '请先填写操作类型' }); return; }
    const tpl: ApprovalTemplate = {
      id: `my_${Date.now()}`, name: name.trim(), tool_name: finalType,
      service_type: serviceType.trim(), priority,
      params: kvRows.filter(r => r.k.trim()),
      summary, customer: customer.trim(), approvers: approvers.trim(),
      risk_note: riskNote, impact_scope: impactScope,
      rollback_plan: rollbackPlan, change_window: changeWindow,
    };
    const next = [...myTpls, tpl];
    setMyTpls(next);
    saveMyTemplates(next);
    setToast({ ok: true, text: `模板「${tpl.name}」已保存` });
  };

  const deleteMyTemplate = (id: string) => {
    const next = myTpls.filter(t => t.id !== id);
    setMyTpls(next);
    saveMyTemplates(next);
  };

  // case 号联动校验: blur 时查 OPS-CORE 信息平台 (平台不可用仅提示, 不阻断提交)
  const checkCase = async () => {
    const v = caseNo.trim();
    if (v.length < 3) { setCaseCheck({ state: 'idle', text: '' }); return; }
    setCaseCheck({ state: 'checking', text: '正在 OPS-CORE 平台校验 case 号...' });
    try {
      const d = await lookupCaseNo(v);
      if (!d.available) {
        setCaseCheck({ state: 'unavail', text: 'OPS-CORE 校验服务暂不可用，可继续提交（平台数据可能有延迟）' });
      } else if (d.found && d.cases[0]) {
        const c = d.cases[0];
        setCaseCheck({ state: 'ok', text: `已在 OPS-CORE 平台找到: ${c.title || c.case_no || v}${c.status ? ` · 状态 ${c.status}` : ''}${c.customer ? ` · 客户 ${c.customer}` : ''}`, hit: c });
      } else {
        setCaseCheck({ state: 'miss', text: '未在 OPS-CORE 平台找到该 case 号，请确认编号无误（新 case 可能尚未同步）' });
      }
    } catch {
      setCaseCheck({ state: 'unavail', text: 'Case 校验请求失败，可继续提交' });
    }
  };

  // 进入维护审批时: 时间窗默认取表单「变更窗口」, 未填时引导手选 (CITP 时间为手选必填)
  useEffect(() => {
    if (category !== 'maintenance') return;
    if (!citpWStart && cwStart) setCitpWStart(cwStart);
    if (!citpWEnd && cwEnd) setCitpWEnd(cwEnd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category]);

  // 单据类型切换 (日常配置申请 / 维护申请): 两张独立填写单;
  // 切换后服务/操作类型不在新目录则清空重选 (避免跨目录残留)
  const switchCategory = (cat: string) => {
    if (cat === category) return;
    setCategory(cat);
    setCatTouched(true);
    const prods = ((meta as any).operation_groups_by_category as OpCatalogMap | undefined)?.[cat];
    if (prods) {
      if (productType && !prods.some(p => p.product === productType)) setProductType('');
      if (serviceType && !(prods.find(p => p.product === productType)?.services || []).some(sv => sv.service === serviceType)) setServiceType('');
      if (toolName && toolName !== CUSTOM_TYPE && !prods.some(p => p.services.some(sv => sv.ops.some(o => o.value === toolName)))) setToolName('');
    }
  };

  // 设备输入 → 候选搜索 (防抖 400ms; ≥2 字符; 选中候选后自动精确匹配)
  useEffect(() => {
    const q = citpTarget.trim();
    if (q.length < 2) { setDevCands([]); setDevNote(''); return; }
    const t = setTimeout(async () => {
      try {
        const d = await searchCitpDevices(q);
        setDevCands(d.candidates || []);
        setDevNote((d.candidates || []).length ? '' : (d.note || '无匹配设备'));
      } catch { setDevNote(''); }
    }, 400);
    return () => clearTimeout(t);
  }, [citpTarget]);

  // 设备匹配 (手选): 主机名/IP/SN → 资源管理系统匹配 SN/IP/型号/机柜 + 受影响客户
  const runMatch = async () => {
    const t = citpTarget.trim();
    if (!t) { setToast({ ok: false, text: '请先填写设备 (主机名/IP/SN)' }); return; }
    setMatchBusy(true); setMatchNote('正在匹配资源管理系统...'); setToast(null);
    try {
      const d = await matchApprovalDevice(t);
      setCitpMatch(d);
      setMatchNote(d.device.matched
        ? `已匹配${d.vms_count > 0 ? ` · 该主机上 VM ${d.vms_count} 台 · 客户 ${d.customers.length} 家` : ''}`
          + (d.device.maintenance_mode === false ? ' · ⚠ 主机未进入维护模式, 请先在 vCenter 置入维护模式' : '')
          + (d.device.maintenance_mode === true ? ' · 已在维护模式' : '')
        : `未匹配: ${d.device.reason || '请确认名称/IP/SN'}`);
    } catch (e: any) {
      setCitpMatch(null);
      setMatchNote(`匹配失败: ${e.message}`);
    }
    setMatchBusy(false);
  };

  // 按指定设备名精确匹配 (候选下拉点击复用)
  const runMatchFor = async (name: string) => {
    setMatchBusy(true); setMatchNote('正在匹配资源管理系统...');
    try {
      const d = await matchApprovalDevice(name);
      setCitpMatch(d);
      setMatchNote(d.device.matched
        ? `已匹配${d.vms_count > 0 ? ` · 该主机上 VM ${d.vms_count} 台 · 客户 ${d.customers.length} 家` : ''}`
          + (d.device.maintenance_mode === false ? ' · ⚠ 主机未进入维护模式, 请先在 vCenter 置入维护模式' : '')
          + (d.device.maintenance_mode === true ? ' · 已在维护模式' : '')
        : `未匹配: ${d.device.reason || '请确认名称/IP/SN'}`);
    } catch (e: any) { setCitpMatch(null); setMatchNote(`匹配失败: ${e.message}`); }
    setMatchBusy(false);
  };

  // CITP 生成 (维护审批): 故障类型模板 + 手选设备/时间窗 → 资源管理系统匹配 → 格式化 Plan/Check_list
  const runCitp = async () => {
    if (!faultType) { setToast({ ok: false, text: '请先选择故障类型 (Plan/Check_list 按该模板填充)' }); return; }
    if (!citpTarget.trim() && !targetHost.trim()) { setToast({ ok: false, text: '请先填写/匹配目标设备 (主机名/IP/SN)' }); return; }
    if (!citpWStart) { setToast({ ok: false, text: '请选择 CITP 时间窗 (开始时间)' }); return; }
    const fdt = (v: string) => v.replace('T', ' ');
    const window = citpWEnd
      ? (fdt(citpWStart).slice(0, 10) === fdt(citpWEnd).slice(0, 10)
          ? `${fdt(citpWStart).slice(0, 10)} ${fdt(citpWStart).slice(11)}-${fdt(citpWEnd).slice(11)}`
          : `${fdt(citpWStart)}-${fdt(citpWEnd)}`)
      : fdt(citpWStart);
    setCitpBusy(true); setToast(null);
    try {
      const params: Record<string, string> = {};
      for (const row of kvRows) if (row.k.trim()) params[row.k.trim()] = row.v.trim();
      const d = await generateApprovalCitp({
        fault_type: faultType, target: citpTarget.trim() || targetHost.trim().split(',')[0],
        window, tz: citpTz, platform: citpPlatform || undefined,
        case_no: caseNo.trim(), site: site.trim(), customer: customer.trim(),
        summary: summary.trim(), tool_name: toolName === CUSTOM_TYPE ? customType.trim() : toolName,
        target_host: targetHost.trim(), params,
      });
      if (d.available) {
        setCitp(d);
        if (d.device) { setCitpMatch({ device: d.device, customers: d.customers || [], vms_count: d.vms_count || 0 }); }
        setToast({ ok: true, text: `CITP 已生成 · ${d.reference_number || d.citp_no} · ${d.note || ''}` });
      } else {
        setToast({ ok: false, text: d.error || 'CITP 生成失败, 请稍后重试' });
      }
    } catch (error: any) {
      setToast({ ok: false, text: `CITP 生成失败: ${error.message}` });
    }
    setCitpBusy(false);
  };

  const submit = async () => {
    if (!summary.trim()) { setToast({ ok: false, text: '请填写申请事由 (如: 客户请求移除这两个vm，已经提供SCCRF，检查已经关机，请审批)' }); return; }
    if (!toolName) { setToast({ ok: false, text: '请选择操作类型' }); return; }
    const finalType = toolName === CUSTOM_TYPE ? customType.trim() : toolName;
    if (!finalType) { setToast({ ok: false, text: '请填写自定义操作类型名称' }); return; }
    if (!site.trim()) { setToast({ ok: false, text: '请选择站点' }); return; }
    if (!caseNo.trim()) { setToast({ ok: false, text: '请填写 case 号' }); return; }
    if (!/^(HD)?\d+$/.test(caseNo.trim())) { setToast({ ok: false, text: 'case 号格式应为纯数字 (如 5958943) 或 HD 前缀 (如 HD0000005955764)' }); return; }
    if (!approverDs.trim()) { setToast({ ok: false, text: 'DS 配置复核为必要级, 不能跳过: 请选择 DS 复核人' }); return; }
    // 维护申请单: 未生成 CITP 时二次确认 (CITP 为维护单核心内容, 允许例外提交)
    if (category === 'maintenance' && !citp) {
      if (!window.confirm('维护申请建议先「生成 CITP」(Plan + Check_list 随单入库)。\n确认不生成 CITP 直接提交吗?')) return;
    }
    const params: Record<string, string> = {};
    for (const row of kvRows) {
      const k = row.k.trim(), v = row.v.trim();
      if (k) params[k] = v;
    }
    // 变更窗口范围组合: 同日合并展示 (2026-08-31 02:00 ~ 04:00), 跨日全格式
    const fdt = (v: string) => v.replace('T', ' ');
    if (cwStart && cwEnd && cwEnd < cwStart) { setToast({ ok: false, text: '变更窗口结束时间不能早于开始时间' }); return; }
    const finalWindow = cwStart && cwEnd
      ? (fdt(cwStart).slice(0, 10) === fdt(cwEnd).slice(0, 10)
          ? `${fdt(cwStart).slice(0, 10)} ${fdt(cwStart).slice(11)} ~ ${fdt(cwEnd).slice(11)}`
          : `${fdt(cwStart)} ~ ${fdt(cwEnd)}`)
      : (cwStart ? fdt(cwStart) : changeWindow);
    setSubmitting(true); setToast(null);
    const fields = {
      tool_name: finalType, summary: summary.trim(), params,
      risk_note: riskNote, site: site.trim(),
      service_type: (productType && serviceType ? `${productType}/${serviceType}` : (productType || serviceType)).trim(),
      case_no: caseNo.trim(),
      priority, planned_ts: plannedTs,
      impact_scope: impactScope, rollback_plan: rollbackPlan,
      change_window: finalWindow,
      customer: customer.trim(), approvers: approvers.trim(),
      customer_gid: customerGid.trim() || undefined,
      customer_site_ids: customerSiteIds.trim() || undefined,
      customer_order_no: customerOrder.trim() || undefined,
      original_approval_id: prefill?.original_approval_id || undefined,
      approver_ds: approverDs || undefined, approver_tech: approverTech || undefined,
      approver_admin: approverAdmin || undefined,
      target_host: targetHost.trim() || undefined,
      related_alerts: relAlerts.filter(a => selAlerts.has(a.alarm_id)).length
        ? relAlerts.filter(a => selAlerts.has(a.alarm_id)) : undefined,
      category,
      citp: category === 'maintenance' && citp ? citp : undefined,
    };
    try {
      // 编辑模式 → PUT 更新待审批单 (审批流重置); 新建 → POST
      const d = editRecord
        ? await updateApproval(editRecord.approval_id, fields)
        : await createApproval(fields);
      reset();
      if (!editRecord) clearApprovalDraft().catch(() => {});   // 新建提交后清草稿
      setRestoredAt(0); setDraftSavedAt(0);
      onSubmitted(editRecord ? editRecord.approval_id : (d as any).approval_id);
    } catch (error: any) {
      const detail = error.message.includes('detail') ? error.message.split('detail')[1].slice(0, 120) : error.message;
      setToast({ ok: false, text: `${editRecord ? '保存' : '提交'}失败: ${detail}` });
    }
    setSubmitting(false);
  };

  // AI 影响分析 (LLM): 依据操作类型/事由/站点/客户生成三段建议, 回填表单可人工编辑
  const runAnalyze = async () => {
    const finalType = toolName === CUSTOM_TYPE ? customType.trim() : toolName;
    if (!finalType && !summary.trim()) { setToast({ ok: false, text: '请先填写操作类型或申请事由再分析' }); return; }
    setAnalyzing(true); setToast(null);
    try {
      const params: Record<string, string> = {};
      for (const row of kvRows) if (row.k.trim()) params[row.k.trim()] = row.v.trim();
      const d = await analyzeApprovalImpact({
        tool_name: finalType, summary: summary.trim(), site: site.trim(),
        customer: customer.trim(), customer_gid: customerGid.trim() || undefined,
        case_no: caseNo.trim(), service_type: serviceType.trim(), params,
      });
      if (d.available) {
        if (d.impact_scope) setImpactScope(d.impact_scope);
        if (d.risk_note) setRiskNote(d.risk_note);
        if (d.rollback_plan) setRollbackPlan(d.rollback_plan);
        setToast({ ok: true, text: 'AI 已生成影响分析 (已回填, 可人工编辑后提交)' });
      } else {
        setToast({ ok: false, text: d.error || '分析服务暂不可用, 请人工填写' });
      }
    } catch (error: any) {
      setToast({ ok: false, text: `分析失败: ${error.message}` });
    }
    setAnalyzing(false);
  };

  // 站点 combobox 过滤: label/值 包含匹配 (前 20 项); 无输入显示全部
  const siteItems = (meta.site_items?.length ? meta.site_items : meta.sites.map(x => ({ value: x, label: x, kw: x })));
  const siteFiltered = siteItems
    .filter(it => { const q = siteText.trim().toLowerCase(); return !q || (it.kw || it.label).toLowerCase().includes(q) || it.value.toLowerCase().includes(q); })
    .slice(0, 20);

  // 客户 ↔ 站点联动: CRM site_ids (如 HKGAAA) 升级匹配站点字典完整码 (HKHKGAAA)
  const siteByCode = (sid: string) => {
    const items = meta.site_items?.length ? meta.site_items : [];
    return items.find(it => it.value === sid || it.value.endsWith(sid)) || null;
  };
  const applySiteByCode = (sid: string) => {
    const it = siteByCode(sid);
    if (it) { setSite(it.value); setSiteText(it.label); }
    else { setSite(sid); setSiteText(sid); }
  };
  // 选客户: 站点为空时自动带出其第一个站点; 客户多站点由徽标点击切换
  const linkCustomerSite = (siteIds?: string[]) => {
    if (site.trim() || !siteIds?.length) return;
    applySiteByCode(siteIds[0]);
  };

  // 客户搜索下拉: 输入过滤 OPS-CORE 字典 (前 20 条); 精确匹配 name 自动带出 GID
  const custFiltered = custList
    .filter(c => !customer.trim() || c.name.toLowerCase().includes(customer.trim().toLowerCase()) || c.gid.toLowerCase().includes(customer.trim().toLowerCase()))
    .slice(0, 20);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6"
         style={{ background: 'rgba(17,24,39,0.45)' }}
         role="dialog" aria-modal="true" aria-label="新建审批申请"
         onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-7xl max-h-[92vh] flex flex-col overflow-hidden">
        {/* 弹窗头: 单据类型随表单切换 */}
        <div className={`flex items-center justify-between gap-3 px-5 py-3 border-b shrink-0 ${category === 'maintenance' ? 'border-amber-200 bg-amber-50/60' : 'border-[#F3F4F6] bg-sky-50/50'}`}>
          <div className="flex items-center gap-2">
            {category === 'maintenance'
              ? <Wrench size={16} className="text-amber-600" />
              : <Settings2 size={16} className="text-sky-600" />}
            <span className="text-sm font-semibold text-[#111827]">
              {editRecord
                ? `编辑审批单 · ${editRecord.approval_id}`
                : prefill?.original_approval_id ? '发起回退申请'
                : category === 'maintenance' ? '新建维护申请' : '新建日常配置申请'}
            </span>
            {editRecord && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200 shrink-0">
                {editRecord.edit_count ? `已编辑 ${editRecord.edit_count} 次` : '首次编辑'} · 保存后审批流重置
              </span>
            )}
            <span className="text-[11px] text-[#9CA3AF] hidden sm:inline">
              {category === 'maintenance' ? '硬件维修 / 停机变更 · 需生成 CITP' : '常规配置 / 策略 / 记录调整'}
            </span>
          </div>
          <button onClick={onClose} title="关闭 (Esc)"
            className="w-7 h-7 flex items-center justify-center rounded-lg text-[#6B7280] hover:bg-black/5 hover:text-[#111827] transition-colors">
            <X size={16} />
          </button>
        </div>
        {/* 弹窗主体 (内部滚动 · 逻辑分区卡片) */}
        <div className="flex-1 overflow-y-auto px-5 py-4 bg-gray-50/50">
        <div className="space-y-3">
      {restoredAt > 0 && (
        <div className="flex items-center gap-2 mb-4 px-3.5 py-2 rounded-lg text-xs bg-emerald-50 text-emerald-800 border border-emerald-200">
          <Save size={13} />
          已恢复 {new Date(restoredAt).toLocaleString('zh-CN', { hour12: false })} 保存的草稿，可继续填写
          <button onClick={() => { clearApprovalDraft().catch(() => {}); reset(); setRestoredAt(0); setDraftSavedAt(0); }}
            className="ml-auto px-2 py-0.5 rounded border border-emerald-300 text-emerald-700 hover:bg-emerald-100 transition-colors shrink-0">
            丢弃草稿
          </button>
        </div>
      )}
      {prefill?.original_approval_id && (
        <div className="flex items-center gap-2 mb-4 px-3.5 py-2.5 rounded-lg text-xs bg-amber-50 text-amber-800 border border-amber-200">
          <RotateCcw size={14} />
          回退申请 · 关联原单 <span className="font-mono font-semibold">{prefill.original_approval_id}</span>
          （该单已批准执行；请补充回退原因与恢复目标，提交后将进入新的审批流程）
        </div>
      )}
      {/* 单据类型: 日常配置申请 / 维护申请 两张独立填写单 (服务类型/操作类型各自目录) */}
      <div className={`flex items-center gap-2 mb-4 pb-4 border-b ${category === 'maintenance' ? 'border-amber-200' : 'border-[#F3F4F6]'}`}>
        <div className={`flex items-center gap-1 p-1 rounded-xl border bg-gray-50 ${editRecord ? 'opacity-60 pointer-events-none border-sky-200' : 'border-[#E5E7EB]'}`}>
          <button type="button" onClick={() => switchCategory('daily')}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${category === 'daily' ? 'bg-sky-600 text-white shadow-sm' : 'text-[#6B7280] hover:text-[#111827]'}`}>
            <Settings2 size={13} />日常配置申请
          </button>
          <button type="button" onClick={() => switchCategory('maintenance')}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${category === 'maintenance' ? 'bg-amber-600 text-white shadow-sm' : 'text-[#6B7280] hover:text-[#111827]'}`}>
            <Wrench size={13} />维护申请<span className={category === 'maintenance' ? 'opacity-80' : 'text-amber-600'}> · CITP</span>
          </button>
        </div>
        <span className="text-[11px] text-[#9CA3AF]">
          {category === 'daily'
            ? '常规配置调整'
            : '硬件维修 / 停机变更 · CITP'}
        </span>
      </div>
      {/* 模板行 */}
      <div className="flex items-start gap-2 mb-4 pb-4 border-b border-[#F3F4F6] flex-wrap">
        <span className="text-xs text-[#9CA3AF] flex items-center gap-1 shrink-0 mt-1.5"><ClipboardList size={13} />快速模板</span>
        <div className="flex items-center gap-1.5 flex-wrap">
          {PRESET_TEMPLATES.map(tpl => (
            <button key={tpl.id} onClick={() => applyTemplate(tpl)}
              className="text-xs px-2.5 py-1 rounded-full border border-[#E5E7EB] bg-white text-[#374151] hover:border-[#E11D48] hover:text-[#E11D48] transition-colors">
              {tpl.name}
            </button>
          ))}
          {myTpls.map(tpl => (
            <span key={tpl.id} className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-full border border-violet-200 bg-violet-50 text-violet-700">
              <button onClick={() => applyTemplate(tpl)} className="hover:underline">{tpl.name}</button>
              <button onClick={() => deleteMyTemplate(tpl.id)} className="hover:text-red-500" title="删除模板"><X size={11} /></button>
            </span>
          ))}
        </div>
        <button onClick={saveAsTemplate}
          className="ml-auto text-xs px-2.5 py-1 rounded-full border border-dashed border-[#D1D5DB] text-[#6B7280] hover:border-[#E11D48] hover:text-[#E11D48] transition-colors shrink-0">
          存为我的模板
        </button>
      </div>
      <FormSection icon={FileText} step={1} title="申请内容" hint="事由与变更操作"
        action={<span>
          <button type="button" onClick={() => runAutofill(true)} disabled={autofilling || submitting}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border border-sky-300 text-sky-700 bg-sky-50 hover:bg-sky-100 disabled:opacity-50 transition-colors">
            <Wand2 size={13} />{autofilling ? '分析中...' : '智能填充'}
          </button>
        </span>}>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="col-span-2 lg:col-span-4">
        <label className={labelCls}>申请事由 <span className="text-red-500">*</span></label>
        <textarea value={summary} onChange={e => setSummary(e.target.value)} rows={2}
          placeholder="如: 客户请求移除 CEPH-HK-8063 上这两个 vm，已经提供SCCRF，case 5911694，检查已经关机，请审批"
          className={inputCls + ' resize-y'} />
      </div>
        {/* 操作类型: 单个下拉按类别分组; 产品/服务归属选后自动推导 (不再让用户逐级选) */}
        <div className="col-span-2 lg:col-span-2">
          <label className={labelCls}>操作类型 <span className="text-red-500">*</span></label>
          <select value={toolName} onChange={e => onOpChange(e.target.value)} className={inputCls}>
            <option value="">请选择</option>
            {OP_PRODUCTS.map(p => p.services.filter(sv => sv.ops.length).map(sv => (
              <optgroup key={`${p.product}/${sv.service}`}
                label={p.product === '平台服务' ? sv.service
                  : p.product === '硬件' ? '硬件维护'
                  : p.product === '存储' ? `存储 · ${sv.service.split(' (')[0]}`
                  : p.product === 'BRR (备份)' ? '备份'
                  : `${p.product} · ${sv.service}`}>
                {sv.ops.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
              </optgroup>
            )))}
            {toolName && toolName !== CUSTOM_TYPE && !allOps.some(o => o.value === toolName) &&
              <option value={toolName}>{toolName}</option>}
            <option value={CUSTOM_TYPE}>自定义类型...</option>
          </select>
        </div>
        {/* 归属 (自动推导): 选中操作后显示 产品 · 服务, 无需手选 */}
        <div>
          <label className={labelCls}>归属</label>
          <div className={'px-3 py-2 rounded-lg border text-xs ' + (productType && serviceType ? 'border-[#E5E7EB] bg-gray-50 text-[#111827]' : 'border-dashed border-[#E5E7EB] text-[#9CA3AF]')}>
            {productType && serviceType ? `${productType} · ${serviceType}` : '—'}
          </div>
        </div>
        {toolName === CUSTOM_TYPE ? (
          <div>
            <label className={labelCls}>自定义类型名称</label>
            <input value={customType} onChange={e => setCustomType(e.target.value)} placeholder="如 db_failover_switch" className={inputCls + ' font-mono'} />
          </div>
        ) : (
          <div>
            <label className={labelCls}>参数建议</label>
            <div className="px-3 py-2 rounded-lg bg-gray-50 text-xs font-mono text-[#6B7280]">
              {OP_HINTS[toolName] || '—'}
            </div>
          </div>
        )}
        </div>
      </FormSection>
      <FormSection icon={Server} step={2} title="关联对象" hint="资源 · 客户 · Case">
        {/* ── 子组: 资源 ── */}
        <div className="text-[11px] font-semibold text-[#374151] mb-2 flex items-center gap-1.5">
          <Box size={11} className="text-[#6B7280]" />资源
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="relative" ref={siteBoxRef}>
          <label className={labelCls}>站点 <span className="text-red-500">*</span></label>
          <input value={siteText}
            onChange={e => { setSiteText(e.target.value); setSite(e.target.value.trim()); setSiteOpen(true); }}
            onFocus={() => setSiteOpen(true)}
            onBlur={() => setTimeout(() => setSiteOpen(false), 180)}
            placeholder="输入站点/机房名，如香港/HKG、CTT" className={inputCls} />
          {/* ComboPanel: portal 到 body, 不被表单滚动区裁剪; 下方空间不足自动向上翻 */}
          {siteOpen && siteFiltered.length > 0 && (
            <ComboPanel anchor={siteBoxRef.current}>
              {/* 选中存城市三字码 (scope/资源池前缀兼容), 显示机房中文名 */}
              {siteFiltered.map(it => (
                <button type="button" key={it.value} onMouseDown={e => { e.preventDefault(); setSite(it.value); setSiteText(it.label); setSiteOpen(false); }}
                  className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 flex items-center gap-2">
                  <span className="text-[#111827] truncate flex-1">{it.label}</span>
                  {site === it.value && <span className="text-[10px] text-emerald-600 shrink-0">✓</span>}
                </button>
              ))}
            </ComboPanel>
          )}
        </div>
        <div>
          <label className={labelCls}>目标主机/服务器 <span className="text-[#9CA3AF] font-normal">(逗号分隔 · 联动 NMS 告警)</span></label>
          <input value={targetHost} onChange={e => setTargetHost(e.target.value)}
            placeholder="如 db-prod-01, web-02" className={inputCls + ' font-mono'} />
        </div>
        {/* NMS 告警联动: 按站点/客户/case/主机自动匹配活动告警, 勾选随单关联 */}
        <div className="lg:col-span-4">
          <label className={labelCls}>
            <span className="inline-flex items-center gap-1.5"><BellRing size={12} className="text-rose-500" />关联 NMS 告警</span>
          </label>
          {relLoading ? (
            <div className="text-xs text-[#9CA3AF] px-3 py-2 rounded-lg bg-gray-50 border border-dashed border-[#E5E7EB]">正在查询 NMS 活动告警...</div>
          ) : relAlerts.length === 0 ? (
            <div className="text-xs text-[#9CA3AF] px-3 py-2 rounded-lg bg-gray-50 border border-dashed border-[#E5E7EB]">{relNote || '填写站点 / 客户 / case 号 / 目标主机后, 自动匹配当前活动告警'}</div>
          ) : (
            <div className="border border-[#E5E7EB] rounded-lg overflow-hidden">
              <div className="px-3 py-1.5 bg-gray-50 border-b border-[#E5E7EB] text-[11px] text-[#6B7280] flex items-center justify-between">
                <span>{relNote}</span>
                <span className="shrink-0 ml-2">已选 {selAlerts.size} 条</span>
              </div>
              <div className="max-h-48 overflow-y-auto divide-y divide-[#F3F4F6]">
                {relAlerts.map(a => (
                  <label key={a.alarm_id} className="flex items-start gap-2 px-3 py-2 cursor-pointer hover:bg-gray-50">
                    <input type="checkbox" checked={selAlerts.has(a.alarm_id)}
                      onChange={() => setSelAlerts(prev => { const n = new Set(prev); if (n.has(a.alarm_id)) n.delete(a.alarm_id); else n.add(a.alarm_id); return n; })}
                      className="mt-0.5 w-3.5 h-3.5 rounded border-gray-300 text-rose-600 focus:ring-rose-500 shrink-0" />
                    <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${SEV_CLS[a.severity] || 'bg-gray-100 text-gray-600'}`}>{a.severity}</span>
                    <span className="flex-1 min-w-0">
                      <span className="text-xs text-[#111827] block truncate">{a.summary}</span>
                      <span className="text-[10px] text-[#9CA3AF] font-mono">{a.node}{a.case_id ? ` · case ${a.case_id}` : ''}{a.time ? ` · ${a.time}` : ''}</span>
                    </span>
                    <span className="flex gap-1 shrink-0">{(a.matched || []).map(m => (
                      <span key={m} className="text-[9px] px-1 py-0.5 rounded bg-rose-50 text-rose-600 border border-rose-100">{m}</span>))}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
        </div>
        {/* __SUBGROUP_CUSTOMER__ */}
        <div className="text-[11px] font-semibold text-[#374151] mb-2 mt-4 flex items-center gap-1.5">
          <Users size={11} className="text-[#6B7280]" />关联客户
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="relative" ref={custBoxRef}>
          <label className={labelCls}>
            客户 <span className="text-[#9CA3AF] font-normal">({custList.length ? ` · ${custList.length} 家` : '...'}{customerGid ? ` · GID ${customerGid}` : ''})</span>
          </label>
          <input value={customer} onChange={e => {
            const v = e.target.value;
            setCustomer(v);
            const hit = custList.find(c => c.name === v.trim());
            setCustomerGid(hit ? hit.gid : '');  // 精确选中字典客户才带 GID, 手输兜底不带
            setCustomerSiteIds(hit?.site_ids?.length ? hit.site_ids.join(',') : '');
            setCustomerOrder(hit?.order_no || '');
            if (hit) linkCustomerSite(hit.site_ids);
          }}
            onFocus={() => setCustOpen(true)}
            onBlur={() => setTimeout(() => setCustOpen(false), 180)}
            placeholder="输入关键词选择 (如 Nidec / TW43727)，或直接手输" className={inputCls} />
          {/* ComboPanel: portal 到 body, 不被表单滚动区裁剪; 下方空间不足自动向上翻 */}
          {custOpen && custFiltered.length > 0 && (
            <ComboPanel anchor={custBoxRef.current}>
              {custFiltered.map(c => (
                <button type="button" key={c.gid} onMouseDown={e => { e.preventDefault(); setCustomer(c.name); setCustomerGid(c.gid); setCustomerSiteIds(c.site_ids?.length ? c.site_ids.join(',') : ''); setCustomerOrder(c.order_no || ''); if (c.site_ids?.length) linkCustomerSite(c.site_ids); setCustOpen(false); }}
                  className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 flex items-center gap-2">
                  <span className="font-mono text-[10px] font-semibold text-indigo-600 bg-indigo-50 border border-indigo-100 rounded px-1.5 py-0.5 shrink-0">{c.gid}</span>
                  <span className="text-[#111827] truncate flex-1">{c.name}</span>
                </button>
              ))}
            </ComboPanel>
          )}
          {(customerGid || customerSiteIds) && !custOpen && (
            <div className="mt-1 flex items-center gap-1.5 flex-wrap">
              {customerGid && <span className="inline-flex items-center gap-1 text-[10px] font-mono font-semibold px-2 py-0.5 rounded-full bg-indigo-50 border border-indigo-200 text-indigo-600">GID {customerGid}</span>}
              {customerOrder && <span className="inline-flex items-center gap-1 text-[10px] font-mono font-semibold px-2 py-0.5 rounded-full bg-violet-50 border border-violet-200 text-violet-700">Order {customerOrder}</span>}
              {customerSiteIds && customerSiteIds.split(',').filter(Boolean).slice(0, 4).map(sid => (
                <button type="button" key={sid} title="点击填入该站点"
                  onClick={() => applySiteByCode(sid)}
                  className={`inline-flex items-center gap-1 text-[10px] font-mono font-semibold px-2 py-0.5 rounded-full border transition-colors ${
                    site === (siteByCode(sid)?.value || sid)
                      ? 'bg-teal-100 border-teal-300 text-teal-800'
                      : 'bg-teal-50 border-teal-200 text-teal-700 hover:bg-teal-100'}`}>
                  Site {sid}{site === (siteByCode(sid)?.value || sid) ? ' ✓' : ''}
                </button>
              ))}
              {customerSiteIds.split(',').filter(Boolean).length > 4 && (
                <span className="text-[10px] font-mono text-teal-700">+{customerSiteIds.split(',').filter(Boolean).length - 4}</span>
              )}
              <span className="text-[10px] text-[#9CA3AF]">OPS-CORE CRM 自动关联</span>
            </div>
          )}
        </div>
        </div>
        {/* __SUBGROUP_CASE__ */}
        <div className="text-[11px] font-semibold text-[#374151] mb-2 mt-4 flex items-center gap-1.5">
          <FileSearch size={11} className="text-[#6B7280]" />Case
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div>
          <label className={labelCls}>case 号 <span className="text-red-500">*</span></label>
          <input value={caseNo}
            onChange={e => { setCaseNo(e.target.value); if (caseCheck.state !== 'idle') setCaseCheck({ state: 'idle', text: '' }); }}
            onBlur={checkCase} placeholder="如 5958943 或 HD0000005955764 (失焦自动校验)" className={inputCls + ' font-mono'} />
          {caseCheck.state !== 'idle' && (
            <div className={`mt-1 text-[11px] leading-snug px-2 py-1 rounded ${
              caseCheck.state === 'ok' ? 'bg-emerald-50 text-emerald-700' :
              caseCheck.state === 'miss' ? 'bg-amber-50 text-amber-700' :
              caseCheck.state === 'checking' ? 'bg-gray-50 text-[#6B7280]' : 'bg-gray-50 text-[#9CA3AF]'}`}>
              {caseCheck.state === 'ok' ? '✓ ' : caseCheck.state === 'miss' ? '⚠ ' : ''}{caseCheck.text}
            </div>
          )}
        </div>
        </div>
      </FormSection>
        {/* CITP (维护审批, 按真实 CITP 附件格式): 故障类型模板 + 设备/时间手选 + 资源管理系统匹配 */}
        {category === 'maintenance' && (
          <div className="border border-amber-200 rounded-xl overflow-hidden bg-white">
            <div className="px-3 py-2 bg-amber-50 border-b border-amber-200 flex items-center justify-between gap-2 flex-wrap">
              <span className="text-xs font-semibold text-amber-800 flex items-center gap-1.5">
                <span className="w-[18px] h-[18px] rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 bg-amber-500 text-white">3</span>
                <FileCheck2 size={13} />CITP 维护测试计划
                
              </span>
              <div className="flex items-center gap-2">
                {citp?.reference_number && <span className="text-[10px] font-mono text-amber-700 truncate max-w-[220px]">{citp.reference_number}</span>}
                <button onClick={runCitp} disabled={citpBusy || submitting}
                  className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium border border-amber-300 text-amber-800 bg-white hover:bg-amber-50 disabled:opacity-50 transition-colors">
                  <Sparkles size={12} />{citpBusy ? '生成中 (匹配设备 + 模板填充)...' : citp ? '重新生成 CITP' : '生成 CITP'}
                </button>
              </div>
            </div>
            <div className="p-3 space-y-3">
              {/* 手选区: 故障类型 + 时间窗 + 设备 */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                <div>
                  <label className="text-[11px] text-[#6B7280] mb-1 block font-medium">故障类型 <span className="text-red-500">*</span></label>
                  <select value={faultType} onChange={e => setFaultType(e.target.value)}
                    className={inputCls + ' !py-1.5 !text-xs' + (faultType ? '' : ' !border-red-300 !bg-red-50/30')}>
                    <option value="" disabled>{(meta.citp_fault_types?.length || 0) > 0 ? '请选择故障类型...' : '目录加载中...'}</option>
                    {(meta.citp_fault_types || []).map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-[11px] text-[#6B7280] mb-1 block font-medium">平台检查单</label>
                  <select value={citpPlatform} onChange={e => setCitpPlatform(e.target.value)}
                    className={inputCls + ' !py-1.5 !text-xs'}>
                    <option value="">自动识别{citp?.platform ? ` (${citp.platform})` : ''}</option>
                    <option value="storage">存储阵列</option>
                    <option value="ceph">主机 (Ceph)</option>
                    <option value="esxi">虚拟化 (ESXi)</option>
                    <option value="generic">通用</option>
                  </select>
                </div>
                <div>
                  <label className="text-[11px] text-[#6B7280] mb-1 block font-medium">时间窗 <span className="text-red-500">*</span></label>
                  <div className="flex items-center gap-1">
                    <input type="datetime-local" value={citpWStart} onChange={e => setCitpWStart(e.target.value)}
                      className={inputCls + ' !py-1.5 !text-xs'} />
                    <span className="text-[#9CA3AF] text-xs shrink-0">-</span>
                    <input type="datetime-local" value={citpWEnd} onChange={e => setCitpWEnd(e.target.value)}
                      className={inputCls + ' !py-1.5 !text-xs'} />
                    <select value={citpTz} onChange={e => setCitpTz(e.target.value)}
                      className="px-1.5 py-1.5 rounded-lg border border-[#E5E7EB] text-[11px] text-[#111827] bg-white shrink-0">
                      {['UTC+8', 'HKT', 'UTC+7', 'UTC+9', 'UTC'].map(tz => <option key={tz} value={tz}>{tz}</option>)}
                    </select>
                  </div>
                </div>
                <div className="lg:col-span-2">
                  <label className="text-[11px] text-[#6B7280] mb-1 block font-medium">目标设备 <span className="text-red-500">*</span></label>
                  <div className="flex items-center gap-1.5">
                    <div className="relative flex-1" ref={devBoxRef}>
                    <input value={citpTarget}
                      onChange={e => { setCitpTarget(e.target.value); setDevOpen(true); }}
                      onFocus={() => setDevOpen(true)}
                      onBlur={() => setTimeout(() => setDevOpen(false), 200)}
                      placeholder="输入设备名/IP 搜索选择, 如 3118 / ESXI-HK-3118 / 3106SN"
                      className={inputCls + ' !py-1.5 !text-xs font-mono'} />
                    {devOpen && devCands.length > 0 && (
                      <ComboPanel anchor={devBoxRef.current}>
                        {devCands.map(cd => (
                          <button type="button" key={cd.name + cd.kind}
                            onMouseDown={e => { e.preventDefault(); setCitpTarget(cd.name); setDevOpen(false);
                              setTimeout(() => runMatchFor(cd.name), 50); }}
                            className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 flex items-center gap-2">
                            <span className={`text-[9px] px-1.5 py-0.5 rounded shrink-0 ${cd.kind === 'array' ? 'bg-amber-50 text-amber-700 border border-amber-100' : 'bg-sky-50 text-sky-700 border border-sky-100'}`}>{cd.kind === 'array' ? '阵列' : '主机'}</span>
                            <span className="font-mono text-[#111827]">{cd.name}</span>
                            <span className="text-[10px] text-[#9CA3AF] truncate">{cd.vendor} {cd.model}{cd.ip ? ` · ${cd.ip}` : ''}</span>
                          </button>
                        ))}
                      </ComboPanel>
                    )}
                    {devOpen && devCands.length === 0 && devNote && (
                      <div className="absolute left-0 right-0 top-full mt-1 text-[10px] text-[#9CA3AF] px-1">{devNote}</div>
                    )}
                    </div>
                    <button onClick={runMatch} disabled={matchBusy}
                      className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-[11px] font-medium border border-[#D1D5DB] bg-white text-[#374151] hover:border-amber-400 hover:text-amber-700 disabled:opacity-50 transition-colors shrink-0">
                      <Server size={11} />{matchBusy ? '匹配中...' : '匹配设备'}
                    </button>
                    <button onClick={() => setCitpTarget(targetHost.trim().split(',')[0] || '')}
                      title="取表单「目标主机/服务器」第一项"
                      className="px-2 py-1.5 rounded-lg text-[11px] border border-dashed border-[#D1D5DB] text-[#6B7280] hover:border-amber-400 shrink-0">取主机字段</button>
                  </div>
                  {matchNote && <div className={`mt-1 text-[11px] px-2 py-1 rounded ${citpMatch?.device.matched ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'}`}>
                    {citpMatch?.device.matched ? '✓ ' : '⚠ '}{matchNote}
                  </div>}
                  {citpMatch && (
                    <div className="mt-1.5 flex flex-wrap gap-1.5 text-[10px]">
                      {citpMatch.device.matched ? (
                        <>
                          <span className="px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-100 font-mono">{citpMatch.device.name || citpMatch.device.hostname}</span>
                          {citpMatch.device.sn && <span className="px-2 py-0.5 rounded-full bg-white border border-[#E5E7EB] font-mono text-[#374151]">SN {citpMatch.device.sn}</span>}
                          {citpMatch.device.ip && <span className="px-2 py-0.5 rounded-full bg-white border border-[#E5E7EB] font-mono text-[#374151]">IP {citpMatch.device.ip}</span>}
                          <span className="px-2 py-0.5 rounded-full bg-white border border-[#E5E7EB] text-[#374151]">{citpMatch.device.vendor || '-'} / {citpMatch.device.model || '-'}</span>
                          {(citpMatch.device.rack || citpMatch.device.site) && <span className="px-2 py-0.5 rounded-full bg-white border border-[#E5E7EB] text-[#374151]">{citpMatch.device.rack || citpMatch.device.site}</span>}
                          {typeof citpMatch.device.maintenance_mode === 'boolean' && (
                            <span className={`px-2 py-0.5 rounded-full border font-medium ${
                              citpMatch.device.maintenance_mode
                                ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                                : 'bg-red-50 border-red-200 text-red-600 animate-pulse'}`}>
                              {citpMatch.device.maintenance_mode ? '✓ 维护模式中' : '⚠ 未进维护模式'}
                            </span>
                          )}
                          {citpMatch.vms_count > 0 && <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-100">VM {citpMatch.vms_count} 台</span>}
                          {citpMatch.customers.slice(0, 6).map(c => (
                            <span key={c.name} className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-100">
                              {c.name}{c.vms ? ` (${c.vms})` : ''}{c.gid ? ` · GID${c.gid}` : ''}
                            </span>
                          ))}
                          {citpMatch.customers.length > 6 && <span className="px-2 py-0.5 rounded-full bg-gray-50 text-[#6B7280] border border-gray-200">等 {citpMatch.customers.length} 家客户</span>}
                        </>
                      ) : (
                        <span className="px-2 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-100">{citpMatch.device.reason}</span>
                      )}
                    </div>
                  )}
                </div>
              </div>
              {/* 生成结果: Plan (Change Management 步骤表) + Check_list 验证表 */}
              {citp && Array.isArray(citp.plan) && (
                <div className="space-y-3">
                  {citp.maintenance_ok === false && (
                    <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs bg-red-50 text-red-700 border border-red-200 font-medium">
                      ⚠ 主机尚未进入维护模式 — 请先在 vCenter 将主机置入维护模式 (迁走/暂停该主机 VM) 后再实施维护
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                    <span className={`px-2 py-0.5 rounded-full font-medium border ${(citp.matched_count ?? 0) > 0 ? 'bg-emerald-50 text-emerald-700 border-emerald-100' : 'bg-amber-100 text-amber-700 border-amber-200'}`}>
                      设备匹配 {citp.matched_count ?? 0} / {citp.total_targets ?? (citp.devices?.length || 1)}
                    </span>
                    <span className="px-2 py-0.5 rounded-full bg-white border border-[#E5E7EB] text-[#374151]">平台 {citp.platform || '-'}</span>
                    <span className={`px-2 py-0.5 rounded-full border ${(citp.alerts_matched ?? 0) > 0 ? 'bg-rose-50 text-rose-600 border-rose-100' : 'bg-gray-50 text-[#6B7280] border-gray-200'}`}>关联活动告警 {citp.alerts_matched ?? 0} 条</span>
                    {citp.window && <span className="px-2 py-0.5 rounded-full bg-teal-50 text-teal-700 border border-teal-100">{citp.window}</span>}
                    {citp.note && <span className="px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-100">{citp.note}</span>}
                  </div>
                  <div>
                    <div className="text-[11px] font-semibold text-[#374151] mb-1">Plan · Change Management</div>
                    <div className="bg-white border border-amber-100 rounded overflow-x-auto">
                      <table className="w-full text-[11px]">
                        <thead>
                          <tr className="bg-gray-50 text-[#6B7280]">
                            <th className="px-2 py-1.5 font-medium text-left">step</th>
                            <th className="px-2 py-1.5 font-medium text-left">time</th>
                            <th className="px-2 py-1.5 font-medium text-left">维修检修步骤</th>
                            <th className="px-2 py-1.5 font-medium text-left">操作人员</th>
                            <th className="px-2 py-1.5 font-medium text-left">执行命令</th>
                            <th className="px-2 py-1.5 font-medium text-left">Verify/test</th>
                            <th className="px-2 py-1.5 font-medium text-left">Fallback</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-[#F3F4F6]">
                          {(citp.plan as CitpPlanStep[]).map((st, i) => (
                            <tr key={i}>
                              <td className="px-2 py-1.5 text-center">{st.step}</td>
                              <td className="px-2 py-1.5 whitespace-nowrap text-[#6B7280]">{st.time}</td>
                              <td className="px-2 py-1.5 text-[#111827] min-w-[240px] whitespace-pre-wrap">{st.desc}</td>
                              <td className="px-2 py-1.5 whitespace-nowrap">{st.operator}</td>
                              <td className="px-2 py-1.5 font-mono text-[#6B7280] whitespace-pre-wrap">{st.command || '-'}</td>
                              <td className="px-2 py-1.5 text-[#6B7280]">{st.verify || '-'}</td>
                              <td className="px-2 py-1.5 text-[#6B7280]">{st.fallback || '-'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] font-semibold text-[#374151] mb-1">Check_list · 验证项</div>
                    <div className="bg-white border border-amber-100 rounded overflow-x-auto">
                      <table className="w-full text-[11px]">
                        <thead>
                          <tr className="bg-gray-50 text-[#6B7280]">
                            <th className="px-2 py-1.5 font-medium text-left">Step</th>
                            <th className="px-2 py-1.5 font-medium text-left">Host</th>
                            <th className="px-2 py-1.5 font-medium text-left">IP</th>
                            <th className="px-2 py-1.5 font-medium text-left">Sub Step</th>
                            <th className="px-2 py-1.5 font-medium text-left">Test item</th>
                            <th className="px-2 py-1.5 font-medium text-left">Expected result</th>
                            <th className="px-2 py-1.5 font-medium text-left">Result</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-[#F3F4F6]">
                          {(citp.checklist || []).map((ck, i) => (
                            <tr key={i}>
                              <td className="px-2 py-1.5">{ck.step}</td>
                              <td className="px-2 py-1.5 font-mono">{ck.host || '-'}</td>
                              <td className="px-2 py-1.5 font-mono">{ck.ip || '-'}</td>
                              <td className="px-2 py-1.5">{ck.sub_step}</td>
                              <td className="px-2 py-1.5 min-w-[220px] whitespace-pre-wrap">{ck.item}</td>
                              <td className="px-2 py-1.5 text-[#6B7280] whitespace-pre-wrap">{ck.expected || '-'}</td>
                              <td className="px-2 py-1.5"></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              )}
              {!citp && (
                <div className="text-[11px] text-amber-800/80 px-1">
                  选择故障类型与时间窗，匹配目标设备后生成。
                </div>
              )}
            </div>
          </div>
        )}

      <FormSection icon={ClipboardList} step={category === 'maintenance' ? 4 : 3} title="计划与审批流" hint="窗口与审批人">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="col-span-2 lg:col-span-4">
          <label className={labelCls}>分级审批流 <span className="text-[#9CA3AF] font-normal">(DS → 技术 → 行政)</span></label>
          <div className="grid grid-cols-3 gap-2">
            {([
              ['DS 配置复核', approverDs, setApproverDs, meta.approvers_ds?.length ? meta.approvers_ds : meta.approvers],
              ['技术审批', approverTech, setApproverTech, meta.approvers],
              ['行政审批', approverAdmin, setApproverAdmin, meta.approvers],
            ] as const).map(([lbl, val, set, list], i) => (
              <div key={lbl} className="relative">
                <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-rose-50 text-rose-600 border border-rose-100 pointer-events-none z-10">{i + 1}</span>
                <select value={val} onChange={e => set(e.target.value)} className={inputCls + ' !pl-8 text-xs'}>
                  {i === 0
                    ? <option value="" disabled>{lbl} (必选)</option>
                    : <option value="">{lbl} (跳过)</option>}
                  {/* DS 级候选 = 持 approval.ds_review 的用户; 技术/行政级 = 持 approval.decide */}
                  {list.map(c => (
                    <option key={c.username} value={c.username}>{c.display_name} (@{c.username})</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
          {approvers && <div className="mt-1 text-[10px] text-[#9CA3AF]">模板沿用指定审批人: @{approvers.replace(/[,，]/g, ' @')} (仅名单内可批)</div>}
        </div>
        <div>
          <label className={labelCls}>优先级</label>
          <select value={priority} onChange={e => setPriority(e.target.value)} className={inputCls}>
            {Object.entries(PRIORITY_META).map(([v, m]) => <option key={v} value={v}>{m.label}</option>)}
          </select>
        </div>
        <div>
          <label className={labelCls}>计划执行时间</label>
          <input type="datetime-local" value={plannedTs} onChange={e => setPlannedTs(e.target.value)} className={inputCls} />
        </div>
          <div className="col-span-2">
          <label className={labelCls}>变更窗口 <span className="text-[#9CA3AF] font-normal">(起 ~ 止)</span></label>
          <div className="flex items-center gap-1">
            <input type="datetime-local" value={cwStart} onChange={e => setCwStart(e.target.value)} className={inputCls} />
            <span className="text-[#9CA3AF] text-xs shrink-0">~</span>
            <input type="datetime-local" value={cwEnd} onChange={e => setCwEnd(e.target.value)} className={inputCls} />
          </div>
        </div>
        </div>
      </FormSection>
      <FormSection icon={Sparkles} step={category === 'maintenance' ? 5 : 4} title="影响分析" hint="AI 辅助生成" action={<span><button onClick={runAnalyze} disabled={analyzing || submitting}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border border-amber-300 text-amber-700 bg-amber-50 hover:bg-amber-100 disabled:opacity-50 transition-colors">
            <Sparkles size={13} />{analyzing ? 'AI 分析中 (最长约 1 分钟)...' : 'AI 分析影响 · 自动填充'}
          </button></span>}>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <label className={labelCls}>影响范围</label>
          <textarea value={impactScope} onChange={e => setImpactScope(e.target.value)} rows={3} placeholder="说明本次操作影响的业务/服务范围, 或点击上方「AI 分析影响」自动生成"
            className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm text-[#111827] focus:outline-none focus:border-[#E11D48]" />
        </div>
        <div>
          <label className={labelCls}>回退方案</label>
          <textarea value={rollbackPlan} onChange={e => setRollbackPlan(e.target.value)} rows={3} placeholder="异常时的回退/恢复措施, 或点击上方「AI 分析影响」自动生成"
            className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm text-[#111827] focus:outline-none focus:border-[#E11D48]" />
        </div>
      </div>
          <div>
        <label className={labelCls}>风险说明</label>
        <textarea value={riskNote} onChange={e => setRiskNote(e.target.value)} rows={3} placeholder="说明操作影响与风险, 或点击上方「AI 分析影响」自动生成"
          className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm text-[#111827] focus:outline-none focus:border-[#E11D48]" />
      </div>
      </FormSection>
      <FormSection icon={Settings2} step={category === 'maintenance' ? 6 : 5} title="其他参数" hint="键值对, 可选" action={<span>{params_toggle}</span>}>
        {paramsOpen ? <div>
          <label className={labelCls}>参数 (键值对, 可选)</label>
          <div className="space-y-1.5">
            {kvRows.map((row, i) => (
              <div key={i} className="flex gap-1.5 max-w-xl">
                <input value={row.k} placeholder="参数名" onChange={e => setKvRows(rs => rs.map((r, j) => j === i ? { ...r, k: e.target.value } : r))}
                  className="w-2/5 px-2.5 py-1.5 rounded-lg border border-[#E5E7EB] text-xs font-mono text-[#111827] focus:outline-none focus:border-[#E11D48]" />
                <input value={row.v} placeholder="参数值" onChange={e => setKvRows(rs => rs.map((r, j) => j === i ? { ...r, v: e.target.value } : r))}
                  className="flex-1 px-2.5 py-1.5 rounded-lg border border-[#E5E7EB] text-xs font-mono text-[#111827] focus:outline-none focus:border-[#E11D48]" />
                {kvRows.length > 1 && (
                  <button onClick={() => setKvRows(rs => rs.filter((_, j) => j !== i))}
                    className="text-[#9CA3AF] hover:text-red-500 shrink-0"><X size={14} /></button>
                )}
              </div>
            ))}
            <button onClick={() => setKvRows(rs => [...rs, { k: '', v: '' }])}
              className="text-xs text-[#E11D48] flex items-center gap-1 hover:underline"><Plus size={12} />添加参数</button>
          </div>
        </div> : <div className="text-[11px] text-[#9CA3AF]">附加键值参数 · 点击右上「展开」填写</div>}
      </FormSection>
      </div>
      {toast && <div className="mt-3"><Toast toast={toast} /></div>}
      <div className="flex items-center justify-between gap-3 mt-4 pt-3 border-t border-[#F3F4F6]">
        <div className="flex items-center gap-2 min-w-0">
          <button onClick={() => { saveApprovalDraft(collectDraft()).then(() => setDraftSavedAt(Date.now())).catch(() => setToast({ ok: false, text: '草稿保存失败' })); setRestoredAt(0); }}
            disabled={submitting}
            className="flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm font-medium border border-[#D1D5DB] text-[#374151] bg-white hover:border-[#9CA3AF] hover:text-[#111827] disabled:opacity-50 transition-colors shrink-0">
            <Save size={14} />保存草稿
          </button>
          <span className="text-[11px] text-[#9CA3AF] truncate">
            {editRecord
              ? '保存后单据更新 · 分级审批流重置从头审批'
              : draftSavedAt
              ? `草稿已保存 ${new Date(draftSavedAt).toLocaleTimeString('zh-CN', { hour12: false })}`
              : '自动暂存草稿'}
          </span>
        </div>
        <button onClick={submit} disabled={submitting}
          className="flex items-center gap-2 px-5 py-2 rounded-lg text-sm font-medium bg-[#E11D48] text-white hover:opacity-90 disabled:opacity-50 transition-opacity shrink-0">
          <Send size={14} />{submitting ? '保存中...' : editRecord ? '保存修改' : '提交申请'}
        </button>
      </div>
        </div>
      </div>
    </div>
  );
}

// 业务字段徽标行
function BizFieldBadges({ a }: { a: any }) {
  const pri = a.priority ? PRIORITY_META[a.priority] : null;
  let relAlerts: { severity?: string; node?: string; summary?: string; case_id?: string }[] = [];
  try { relAlerts = JSON.parse(a.related_alerts || '[]'); } catch { relAlerts = []; }
  return (
    <>
    <div className="flex flex-wrap items-center gap-1.5 mb-2">
      <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full border ${CATEGORY_META[a.category || 'daily']?.cls}`}>{catLabel(a.category)}</span>
      {a.group && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200">{a.group}</span>}
      {a.customer && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600">客户 {a.customer}</span>}
      {a.customer_gid && <span className="text-[10px] font-mono font-medium px-2 py-0.5 rounded-full border border-indigo-200 text-indigo-500">GID {a.customer_gid}</span>}
      {a.customer_order_no && <span className="text-[10px] font-mono font-medium px-2 py-0.5 rounded-full bg-violet-50 text-violet-700 border border-violet-200">Order {a.customer_order_no}</span>}
      {((a.customer_site_ids || '') as string).split(',').filter(Boolean).slice(0, 3).map((sid: string) => (
        <span key={sid} className="text-[10px] font-mono font-medium px-2 py-0.5 rounded-full bg-teal-50 text-teal-700 border border-teal-200">Site {sid}</span>
      ))}
      {a.site && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-blue-50 text-blue-600">站点 {a.site}</span>}
      {a.service_type && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-violet-50 text-violet-600">服务 {a.service_type}</span>}
      {a.case_no && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-sky-50 text-sky-700">Case {a.case_no}</span>}
      {pri && <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${pri.cls}`}>{pri.label}</span>}
      {a.planned_ts && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">计划 {fmtPlanned(a.planned_ts)}</span>}
      {a.change_window && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-teal-50 text-teal-700">窗口 {a.change_window}</span>}
      {a.target_host && <span className="text-[10px] font-mono font-medium px-2 py-0.5 rounded-full bg-rose-50 text-rose-600 border border-rose-100">主机 {a.target_host}</span>}
      {relAlerts.length > 0 && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-rose-50 text-rose-600 border border-rose-100 inline-flex items-center gap-1"><BellRing size={9} />关联告警 {relAlerts.length} 条</span>}
    </div>
    {relAlerts.length > 0 && (
      <div className="mb-2 space-y-1 border border-rose-100 rounded-lg p-2 bg-rose-50/30">
        {relAlerts.map((x, i) => (
          <div key={i} className="flex items-center gap-1.5 text-[10px] text-[#6B7280]">
            <span className={`font-semibold px-1 py-0.5 rounded shrink-0 ${SEV_CLS[x.severity || ''] || 'bg-gray-100 text-gray-600'}`}>{x.severity}</span>
            <span className="font-mono shrink-0">{x.node}</span>
            <span className="truncate">{x.summary}</span>
          </div>
        ))}
      </div>
    )}
    </>
  );
}

// ═══ 申请列表行 (左侧紧凑行) ═══
function ApprovalListRow({ a, active, onClick }: { a: any; active: boolean; onClick: () => void }) {
  const st = STATUS_META[a.status] || STATUS_META.pending;
  return (
    <button onClick={onClick}
      className={`w-full text-left px-3.5 py-3 rounded-xl border transition-colors ${active ? 'border-[#E11D48] bg-rose-50/60' : 'border-[#E5E7EB] bg-white hover:border-[#D1D5DB]'}`}>
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${st.dot}`} />
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full shrink-0 ${CATEGORY_META[a.category || 'daily']?.cls || ''}">{catLabel(a.category)}</span>
          <span className="text-xs font-semibold font-mono text-[#111827] truncate">{a.tool_name}</span>
        </div>
        <span className="text-[10px] text-[#9CA3AF] shrink-0">{fmtTime(a.created_ts).slice(5)}</span>
      </div>
      {a.summary && <div className="text-xs text-[#6B7280] truncate">{a.summary}</div>}
      {a.status === 'pending' && (Date.now() / 1000 - parseFloat(a.created_ts || '0')) > 86400 && (
        <div className="mt-1 inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-red-50 text-red-600 border border-red-100">
          <Clock size={9} />超时待审 {Math.floor((Date.now() / 1000 - parseFloat(a.created_ts || '0')) / 3600)}h
        </div>
      )}
    </button>
  );
}

// ═══ 申请详情 (右侧) ═══
// 流程轨迹时间线 (审计事件: 创建/编辑/逐级决策/终审/撤销/CITP)
const AUDIT_ACT_META: Record<string, { label: string; cls: string }> = {
  approval_created: { label: '提交申请', cls: 'bg-sky-500' },
  approval_updated: { label: '编辑单据', cls: 'bg-violet-500' },
  approval_level: { label: '逐级通过', cls: 'bg-emerald-500' },
  approval_decision: { label: '终审决策', cls: 'bg-emerald-600' },
  approval_approved: { label: '已批准', cls: 'bg-emerald-600' },
  approval_rejected: { label: '已拒绝', cls: 'bg-red-500' },
  approval_cancelled: { label: '申请人撤销', cls: 'bg-gray-400' },
  approval_citp_generated: { label: 'CITP 生成', cls: 'bg-amber-500' },
  approval_citp_exported: { label: 'CITP 导出', cls: 'bg-amber-400' },
};

function ApprovalTimeline({ approvalId }: { approvalId: string }) {
  const [events, setEvents] = useState<ApprovalHistoryEvent[]>([]);
  useEffect(() => {
    let alive = true;
    setEvents([]);
    getApprovalHistory(approvalId)
      .then(d => { if (alive) setEvents(d.events || []); })
      .catch(() => {});
    return () => { alive = false; };
  }, [approvalId]);
  if (!events.length) return null;
  return (
    <div className="border border-[#E5E7EB] rounded-lg mb-3 bg-white overflow-hidden">
      <div className="px-3 py-2 bg-gray-50/70 border-b border-[#F3F4F6] text-xs font-semibold text-[#374151] flex items-center gap-1.5">
        <History size={13} className="text-[#6B7280]" />流程轨迹 <span className="text-[10px] font-normal text-[#9CA3AF]">审计留痕 · {events.length} 个事件</span>
      </div>
      <div className="px-3.5 py-2.5 space-y-2">
        {events.map((e, i) => {
          const meta = AUDIT_ACT_META[e.action || ''] || { label: e.action || '事件', cls: 'bg-gray-400' };
          const ts = parseFloat(String(e.ts || 0));
          return (
            <div key={i} className="flex items-start gap-2.5">
              <span className={`w-2 h-2 rounded-full shrink-0 mt-1.5 ${meta.cls}`} />
              <span className="text-[10px] font-mono text-[#9CA3AF] shrink-0 mt-0.5 w-[112px]">{ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-'}</span>
              <span className="text-[11px] font-medium text-[#374151] shrink-0">{meta.label}</span>
              <span className="text-[11px] text-[#9CA3AF] min-w-0 break-all">{e.username && e.username !== 'anonymous' ? `@${e.username} · ` : ''}{e.summary}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ApprovalDetail({ a, canDecide, busy, onDecide, isMine, myUsername, onCancel, onRollback, onEdit, onExecute, onRefresh, busyAction }: {
  a: any; canDecide: boolean; busy: boolean;
  onDecide: (aid: string, decision: 'approve' | 'reject') => void;
  isMine: boolean; myUsername: string;
  onCancel: (aid: string) => void; onRollback: (a: any) => void;
  onEdit: (a: any) => void; onExecute: (aid: string) => void;
  onRefresh: () => void; busyAction: boolean;
}) {
  if (!a) {
    return (
      <div className="h-full bg-white border border-dashed border-[#E5E7EB] rounded-xl flex flex-col items-center justify-center text-center px-8">
        <FileText size={36} className="text-[#D1D5DB] mb-3" />
        <p className="text-sm text-[#6B7280]">点击左侧申请查看详情</p>
        <p className="text-xs text-[#9CA3AF] mt-1">发起申请请点击右上角「新建申请」</p>
      </div>
    );
  }
  const st = STATUS_META[a.status] || STATUS_META.pending;
  const decided = a.status && a.status !== 'pending';
  return (
    <div className="bg-white border border-[#E5E7EB] rounded-xl p-5 shadow-sm">
      <div className="flex items-center justify-between mb-3 pb-3 border-b border-[#F3F4F6]">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded-full ${st.cls}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${st.dot}`} />{st.label}
          </span>
          <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border ${CATEGORY_META[a.category || 'daily']?.cls}`}>
            {a.category === 'maintenance' ? <Wrench size={9} /> : null}{catLabel(a.category)}
          </span>
          <span className="font-semibold font-mono text-sm text-[#111827]">{a.tool_name}</span>
          {!Object.prototype.hasOwnProperty.call(OP_HINTS, a.tool_name) && (
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-purple-50 text-purple-600">自定义</span>
          )}
          {a.agent === 'manual' && <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-blue-50 text-blue-600">用户申请</span>}
        </div>
        <span className="text-[10px] text-[#D1D5DB] font-mono shrink-0">{a.approval_id}</span>
      </div>
      {a.original_approval_id && (
        <div className="flex items-center gap-1.5 mb-2 text-[11px] px-2.5 py-1.5 rounded-lg bg-amber-50 text-amber-800 border border-amber-100">
          <RotateCcw size={12} />回退申请 · 关联原单 <span className="font-mono font-semibold">{a.original_approval_id}</span>
        </div>
      )}
      {a.summary && <div className="text-sm text-[#111827] mb-3 leading-snug">{a.summary}</div>}
      <BizFieldBadges a={a} />
      {a.status === 'cancelled' && a.cancel_reason && (
        <div className="text-xs rounded-lg px-3 py-2 mb-2 bg-gray-50 text-[#6B7280]">
          <span className="font-medium text-[#374151]">撤销原因：</span>{a.cancel_reason}
          {a.cancelled_ts && <span className="ml-2 text-[#9CA3AF]">· {fmtTime(a.cancelled_ts)}</span>}
        </div>
      )}
      {a.approvers && <div className="text-xs text-[#9CA3AF] mb-2">指定审批人 <span className="ml-1.5 font-medium text-[#374151]">@{a.approvers.replace(/[,，]/g, ' @')}</span></div>}
      {(a.approver_ds || a.approver_tech || a.approver_admin) && (() => {
        // 分级审批流进度: done=✓绿, 当前待审=amber 脉冲, 未到=灰
        const done = new Set((a.done_levels || '').split(',').filter(Boolean));
        const lvls = ([['ds', 'DS 配置复核', a.approver_ds], ['tech', '技术审批', a.approver_tech], ['admin', '行政审批', a.approver_admin]] as const).filter(x => x[2]);
        const curKey = a.status === 'pending' ? lvls.find(x => !done.has(x[0]))?.[0] : undefined;
        return (
          <div className="flex items-center gap-1.5 flex-wrap mb-2 p-2.5 rounded-lg bg-gray-50 border border-[#F3F4F6]">
            <span className="text-[10px] font-semibold text-[#6B7280] shrink-0">审批流</span>
            {lvls.map((x, i) => {
              const [k, lbl, names] = x;
              const isDone = done.has(k);
              const isCur = k === curKey;
              return (
                <span key={k} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-[#D1D5DB] text-[10px]">→</span>}
                  <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border ${
                    isDone ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : isCur ? 'bg-amber-50 border-amber-300 text-amber-700'
                    : 'bg-white border-[#E5E7EB] text-[#9CA3AF]'}`}>
                    {isDone ? '✓ ' : isCur ? '● ' : ''}{lbl}
                    <span className="font-mono opacity-70">@{names}</span>
                  </span>
                </span>
              );
            })}
            {a.level_log && <span className="text-[9px] text-[#9CA3AF] font-mono basis-full">{a.level_log}</span>}
          </div>
        );
      })()}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs mb-2">
        <div className="text-[#9CA3AF]">发起人<span className="ml-1.5 text-[#374151]">{a.username || '-'}</span></div>
        <div className="text-[#9CA3AF]">来源<span className="ml-1.5 text-[#374151]">{a.agent === 'manual' ? '用户申请' : (a.agent || '-')}</span></div>
        <div className="text-[#9CA3AF]">计划执行<span className="ml-1.5 text-[#374151]">{fmtPlanned(a.planned_ts)}</span></div>
        <div className="text-[#9CA3AF]">创建时间<span className="ml-1.5 text-[#374151]">{fmtTime(a.created_ts)}</span></div>
      </div>
      {a.params && a.params !== '{}' && (
        <div className="text-xs rounded-lg px-3 py-2 mb-2 bg-gray-50 text-[#6B7280] break-all"><span className="font-medium text-[#374151]">参数：</span><span className="font-mono">{a.params}</span></div>
      )}
      {a.impact_scope && <div className="text-xs rounded-lg px-3 py-2 mb-2 bg-gray-50 text-[#6B7280]"><span className="font-medium text-[#374151]">影响范围：</span>{a.impact_scope}</div>}
      {a.rollback_plan && <div className="text-xs rounded-lg px-3 py-2 mb-2 bg-gray-50 text-[#6B7280]"><span className="font-medium text-[#374151]">回退方案：</span>{a.rollback_plan}</div>}
      {a.risk_note && <div className="text-xs rounded-lg px-3 py-2 mb-2 bg-gray-50 text-[#6B7280]"><span className="font-medium text-[#374151]">风险说明：</span>{a.risk_note}</div>}
      {(() => {
        // CITP 展示 (维护审批): 设备匹配 + 客户列表 + Plan/Check_list (按真实模板格式)
        try {
          if (a.category !== 'maintenance' || !a.citp) return null;
          const citp = JSON.parse(a.citp);
          const devs = citp.devices || [citp.device].filter(Boolean);
          const plan = Array.isArray(citp.plan) ? citp.plan : [];
          const checklist = citp.checklist || [];
          const custs = citp.customers || [];
          return (
            <div className="border border-amber-200 rounded-lg overflow-hidden mb-3 bg-amber-50/30">
              <div className="px-3 py-2 bg-amber-50 border-b border-amber-200 flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold text-amber-800 flex items-center gap-1.5"><FileCheck2 size={13} />CITP 维护测试计划</span>
                <span className="text-[10px] font-mono text-amber-700">{citp.reference_number || citp.citp_no}</span>
                {citp.fault_type && <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">{citp.fault_type}</span>}
                {citp.window && <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 border border-teal-100">{citp.window}</span>}
                <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${(citp.matched_count ?? 0) > 0 ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>
                  设备匹配 {citp.matched_count ?? 0} / {citp.total_targets ?? devs.length}
                </span>
                {(() => {
                  // 网卡验收按钮: ESXi 主机设备 (未验收/可重测)
                  const _dev0 = citp.device || {};
                  return _dev0.platform === 'esxi' ? (
                    <button onClick={async () => {
                      if (!window.confirm(`对 ${_dev0.name || '该主机'} 执行网卡验收测试? (结果将写入 CITP 验收记录)`)) return;
                      try {
                        const d = await runAcceptanceTest(a.approval_id);
                        onRefresh();
                        window.alert(`网卡验收完成: ${d.acceptance.overall_status === 'green' ? '✅ 通过' : d.acceptance.overall_status === 'red' ? '❌ 失败' : '⚠ 有告警项'}\n通过 ${d.acceptance.pass_n ?? 0} / 告警 ${d.acceptance.warn_n ?? 0} / 失败 ${d.acceptance.fail_n ?? 0}\n${d.acceptance.tested_at || ''}`);
                      } catch (e: any) { window.alert(`验收失败: ${e.message}`); }
                    }}
                      className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-medium border border-emerald-300 text-emerald-700 bg-white hover:bg-emerald-50 transition-colors shrink-0">
                      <CheckCircle2 size={11} />{citp.acceptance ? '重新验收' : '网卡验收'}
                    </button>
                  ) : null;
                })()}
                <button onClick={async () => {
                  // blob 下载 (端点仅认 Authorization 头, 直链 <a> 无法携带)
                  try {
                    const token = localStorage.getItem('zops_token') || '';
                    const res = await fetch(citpExportUrl(a.approval_id), { headers: { Authorization: `Bearer ${token}` } });
                    if (!res.ok) throw Error(`HTTP ${res.status}`);
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const el = document.createElement('a');
                    el.href = url;
                    el.download = `${citp.reference_number || citp.citp_no || a.approval_id}.xlsx`;
                    el.click();
                    URL.revokeObjectURL(url);
                  } catch (e: any) { alert(`导出失败: ${e.message}`); }
                }}
                  className="ml-auto flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-medium border border-amber-300 text-amber-800 bg-white hover:bg-amber-100 transition-colors">
                  <FileCheck2 size={11} />导出 xlsx
                </button>
              </div>
              {/* 设备信息行 */}
              <div className="px-3 pt-2 flex flex-wrap gap-1.5 text-[10px]">
                {devs.map((d: any, i: number) => d.matched ? (
                  <span key={i} className="px-2 py-0.5 rounded-full bg-white border border-emerald-100 text-[#374151]">
                    ✓ <span className="font-mono">{d.name || d.hostname || d.target}</span>
                    {d.sn && <span className="font-mono text-[#6B7280]"> · SN {d.sn}</span>}
                    {d.ip && <span className="font-mono text-[#6B7280]"> · {d.ip}</span>}
                    <span className="text-[#6B7280]"> · {d.vendor || '-'}/{d.model || '-'}</span>
                    {(d.rack || d.site) && <span className="text-[#6B7280]"> · {d.rack || d.site}</span>}
                  </span>
                ) : (
                  <span key={i} className="px-2 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-100">✗ {d.target} · {d.reason || '未匹配'}</span>
                ))}
                {(citp.vms_count ?? 0) > 0 && <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-100">VM {citp.vms_count} 台</span>}
                {custs.slice(0, 6).map((c: any, i: number) => (
                  <span key={i} className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-100">{c.name}{c.vms ? ` (${c.vms})` : ''}</span>
                ))}
                {custs.length > 6 && <span className="px-2 py-0.5 rounded-full bg-gray-50 text-[#6B7280] border border-gray-200">等 {custs.length} 家客户</span>}
              </div>
              {/* Plan 步骤表 (新模板结构) */}
              {plan.length > 0 && (
                <div className="p-3 pb-0 overflow-x-auto">
                  <div className="text-[11px] font-semibold text-[#374151] mb-1">Plan · Change Management</div>
                  <table className="w-full text-[11px] bg-white border border-amber-100 rounded">
                    <thead>
                      <tr className="bg-gray-50 text-[#6B7280]">
                        <th className="px-2 py-1 font-medium text-left">step</th>
                        <th className="px-2 py-1 font-medium text-left">time</th>
                        <th className="px-2 py-1 font-medium text-left">维修检修步骤</th>
                        <th className="px-2 py-1 font-medium text-left">操作人员</th>
                        <th className="px-2 py-1 font-medium text-left">执行命令</th>
                        <th className="px-2 py-1 font-medium text-left">Verify/test</th>
                        <th className="px-2 py-1 font-medium text-left">Fallback</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#F3F4F6]">
                      {plan.map((st: any, i: number) => (
                        <tr key={i}>
                          <td className="px-2 py-1 text-center">{st.step}</td>
                          <td className="px-2 py-1 whitespace-nowrap text-[#6B7280]">{st.time}</td>
                          <td className="px-2 py-1 text-[#111827] min-w-[240px] whitespace-pre-wrap">{st.desc}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{st.operator}</td>
                          <td className="px-2 py-1 font-mono text-[#6B7280] whitespace-pre-wrap">{st.command || '-'}</td>
                          <td className="px-2 py-1 text-[#6B7280]">{st.verify || '-'}</td>
                          <td className="px-2 py-1 text-[#6B7280]">{st.fallback || '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {checklist.length > 0 && (
                <div className="p-3 overflow-x-auto">
                  <div className="text-[11px] font-semibold text-[#374151] mb-1">Check_list · 验证项</div>
                  <table className="w-full text-[11px] bg-white border border-amber-100 rounded">
                    <thead>
                      <tr className="bg-gray-50 text-[#6B7280]">
                        <th className="px-2 py-1 font-medium text-left">Step</th>
                        <th className="px-2 py-1 font-medium text-left">Host</th>
                        <th className="px-2 py-1 font-medium text-left">IP</th>
                        <th className="px-2 py-1 font-medium text-left">Sub Step</th>
                        <th className="px-2 py-1 font-medium text-left">Test item</th>
                        <th className="px-2 py-1 font-medium text-left">Expected result</th>
                        <th className="px-2 py-1 font-medium text-left">Result</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#F3F4F6]">
                      {checklist.map((ck: any, i: number) => (
                        <tr key={i}>
                          <td className="px-2 py-1">{ck.step}</td>
                          <td className="px-2 py-1 font-mono">{ck.host || '-'}</td>
                          <td className="px-2 py-1 font-mono">{ck.ip || '-'}</td>
                          <td className="px-2 py-1">{ck.sub_step}</td>
                          <td className="px-2 py-1 min-w-[220px] whitespace-pre-wrap">{ck.item}</td>
                          <td className="px-2 py-1 text-[#6B7280] whitespace-pre-wrap">{ck.expected || '-'}</td>
                          <td className="px-2 py-1"></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {(() => {
                // 验收结果 (网卡测试写入 CITP 的验收记录)
                const acc = citp.acceptance as AcceptanceResult | undefined;
                if (!acc) return null;
                const st = acc.overall_status;
                return (
                  <div className="border border-emerald-200 rounded-lg p-3 bg-emerald-50/30">
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <span className="text-[11px] font-semibold text-emerald-800 flex items-center gap-1"><CheckCircle2 size={12} />验收结果 · 网卡测试</span>
                      <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${
                        st === 'green' ? 'bg-emerald-100 text-emerald-700'
                        : st === 'red' ? 'bg-red-100 text-red-600' : 'bg-amber-100 text-amber-700'}`}>
                        {st === 'green' ? '✅ 通过' : st === 'red' ? '❌ 失败' : '⚠ 有告警项'}
                      </span>
                      <span className="text-[10px] text-[#6B7280]">{acc.tested_at} · @{acc.by} · 通过 {acc.pass_n ?? 0}/告警 {acc.warn_n ?? 0}/失败 {acc.fail_n ?? 0}</span>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-[11px] bg-white border border-emerald-100 rounded">
                        <thead><tr className="bg-gray-50 text-[#6B7280]">
                          <th className="text-left px-2 py-1 font-medium">网卡</th>
                          <th className="text-left px-2 py-1 font-medium">vSwitch</th>
                          <th className="text-left px-2 py-1 font-medium">Link</th>
                          <th className="text-left px-2 py-1 font-medium">结论</th>
                          <th className="text-left px-2 py-1 font-medium">问题明细</th>
                        </tr></thead>
                        <tbody className="divide-y divide-[#F3F4F6]">
                          {(acc.nics || []).map((n, i) => (
                            <tr key={i}>
                              <td className="px-2 py-1 font-mono">{n.name}</td>
                              <td className="px-2 py-1">{n.vswitch || '-'}</td>
                              <td className="px-2 py-1">{n.link_up ? 'Up' : 'Down'}</td>
                              <td className={`px-2 py-1 font-medium ${n.summary === 'pass' ? 'text-emerald-600' : n.summary === 'warn' ? 'text-amber-600' : 'text-red-500'}`}>{n.summary}</td>
                              <td className="px-2 py-1 text-[#6B7280]">{(n.issues || []).join('; ') || '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              })()}
              {/* 旧结构兜底 (v1 CITP: 四段文本) */}
              {!plan.length && !checklist.length && citp.plan && typeof citp.plan === 'object' && (
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-2 p-3 pt-1">
                  {([['pre_check', '维护前检查'], ['steps', '执行步骤'], ['verify', '验证步骤'], ['rollback', '回退步骤']] as const).map(([k, lbl]) =>
                    citp.plan[k] ? (
                      <div key={k} className="text-xs rounded-lg px-2.5 py-2 bg-white border border-amber-100 text-[#6B7280] whitespace-pre-wrap">
                        <span className="font-medium text-[#374151] block mb-0.5">{lbl}</span>{citp.plan[k]}
                      </div>
                    ) : null)}
                </div>
              )}
            </div>
          );
        } catch { return null; }
      })()}
      <ApprovalTimeline approvalId={a.approval_id} />
      <div className="flex items-center justify-between gap-2 flex-wrap pt-3 border-t border-[#F3F4F6]">
        {decided ? (
          <span className="text-[11px] text-[#9CA3AF]">
            {a.status === 'cancelled'
              ? `申请人撤销 · ${fmtTime(a.cancelled_ts)} · 撤销已留痕`
              : `决策人 ${a.approver || '-'} · ${fmtTime(a.decided_ts)} · 决策仅流程留痕`}
          </span>
        ) : (
          <span className="text-[11px] text-[#9CA3AF] flex items-center gap-1">
            <ShieldAlert size={12} />{(a.approver_ds || a.approver_tech || a.approver_admin) && a.done_levels !== undefined
              ? `待 ${(a.done_levels || '').split(',').filter(Boolean).length === 0
                  ? (a.approver_ds ? 'DS 配置复核' : a.approver_tech ? '技术审批' : '行政审批')
                  : (([['ds', 'DS 配置复核', a.approver_ds], ['tech', '技术审批', a.approver_tech], ['admin', '行政审批', a.approver_admin]] as const)
                      .filter(x => x[2] && !(a.done_levels || '').split(',').includes(x[0]))[0]?.[1] || '下一级')} 级决策 · `
              : ''}系统不会自动执行
          </span>
        )}
        <div className="flex gap-2">
          {/* 我的申请 · 本人: pending 可编辑 (业务字段/审批流/CITP; 保存后审批流重置) */}
          {isMine && a.status === 'pending' && a.username === myUsername && (
            <button onClick={() => onEdit(a)}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium border border-sky-300 text-sky-700 hover:bg-sky-50 transition-colors">
              <Pencil size={13} />{a.edit_count ? `编辑 (已改 ${a.edit_count} 次)` : '编辑'}
            </button>
          )}
          {/* 我的申请 · 本人: pending 可撤销 (误提交回退) */}
          {isMine && a.status === 'pending' && a.username === myUsername && (
            <button onClick={() => onCancel(a.approval_id)} disabled={busyAction}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-50 transition-colors">
              <Undo2 size={13} />{busyAction ? '撤销中...' : '撤销申请'}
            </button>
          )}
          {/* 已批准单: 线下执行完成后确认 (生命周期闭环; 本人/同组/管理员) */}
          {a.status === 'approved' && (
            <button onClick={() => onExecute(a.approval_id)} disabled={busyAction}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium border border-emerald-300 text-emerald-700 hover:bg-emerald-50 disabled:opacity-50 transition-colors">
              <CheckCircle2 size={13} />{busyAction ? '确认中...' : '确认已执行'}
            </button>
          )}
          {/* 我的申请 · 本人: 已批准可发起回退申请 (关联原单走新审批流程) */}
          {isMine && a.status === 'approved' && a.username === myUsername && !a.original_approval_id && (
            <button onClick={() => onRollback(a)}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium border border-amber-300 text-amber-700 hover:bg-amber-50 transition-colors">
              <RotateCcw size={13} />申请回退
            </button>
          )}
          {canDecide && !decided && (
            <>
              <button onClick={() => onDecide(a.approval_id, 'reject')} disabled={busy}
                className="px-3 py-1.5 rounded-lg text-xs font-medium border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-50 transition-colors">拒绝</button>
              <button onClick={() => onDecide(a.approval_id, 'approve')} disabled={busy}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50 transition-colors">{busy ? '决策中...' : '批准（流程）'}</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══ 主页面: tab 切换 + 左列表右详情 + 新建按钮展开表单 ═══
export default function ApprovalsPage() {
  const [tab, setTab] = useState<'mine' | 'review'>('mine');
  const [catFilter, setCatFilter] = useState<'all' | 'daily' | 'maintenance'>('all');
  const [showForm, setShowForm] = useState(false);
  const [prefill, setPrefill] = useState<ApplyPrefill | null>(null);  // 回退申请预填
  const [editRecord, setEditRecord] = useState<any | null>(null);     // 编辑模式: 待审批单原始记录
  const [stats, setStats] = useState<any>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedId, setSelectedId] = useState('');
  const [mine, setMine] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState('');
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [me, setMe] = useState<any>(null);

  useEffect(() => {
    const reread = () => { try { setMe(JSON.parse(localStorage.getItem('zops_user') || '{}')); } catch { setMe({}); } };
    reread();
    // 窗口重新聚焦时重读登录态 (权限中途调整可及时反映)
    window.addEventListener('focus', reread);
    return () => window.removeEventListener('focus', reread);
  }, []);

  // toast 自动消失
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  const bizPerms: string[] = me?.biz_perms || [];
  // 提单全分组可见: 任何用户可查看审批管理 tab (只读透明); 决策按钮需 approval.decide
  const canDecide = me?.role === 'admin' || bizPerms.includes('approval.decide');
  const auditOnly = !canDecide;

  const load = useCallback(async (which: 'mine' | 'review' = 'mine') => {
    try {
      const byTsDesc = (arr: any[]) => [...arr].sort((a, b) =>
        parseFloat(String(b.created_ts || 0)) - parseFloat(String(a.created_ts || 0)));
      if (which === 'mine') {
        const d = await getMyApprovals();
        setMine(byTsDesc(d.mine || []));
      } else {
        const d = await getPendingApprovals();
        setPending(byTsDesc(d.pending || []));
      }
    } catch (e) { console.error('Failed to load approvals:', e); }
    setLoading(false);
  }, []);

  useEffect(() => { load('mine'); load('review'); getApprovalStats().then(setStats).catch(() => {}); }, [load, refreshKey]);
  // 审批管理 tab 下 10s 轮询待办池
  useEffect(() => {
    if (tab !== 'review') return;
    const t = setInterval(() => { if (document.visibilityState === 'visible') load('review'); }, 10000);
    return () => clearInterval(t);
  }, [tab, load]);

  const decide = async (aid: string, decision: 'approve' | 'reject') => {
    if (decision === 'reject' && !window.confirm('确认拒绝该流程申请?')) return;
    setBusyId(aid); setToast(null);
    try {
      const d = await decideApproval(aid, decision);
      // 分级审批流: 中间级通过 → 提示解锁下一级 (单据保持待审批)
      const text = d.staged
        ? `${d.level_done}已通过 · 待「${d.next_level}」级决策 · ${aid} 保持待审批 · 已写审计日志`
        : (d.note || `审批单 ${aid} ${d.decision === 'approved' ? '已批准（流程留痕）· 操作不会自动执行' : '已拒绝（流程留痕）'} · 决策人 ${d.approver} · 已写审计日志`);
      setToast({ ok: true, text });
      load('review'); load('mine');  // 选中态由修正 effect 自动跳到下一张待办
    } catch (error: any) {
      setToast({ ok: false, text: `决策失败: ${error.message}` });
    }
    setBusyId('');
  };

  // 申请人撤销待审批单 (临时操作回退: 误提交/计划变更; 撤销写审计日志)
  const cancel = async (aid: string) => {
    const reason = window.prompt('撤销原因 (将写入审计日志):', '误提交 / 计划变更');
    if (reason === null) return;
    setBusyId(aid); setToast(null);
    try {
      await cancelApproval(aid, reason.trim() || '申请人主动撤销');
      setToast({ ok: true, text: `审批单 ${aid} 已撤销 · 撤销记录已写入审计日志` });
      load('mine'); load('review');
    } catch (error: any) {
      setToast({ ok: false, text: `撤销失败: ${error.message}` });
    }
    setBusyId('');
  };

  // 已批准单发起回退申请: 预填原单业务字段并关联 (走新审批流程, 决策同样仅留痕)
  // 执行确认: 已批准单线下执行完成 (approved → executed, 审计留痕)
  const execute = async (aid: string) => {
    if (!window.confirm(`确认该变更已线下执行完成? (单号 ${aid}, 状态将变为「已执行」)`)) return;
    setBusyId(aid); setToast(null);
    try {
      const d = await confirmExecution(aid);
      setToast({ ok: true, text: `单据 ${aid} 已确认执行 · 已写入审计${d.note ? ' · ' + d.note : ''}` });
      load('mine'); load('review'); setRefreshKey(k => k + 1);
    } catch (error: any) {
      setToast({ ok: false, text: `执行确认失败: ${error.message}` });
    }
    setBusyId('');
  };

  // 编辑待审批单: 弹窗预填单据全字段 (PUT 提交, 审批流重置)
  const startEdit = (a: any) => {
    setPrefill(null);
    setEditRecord(a);
    setShowForm(true);
  };

  const rollback = (a: any) => {
    setPrefill({
      tool_name: a.tool_name,
      summary: `回退申请: 原单 ${a.approval_id} (${a.tool_name}) 已批准执行, 因 [请补充回退原因] 需回退该临时操作, 恢复目标 [请补充]`,
      site: a.site, customer: a.customer, customer_gid: a.customer_gid, case_no: a.case_no,
      original_approval_id: a.approval_id,
    });
    setShowForm(true);
    setTab('mine');
  };

  // 分类过滤: 全部 / 日常配置 / 维护 (单据缺失 category 字段按 daily 兼容)
  const catOf = (a: any) => a.category === 'maintenance' ? 'maintenance' : 'daily';
  const list = (tab === 'mine' ? mine : pending).filter(a => catFilter === 'all' || catOf(a) === catFilter);
  const selected = list.find(x => x.approval_id === selectedId) || null;

  // 选中态修正: 列表就绪/tab 切换/已决策单移出后, selectedId 失效时自动选中第一张
  useEffect(() => {
    if (selectedId && list.some(x => x.approval_id === selectedId)) return;
    setSelectedId(list[0]?.approval_id || '');
  }, [list, selectedId]);

  return (
    <div className="h-full flex flex-col bg-gray-50 overflow-hidden">
      <PageHeader icon={ShieldAlert} title="审批中心" subtitle="我的申请与审批管理统一入口 · 审批只走流程 · 决策永久写入审计日志" />
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {/* 工具栏: 视图 tab + 分类过滤 + 新建按钮 */}
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex items-center gap-1 bg-white border border-[#E5E7EB] rounded-lg p-1">
              <button onClick={() => setTab('mine')}
                className={`px-3.5 py-1.5 rounded-md text-xs font-medium transition-colors ${tab === 'mine' ? 'bg-[#E11D48] text-white' : 'text-[#6B7280] hover:text-[#111827]'}`}>
                我的申请{mine.length > 0 && <span className="ml-1 opacity-80">{mine.length}</span>}
              </button>
              <button onClick={() => setTab('review')}
                  className={`px-3.5 py-1.5 rounded-md text-xs font-medium transition-colors ${tab === 'review' ? 'bg-[#E11D48] text-white' : 'text-[#6B7280] hover:text-[#111827]'}`}>
                  审批管理{pending.length > 0 && <span className="ml-1 inline-flex items-center gap-1"><span className="w-1.5 h-1.5 bg-amber-400 rounded-full animate-pulse" />{pending.length}</span>}
                </button>
            </div>
            {/* 分类过滤: 日常配置审批 / 维护审批 */}
            <div className="flex items-center gap-1 bg-white border border-[#E5E7EB] rounded-lg p-0.5">
              {([['all', '全部'], ['daily', '日常配置'], ['maintenance', '维护']] as const).map(([v, lbl]) => (
                <button key={v} onClick={() => setCatFilter(v)}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors ${catFilter === v ? 'bg-[#111827] text-white' : 'text-[#6B7280] hover:text-[#111827]'}`}>
                  {lbl}
                </button>
              ))}
            </div>
          </div>
          <button onClick={() => { if (!showForm) setPrefill(null); setShowForm(v => !v); }}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-xs font-medium bg-[#E11D48] text-white hover:opacity-90 transition-opacity">
            <Plus size={13} />新建申请
          </button>
        </div>

        {/* 统计概览条 (审批管理视角; 管理员全局/普通用户本组) */}
        {stats && (stats.pending !== undefined) && (
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            {[
              ['待处理', stats.pending, 'bg-amber-50 text-amber-700 border-amber-200'],
              ['今日新建', stats.today, 'bg-sky-50 text-sky-700 border-sky-200'],
              ['本周新建', stats.week, 'bg-violet-50 text-violet-700 border-violet-200'],
              ['维护单', stats.maintenance, 'bg-orange-50 text-orange-700 border-orange-200'],
              ['日常单', stats.daily, 'bg-teal-50 text-teal-700 border-teal-200'],
              ['平均决策(h)', stats.avg_decide_hours, 'bg-emerald-50 text-emerald-700 border-emerald-200'],
            ].map(([label, val, cls]) => (
              <div key={String(label)} className={`px-3 py-1.5 rounded-lg border text-xs ${cls as string}`}>
                <span className="opacity-70">{label}</span>
                <span className="ml-1.5 font-bold">{String(val ?? 0)}</span>
              </div>
            ))}
            {(stats.slow_pending || []).length > 0 && (
              <div className="px-3 py-1.5 rounded-lg border text-xs bg-red-50 text-red-600 border-red-200">
                <span className="opacity-70">超时待审(&gt;24h)</span>
                <span className="ml-1.5 font-bold">{(stats.slow_pending || []).length}</span>
                <span className="ml-2 opacity-70 font-mono">{(stats.slow_pending || []).map((x: any) => x.tool_name).slice(0, 3).join('、')}{(stats.slow_pending || []).length > 3 ? '…' : ''}</span>
              </div>
            )}
            <span className="text-[10px] text-[#9CA3AF]">{stats.scope === 'all' ? '管理员全局视图' : '本组视图'}</span>
          </div>
        )}

        {/* 新建/编辑申请弹窗 (编辑模式预填待审批单, PUT 提交后审批流重置) */}
        {showForm && (
          <ApplyForm prefill={prefill} editRecord={editRecord}
            onClose={() => { setShowForm(false); setPrefill(null); setEditRecord(null); }}
            onSubmitted={(aid) => {
              const wasEdit = !!editRecord;
              setShowForm(false);
              setPrefill(null);
              setEditRecord(null);
              setRefreshKey(k => k + 1);  // 刷新我的申请列表
              setTab('mine');
              if (aid) {
                setSelectedId(aid);
                setToast({ ok: true, text: wasEdit
                  ? `审批单 ${aid} 已更新 · 分级审批流已重置从头审批`
                  : `申请已提交 · 单号 ${aid} · 审批仅流程留痕，批准后由运维人员线下执行` });
              }
            }} />
        )}

        {auditOnly && tab === 'review' && (
          <div className="flex items-center gap-2 mb-3 text-xs rounded-lg px-3.5 py-2.5 bg-sky-50 text-sky-700 border border-sky-100">
            <Eye size={14} />
            只读查看模式 · 仅显示本组及本人的提单（管理员全局可见）；当前账号未持有「审批决策」业务权限，如需决策请联系管理员在用户管理中授予
          </div>
        )}
        {toast && <div className="mb-3"><Toast toast={toast} /></div>}

        {/* 左列表 + 右详情 (固定视口高度, 表单展开时整页滚动) */}
        <div className="flex gap-4 h-[calc(100vh-215px)]">
          <div className="w-80 shrink-0 overflow-y-auto space-y-2">
            {loading ? (
              <div className="text-sm text-[#9CA3AF] py-8 text-center">加载中...</div>
            ) : list.length === 0 ? (
              <div className="bg-white border border-dashed border-[#E5E7EB] rounded-xl py-10 text-center">
                <FileText size={28} className="mx-auto mb-2 text-[#D1D5DB]" />
                <p className="text-xs text-[#6B7280]">{tab === 'mine' ? '暂无申请记录' : '暂无待办审批'}</p>
                {tab === 'mine' && <p className="text-[11px] text-[#9CA3AF] mt-1">点击右上角「新建申请」发起</p>}
              </div>
            ) : list.map(a => (
              <ApprovalListRow key={a.approval_id} a={a} active={a.approval_id === selectedId}
                onClick={() => setSelectedId(a.approval_id === selectedId ? '' : a.approval_id)} />
            ))}
          </div>
          <div className="flex-1 min-w-0 overflow-y-auto">
            <ApprovalDetail a={selected} canDecide={tab === 'review' && canDecide}
              busy={busyId !== '' && busyId === selectedId} onDecide={decide}
              isMine={tab === 'mine'} myUsername={me?.username || ''}
              onCancel={cancel} onRollback={rollback} onEdit={startEdit}
              onExecute={execute}
              onRefresh={() => setRefreshKey(k => k + 1)}
              busyAction={busyId !== '' && busyId === selectedId} />
          </div>
        </div>
      </div>
    </div>
  );
}
