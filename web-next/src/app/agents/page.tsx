'use client';

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Cpu, Brain, Database, BookOpen, Server, Network, Cloud, Code,
  Globe, HelpCircle, Wrench, Activity, CheckCircle2, XCircle,
  RefreshCw, ChevronDown, ChevronUp, Zap, Clock, Tag, AlertTriangle,
  Wifi, WifiOff, Settings2, BarChart3, Layers, MessageSquare,
  LayoutGrid, GitBranch
} from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';

// ═══ Types ═══
interface AgentStats { total_calls: number; success_rate: number; avg_latency_ms: number; }
interface AgentTool { name: string; description: string; }
interface AgentInfo {
  id: string; name: string; description: string;
  status: 'online' | 'offline'; process_alive: boolean;
  toolpool_registered: boolean; model: string;
  skills: string[]; tools: AgentTool[];
  capabilities: string[]; tool_count: number;
  bindings_keywords: string[]; enabled: boolean;
  stats: AgentStats;
}
interface AgentsResponse {
  agents: AgentInfo[]; online: number; total: number;
  global_model: string; error?: string; timestamp?: number;
}

// ═══ Icons & Colors ═══
const ICON_MAP: Record<string, React.ElementType> = {
  orchestrator: Brain, general_agent: HelpCircle, gcoc_agent: Database,
  kb_agent: BookOpen, ops_agent: Server, network_agent: Network,
  vm_agent: Cloud, dev_agent: Code, browser_agent: Globe, nms_agent: Activity,
};
const COLOR_MAP: Record<string, { main: string; light: string; ring: string }> = {
  orchestrator:   { main: '#8b5cf6', light: '#ede9fe', ring: 'ring-violet-400' },
  general_agent:  { main: '#06b6d4', light: '#cffafe', ring: 'ring-cyan-400' },
  gcoc_agent:     { main: '#ec4899', light: '#fce7f3', ring: 'ring-pink-400' },
  kb_agent:       { main: '#6366f1', light: '#e0e7ff', ring: 'ring-indigo-400' },
  ops_agent:      { main: '#f97316', light: '#ffedd5', ring: 'ring-orange-400' },
  network_agent:  { main: '#3b82f6', light: '#dbeafe', ring: 'ring-blue-400' },
  vm_agent:       { main: '#22c55e', light: '#dcfce7', ring: 'ring-green-400' },
  dev_agent:      { main: '#a855f7', light: '#f3e8ff', ring: 'ring-purple-400' },
  browser_agent:  { main: '#14b8a6', light: '#ccfbf1', ring: 'ring-teal-400' },
  nms_agent:      { main: '#0ea5e9', light: '#e0f2fe', ring: 'ring-sky-400' },
};
const DEF_COLOR = { main: '#6b7280', light: '#f3f4f6', ring: 'ring-gray-400' };

// Agent-to-Service mapping for architecture view
const AGENT_SERVICES: Record<string, string[]> = {
  orchestrator: [], general_agent: ['llm', 'kb'], gcoc_agent: ['llm', 'gcoc_info', 'gcoc_res'],
  kb_agent: ['llm', 'kb'], ops_agent: ['llm', 'gcoc_info', 'n8n'], network_agent: ['llm', 'gcoc_info'],
  vm_agent: ['llm', 'vmware', 'gcoc_res', 'n8n'], dev_agent: ['llm'], browser_agent: ['llm'],
  nms_agent: ['llm', 'gcoc_info'],
};
const SERVICES = [
  { id: 'llm', icon: Cpu, label: 'LLM API', color: '#10b981' },
  { id: 'kb', icon: BookOpen, label: '知识库', color: '#8b5cf6' },
  { id: 'gcoc_info', icon: Server, label: 'GCOC 信息', color: '#3b82f6' },
  { id: 'gcoc_res', icon: Database, label: 'GCOC 资源', color: '#6366f1' },
  { id: 'vmware', icon: Cloud, label: 'VMware', color: '#f97316' },
  { id: 'n8n', icon: Zap, label: 'N8N 工作流', color: '#ef4444' },
];


function getToken() {
  return typeof window !== 'undefined' ? localStorage.getItem('zops_token') || '' : '';
}

// ═══ Main Page ═══
export default function AgentsPage() {
  const [data, setData] = useState<AgentsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [view, setView] = useState<'arch' | 'cards'>('arch');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [countdown, setCountdown] = useState(15);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/agents/status', {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (res.status === 401) { setError('认证已过期'); return; }
      if (!res.ok) { setError(`HTTP ${res.status}`); return; }
      const json = await res.json();
      if (json.error) setError(json.error);
      setData(json);
    } catch (e: any) { setError(e.message || '网络错误'); }
    finally { setLoading(false); setCountdown(15); }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(() => setCountdown(p => { if (p <= 1) { load(true); return 15; } return p - 1; }), 1000);
    return () => clearInterval(t);
  }, [load]);

  const agents = data?.agents || [];
  const online = data?.online || 0;
  const total = data?.total || 0;

  return (
    <div className="h-full flex flex-col bg-gray-50">
      {/* Header */}
      <PageHeader
        icon={Cpu}
        title="Agent 集群"
        subtitle={`实时状态 · ${countdown}s 刷新`}
        accent="#7C3AED"
        iconBg="rgba(124,58,237,0.1)"
        actions={
          <>
            {/* View Toggle */}
            <div className="flex bg-gray-100 rounded-lg p-0.5">
              <button onClick={() => setView('arch')}
                className={`flex items-center gap-1 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                  view === 'arch' ? 'bg-white text-indigo-700 ' : 'text-[#6B7280] hover:text-[#374151]'}`}>
                <GitBranch size={13} /> 架构图
              </button>
              <button onClick={() => setView('cards')}
                className={`flex items-center gap-1 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                  view === 'cards' ? 'bg-white text-indigo-700 ' : 'text-[#6B7280] hover:text-[#374151]'}`}>
                <LayoutGrid size={13} /> 详情
              </button>
            </div>
            {/* Status */}
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold ${
              online === total && total > 0 ? 'bg-green-50 text-green-700 border border-green-200'
                : 'bg-red-50 text-red-700 border border-red-200'}`}>
              <span className={`w-2 h-2 rounded-full ${online === total && total > 0 ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
              {online}/{total}
            </div>
            <button onClick={() => load()} className="p-2 hover:bg-[#F3F4F6] rounded-lg text-[#9CA3AF]">
              <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
            </button>
          </>
        }
      />

      {/* Error */}
      {error && (
        <div className="mx-6 mt-3 bg-red-50 border border-red-200 rounded-lg px-4 py-2 flex items-center gap-2 text-sm text-red-700">
          <AlertTriangle size={16} /> {error}
          <button onClick={() => load()} className="ml-auto text-xs bg-[#FEF2F2] px-2 py-1 rounded">重试</button>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        <AnimatePresence mode="wait">
          {view === 'arch' ? (
            <motion.div key="arch" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="h-full">
              <ArchView agents={agents} globalModel={data?.global_model || ''} expandedId={expandedId} setExpandedId={setExpandedId} />
            </motion.div>
          ) : (
            <motion.div key="cards" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="h-full overflow-y-auto p-6">
              <CardsView agents={agents} data={data} loading={loading} expandedId={expandedId} setExpandedId={setExpandedId} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

// ═════════════════════════════════════════
// Architecture Topology View (数据驱动动画)
// ═════════════════════════════════════════
interface Line { id: string; x1: number; y1: number; x2: number; y2: number; color: string; online: boolean; calls: number; }

function ArchView({ agents, globalModel, expandedId, setExpandedId }: {
  agents: AgentInfo[]; globalModel: string;
  expandedId: string | null; setExpandedId: (id: string | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const orchRef = useRef<HTMLDivElement>(null);
  const agentRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const svcRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const [lines, setLines] = useState<Line[]>([]);
  const nonOrchAgents = useMemo(() => agents.filter(a => a.id !== 'orchestrator'), [agents]);
  const orchestrator = useMemo(() => agents.find(a => a.id === 'orchestrator'), [agents]);
  const onlineCount = agents.filter(a => a.status === 'online').length;

  // ═══ 数据驱动: 仅在有真实流量时显示动点 ═══
  // 追踪上次 total_calls 快照，对比当前值判断是否有新活动
  const prevStatsRef = useRef<Record<string, number>>({});
  const [activeAgents, setActiveAgents] = useState<Set<string>>(new Set());

  useEffect(() => {
    const prev = prevStatsRef.current;
    const newActive = new Set<string>();
    agents.forEach(a => {
      const prevCalls = prev[a.id] ?? a.stats.total_calls;
      if (a.stats.total_calls > prevCalls) {
        newActive.add(a.id);
      }
      prev[a.id] = a.stats.total_calls;
    });
    // 如果 orchestrator 有新调用，所有在线 agent 视为活跃
    if (newActive.has('orchestrator')) {
      agents.filter(a => a.status === 'online' && a.id !== 'orchestrator').forEach(a => newActive.add(a.id));
    }
    setActiveAgents(newActive);
    // 活跃状态 8 秒后自动清除 (一次性粒子动画结束)
    if (newActive.size > 0) {
      const timer = setTimeout(() => setActiveAgents(new Set()), 8000);
      return () => clearTimeout(timer);
    }
  }, [agents]);

  // 判断某条连线是否应该显示粒子动画
  const isLinkActive = (lineId: string): boolean => {
    if (activeAgents.size === 0) return false;
    const parts = lineId.split('-');
    if (parts[0] === 'o') {
      // orchestrator→agent 链路: agent 活跃时显示
      const agentId = parts.slice(1).join('-');
      return activeAgents.has(agentId);
    }
    // agent→service 链路: agent 活跃时显示
    const agentId = parts[0];
    return activeAgents.has(agentId);
  };

  useEffect(() => {
    const calc = () => {
      if (!containerRef.current || !orchRef.current) return;
      const cr = containerRef.current.getBoundingClientRect();
      const or2 = orchRef.current.getBoundingClientRect();
      const ox = or2.left + or2.width / 2 - cr.left;
      const oy = or2.bottom - cr.top;
      const nl: Line[] = [];
      nonOrchAgents.forEach(a => {
        const el = agentRefs.current[a.id];
        if (!el) return;
        const ar = el.getBoundingClientRect();
        const ax = ar.left + ar.width / 2 - cr.left;
        const ay = ar.top - cr.top;
        nl.push({ id: `o-${a.id}`, x1: ox, y1: oy, x2: ax, y2: ay, color: '#8b5cf6', online: a.status === 'online', calls: a.stats.total_calls });
        // Agent → Services
        const svcIds = AGENT_SERVICES[a.id] || [];
        svcIds.forEach(sid => {
          const sel = svcRefs.current[sid];
          if (!sel) return;
          const sr = sel.getBoundingClientRect();
          const sx = sr.left + sr.width / 2 - cr.left;
          const sy = sr.top - cr.top;
          const svc = SERVICES.find(s => s.id === sid);
          nl.push({ id: `${a.id}-${sid}`, x1: ax, y1: ar.bottom - cr.top, x2: sx, y2: sy, color: svc?.color || '#9ca3af', online: a.status === 'online', calls: a.stats.total_calls });
        });
      });
      setLines(nl);
    };
    // 图标入场为纯透明度动画（无位移），元素位置从渲染起即固定，
    // 线条可立即按准确坐标绘制；线条自身通过淡入延迟与图标错层出现
    if (agents.length === 0) return;
    calc();
    const ro = new ResizeObserver(calc);
    if (containerRef.current) ro.observe(containerRef.current);
    window.addEventListener('resize', calc);
    return () => { ro.disconnect(); window.removeEventListener('resize', calc); };
  }, [agents, nonOrchAgents]);

  const selected = expandedId ? agents.find(a => a.id === expandedId) : null;
  // 线宽基准：取调用量最大者做对数归一化
  const maxCalls = Math.max(1, ...lines.map(l => l.calls));

  return (
    <div ref={containerRef} className="h-full flex flex-col relative p-6 overflow-hidden">
      {/* SVG Lines — 图标以纯透明度入场、位置固定，线条按层延迟淡入 */}
      <svg className="absolute inset-0 w-full h-full pointer-events-none" style={{ zIndex: 1 }}>
        <defs>
          {lines.filter(l => l.online).map(l => (
            <linearGradient key={`g-${l.id}`} id={`grad-${l.id}`} x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor={l.color} stopOpacity="0.8" />
              <stop offset="100%" stopColor={l.color} stopOpacity="0.3" />
            </linearGradient>
          ))}
        </defs>
        {lines.map(l => {
          const midY = (l.y1 + l.y2) / 2;
          const pathD = `M${l.x1},${l.y1} C${l.x1},${midY} ${l.x2},${midY} ${l.x2},${l.y2}`;
          const reversePath = `M${l.x2},${l.y2} C${l.x2},${midY} ${l.x1},${midY} ${l.x1},${l.y1}`;
          // 线宽 ∝ log(调用量)：调用越多的线越粗，流速也越快
          const ratio = Math.log1p(l.calls) / Math.log1p(maxCalls);
          const w = l.online ? 1.5 + ratio * 3.5 : 1;
          const dash = 5 + w * 1.5;
          const gap = 10 + w * 2;
          const dur = Math.max(1, 2.4 - ratio * 1.4);
          const showParticle = isLinkActive(l.id);
          return (
            <g key={l.id}>
              {/* 底路径：静态显示，不做入场动画 */}
              <path
                d={pathD}
                fill="none" stroke={l.online ? `url(#grad-${l.id})` : '#d1d5db'}
                strokeWidth={w} opacity={l.online ? 0.9 : 0.4}
                strokeDasharray={l.online ? '0' : '4,4'}
              />
              {/* 持续流动的流量虚线 — 始终流动，越繁忙流动越快 */}
              {l.online && (
                <path d={pathD} fill="none" stroke={l.color} strokeWidth={Math.max(1.5, w * 0.8)}
                  strokeLinecap="round" strokeDasharray={`${dash} ${gap}`} opacity="0.6">
                  <animate attributeName="stroke-dashoffset" from="0" to={`-${dash + gap}`} dur={`${dur}s`} repeatCount="indefinite" />
                </path>
              )}
              {/* 请求粒子 — 新调用突发时高亮 */}
              {showParticle && (
                <circle r={2.5} fill={l.color} opacity="0.85">
                  <animateMotion dur="3s" repeatCount="1" fill="freeze" path={pathD} />
                </circle>
              )}
              {/* 响应粒子 — 延迟出现, 模拟数据返回 */}
              {showParticle && (
                <circle r={1.5} fill={l.color} opacity="0.5">
                  <animateMotion dur="2s" repeatCount="1" fill="freeze" begin="1.5s" path={reversePath} />
                </circle>
              )}
            </g>
          );
        })}
      </svg>

      {/* Entry Points — 组件静态显示，不做入场动画 */}
      <div className="flex justify-center gap-16 mb-3 relative z-10">
        <div className="flex flex-col items-center">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center shadow-sm bg-blue-500">
            <Globe className="text-white" size={20} />
          </div>
          <div className="mt-1.5 font-bold text-[#111827] text-xs">Web 控制台</div>
        </div>
        <div className="flex flex-col items-center">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center shadow-sm bg-green-500">
            <MessageSquare className="text-white" size={20} />
          </div>
          <div className="mt-1.5 font-bold text-[#111827] text-xs">飞书机器人</div>
        </div>
      </div>

      {/* Entry → Orchestrator connectors — 入口流量持续流动 */}
      <div className="flex justify-center h-10 relative z-0">
        <svg width="260" height="40" className="overflow-visible">
          <path d="M65,0 Q65,20 130,40" fill="none" stroke="#3b82f6" strokeWidth="1.5" opacity="0.5" />
          <path d="M195,0 Q195,20 130,40" fill="none" stroke="#10b981" strokeWidth="1.5" opacity="0.5" />
          {/* 持续流动的入口流量虚线 */}
          {orchestrator?.status === 'online' && (
            <>
              <path d="M65,0 Q65,20 130,40" fill="none" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round" strokeDasharray="4 7" opacity="0.7">
                <animate attributeName="stroke-dashoffset" from="0" to="-11" dur="1.6s" repeatCount="indefinite" />
              </path>
              <path d="M195,0 Q195,20 130,40" fill="none" stroke="#10b981" strokeWidth="1.5" strokeLinecap="round" strokeDasharray="4 7" opacity="0.7">
                <animate attributeName="stroke-dashoffset" from="0" to="-11" dur="1.8s" repeatCount="indefinite" />
              </path>
            </>
          )}
          {/* 仅在有活跃请求时显示入口粒子 */}
          {activeAgents.size > 0 && (
            <>
              <circle r="2.5" fill="#3b82f6" opacity="0.8">
                <animateMotion dur="2s" repeatCount="1" fill="freeze" path="M65,0 Q65,20 130,40" />
              </circle>
              <circle r="2" fill="#10b981" opacity="0.7">
                <animateMotion dur="2.5s" repeatCount="1" fill="freeze" begin="0.3s" path="M195,0 Q195,20 130,40" />
              </circle>
            </>
          )}
        </svg>
      </div>

      {/* Orchestrator */}
      <div className="flex justify-center mb-6 relative z-10">
        <motion.div ref={orchRef} whileHover={{ scale: 1.05 }}
          className={`w-28 p-3 rounded-lg border-2 shadow-xl flex flex-col items-center cursor-pointer transition-all ${
            orchestrator?.status === 'online' ? 'border-purple-500 bg-purple-50' : 'border-red-300 bg-red-50'}`}
          onClick={() => setExpandedId(expandedId === 'orchestrator' ? null : 'orchestrator')}>
          <div className="relative">
            <div className="w-10 h-10 rounded-lg bg-purple-100 flex items-center justify-center">
              <Brain className="text-purple-600" size={22} />
            </div>
            {orchestrator?.status === 'online' && (
              <span className="absolute -top-0.5 -right-0.5 flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500 border-2 border-white" />
              </span>
            )}
          </div>
          <div className="font-bold text-xs text-purple-700 mt-1 truncate w-full text-center">{orchestrator?.name || '调度中枢'}</div>
          {orchestrator && <div className="text-[9px] text-purple-500 font-mono mt-0.5">{orchestrator.stats.total_calls}调度</div>}
        </motion.div>
      </div>

      {/* Agents Row */}
      <div className="mb-6 relative z-10">
        <div className="flex justify-center gap-3 px-2">
          {nonOrchAgents.map((agent) => {
            const Icon = ICON_MAP[agent.id] || Cpu;
            const c = COLOR_MAP[agent.id] || DEF_COLOR;
            const isOn = agent.status === 'online';
            const isSel = expandedId === agent.id;
            return (
              <div key={agent.id} ref={el => { agentRefs.current[agent.id] = el; }}
                className="flex flex-col items-center w-[88px] flex-shrink-0">
                <div
                  className={`w-full h-[88px] p-2 rounded-lg border-2 cursor-pointer transition-all hover:shadow-lg flex flex-col items-center justify-center ${
                    !isOn ? 'opacity-50 grayscale' : ''} ${isSel ? 'shadow-lg scale-105' : ''}`}
                  style={{
                    borderColor: isSel ? c.main : isOn ? `${c.main}40` : '#e5e7eb',
                    backgroundColor: isSel ? `${c.main}10` : 'white',
                  }}
                  onClick={() => setExpandedId(isSel ? null : agent.id)}>
                  <div className="relative">
                    <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ backgroundColor: c.light }}>
                      <Icon size={16} style={{ color: c.main }} />
                    </div>
                    <div className={`absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full border-2 border-white ${isOn ? 'bg-green-500' : 'bg-red-400'}`} />
                  </div>
                  <div className="font-semibold text-[10px] text-[#111827] text-center mt-1 leading-tight truncate w-full px-0.5">{agent.name}</div>
                  <div className="text-[8px] text-[#9CA3AF] mt-0.5 font-mono">{agent.stats.total_calls}调用</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Services Row — 图标在上，标题移到底下避免被流线穿过 */}
      <div className="mt-auto relative z-10">
        <div className="flex justify-center gap-5">
          {SERVICES.map((svc) => {
            const Icon = svc.icon;
            return (
              <motion.div key={svc.id} ref={el => { svcRefs.current[svc.id] = el; }}
                whileHover={{ scale: 1.1 }} className="flex flex-col items-center">
                <div className="relative">
                  <div className="w-11 h-11 rounded-lg flex items-center justify-center "
                    style={{ backgroundColor: `${svc.color}12` }}>
                    <Icon size={18} style={{ color: svc.color }} />
                  </div>
                  <div className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full border-2 border-white bg-green-500" />
                </div>
                <span className="text-[10px] text-[#4B5563] mt-1.5 font-medium">{svc.label}</span>
              </motion.div>
            );
          })}
        </div>
        <div className="flex items-center justify-center gap-2 mt-3">
          <div className="h-px bg-gray-300 flex-1 max-w-24" />
          <span className="text-[10px] text-[#9CA3AF] font-medium flex items-center gap-1"><Cloud size={10} />外部服务 & 数据源</span>
          <div className="h-px bg-gray-300 flex-1 max-w-24" />
        </div>
        {/* 图例：放底部 */}
        <div className="flex justify-center mt-2">
          <span className="text-[9px] text-[#B7A8EC] bg-[#F5F3FF] px-1.5 py-0.5 rounded-full border border-[#EDE9FE]">线宽 ∝ 调用量</span>
        </div>
      </div>

      {/* Status Panel (top-right) */}
      <div className="absolute top-4 right-4 bg-white/90 backdrop-blur rounded-xl  border border-[#E5E7EB] p-3 w-40 z-20">
        <div className="text-xs font-semibold text-[#374151] mb-2 flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" /> 系统状态
        </div>
        <div className="space-y-1.5 text-xs">
          <div className="flex justify-between"><span className="text-[#6B7280]">Agents</span><span className="font-bold text-[#16A34A]">{onlineCount}/{agents.length}</span></div>
          <div className="flex justify-between"><span className="text-[#6B7280]">技能</span><span className="font-bold text-[#2563EB]">{agents.reduce((s, a) => s + a.tool_count, 0)}</span></div>
          <div className="flex justify-between"><span className="text-[#6B7280]">调用总量</span><span className="font-bold text-[#7C3AED]">{agents.reduce((s, a) => s + (a.stats?.total_calls || 0), 0)}</span></div>
          <div className="flex justify-between"><span className="text-[#6B7280]">模型</span><span className="font-mono text-[10px] text-purple-600 truncate max-w-[80px]">{globalModel?.split('/').pop()}</span></div>
        </div>
      </div>

      {/* Detail Popup */}
      <AnimatePresence>
        {selected && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 20 }}
            className="absolute bottom-4 left-4 right-4 z-30 bg-white/95 backdrop-blur-sm rounded-xl shadow-lg border border-[#E5E7EB] p-4">
            <AgentDetailPanel agent={selected} onClose={() => setExpandedId(null)} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function AgentDetailPanel({ agent, onClose }: { agent: AgentInfo; onClose: () => void }) {
  const c = COLOR_MAP[agent.id] || DEF_COLOR;
  const Icon = ICON_MAP[agent.id] || Cpu;
  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ backgroundColor: c.light }}>
            <Icon size={16} style={{ color: c.main }} />
          </div>
          <div>
            <span className="font-bold text-sm text-[#111827]">{agent.name}</span>
            <span className={`ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold ${agent.status === 'online' ? 'bg-[#F0FDF4] text-green-700' : 'bg-[#FEF2F2] text-[#DC2626]'}`}>{agent.status.toUpperCase()}</span>
          </div>
        </div>
        <button onClick={onClose} className="text-[#9CA3AF] hover:text-[#4B5563]"><XCircle size={18} /></button>
      </div>
      <p className="text-xs text-[#6B7280] mb-3">{agent.description}</p>
      <div className="grid grid-cols-5 gap-4 text-xs">
        <div><span className="text-[#9CA3AF]">调用</span><div className="font-bold text-[#2563EB]">{agent.stats.total_calls}</div></div>
        <div><span className="text-[#9CA3AF]">成功率</span><div className="font-bold text-[#16A34A]">{agent.stats.total_calls > 0 ? `${agent.stats.success_rate.toFixed(0)}%` : '-'}</div></div>
        <div><span className="text-[#9CA3AF]">延迟</span><div className="font-bold">{fmtLatency(agent.stats.avg_latency_ms)}</div></div>
        <div><span className="text-[#9CA3AF]">技能</span><div className="font-bold text-[#7C3AED]">{agent.tool_count}</div></div>
        <div><span className="text-[#9CA3AF]">模型</span><div className="font-mono text-[10px] text-[#4B5563] truncate">{agent.model?.split('/').pop()}</div></div>
      </div>
      {agent.skills.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-3">{agent.skills.map((s, i) => (
          <span key={i} className="px-2 py-0.5 text-[10px] font-mono rounded border" style={{ backgroundColor: `${c.main}08`, borderColor: `${c.main}25`, color: c.main }}>{s}</span>
        ))}</div>
      )}
    </div>
  );
}

// ═════════════════════════════════════════
// Cards Detail View
// ═════════════════════════════════════════
function CardsView({ agents, data, loading, expandedId, setExpandedId }: {
  agents: AgentInfo[]; data: AgentsResponse | null; loading: boolean;
  expandedId: string | null; setExpandedId: (id: string | null) => void;
}) {
  const totalCalls = agents.reduce((s, a) => s + (a.stats?.total_calls || 0), 0);
  const totalTools = agents.reduce((s, a) => s + (a.tool_count || 0), 0);
  return (
    <>
      <div className="grid grid-cols-4 gap-4 mb-6">
        <MiniSummary icon={Wifi} label="在线" value={`${data?.online || 0}/${data?.total || 0}`} color="emerald" />
        <MiniSummary icon={BarChart3} label="总调用" value={totalCalls} color="blue" />
        <MiniSummary icon={Layers} label="总技能" value={totalTools} color="violet" />
        <MiniSummary icon={Settings2} label="模型" value={data?.global_model?.split('/').pop() || '-'} color="amber" small />
      </div>
      <div className="grid grid-cols-3 gap-5">
        {agents.map(agent => {
          const Icon = ICON_MAP[agent.id] || Cpu;
          const c = COLOR_MAP[agent.id] || DEF_COLOR;
          const isOn = agent.status === 'online';
          const exp = expandedId === agent.id;
          return (
            <div key={agent.id} className={`rounded-lg border transition-all ${exp ? 'col-span-3 shadow-xl' : 'hover:shadow-lg hover:-translate-y-0.5'} ${isOn ? 'bg-white border-[#E5E7EB] ' : 'bg-red-50/50 border-red-200'}`}>
              <div className="p-4 cursor-pointer" onClick={() => setExpandedId(exp ? null : agent.id)}>
                <div className="flex items-start gap-3">
                  <div className="relative flex-shrink-0">
                    <div className={`w-13 h-13 rounded-lg flex items-center justify-center ring-2 ring-offset-2 ${isOn ? c.ring : 'ring-red-300'}`} style={{ backgroundColor: c.light }}>
                      <Icon size={22} style={{ color: c.main }} />
                    </div>
                    <span className="absolute -top-0.5 -right-0.5 flex h-3 w-3">
                      {isOn && <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />}
                      <span className={`relative inline-flex rounded-full h-3 w-3 border-2 border-white ${isOn ? 'bg-green-500' : 'bg-red-400'}`} />
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2"><span className="font-bold text-sm text-[#111827]">{agent.name}</span>
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${isOn ? 'bg-[#F0FDF4] text-green-700' : 'bg-[#FEF2F2] text-[#DC2626]'}`}>{isOn ? 'ONLINE' : 'OFFLINE'}</span>
                    </div>
                    <p className="text-xs text-[#6B7280] line-clamp-1">{agent.description}</p>
                  </div>
                  <div className="text-[#9CA3AF]">{exp ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</div>
                </div>
                <div className="grid grid-cols-4 gap-2 mt-3">
                  <StatChip icon={<BarChart3 size={11} />} label="调用" value={agent.stats.total_calls > 0 ? String(agent.stats.total_calls) : '-'} color="text-[#2563EB] bg-blue-50" />
                  <StatChip icon={<Activity size={11} />} label="成功率" value={agent.stats.total_calls > 0 ? `${agent.stats.success_rate.toFixed(0)}%` : '-'} color="text-[#16A34A] bg-green-50" />
                  <StatChip icon={<Clock size={11} />} label="延迟" value={fmtLatency(agent.stats.avg_latency_ms)} color="text-[#6B7280] bg-[#F9FAFB]" />
                  <StatChip icon={<Wrench size={11} />} label="技能" value={String(agent.tool_count)} color="text-[#7C3AED] bg-violet-50" />
                </div>
              </div>
              {exp && (
                <div className="border-t border-[#F3F4F6] px-5 pb-4 pt-3 bg-[#F9FAFB]/50 rounded-b-xl">
                  <div className="grid grid-cols-3 gap-4 text-xs">
                    <div>
                      <div className="font-semibold text-[#4B5563] mb-2 flex items-center gap-1"><CheckCircle2 size={12} /> 运行状态</div>
                      <div className="space-y-1.5">
                        <DRow label="进程" value={agent.process_alive ? '运行中' : '已停止'} ok={agent.process_alive} />
                        <DRow label="ToolPool" value={agent.toolpool_registered ? '已注册' : '未注册'} ok={agent.toolpool_registered} neutral={!agent.toolpool_registered} />
                        <DRow label="启用" value={agent.enabled ? '是' : '否'} ok={agent.enabled} />
                        <p className="font-mono text-[10px] text-[#6B7280] bg-white px-2 py-1 rounded border truncate mt-1">{agent.model}</p>
                      </div>
                    </div>
                    <div>
                      <div className="font-semibold text-[#4B5563] mb-2 flex items-center gap-1"><Zap size={12} /> 技能</div>
                      <div className="flex flex-wrap gap-1">{agent.skills.map((s, i) => (
                        <span key={i} className="px-2 py-0.5 text-[10px] font-mono rounded border" style={{ backgroundColor: `${c.main}08`, borderColor: `${c.main}25`, color: c.main }}>{s}</span>
                      ))}{agent.skills.length === 0 && <span className="text-[#9CA3AF] text-[10px] italic">无</span>}</div>
                    </div>
                    <div>
                      <div className="font-semibold text-[#4B5563] mb-2 flex items-center gap-1"><Tag size={12} /> 触发词</div>
                      <div className="flex flex-wrap gap-1">{agent.bindings_keywords.slice(0, 12).map((kw, i) => (
                        <span key={i} className="px-1.5 py-0.5 bg-white text-[#4B5563] rounded border border-[#E5E7EB] text-[10px]">{kw}</span>
                      ))}{agent.bindings_keywords.length > 12 && <span className="text-[10px] text-[#9CA3AF]">+{agent.bindings_keywords.length - 12}</span>}</div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {agents.length === 0 && !loading && <div className="text-center text-[#9CA3AF] py-20"><Cpu size={48} className="mx-auto mb-3 opacity-30" /><p>暂无 Agent</p></div>}
    </>
  );
}

// ═══ Sub Components ═══
// 延迟智能格式化：分钟/秒/毫秒
function fmtLatency(ms: number): string {
  if (!ms || ms <= 0) return '-';
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}min`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}
function StatChip({ icon, label, value, color }: { icon: React.ReactNode; label: string; value: string; color: string }) {
  return <div className={`flex items-center gap-1.5 px-2.5 py-2 rounded-lg text-xs ${color}`}>{icon}<span className="font-bold">{value}</span><span className="text-[10px] opacity-60">{label}</span></div>;
}
function MiniSummary({ icon: Icon, label, value, color, small }: { icon: any; label: string; value: string | number; color: string; small?: boolean }) {
  const g: Record<string, string> = { emerald: 'bg-emerald-500', blue: 'bg-blue-500', violet: 'bg-violet-500', amber: 'bg-amber-500' };
  return (
    <div className="bg-white rounded-xl border border-[#E5E7EB] p-4 flex items-center gap-4  hover:shadow-md hover:-translate-y-1 transition-all group">
      <div className={`w-9 h-9 rounded-lg ${g[color] || g.blue} flex items-center justify-center flex-shrink-0 transition-transform`}><Icon size={20} className="text-white" /></div>
      <div className="min-w-0"><div className={`${small ? 'text-sm font-mono' : 'text-xl'} font-bold text-[#111827] truncate`}>{value}</div><div className="text-xs text-[#6B7280] mt-0.5">{label}</div></div>
    </div>
  );
}
function DRow({ label, value, ok, neutral }: { label: string; value: string; ok: boolean; neutral?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[#6B7280]">{label}</span>
      <span className={`flex items-center gap-1 font-medium ${neutral ? 'text-[#9CA3AF]' : ok ? 'text-[#16A34A]' : 'text-red-500'}`}>
        {neutral ? <span className="w-1.5 h-1.5 rounded-full bg-gray-300" /> : ok ? <CheckCircle2 size={11} /> : <XCircle size={11} />}{value}
      </span>
    </div>
  );
}
