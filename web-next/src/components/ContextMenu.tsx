'use client';

import { useEffect, useRef, useState } from 'react';

export interface MenuItem {
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  danger?: boolean;
  divider?: boolean;
}

interface ContextMenuProps {
  items: MenuItem[];
  children: React.ReactNode;
  className?: string;
}

export function ContextMenu({ items, children, className }: ContextMenuProps) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const menuRef = useRef<HTMLDivElement>(null);

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const x = e.clientX;
    const y = e.clientY;
    // 边界检测，避免菜单超出视口
    const menuW = 180;
    const menuH = items.length * 36 + 8;
    setPos({
      x: x + menuW > window.innerWidth ? x - menuW : x,
      y: y + menuH > window.innerHeight ? y - menuH : y,
    });
    setShow(true);
  };

  useEffect(() => {
    if (!show) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShow(false);
      }
    };
    const escHandler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShow(false);
    };
    document.addEventListener('mousedown', handler);
    document.addEventListener('keydown', escHandler);
    return () => {
      document.removeEventListener('mousedown', handler);
      document.removeEventListener('keydown', escHandler);
    };
  }, [show]);

  return (
    <>
      <div onContextMenu={handleContextMenu} className={className}>
        {children}
      </div>
      {show && (
        <div
          ref={menuRef}
          className="fixed z-[9999] min-w-[160px] bg-white rounded-lg py-1 shadow-lg animate-in fade-in duration-100"
          style={{
            left: pos.x,
            top: pos.y,
            border: '1px solid var(--border-light)',
            boxShadow: '0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04)',
          }}
        >
          {items.map((item, idx) => (
            <button
              key={idx}
              onClick={() => {
                item.onClick();
                setShow(false);
              }}
              className={`w-full flex items-center gap-2 px-3 py-2 text-left text-[13px] transition-colors ${
                item.danger
                  ? 'hover:bg-red-50'
                  : 'hover:bg-gray-50'
              }`}
              style={{
                color: item.danger ? 'var(--accent-red)' : 'var(--text-primary)',
                borderTop: item.divider ? '1px solid var(--border-light)' : 'none',
              }}
            >
              {item.icon && <span className="w-4 h-4 flex items-center justify-center">{item.icon}</span>}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
