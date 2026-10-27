'use client';

import { Monitor } from 'lucide-react';

export function BrowserConnectCard({ variant = 'message' }: { variant?: 'message' | 'panel' }) {
  return (
    <div className={`${variant === 'message' ? 'ml-8 mt-3' : 'mt-3'} rounded-xl border border-gray-200 bg-gray-50 p-3.5 text-xs text-gray-600`}>
      <h3 className="flex items-center gap-2 text-sm font-medium text-gray-900"><Monitor size={14} />连接本机 Companion</h3>
      <p className="mt-2 leading-relaxed">请在“设置 → 电脑操作”查看安装说明、生成配对码并明确选择设备。配对与本机授权在 Companion 确认，扩展弹窗只显示状态。</p>
      <p className="mt-2 leading-relaxed">原生客户端尚未完整可用，Windows 构建尚未签名；仅安装扩展不代表已连接，也不会降级到旧服务器连接。</p>
      <button onClick={() => window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'web' } }))}
        className="mt-3 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-gray-900 hover:bg-gray-100">打开电脑操作设置</button>
      <p className="mt-2 text-[11px] leading-relaxed">连接后不会自动重放旧任务。请在任务小窗核验状态；登录、桌面同意与安全继续均需在本机确认，unknown 写操作不得自动重试。</p>
    </div>
  );
}

export default BrowserConnectCard;
