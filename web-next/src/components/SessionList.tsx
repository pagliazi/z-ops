'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { MessageSquare, Plus, Trash2, Pencil, Copy } from 'lucide-react';
import { ContextMenu, MenuItem } from '@/components/ContextMenu';

const LIST_MIN = 200;
const LIST_MAX = 480;
const LIST_DEFAULT = 240;

interface Session {
  id: string;
  title: string;
  updated_at: string;
  message_count?: number;
}

interface SessionListProps {
  sessions: Session[];
  currentSessionId: string;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

export function SessionList({
  sessions = [],
  currentSessionId,
  onSelect,
  onCreate,
  onDelete,
  onRename,
}: SessionListProps) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [width, setWidth] = useState(LIST_DEFAULT);
  const [isResizing, setIsResizing] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const widthRef = useRef(LIST_DEFAULT);
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const saved = localStorage.getItem('zops_session_list_width');
    if (saved) {
      const w = parseInt(saved, 10);
      if (!isNaN(w) && w >= LIST_MIN && w <= LIST_MAX) {
        setWidth(w);
        widthRef.current = w;
      }
    }
  }, []);

  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsResizing(true);
    const startX = e.clientX;
    const startWidth = widthRef.current;

    const onMouseMove = (ev: MouseEvent) => {
      const next = Math.min(LIST_MAX, Math.max(LIST_MIN, startWidth + (ev.clientX - startX)));
      widthRef.current = next;
      setWidth(next);
    };
    const onMouseUp = () => {
      setIsResizing(false);
      localStorage.setItem('zops_session_list_width', String(widthRef.current));
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  const formatTime = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));

    if (days === 0) {
      return date.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' });
    } else if (days === 1) {
      return '昨天';
    } else if (days < 7) {
      return `${days}天前`;
    } else {
      return date.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'short', day: 'numeric' });
    }
  };

  const startRename = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title || '新对话');
    setTimeout(() => renameInputRef.current?.select(), 50);
  };

  const commitRename = () => {
    if (renamingId && renameValue.trim()) {
      onRename(renamingId, renameValue.trim());
    }
    setRenamingId(null);
    setRenameValue('');
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameValue('');
  };

  const handleRenameKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') commitRename();
    if (e.key === 'Escape') cancelRename();
  };

  return (
    <div
      className="flex flex-col flex-shrink-0 relative"
      style={{ width: `${width}px`, background: 'var(--bg-secondary)', borderRight: '1px solid var(--border-light)', transition: isResizing ? 'none' : 'width 0.15s ease' }}
    >
      {/* Header */}
      <div className="p-3" style={{ borderBottom: '1px solid var(--border-light)' }}>
        <button
          onClick={onCreate}
          className="w-full flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-[13px] font-medium transition-all"
          style={{
            background: 'var(--cf-orange)',
            color: '#fff',
          }}
          onMouseEnter={e => (e.currentTarget.style.background = 'var(--cf-orange-hover)')}
          onMouseLeave={e => (e.currentTarget.style.background = 'var(--cf-orange)')}
        >
          <Plus size={15} />
          新建对话
        </button>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto p-2">
        {sessions.length === 0 ? (
          <div className="text-center py-10" style={{ color: 'var(--text-tertiary)' }}>
            <MessageSquare size={28} className="mx-auto mb-2 opacity-40" />
            <p className="text-xs">暂无会话</p>
          </div>
        ) : (
          <div className="space-y-0.5">
            {sessions.map((session) => {
              const isActive = currentSessionId === session.id;
              const isHovered = hoveredId === session.id;
              const isRenaming = renamingId === session.id;

              const menuItems: MenuItem[] = [
                { label: '重命名', icon: <Pencil size={13} />, onClick: () => startRename(session) },
                { label: '删除', icon: <Trash2 size={13} />, onClick: () => onDelete(session.id), danger: true, divider: true },
              ];

              return (
                <ContextMenu key={session.id} items={menuItems}>
                  <div
                    onClick={() => !isRenaming && onSelect(session.id)}
                    onMouseEnter={() => setHoveredId(session.id)}
                    onMouseLeave={() => setHoveredId(null)}
                    className="group flex items-center gap-2.5 px-3 py-2.5 rounded-lg cursor-pointer transition-all duration-150"
                    style={{
                      background: isActive ? 'var(--bg-tertiary)' : isHovered ? 'rgba(0,0,0,0.03)' : 'transparent',
                    }}
                  >
                    <MessageSquare size={14} style={{ color: isActive ? 'var(--cf-orange)' : 'var(--text-tertiary)', flexShrink: 0 }} />
                    <div className="flex-1 min-w-0">
                      {isRenaming ? (
                        <input
                          ref={renameInputRef}
                          type="text"
                          value={renameValue}
                          onChange={e => setRenameValue(e.target.value)}
                          onBlur={commitRename}
                          onKeyDown={handleRenameKeyDown}
                          onClick={e => e.stopPropagation()}
                          className="w-full text-[13px] bg-white border rounded px-1.5 py-0.5 outline-none"
                          style={{ borderColor: 'var(--cf-orange)', color: 'var(--text-primary)' }}
                          autoFocus
                        />
                      ) : (
                        <>
                          <div className="text-[13px] truncate" style={{ color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)', fontWeight: isActive ? 500 : 400 }}>
                            {session.title || '新对话'}
                          </div>
                          <div className="text-[11px] flex items-center gap-1 mt-0.5" style={{ color: 'var(--text-tertiary)' }}>
                            <span>{formatTime(session.updated_at)}</span>
                            {session.message_count && session.message_count > 1 && (
                              <span>· {session.message_count} 条</span>
                            )}
                          </div>
                        </>
                      )}
                    </div>

                    {!isRenaming && (isHovered || isActive) && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onDelete(session.id);
                        }}
                        className="p-1 rounded transition-colors opacity-0 group-hover:opacity-100"
                        style={{ color: 'var(--text-tertiary)' }}
                        onMouseEnter={e => {
                          (e.currentTarget as HTMLElement).style.color = 'var(--accent-red)';
                          (e.currentTarget as HTMLElement).style.background = 'rgba(239,68,68,0.08)';
                        }}
                        onMouseLeave={e => {
                          (e.currentTarget as HTMLElement).style.color = 'var(--text-tertiary)';
                          (e.currentTarget as HTMLElement).style.background = 'transparent';
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    )}
                  </div>
                </ContextMenu>
              );
            })}
          </div>
        )}
      </div>

      {/* Resize handle */}
      <div
        onMouseDown={handleResizeStart}
        className="absolute top-0 right-0 h-full w-1.5 cursor-col-resize group"
        style={{ transform: 'translateX(50%)', zIndex: 20 }}
        title="拖拽调整列表宽度"
      >
        <div
          className="absolute top-0 right-1/2 translate-x-1/2 h-full w-px group-hover:bg-[var(--cf-orange)] transition-colors"
          style={{ background: isResizing ? 'var(--cf-orange)' : 'transparent' }}
        />
      </div>
    </div>
  );
}
