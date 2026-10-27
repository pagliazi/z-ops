'use client';

import { useState, useEffect } from 'react';
import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { LoginPage } from '@/components/LoginPage';
import { GlobalSidebar } from '@/components/chat/GlobalSidebar';
import { SettingsModal } from '@/components/SettingsModal';
import { CommandPalette } from '@/components/CommandPalette';
import { ShieldX } from 'lucide-react';

// 系统管理专属路由 (纯 admin; 审计与系统配置)
const ADMIN_ONLY_ROUTES = ['/admin', '/logs'];
// 业务权限路由: 需要指定业务权限点 (admin 可审计查看); 与系统角色分离
// 权限点目录由后端 core/bizperms.py BIZ_PERMS 统一注册, 此处仅维护路由→权限点映射
const BIZ_PERM_ROUTES: { prefix: string; perms: string[] }[] = [
  // perms: 任一持有即可访问 (any-of); 权限点命名 = 域.动作级 (后端 core/bizperms.py 统一注册)
  { prefix: '/llm-wiki', perms: ['knowledge.manage'] },
  { prefix: '/n8n', perms: ['n8n.operate', 'n8n.manage'] },
  { prefix: '/notify', perms: ['notify.manage'] },
  { prefix: '/notify-config', perms: ['notify.manage'] },
  { prefix: '/observability', perms: ['observability.view', 'observability.detail'] },
  { prefix: '/usage', perms: ['observability.view', 'observability.detail'] },
];

interface User {
  username: string;
  display_name?: string;
  role: string;
  group?: string;
  visible_tabs?: string[];
  biz_perms?: string[];
}

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoggedIn, setIsLoggedIn] = useState(false);
  const [overview, setOverview] = useState<any>(null);
  const [checking, setChecking] = useState(true);
  const pathname = usePathname();

  // 设置弹窗 (ChatGPT 式) + 命令面板 (Claude Code 式 Ctrl+K)
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<'general' | 'memory' | 'web' | 'data'>('memory');
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    const openSettings = (e: Event) => {
      const tab = (e as CustomEvent).detail?.tab;
      if (tab === 'general' || tab === 'memory' || tab === 'data' || tab === 'web') setSettingsTab(tab);
      else if (tab === 'computer' || tab === 'computer-use') setSettingsTab('web');
      setSettingsOpen(true);
    };
    const openPalette = () => setPaletteOpen(true);
    window.addEventListener('zops:open-settings', openSettings);
    window.addEventListener('zops:open-palette', openPalette);
    return () => {
      window.removeEventListener('zops:open-settings', openSettings);
      window.removeEventListener('zops:open-palette', openPalette);
    };
  }, []);

  useEffect(() => {
    const token = localStorage.getItem('zops_token');
    const savedUser = localStorage.getItem('zops_user');
    if (token && savedUser) {
      try {
        const parsed = JSON.parse(savedUser);
        setUser(parsed);
        setIsLoggedIn(true);
      } catch {
        localStorage.removeItem('zops_user');
      }
    }
    setChecking(false);
  }, []);

  useEffect(() => {
    // 依赖仅 isLoggedIn: loadUserInfo 内 setUser({...prev}) 每次产生新引用,
    // 若把 user 放进依赖会造成 fetch→setUser→重跑 的无限循环 (me/overview 高频对刷)
    if (!isLoggedIn) return;
    loadOverview();
    loadUserInfo();
    // 每 30 秒刷新一次 overview 数据，确保 Agent 状态和模型信息准确
    const interval = setInterval(loadOverview, 30000);
    return () => clearInterval(interval);
  }, [isLoggedIn]);

  const loadUserInfo = async () => {
    try {
      const token = localStorage.getItem('zops_token') || '';
      const res = await fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${token}` } });
      if (res.ok) {
        const d = await res.json();
        setUser(prev => prev ? { ...prev, group: d.group, visible_tabs: d.visible_tabs, biz_perms: d.biz_perms || [] } : prev);
        // 同步到 localStorage
        const saved = localStorage.getItem('zops_user');
        if (saved) {
          const parsed = JSON.parse(saved);
          parsed.group = d.group;
          parsed.visible_tabs = d.visible_tabs;
          parsed.biz_perms = d.biz_perms || [];
          localStorage.setItem('zops_user', JSON.stringify(parsed));
        }
      }
    } catch (e) { console.error('Failed to load user info:', e); }
  };

  const loadOverview = async () => {
    try {
      const token = localStorage.getItem('zops_token') || '';
      const res = await fetch('/api/overview', {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (res.ok) {
        setOverview(await res.json());
      }
    } catch (e) {
      console.error('Failed to load overview:', e);
    }
  };

  const handleLogin = (userData: User & { token: string }) => {
    setUser(userData);
    setIsLoggedIn(true);
  };

  const handleLogout = () => {
    localStorage.removeItem('zops_token');
    localStorage.removeItem('zops_user');
    setUser(null);
    setIsLoggedIn(false);
    setOverview(null);
    setSettingsOpen(false);
  };

  if (checking) return null;
  if (!isLoggedIn) return <LoginPage onLogin={handleLogin} />;

  const isAdmin = user?.role === 'admin';
  const isAdminRoute = ADMIN_ONLY_ROUTES.some(r => pathname === r || pathname.startsWith(r + '/'));
  // 组别权限: 若用户有visible_tabs配置，则根据配置判断是否可访问
  const groupAllowed = user?.visible_tabs && user.visible_tabs.length > 0
    ? user.visible_tabs.some(t => pathname === t || pathname.startsWith(t + '/'))
    : true;
  // 业务权限路由: admin (审计查看) 或持有对应业务权限点
  const bizPermAllowed = BIZ_PERM_ROUTES.every(
    bp => !(pathname === bp.prefix || pathname.startsWith(bp.prefix + '/'))
      || isAdmin || bp.perms.some(p => (user?.biz_perms || []).includes(p))
  );
  const accessDenied = (isAdminRoute && !isAdmin && !groupAllowed) || !bizPermAllowed;

  // 版本号唯一真源在后端 config.VERSION，经 /api/overview 下发；只取 major.minor 展示
  const version = overview?.version ? `v${String(overview.version).split('.').slice(0, 2).join('.')}` : undefined;


  // 统一壳 (ChatGPT 范式): 全站唯一 GlobalSidebar 常驻 — 任何路由切换零布局跳变
  return (
    <div className="h-screen flex bg-white">
      <GlobalSidebar onLogout={handleLogout} />
      <div className="flex-1 flex flex-col overflow-hidden bg-[#F7F7F8]">
        <main className="flex-1 flex flex-col overflow-y-auto">
          {accessDenied ? (
            <div className="flex-1 flex items-center justify-center">
              <div className="text-center space-y-4 max-w-sm">
                <div className="mx-auto w-14 h-14 rounded-full flex items-center justify-center"
                     style={{ background: 'rgba(239,68,68,0.08)' }}>
                  <ShieldX size={28} style={{ color: 'var(--accent-red)' }} />
                </div>
                <h2 className="text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>无访问权限</h2>
                <p className="text-sm" style={{ color: 'var(--text-tertiary)' }}>
                  此页面仅管理员可访问。如需权限，请联系系统管理员。
                </p>
                <Link href="/" className="inline-block px-4 py-2 text-white text-sm font-medium rounded-lg transition-colors"
                      style={{ background: 'var(--cf-orange)' }}>
                  返回首页
                </Link>
              </div>
            </div>
          ) : children}
        </main>
        {/* Footer: 高度与侧边栏底部“系统运行状态”区保持一致 (同 py-3 + text-[11px]) */}
        <footer className="flex items-center justify-center flex-shrink-0 py-3 text-[11px] tracking-wide"
                style={{ borderTop: '1px solid var(--border-light)', color: 'var(--text-tertiary)', background: 'var(--bg-primary)' }}>
          © 2024-2026 Z-OPS Project | Apache-2.0
        </footer>
      </div>
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)}
                    initialTab={settingsTab} username={user?.username}
                    displayName={user?.display_name} role={user?.role} onLogout={handleLogout} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
