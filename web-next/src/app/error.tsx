'use client';

import { useEffect } from 'react';
import { AlertTriangle, RefreshCw, Home } from 'lucide-react';

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('[Z-OPS] Page error:', error);
    // 重新构建后旧标签页可能引用已被替换的 chunk，自动整页刷新一次（60s 内不重复，避免循环）
    const msg = `${error.name} ${error.message}`;
    if (/ChunkLoadError|Loading chunk .* failed|Failed to fetch dynamically imported module/i.test(msg)) {
      const key = 'zops_chunk_reload_at';
      const last = Number(sessionStorage.getItem(key) || 0);
      if (Date.now() - last > 60000) {
        sessionStorage.setItem(key, String(Date.now()));
        window.location.reload();
      }
    }
  }, [error]);

  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="text-center space-y-4 max-w-md">
        <div className="mx-auto w-14 h-14 bg-red-50 rounded-full flex items-center justify-center">
          <AlertTriangle size={28} className="text-red-400" />
        </div>
        <h2 className="text-lg font-semibold text-gray-800">页面加载出错</h2>
        <p className="text-sm text-gray-500">
          {error.message || '发生了未知错误，请尝试刷新页面。'}
        </p>
        <div className="flex items-center justify-center gap-3 pt-2">
          <button
            onClick={reset}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
          >
            <RefreshCw size={14} />
            重试
          </button>
          <a
            href="/"
            className="inline-flex items-center gap-1.5 px-4 py-2 border border-gray-300 text-gray-600 text-sm font-medium rounded-lg hover:bg-gray-50 transition-colors"
          >
            <Home size={14} />
            返回首页
          </a>
        </div>
      </div>
    </div>
  );
}
