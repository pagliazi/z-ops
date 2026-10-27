'use client';

import { useEffect, useRef, useState } from 'react';

export type CuGrants = {
  roots?: { name: string; read: boolean; write_new: boolean; manage?: boolean }[];
  origins?: string[]; scripts?: string[]; native_origins?: string[]; apps?: string[];
};
export type CuDevice = {
  device_id: string; name: string; platform: string; online: boolean;
  version: string; capabilities: string[] | Record<string, unknown>; revoked: boolean;
  grants?: CuGrants;
};
export type CuDevices = { devices: CuDevice[]; selected_device_id: string | null };

// 凭据只进入认证头；画面和配对码不进入持久缓存。
export async function cuRequest<T>(path: string, signal: AbortSignal, method = 'GET', body?: unknown): Promise<T> {
  const token = localStorage.getItem('zops_token');
  if (!token) throw new Error('登录已失效，请重新登录');
  signal.throwIfAborted();
  const request = new AbortController();
  const abort = () => request.abort();
  signal.addEventListener('abort', abort, { once: true });
  const timeout = setTimeout(() => request.abort(), 15000);
  try {
    const response = await fetch(path, {
      method, signal: request.signal, cache: 'no-store',
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = typeof data.detail === 'string' ? data.detail : '';
      throw new Error(`${detail || '电脑操作请求失败'}（${response.status}）`);
    }
    const text = await response.text();
    signal.throwIfAborted();
    return text ? JSON.parse(text) : undefined as T;
  } catch (e) {
    if (request.signal.aborted && !signal.aborted) throw new Error('请求超时，请刷新核对实际状态');
    throw e;
  } finally {
    clearTimeout(timeout); signal.removeEventListener('abort', abort);
  }
}

export function validateCuDevices(data: CuDevices): CuDevices {
  if (!data || !Array.isArray(data.devices) ||
      (data.selected_device_id !== null && data.selected_device_id !== undefined && typeof data.selected_device_id !== 'string') ||
      data.devices.some(d => !d || typeof d.device_id !== 'string' || !d.device_id || typeof d.online !== 'boolean' || typeof d.revoked !== 'boolean') ||
      new Set(data.devices.map(d => d.device_id)).size !== data.devices.length) {
    throw new Error('设备列表格式不正确');
  }
  return data;
}

const button = 'rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-40 whitespace-nowrap';

export function ComputerUseSettings() {
  const [devices, setDevices] = useState<CuDevices>({ devices: [], selected_device_id: null });
  const [ready, setReady] = useState(false);
  const [frames, setFrames] = useState<boolean | null>(null);
  const [desktopOptin, setDesktopOptin] = useState<boolean | null>(null);
  const [persistShots, setPersistShots] = useState<boolean | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [pair, setPair] = useState<{ code: string; expires: number } | null>(null);
  const [now, setNow] = useState(Date.now());
  const operation = useRef<AbortController | null>(null);
  const alive = useRef(false);

  async function run(label: string, action: (signal: AbortSignal) => Promise<void>) {
    if (!alive.current || operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(label); setError(''); setNote('');
    try { await action(controller.signal); }
    catch (e) {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : '请求失败');
        if (label !== '正在刷新' && label !== '正在生成配对码') { setReady(false); setFrames(null); setDesktopOptin(null); setPersistShots(null); }
      }
    }
    finally {
      if (operation.current === controller) operation.current = null;
      if (!controller.signal.aborted) setBusy('');
    }
  }

  async function readDevices(signal: AbortSignal) {
    const data = validateCuDevices(await cuRequest<CuDevices>('/api/cu/devices', signal));
    if (!signal.aborted) { setDevices(data); setReady(true); }
    return data;
  }
  const refresh = () => run('正在刷新', async signal => {
    setReady(false); setFrames(null); setDesktopOptin(null); setPersistShots(null);
    const results = await Promise.allSettled([
      readDevices(signal),
      cuRequest<{ push_frames?: boolean; desktop_optin?: boolean; persist_shots?: boolean }>('/api/cu/prefs', signal).then(data => {
        if (typeof data.push_frames !== 'boolean' || typeof data.desktop_optin !== 'boolean' || typeof data.persist_shots !== 'boolean') throw new Error('偏好接口暂不可用');
        if (!signal.aborted) { setFrames(data.push_frames); setDesktopOptin(data.desktop_optin); setPersistShots(data.persist_shots); }
      }),
    ]);
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected');
    if (failures.length) throw new Error(failures.map(r => r.reason?.message || '请求失败').join('；'));
  });

  useEffect(() => {
    alive.current = true;
    refresh();
    return () => {
      alive.current = false;
      operation.current?.abort(); operation.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!pair) return;
    const timer = setInterval(() => {
      const time = Date.now(); setNow(time);
      if (time >= pair.expires) setPair(null);
    }, 1000);
    return () => clearInterval(timer);
  }, [pair]);

  const select = (device: CuDevice) => run('正在选择设备', async signal => {
    await cuRequest('/api/cu/devices/selection', signal, 'PUT', { device_id: device.device_id });
    const data = await readDevices(signal);
    if (data.selected_device_id !== device.device_id) throw new Error('选择尚未确认，请刷新后核对');
    if (!signal.aborted) { setNote(`已选择 ${device.name || device.device_id}，不代表已获本机授权`); window.dispatchEvent(new Event('zops:cu-devices-changed')); }
  });
  const revoke = (device: CuDevice) => {
    if (operation.current || !window.confirm(`吊销设备“${device.name || device.device_id}”？该设备将不能继续执行任务，重新使用需重新配对。`)) return;
    void run('正在吊销设备', async signal => {
      await cuRequest(`/api/cu/devices/${encodeURIComponent(device.device_id)}`, signal, 'DELETE');
      const data = await readDevices(signal);
      if (data.devices.some(d => d.device_id === device.device_id && !d.revoked)) throw new Error('吊销尚未确认，请刷新后核对');
      if (!signal.aborted) { setNote('设备已吊销'); window.dispatchEvent(new Event('zops:cu-devices-changed')); }
    });
  };
  const generate = () => run('正在生成配对码', async signal => {
    setPair(null);
    const data = await cuRequest<{ code: string; expires_in: number }>('/api/cu/devices/pair-code', signal, 'POST');
    if (typeof data.code !== 'string' || !data.code || !Number.isFinite(data.expires_in) || data.expires_in <= 0) throw new Error('配对码响应无效');
    if (!signal.aborted) { setNow(Date.now()); setPair({ code: data.code, expires: Date.now() + data.expires_in * 1000 }); }
  });
  const saveFrames = () => run('正在保存画面偏好', async signal => {
    const next = !frames;
    await cuRequest('/api/cu/prefs', signal, 'PUT', { push_frames: next });
    const data = await cuRequest<{ push_frames?: boolean }>('/api/cu/prefs', signal);
    if (data.push_frames !== next) throw new Error('画面偏好尚未确认，请刷新后核对');
    if (!signal.aborted) { setFrames(next); setNote('画面偏好已保存；实际可用性由执行器能力决定'); window.dispatchEvent(new Event('zops:cu-prefs-changed')); }
  });
  const saveDesktopOptin = () => run('正在保存桌面策略', async signal => {
    const next = !desktopOptin;
    await cuRequest('/api/cu/prefs', signal, 'PUT', { desktop_optin: next });
    const data = await cuRequest<{ desktop_optin?: boolean }>('/api/cu/prefs', signal);
    if (data.desktop_optin !== next) throw new Error('桌面策略尚未确认，请刷新后核对');
    if (!signal.aborted) { setDesktopOptin(next); setNote(next ? '已允许申请桌面接管；每次仍需本机原生批准和 60 秒租约' : '已禁止桌面接管申请'); window.dispatchEvent(new Event('zops:cu-prefs-changed')); }
  });
  const savePersistShots = () => run('正在保存截图证据策略', async signal => {
    const next = !persistShots;
    if (next && !persistShots &&
        !window.confirm('开启后，任务浏览器截图会持久保存到服务器并保留，不自动删除（每任务最多 20 张、至少间隔 10 秒）。仅在你需要留存证据时开启。')) return;
    await cuRequest('/api/cu/prefs', signal, 'PUT', { persist_shots: next });
    const data = await cuRequest<{ persist_shots?: boolean }>('/api/cu/prefs', signal);
    if (data.persist_shots !== next) throw new Error('截图证据策略尚未确认，请刷新后核对');
    if (!signal.aborted) { setPersistShots(next); setNote(next ? '已开启截图证据持久保存：仅任务浏览器截图，另行授权，可随时关闭' : '已关闭截图证据保存；已保存的证据不会被自动删除'); window.dispatchEvent(new Event('zops:cu-prefs-changed')); }
  });

  return (
    <div className="space-y-4 text-xs text-gray-600">
      <section className="rounded-xl border border-gray-200 bg-gray-50 p-4 space-y-2">
        <h3 className="text-sm font-medium text-gray-900">在已选设备上执行</h3>
        <p>无法可靠绑定当前浏览器与电脑，请明确选择设备；不会自动选择唯一在线设备。已保存的选择也不代表这台浏览器所在的电脑。</p>
        <p>后台优先：浏览器任务在 Companion 的独立无头浏览器中执行，不占用你的鼠标键盘；真实桌面接管默认关闭，即使开启也需本机桌面验收（Windows UIA / Linux X11）和每次本机原生批准。Linux 上不提供控件树与窗口截图，且没有 _NET_WM_PID 的窗口不可接管；Wayland 会话一律拒绝。设备离线、拒绝授权或结果未知时任务暂停，不会改用服务器执行。</p>
      </section>
      <section className="rounded-xl border border-gray-200 p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium text-gray-900">设备</h3>
          <button className={button} disabled={!!busy} onClick={refresh}>刷新状态</button>
        </div>
        {!ready && <p>设备状态尚未确认。</p>}
        {ready && devices.devices.length === 0 && <p>尚无配对设备，请先安装并配对 Companion。</p>}
        <div className="space-y-2">
          {devices.devices.map(device => {
            const selected = device.device_id === devices.selected_device_id;
            const capabilities = Array.isArray(device.capabilities) ? device.capabilities : Object.entries(device.capabilities || {}).filter(([, v]) => v === true).map(([k]) => k);
            return (
              <div key={device.device_id} className={`rounded-lg border p-3 ${selected ? 'border-gray-400 bg-gray-50' : 'border-gray-200 bg-white'}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <strong className="text-gray-900 break-all">{device.name || device.device_id}</strong>
                  <span>{device.revoked ? '已吊销' : device.online ? '在线' : '离线'}{selected ? ' · 已选择' : ''}</span>
                </div>
                <p className="mt-1 break-all">{device.platform || '平台未知'} · 版本 {device.version || '未知'} · {device.device_id}</p>
                <p className="mt-1 break-all">能力：{capabilities.join('、') || '未上报'}（能力不等于授权）</p>
                {device.grants && <div className="mt-1 space-y-0.5 text-gray-500">
                  <p className="break-all">授权目录：{(device.grants.roots || []).map(r => `${r.name}（${r.read ? '读' : ''}${r.write_new ? '新建' : ''}${r.manage ? '管理' : ''}）`).join('、') || '无'}</p>
                  <p className="break-all">授权站点：{(device.grants.origins || []).join('、') || '无'}</p>
                  <p className="break-all">登记脚本：{(device.grants.scripts || []).join('、') || '无'}</p>
                  <p className="break-all">授权桌面应用：{(device.grants.apps || []).join('、') || '无（未授权任何应用接管）'}</p>
                  <p className="break-all">扩展 Native origin：{(device.grants.native_origins || []).join('、') || '无'}</p>
                  <p>本机授权由 Companion 上报，本页只读；新增或扩大授权必须在本机 CLI 完成。</p>
                </div>}
                <div className="mt-2 flex gap-2">
                  <button className={button} disabled={!!busy || !ready || selected || device.revoked || !device.online} onClick={() => select(device)}>选择此设备</button>
                  <button className={button} disabled={!!busy || !ready || device.revoked} onClick={() => revoke(device)}>吊销</button>
                </div>
              </div>
            );
          })}
        </div>
        {ready && !devices.devices.some(d => d.device_id === devices.selected_device_id && !d.revoked) && <p className="mt-3">尚未选择有效设备，任务不会自动落到其他电脑。</p>}
      </section>
      <section className="rounded-xl border border-gray-200 p-4 space-y-2">
        <h3 className="text-sm font-medium text-gray-900">安装与配对</h3>
        <ol className="list-decimal pl-4 space-y-1">
          <li>向管理员获取并核验 Companion 构建及安装步骤。Windows 执行器构建尚未签名，当前不提供可用的 exe 下载承诺。</li>
          <li>安装并注册本机 Native Host：net.zops.companion。扩展用于本机状态与可选登录态交接；网页任务由 Companion 的独立浏览器执行，仅安装扩展不代表已连接。</li>
          <li>安装经核验的 2.x 扩展：Chrome 扩展管理页开启开发者模式，加载已解压扩展；不再使用旧版服务器直连扩展。</li>
          <li>需要现有 Chrome 登录态时，在扩展弹窗点击“授权当前站点登录态”：只交接当前站点 Cookie 到本机 Companion，5 分钟内单次使用，不经过服务器，也不保证免登录。</li>
          <li>生成配对码，在 Companion 本机输入并确认，然后刷新设备列表并明确选择设备。扩展弹窗只显示本机状态与登录态交接结果，不批准任务。</li>
        </ol>
        <button className={button} disabled={!!busy} onClick={generate}>{pair ? '重新生成配对码' : '生成配对码'}</button>
        {pair && <div className="rounded-lg border border-gray-200 bg-gray-50 p-3" aria-live="polite">
          <span className="font-mono text-base tracking-widest select-all">{pair.code}</span>
          <p className="mt-1">剩余 {Math.max(0, Math.ceil((pair.expires - now) / 1000))} 秒，仅在 Companion 输入。关闭设置即清除本页显示。</p>
        </div>}
      </section>
      <section className="rounded-xl border border-gray-200 p-4 space-y-2">
        <h3 className="text-sm font-medium text-gray-900">后台策略与本机授权</h3>
        <p>初期动作受 admin/runtime action allowlist 与本地 config 共同限制，云端不能默默扩大本机授权。目录、站点、应用与脚本授权以各设备 Companion 上报为准，本页只读；新增或扩大授权必须在本机 CLI 显式完成，本页不会伪造已保存权限。覆盖、移动、删除需本机 manage 授权，且每次由你单独回复「确认」。桌面接管还需本机按进程名授权应用（--allow-desktop-app），未列出的应用不可操作。</p>
        <p>目录/站点/应用/脚本四类授权均可在此核对，但仅展示已上报的摘要。</p>
        <div className="flex items-center justify-between gap-3 pt-2">
          <div><p className="text-gray-900">桌面接管申请</p><p className="mt-1">默认禁止。开启后仍须设备完成本机桌面验收（Windows UIA / Linux X11）；每次接管由 Companion 本机弹窗批准，60 秒租约不可续期，对话框 30 秒无人操作自动拒绝。</p></div>
          <button className={button} role="switch" aria-checked={desktopOptin === true} aria-label="桌面接管申请" disabled={!!busy || desktopOptin === null} onClick={saveDesktopOptin}>{desktopOptin === null ? '未获取' : desktopOptin ? '已允许申请' : '已禁止'}</button>
        </div>
        <div className="flex items-center justify-between gap-3 pt-2">
          <div><p className="text-gray-900">任务画面推送</p><p className="mt-1">仅临时查看当前任务；画面来自 Companion 任务浏览器，不可用时明确提示，不切换你的标签页。临时帧不持久保存。</p></div>
          <button className={button} role="switch" aria-checked={frames === true} aria-label="任务画面推送" disabled={!!busy || frames === null} onClick={saveFrames}>{frames === null ? '未获取' : frames ? '已开启' : '已关闭'}</button>
        </div>
        <div className="flex items-center justify-between gap-3 pt-2">
          <div><p className="text-gray-900">截图证据保存</p><p className="mt-1">默认关闭。另行授权后，已接收的任务浏览器截图写入 downloads/computer_use/ 并保留（每任务最多 20 张、至少间隔 10 秒、不自动删除）；关闭不会删除既有证据。真实桌面画面只在接管时推送：需在本机批准时勾选 view 范围，且只截被接管的那个窗口，锁屏或密码框有焦点时自动暂停采集。</p></div>
          <button className={button} role="switch" aria-checked={persistShots === true} aria-label="截图证据保存" disabled={!!busy || persistShots === null} onClick={savePersistShots}>{persistShots === null ? '未获取' : persistShots ? '已开启' : '已关闭'}</button>
        </div>
      </section>
      {busy && <p role="status">{busy}…</p>}
      {note && <p role="status">{note}</p>}
      {error && <p role="alert" className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-gray-900">{error}。网络中断时请刷新核对，不要重复提交。</p>}
    </div>
  );
}
