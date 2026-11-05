'use client';

/**
 * ChatGPT 式对话主界面 (task-c17 前端重写)
 * 布局: 左侧会话栏 (260px 可折叠) + 沉浸对话区 (max-w-3xl 居中 + 底部自增高 composer)
 * 保留全部既有能力: SSE 流式/思考链折叠/停止/断线补读/幂等键/技能·记忆徽章/会话管理
 */
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  getSessions, deleteSession, renameSession, getUsage,
} from '@/lib/api';
import { Sidebar as SysSidebar } from '@/components/Sidebar';
import { useResizableWidth } from '@/components/chat/ResizeHandle';
import { MarkdownContent } from '@/components/MarkdownContent';
import BrowserLivePanel from '@/components/chat/BrowserLivePanel';
import { BrowserConnectCard } from '@/components/chat/BrowserConnectCard';
import { ComputerUseMessage } from '@/components/chat/ComputerUseMessage';
import { cuRequest, validateCuDevices, type CuDevices } from '@/components/ComputerUseSettings';
import {
  MessageSquare,
  ArrowUp, Square, Copy, Check, Brain, Cpu, ChevronDown, ChevronRight, Sparkles, PenLine, Monitor,
  Plus, Search, Trash2, PanelRight, Wrench, Users, Link2, X, FileText, Zap,
  Database, BookOpen,
} from 'lucide-react';

/* ───────────────────────── 类型 ───────────────────────── */
interface Msg {
  id: string; role: 'user' | 'assistant' | 'error' | 'system';
  content: string; thinking?: string; streaming?: boolean;
  agent?: string; agents?: string[]; skills?: string[]; memory_used?: boolean;
  usage?: { input_tokens?: number; output_tokens?: number };
  ts?: string; progress?: string;
  kb_source?: boolean; data_source?: string; data_count?: number;
  model?: string; file_url?: string; file_name?: string;
  all_shots?: string[];   // CU 页面快照 (右侧调用详情卡片展示)
  ask?: { question?: string; options?: string[]; input?: string };      // 结构化问询卡片 (chat harness 通用)
  cu_ask?: { question?: string; options?: string[]; input?: string };  // 旧协议兼容
  cu_backend?: 'companion' | 'extension' | 'server';
  device_id?: string; cu_task_id?: string;
  need_extension?: boolean;              // 需用户本机浏览器才能继续 → 渲染连接引导卡片
  login_handoff?: boolean;               // 等待用户在自己的浏览器里完成登录
  steps?: { t: string; text: string; source?: string;    // 执行过程事件流 (工具/子代理 tab 数据源)
    seq?: number; tool?: string; args?: Record<string, unknown>;  // 工具调用详情 (可折叠展开)
    result?: string; ok?: boolean; secs?: number; result_len?: number }[];
}
interface Sess { id: string; title: string; updated_at?: string; message_count?: number; }

export function isCuMessage(m: Msg): boolean {
  return !!(m.cu_backend || m.cu_task_id || m.cu_ask || m.agent === 'computer_agent' ||
    m.agents?.includes('computer_agent') || m.steps?.some(s => s.source === 'computer_agent') ||
    m.need_extension || m.login_handoff || m.all_shots?.length || /\/api\/cu\//i.test(m.file_url || ''));
}

// 保留任务元数据；通用历史/补读助手会裁剪 CU 字段，不能用于任务归属恢复。
export async function readSessionHistory(sessionId: string, signal: AbortSignal): Promise<any[]> {
  const data = await cuRequest<any>(`/api/chat/messages/${encodeURIComponent(sessionId)}`, signal);
  if (data?.session_id && data.session_id !== sessionId) throw new Error('历史会话绑定不匹配');
  const list = Array.isArray(data) ? data : data?.messages;
  if (!Array.isArray(list)) throw new Error('历史消息格式不正确');
  return list.filter(m => m && !(m.session_id && m.session_id !== sessionId) &&
    !(m.metadata?.session_id && m.metadata.session_id !== sessionId));
}

export async function* streamSessionReply(payload: Record<string, unknown>, signal: AbortSignal): AsyncGenerator<any> {
  signal.throwIfAborted();
  const token = localStorage.getItem('zops_token');
  if (!token) throw new Error('登录已失效，请重新登录');
  const request = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const abort = () => { request.abort(); void reader?.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  let timeout = setTimeout(abort, 60000);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST', signal: request.signal, credentials: 'include', cache: 'no-store',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('') }, body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`聊天请求失败（${response.status}）`);
    reader = response.body?.getReader();
    if (!reader) throw new Error('聊天响应流不可用');
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (request.signal.aborted) throw new Error('连接中断，请核对任务状态，不要重复提交');
      clearTimeout(timeout); timeout = setTimeout(abort, 60000);
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length > 1_000_000) throw new Error('聊天事件超出安全大小');
      const lines = buffer.split('\n'); buffer = done ? '' : lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (raw === '[DONE]') return;
        let event;
        try { event = JSON.parse(raw); } catch { continue; }
        if (event && typeof event.type === 'string') yield event;
      }
      if (done) return;
    }
  } finally {
    clearTimeout(timeout); signal.removeEventListener('abort', abort); request.abort();
    try { await reader?.cancel(); } catch { /* 已关闭 */ }
    reader?.releaseLock();
  }
}

export async function* recoverSessionReply(taskId: string, signal: AbortSignal): AsyncGenerator<any> {
  let cursor = 0;
  for (let attempt = 0; attempt < 90; attempt++) {
    const data = await cuRequest<any>(`/api/chat/tasks/${encodeURIComponent(taskId)}/events?after=${cursor}`, signal);
    for (const event of data.events || []) {
      cursor = Math.max(cursor, event.seq || 0);
      if (event.type === 'reply' || event.type === 'progress') yield { ...event.data, type: event.type, event_id: event.event_id };
      else if (event.type === 'task_terminal') {
        if (event.data?.error) yield { type: 'error', text: event.data.error };
        yield { type: 'done' };
      }
    }
    if (data.terminal) return;
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(new Error('补读已取消')); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 2000);
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
  }
  throw new Error('事件补读超时，请核对任务状态');
}

const AGENTS = [
  { id: '', label: '智能助手', icon: Sparkles, desc: '自动路由到最合适的专家' },
  { id: 'opscore_agent', label: '数据查询', icon: Cpu, desc: 'VM/备份/CMDB 实时生产数据' },
  { id: 'ops_agent', label: '运维诊断', icon: PenLine, desc: '主机检查/日志分析/健康报告' },
  { id: 'kb_agent', label: '知识库', icon: MessageSquare, desc: '内部知识库检索问答' },
  { id: 'dev_agent', label: '脚本执行', icon: PenLine, desc: '脚本生成与登记执行' },
  { id: 'computer_agent', label: '电脑操作', icon: Monitor, desc: 'Companion 本机任务 · 需明确选择设备' },
  { id: 'general_agent', label: '通用问答', icon: Sparkles, desc: '通用问答与知识增强' },
];

/* ───────────────────────── 主组件 ───────────────────────── */
export default function ChatApp() {
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [activeId, setActiveId] = useState<string>('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [loading, setLoading] = useState(false);
  const [agentId, setAgentId] = useState(() =>
    typeof window !== 'undefined' ? (localStorage.getItem('zops_pref_default_agent') || '') : '');
  const [agentMenu, setAgentMenu] = useState(false);
  const [recovery, setRecovery] = useState('');
  const [renamingId, setRenamingId] = useState('');
  const [renameVal, setRenameVal] = useState('');
  const [usage, setUsage] = useState<any>(null);
  const [draftText, setDraftText] = useState('');
  const [browserOpen, setBrowserOpen] = useState(false);
  const [extOnline, setExtOnline] = useState(false);
  const [cuBinding, setCuBinding] = useState<{ sessionId: string; taskId: string; deviceId: string } | null>(null);
  const sessionEpoch = useRef(0);
  const sessionRequests = useRef(new AbortController());
  const streamRequest = useRef<AbortController | null>(null);
  const stopRequest = useRef<AbortController | null>(null);
  const resetSessionRequests = useCallback(() => {
    sessionRequests.current.abort(); sessionRequests.current = new AbortController();
    streamRequest.current?.abort(); streamRequest.current = null;
    stopRequest.current?.abort(); stopRequest.current = null;
  }, []);
  const [sessionQ, setSessionQ] = useState('');
  // Codex 式右侧执行详情面板: 展示当前选中(或最新)回复的 工具/子代理/来源
  const [infoOpen, setInfoOpen] = useState(true);
  const [infoSel, setInfoSel] = useState('');
  const lastAutoRef = useRef('');
  const cuPanelRef = useRef(false);   // 本轮对话是否已自动开过 CU 小窗 (用户关掉后不再弹)
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlSession = searchParams.get('s') || '';
  const urlQuery = searchParams.get('q') || '';
  const qSent = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  /* 会话列表 (对话页专属左栏数据源) */
  const loadSessions = useCallback(async () => {
    try { setSessions((await getSessions()) as any); } catch { /* 网络异常 */ }
  }, []);
  useEffect(() => { loadSessions(); }, [loadSessions]);

  // 从能力中心跳转: 自动发送 q 参数作为首条消息
  useEffect(() => {
    if (urlQuery && !qSent.current) {
      qSent.current = true;
      setTimeout(() => send(urlQuery), 800);  // 等 UI 渲染完
    }
  }, [urlQuery]); // eslint-disable-line react-hooks/exhaustive-deps

  const scrollBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, []);

  /* 右侧面板自动跟踪最新一条 AI 回复 (用户点击旧消息后不打扰, 直到更新的回复出现) */
  useEffect(() => {
    const lastA = [...msgs].reverse().find(m => m.role !== 'user' && (m.content || (m.steps || []).length));
    if (lastA && lastA.id !== lastAutoRef.current) {
      lastAutoRef.current = lastA.id;
      setInfoSel(lastA.id);
    }
  }, [msgs]);
  const selectedInfo = useMemo(() =>
    msgs.find(m => m.id === infoSel && m.role !== 'user') ||
    [...msgs].reverse().find(m => m.role !== 'user') || null, [msgs, infoSel]);

  /* 载入会话历史 */
  const openSession = useCallback(async (sid: string) => {
    const epoch = ++sessionEpoch.current;
    resetSessionRequests();
    activeIdRef.current = sid;
    setActiveId(sid); setCuBinding(null); setBrowserOpen(false); setRecovery('');
    taskIdRef.current = ''; setLoading(false);
    setMsgs([]);
    try {
      const list = await readSessionHistory(sid, sessionRequests.current.signal);
      if (epoch !== sessionEpoch.current) return;
      const latestCu = [...list].reverse().find(m => (m.cu_backend || m.metadata?.cu_backend) === 'companion' && (m.cu_task_id || m.metadata?.cu_task_id));
      if (latestCu) setCuBinding({ sessionId: sid, taskId: latestCu.cu_task_id || latestCu.metadata.cu_task_id, deviceId: latestCu.device_id || latestCu.metadata?.device_id || '' });
      setMsgs(list.map(m => ({
        id: `${m.timestamp || ''}-${Math.random().toString(36).slice(2, 7)}`,
        role: m.role === 'user' ? 'user' : 'assistant',
        content: m.content || '', agent: m.source || m.agent, ts: m.timestamp,
        agents: m.agents, all_shots: m.all_shots, ask: m.ask || m.cu_ask, cu_ask: m.cu_ask,
        file_url: m.file_url, file_name: m.file_name,
        cu_backend: m.cu_backend || m.metadata?.cu_backend, need_extension: !!m.need_extension,
        device_id: m.device_id || m.metadata?.device_id, cu_task_id: m.cu_task_id || m.metadata?.cu_task_id,
        login_handoff: !!m.login_handoff,
        // 后端落库的过程事件 (10-19): 历史消息重开时 工具/子代理 tab 仍有数据
        // 工具详情字段 (seq/tool/args/result/ok/secs) 保留; 开始+完成两条按 seq 合并为一行 (与实时流一致)
        steps: ((m.steps || []) as any[]).reduce<any[]>((out, s) => {
          const st = {
            t: typeof s.t === 'number' ? new Date(s.t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : (s.t || ''),
            text: s.text || '', source: s.source || '',
            seq: s.seq, tool: s.tool, args: s.args, result: s.result, ok: s.ok, secs: s.secs, result_len: s.result_len,
          };
          if (s.seq != null && s.result !== undefined) {
            // 完成行合并回开始行; 精确匹配失败回退同 tool 最后未完成行
            let i = out.findIndex(o => o.seq === s.seq && o.tool === s.tool);
            if (i < 0) for (let j = out.length - 1; j >= 0; j--) {
              if (out[j].tool === st.tool && out[j].result === undefined) { i = j; break; }
            }
            if (i >= 0) { out[i] = { ...out[i], text: st.text, result: st.result, ok: st.ok, secs: st.secs, result_len: st.result_len }; return out; }
          }
          out.push(st); return out;
        }, []),
      })));
      scrollBottom();
      getUsage(sid).then(data => { if (epoch === sessionEpoch.current) setUsage(data); }).catch(() => {});
    } catch { /* 404 等 */ }
  }, [scrollBottom, resetSessionRequests]);
  // URL 会话响应式: 侧栏点击/前进后退都触发; 无 s 参数 = 新对话 (重置)
  // URL 会话响应式: 侧栏点击/前进后退都触发; 无 s 参数 = 新对话 (重置)。
  // 关键: 首条消息后 replaceState('/?s=新会话') 会让 urlSession 追上 activeId —
  // 此时跳过重载, 否则 openSession 会用服务端历史整组替换内存消息, 丢失
  // steps 执行轨迹/agents 等仅在本地存在的过程数据 (工具/子代理 tab 消失)。
  const activeIdRef = useRef('');
  activeIdRef.current = activeId;
  useEffect(() => {
    if (urlSession) {
      if (urlSession !== activeIdRef.current) openSession(urlSession);
    } else {
      sessionEpoch.current += 1; activeIdRef.current = ''; resetSessionRequests();
      setActiveId(''); setMsgs([]); setCuBinding(null); setBrowserOpen(false); setRecovery('');
      taskIdRef.current = ''; setLoading(false);
    }
  }, [urlSession, openSession, resetSessionRequests]);
  useEffect(() => () => { sessionEpoch.current += 1; resetSessionRequests(); }, [resetSessionRequests]);


  /* 发送 (幂等 + 流式 + 补读 + 停止) */
  const send = useCallback(async (text: string) => {
    if (!text.trim() || loading || streamRequest.current) return;
    const controller = new AbortController(); streamRequest.current = controller;
    const epoch = sessionEpoch.current;
    let streamSession = activeId;
    setLoading(true);
    setMsgs(p => [...p,
      { id: `u-${Date.now()}`, role: 'user', content: text, ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }) },
      { id: `a-${Date.now()}`, role: 'assistant', content: '', streaming: true, agent: agentId || undefined },
    ]);
    scrollBottom();

    let taskId = '', gotDone = false, lastNote = '';

    const patch = (fn: (m: Msg) => Msg) => {
      if (epoch !== sessionEpoch.current) return;
      setMsgs(p => {
        if (epoch !== sessionEpoch.current || !p.length) return p;
        const u = [...p]; u[u.length - 1] = fn(u[u.length - 1]); return u;
      });
    };
    const bindCu = (chunk: any) => {
      const data = { ...(chunk.metadata || {}), ...chunk };
      const isCu = data.cu_backend === 'companion' || data.source === 'computer_agent' ||
        data.agents?.includes('computer_agent') || !!data.cu_task_id;
      const id = data.cu_task_id || (data.type === 'progress' && isCu ? data.task_id : '');
      if (isCu && typeof id === 'string' && id && streamSession) {
        data.cu_task_id = id;
        setCuBinding(previous => ({ sessionId: streamSession, taskId: id,
          deviceId: typeof data.device_id === 'string' && data.device_id ? data.device_id
            : previous?.sessionId === streamSession && previous.taskId === id ? previous.deviceId : '' }));
      }
      return data;
    };

    try {
      const payload: any = { message: text, target: 'manager' };
      if (agentId) payload.target_agent = agentId;
      if (activeId) payload.session_id = activeId;
      cuPanelRef.current = false;

      for await (const raw of streamSessionReply(payload, controller.signal)) {
        if (epoch !== sessionEpoch.current) break;
        const eventSession = raw.session_id || (raw as any).metadata?.session_id;
        if (eventSession && streamSession && eventSession !== streamSession) continue;
        if (raw.type === 'start' && raw.session_id && !streamSession) streamSession = raw.session_id;
        const c = bindCu(raw);
        if (c.cu_backend || c.cu_task_id || c.source === 'computer_agent' || c.cu_ask) {
          patch(m => ({ ...m, cu_backend: c.cu_backend || m.cu_backend, cu_task_id: c.cu_task_id || m.cu_task_id,
            device_id: c.device_id || m.device_id, cu_ask: c.cu_ask || m.cu_ask,
            agent: c.source === 'computer_agent' ? c.source : m.agent }));
        }
        if (c.type === 'start') {
          if (c.task_id) { taskId = c.task_id; taskIdRef.current = c.task_id; }
          if (c.session_id && !activeIdRef.current) {
            streamSession = c.session_id; activeIdRef.current = c.session_id;
            setActiveId(c.session_id); loadSessions();
            if (typeof window !== 'undefined')
              window.history.replaceState(null, '', `/?s=${c.session_id}`);
          }
        }
        if (c.type === 'progress') {
          lastNote = c.text || '';
          // 采集执行过程事件流 (供 工具/子代理 tab 展示); detail 携带工具调用详情
          const d = (c as any).detail as { seq?: number; tool?: string; args?: Record<string, unknown>;
            result?: string; ok?: boolean; secs?: number; result_len?: number } | undefined;
          const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
          patch(m => {
            const steps = [...(m.steps || [])];
            if (d && typeof d.seq === 'number' && d.result !== undefined) {
              // 工具完成事件: 合并到对应"正在查询"步骤 — 一次查询 = 一行 (展开可见参数+结果)
              // seq+tool 精确匹配失败时回退合并同 tool 最后一条未完成行 (容错旧数据 seq 错位)
              let i = steps.findIndex(s => s.seq === d.seq && s.tool === d.tool);
              if (i < 0) for (let j = steps.length - 1; j >= 0; j--) {
                if (steps[j].tool === d.tool && steps[j].result === undefined) { i = j; break; }
              }
              if (i >= 0) steps[i] = { ...steps[i], text: c.text || steps[i].text, result: d.result, ok: d.ok, secs: d.secs, result_len: d.result_len };
              else steps.push({ t: now(), text: c.text || '', source: (c as any).source, seq: d.seq, tool: d.tool, args: d.args, result: d.result, ok: d.ok, secs: d.secs, result_len: d.result_len });
            } else {
              steps.push({ t: now(), text: c.text || '', source: (c as any).source,
                seq: d?.seq, tool: d?.tool, args: d?.args });
            }
            return { ...m, progress: c.text, steps,
              agent: (c as any).source === 'computer_agent' ? (c as any).source : m.agent,
              cu_backend: c.cu_backend || m.cu_backend, cu_task_id: c.cu_task_id || m.cu_task_id, device_id: c.device_id || m.device_id };
          });
          // 只展开任务视图，画面仍须通过会话、任务与设备绑定校验。
          if ((c as any).source === 'computer_agent' && !cuPanelRef.current) {
            if (!c.cu_task_id) setCuBinding(null);
            cuPanelRef.current = true;
            setBrowserOpen(true);
          }
        }
        if (c.type === 'delta') {
          if (c.kind === 'thinking') patch(m => ({ ...m, thinking: (m.thinking || '') + c.text! }));
          else patch(m => ({ ...m, content: m.content + c.text!, progress: '' }));
          scrollBottom();
        }
        if (c.type === 'reply' && c.text) {
          gotDone = true;
          // 连接信息不表示执行已授权，具体状态由任务接口确认。
          if ((c as any).cu_backend || (c as any).need_extension) setBrowserOpen(true);
          patch(m => ({
            ...m, content: c.text!, streaming: false, agent: c.source || m.agent,
            agents: c.agents, skills: (c as any).skills, all_shots: (c as any).all_shots,
            ask: (c as any).ask || (c as any).cu_ask, cu_ask: c.cu_ask || m.cu_ask,
            cu_backend: c.cu_backend || m.cu_backend,
            device_id: c.device_id || m.device_id, cu_task_id: c.cu_task_id || m.cu_task_id,
            need_extension: !!(c as any).need_extension,
            login_handoff: !!(c as any).login_handoff,
            memory_used: (c as any).memory_used, usage: c.usage,
            kb_source: c.kb_source, data_source: c.data_source,
            file_url: c.file_url, file_name: c.file_name,
            data_count: c.data_count, model: (c as any).model,
            ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
          }));
          scrollBottom();
        }
        if (c.type === 'error') {
          patch(m => ({ ...m, content: c.text || '出错了', streaming: false, role: 'error' }));
        }
        if (c.type === 'done') gotDone = true;
      }
      if (epoch !== sessionEpoch.current) return;
      if (!gotDone && taskId) {
        setRecovery('连接中断，仅补读已发生事件，不重新执行任务…');
        try {
          for await (const c of recoverSessionReply(taskId, controller.signal)) {
            if (epoch !== sessionEpoch.current) break;
            const eventSession = c.session_id || c.metadata?.session_id;
            if (eventSession && streamSession && eventSession !== streamSession) continue;
            const data = bindCu(c);
            if (c.type === 'reply' && c.text) patch(m => ({ ...m, content: c.text!, streaming: false, agent: c.source || m.agent,
              agents: c.agents || m.agents, ask: c.ask || c.cu_ask, cu_ask: c.cu_ask || m.cu_ask,
              need_extension: c.need_extension, login_handoff: c.login_handoff,
              cu_backend: data.cu_backend || m.cu_backend, device_id: data.device_id || m.device_id, cu_task_id: data.cu_task_id || m.cu_task_id }));
          }
        } catch { /* 保留已渲染 */ }
        setTimeout(() => { if (epoch === sessionEpoch.current) setRecovery(''); }, 4000);
      }
      loadSessions();
      // 用量统计实时刷新 (会话内发消息后立即更新, 不必重开会话)
      const sid = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('s') : '';
      if (sid) getUsage(sid).then(data => { if (epoch === sessionEpoch.current) setUsage(data); }).catch(() => {});
    } catch (e: any) {
      patch(m => ({ ...m, content: `请求失败: ${e.message}`, streaming: false, role: 'error' }));
    }
    if (streamRequest.current === controller) streamRequest.current = null;
    patch(m => ({ ...m, streaming: false }));
    if (epoch === sessionEpoch.current) { taskIdRef.current = ''; setLoading(false); }
  }, [loading, agentId, activeId, loadSessions, scrollBottom]);

  const stop = useCallback(async () => {
    if (!taskIdRef.current || stopRequest.current) return;
    const controller = new AbortController(); stopRequest.current = controller;
    const epoch = sessionEpoch.current;
    try {
      const res = await cuRequest<{ cu?: { state?: string; confirmed?: boolean } | null }>(
        `/api/chat/tasks/${encodeURIComponent(taskIdRef.current)}/stop`, controller.signal, 'POST');
      if (epoch === sessionEpoch.current) {
        // 停止请求不等于已停止: 只有设备回执确认 cancelled 才算; 失联按未知处理。
        if (res?.cu?.confirmed) setRecovery('本机执行端已确认停止；已发出的外部操作不会自动撤销');
        else if (res?.cu?.state === 'unknown') setRecovery('停止结果未知（设备可能失联），请先在设备上核对，不自动重发');
        else if (res?.cu) setRecovery('已请求停止；执行端尚未确认，请在任务小窗核验');
        else setRecovery('已请求停止；当前任务没有本机执行记录，本次只取消对话生成');
      }
    } catch {
      if (!controller.signal.aborted && epoch === sessionEpoch.current) setRecovery('停止请求结果未知，请核对任务状态，不自动重发');
    } finally { if (stopRequest.current === controller) stopRequest.current = null; }
  }, []);
  const taskIdRef = useRef('');

  /* 会话操作 */
  const newChat = () => {
    sessionEpoch.current += 1; activeIdRef.current = ''; taskIdRef.current = ''; resetSessionRequests();
    setActiveId(''); setMsgs([]); setCuBinding(null); setBrowserOpen(false); setRecovery(''); setLoading(false);
  };
  const doRename = async (sid: string) => {
    const title = renameVal.trim();
    setRenamingId('');
    if (!title) return;
    try {
      await renameSession(sid, title);
      setSessions(p => p.map(s => s.id === sid ? { ...s, title } : s));
    } catch { /* 忽略 */ }
  };
  const removeSession = async (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('删除该会话?')) return;
    await deleteSession(sid);
    if (activeId === sid) newChat();
    loadSessions();
  };

  const cur = AGENTS.find(a => a.id === agentId) || AGENTS[0];
  const [user, setUser] = useState<any>({});
  useEffect(() => {
    try { setUser(JSON.parse(localStorage.getItem('zops_user') || '{}')); } catch { setUser({}); }
    const controller = new AbortController();
    let extTimer: ReturnType<typeof setTimeout>;
    let polling = false;
    const pollExt = async () => {
      if (polling || controller.signal.aborted) return;
      polling = true; clearTimeout(extTimer);
      try {
        const data = validateCuDevices(await cuRequest<CuDevices>('/api/cu/devices', controller.signal));
        const selected = data.devices.find(d => d.device_id === data.selected_device_id);
        if (!controller.signal.aborted) setExtOnline(!!selected?.online && !selected?.revoked);
      } catch { if (!controller.signal.aborted) setExtOnline(false); }
      finally { polling = false; if (!controller.signal.aborted) extTimer = setTimeout(pollExt, 20000); }
    };
    void pollExt();
    window.addEventListener('zops:cu-devices-changed', pollExt);
    fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUser((p: any) => ({ ...p, ...d })); })
      .catch(() => {});
    return () => { controller.abort(); clearTimeout(extTimer); window.removeEventListener('zops:cu-devices-changed', pollExt); };
  }, []);

  return (
    <div className="h-full flex bg-white text-[#0d0d0d]">
      {/* ═══ 会话面板 (智能对话页专属左栏; 全局导航侧栏由 AppShell 挂载) ═══ */}
      <SessionPanel
        sessions={sessions} activeId={activeId}
        q={sessionQ} onQ={setSessionQ}
        renamingId={renamingId} renameVal={renameVal}
        setRenameVal={setRenameVal} setRenamingId={setRenamingId}
        onRename={doRename}
        onSelect={(sid) => {
          // 直接加载消息, 不依赖路由生效 — router.push 存在两类静默 no-op:
          // ① push 相同 URL; ② 新建对话用 replaceState 后 Next 内部 searchParams
          //    仍是旧值, push 到"旧值"目标不触发导航 → urlSession 效应不 fire。
          // 故点击一律先 openSession, push 仅作 URL 同步 (收藏/前进后退用)。
          openSession(sid);
          router.push(`/?s=${sid}`);
        }}
        onDelete={removeSession}
        onNew={() => {
          // Next 静态页 router.push('/') 去参不导航 (实测 URL 卡在旧 ?s=) →
          // 直接重置状态 + replaceState 清 URL, 不依赖路由生效
          newChat();
          window.history.replaceState(null, '', '/');
        }}
      />

      {/* ═══ 主对话区 ═══ */}
      <div className="flex-1 flex flex-col min-w-0 relative">
        {/* 顶栏 */}
        <header className="h-13 shrink-0 flex items-center gap-2 px-3 py-2.5 border-b border-[#ececec] z-10">
          <div className="relative">
            <button onClick={() => setAgentMenu(v => !v)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium hover:bg-black/[0.04]">
              <cur.icon size={14} /> {cur.label} <ChevronDown size={13} className="text-[#8f8f8f]" />
            </button>
            {agentMenu && (
              <div className="absolute top-full mt-1 left-0 w-64 rounded-xl border border-[#ececec] bg-white shadow-lg py-1.5 z-20"
                   onClick={() => setAgentMenu(false)}>
                {AGENTS.map(a => (
                  <button key={a.id} onClick={() => { setAgentId(a.id); }}
                    className={`w-full flex items-start gap-2.5 px-3.5 py-2.5 text-left hover:bg-black/[0.03]
                      ${agentId === a.id ? 'bg-black/[0.04]' : ''}`}>
                    <a.icon size={15} className="mt-0.5 text-[#8f8f8f]" />
                    <div>
                      <div className="text-[13px] font-medium">{a.label}</div>
                      <div className="text-[11px] text-[#8f8f8f]">{a.desc}</div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="ml-auto text-[11px] text-[#b4b4b4]">{recovery}</div>
          <button onClick={() => setInfoOpen(v => !v)} title={infoOpen ? '收起执行详情' : '展开执行详情'}
            className={`ml-1 p-1.5 rounded-lg transition-colors ${infoOpen ? 'bg-black/[0.05] text-[#0d0d0d]' : 'text-[#8f8f8f] hover:bg-black/[0.04]'}`}>
            <PanelRight size={15} />
          </button>
        </header>

        {/* 消息区: 隐藏原生滚动条, 滚动指示由视口右缘的 EdgeScrollbar 呈现 */}
        <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto zops-hide-scroll">
          <div className="max-w-3xl mx-auto px-4 pb-10 pt-6">
            {msgs.length === 0 ? <Welcome onPick={send} /> : msgs.map((m, i) => (
              <MessageItem key={m.id} m={m} last={i === msgs.length - 1}
                selected={m.id === infoSel && m.role !== 'user'}
                onSelect={() => setInfoSel(m.id)}
                onAskReply={send}
                askAnswered={!!(m.ask || m.cu_ask) && msgs.slice(i + 1).some(x => x.role === 'user')} />
            ))}
          </div>
        </div>

        {/* Composer */}
        {usage && usage.total_calls > 0 && (
          <div className="shrink-0 px-6 py-1 flex items-center justify-center gap-4 text-[10px] text-[#b4b4b4]">
            <span>⚡ {usage.total_tokens >= 1000 ? `${(usage.total_tokens / 1000).toFixed(1)}k` : usage.total_tokens} tokens</span>
            <span>📞 {usage.total_calls} 次调用</span>
            {usage.success_rate != null && <span>✓ {Math.round(usage.success_rate * 100)}%</span>}
          </div>
        )}
        <CapabilityPanel onSend={send} onDraft={(t) => setDraftText(t)}
          extOnline={extOnline} browserOpen={browserOpen}
          onToggleBrowser={() => setBrowserOpen(v => !v)} />
        <Composer loading={loading} onSend={send} onStop={stop} canStop={!!taskIdRef.current} draftText={draftText} onDraftUsed={() => setDraftText("")} />
      </div>

      <BrowserLivePanel open={browserOpen} onClose={() => setBrowserOpen(false)} sessionId={activeId}
                        taskId={cuBinding?.sessionId === activeId ? cuBinding.taskId : undefined}
                        deviceId={cuBinding?.sessionId === activeId ? cuBinding.deviceId : undefined} />

      {/* ═══ 右侧调用详情卡片: 占位的动态容器 — 展开时对话区收缩左移, 不重叠遮挡 ═══ */}
      {infoOpen && (
        <InfoCard msg={selectedInfo} onClose={() => setInfoOpen(false)} />
      )}

      {/* 视口右缘滚动指示条 (原生滚动条已隐藏) */}
      <EdgeScrollbar targetRef={scrollRef} />
    </div>
  );
}

/* ───────────────────────── 会话面板 (智能对话页专属左栏, 与全局导航分离) ───────────────────────── */
function SessionPanel({ sessions, activeId, q, onQ, renamingId, renameVal,
                        setRenameVal, setRenamingId, onRename, onSelect, onDelete, onNew }: {
  sessions: Sess[]; activeId: string; q: string; onQ: (v: string) => void;
  renamingId: string; renameVal: string; setRenameVal: (v: string) => void;
  setRenamingId: (v: string) => void; onRename: (sid: string) => void;
  onSelect: (sid: string) => void; onDelete: (sid: string, e: React.MouseEvent) => void;
  onNew: () => void;
}) {
  const list = sessions.filter(s =>
    !q.trim() || (s.title || '').toLowerCase().includes(q.trim().toLowerCase()));
  // 可拖拽调宽 (200-480, 默认 260)
  const { width, handle } = useResizableWidth('zops_sessions_width', 200, 520, 300);
  return (
    <aside className="shrink-0 flex flex-col bg-[#fafafa] border-r border-[#ececec] relative"
           style={{ width: `${width}px` }}>
      {handle}
      <div className="p-2.5 space-y-1.5">
        <button onClick={onNew}
          className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-xl text-[13px] font-medium
                     bg-[#0d0d0d] text-white hover:bg-[#2a2a2a] transition-colors">
          <Plus size={14} /> 新建对话
        </button>
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl border border-transparent focus-within:border-[#e3e3e3] bg-black/[0.02]">
          <Search size={13} className="text-[#8f8f8f] shrink-0" />
          <input value={q} onChange={e => onQ(e.target.value)} placeholder="搜索会话…"
            className="flex-1 bg-transparent outline-none text-[12.5px] placeholder:text-[#b4b4b4] min-w-0" />
          {q && <button onClick={() => onQ('')} className="text-[#b4b4b4] hover:text-[#0d0d0d] text-xs shrink-0">✕</button>}
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2 zops-hide-scroll">
        {list.length === 0 && (
          <div className="text-center py-8 text-[12px] text-[#b4b4b4]">{q ? '无匹配会话' : '暂无会话'}</div>
        )}
        {list.map(s => (
          <div key={s.id}
               onClick={() => renamingId !== s.id && onSelect(s.id)}
               className={`group flex items-start gap-2 px-3 py-2 rounded-xl cursor-pointer text-[13px] transition-colors
                 ${activeId === s.id ? 'bg-black/[0.06] font-medium' : 'hover:bg-black/[0.04]'}`}>
            <MessageSquare size={13} className="shrink-0 text-[#8f8f8f] mt-[3px]" />
            {renamingId === s.id ? (
              <input autoFocus value={renameVal}
                onChange={e => setRenameVal(e.target.value)}
                onClick={e => e.stopPropagation()}
                onKeyDown={e => {
                  if (e.key === 'Enter') onRename(s.id);
                  if (e.key === 'Escape') setRenamingId('');
                }}
                onBlur={() => onRename(s.id)}
                className="flex-1 bg-transparent outline-none border-b border-[#0d0d0d]/30 text-[13px]" />
            ) : (
              <div className="flex-1 min-w-0">
                <div className="line-clamp-2 leading-snug break-words" title={s.title || '新对话'}
                     onDoubleClick={() => { setRenamingId(s.id); setRenameVal(s.title || ''); }}>
                  {s.title || '新对话'}
                </div>
                <div className="text-[10.5px] text-[#b4b4b4] mt-0.5">
                  {s.updated_at}{s.message_count ? ` · ${s.message_count} 条` : ''}
                </div>
              </div>
            )}
            {renamingId !== s.id && (
              <>
                <button onClick={e => { e.stopPropagation(); setRenamingId(s.id); setRenameVal(s.title || ''); }}
                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-black/10 shrink-0" title="重命名">
                  <PenLine size={12} className="text-[#8f8f8f]" />
                </button>
                <button onClick={e => onDelete(s.id, e)}
                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-black/10 shrink-0" title="删除">
                  <Trash2 size={12} className="text-[#8f8f8f]" />
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}

/* ───────────────────────── ChatGPT 风格三点动画 ───────────────────────── */
function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 pl-8 py-2" aria-label="正在思考">
      <style jsx>{`
        @keyframes zops-dot {
          0%, 80%, 100% { transform: translateY(0); opacity: 0.3; }
          40% { transform: translateY(-6px); opacity: 1; }
        }
      `}</style>
      {[0, 1, 2].map(i => (
        <span key={i}
          style={{
            width: 8, height: 8, borderRadius: '50%', backgroundColor: '#0d0d0d',
            animation: `zops-dot 1.4s ease-in-out ${i * 0.16}s infinite`,
          }} />
      ))}
    </span>
  );
}

/* 状态指示器: 点动画 + 上下文提示 (路由中/思考中/生成中) */
function StatusIndicator({ phase, text }: { phase: 'routing' | 'thinking' | 'generating' | 'idle'; text?: string }) {
  const labels = {
    routing: '正在路由到专家…',
    thinking: '正在思考…',
    generating: '正在生成…',
    idle: text || '处理中…',
  };
  return (
    <div className="flex items-center gap-2 pl-8 py-2">
      <TypingDots />
      <span className="text-[13px] text-gray-400">{labels[phase]}</span>
    </div>
  );
}

/* ───────────────────────── 欢迎空态 ───────────────────────── */
function Welcome({ onPick }: { onPick: (t: string) => void }) {
  const SUGGEST = [
    '查一下 HKG 站点昨天的备份状态',
    '当前有哪些 Critical 级别的告警?',
    '虚拟机备份怎么核对?',
    '帮我梳理近期的高频故障模式',
  ];
  return (
    <div className="pt-[18vh] pb-10 text-center">
      <div className="w-14 h-14 mx-auto mb-5 rounded-2xl flex items-center justify-center"
           style={{ background: 'linear-gradient(135deg,#7C3AED22,#4F46E522)' }}>
        <Sparkles size={26} className="text-[#7C3AED]" />
      </div>
      <h1 className="text-2xl font-semibold mb-8">有什么可以帮忙？</h1>
      <div className="grid grid-cols-2 gap-2.5 max-w-xl mx-auto">
        {SUGGEST.map(s => (
          <button key={s} onClick={() => onPick(s)}
            className="text-left text-[13px] px-4 py-3 rounded-xl border border-[#ececec] hover:bg-black/[0.02] transition-colors">
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ───────────────────────── 消息条目 ───────────────────────── */
function MessageItem({ m, last, selected, onSelect, onAskReply, askAnswered }: {
  m: Msg; last: boolean; selected?: boolean; onSelect?: () => void; onAskReply?: (text: string) => void; askAnswered?: boolean;
}) {
  if (m.role === 'user') {
    return (
      <div className="flex justify-end mb-6">
        <div className="max-w-[75%] rounded-3xl bg-[#f4f4f4] px-4.5 py-2.5 px-5 text-[15px] leading-relaxed whitespace-pre-wrap">
          {m.content}
        </div>
      </div>
    );
  }
  return (
    <div onClick={onSelect}
      className={`mb-7 group rounded-xl -mx-2 px-2 transition-colors cursor-pointer
        ${selected ? 'bg-black/[0.03] ring-1 ring-black/[0.06]' : 'hover:bg-black/[0.015]'}`}
      title="点击后右侧面板展示该回复的执行详情">
      <div className="flex items-center gap-2 mb-1.5">
        <div className="w-6 h-6 rounded-full flex items-center justify-center shrink-0"
             style={{ background: isCuMessage(m) ? '#f3f4f6' : 'linear-gradient(135deg,#7C3AED,#4F46E5)' }}>
          {isCuMessage(m) ? <Monitor size={12} className="text-gray-600" /> : <Sparkles size={12} className="text-white" />}
        </div>
        <span className="text-[13px] font-medium">{(m.agent || 'assistant').replace('_agent', '')}</span>
        {selected && (
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-black/[0.05] text-[#6b6b6b]">详情展示中</span>
        )}
      </div>
      <MessageBody m={m} last={last} onAskReply={onAskReply} askAnswered={askAnswered} />
    </div>
  );
}

/* ───────────────────────── 问询信息卡片 ─────────────────────────
   agent 需要补充信息 (CU ask 等) 时在消息下方渲染: 预置选项点选 + 自由输入框,
   提交即作为下一条消息发送 (续跑路由会自动带回挂起任务)。 */
function AskCard({ ask, onReply, answered }: {
  ask: { question?: string; options?: string[]; input?: string };
  onReply: (text: string) => void;
  answered?: boolean;
}) {
  const [val, setVal] = useState('');
  const opts = (ask.options || []).filter(Boolean);
  const submit = (t: string) => {
    if (answered) return;
    const v = (t ?? val).trim();
    if (v) { onReply(v); setVal(''); }
  };
  return (
    <div className="ml-8 mt-3 rounded-2xl border border-indigo-100 bg-indigo-50/50 p-3.5">
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-[#0d0d0d] mb-2.5">
        <span className="w-5 h-5 rounded-full bg-indigo-100 flex items-center justify-center text-[11px]">❓</span>
        {ask.question || '需要补充信息'}
      </div>
      {opts.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2.5">
          {opts.map(o => (
            <button key={o} onClick={() => submit(o)}
              className="px-3 py-1.5 rounded-xl bg-white border border-indigo-200 text-[12.5px] text-indigo-700
                         hover:bg-indigo-100 transition-colors">
              {o}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-center gap-2">
        <input value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit(val); }}
          placeholder={ask.input ? `请输入${ask.input}…` : '输入补充信息…'}
          className="flex-1 px-3 py-2 rounded-xl bg-white border border-indigo-200 text-[13px] outline-none
                     focus:border-indigo-400 transition-colors" />
        <button onClick={() => submit(val)} disabled={!val.trim()}
          className="px-3.5 py-2 rounded-xl bg-indigo-600 text-white text-[12.5px] font-medium
                     hover:bg-indigo-500 disabled:opacity-30 transition-colors">
          提交
        </button>
      </div>
      {answered
        ? <div className="text-[10.5px] text-[#8f8f8f] mt-2">✓ 已答复，任务已续跑</div>
        : <div className="text-[10.5px] text-[#8f8f8f] mt-2">提交后将自动继续上一次任务</div>}
    </div>
  );
}

/* 参考来源块解析: 正文与来源链接分离 (链接进入右侧面板, 正文保持干净) */
function parseSources(content: string): { body: string; links: [string, string][] } {
  const sm = content.match(/\*\*📚\s*参考来源[：:]\*\*([\s\S]*)$/);
  if (!sm || sm.index == null) return { body: content, links: [] };
  return {
    body: content.slice(0, sm.index).trimEnd(),
    links: [...sm[1].matchAll(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g)].map(x => [x[1], x[2]] as [string, string]),
  };
}

function MessageBody({ m, last, onAskReply, askAnswered }: { m: Msg; last: boolean; onAskReply?: (text: string) => void; askAnswered?: boolean }) {
  const { body } = parseSources(m.content || '');
  return (
    <>
      {m.thinking && <Thinking thinking={m.thinking} streaming={!!(m.streaming && !m.content)} defaultOpen={typeof window !== 'undefined' && localStorage.getItem('zops_pref_show_thinking') !== '0'} />}
      {m.streaming && !m.content && !m.progress && !m.thinking && (
        <StatusIndicator phase="routing" />
      )}
      {m.progress && !m.content && (
        <div className="flex items-center gap-2 pl-8 py-1">
          <TypingDots />
          <span className="text-[13px] text-gray-500">{m.progress}</span>
        </div>
      )}
      {m.streaming && m.thinking && !m.content && (
        <div className="flex items-center gap-2 pl-8 py-1">
          <TypingDots />
          <span className="text-[13px] text-gray-400">正在深度思考…</span>
        </div>
      )}
      {body && (
        <div className={`pl-8 text-[15px] leading-[1.75] ${m.role === 'error' ? 'text-red-600' : ''}`}>
          {(isCuMessage(m) || m.streaming) ? <ComputerUseMessage content={body} /> : <MarkdownContent content={body} />}
          {m.streaming && last && (
            <span className="inline-flex items-center gap-[3px] ml-1.5 align-middle">
              <style jsx>{`
                @keyframes zops-dot-sm {
                  0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
                  40% { transform: translateY(-3px); opacity: 1; }
                }
              `}</style>
              {[0, 1, 2].map(i => (
                <span key={i} style={{
                  width: 4, height: 4, borderRadius: '50%', backgroundColor: '#0d0d0d',
                  animation: `zops-dot-sm 1.2s ease-in-out ${i * 0.12}s infinite`,
                }} />
              ))}
            </span>
          )}
        </div>
      )}
      {!m.streaming && m.content && (
        <div className="pl-8 mt-2 flex items-center gap-2.5 flex-wrap">
          <span className="text-[11px] text-[#b4b4b4]">{m.ts}</span>
          {m.usage?.output_tokens != null && (
            <span className="text-[11px] text-[#b4b4b4]">· {m.usage.output_tokens} tokens</span>
          )}
          {m.kb_source && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-50 text-indigo-600">📚 知识库</span>
          )}
          {m.data_source && (
            <span className="inline-flex items-center gap-1 text-[11px] text-[#8f8f8f]">
              <Cpu size={9} />{m.data_source}{m.data_count != null && <span>· {m.data_count}条</span>}
            </span>
          )}
          {m.model && (
            <span className="text-[11px] text-[#b4b4b4]">{String(m.model).split('/').pop()}</span>
          )}
          {m.cu_backend && (
            <span className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-600"
              title={m.cu_backend === 'companion' ? `设备 ${m.device_id || '待确认'} · 任务 ${m.cu_task_id || '待确认'}` : '旧版记录不提供实时画面或续跑入口'}>
              <Monitor size={9} />{m.cu_backend === 'companion' ? 'Companion 设备任务' : '旧版电脑操作记录'}
            </span>
          )}
          {(!isCuMessage(m) && (m.all_shots?.length || (m.file_url?.match(/\.(png|jpg)$/i) ? 1 : 0)))
            ? <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600">📸 页面快照 {(m.all_shots?.length || 1)} 张 · 见右侧调用详情</span>
            : null}
          {!isCuMessage(m) && m.file_url && !m.file_url.match(/\.(png|jpg)$/i) && (
            <a href={`${m.file_url}${m.file_url.includes('?') ? '&' : '?'}token=${typeof window !== 'undefined' ? localStorage.getItem('zops_token') || '' : ''}`}
               className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 hover:opacity-80">
              ⬇ {m.file_name || '下载文件'}
            </a>
          )}
          {m.skills && m.skills.length > 0 && (
            <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700"
                  title="已发布技能 (经生命周期审批)">
              <Cpu size={9} />{m.skills.slice(0, 2).join(', ')}
            </span>
          )}
          {m.memory_used && (
            <button onClick={() => window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'memory' } }))}
              className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-violet-50 text-violet-700 hover:opacity-80"
              title="已参考你的历史会话记忆 (仅本人, 点击管理)">
              <Brain size={9} /> 记忆
            </button>
          )}
          <CopyBtn text={m.content} />
        </div>
      )}
      {(m.need_extension || m.login_handoff) && <BrowserConnectCard />}
      {isCuMessage(m) && (m.ask || m.cu_ask) && <div className="ml-8 mt-3 rounded-xl border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600">{(m.ask || m.cu_ask)?.question || '任务需要确认'}。请在任务小窗核验，并在 Companion 本机安全继续；本页不会自动重放操作。</div>}
      {!isCuMessage(m) && (m.ask || m.cu_ask) && (
        <AskCard
          ask={m.ask || (m.cu_ask as any)}
          onReply={onAskReply || (() => {})}
          answered={askAnswered}
        />
      )}
    </>
  );
}

/* ───────────────────────── 右侧调用详情卡片 (Codex 式) ─────────────────────────
   浮动卡片出现在对话区右侧: 多级分层树展示当前选中(或自动跟踪的最新)回复的
   完整调用链 — 智能调度(orchestrator) → 子代理 → 各执行步骤 → 参考来源。
   点击任意 AI 回复切换展示对象; 顶栏按钮开关。 */

interface CallNode {
  id: string; kind: 'root' | 'agent' | 'step' | 'sources' | 'thinking';
  title: string; time?: string; link?: string;
  children?: CallNode[]; defaultOpen?: boolean;
  detail?: { tool: string; args?: Record<string, unknown>; result?: string; ok?: boolean; secs?: number; result_len?: number };
}

/* step 节点构造: 有工具详情 (tool) 的步骤可折叠展开参数/结果 */
function stepNode(s: NonNullable<Msg['steps']>[number], tag: string): CallNode {
  return {
    id: `st-${s.t}-${tag}${s.seq ?? ''}-${s.tool || ''}`,
    kind: 'step', title: s.text || '', time: s.t,
    detail: s.tool ? { tool: s.tool, args: s.args, result: s.result, ok: s.ok, secs: s.secs, result_len: s.result_len } : undefined,
  };
}

/* 由消息的 steps 时间线 + agents + 来源链接构建多级调用树:
   路由行(已路由到 X)开启一个 agent 子树, 该 agent 的后续步骤归入其下;
   orchestrator 自身步骤留在根层; 来源/思考为独立折叠组。 */
function buildCallTree(msg: Msg | null): CallNode[] {
  if (!msg) return [];
  const steps = msg.steps || [];
  const root: CallNode = { id: 'root', kind: 'root', title: '智能调度 orchestrator', children: [] };
  const agentNodes = new Map<string, CallNode>();

  const agentNodeOf = (name: string, time?: string): CallNode => {
    let n = agentNodes.get(name);
    if (!n) {
      n = { id: `ag-${name}`, kind: 'agent', title: name.replace('_agent', ''), time, children: [] };
      agentNodes.set(name, n);
      root.children!.push(n);
    }
    return n;
  };

  for (const s of steps) {
    const routed = /(?:已路由到|转发到|转发)\s*([a-zA-Z_][\w]*)/.exec(s.text || '');
    if (routed && (!s.source || s.source === 'orchestrator')) {
      agentNodeOf(routed[1], s.t);
    } else if (s.source && s.source !== 'orchestrator') {
      agentNodeOf(s.source).children!.push(stepNode(s, s.source));
    } else {
      root.children!.push(stepNode(s, 'o'));
    }
  }
  // agents 字段里有但 steps 未覆盖的 agent (如历史消息) → 空子树占位
  for (const a of msg.agents || []) if (!agentNodes.has(a)) agentNodeOf(a);

  const out: CallNode[] = [];
  if (steps.length || (msg.agents?.length || 0) > 0) out.push(root);
  if (msg.thinking) out.push({ id: 'think', kind: 'thinking', title: `深度思考 (${msg.thinking.length} 字)`, children: [] });
  return out;
}

/* Outputs 生成: 从当前回复产出文档 (客户端生成, 无需后端) */
function triggerDownload(name: string, content: BlobPart, type: string): { name: string; url: string } {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 8000);
  return { name, url };
}

function InfoCard({ msg, onClose }: { msg: Msg | null; onClose: () => void }) {
  const tree = useMemo(() => buildCallTree(msg), [msg]);
  const srcLinks = useMemo(() => msg && isCuMessage(msg) ? [] : parseSources(msg?.content || '').links, [msg]);
  const [outputs, setOutputs] = useState<Record<string, { name: string; url: string }[]>>({});
  const [plusOpen, setPlusOpen] = useState(false);
  const msgId = msg?.id || '';

  if (!msg) return null;
  const { body } = parseSources(msg.content || '');
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '').slice(0, 12);
  const meta = `- 助手: ${(msg.agent || 'assistant').replace('_agent', '')}\n- 生成时间: ${new Date().toLocaleString('zh-CN', { hour12: false })}`;
  const srcMd = srcLinks.length ? `\n\n---\n\n**参考来源**\n${srcLinks.map(l => `- [${l[0]}](${l[1]})`).join('\n')}` : '';

  const genMd = () => {
    const f = triggerDownload(`Z-OPS回复_${stamp}.md`, `# Z-OPS 对话产出\n\n${meta}\n\n---\n\n${body}${srcMd}\n`,
      'text/markdown;charset=utf-8');
    setOutputs(p => ({ ...p, [msgId]: [...(p[msgId] || []), f] }));
    setPlusOpen(false);
  };
  const genWord = () => {
    const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const html = `<html xmlns:o="urn:schemas-microsoft-com:office:office"><head><meta charset="utf-8">
      <style>body{font-family:'微软雅黑','Microsoft YaHei';font-size:12pt;line-height:1.7}
      h1{font-size:18pt} pre{white-space:pre-wrap;font-family:inherit}</style></head>
      <body><h1>Z-OPS 对话产出</h1><p>${esc(meta).replace(/\n/g, '<br>')}</p><hr><pre>${esc(body)}${esc(srcMd)}</pre></body></html>`;
    const f = triggerDownload(`Z-OPS回复_${stamp}.doc`, html, 'application/msword;charset=utf-8');
    setOutputs(p => ({ ...p, [msgId]: [...(p[msgId] || []), f] }));
    setPlusOpen(false);
  };

  const _tok = typeof window !== 'undefined' ? (localStorage.getItem('zops_token') || '') : '';
  const withTok = (u: string) => `${u}${u.includes('?') ? '&' : '?'}token=${_tok}`;
  const shots = isCuMessage(msg) ? [] : [...new Set([...(msg.all_shots || []), ...(msg.file_url && /\.(png|jpg)$/i.test(msg.file_url) ? [msg.file_url] : [])])];
  const serverFiles = !isCuMessage(msg) && msg.file_url
    ? [{ name: msg.file_name || '导出文件',
        url: `${msg.file_url}${msg.file_url.includes('?') ? '&' : '?'}token=${typeof window !== 'undefined' ? localStorage.getItem('zops_token') || '' : ''}` }]
    : [];
  const outFiles = isCuMessage(msg) ? [] : [...serverFiles, ...(outputs[msgId] || [])];
  const hasAny = tree.length > 0 || srcLinks.length > 0 || outFiles.length > 0;
  if (!hasAny && !msg) return null;

  return (
    <div className="w-[400px] max-w-[36vw] shrink-0 flex flex-col justify-start py-3 pr-4 pl-1">
      {/* 高度随内容自适应 (显示多少内容就多高), 超过可用高度时封顶、树区内部滚动 */}
      <div className="flex flex-col max-h-full rounded-2xl border border-[#e5e5e8] bg-white
                      shadow-[0_4px_24px_rgba(0,0,0,0.07)] overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-[#f0f0f2] shrink-0">
          <span className="text-[13px] font-semibold">调用详情</span>
          <span className="text-[11px] text-[#b4b4b4] truncate flex-1">
            {(msg.agent || 'assistant').replace('_agent', '')}
          </span>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-black/[0.05]" title="收起">
            <X size={14} className="text-[#8f8f8f]" />
          </button>
        </div>
        <div className="overflow-y-auto min-h-0 zops-hide-scroll">
          {/* ── Subagents: 调度与子代理工作 ── */}
          {tree.length > 0 && (
            <div className="p-2.5 border-b border-[#f0f0f2]">
              <div className="px-1.5 pb-1.5 text-[10px] font-semibold tracking-widest text-[#b4b4b4]">
                SUBAGENTS · 调度与子代理
              </div>
              {tree.map(node => <TreeNode key={node.id} node={node} depth={0} />)}
            </div>
          )}
          {/* ── Screenshots: CU 页面快照 (浏览器工具窗口留存) ── */}
          {shots.length > 0 && (
            <div className="p-2.5 border-b border-[#f0f0f2]">
              <div className="px-1.5 pb-1.5 text-[10px] font-semibold tracking-widest text-[#b4b4b4]">
                SCREENSHOTS · 页面快照 {shots.length}
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {shots.map((u, i) => (
                  <a key={i} href={withTok(u)} target="_blank" rel="noreferrer"
                     className="block rounded-lg overflow-hidden border border-[#ececec] hover:opacity-90">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={withTok(u)} alt={`页面快照 ${i + 1}`}
                         className="w-full h-[86px] object-cover object-top" loading="lazy" />
                  </a>
                ))}
              </div>
            </div>
          )}
          {/* ── Sources: 参考来源 ── */}
          {srcLinks.length > 0 && (
            <div className="p-2.5 border-b border-[#f0f0f2]">
              <div className="px-1.5 pb-1.5 text-[10px] font-semibold tracking-widest text-[#b4b4b4]">
                SOURCES · 参考来源 {srcLinks.length}
              </div>
              <div className="space-y-0.5">
                {srcLinks.map((l, i) => (
                  <a key={i} href={l[1]} target="_blank" rel="noreferrer"
                     className="block px-2.5 py-1.5 rounded-lg hover:bg-black/[0.03] text-[12.5px] text-indigo-600 leading-snug">
                    📄 {l[0]}
                  </a>
                ))}
              </div>
            </div>
          )}
          {/* ── Outputs: 产出 (➕ 生成文档) ── */}
          <div className="p-2.5 relative">
            <div className="flex items-center px-1.5 pb-1.5">
              <div className="flex-1 text-[10px] font-semibold tracking-widest text-[#b4b4b4]">
                OUTPUTS · 产出 {outFiles.length > 0 ? outFiles.length : ''}
              </div>
              {!isCuMessage(msg) && <button onClick={() => setPlusOpen(v => !v)}
                className="w-5 h-5 rounded-md flex items-center justify-center bg-black/[0.05] hover:bg-black/[0.1] transition-colors"
                title="从该回复生成文档">
                <Plus size={12} className="text-[#4b4b4b]" />
              </button>}
              {!isCuMessage(msg) && plusOpen && (
                <div className="absolute right-2.5 top-9 z-10 w-40 rounded-xl border border-[#ececec] bg-white shadow-lg py-1">
                  <button onClick={genMd} className="w-full flex items-center gap-2 px-3 py-2 text-[12.5px] hover:bg-black/[0.03]">
                    <FileText size={13} className="text-[#8f8f8f]" /> 生成 Markdown
                  </button>
                  <button onClick={genWord} className="w-full flex items-center gap-2 px-3 py-2 text-[12.5px] hover:bg-black/[0.03]">
                    <FileText size={13} className="text-[#8f8f8f]" /> 生成 Word 文档
                  </button>
                </div>
              )}
            </div>
            {outFiles.length > 0 ? (
              <div className="space-y-0.5">
                {outFiles.map((f, i) => (
                  <a key={i} href={f.url} download={f.name}
                     className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg hover:bg-black/[0.03] text-[12.5px] text-[#0d0d0d]">
                    <FileText size={13} className="text-indigo-500 shrink-0" />
                    <span className="truncate">{f.name}</span>
                  </a>
                ))}
              </div>
            ) : (
              <div className="px-2.5 py-1.5 text-[11.5px] text-[#b4b4b4]">{isCuMessage(msg) ? '电脑操作画面仅在绑定任务小窗中临时查看，不提供存档下载。' : '点击 + 从该回复生成文档'}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* 工具调用详情 (步骤展开区): 工具名 + 参数 + 结果摘要 */
function ToolDetail({ detail }: { detail: NonNullable<CallNode['detail']> }) {
  const args = detail.args || {};
  const argEntries = Object.entries(args).filter(([, v]) => v !== '' && v != null);
  const result = detail.result || '';
  const total = detail.result_len || result.length;
  const CLIP = 300;
  const [showAll, setShowAll] = useState(false);
  const clipped = result.length > CLIP && !showAll;
  return (
    <div className='my-1 rounded-xl bg-[#fafafa] border border-[#ececf0] px-3 py-2.5 text-[11px] leading-relaxed'>
      <div className='flex items-center gap-1.5 min-w-0'>
        <Wrench size={11} className='text-[#8f8f8f] shrink-0' />
        <code className='font-mono text-[10.5px] text-indigo-600 truncate' title={detail.tool}>{detail.tool}</code>
      </div>
      {argEntries.length > 0 && (
        <div className='mt-2'>
          <div className='text-[#b4b4b4] text-[9.5px] font-semibold tracking-wider mb-1'>参数</div>
          <div className='rounded-lg bg-white border border-[#efefef] px-2.5 py-1.5 space-y-0.5'>
            {argEntries.map(([k, v]) => (
              <div key={k} className='flex gap-2 min-w-0'>
                <span className='text-[#8f8f8f] shrink-0 font-mono text-[10px]'>{k}</span>
                <span className='text-[#333] font-mono text-[10px] break-all'>{typeof v === 'string' ? v : JSON.stringify(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {result !== '' && (
        <div className='mt-2'>
          <div className='text-[#b4b4b4] text-[9.5px] font-semibold tracking-wider mb-1'>
            结果{detail.ok === false ? ' · 失败' : ''}{total > result.length ? `（显示前 ${result.length} 字符，共 ${total}）` : total > CLIP ? `（${total} 字符）` : ''}
          </div>
          <div className='relative'>
            <pre className={`whitespace-pre-wrap break-all font-mono text-[10.5px] rounded-lg border px-2.5 py-2 overflow-y-auto
              ${detail.ok === false ? 'text-red-600 bg-red-50/50 border-red-100' : 'text-[#44464a] bg-white border-[#efefef]'}
              ${clipped ? 'max-h-[132px] overflow-hidden' : 'max-h-60'}`}>{showAll ? result : result.slice(0, CLIP)}</pre>
            {clipped && <div className='pointer-events-none absolute bottom-0 left-0 right-0 h-8 rounded-b-lg bg-gradient-to-b from-transparent to-white' />}
          </div>
          {result.length > CLIP && (
            <button onClick={() => setShowAll(v => !v)} className='mt-1 text-[10.5px] text-indigo-600 hover:text-indigo-700 hover:underline'>
              {showAll ? '收起结果' : `展开全文（共 ${total} 字符）`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function TreeNode({ node, depth }: { node: CallNode; depth: number }) {
  const hasDetail = !!node.detail;
  const [open, setOpen] = useState(!hasDetail && node.defaultOpen !== false);
  const hasKids = (node.children?.length || 0) > 0;
  const expandable = hasKids || hasDetail;
  const icon = node.kind === 'root' ? '🧠' : node.kind === 'agent' ? '🤖'
    : node.kind === 'sources' ? '📚' : node.kind === 'thinking' ? '💭' : '•';
  const d = node.detail;
  return (
    <div className="py-[2px]">
      <div onClick={() => expandable && setOpen(v => !v)}
        className={`flex items-start gap-1.5 rounded-lg px-2 py-1 text-[12.5px] leading-snug
          ${expandable ? 'cursor-pointer hover:bg-black/[0.03]' : ''}`}>
        {expandable ? (
          <ChevronRight size={12} className={`mt-[3px] shrink-0 text-[#b4b4b4] transition-transform ${open ? 'rotate-90' : ''}`} />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className="shrink-0 text-[11px] mt-[1px]">{icon}</span>
        {node.time && <span className="text-[#b4b4b4] text-[10.5px] tabular-nums mt-[3px] shrink-0">{node.time}</span>}
        {node.link ? (
          <a href={node.link} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
             className="text-indigo-600 hover:underline truncate">{node.title}</a>
        ) : (
          <span className={`truncate ${node.kind === 'root' || node.kind === 'agent' || node.kind === 'sources' || node.kind === 'thinking'
            ? 'font-medium text-[#0d0d0d]' : 'text-[#4b4b4b]'}`}>{node.title}</span>
        )}
        {d && d.result !== undefined && (
          <span className={`ml-auto shrink-0 mt-[2px] text-[10px] ${d.ok === false ? 'text-red-500' : 'text-emerald-500'}`}>
            {d.ok === false ? '✕' : '✓'}{d.secs != null ? ` ${d.secs}s` : ''}
          </span>
        )}
      </div>
      {open && expandable && (
        <div className="border-l border-[#efefef] ml-[13px] pl-1">
          {hasDetail && <ToolDetail detail={node.detail!} />}
          {node.children?.map(c => <TreeNode key={c.id} node={c} depth={depth + 1} />)}
        </div>
      )}
    </div>
  );
}

/* ───────────────────────── 视口右缘滚动指示条 ─────────────────────────
   隐藏原生滚动条后, 消息区的滚动位置以贴视口最右缘的细指示条呈现:
   fixed 定位 (不受对话容器/卡片列影响), 滚动时淡入、停止 ~0.9s 后淡出。 */
function EdgeScrollbar({ targetRef }: { targetRef: React.RefObject<HTMLDivElement | null> }) {
  const [vis, setVis] = useState(false);
  const [geo, setGeo] = useState<{ top: number; height: number } | null>(null);
  const hideTmr = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const el = targetRef.current;
    if (!el) return;
    const update = (show: boolean) => {
      const { scrollTop, scrollHeight, clientHeight } = el;
      if (scrollHeight <= clientHeight + 2) { setGeo(null); return; }
      const rect = el.getBoundingClientRect();
      const ratio = clientHeight / scrollHeight;
      const thumbH = Math.max(36, rect.height * ratio);
      const maxScroll = scrollHeight - clientHeight;
      const top = rect.top + (scrollTop / maxScroll) * (rect.height - thumbH);
      setGeo({ top, height: thumbH });
      if (show) {
        setVis(true);
        if (hideTmr.current) clearTimeout(hideTmr.current);
        hideTmr.current = setTimeout(() => setVis(false), 900);
      }
    };
    const onScroll = () => update(true);
    const onIdle = () => update(false);
    el.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onIdle);
    update(false);
    return () => {
      el.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onIdle);
      if (hideTmr.current) clearTimeout(hideTmr.current);
    };
  }, [targetRef]);

  if (!geo) return null;
  return (
    <div style={{ top: geo.top, height: geo.height }}
      className={`fixed right-[3px] w-[5px] rounded-full bg-black/25 z-40 pointer-events-none
                  transition-opacity duration-500 ${vis ? 'opacity-100' : 'opacity-0'}`} />
  );
}

function Thinking({ thinking, streaming, defaultOpen }: { thinking: string; streaming: boolean; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(!!defaultOpen);
  const n = thinking.length;
  return (
    <div className="pl-8 mb-2">
      <button onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1.5 text-[12px] text-[#8f8f8f] hover:text-[#0d0d0d] py-1">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {streaming ? '深度思考中…' : `已深度思考 (${n} 字)`}
      </button>
      {open && (
        <div className="mt-1 mb-2 px-3.5 py-2.5 rounded-xl bg-[#f9f9f9] text-[13px] leading-relaxed text-[#6b6b6b]
                        max-h-64 overflow-y-auto whitespace-pre-wrap border border-[#f0f0f0]">
          {thinking}
        </div>
      )}
    </div>
  );
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <button onClick={() => { navigator.clipboard?.writeText(text); setOk(true); setTimeout(() => setOk(false), 1500); }}
      className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-black/5 transition-opacity">
      {ok ? <Check size={12} className="text-emerald-600" /> : <Copy size={12} className="text-[#b4b4b4]" />}
    </button>
  );
}

/* ───────────────────────── 能力面板 (点 + 展开, 快捷触发能力) ───────────────────────── */
function CapabilityPanel({ onSend, onDraft, extOnline, browserOpen, onToggleBrowser }: {
  onSend: (t: string) => void; onDraft: (t: string) => void;
  extOnline?: boolean; browserOpen?: boolean; onToggleBrowser?: () => void;
}) {
  const [open, setOpen] = useState(false);
  // instant=true → 完整指令, 点击直接发送
  // instant=false → 需要用户补充参数, 点击填入输入框并聚焦
  const QUICK = [
    { icon: Cpu, label: '查主机', text: '检查以下主机的连通性和状态:', instant: false },
    { icon: Database, label: '查 VM 备份', text: '帮我查一下以下虚拟机的备份状态:', instant: false },
    { icon: Monitor, label: '电脑操作', text: '在已选择的 Companion 设备上打开 ', instant: false },
    { icon: Wrench, label: '执行脚本', text: '执行脚本 ', instant: false },
    { icon: BookOpen, label: '搜知识库', text: '帮我搜索以下知识:', instant: false },
    { icon: MessageSquare, label: '通用问答', text: '', instant: true },
  ];
  if (!open) return (
    <div className="max-w-3xl mx-auto px-4 pb-1 flex items-center gap-3">
      <button onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 text-[12px] text-gray-400 hover:text-gray-700 transition-colors px-1">
        <Plus size={13} /> 快捷能力
      </button>
      {onToggleBrowser && (
        <button onClick={onToggleBrowser}
          title={extOnline ? '已选设备在线（不代表已授权）— 查看任务' : '已选设备未在线或状态未确认 — 查看连接引导'}
          className={`flex items-center gap-1.5 text-[12px] transition-colors px-1 ${
            extOnline ? 'text-emerald-600 hover:text-emerald-700' : 'text-gray-400 hover:text-gray-700'}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${extOnline ? 'bg-emerald-500 animate-pulse' : 'bg-gray-300'}`} />
          {browserOpen ? '任务小窗' : '电脑操作'}
        </button>
      )}
    </div>
  );
  return (
    <div className="max-w-3xl mx-auto px-4 pb-1">
      <div className="flex flex-wrap gap-2 mb-1">
        {QUICK.map(q => (
          <button key={q.label}
            onClick={() => {
              if (q.instant) { onSend(q.text); }
              else { onDraft(q.text); }
              setOpen(false);
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 text-[12px] text-gray-600 hover:bg-gray-50 hover:border-gray-300 transition-colors">
            <q.icon size={12} /> {q.label}
            {!q.instant && <span className="text-[10px] text-gray-400 ml-0.5">补参数</span>}
          </button>
        ))}
        <button onClick={() => setOpen(false)}
          className="text-gray-300 hover:text-gray-500 px-1"><X size={14} /></button>
      </div>
    </div>
  );
}

/* ───────────────────────── Composer (自增高 + 内嵌发送/停止) ───────────────────────── */
function Composer({ loading, onSend, onStop, canStop, draftText, onDraftUsed }: {
  loading: boolean; onSend: (t: string) => void; onStop: () => void; canStop: boolean;
  draftText?: string; onDraftUsed?: () => void;
}) {
  const [text, setText] = useState('');
  const ref = useRef<HTMLTextAreaElement>(null);

  // 从快捷能力面板填入的预填文本
  useEffect(() => {
    if (draftText) {
      setText(draftText);
      onDraftUsed?.();
      ref.current?.focus();
      // 光标移到末尾
      const el = ref.current;
      if (el) { el.selectionStart = el.selectionEnd = el.value.length; }
    }
  }, [draftText, onDraftUsed]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [text]);

  const submit = () => {
    const t = text.trim();
    if (!t || loading) return;
    setText('');
    onSend(t);
  };

  return (
    <div className="shrink-0 pb-4 pt-2 px-4 bg-gradient-to-t from-white via-white">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-end gap-2 rounded-[26px] border border-[#e3e3e3] bg-white shadow-[0_2px_12px_rgba(0,0,0,0.04)]
                        pl-5 pr-2 py-2 focus-within:border-[#d0d0d0] transition-colors">
          <textarea ref={ref} value={text} rows={1}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault(); submit();
              }
            }}
            placeholder="发送消息 (Enter 发送, Shift+Enter 换行)"
            className="flex-1 resize-none bg-transparent outline-none text-[15px] leading-relaxed py-1.5 max-h-[200px]
                       placeholder:text-[#b4b4b4]" />
          {loading ? (
            <button onClick={onStop} title="停止生成"
              className="w-8 h-8 rounded-full bg-[#0d0d0d] hover:bg-[#2a2a2a] flex items-center justify-center shrink-0 transition-colors">
              <Square size={11} className="text-white fill-white" />
            </button>
          ) : (
            <button onClick={submit} disabled={!text.trim()}
              className="w-8 h-8 rounded-full bg-[#0d0d0d] flex items-center justify-center shrink-0 transition-all
                         disabled:opacity-15 hover:bg-[#2a2a2a]"
              title="发送">
              <ArrowUp size={16} className="text-white" />
            </button>
          )}
        </div>
        <div className="text-center text-[11px] text-[#b4b4b4] mt-2">
          Z-OPS 可能会犯错，请核验重要信息 · 技能经审批发布 · 记忆仅本人可见
        </div>
      </div>
    </div>
  );
}
