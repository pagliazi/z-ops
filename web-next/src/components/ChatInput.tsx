'use client';

import { useState, useRef, useCallback } from 'react';
import { ArrowUp, Loader2, Brain, Database, Terminal, Code, MessageSquare, Sparkles, BookOpen } from 'lucide-react';

interface AgentOption {
  value: string;
  label: string;
  icon: React.ReactNode;
  color: string;
  activeBg: string;
  activeBorder: string;
}

const AGENT_OPTIONS: AgentOption[] = [
  { value: '', label: '自动', icon: <Sparkles size={13} />, color: 'var(--cf-orange)', activeBg: 'rgba(37,99,235,0.08)', activeBorder: 'rgba(37,99,235,0.3)' },
  { value: 'kb_agent', label: '知识库', icon: <BookOpen size={13} />, color: '#6366F1', activeBg: 'rgba(99,102,241,0.08)', activeBorder: 'rgba(99,102,241,0.3)' },
  { value: 'gcoc_agent', label: 'GCOC', icon: <Database size={13} />, color: '#059669', activeBg: 'rgba(5,150,105,0.08)', activeBorder: 'rgba(5,150,105,0.3)' },
  { value: 'ops_agent', label: '运维', icon: <Terminal size={13} />, color: '#D97706', activeBg: 'rgba(217,119,6,0.08)', activeBorder: 'rgba(217,119,6,0.3)' },
  { value: 'dev_agent', label: '脚本', icon: <Code size={13} />, color: '#0891B2', activeBg: 'rgba(8,145,178,0.08)', activeBorder: 'rgba(8,145,178,0.3)' },
  { value: 'general_agent', label: '通用', icon: <MessageSquare size={13} />, color: '#6B7280', activeBg: 'rgba(107,114,128,0.08)', activeBorder: 'rgba(107,114,128,0.3)' },
];

interface ChatInputProps {
  onSend: (message: string, targetAgent?: string) => void;
  isLoading?: boolean;
  placeholder?: string;
}

export function ChatInput({ onSend, isLoading = false, placeholder = '输入消息，按 Enter 发送...' }: ChatInputProps) {
  const [input, setInput] = useState('');
  const [targetAgent, setTargetAgent] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const currentAgent = AGENT_OPTIONS.find(a => a.value === targetAgent) || AGENT_OPTIONS[0];

  const handleSubmit = useCallback(() => {
    if (!input.trim() || isLoading) return;
    onSend(input.trim(), targetAgent || undefined);
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [input, isLoading, onSend, targetAgent]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }, [handleSubmit]);

  const handleInput = useCallback((e: React.FormEvent<HTMLTextAreaElement>) => {
    const target = e.currentTarget;
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
  }, []);

  return (
    <div className="px-4 pt-2 pb-4" style={{ background: 'var(--bg-primary)' }}>
      <div className="max-w-5xl mx-auto">
        {/* Input container — ChatGPT-style large rounded box */}
        <div className="rounded-[26px] transition-all duration-200 bg-white"
             style={{
               border: '1px solid var(--border-medium)',
               boxShadow: isFocused
                 ? '0 4px 16px rgba(0,0,0,0.08)'
                 : '0 2px 8px rgba(0,0,0,0.04)',
             }}>
          {/* Textarea */}
          <div className="px-5 pt-3.5 pb-1">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              onInput={handleInput}
              onFocus={() => setIsFocused(true)}
              onBlur={() => setIsFocused(false)}
              placeholder={placeholder}
              disabled={isLoading}
              className="w-full resize-none bg-transparent text-[15px] leading-relaxed focus:outline-none disabled:cursor-not-allowed"
              style={{ color: 'var(--text-primary)', minHeight: '28px', maxHeight: '200px' }}
              rows={1}
            />
          </div>

          {/* Bottom toolbar */}
          <div className="flex items-center justify-between pl-3 pr-2.5 pb-2.5 pt-1">
            {/* Agent selector pills */}
            <div className="flex items-center gap-1">
              {AGENT_OPTIONS.map(a => {
                const isActive = targetAgent === a.value;
                return (
                  <button
                    key={a.value}
                    onClick={() => setTargetAgent(a.value)}
                    title={a.label}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded-full text-[12px] font-medium transition-all border"
                    style={{
                      background: isActive ? a.activeBg : 'transparent',
                      color: isActive ? a.color : 'var(--text-tertiary)',
                      borderColor: isActive ? a.activeBorder : 'transparent',
                    }}
                  >
                    {a.icon}
                    <span className="hidden sm:inline">{a.label}</span>
                  </button>
                );
              })}
            </div>

            {/* Send button — ChatGPT-style circular */}
            <button
              onClick={handleSubmit}
              disabled={!input.trim() || isLoading}
              className="flex items-center justify-center w-9 h-9 rounded-full transition-all flex-shrink-0"
              style={{
                background: input.trim() && !isLoading ? 'var(--text-primary)' : 'var(--bg-tertiary)',
                color: input.trim() && !isLoading ? '#fff' : 'var(--text-tertiary)',
              }}
              title={isLoading ? '处理中' : '发送 (Enter)'}
            >
              {isLoading ? (
                <Loader2 className="animate-spin" size={18} />
              ) : (
                <ArrowUp size={18} strokeWidth={2.5} />
              )}
            </button>
          </div>
        </div>

        {/* Hint bar */}
        <div className="flex items-center justify-center gap-3 mt-2 px-1">
          <span className="text-[11px]" style={{ color: 'var(--text-tertiary)' }}>
            <span className="hidden sm:inline">Shift+Enter 换行 · </span>
            路由: <span className="font-medium" style={{ color: currentAgent.color }}>{currentAgent.label}</span>
          </span>
          {input.length > 0 && (
            <span className="text-[11px]" style={{ color: input.length > 4000 ? 'var(--accent-red)' : 'var(--text-tertiary)' }}>
              {input.length} / 4000
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
