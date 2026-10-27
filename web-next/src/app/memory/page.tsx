'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

// 记忆管理已并入设置弹窗 (ChatGPT 式); 旧链接重定向到首页并打开记忆设置
export default function MemoryRedirect() {
  const router = useRouter();
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'memory' } }));
    router.replace('/');
  }, [router]);
  return (
    <div className="h-full flex items-center justify-center text-sm" style={{ color: 'var(--text-tertiary)' }}>
      记忆管理已移至「设置」— 正在跳转…
    </div>
  );
}
