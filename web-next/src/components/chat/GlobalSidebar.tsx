'use client';

/**
 * 全局导航侧栏 (全站唯一, 所有路由常驻 — 根治布局跳变)
 * 组成: logo/折叠 · 系统导航(权限过滤) · 底部用户块(设置/退出)
 * 2026-10-18 拆分: 会话列表独立为对话页专属左栏 (ChatApp 内 SessionPanel),
 * 导航栏不再混排会话记录 (此前挤在导航下方, 且 s.id 字段错误致点击跳 undefined)。
 */
import { useState, useEffect } from 'react';
import { usePathname } from 'next/navigation';
import Link from 'next/link';
import {
  MessageSquare as NavChat, Activity, BookOpen, ShieldAlert, Cpu, FileText,
  Eye, Workflow, Bell, Settings as NavSettings, Sparkles, Zap,
} from 'lucide-react';

/* 系统导航 — 与原侧栏完全一致的清单与权限语义 (12 项, 无一遗漏) */
const NAV_SECTIONS: { title?: string; items: NavItem[] }[] = [
  { items: [
    { href: '/', icon: NavChat, label: '智能对话' },
    { href: '/nms', icon: Activity, label: 'NMS 告警', matchPaths: ['/nms'] },
    { href: '/kb', icon: BookOpen, label: '知识库', matchPaths: ['/kb'] },
    { href: '/approvals', icon: ShieldAlert, label: '审批中心', exact: true, alwaysVisible: true },
  ]},
  { title: '系统', items: [
    { href: '/agents', icon: Cpu, label: '系统架构', matchPaths: ['/agents', '/architecture'] },
    { href: '/llm-wiki', icon: Sparkles, label: 'LLM-Wiki', bizPerms: ['knowledge.manage'], matchPaths: ['/llm-wiki'] },
    { href: '/observability', icon: Eye, label: '监控大盘', bizPerms: ['observability.view', 'observability.detail'], matchPaths: ['/observability', '/usage'] },
    { href: '/logs', icon: FileText, label: '运维日志', adminOnly: true },
  ]},
  { title: '自动化', items: [
    { href: '/n8n', icon: Workflow, label: '工作流', bizPerms: ['n8n.operate', 'n8n.manage'], matchPaths: ['/n8n'] },
    { href: '/notify', icon: Bell, label: '通知测试', bizPerms: ['notify.manage'] },
  ]},
  { title: '管理', items: [
    { href: '/admin', icon: NavSettings, label: '系统设置', adminOnly: true, matchPaths: ['/admin'] },
    // 通知配置已并入 admin 子 tab (/admin?tab=notify), 不再单列导航
  ]},
];

interface NavItem {
  href: string; icon: any; label: string;
  adminOnly?: boolean; bizPerms?: string[]; matchPaths?: string[];
  exact?: boolean; alwaysVisible?: boolean;
}

/* 姓名缩写头像: 中文取首字, 英文取前两词首字母 (替代单字母 O) */
export function initialsOf(name?: string): string {
  const n = (name || '').trim();
  if (!n) return '?';
  if (/[\u4e00-\u9fa5]/.test(n)) return n.slice(0, 1);
  const words = n.split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return n.slice(0, 2).toUpperCase();
}
import {
  PanelLeftClose, Settings, LogOut, ChevronDown,
} from 'lucide-react';
import { useResizableWidth } from '@/components/chat/ResizeHandle';

export function GlobalSidebar({ onLogout }: { onLogout: () => void }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(true);
  const [userMenu, setUserMenu] = useState(false);
  const [user, setUser] = useState<any>({});
  // 可拖拽调宽 (200-420, 默认 240): 导航内容不再局促, 宽度记忆到 localStorage
  const { width, handle } = useResizableWidth('zops_nav_width', 200, 420, 240);
  useEffect(() => {
    try { setUser(JSON.parse(localStorage.getItem('zops_user') || '{}')); } catch { setUser({}); }
    fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${localStorage.getItem('zops_token') || ''}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUser((p: any) => ({ ...p, ...d })); })
      .catch(() => {});
  }, []);

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        className="fixed left-3 top-3 z-30 p-2 rounded-lg hover:bg-black/5 bg-white/80 backdrop-blur border border-[#ececec]"
        title="展开侧栏">
        <PanelLeftClose size={16} className="text-[#8f8f8f] rotate-180" />
      </button>
    );
  }

  return (
    <aside className="shrink-0 flex flex-col bg-[#f9f9f9] border-r border-[#ececec] h-screen sticky top-0 relative"
           style={{ width: `${width}px` }}>
      {handle}
      <div className="p-2.5 flex items-center justify-between">
        <Link href="/" className="font-semibold text-sm px-2 hover:opacity-80">智 OC</Link>
        <button onClick={() => setOpen(false)} className="p-1.5 rounded-lg hover:bg-black/5" title="收起">
          <PanelLeftClose size={16} className="text-[#8f8f8f]" />
        </button>
      </div>

      {/* 系统导航 (纯导航; 会话记录在智能对话页专属左栏) */}
      <nav className="flex-1 overflow-y-auto px-2 py-1.5">
        {NAV_SECTIONS.map((sec, si) => {
          const isAdmin = user.role === 'admin';
          const visibleTabs = user.visible_tabs as string[] | undefined;
          const tabVisible = (it: NavItem) => {
            if (isAdmin || it.alwaysVisible) return true;
            if (!visibleTabs || visibleTabs.length === 0) return true;
            return visibleTabs.includes(it.href);
          };
          const bizVisible = (it: NavItem) =>
            !it.bizPerms?.length || isAdmin || it.bizPerms.some(p => (user.biz_perms || []).includes(p));
          const visible = sec.items.filter(it =>
            (!it.adminOnly || isAdmin) && tabVisible(it) && bizVisible(it));
          if (!visible.length) return null;
          return (
            <div key={si} className="mb-1">
              {sec.title && <div className="px-3 pt-2 pb-1 text-[10px] font-medium text-[#b4b4b4]">{sec.title}</div>}
              {visible.map(it => {
                const active = it.exact ? pathname === it.href
                  : (it.matchPaths ? it.matchPaths.some(p => pathname === p || pathname.startsWith(p + '/'))
                  : pathname === it.href);
                return (
                  <Link key={it.href} href={it.href}
                    className={`flex items-center gap-2.5 px-3 py-[7px] rounded-lg text-[13px] transition-colors
                      ${active ? 'bg-black/[0.06] font-semibold text-[#0d0d0d]' : 'text-[#565869] hover:bg-black/[0.04] hover:text-[#0d0d0d]'}`}>
                    <it.icon size={14} className={active ? 'text-[#0d0d0d]' : 'text-[#8f8f8f]'} />
                    {it.label}
                  </Link>
                );
              })}
            </div>
          );
        })}
      </nav>

      {/* 底部用户块: 设置 / 退出 */}
      <div className="p-2.5 border-t border-[#ececec] relative">
        {userMenu && (
          <div className="absolute bottom-full left-2.5 right-2.5 mb-1 rounded-xl border border-[#ececec] bg-white shadow-lg py-1 z-20">
            <button onClick={() => { setUserMenu(false); window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'general' } })); }}
              className="w-full flex items-center gap-2.5 px-3.5 py-2.5 text-[13px] hover:bg-black/[0.03]">
              <Settings size={14} className="text-[#8f8f8f]" /> 设置
            </button>
            <button onClick={() => { setUserMenu(false); onLogout(); }}
              className="w-full flex items-center gap-2.5 px-3.5 py-2.5 text-[13px] hover:bg-black/[0.03] text-red-600">
              <LogOut size={14} /> 退出登录
            </button>
          </div>
        )}
        <button onClick={() => setUserMenu(v => !v)}
          className="w-full flex items-center gap-2.5 px-2 py-2 rounded-xl hover:bg-black/[0.04] text-left">
          <div className="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold text-white shrink-0"
               style={{ background: 'linear-gradient(135deg,#7C3AED,#4F46E5)' }}>
            {initialsOf(user.display_name || user.username)}
          </div>
          <span className="text-[13px] truncate flex-1">{user.display_name || user.username || '用户'}</span>
          <ChevronDown size={13} className="text-[#8f8f8f] shrink-0" />
        </button>
      </div>
    </aside>
  );
}
