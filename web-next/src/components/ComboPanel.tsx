'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

// fixed 下拉面板: portal 到 body 脱离祖先 overflow 裁剪 (弹窗/滚动区内不再被挡);
// 下方空间不足自动向上翻转 (底边贴输入框, 内容自适应高度不悬空); resize/scroll (捕获) 实时跟随锚点
export function ComboPanel({ anchor, maxHeight = 224, children }: {
  anchor: HTMLElement | null; maxHeight?: number; children: ReactNode;
}) {
  // top/bottom 二选一: 向下=顶边贴输入框下沿; 向上=底边贴输入框上沿 (高度由内容自适应, 不悬空)
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; width: number } | null>(null);

  useEffect(() => {
    if (!anchor) return;
    const calc = () => {
      const r = anchor.getBoundingClientRect();
      const down = r.bottom + maxHeight + 8 <= window.innerHeight;
      setPos({
        left: Math.max(8, Math.min(r.left, window.innerWidth - r.width - 8)),
        width: r.width,
        ...(down
          ? { top: r.bottom + 4 }
          : { bottom: window.innerHeight - r.top + 4 }),
      });
    };
    calc();
    window.addEventListener('resize', calc);
    window.addEventListener('scroll', calc, true);
    return () => {
      window.removeEventListener('resize', calc);
      window.removeEventListener('scroll', calc, true);
    };
  }, [anchor, maxHeight]);

  if (!pos) return null;
  return createPortal(
    <div data-combo-panel className="fixed z-[60] overflow-y-auto bg-white border border-[#E5E7EB] rounded-lg shadow-xl"
      style={{ left: pos.left, width: pos.width, maxHeight, ...(pos.top !== undefined ? { top: pos.top } : { bottom: pos.bottom }) }}>
      {children}
    </div>,
    document.body);
}
