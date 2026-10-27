'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import {
  MessageSquare, Cpu, FileText, Eye,
  Workflow, Settings, BookOpen, Activity, Sparkles, ShieldAlert, Bell
} from 'lucide-react';

interface SidebarProps {
  isAdmin: boolean;
  visibleTabs?: string[];
  bizPerms?: string[];
  version?: string; // 由 /api/overview 下发 (源自后端 config.VERSION)，禁止硬编码
  username?: string;
  embedded?: boolean;  // 嵌入模式 (ChatApp 会话栏内): 无外框/无 logo/无底部用户块
}

interface NavSection {
  title?: string;
  items: { href: string; icon: React.ElementType; label: string; adminOnly: boolean; matchPaths?: string[]; exact?: boolean; bizPerms?: string[]; alwaysVisible?: boolean }[];  // bizPerms: 任一持有即可见 (any-of); alwaysVisible: 全员默认入口, 不受组 TAB 白名单过滤
}

const NAV_SECTIONS: NavSection[] = [
  {
    items: [
      { href: '/', icon: MessageSquare, label: '智能对话', adminOnly: false },
      { href: '/nms', icon: Activity, label: 'NMS 告警', adminOnly: false, matchPaths: ['/nms'] },
      { href: '/kb', icon: BookOpen, label: '知识库', adminOnly: false, matchPaths: ['/kb'] },
      // 审批中心为全员默认入口 (申请/查询审批为基本能力), 不受组 TAB 白名单限制
      { href: '/approvals', icon: ShieldAlert, label: '审批中心', adminOnly: false, exact: true, alwaysVisible: true },
    ],
  },
  {
    title: '系统',
    items: [
      { href: '/agents', icon: Cpu, label: '系统架构', adminOnly: false, matchPaths: ['/agents', '/architecture'] },
      // 业务权限入口 (admin 审计查看 或 持对应业务权限点可见)
      { href: '/llm-wiki', icon: Sparkles, label: 'LLM-Wiki', adminOnly: false, bizPerms: ['knowledge.manage'], matchPaths: ['/llm-wiki'] },
      { href: '/observability', icon: Eye, label: '监控大盘', adminOnly: false, bizPerms: ['observability.view', 'observability.detail'], matchPaths: ['/observability', '/usage'] },
      { href: '/logs', icon: FileText, label: '运维日志', adminOnly: true },
    ],
  },
  {
    title: '自动化',
    items: [
      { href: '/n8n', icon: Workflow, label: '工作流', adminOnly: false, bizPerms: ['n8n.operate', 'n8n.manage'], matchPaths: ['/n8n'] },
      { href: '/notify', icon: Bell, label: '通知测试', adminOnly: false, bizPerms: ['notify.manage'] },
    ],
  },
  {
    title: '管理',
    items: [
      { href: '/admin', icon: Settings, label: '系统设置', adminOnly: true, matchPaths: ['/admin'] },
      { href: '/notify-config', icon: Bell, label: '通知配置', adminOnly: true, matchPaths: ['/notify-config'] },
    ],
  },
];

export function Sidebar({ isAdmin, visibleTabs, bizPerms, version, username, embedded }: SidebarProps) {
  const pathname = usePathname();

  // 组别权限过滤: 若 visibleTabs 存在且非空，只显示列表中的tab；管理员不受限制
  // alwaysVisible 项 (审批中心) 为全员默认入口, 不受白名单过滤
  const isTabVisible = (href: string, alwaysVisible?: boolean) => {
    if (isAdmin || alwaysVisible) return true;
    if (!visibleTabs || visibleTabs.length === 0) return true;
    return visibleTabs.includes(href);
  };
  // 业务权限入口: admin (审计查看) 或持有对应业务权限点

  return (
    <aside className={embedded
      ? "w-full flex flex-col"
      : "zops-sidebar w-52 flex flex-col flex-shrink-0"}>
      {/* Logo */}
      <div className="px-4 pt-4 pb-2">
        {!embedded && (<Link href="/" className="flex items-center gap-2.5 group">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
               style={{ background: 'var(--cf-orange)' }}>
            Z
          </div>
          <div>
            <div className="text-white font-semibold text-sm leading-tight tracking-tight">
              智OC
              {version && (
                <span className="ml-1.5 text-[9px] font-medium px-1.5 py-0.5 rounded-full"
                      style={{ background: 'rgba(37,99,235,0.15)', color: 'var(--cf-orange)' }}>
                  {version}
                </span>
              )}
            </div>
            <div className="text-[10px] leading-tight" style={{ color: 'var(--text-tertiary)' }}>
              Operation Claw
            </div>
          </div>
        </Link>)}
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-3 space-y-4 overflow-y-auto">
        {NAV_SECTIONS.map((section, si) => {
          const visibleItems = section.items.filter(item => {
            if (item.adminOnly && !isAdmin) return false;
            if (item.bizPerms && !isAdmin && !(item.bizPerms || []).some(p => (bizPerms || []).includes(p))) return false;
            return isTabVisible(item.href, item.alwaysVisible);
          });
          if (visibleItems.length === 0) return null;
          return (
            <div key={si}>
              {section.title && (
                <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-widest"
                     style={{ color: 'var(--text-tertiary)' }}>
                  {section.title}
                </div>
              )}
              <div className="space-y-0.5">
                {visibleItems.map(item => {
                  const Icon = item.icon;
                  const active = item.exact
                    ? pathname === item.href
                    : (item.matchPaths
                      ? item.matchPaths.some(p => pathname === p || pathname.startsWith(p + '/'))
                      : pathname === item.href);
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] font-medium transition-all duration-150 relative ${
                        active
                          ? 'text-white'
                          : 'hover:text-white'
                      }`}
                      style={{
                        background: active ? 'var(--bg-sidebar-active)' : 'transparent',
                        color: active ? 'var(--text-sidebar-active)' : 'var(--text-sidebar)',
                      }}
                      onMouseEnter={e => {
                        if (!active) (e.currentTarget as HTMLElement).style.background = 'var(--bg-sidebar-hover)';
                      }}
                      onMouseLeave={e => {
                        if (!active) (e.currentTarget as HTMLElement).style.background = 'transparent';
                      }}
                    >
                      {active && (
                        <div className="absolute left-0 w-[3px] h-5 rounded-r-full"
                             style={{ background: 'var(--cf-orange)' }} />
                      )}
                      <Icon size={16} style={{ opacity: active ? 1 : 0.7 }} />
                      <span>{item.label}</span>
                    </Link>
                  );
                })}
              </div>
            </div>
          );
        })}
      </nav>

      {/* Bottom: 用户块 (ChatGPT 式 — 点击打开设置) + 运行状态 (embedded 模式隐藏) */}
      {!embedded && (
      <div className="px-3 py-3 border-t" style={{ borderColor: 'rgba(255,255,255,0.08)' }}>
        <button
          onClick={() => window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'memory' } }))}
          className="w-full flex items-center gap-2.5 px-2 py-2 rounded-lg hover:bg-white/5 transition-colors text-left">
          <div className="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold text-white shrink-0"
               style={{ background: 'linear-gradient(135deg, #7C3AED, #4F46E5)' }}>
            {(username || '?').slice(0, 1).toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-medium truncate" style={{ color: 'var(--text-primary)' }}>{username || '用户'}</div>
            <div className="text-[10px]" style={{ color: 'var(--text-tertiary)' }}>设置 · 记忆 · 数据</div>
          </div>
        </button>
        <div className="flex items-center gap-2 text-[11px] mt-2 px-2" style={{ color: 'var(--text-tertiary)' }}>
          <div className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--accent-green)' }} />
          <span>系统运行中</span>
        </div>
      </div>)}
    </aside>
  );
}
