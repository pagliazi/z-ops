'use client';

/**
 * 可拖拽调宽 hook: 侧栏/信息面板共用。
 * 用法: const { width, handle } = useResizableWidth('zops_xxx_width', 200, 420, 240);
 *       右缘把手 (默认) — 面板在左侧时用; invert=true 时把手在左缘且拖左=变宽 (最右侧的面板用)。
 * width 挂到 aside 的 style, handle 渲染在面板拖拽边缘。宽度持久化 localStorage。
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export function useResizableWidth(storageKey: string, min: number, max: number, def: number,
                                  invert = false) {
  const [width, setWidth] = useState(def);
  const [dragging, setDragging] = useState(false);
  const wRef = useRef(def);

  useEffect(() => {
    try {
      const saved = parseInt(localStorage.getItem(storageKey) || '', 10);
      if (!isNaN(saved) && saved >= min && saved <= max) {
        setWidth(saved);
        wRef.current = saved;
      }
    } catch { /* localStorage 不可用 */ }
  }, [storageKey, min, max]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    setDragging(true);
    const startX = e.clientX;
    const startW = wRef.current;
    const move = (ev: PointerEvent) => {
      // invert(左缘把手): 光标左移(dx<0) = 面板变宽
      const dx = invert ? startX - ev.clientX : ev.clientX - startX;
      const next = Math.min(max, Math.max(min, Math.round(startW + dx)));
      wRef.current = next;
      setWidth(next);
    };
    const up = () => {
      setDragging(false);
      try { localStorage.setItem(storageKey, String(wRef.current)); } catch { /* 忽略 */ }
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, [max, min, storageKey, invert]);

  const handle = (
    <div
      onPointerDown={onPointerDown}
      className={`group absolute top-0 h-full w-1.5 cursor-col-resize z-20 ${invert ? 'left-0' : 'right-0'}`}
      style={invert ? { transform: 'translateX(-50%)' } : { transform: 'translateX(50%)' }}
      title="拖拽调整宽度"
    >
      <div className={`absolute top-0 ${invert ? 'left-1/2 -translate-x-1/2' : 'right-1/2 translate-x-1/2'} h-full w-px transition-colors
        ${dragging ? 'bg-[#ff6a00]' : 'bg-transparent group-hover:bg-[#c9c9c9]'}`} />
    </div>
  );

  return { width, handle, dragging };
}
