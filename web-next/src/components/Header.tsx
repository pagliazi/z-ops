'use client';

import { useState, useRef, useEffect } from 'react';
import { LogOut, ChevronDown } from 'lucide-react';

interface User {
  username: string;
  display_name?: string;
  role: string;
}

interface HeaderProps {
  user: User;
  overview?: {
    agents?: Record<string, { status: string }>;
    llm_model?: string;
    llm_real_model?: string;
    llm_model_alias?: boolean;
  };
  onLogout: () => void;
}

function getInitials(name: string): string {
  if (!name) return 'U';
  if (/[\u4e00-\u9fa5]/.test(name)) return name.charAt(0).toUpperCase();
  const words = name.split(/\s+/);
  if (words.length >= 2) return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
  return name.charAt(0).toUpperCase();
}

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    online: 'var(--accent-green)',
    offline: 'var(--accent-red)',
    timeout: '#F59E0B',
    unknown: 'var(--text-tertiary)',
  };
  return <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: colors[status] || colors.unknown }} />;
}

export function Header({ user, overview, onLogout }: HeaderProps) {
  const [showMenu, setShowMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const onlineCount = overview?.agents
    ? Object.values(overview.agents).filter(a => a.status === 'online').length
    : 0;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShowMenu(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <header className="h-12 bg-white flex items-center justify-between px-5 flex-shrink-0 relative"
            style={{ borderBottom: '1px solid var(--border-light)' }}>
      {/* Left: Page title area */}
      <div className="flex items-center gap-2" />

      {/* Center: Agent Status & Model */}
      <div className="absolute left-1/2 -translate-x-1/2 flex items-center gap-4">
        <div className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--text-tertiary)' }}>
          <StatusDot status="online" />
          <span className="font-medium" style={{ color: 'var(--text-secondary)' }}>{onlineCount}</span>
          <span>Agent 在线</span>
        </div>
        <div className="w-px h-3" style={{ background: 'var(--border-light)' }} />
        <div className="text-xs" style={{ color: 'var(--text-tertiary)' }}>
          模型: <span className="font-medium" style={{ color: 'var(--text-secondary)' }}>{overview?.llm_real_model || overview?.llm_model || '-'}</span>
        </div>
      </div>

      {/* Right: User Menu */}
      <div className="relative" ref={menuRef}>
        <button
          onClick={() => setShowMenu(!showMenu)}
          className="flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-gray-50"
        >
          <div className="w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-semibold"
               style={{ background: 'var(--cf-orange)' }}>
            {getInitials(user?.display_name || user?.username || '')}
          </div>
          <div className="text-right hidden sm:block">
            <div className="text-[13px] font-medium leading-tight" style={{ color: 'var(--text-primary)' }}>
              {user?.display_name || user?.username}
            </div>
          </div>
          <ChevronDown
            size={14}
            className="transition-transform"
            style={{ color: 'var(--text-tertiary)', transform: showMenu ? 'rotate(180deg)' : '' }}
          />
        </button>

        {showMenu && (
          <div className="absolute right-0 top-full mt-1.5 w-52 bg-white rounded-xl py-1 z-50 animate-fade-in"
               style={{ border: '1px solid var(--border-light)', boxShadow: 'var(--shadow-lg)' }}>
            <div className="px-4 py-2.5" style={{ borderBottom: '1px solid var(--border-light)' }}>
              <div className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>
                {user?.display_name || user?.username}
              </div>
              <div className="text-xs mt-0.5" style={{ color: 'var(--text-tertiary)' }}>
                @{user?.username} · {user?.role === 'admin' ? '管理员' : '用户'}
              </div>
            </div>
            <button
              onClick={() => { setShowMenu(false); onLogout(); }}
              className="w-full text-left px-4 py-2.5 text-sm flex items-center gap-2 transition-colors hover:bg-red-50"
              style={{ color: 'var(--accent-red)' }}
            >
              <LogOut size={14} />
              退出登录
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
