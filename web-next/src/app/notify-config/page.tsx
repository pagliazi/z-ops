'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

// 通知配置已并入 系统设置(/admin) 的「通知配置」tab — 这里仅做直达跳转
export default function NotifyConfigPage() {
  const router = useRouter();
  useEffect(() => { router.replace('/admin?tab=notify'); }, [router]);
  return null;
}
