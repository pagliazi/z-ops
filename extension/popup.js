'use strict';

const element = id => document.getElementById(id);
let busy = false;
let closed = false;
let timer;

async function refresh(reconnect = false) {
  if (busy || closed) return;
  busy = true; element('refresh').disabled = true;
  try {
    if (reconnect) await chrome.runtime.sendMessage({ type: 'cu-local-reconnect' });
    const state = await chrome.runtime.sendMessage({ type: 'cu-local-status' });
    if (closed) return;
    if (!state || state.protocol !== 2) throw new Error('本机状态不可用');
    element('status').textContent = state.connected ? '本机通道已连接' : 'Companion 未连接 / 等待本机响应';
    element('message').textContent = state.message || '请在本机核验连接状态';
    element('tasks').textContent = `本扩展会话任务数：${state.tasks || 0} · 截图不可用`;
  } catch {
    if (!closed) {
      element('status').textContent = 'Companion 未连接';
      element('message').textContent = '需安装并注册本机 Native Host，或重新加载扩展后检查。不会改用旧 WSS。';
      element('tasks').textContent = '';
    }
  } finally {
    busy = false;
    if (!closed) element('refresh').disabled = false;
  }
}
async function handoff() {
  if (busy || closed) return;
  busy = true; element('handoff').disabled = true;
  element('handoff-status').textContent = '正在读取当前站点…';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https?:/i.test(tab.url || '')) throw new Error('请先切到要授权的 http(s) 站点标签');
    const result = await chrome.runtime.sendMessage({ type: 'cu-login-handoff', origin: tab.url });
    if (!result || !result.ok) {
      const reasons = { native_not_connected: 'Companion 本机通道未连接',
        no_cookies_for_site: '当前站点没有可用 Cookie', cookies_unavailable: '本机 Cookie 权限不可用',
        invalid_origin: '当前页面不是可授权站点' };
      throw new Error(reasons[result && result.error] || '登录态交接被拒绝');
    }
    // 只显示数量；Cookie 值不进入弹窗、扩展存储或 Z-OPS 服务器。
    element('handoff-status').textContent =
      `已交给本机 Companion：${result.count} 个 Cookie，5 分钟内单次使用。`;
  } catch (e) {
    if (!closed) element('handoff-status').textContent = e instanceof Error ? e.message : '登录态交接失败';
  } finally {
    busy = false;
    if (!closed) element('handoff').disabled = false;
  }
}
element('refresh').addEventListener('click', () => void refresh(true));
element('handoff').addEventListener('click', () => void handoff());
window.addEventListener('unload', () => { closed = true; clearInterval(timer); });
void refresh();
timer = setInterval(() => void refresh(), 2500);
