'use client';

import { useState, memo } from 'react';
import { motion } from 'framer-motion';
import { MarkdownContent } from './MarkdownContent';
import { ContextMenu, MenuItem } from './ContextMenu';
import { Loader2, Database, Cpu, Zap, Brain, Copy, Check, Download, Pencil, ChevronRight } from 'lucide-react';
import { getToken } from '@/lib/api';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
  agent?: string;
  agents?: string[];     // 参与链: 处理过该消息的所有 agent (多 Agent 协作时全部展示)
  kb_source?: boolean;   // 知识来源标记: 回答引用了内部知识库 (KB 徽章)
  isStreaming?: boolean;
  thinking?: string;
  data_source?: string;
  data_count?: number;
  usage?: { input_tokens?: number; output_tokens?: number };
  skills?: string[];      // 本轮注入的已发布技能 (P2 闭环可见性)
  memory_used?: boolean;  // 是否参考了用户跨会话记忆 (仅本人)
  model?: string;
  progress?: string;
  createdAt?: number;
  file_url?: string;
  file_name?: string;
}

const AGENT_COLORS: Record<string, { bg: string; text: string }> = {
  gcoc_agent:    { bg: '#ECFDF5', text: '#059669' },
  general_agent: { bg: '#F3F4F6', text: '#4B5563' },
  ops_agent:     { bg: '#FFFBEB', text: '#D97706' },
  dev_agent:     { bg: '#ECFEFF', text: '#0891B2' },
  kb_agent:      { bg: '#F5F3FF', text: '#6D28D9' },
  orchestrator:  { bg: '#F5F3FF', text: '#7C3AED' },
};

function AgentBadge({ agent }: { agent: string }) {
  const colors = AGENT_COLORS[agent] || { bg: '#F3F4F6', text: '#6B7280' };
  const label = agent.replace(/_agent$/, '').toUpperCase();
  return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold"
          style={{ background: colors.bg, color: colors.text }}>
      <Cpu size={9} />
      {label}
    </span>
  );
}

/* ── AgentBadges: 多来源徽章行 ──
   优先展示参与链 agents[] (多 Agent 协作全部显示)；
   回答引用了知识库但 kb_agent 不在链中 (如 general_agent 的 KB RAG) 时追加 KB 徽章；
   历史旧消息只有 agent 单字段时回退显示单个徽章。 */
function AgentBadges({ agent, agents, kbSource }: { agent?: string; agents?: string[]; kbSource?: boolean }) {
  let chain: string[] = agents && agents.length > 0 ? [...agents] : (agent ? [agent] : []);
  if (kbSource && !chain.includes('kb_agent')) chain.push('kb_agent');
  if (chain.length === 0) return null;
  return (
    <>
      {chain.map(a => <AgentBadge key={a} agent={a} />)}
    </>
  );
}

function formatTokens(n?: number): string {
  if (!n) return '0';
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatRelativeTime(createdAt?: number): string {
  if (!createdAt) return '';
  const now = Date.now();
  const diff = now - createdAt;
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (seconds < 60) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (hours < 24) return `${hours} 小时前`;
  if (days < 7) return `${days} 天前`;
  return '';
}

/* ── 统一时间标注格式: 短时间 HH:MM; 非当天追加日期 MM/DD ──
   输入兼容 'HH:MM:SS' 与 'YYYY/MM/DD HH:MM:SS' (含 - 分隔) */
function formatChatTime(t?: string): string {
  if (!t) return '';
  const timeMatch = t.match(/(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$/);
  if (!timeMatch) return t;
  // 完整日期 (年-月-日) 优先, 取月/日; 否则尝试无年份的月/日
  const fullDate = t.match(/\d{4}[-/](\d{1,2})[-/](\d{1,2})/);
  const shortDate = t.match(/(?:^|\s)(\d{1,2})[-/](\d{1,2})(?=\s|$)/);
  let datePart = '';
  if (fullDate) {
    datePart = `${fullDate[1]}/${fullDate[2]} `;
  } else if (shortDate) {
    datePart = `${shortDate[1]}/${shortDate[2]} `;
  }
  return `${datePart}${timeMatch[1]}:${timeMatch[2]}`;
}

/* ── 用户头像缩写: 按真实姓名 (display_name) 生成 ──
   中文名取第一个汉字；英文名取每个单词首字母 (最多 2 字符, 如 John Doe→JD)；
   无姓名时兑底 'U'。 */
function getAvatarInitials(name?: string): string {
  if (!name) return 'U';
  const trimmed = name.trim();
  if (!trimmed) return 'U';
  if (/[\u4e00-\u9fff]/.test(trimmed)) {
    return trimmed.charAt(0);  // 中文名: 第一个汉字
  }
  const words = trimmed.split(/[\s._-]+/).filter(Boolean);
  if (words.length > 1) {
    return words.slice(0, 2).map(w => w.charAt(0).toUpperCase()).join('');
  }
  return trimmed.charAt(0).toUpperCase();
}

interface ChatMessageProps {
  message: Message;
  username?: string;
}

/* ── ThinkingIndicator: 文字 + 跳动圆点 (头像由外层统一提供, 保证与正常消息对齐) ── */
function ThinkingIndicator({ progress }: { progress?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col gap-0.5 py-1"
    >
      <div className="flex items-center gap-2">
        <div className="flex gap-1">
          <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--cf-orange)', animationDelay: '0ms' }} />
          <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--cf-orange)', animationDelay: '150ms' }} />
          <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--cf-orange)', animationDelay: '300ms' }} />
        </div>
        <span className="text-xs font-medium" style={{ color: 'var(--text-secondary)' }}>思考中</span>
      </div>
      {progress && (
        <span className="text-[11px] truncate max-w-[300px]" style={{ color: 'var(--text-tertiary)' }}>{progress}</span>
      )}
    </motion.div>
  );
}

/* ── ThinkingSection: 思考链折叠展示 (DeepSeek thinking mode) ── */
function ThinkingSection({ thinking, streaming }: { thinking: string; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-[11px] py-1 select-none"
        style={{ color: 'var(--text-tertiary)' }}
        onMouseEnter={e => (e.currentTarget.style.color = 'var(--text-secondary)')}
        onMouseLeave={e => (e.currentTarget.style.color = 'var(--text-tertiary)')}
      >
        <Brain size={11} />
        {streaming ? '正在深度思考…' : '已深度思考'}
        {streaming && (
          <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: 'var(--cf-orange)' }} />
        )}
        <ChevronRight
          size={11}
          style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}
        />
      </button>
      {open && (
        <div
          className="mt-1 pl-3 border-l-2 text-[11px] leading-relaxed whitespace-pre-wrap max-h-[240px] overflow-y-auto"
          style={{ borderColor: 'var(--border-light)', color: 'var(--text-tertiary)' }}
        >
          {thinking}
        </div>
      )}
    </div>
  );
}

export const ChatMessage = memo(({ message, username }: ChatMessageProps) => {
  const isUser = message.role === 'user';
  const isAssistant = message.role === 'assistant';
  const isThinking = isAssistant && message.isStreaming && !message.content && !message.thinking;
  const [copied, setCopied] = useState(false);

  // username 实参由 Dashboard 传入 displayName || username (优先真实姓名)
  const avatarInitials = getAvatarInitials(username);

  if (isAssistant && !message.content && !message.thinking && !message.isStreaming) {
    return null;
  }

  const handleCopy = async () => {
    if (!message.content) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  /* ── Thinking state: 外层与正常消息一致的头像列, 保持视觉对齐 ── */
  if (isThinking) {
    return (
      <div className="py-3">
        <div className="flex items-start gap-2.5">
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 select-none"
            style={{ background: 'var(--cf-orange)' }}
          >
            <Brain size={16} className="text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <ThinkingIndicator progress={message.progress} />
          </div>
        </div>
      </div>
    );
  }

  /* ── User message — 右侧: 气泡列(气泡+时间戳) + 头像列, 时间戳与气泡右缘对齐 ── */
  if (isUser) {
    const userMenuItems: MenuItem[] = [
      { label: '复制内容', icon: <Copy size={13} />, onClick: handleCopy },
    ];

    return (
      <ContextMenu items={userMenuItems}>
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
          className="py-3"
        >
          <div className="flex items-start justify-end gap-2.5">
            {/* 气泡 + 时间戳列 (右对齐) */}
            <div className="flex flex-col items-end max-w-[70%]">
              <div className="rounded-3xl px-5 py-2.5" style={{ background: 'var(--bg-tertiary)' }}>
                <div className="prose chat-prose max-w-none">
                  <MarkdownContent content={message.content} />
                </div>
              </div>
              {message.timestamp && (
                <span className="text-[10px] mt-1 mr-1.5" style={{ color: 'var(--text-tertiary)' }}>
                  {formatChatTime(message.timestamp)}
                </span>
              )}
            </div>
            {/* 用户头像: display_name 缩写 (中文名取首字, 英文名取词首字母) */}
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-semibold text-white select-none"
              style={{ background: 'var(--cf-orange)' }}
              title={username || ''}
            >
              {avatarInitials}
            </div>
          </div>
        </motion.div>
      </ContextMenu>
    );
  }

  /* ── Assistant message — 左侧: 头像列 + 正文列 (meta bar 末尾统一时间戳) ── */
  const assistantMenuItems: MenuItem[] = [
    { label: '复制内容', icon: <Copy size={13} />, onClick: handleCopy },
  ];

  return (
    <ContextMenu items={assistantMenuItems}>
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
        className="py-3"
      >
      <div className="flex items-start gap-2.5">
        {/* AI 助手头像 (品牌色 + Brain 图标) */}
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 select-none"
          style={{ background: 'var(--cf-orange)' }}
        >
          <Brain size={16} className="text-white" />
        </div>
        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="relative">
            {/* 思考链折叠区 (DeepSeek thinking mode) */}
            {message.thinking && (
              <ThinkingSection thinking={message.thinking} streaming={!!message.isStreaming} />
            )}
            <div className="prose chat-prose max-w-none">
              <MarkdownContent content={message.content} />
            </div>
            {message.isStreaming && message.content && (
              <span className="inline-block w-0.5 h-4 animate-pulse ml-0.5 align-middle"
                    style={{ background: 'var(--cf-orange)' }} />
            )}
          </div>

          {/* Meta bar */}
          {!message.isStreaming && (
            <div className="flex items-center gap-2 mt-2.5 flex-wrap">
              <AgentBadges agent={message.agent} agents={message.agents} kbSource={message.kb_source} />

              {message.skills && message.skills.length > 0 && (
                <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded"
                      style={{ background: 'rgba(16,185,129,0.10)', color: '#059669' }} title="已发布技能 (经生命周期审批)">
                  <Cpu size={9} />
                  {message.skills.slice(0, 2).join(', ')}
                  {message.skills.length > 2 && <span>+{message.skills.length - 2}</span>}
                </span>
              )}

              {message.memory_used && (
                <button
                  onClick={() => window.dispatchEvent(new CustomEvent('zops:open-settings', { detail: { tab: 'memory' } }))}
                  className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded hover:opacity-80"
                  style={{ background: 'rgba(139,92,246,0.10)', color: '#7C3AED' }}
                  title="已参考你的历史会话记忆 (仅本人可见, 点击管理)">
                  <Brain size={9} />
                  记忆
                </button>
              )}

              {message.data_source && (
                <span className="inline-flex items-center gap-1 text-[10px]" style={{ color: 'var(--text-tertiary)' }}>
                  <Database size={9} />
                  {message.data_source}
                  {message.data_count != null && <span>· {message.data_count}条</span>}
                </span>
              )}

              {message.usage && (message.usage.input_tokens || message.usage.output_tokens) && (
                <span className="inline-flex items-center gap-1 text-[10px]" style={{ color: 'var(--text-tertiary)' }}>
                  <Zap size={9} />
                  {formatTokens(message.usage.input_tokens)}→{formatTokens(message.usage.output_tokens)}
                </span>
              )}

              {message.model && (
                <span className="text-[10px]" style={{ color: 'var(--text-tertiary)' }}>
                  {message.model.split('/').pop()}
                </span>
              )}

              {/* File download */}
              {message.file_url && (
                <button
                  onClick={async () => {
                    const token = typeof window !== 'undefined' ? getToken() : '';
                    const sep = message.file_url!.includes('?') ? '&' : '?';
                    const url = `${message.file_url}${sep}token=${token}`;
                    try {
                      const resp = await fetch(url);
                      const ct = resp.headers.get('content-type') || '';
                      if (!resp.ok || ct.includes('application/json')) {
                        alert('下载失败：登录已过期，请刷新页面重新登录后再试');
                        return;
                      }
                      const blob = await resp.blob();
                      const a = document.createElement('a');
                      a.href = URL.createObjectURL(blob);
                      a.download = message.file_name || 'export.csv';
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      URL.revokeObjectURL(a.href);
                    } catch {
                      alert('下载失败：网络错误，请重试');
                    }
                  }}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium transition-colors"
                  style={{ background: 'rgba(37,99,235,0.08)', color: 'var(--cf-orange)', border: '1px solid rgba(37,99,235,0.2)' }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'rgba(37,99,235,0.15)')}
                  onMouseLeave={e => (e.currentTarget.style.background = 'rgba(37,99,235,0.08)')}
                  title={`下载 ${message.file_name || 'CSV 文件'}`}
                >
                  <Download size={11} />
                  {message.file_name || '下载表格'}
                </button>
              )}

              {/* Copy button */}
              {message.content && (
                <button
                  onClick={handleCopy}
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors"
                  style={{ color: 'var(--text-tertiary)' }}
                  onMouseEnter={e => (e.currentTarget.style.color = 'var(--text-secondary)')}
                  onMouseLeave={e => (e.currentTarget.style.color = 'var(--text-tertiary)')}
                >
                  {copied ? <Check size={11} /> : <Copy size={11} />}
                  {copied ? '已复制' : '复制'}
                </button>
              )}

              {message.timestamp && (
                <span className="text-[10px]" style={{ color: 'var(--text-tertiary)' }}>
                  {message.createdAt
                    ? (formatRelativeTime(message.createdAt) || formatChatTime(message.timestamp))
                    : formatChatTime(message.timestamp)}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </motion.div>
    </ContextMenu>
  );
});

ChatMessage.displayName = 'ChatMessage';
