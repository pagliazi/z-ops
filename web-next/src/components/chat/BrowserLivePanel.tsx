'use client';

import { useEffect, useRef, useState } from 'react';
import { X, MinusSquare, Monitor, Pause, Square } from 'lucide-react';
import { cuRequest } from '@/components/ComputerUseSettings';
import { BrowserConnectCard } from '@/components/chat/BrowserConnectCard';

export type CuTask = {
  task_id: string; device_id: string; device_name?: string; session_id: string;
  state: string; reason?: string; pause_reason?: string; updated_at?: string | number;
  execution_mode?: string; mode?: string; cu_backend?: string;
  step?: number; approval?: boolean;
  frame?: { jpeg: string; w: number; h: number };
  evidence?: string[];
};
const labels: Record<string, string> = {
  queued: '排队中', pending: '待执行', running: '执行中', executing: '执行中',
  paused: '已暂停', pause_requested: '暂停待确认', cancelling: '停止待确认', cancel_requested: '停止待确认',
  cancelled: '已停止', canceled: '已停止', stopped: '已停止', completed: '已完成', succeeded: '已完成',
  failed: '执行失败', denied: '已拒绝', rejected: '已拒绝', unknown: '执行结果未知',
  waiting_user: '等待本机确认', waiting_login: '等待本机登录', offline: '设备离线',
};
const terminal = new Set(['cancelled', 'canceled', 'stopped', 'completed', 'succeeded', 'failed', 'denied', 'rejected']);
const stopped = new Set(['cancelled', 'canceled', 'stopped']);
// 执行端只上报静态暂停码; 这里翻译为可读原因, 未登记的码回退到原始原因。
const pauseReasons: Record<string, string> = {
  local_authorization_required: '本机需要你确认后方可继续',
  sensitive_or_login_page: '页面需要登录或包含敏感输入，已在采集前暂停',
  origin_blocked: '页面访问了未授权站点，已被阻止并暂停',
  redirect_limit: '页面重定向次数超限，已暂停',
  desktop_control_not_approved: '桌面接管尚未在本机批准',
  desktop_approval_declined: '本机已拒绝桌面接管',
  desktop_approval_timeout: '本机批准对话框无人操作，已按到期自动拒绝',
  desktop_control_suspended: '桌面接管已挂起',
  desktop_focus_denied: '目标窗口焦点校验未通过',
  desktop_secure_desktop: '安全桌面不可自动化',
};
const button = 'rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-40';

export function validateCuTask(task: CuTask, sessionId: string, taskId: string, deviceId: string): CuTask {
  if (!task || task.session_id !== sessionId || task.task_id !== taskId || !deviceId || task.device_id !== deviceId || typeof task.state !== 'string') {
    throw new Error('任务、会话或设备绑定不匹配，已拒绝显示画面');
  }
  return task;
}
export function taskFrame(task: CuTask, allowed: boolean): string {
  const frame = task.frame;
  if (!allowed || !['running', 'executing'].includes(task.state.toLowerCase()) || !frame ||
      typeof frame.jpeg !== 'string' || frame.jpeg.length > 8_000_000 || !/^[A-Za-z0-9+/]+={0,2}$/.test(frame.jpeg) ||
      !Number.isFinite(frame.w) || !Number.isFinite(frame.h) || frame.w <= 0 || frame.h <= 0) return '';
  return `data:image/jpeg;base64,${frame.jpeg}`;
}

type PanelProps = { open: boolean; onClose: () => void; sessionId?: string; taskId?: string; deviceId?: string };
export function BrowserLivePanel(props: PanelProps) {
  // 身份变化时先卸载旧实例，避免 effect 执行前闪现上一会话画面。
  if (!props.open) return null;
  return <TaskPanel key={JSON.stringify([props.sessionId, props.taskId, props.deviceId])} {...props} />;
}

function TaskPanel({ sessionId = '', taskId = '', deviceId = '', onClose }: PanelProps) {
  const [chosen, setChosen] = useState(taskId);
  const [tasks, setTasks] = useState<CuTask[]>([]);
  const [task, setTask] = useState<CuTask | null>(null);
  const [frame, setFrame] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [minimized, setMinimized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState<'pause' | 'cancel' | null>(null);
  const [controlNote, setControlNote] = useState('');
  const [refresh, setRefresh] = useState(0);
  const boundDevice = useRef(deviceId);
  const control = useRef<AbortController | null>(null);
  const revision = useRef(0);
  const pending = useRef<'pause' | 'cancel' | null>(null);

  useEffect(() => {
    const changed = () => { setFrame(''); setRefresh(n => n + 1); };
    window.addEventListener('zops:cu-prefs-changed', changed);
    window.addEventListener('zops:cu-devices-changed', changed);
    return () => {
      window.removeEventListener('zops:cu-prefs-changed', changed);
      window.removeEventListener('zops:cu-devices-changed', changed);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;
    let frameTimer: ReturnType<typeof setTimeout>;
    setTask(null); setFrame(''); setError(''); setLoading(true);
    async function poll() {
      const currentRevision = revision.current;
      try {
        if (!sessionId) { setLoading(false); return; }
        const list = await cuRequest<{ tasks: CuTask[] }>(`/api/cu/tasks?session_id=${encodeURIComponent(sessionId)}`, signal);
        if (!list || !Array.isArray(list.tasks)) throw new Error('任务列表格式不正确');
        const own = list.tasks.filter(t => t && t.session_id === sessionId && typeof t.task_id === 'string' && typeof t.state === 'string');
        if (signal.aborted || currentRevision !== revision.current) return;
        setTasks(own);
        if (!chosen) { setError(''); return; }
        const entry = own.find(t => t.task_id === chosen);
        if (!entry?.device_id) throw new Error('当前会话尚未确认此任务及设备，请等待状态同步');
        if (boundDevice.current && boundDevice.current !== entry.device_id) throw new Error('任务设备发生变化，已阻止跨设备显示');
        boundDevice.current = entry.device_id;
        const data = await cuRequest<CuTask>(`/api/cu/tasks/${encodeURIComponent(chosen)}/status`, signal);
        validateCuTask(data, sessionId, chosen, boundDevice.current);
        // 截图偏好不可获取时默认不展示，不能把接口失败当作授权。
        const prefs = await cuRequest<{ push_frames?: boolean }>('/api/cu/prefs', signal).catch(() => ({ push_frames: false }));
        if (signal.aborted || currentRevision !== revision.current) return;
        setTask({ ...data, frame: undefined });
        setFrame(taskFrame(data, prefs.push_frames === true && !pending.current));
        clearTimeout(frameTimer);
        frameTimer = setTimeout(() => { if (!signal.aborted) setFrame(''); }, 8000);
        setError('');
        const state = data.state.toLowerCase();
        if (!control.current && pending.current === 'pause' && state === 'paused') {
          setControlNote('已确认暂停'); pending.current = null; setRequested(null);
        } else if (!control.current && pending.current === 'cancel' && stopped.has(state)) {
          setControlNote('已确认停止'); pending.current = null; setRequested(null);
        } else if (!control.current && pending.current && terminal.has(state)) {
          setControlNote(`任务已结束：${labels[state] || state}；不表示先前控制操作已执行`);
          pending.current = null; setRequested(null);
        }
      } catch (e) {
        if (!signal.aborted) { setFrame(''); setTask(null); setError(e instanceof Error ? e.message : '获取状态失败'); }
      } finally {
        if (!signal.aborted) { setLoading(false); timer = setTimeout(poll, 2000); }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); clearTimeout(frameTimer); };
  }, [sessionId, chosen, refresh]);

  useEffect(() => () => { control.current?.abort(); control.current = null; }, []);

  function choose(id: string) {
    if (control.current) return;
    revision.current += 1; boundDevice.current = id === taskId ? deviceId : '';
    setFrame(''); setTask(null); setError(''); setControlNote(''); setRequested(null); pending.current = null;
    setChosen(id);
  }
  async function request(action: 'pause' | 'cancel') {
    if (!task || control.current || (pending.current === action)) return;
    if (action === 'cancel' && !window.confirm('停止此设备任务？已执行的操作不会自动撤销。')) return;
    const controller = new AbortController(); control.current = controller;
    revision.current += 1; pending.current = action;
    setRequested(action); setBusy(true); setFrame(''); setControlNote(action === 'pause' ? '正在请求暂停…' : '正在请求停止…');
    try {
      await cuRequest(`/api/cu/tasks/${encodeURIComponent(task.task_id)}/control`, controller.signal, 'POST', { action });
      if (!controller.signal.aborted) setControlNote(action === 'pause' ? '已请求暂停，等待执行器确认' : '已请求停止，等待执行器确认');
    } catch (e) {
      if (!controller.signal.aborted) setControlNote(`控制请求结果未知：${e instanceof Error ? e.message : '网络异常'}。仅核对状态，不自动重发。`);
    } finally {
      if (!controller.signal.aborted) { setBusy(false); setRefresh(n => n + 1); }
      if (control.current === controller) control.current = null;
    }
  }

  const state = task?.state.toLowerCase() || '';
  const evidence = Array.isArray(task?.evidence)
    ? task!.evidence!.filter((name): name is string => typeof name === 'string' && /^computer_use\/[\w-]+\.jpg$/.test(name))
    : [];
  const token = typeof window !== 'undefined' ? localStorage.getItem('zops_token') || '' : '';
  const reportedMode = task?.execution_mode || task?.mode;
  const modes: Record<string, string> = { unfocused_window: '不聚焦任务窗口（非完整后台）', background: '后台执行', desktop: '桌面操作', foreground: '前台操作', browser: '浏览器操作' };
  const mode = reportedMode ? modes[reportedMode] || reportedMode : '';
  return (
    <aside className="fixed bottom-4 right-4 z-40 w-[410px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-gray-200 bg-white shadow-lg text-gray-700" aria-label="电脑操作任务小窗">
      <header className="flex items-center gap-2 border-b border-gray-100 px-3 py-2">
        <Monitor size={14} /><span className="flex-1 truncate text-sm font-medium">{task?.device_name || '电脑操作'} · {labels[state] || (state ? `未知状态：${state}` : '任务状态')}</span>
        <button className={button} onClick={() => setMinimized(v => !v)} aria-label={minimized ? '展开' : '收起'}><MinusSquare size={13} /></button>
        <button className={button} onClick={onClose} aria-label="关闭小窗"><X size={13} /></button>
      </header>
      {!minimized && <div className="max-h-[70vh] overflow-y-auto p-3 space-y-3 text-xs">
        {!sessionId ? <p>请先打开一个会话。未绑定会话时不会接收画面。</p> : <>
          <label className="block">当前会话任务
            <select className="mt-1 w-full rounded-lg border border-gray-200 bg-white p-2" value={chosen} disabled={busy} onChange={e => choose(e.target.value)}>
              <option value="">请选择要查看的任务</option>
              {chosen && !tasks.some(t => t.task_id === chosen) && <option value={chosen}>{chosen} · 等待确认</option>}
              {tasks.map(t => <option key={t.task_id} value={t.task_id}>{t.device_name || t.device_id} · {labels[t.state?.toLowerCase()] || t.state} · {t.task_id}</option>)}
            </select>
          </label>
          {loading && <p role="status">正在获取任务状态…</p>}
          {error && <p role="alert" className="rounded-lg border border-gray-200 bg-gray-50 p-2">{error}（将自动核对状态，不重放任务）</p>}
          {task && <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 space-y-1 break-all">
            <p>设备：{task.device_name || '名称未上报'} · {task.device_id}</p>
            <p>任务：{task.task_id}</p>
            <p>执行方式：{mode || '执行器未上报，不推断为后台或桌面模式'}</p>
            <p>当前步骤：{typeof task.step === 'number' ? `第 ${task.step} 步` : '执行器未上报'}</p>
            <p>状态：{labels[state] || `未知状态：${state}`}</p>
            <p>审批：{task.approval === true
              ? '等待你在对话中回复「确认」；提交、发送、删除类副作用不会自动放行'
              : '无待确认动作'}</p>
            <p>原因：{pauseReasons[task.pause_reason || ''] || task.reason || (['paused', 'denied', 'rejected', 'unknown'].includes(state) ? '执行器未提供原因，请在本机核验' : '无额外说明')}</p>
            {['paused', 'denied', 'rejected', 'unknown', 'waiting_user', 'waiting_login'].includes(state) &&
              <p>恢复入口：在对话中回复「继续」；暂停、拒绝及 unknown 需先在 Companion 核验后再继续。</p>}
            {/能力|不支持|capability|unsupported/i.test(task.reason || '') &&
              <p role="alert">能力不足：本机设备未声明该能力；请在设置 → 电脑操作查看设备版本能力，不会改用服务器执行。</p>}
            {task.updated_at && <p>更新时间：{String(task.updated_at)}</p>}
          </div>}
          {evidence.length > 0 && <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 space-y-1 break-all">
            <p className="text-gray-900">截图证据（已另行授权保存，不自动删除）</p>
            {evidence.map(name => <p key={name}>{
              token ? <a className="underline" href={`/api/download/${name}?token=${encodeURIComponent(token)}`} target="_blank" rel="noreferrer">{name.split('/')[1]}</a> : name.split('/')[1]
            }</p>)}
            <p className="text-gray-500">仅任务归属用户可下载；关闭保存不会删除已保存证据。</p>
          </div>}
          {frame ? <div className="rounded-lg border border-gray-200 bg-gray-50 p-1" onContextMenu={e => e.preventDefault()}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={frame} alt="当前任务临时画面" draggable={false} className="max-h-64 w-full object-contain" />
          </div> : <p className="rounded-lg border border-gray-200 bg-gray-50 p-3">暂无已授权的任务画面。扩展截图暂不可用；暂停、停止、断连或切换任务时清除画面。</p>}
          <div className="flex gap-2">
            <button className={button} disabled={!task || busy || !!requested || terminal.has(state) || state === 'paused'} onClick={() => request('pause')}><Pause size={12} className="mr-1 inline" />暂停</button>
            <button className={button} disabled={!task || busy || requested === 'cancel' || terminal.has(state)} onClick={() => request('cancel')}><Square size={12} className="mr-1 inline" />停止</button>
          </div>
          {controlNote && <p role="status">{controlNote}</p>}
          <p className="leading-relaxed text-gray-500">本页只核对状态，不自动续跑。登录请在任务窗口手动完成；暂停、拒绝及 unknown 需在 Companion 核验后安全继续，未知写操作不得自动重放。后台优先不等于完整后台。</p>
        </>}
        <BrowserConnectCard variant="panel" />
      </div>}
    </aside>
  );
}

export default BrowserLivePanel;
