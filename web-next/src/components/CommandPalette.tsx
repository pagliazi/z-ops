'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { Search, MessageSquare, Brain, Settings, Activity, BookOpen, ShieldAlert, Command, Cpu, ArrowRight, Zap } from 'lucide-react';

interface Action {
  id: string;
  label: string;
  hint?: string;
  icon: any;
  keywords?: string;
  run: () => void;
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [q, setQ] = useState('');
  const [idx, setIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setQ(''); setIdx(0);
        window.dispatchEvent(new Event('zops:open-palette'));
      }
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 30); }, [open]);

  const router = useRouter();
  const nav = (href: string) => () => { router.push(href); onClose(); };
  const openSettings = (tab: string) => () => {
    window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab } }));
    onClose();
  };

  const ACTIONS: Action[] = [
    { id: 'chat', label: '新对话', hint: '智能对话', icon: MessageSquare, keywords: 'chat 对话', run: nav('/') },
    { id: 'memory', label: '记忆设置', hint: '查看/管理你的跨会话记忆', icon: Brain, keywords: 'memory 记忆 设置', run: openSettings('memory') },
    { id: 'settings', label: '打开设置', hint: '通用/数据控制', icon: Settings, keywords: 'settings 设置', run: openSettings('general') },
    { id: 'approvals', label: '审批中心', icon: ShieldAlert, keywords: 'approval 审批', run: nav('/approvals') },
    { id: 'nms', label: 'NMS 告警', icon: Activity, keywords: 'nms 告警 监控', run: nav('/nms') },
    { id: 'kb', label: '知识库', icon: BookOpen, keywords: 'kb 知识', run: nav('/kb') },
    { id: 'capabilities', label: '能力中心', hint: '全部系统能力', icon: Zap, keywords: 'capability 能力 工具 脚本', run: nav('/capabilities') },
    { id: 'agents', label: '系统架构', icon: Cpu, keywords: 'agent 架构', run: nav('/agents') },
  ];

  const filtered = q.trim()
    ? ACTIONS.filter(a => (a.label + ' ' + (a.hint || '') + ' ' + (a.keywords || '')).toLowerCase().includes(q.toLowerCase()))
    : ACTIONS;

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center pt-[18vh]"
         style={{ background: 'rgba(0,0,0,0.4)' }} onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl shadow-2xl overflow-hidden"
           style={{ background: 'var(--bg-secondary, #fff)' }} onClick={e => e.stopPropagation()}>

        {/* 输入行 */}
        <div className="flex items-center gap-2.5 px-4 py-3.5 border-b"
             style={{ borderColor: 'var(--border-color, #e5e7eb)' }}>
          <Search size={16} style={{ color: 'var(--text-tertiary)' }} />
          <input ref={inputRef} value={q} onChange={e => { setQ(e.target.value); setIdx(0); }}
            onKeyDown={e => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setIdx(i => Math.min(i + 1, filtered.length - 1)); }
              if (e.key === 'ArrowUp') { e.preventDefault(); setIdx(i => Math.max(i - 1, 0)); }
              if (e.key === 'Enter' && filtered[idx]) { e.preventDefault(); filtered[idx].run(); }
            }}
            placeholder="输入命令或搜索页面… (↑↓ 选择, Enter 执行)"
            className="flex-1 bg-transparent outline-none text-sm"
            style={{ color: 'var(--text-primary)' }} />
          <span className="text-[10px] px-1.5 py-0.5 rounded border"
                style={{ color: 'var(--text-tertiary)', borderColor: 'var(--border-color, #e5e7eb)' }}>ESC</span>
        </div>

        {/* 结果 */}
        <div className="max-h-72 overflow-y-auto py-1.5">
          {filtered.length === 0 ? (
            <div className="text-center text-xs py-8" style={{ color: 'var(--text-tertiary)' }}>无匹配命令</div>
          ) : filtered.map((a, i) => (
            <button key={a.id} onMouseEnter={() => setIdx(i)} onClick={a.run}
              className="w-full flex items-center gap-3 px-4 py-2.5 text-left"
              style={{ background: i === idx ? 'var(--bg-primary, #f5f5f5)' : 'transparent' }}>
              <a.icon size={15} style={{ color: i === idx ? '#7C3AED' : 'var(--text-tertiary)' }} />
              <span className="text-sm" style={{ color: 'var(--text-primary)' }}>{a.label}</span>
              {a.hint && <span className="text-[11px] ml-1" style={{ color: 'var(--text-tertiary)' }}>{a.hint}</span>}
              {i === idx && <ArrowRight size={13} className="ml-auto" style={{ color: 'var(--text-tertiary)' }} />}
            </button>
          ))}
        </div>

        {/* 底部提示 */}
        <div className="flex items-center gap-1.5 px-4 py-2 border-t text-[10px]"
             style={{ borderColor: 'var(--border-color, #e5e7eb)', color: 'var(--text-tertiary)' }}>
          <Command size={10} />
          <span>Ctrl/⌘ + K 随时唤起 · Z-OPS 键盘优先</span>
        </div>
      </div>
    </div>
  );
}
