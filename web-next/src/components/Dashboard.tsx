'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { SessionList } from '@/components/SessionList';
import { ChatMessage } from '@/components/ChatMessage';
import { ChatInput } from '@/components/ChatInput';
import {
  getSessions,
  createSession,
  deleteSession,
  renameSession,
  getSessionMessages,
  getSessionPending,
  sendMessageStream,
  catchUpTaskEvents,
  getUsage,
  resumeSessionContext,
  ChatMessage as ChatMessageType,
  Session,
  UsageStats
} from '@/lib/api';
import { MessageSquare, Zap, ArrowDownUp, Clock, ArrowDown } from 'lucide-react';

export default function Dashboard() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  // P1 可见性: 断线恢复提示 + 进行中任务 (停止按钮)
  const [recoveryNote, setRecoveryNote] = useState('');
  const [activeTaskId, setActiveTaskId] = useState('');
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [sessionUsage, setSessionUsage] = useState<UsageStats | null>(null);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  // v4.0 Phase 3.3: 会话从持久化 transcript 恢复时的提示条
  const [resumeNote, setResumeNote] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  // 流式 delta 渲染节流: 缓冲 + rAF 批量刷新 (避免逐 token 频繁 setState)
  const deltaBufRef = useRef('');   // 正文增量缓冲
  const thinkBufRef = useRef('');   // 思考链增量缓冲 (DeepSeek thinking mode)
  const rafIdRef = useRef<number | null>(null);
  // 切页恢复轮询: 对话中途切页后回页时，轮询会话历史等待回复落库
  const pendingPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 当前活跃会话 ref: 轮询回调中判断用户是否已切换会话 (避免覆盖新会话视图)
  const activeSessionRef = useRef('');
  useEffect(() => { activeSessionRef.current = currentSessionId; }, [currentSessionId]);
  const isNearBottom = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return true;
    const threshold = 100;
    return container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
  }, []);

  useEffect(() => {
    const savedUser = localStorage.getItem('zops_user');
    if (savedUser) {
      try {
        const u = JSON.parse(savedUser);
        setUsername(u.username || '');
        setDisplayName(u.display_name || u.username || '');
      } catch {}
    }
    loadSessions();
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        handleCreateSession();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === '/') {
        e.preventDefault();
        const textarea = document.querySelector('textarea');
        if (textarea) textarea.focus();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // 组件卸载时清理恢复轮询定时器 (避免泄漏)
  useEffect(() => {
    return () => {
      if (pendingPollRef.current) {
        clearInterval(pendingPollRef.current);
        pendingPollRef.current = null;
      }
    };
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    if (shouldAutoScroll && messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior, block: 'end' });
    }
  }, [shouldAutoScroll]);

  // 消息数量变化（刷新加载/切换会话/新增消息）时立即定位到底部，避免平滑滚动造成的弹动
  useEffect(() => { scrollToBottom('auto'); }, [messages.length, scrollToBottom]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const handleScroll = () => {
      const nearBottom = isNearBottom();
      setShouldAutoScroll(nearBottom);
      setShowScrollButton(!nearBottom && messages.length > 0);
    };
    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, [isNearBottom, messages.length]);

  const handleScrollToBottom = () => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
      setShouldAutoScroll(true);
    }
  };

  const loadSessions = async () => {
    try {
      const data = await getSessions();
      const arr = Array.isArray(data) ? data : [];
      // 仅按 id 去重 (防御后端重复项); 同名会话 (如多个"新对话") 必须全部保留
      const seen = new Set<string>();
      const unique = arr.filter(s => {
        if (seen.has(s.id)) return false;
        seen.add(s.id);
        return true;
      });
      setSessions(unique);
      if (unique.length > 0 && !currentSessionId) {
        const latest = unique[0];
        setCurrentSessionId(latest.id);
        loadSessionMessages(latest.id);
      }
    } catch (error) {
      console.error('Failed to load sessions:', error);
      setSessions([]);
    }
  };

  const loadSessionMessages = async (sessionId: string) => {
    // 清除旧会话的恢复轮询 (切换会话时旧轮询不能继续覆盖新会话视图)
    if (pendingPollRef.current) {
      clearInterval(pendingPollRef.current);
      pendingPollRef.current = null;
    }
    setResumeNote('');
    try {
      const data = await getSessionMessages(sessionId);
      const filtered = (data || []).filter(m =>
        m.role === 'user' || (m.content && m.content.trim())
      );
      // 历史消息兼容: Redis 存的是 source 字段，统一映射为前端 agent/agents (多徽章)
      const mapped = filtered.map((m: any) => {
        const agentVal = m.agent || m.source || undefined;
        const agentsVal = m.agents || (agentVal ? [agentVal] : undefined);
        return { ...m, agent: agentVal, agents: agentsVal, kb_source: m.kb_source };
      });
      setMessages(mapped);
      // 切页恢复: 最后一条是用户消息 → 先做权威判定 (问后端任务状态),
      // 失败/无任务且陈旧 → 直接展示失败结论, 不再给出"正在恢复回复"的误导说法;
      // 仅任务确在执行时才进入恢复轮询。
      const lastMsg = mapped[mapped.length - 1];
      if (lastMsg && lastMsg.role === 'user') {
        resolvePendingReply(sessionId, mapped);
      }
    } catch (error) {
      // v4.0 Phase 3.3: Redis 会话过期/丢失 (404) → 降级从持久化 transcript (jsonl) 重建上下文
      try {
        const d = await resumeSessionContext(sessionId);
        const mapped = (d.messages || []).filter((m: any) =>
          m.role === 'user' || (m.content && m.content.trim())
        ).map((m: any, idx: number) => ({
          id: `resume-${sessionId}-${idx}`,
          role: m.role,
          content: m.content,
          timestamp: m.timestamp,
          agent: m.source || m.agent || undefined,
          agents: m.agents || (m.source ? [m.source] : undefined),
          kb_source: m.kb_source,
        }));
        if (mapped.length > 0) {
          setMessages(mapped);
          if (d.source === 'jsonl') setResumeNote('会话缓存已过期，已从持久化记录完整恢复');
          return;
        }
      } catch { /* resume 也失败 → 按原逻辑置空 */ }
      console.error('Failed to load messages:', error);
      setMessages([]);
    }
  };

  // 末条用户消息的回复状态权威判定: 问后端任务运行时 (P1 账本)。
  // 失败/取消/未知 → 直接展示失败结论 (用户要求: 死任务不许再显示"正在恢复回复"误导);
  // 活跃 → 恢复轮询; 查询失败或无任务记录 (如 P1 之前的死消息) → 按消息时间兜底
  // (后端绝对上限 3×REPLY_TIMEOUT=18min, 超过 15min 视为死请求)。
  const resolvePendingReply = async (sessionId: string, mapped: any[]) => {
    let verdict: 'active' | 'failed' | 'unknown' = 'unknown';
    let errorText = '';
    try {
      const p = await getSessionPending(sessionId);
      if (p.state === 'active') {
        verdict = 'active';
      } else if (p.state === 'failed') {
        verdict = 'failed';
        errorText = p.error || '回复失败';
      } else if (p.state === 'succeeded') {
        // 任务已成功但回复不在会话历史 (落库丢失) → 同样给结论, 不悬挂
        verdict = 'failed';
        errorText = '回复已完成但结果未能保存到会话';
      }
    } catch { /* 后端不可达 → 走时间阈值兜底 */ }
    if (verdict === 'unknown') {
      const tsSec = Number(mapped[mapped.length - 1]?.timestamp) || 0;
      if (tsSec > 0 && Date.now() / 1000 - tsSec > 15 * 60) {
        verdict = 'failed';
        errorText = '该消息发送后未获得回复（当时服务不可用或已超时），消息未被处理';
      } else {
        verdict = 'active';
      }
    }
    if (verdict === 'failed') {
      setMessages(prev => [...prev.filter(m => !m.id?.toString().startsWith('resume-')), {
        id: `pending-fail-${sessionId}`,
        role: 'assistant',
        content: `⚠️ ${errorText}。如仍需要，请重新发送。`,
        isStreaming: false,
      }]);
    } else {
      resumePendingReply(sessionId);
    }
  };

  // 切页后恢复进行中的回复: 轮询会话历史 (每 3s)，回复落库后自动上屏
  const resumePendingReply = (sessionId: string) => {
    if (pendingPollRef.current) clearInterval(pendingPollRef.current);
    const thinkingId = `resume-${Date.now()}`;
    setMessages(prev => [...prev, {
      id: thinkingId,
      role: 'assistant',
      content: '',
      thinking: '',
      isStreaming: true,
      progress: '正在恢复回复（切页后回复仍在后台生成）...',
    }]);
    const started = Date.now();
    pendingPollRef.current = setInterval(async () => {
      try {
        const data = await getSessionMessages(sessionId);
        const filtered = (data || []).filter(m =>
          m.role === 'user' || (m.content && m.content.trim())
        );
        const mapped = filtered.map((m: any) => {
          const agentVal = m.agent || m.source || undefined;
          const agentsVal = m.agents || (agentVal ? [agentVal] : undefined);
          return { ...m, agent: agentVal, agents: agentsVal, kb_source: m.kb_source };
        });
        const last = mapped[mapped.length - 1];
        if (last && last.role === 'assistant') {
          // 回复已落库: 用完整历史替换占位消息
          if (pendingPollRef.current) {
            clearInterval(pendingPollRef.current);
            pendingPollRef.current = null;
          }
          // 仅当用户仍停留在此会话时才更新视图 (切换会话后由新会话加载负责)
          if (activeSessionRef.current === sessionId) {
            setMessages(mapped);
          }
        } else if (Date.now() - started > 10 * 60 * 1000) {
          // 10 分钟仍未落库 (后端可能已失败/超时): 移除占位停止轮询, 显式标注失败
          // (此前静默移除占位, 用户看不到任何失败提示)
          if (pendingPollRef.current) {
            clearInterval(pendingPollRef.current);
            pendingPollRef.current = null;
          }
          if (activeSessionRef.current === sessionId) {
            setMessages([...mapped, {
              id: `fail-${sessionId}`,
              role: 'assistant',
              content: '⚠️ 回复超时/失败（10 分钟内未收到结果）。请重新发送。',
              isStreaming: false,
            }]);
          }
        }
      } catch {
        // 网络异常忽略，继续轮询
      }
    }, 3000);
  };

  const handleCreateSession = async () => {
    try {
      const session = await createSession();
      setSessions([session, ...sessions]);
      setCurrentSessionId(session.id);
      setMessages([]);
    } catch (error) {
      console.error('Failed to create session:', error);
    }
  };

  const handleSelectSession = async (id: string) => {
    setCurrentSessionId(id);
    await loadSessionMessages(id);
    loadUsage(id);
  };

  const loadUsage = async (sid: string) => {
    try {
      const usage = await getUsage(sid);
      setSessionUsage(usage);
    } catch {
      setSessionUsage(null);
    }
  };

  const handleDeleteSession = async (id: string) => {
    try {
      await deleteSession(id);
      setSessions(sessions.filter(s => s.id !== id));
      if (currentSessionId === id) {
        setCurrentSessionId('');
        setMessages([]);
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
    }
  };

  const handleRenameSession = async (id: string, title: string) => {
    try {
      await renameSession(id, title);
      setSessions(sessions.map(s => s.id === id ? { ...s, title } : s));
    } catch (error) {
      console.error('Failed to rename session:', error);
    }
  };

  const handleSend = useCallback(async (content: string, targetAgent?: string) => {
    let sid = currentSessionId;
    if (!sid) {
      try {
        const session = await createSession();
        setSessions(prev => [session, ...prev]);
        sid = session.id;
        setCurrentSessionId(sid);
      } catch { return; }
    }

    const userMessage: ChatMessageType = {
      id: Date.now().toString(),
      role: 'user',
      content,
      timestamp: new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }),
      createdAt: Date.now(),
    };

    setMessages(prev => [...prev, userMessage]);
    setIsLoading(true);

    const thinkingId = (Date.now() + 1).toString();
    setMessages(prev => [...prev, {
      id: thinkingId,
      role: 'assistant',
      content: '',
      thinking: '',
      isStreaming: true,
      progress: '正在连接服务...',
    }]);

    const timeoutId = setTimeout(() => {
      setMessages(prev =>
        prev.map(msg =>
          msg.id === thinkingId && msg.isStreaming
            ? { ...msg, progress: '响应较慢，仍在处理中，请耐心等待...' }
            : msg
        )
      );
    }, 240000);

    try {
      let fullContent = '';
      let gotReply = false;
      let gotError = false;
      let gotDelta = false;
      let gotThinking = false;
      let streamedContent = '';
      let errorText = '';
      let chunkCount = 0;
      // P1 (task-c17): 任务 id + 终态标记 — 流中断时按游标补读, 不重新提交
      let taskId = '';
      let gotDone = false;
      let recovered = false;   // 断线恢复提示 (P1 可见性)
      setActiveTaskId('');

      for await (const chunk of sendMessageStream({
        message: content,
        session_id: sid,
        target_agent: targetAgent,
      })) {
        chunkCount++;
        if (chunk.type === 'start' && chunk.session_id && !currentSessionId) {
          setCurrentSessionId(chunk.session_id);
        }
        if (chunk.type === 'start' && chunk.task_id) {
          taskId = chunk.task_id;
          setActiveTaskId(chunk.task_id);
        }
        if (chunk.type === 'done') {
          gotDone = true;
        }
        if (chunk.type === 'progress' && !gotReply) {
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId && !msg.content
                ? { ...msg, progress: chunk.text || '处理中...' }
                : msg
            )
          );
          scrollToBottom();
        }
        if (chunk.type === 'delta' && chunk.text) {
          // 流式增量: LLM 边生成边实时渲染；rAF 节流避免逐 token 频繁 setState
          // kind=thinking → 思考链 (折叠展示) / 默认 → 正文
          gotDelta = true;
          if (chunk.kind === 'thinking') {
            if (!gotThinking) {
              gotThinking = true;
              // 首个思考分片立即同步上屏并让出宏任务，确保"正在深度思考…"中间态可见
              // (SSE 成批到达时 rAF 可能永不触发，否则会直接跳到"已深度思考"最终态)
              setMessages(prev =>
                prev.map(msg =>
                  msg.id === thinkingId
                    ? { ...msg, thinking: chunk.text, isStreaming: true }
                    : msg
                )
              );
              scrollToBottom();
              await new Promise(r => setTimeout(r, 0));
            } else {
              thinkBufRef.current += chunk.text;
            }
          } else {
            streamedContent += chunk.text;
            deltaBufRef.current += chunk.text;
          }
          if (rafIdRef.current === null) {
            rafIdRef.current = requestAnimationFrame(() => {
              rafIdRef.current = null;
              const buf = deltaBufRef.current;
              const tbuf = thinkBufRef.current;
              deltaBufRef.current = '';
              thinkBufRef.current = '';
              if (buf || tbuf) {
                setMessages(prev =>
                  prev.map(msg =>
                    msg.id === thinkingId
                      ? {
                          ...msg,
                          content: (msg.content || '') + buf,
                          thinking: (msg.thinking || '') + tbuf,
                          isStreaming: true,
                        }
                      : msg
                  )
                );
                scrollToBottom();
              }
            });
          }
        }
        if (chunk.type === 'reply' && chunk.text) {
          gotReply = true;
          fullContent = chunk.text;
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId
                ? {
                    ...msg,
                    agent: chunk.source,
                    agents: chunk.agents || (chunk.source ? [chunk.source] : undefined),
                    kb_source: chunk.kb_source,
                    data_source: chunk.data_source,
                    data_count: chunk.data_count,
                    usage: chunk.usage,
                    model: chunk.model,
                    file_url: chunk.file_url,
                    file_name: chunk.file_name,
                    skills: chunk.skills,
                    memory_used: chunk.memory_used,
                  }
                : msg
            )
          );
          scrollToBottom();
        } else if (chunk.type === 'error') {
          gotError = true;
          errorText = chunk.text || '发生错误，请重试';
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId
                ? { ...msg, content: errorText, isStreaming: false }
                : msg
            )
          );
          scrollToBottom();
        }
      }

      clearTimeout(timeoutId);

      // P1 (task-c17): 流中断但任务未终态 (切页/网络闪断) → 按任务事件补读直至终态,
      // 不重新 POST (幂等键兜底); 只补关键事件, delta 已由实时流渲染
      if (!gotReply && !gotError && !gotDone && taskId) {
        recovered = true;
        setRecoveryNote('网络中断，正在从任务断点恢复…');
        try {
          for await (const c of catchUpTaskEvents(taskId)) {
            if (c.type === 'reply' && c.text) {
              gotReply = true;
              const replyText: string = c.text;
              fullContent = replyText;
              setMessages(prev =>
                prev.map(msg =>
                  msg.id === thinkingId
                    ? { ...msg, agent: c.source, content: replyText, isStreaming: false }
                    : msg
                )
              );
              scrollToBottom();
            } else if (c.type === 'error') {
              gotError = true;
              errorText = c.text || '发生错误，请重试';
              setMessages(prev =>
                prev.map(msg =>
                  msg.id === thinkingId
                    ? { ...msg, content: errorText, isStreaming: false }
                    : msg
                )
              );
            }
          }
        } catch {
          // 补读失败保留已渲染内容, 走下方既有兜底逻辑
        } finally {
          if (recovered) setTimeout(() => setRecoveryNote(''), 4000);
        }
      }

      // 冲刷 rAF 节流缓冲区中残留的流式文本 (正文 + 思考链)
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      const deltaLeftover = deltaBufRef.current;
      deltaBufRef.current = '';
      const thinkLeftover = thinkBufRef.current;
      thinkBufRef.current = '';

      if (!gotReply || !fullContent.trim()) {
        if (gotDelta && streamedContent.trim()) {
          // 流式已显示部分内容但未收到最终 reply (如 agent 异常退出) →
          // 保留已实时显示的内容，仅标记生成结束，不覆盖为"内容为空"
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId
                ? { ...msg, content: (msg.content || '') + deltaLeftover, thinking: (msg.thinking || '') + thinkLeftover, isStreaming: false, timestamp: new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }
                : msg
            )
          );
          scrollToBottom();
        } else if (!gotError) {
          // 已收到过 error 事件(如"回复超时")时保留真实错误信息，不再覆盖为"内容为空"
          const fallbackText = !fullContent.trim()
            ? '回复内容为空，请重试'
            : '未收到回复，请检查问题后重试，或联系管理员';
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId
                ? { ...msg, content: fallbackText, isStreaming: false, timestamp: new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }
                : msg
            )
          );
        }
      } else if (gotDelta) {
        // 流式已实时显示 → 最终回复直接替换为完整文本 (含脱敏还原后的最终版本)
        const finalizeTimestamp = new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
        setMessages(prev =>
          prev.map(msg =>
            msg.id === thinkingId
              ? { ...msg, content: fullContent, thinking: (msg.thinking || '') + thinkLeftover, isStreaming: false, timestamp: finalizeTimestamp }
              : msg
          )
        );
        scrollToBottom();
      } else {
        const revealSpeed = Math.max(3, Math.min(15, Math.ceil(fullContent.length / 60)));
        let revealed = 0;
        const finalizeTimestamp = new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });

        const revealInterval = setInterval(() => {
          revealed = Math.min(revealed + revealSpeed, fullContent.length);
          setMessages(prev =>
            prev.map(msg =>
              msg.id === thinkingId
                ? { ...msg, content: fullContent.slice(0, revealed) }
                : msg
            )
          );
          scrollToBottom();

          if (revealed >= fullContent.length) {
            clearInterval(revealInterval);
            setMessages(prev =>
              prev.map(msg =>
                msg.id === thinkingId
                  ? { ...msg, content: fullContent, isStreaming: false, timestamp: finalizeTimestamp }
                  : msg
              )
            );
          }
        }, 25);
      }

      scrollToBottom();
      if (sid) loadUsage(sid);
      loadSessions();
    } catch (error) {
      clearTimeout(timeoutId);
      console.error('Chat error:', error);
      const errMsg = error instanceof Error ? error.message : '请求失败，请稍后重试';
      setMessages(prev =>
        prev.map(msg =>
          msg.id === thinkingId
            ? { ...msg, content: `抱歉，发生错误: ${errMsg}`, isStreaming: false }
            : msg
        )
      );
      scrollToBottom();
    } finally {
      setIsLoading(false);
      setActiveTaskId('');
    }
  }, [currentSessionId]);

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* Session List */}
      <SessionList
        sessions={sessions}
        currentSessionId={currentSessionId}
        onSelect={handleSelectSession}
        onCreate={handleCreateSession}
        onDelete={handleDeleteSession}
        onRename={handleRenameSession}
      />

      {/* Chat Area */}
      <div className="flex-1 flex flex-col overflow-hidden relative" style={{ background: 'var(--bg-primary)' }}>
        <div ref={messagesContainerRef} className="flex-1 overflow-y-auto">
          <div className="w-full px-4 sm:px-6 py-4 max-w-5xl mx-auto">
            {messages.length === 0 ? (
              /* ── Empty State — ChatGPT style ── */
              <div className="flex flex-col items-center justify-center min-h-[60vh]">
                <h2 className="text-[28px] font-semibold mb-3 tracking-tight" style={{ color: 'var(--text-primary)' }}>
                  有什么可以帮忙的？
                </h2>
                <p className="text-[15px] mb-8" style={{ color: 'var(--text-tertiary)' }}>
                  Z-OPS 智能运维助手，随时为你提供帮助
                </p>
                <div className="flex flex-wrap items-center justify-center gap-2 max-w-lg">
                  {[
                    { icon: '🔍', text: '查询虚拟机列表' },
                    { icon: '📊', text: '查看系统架构' },
                    { icon: '🔧', text: '运维故障排查' },
                    { icon: '📝', text: '查看运维日志' },
                  ].map(item => (
                    <button
                      key={item.text}
                      onClick={() => {
                        const textarea = document.querySelector('textarea') as HTMLTextAreaElement;
                        if (textarea) {
                          textarea.value = item.text;
                          textarea.focus();
                        }
                      }}
                      className="flex items-center gap-1.5 px-4 py-2 rounded-full text-[13px] transition-all bg-white"
                      style={{ color: 'var(--text-secondary)', border: '1px solid var(--border-medium)' }}
                      onMouseEnter={e => {
                        (e.currentTarget as HTMLElement).style.background = 'var(--bg-secondary)';
                        (e.currentTarget as HTMLElement).style.color = 'var(--text-primary)';
                      }}
                      onMouseLeave={e => {
                        (e.currentTarget as HTMLElement).style.background = '#fff';
                        (e.currentTarget as HTMLElement).style.color = 'var(--text-secondary)';
                      }}
                    >
                      <span>{item.icon}</span>
                      <span>{item.text}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <>
                {resumeNote && (
                  <div className="mb-4 px-3 py-2 rounded-lg text-xs bg-amber-50 text-amber-700 border border-amber-100">
                    {resumeNote}
                  </div>
                )}
                {messages.map(message => (
                  <ChatMessage key={message.id} message={message} username={displayName || username} />
                ))}
              </>
            )}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Scroll to bottom */}
        {showScrollButton && (
          <button
            onClick={handleScrollToBottom}
            className="absolute bottom-4 right-4 w-9 h-9 bg-white rounded-full flex items-center justify-center transition-all z-10"
            style={{ border: '1px solid var(--border-light)', boxShadow: 'var(--shadow-md)' }}
            title="滚动到底部"
          >
            <ArrowDown size={16} style={{ color: 'var(--text-secondary)' }} />
          </button>
        )}

        {/* Usage stats bar */}
        {sessionUsage && sessionUsage.total_calls > 0 && (
          <div className="px-4 py-1.5 flex items-center gap-4 text-[10px]"
               style={{ borderTop: '1px solid var(--border-light)', background: 'var(--bg-secondary)', color: 'var(--text-tertiary)' }}>
            <span className="inline-flex items-center gap-1">
              <Zap size={10} />
              {sessionUsage.total_tokens >= 1000
                ? `${(sessionUsage.total_tokens / 1000).toFixed(1)}k`
                : sessionUsage.total_tokens} tokens
            </span>
            <span className="inline-flex items-center gap-1">
              <ArrowDownUp size={10} />
              {sessionUsage.total_calls} 次调用
            </span>
            {sessionUsage.total_tool_calls > 0 && (
              <span>{sessionUsage.total_tool_calls} 工具调用</span>
            )}
            {sessionUsage.total_elapsed_seconds > 0 && (
              <span className="inline-flex items-center gap-1">
                <Clock size={10} />
                {sessionUsage.total_elapsed_seconds.toFixed(1)}s
              </span>
            )}
            {sessionUsage.compact_stats && (sessionUsage.compact_stats as any).compact_count > 0 && (
              <span className="ml-auto" style={{ color: 'var(--cf-orange)' }}>
                已压缩 {(sessionUsage.compact_stats as any).compact_count} 次
              </span>
            )}
          </div>
        )}

        {/* P1 可见性: 断线恢复提示 + 任务停止 */}
        {recoveryNote && (
          <div className="mx-4 mb-2 text-xs px-3 py-2 rounded-lg flex items-center gap-2"
               style={{ background: 'rgba(245,158,11,0.10)', color: '#B45309' }}>
            <span className="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: '#F59E0B' }} />
            {recoveryNote}
          </div>
        )}
        {isLoading && activeTaskId && (
          <div className="mx-4 mb-2 flex justify-end">
            <button
              onClick={async () => {
                try {
                  const token = localStorage.getItem('zops_token') || '';
                  await fetch(`/api/chat/tasks/${encodeURIComponent(activeTaskId)}/stop`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${token}` },
                  });
                  setRecoveryNote('已请求停止 (协作取消; 已发出的外部操作不会自动撤销)');
                  setTimeout(() => setRecoveryNote(''), 4000);
                } catch { /* 网络异常忽略 */ }
              }}
              className="text-xs px-3 py-1.5 rounded-lg border transition-colors hover:bg-red-50"
              style={{ borderColor: 'rgba(239,68,68,0.3)', color: '#DC2626' }}>
              ■ 停止生成
            </button>
          </div>
        )}
        <ChatInput onSend={handleSend} isLoading={isLoading} />
      </div>
    </div>
  );
}
