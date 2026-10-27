'use client';

import { useState, useEffect } from 'react';
import { Sparkles, Search, FileText, Tag, Clock, ExternalLink, BookMarked, Layers, AlertTriangle, ChevronDown, ChevronRight, Activity, HeartPulse } from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';
import { MarkdownContent } from '@/components/MarkdownContent';

const TYPE_ORDER = ['entities', 'concepts', 'summaries', 'synthesis', 'queries'] as const;

const TYPE_LABELS: Record<string, string> = {
  entities: '实体',
  concepts: '概念',
  summaries: '会话摘要',
  synthesis: '综合分析',
  queries: '查询',
};

const TYPE_COLORS: Record<string, string> = {
  entities: '#2563EB',
  concepts: '#7C3AED',
  summaries: '#0D9488',
  synthesis: '#D97706',
  queries: '#DC2626',
};

const TYPE_ICONS: Record<string, any> = {
  entities: BookMarked,
  concepts: Sparkles,
  summaries: FileText,
  synthesis: Layers,
  queries: Search,
};

const LINT_LABELS: Record<string, string> = {
  orphan: '孤立页面（未被引用）',
  broken_link: '断链（指向不存在页面）',
  stale: '长期未更新',
  duplicate: '疑似重复',
  missing_fm: '缺少元数据',
  empty_sources: '缺少来源',
};

function fmtDate(iso: string) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString('zh-CN');
}

function fmtDateTime(iso: string) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN');
}

export default function LlmWikiPage() {
  const [stats, setStats] = useState<any>(null);
  const [pages, setPages] = useState<any>(null);
  const [expandedTypes, setExpandedTypes] = useState<Set<string>>(new Set(['concepts', 'entities']));
  const [selectedPage, setSelectedPage] = useState<any>(null);
  const [selectedId, setSelectedId] = useState<string>('');
  const [pageLoading, setPageLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [showLint, setShowLint] = useState(false);

  const token = typeof window !== 'undefined' ? localStorage.getItem('zops_token') : null;

  useEffect(() => {
    fetchStats();
    fetchPages();
  }, []);

  const fetchStats = async () => {
    try {
      const res = await fetch('/api/llm-wiki/stats', { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setStats(await res.json());
    } catch (e) { console.error('Failed to fetch llm-wiki stats:', e); }
  };

  const fetchPages = async () => {
    try {
      const res = await fetch('/api/llm-wiki/pages', { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setPages(await res.json());
    } catch (e) { console.error('Failed to fetch llm-wiki pages:', e); }
  };

  const fetchPage = async (pageId: string, pageType: string) => {
    setPageLoading(true);
    setSelectedId(pageId);
    try {
      const res = await fetch(`/api/llm-wiki/page/${pageId}?page_type=${encodeURIComponent(pageType || '')}`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setSelectedPage(await res.json());
    } catch (e) { console.error('Failed to fetch page:', e); }
    setPageLoading(false);
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) { setSearchResults([]); return; }
    setSearching(true);
    try {
      const res = await fetch(`/api/llm-wiki/search?q=${encodeURIComponent(searchQuery)}&top_k=20`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) { const data = await res.json(); setSearchResults(data.hits || []); }
    } catch (e) { console.error('Search failed:', e); }
    setSearching(false);
  };

  const toggleType = (type: string) => {
    const next = new Set(expandedTypes);
    if (next.has(type)) next.delete(type); else next.add(type);
    setExpandedTypes(next);
  };

  const groups = pages?.groups || {};
  const total = pages?.total || 0;
  const byType = stats?.by_type || {};
  const lintByCat = stats?.lint_by_category || {};
  const lintTotal = Object.values(lintByCat).reduce((a: number, b: any) => a + b, 0) as number;

  return (
    <div className="h-full flex flex-col overflow-hidden bg-[#F9FAFB]">
      <PageHeader
        icon={Sparkles}
        title="LLM-Wiki"
        subtitle={`LLM 自动编译的知识库 — 共 ${total} 个页面`}
        accent="#7C3AED"
        iconBg="rgba(124,58,237,0.1)"
      />
      <div className="flex-1 overflow-y-auto">
        <div className="px-6 py-5">
          {/* Stats 卡片 */}
          <div className="grid grid-cols-4 gap-4 mb-6">
            <div className="bg-white rounded-xl border border-[#E5E7EB] p-5 shadow-sm">
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm text-[#6B7280]">知识页面</span>
                <FileText size={20} className="text-[#7C3AED]" />
              </div>
              <div className="text-2xl font-bold text-[#111827]">{stats?.total_pages ?? total}</div>
              {/* 类型分布条 */}
              {total > 0 && (
                <div className="mt-3 flex h-1.5 rounded-full overflow-hidden bg-[#F3F4F6]">
                  {TYPE_ORDER.map(t => (byType[t] || 0) > 0 && (
                    <div key={t} style={{ width: `${((byType[t] / total) * 100)}%`, background: TYPE_COLORS[t] }} />
                  ))}
                </div>
              )}
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5">
                {TYPE_ORDER.filter(t => (byType[t] || 0) > 0).map(t => (
                  <span key={t} className="flex items-center gap-1 text-[10px] text-[#9CA3AF]">
                    <span className="w-1.5 h-1.5 rounded-full" style={{ background: TYPE_COLORS[t] }} />
                    {TYPE_LABELS[t]} {byType[t]}
                  </span>
                ))}
              </div>
            </div>

            <div className="bg-white rounded-xl border border-[#E5E7EB] p-5 shadow-sm">
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm text-[#6B7280]">最近编译</span>
                <Clock size={20} className="text-[#2563EB]" />
              </div>
              <div className="text-2xl font-bold text-[#111827]">{fmtDate(stats?.last_updated) || '—'}</div>
              <div className="text-[10px] text-[#9CA3AF] mt-2">{stats?.last_updated ? fmtDateTime(stats.last_updated) : '暂无编译记录'}</div>
            </div>

            <div className="bg-white rounded-xl border border-[#E5E7EB] p-5 shadow-sm">
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm text-[#6B7280]">知识类型</span>
                <Layers size={20} className="text-[#0D9488]" />
              </div>
              <div className="text-2xl font-bold text-[#111827]">{Object.values(byType).filter(Boolean).length || Object.keys(groups).filter(k => (groups[k] || []).length > 0).length}</div>
              <div className="text-[10px] text-[#9CA3AF] mt-2">实体 / 概念 / 摘要 / 综合 / 查询</div>
            </div>

            <div className="bg-white rounded-xl border border-[#E5E7EB] p-5 shadow-sm">
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm text-[#6B7280]">健康问题</span>
                <HeartPulse size={20} className={lintTotal > 0 ? 'text-[#D97706]' : 'text-[#16A34A]'} />
              </div>
              <div className={`text-2xl font-bold ${lintTotal > 0 ? 'text-[#D97706]' : 'text-[#16A34A]'}`}>{lintTotal}</div>
              {lintTotal > 0 ? (
                <button onClick={() => setShowLint(v => !v)} className="text-[10px] text-[#7C3AED] hover:underline mt-2 flex items-center gap-0.5">
                  <Activity size={10} /> 查看报告
                </button>
              ) : (
                <div className="text-[10px] text-[#16A34A] mt-2">全部健康</div>
              )}
            </div>
          </div>

          {/* Lint 健康报告 */}
          {showLint && lintTotal > 0 && (
            <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm mb-6 overflow-hidden">
              <div className="px-4 py-3 border-b border-[#E5E7EB] flex items-center gap-2">
                <AlertTriangle size={14} className="text-[#D97706]" />
                <h2 className="text-sm font-semibold text-[#111827]">健康报告</h2>
                <span className="text-xs text-[#9CA3AF]">由 wiki_lint 自动生成</span>
                <button onClick={() => setShowLint(false)} className="ml-auto text-xs text-[#9CA3AF] hover:text-[#6B7280]">收起</button>
              </div>
              <div className="p-4 grid grid-cols-2 gap-4">
                {Object.entries(lintByCat).map(([cat, items]: [string, any]) => (
                  <div key={cat} className="rounded-lg border border-[#F3F4F6] bg-[#F9FAFB] p-3">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-medium text-[#374151]">{LINT_LABELS[cat] || cat}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#FEF3C7] text-[#92400E]">{items}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="px-4 pb-4">
                <div className="text-[10px] text-[#9CA3AF]">
                  提示：孤立/过期多为 Qoder 记忆批量导入所致，属正常现象；断链与重复页面建议后续 ingest 时合并。
                </div>
              </div>
            </div>
          )}

          {/* 搜索 */}
          <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm p-5 mb-6">
            <form onSubmit={handleSearch} className="flex gap-3">
              <div className="flex-1 flex items-center gap-3 bg-[#F9FAFB] border border-[#E5E7EB] rounded-full px-5 py-2.5 focus-within:border-[#7C3AED] focus-within:ring-1 focus-within:ring-[#7C3AED]/20 transition-all">
                <Search size={18} className="text-[#9CA3AF]" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="搜索知识，如 OneAPI、LDAP、VM 备份..."
                  className="flex-1 text-sm text-[#111827] focus:outline-none bg-transparent placeholder:text-[#9CA3AF]"
                />
              </div>
              <button type="submit" disabled={searching}
                className="px-6 py-2.5 bg-[#7C3AED] text-white rounded-full text-sm font-medium hover:bg-[#6D28D9] disabled:opacity-50 transition-colors">
                {searching ? '搜索中...' : '搜索'}
              </button>
            </form>
          </div>

          {/* 搜索结果 */}
          {searchResults.length > 0 && (
            <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm mb-6 overflow-hidden">
              <div className="px-4 py-3 border-b border-[#E5E7EB] flex items-center gap-2">
                <Search size={14} className="text-[#6B7280]" />
                <h2 className="text-sm font-semibold text-[#111827]">搜索结果</h2>
                <span className="ml-auto text-xs text-[#9CA3AF] bg-[#F3F4F6] px-2 py-0.5 rounded">{searchResults.length}</span>
              </div>
              <div className="divide-y divide-[#F3F4F6] max-h-[400px] overflow-y-auto">
                {searchResults.map((hit, i) => (
                  <button key={i} onClick={() => fetchPage(hit.page_id, hit.page_type)}
                    className="w-full text-left p-4 hover:bg-[#F9FAFB] transition-colors">
                    <div className="flex items-start gap-3">
                      <div className="w-8 h-8 rounded flex items-center justify-center flex-shrink-0 mt-0.5"
                        style={{ background: `${TYPE_COLORS[hit.page_type] || '#6B7280'}15` }}>
                        <FileText size={14} style={{ color: TYPE_COLORS[hit.page_type] || '#6B7280' }} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium text-[#111827] truncate">{hit.title}</div>
                        <div className="flex items-center gap-2 mt-1">
                          <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: `${TYPE_COLORS[hit.page_type] || '#6B7280'}15`, color: TYPE_COLORS[hit.page_type] || '#6B7280' }}>
                            {TYPE_LABELS[hit.page_type] || hit.page_type}
                          </span>
                          {hit.score > 0 && <span className="text-[10px] text-[#9CA3AF]">相关度 {hit.score.toFixed(2)}</span>}
                        </div>
                        {hit.snippet && (
                          <div className="text-xs text-[#6B7280] mt-1.5 line-clamp-2 leading-relaxed">{hit.snippet}</div>
                        )}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* 内容区：分组列表 + 详情 */}
          <div className="grid grid-cols-12 gap-4">
            {/* 左：分组列表 */}
            <div className="col-span-5 xl:col-span-4 space-y-3">
              {TYPE_ORDER.map(type => {
                const items = groups[type] || [];
                if (items.length === 0) return null;
                const expanded = expandedTypes.has(type);
                const Icon = TYPE_ICONS[type] || FileText;
                return (
                  <div key={type} className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm overflow-hidden">
                    <button onClick={() => toggleType(type)}
                      className="w-full flex items-center gap-2 px-4 py-3 hover:bg-[#F9FAFB] transition-colors">
                      {expanded ? <ChevronDown size={16} className="text-[#6B7280]" /> : <ChevronRight size={16} className="text-[#6B7280]" />}
                      <div className="w-6 h-6 rounded flex items-center justify-center" style={{ background: `${TYPE_COLORS[type]}15` }}>
                        <Icon size={13} style={{ color: TYPE_COLORS[type] }} />
                      </div>
                      <span className="text-sm font-semibold text-[#111827] flex-1 text-left">{TYPE_LABELS[type] || type}</span>
                      <span className="text-xs text-[#9CA3AF] bg-[#F3F4F6] px-2 py-0.5 rounded">{items.length}</span>
                    </button>
                    {expanded && (
                      <div className="divide-y divide-[#F3F4F6] max-h-[420px] overflow-y-auto">
                        {items.map((p: any) => (
                          <button key={p.page_id} onClick={() => fetchPage(p.page_id, p.page_type)}
                            className={`w-full text-left px-4 py-2.5 hover:bg-[#F9FAFB] transition-colors ${selectedId === p.page_id ? 'bg-[#F5F3FF]' : ''}`}>
                            <div className="text-sm text-[#111827] truncate">{p.title}</div>
                            <div className="flex items-center gap-2 mt-0.5">
                              {p.updated && <span className="text-[10px] text-[#9CA3AF]">{fmtDate(p.updated)}</span>}
                              {p.sources_count > 0 && <span className="text-[10px] text-[#9CA3AF]">{p.sources_count} 来源</span>}
                              {(p.tags || []).slice(0, 2).map((t: string, i: number) => (
                                <span key={i} className="text-[10px] text-[#7C3AED] bg-[#F5F3FF] px-1 rounded">{t}</span>
                              ))}
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}

              {total === 0 && (
                <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm p-8 text-center">
                  <div className="w-16 h-16 rounded-lg bg-[#F3F4F6] flex items-center justify-center mx-auto mb-3">
                    <Sparkles size={28} className="text-[#9CA3AF]" />
                  </div>
                  <p className="text-sm font-medium text-[#374151]">暂无知识页面</p>
                  <p className="text-xs text-[#9CA3AF] mt-1">会话结束后 LLM 将自动编译知识页面</p>
                </div>
              )}
            </div>

            {/* 右：详情 */}
            <div className="col-span-7 xl:col-span-8">
              {pageLoading ? (
                <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm p-12 text-center">
                  <div className="w-6 h-6 border-2 border-[#7C3AED] border-t-transparent rounded-full animate-spin mx-auto mb-3" />
                  <p className="text-sm text-[#6B7280]">加载中...</p>
                </div>
              ) : selectedPage ? (
                <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm p-5">
                  <div className="flex items-center gap-3 mb-4">
                    <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
                      style={{ background: `${TYPE_COLORS[selectedPage.page_type] || '#6B7280'}15` }}>
                      <BookMarked size={16} style={{ color: TYPE_COLORS[selectedPage.page_type] || '#6B7280' }} />
                    </div>
                    <div className="min-w-0">
                      <h2 className="text-base font-semibold text-[#111827] leading-snug">{selectedPage.title}</h2>
                      <div className="flex gap-3 mt-1 text-[11px] text-[#9CA3AF]">
                        <span className="px-1.5 py-0.5 rounded" style={{ background: `${TYPE_COLORS[selectedPage.page_type] || '#6B7280'}15`, color: TYPE_COLORS[selectedPage.page_type] || '#6B7280' }}>
                          {TYPE_LABELS[selectedPage.page_type] || selectedPage.page_type}
                        </span>
                        {selectedPage.metadata?.updated && (
                          <span className="flex items-center gap-1"><Clock size={10} />更新于 {fmtDateTime(selectedPage.metadata.updated)}</span>
                        )}
                        <span className="flex items-center gap-1 truncate"><Tag size={10} />{selectedPage.page_id}</span>
                      </div>
                    </div>
                  </div>

                  {selectedPage.metadata?.tags?.length > 0 && (
                    <div className="flex gap-2 mb-4 flex-wrap">
                      {selectedPage.metadata.tags.map((tag: string, i: number) => (
                        <span key={i} className="px-2 py-0.5 bg-[#F5F3FF] text-[#7C3AED] text-xs rounded border border-[#EDE9FE]">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="bg-[#F9FAFB] p-4 rounded-lg border border-[#E5E7EB] max-h-[640px] overflow-y-auto">
                    <MarkdownContent content={selectedPage.content} className="text-[#374151]" />
                  </div>

                  {selectedPage.metadata?.sources?.length > 0 && (
                    <div className="mt-4 pt-4 border-t border-[#E5E7EB]">
                      <h3 className="text-sm font-medium text-[#374151] mb-2 flex items-center gap-1.5"><ExternalLink size={14} /> 来源</h3>
                      <ul className="text-xs text-[#6B7280] space-y-1">
                        {selectedPage.metadata.sources.map((src: string, i: number) => (
                          <li key={i} className="flex items-center gap-2"><span className="w-1 h-1 rounded-full bg-[#D1D5DB]" />{src}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : (
                <div className="bg-white rounded-xl border border-[#E5E7EB] shadow-sm p-12 text-center">
                  <div className="w-16 h-16 rounded-lg bg-[#F3F4F6] flex items-center justify-center mx-auto mb-3">
                    <Sparkles size={28} className="text-[#D1D5DB]" />
                  </div>
                  <p className="text-sm font-medium text-[#6B7280]">选择一个知识页面查看详情</p>
                  <p className="text-xs text-[#9CA3AF] mt-1">从左侧分组中浏览，或搜索知识</p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
