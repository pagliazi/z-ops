'use client';

import { useState, useEffect } from 'react';
import {
  BookOpen, Search, FileText, Tag, Clock, ExternalLink, BookMarked,
  FolderOpen, ChevronRight, ArrowLeft, Loader2, Database, RefreshCw, Layers,
} from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';
import { MarkdownContent } from '@/components/MarkdownContent';

const pad = (n: number) => n.toString().padStart(2, '0');

// 同步时间格式化：2026-08-11 04:00:09.643657 → 08-11 04:00
function fmtSync(s?: string): string {
  if (!s) return '';
  const d = new Date(s.replace(' ', 'T').split('.')[0]);
  if (isNaN(d.getTime())) return s;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 日期格式化：2024-12-27 / ISO → 2024-12-27
function fmtDate(s?: string): string {
  if (!s) return '';
  const d = new Date(s.replace(' ', 'T').split('.')[0]);
  if (isNaN(d.getTime())) return s.slice(0, 10);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// 搜索结果摘要可能携带 <b> 等高亮标签，展示前剥离
const stripHtml = (s: string) => (s || '').replace(/<[^>]+>/g, '');

// Outline 附件图无法外联，替换为占位卡片，避免满屏碎图图标
const sanitizeContent = (md: string) =>
  (md || '').replace(/!\[([^\]]*)\]\([^)]*\)/g, (_m, alt: string) => {
    const safe = String(alt).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<div class="kb-img-ph"><span>🖼️</span><span>附图：${safe || '未命名'}</span><em>附件不可外联 · 请在 KB 原文查看</em></div>`;
  });

type Coll = { id: string; name: string; description?: string; count?: number };
type Doc = {
  page_id: string; title: string; snippet?: string; updated_at?: string;
  collection?: string; url?: string; score?: number;
  breadcrumb?: string; match_type?: string;
  child_count?: number; empty_content?: boolean;
};
type TreeNode = { page_id: string; title: string; updated_at?: string; url?: string; children: TreeNode[] };
type ListMode =
  | { kind: 'collections' }
  | { kind: 'browse'; coll: Coll; tree: TreeNode[]; total: number }
  | { kind: 'search'; title: string; hits: Doc[] };

export default function KbPage() {
  const [stats, setStats] = useState<any>(null);
  const [collections, setCollections] = useState<Coll[]>([]);
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<ListMode>({ kind: 'collections' });
  const [listLoading, setListLoading] = useState(false);
  const [selectedPage, setSelectedPage] = useState<any>(null);
  const [selectedId, setSelectedId] = useState('');
  const [pageLoading, setPageLoading] = useState(false);

  const token = typeof window !== 'undefined' ? localStorage.getItem('zops_token') : null;
  const authHeaders = { Authorization: `Bearer ${token}` };

  useEffect(() => { fetchStats(); fetchCollections(); }, []);

  const fetchStats = async () => {
    try {
      const res = await fetch('/api/wiki/stats', { headers: authHeaders });
      if (res.ok) setStats(await res.json());
    } catch (e) { console.error('Failed to fetch wiki stats:', e); }
  };

  // 真实集合列表 + 每集合文档数（Outline 原生 API）
  const fetchCollections = async () => {
    try {
      const res = await fetch('/api/wiki/collections', { headers: authHeaders });
      if (res.ok) {
        const d = await res.json();
        setCollections(d.collections || []);
      }
    } catch (e) { console.error('Failed to fetch collections:', e); }
  };

  // 浏览集合：拉取完整文档树（父文档嵌套子文档）
  const openCollection = async (coll: Coll) => {
    setListLoading(true);
    try {
      const res = await fetch(`/api/wiki/collection/${coll.id}/docs`, { headers: authHeaders });
      if (res.ok) {
        const d = await res.json();
        setMode({ kind: 'browse', coll, tree: d.tree || [], total: d.total || 0 });
      }
    } catch (e) { console.error('Failed to load collection docs:', e); }
    setListLoading(false);
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setListLoading(true);
    try {
      const res = await fetch(`/api/wiki/search?q=${encodeURIComponent(q)}&top_k=50`, { headers: authHeaders });
      if (res.ok) {
        const d = await res.json();
        setMode({ kind: 'search', title: `搜索“${q}”`, hits: d.hits || [] });
      }
    } catch (e2) { console.error('Search failed:', e2); }
    setListLoading(false);
  };

  const fetchPage = async (pageId: string, collection?: string) => {
    setSelectedId(pageId);
    setPageLoading(true);
    try {
      const res = await fetch(`/api/wiki/page/${pageId}`, { headers: authHeaders });
      if (res.ok) {
        const page = await res.json();
        setSelectedPage({ ...page, collection: collection || page.page_type || '' });
      }
    } catch (e) { console.error('Failed to fetch page:', e); }
    setPageLoading(false);
  };

  const online = stats?.api_status === 'online';

  // 搜索结果按上级集合分组；标题与集合同名的视为目录条目
  const searchView = (() => {
    if (mode.kind !== 'search') return null;
    const dirs: Doc[] = [];
    const groups: { name: string; items: Doc[] }[] = [];
    for (const h of mode.hits) {
      const asDir = collections.find((c) => c.name === h.title);
      if (asDir) { dirs.push(h); continue; }
      const name = h.collection || '未分类';
      let g = groups.find((x) => x.name === name);
      if (!g) { g = { name, items: [] }; groups.push(g); }
      g.items.push(h);
    }
    return { dirs, groups };
  })();

  const listCount =
    mode.kind === 'collections' ? collections.length
    : mode.kind === 'browse' ? mode.total
    : mode.hits.length;

  return (
    <div className="h-full flex flex-col overflow-hidden bg-[#F9FAFB]">
      <PageHeader
        icon={BookOpen}
        title="知识库"
        subtitle={`企业知识库文档搜索与浏览 · 共 ${stats?.total_pages ?? '—'} 篇文档 · ${collections.length || stats?.total_collections || '—'} 个集合`}
        actions={
          <>
            {stats?.last_sync && (
              <span className="text-xs text-[#6B7280] bg-white border border-[#E5E7EB] rounded-md px-2.5 py-1">
                同步于 {fmtSync(stats.last_sync)}
              </span>
            )}
            <span className={`inline-flex items-center gap-1.5 text-xs font-medium rounded-md px-2.5 py-1 border ${
              online ? 'text-[#16A34A] bg-[#F0FDF4] border-[#BBF7D0]' : 'text-[#DC2626] bg-[#FEF2F2] border-[#FECACA]'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${online ? 'bg-[#16A34A]' : 'bg-[#DC2626]'}`} />
              API {online ? '在线' : '离线'}
            </span>
          </>
        }
      />

      <div className="flex-1 min-h-0 px-6 py-4">
        <div className="h-full grid grid-cols-12 gap-4">
          {/* ── 左栏：搜索 + 集合/文档浏览器 ── */}
          <aside className="col-span-3 h-full flex flex-col bg-white rounded-xl border border-[#E5E7EB] overflow-hidden">
            {/* 搜索区 */}
            <form onSubmit={handleSearch} className="p-3 border-b border-[#E5E7EB] flex-shrink-0">
              <div className="flex items-center gap-2 bg-[#F9FAFB] border border-[#E5E7EB] rounded-lg px-3 py-2 focus-within:border-[#2563EB] focus-within:ring-1 focus-within:ring-[#2563EB]/20 transition-all">
                <Search size={14} className="text-[#9CA3AF] flex-shrink-0" />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索知识库文档..."
                  className="flex-1 min-w-0 text-sm text-[#111827] focus:outline-none bg-transparent placeholder:text-[#9CA3AF]"
                />
                <button type="submit" className="text-xs text-[#2563EB] font-medium hover:text-[#1D4ED8] flex-shrink-0">
                  搜索
                </button>
              </div>
            </form>

            {/* 列表头 */}
            <div className="px-4 py-2.5 border-b border-[#E5E7EB] flex items-center gap-2 flex-shrink-0 bg-[#F9FAFB]/60">
              {mode.kind !== 'collections' ? (
                <>
                  <button onClick={() => setMode({ kind: 'collections' })}
                    className="flex items-center gap-1 text-xs text-[#6B7280] hover:text-[#111827] transition-colors">
                    <ArrowLeft size={12} />返回
                  </button>
                  <span className="w-px h-3.5 bg-[#E5E7EB]" />
                  <h2 className="text-sm font-semibold text-[#111827] truncate">{mode.kind === 'search' ? mode.title : mode.coll.name}</h2>
                </>
              ) : (
                <>
                  <FolderOpen size={14} className="text-[#6B7280]" />
                  <h2 className="text-sm font-semibold text-[#111827]">知识集合</h2>
                </>
              )}
              <span className="ml-auto text-xs text-[#9CA3AF] bg-[#F3F4F6] px-2 py-0.5 rounded">
                {listCount}
              </span>
            </div>

            {/* 列表内容 */}
            <div className="flex-1 overflow-y-auto">
              {listLoading ? (
                <div className="p-8 text-center">
                  <Loader2 size={20} className="animate-spin text-[#9CA3AF] mx-auto mb-2" />
                  <p className="text-xs text-[#9CA3AF]">加载文档中...</p>
                </div>
              ) : mode.kind === 'search' && searchView ? (
                <div className="divide-y divide-[#F3F4F6]">
                  {searchView.dirs.length > 0 && (
                    <div>
                      <div className="sticky top-0 z-10 px-4 py-1.5 bg-[#F9FAFB] border-b border-[#F3F4F6] text-[11px] font-medium text-[#6B7280]">目录</div>
                      {searchView.dirs.map((h, i) => {
                        const coll = collections.find((c) => c.name === h.title)!;
                        return (
                          <button key={`dir-${i}`} onClick={() => openCollection(coll)}
                            className="w-full flex items-center gap-2.5 px-4 py-2.5 hover:bg-[#F9FAFB] transition-colors text-left">
                            <FolderOpen size={14} className="text-[#2563EB] flex-shrink-0" />
                            <span className="text-[13px] font-medium text-[#111827] flex-1 truncate">{coll.name}</span>
                            <span className="text-[10px] text-[#9CA3AF]">{coll.count ?? '—'} 篇</span>
                            <ChevronRight size={13} className="text-[#9CA3AF]" />
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {searchView.groups.map((g) => (
                    <div key={g.name}>
                      <div className="sticky top-0 z-10 px-4 py-1.5 bg-[#F9FAFB] border-b border-[#F3F4F6] text-[11px] font-medium text-[#6B7280] flex items-center gap-1.5">
                        <FolderOpen size={11} className="text-[#9CA3AF]" />
                        {g.name}
                        <span className="text-[#D1D5DB]">·</span>
                        <span className="text-[#9CA3AF]">{g.items.length}</span>
                      </div>
                      <div className="divide-y divide-[#F3F4F6]">
                        {g.items.map((h, i) => (
                          <DocRow key={i} doc={h} active={selectedId === h.page_id} showSnippet
                            onClick={() => fetchPage(h.page_id, h.collection)} />
                        ))}
                      </div>
                    </div>
                  ))}
                  {mode.hits.length === 0 && (
                    <div className="p-8 text-center">
                      <Search size={20} className="text-[#D1D5DB] mx-auto mb-2" />
                      <p className="text-xs text-[#9CA3AF]">未找到相关文档</p>
                      <p className="text-[10px] text-[#D1D5DB] mt-1">试试工单号、GID、IP，或换个说法</p>
                    </div>
                  )}
                </div>
              ) : mode.kind === 'browse' ? (
                <div className="flex-1 overflow-y-auto py-1">
                  {mode.tree.length > 0 ? mode.tree.map((n) => (
                    <TreeRow key={n.page_id} node={n} depth={0} activeId={selectedId}
                      onOpen={(d) => fetchPage(d.page_id, mode.coll.name)} />
                  )) : (
                    <div className="p-8 text-center text-xs text-[#9CA3AF]">该集合暂无文档</div>
                  )}
                </div>
              ) : collections.length > 0 ? (
                <div className="divide-y divide-[#F3F4F6]">
                  {collections.map((c) => (
                    <button key={c.id} onClick={() => openCollection(c)}
                      className="w-full flex items-center gap-3 px-4 py-3 hover:bg-[#F9FAFB] transition-colors text-left">
                      <div className="w-7 h-7 rounded bg-[#EFF6FF] flex items-center justify-center flex-shrink-0">
                        <FolderOpen size={13} className="text-[#2563EB]" />
                      </div>
                      <span className="text-sm font-medium text-[#111827] flex-1 truncate">{c.name}</span>
                      <span className="text-[10px] text-[#9CA3AF] bg-[#F3F4F6] px-1.5 py-0.5 rounded">{c.count ?? '—'} 篇</span>
                      <ChevronRight size={14} className="text-[#9CA3AF]" />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="p-8 text-center">
                  <Loader2 size={20} className="animate-spin text-[#9CA3AF] mx-auto mb-2" />
                  <p className="text-xs text-[#9CA3AF]">加载集合中...</p>
                </div>
              )}
            </div>

            {mode.kind === 'collections' && collections.length > 0 && (
              <div className="px-4 py-2 border-t border-[#F3F4F6] text-[11px] text-[#9CA3AF] flex-shrink-0">点击集合浏览其下文档</div>
            )}
          </aside>

          {/* ── 右栏：文档查看器 / 概览仪表盘 ── */}
          <section className="col-span-9 h-full min-h-0 overflow-y-auto">
            {pageLoading ? (
              <div className="h-full flex flex-col items-center justify-center bg-white rounded-xl border border-[#E5E7EB]">
                <Loader2 size={24} className="animate-spin text-[#2563EB] mb-3" />
                <p className="text-sm text-[#6B7280]">加载文档中...</p>
              </div>
            ) : selectedPage ? (
              <div className="h-full flex flex-col bg-white rounded-xl border border-[#E5E7EB] overflow-hidden">
                <div className="px-5 py-3.5 border-b border-[#E5E7EB] flex-shrink-0">
                  <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded bg-[#EFF6FF] flex items-center justify-center flex-shrink-0">
                      <BookMarked size={16} className="text-[#2563EB]" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h2 className="text-base font-semibold text-[#111827] truncate">{selectedPage.title}</h2>
                      <div className="flex gap-3 mt-0.5 text-xs text-[#9CA3AF]">
                        {selectedPage.collection && (
                          <span className="flex items-center gap-1 text-[#2563EB]">
                            <FolderOpen size={10} />{selectedPage.collection}
                          </span>
                        )}
                        {selectedPage.metadata?.updated && (
                          <span className="flex items-center gap-1"><Clock size={10} />更新于 {fmtDate(selectedPage.metadata.updated)}</span>
                        )}
                      </div>
                    </div>
                    {selectedPage.url && (
                      <a href={selectedPage.url} target="_blank" rel="noopener noreferrer"
                        className="flex items-center gap-1.5 px-3.5 py-2 bg-[#2563EB] text-white text-xs font-medium rounded-lg hover:bg-[#1D4ED8] transition-colors flex-shrink-0 shadow-sm">
                        <ExternalLink size={13} />KB 原文
                      </a>
                    )}
                  </div>
                  {selectedPage.metadata?.tags && selectedPage.metadata.tags.length > 0 && (
                    <div className="flex gap-2 mt-2.5 flex-wrap">
                      {selectedPage.metadata.tags.map((tag: string, i: number) => (
                        <span key={i} className="px-2 py-0.5 bg-[#F3F4F6] text-[#6B7280] text-xs rounded border border-[#E5E7EB]">{tag}</span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex-1 overflow-y-auto px-6 py-5">
                  {(selectedPage.children?.length ?? 0) > 0 ? (
                    <>
                      {selectedPage.content ? (
                        <MarkdownContent content={sanitizeContent(selectedPage.content)} />
                      ) : (
                        <div className="mb-4 px-4 py-3 bg-[#FFFBEB] border border-[#FDE68A] rounded-lg text-xs text-[#92400E] flex items-center gap-2">
                          <FolderOpen size={13} className="flex-shrink-0" />
                          该文档为目录页（无正文），包含 {selectedPage.children.length} 篇子文档，点击下方展开浏览
                        </div>
                      )}
                      <div className={selectedPage.content ? 'mt-6 pt-5 border-t border-[#F3F4F6]' : ''}>
                        <div className="flex items-center gap-2 mb-2">
                          <FolderOpen size={14} className="text-[#7C3AED]" />
                          <h3 className="text-sm font-semibold text-[#111827]">子文档</h3>
                          <span className="text-xs text-[#9CA3AF]">{selectedPage.children.length}</span>
                        </div>
                        <div className="border border-[#F3F4F6] rounded-lg overflow-hidden py-1">
                          {selectedPage.children.map((n: TreeNode) => (
                            <TreeRow key={n.page_id} node={n} depth={0} activeId={selectedId}
                              onOpen={(d) => fetchPage(d.page_id, selectedPage.collection)} />
                          ))}
                        </div>
                      </div>
                    </>
                  ) : selectedPage.content ? (
                    <MarkdownContent content={sanitizeContent(selectedPage.content)} />
                  ) : (
                    <div className="py-16 text-center">
                      <FileText size={24} className="text-[#E5E7EB] mx-auto mb-3" />
                      <p className="text-sm text-[#9CA3AF]">该文档暂无正文内容</p>
                      {selectedPage.url && (
                        <a href={selectedPage.url} target="_blank" rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-[#2563EB] hover:underline mt-2">
                          在 KB 原文查看 <ExternalLink size={11} />
                        </a>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              /* 概览仪表盘：首屏不空白 */
              <div className="space-y-4">
                <div className="grid grid-cols-4 gap-4">
                  {[
                    { icon: FileText, label: '文档总数', value: stats?.total_pages ?? '—', tint: 'text-[#2563EB] bg-[#EFF6FF]' },
                    { icon: Layers, label: '知识集合', value: collections.length || stats?.total_collections || '—', tint: 'text-[#7C3AED] bg-[#F5F3FF]' },
                    { icon: RefreshCw, label: '最后同步', value: stats?.last_sync ? fmtSync(stats.last_sync) : '—', tint: 'text-[#0891B2] bg-[#ECFEFF]' },
                    { icon: Database, label: '数据源', value: online ? 'Outline 在线' : '离线', tint: online ? 'text-[#16A34A] bg-[#F0FDF4]' : 'text-[#DC2626] bg-[#FEF2F2]' },
                  ].map((t, i) => (
                    <div key={i} className="bg-white rounded-xl border border-[#E5E7EB] p-4 flex items-center gap-3">
                      <div className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${t.tint}`}>
                        <t.icon size={16} />
                      </div>
                      <div className="min-w-0">
                        <div className="text-lg font-semibold text-[#111827] leading-tight truncate">{t.value}</div>
                        <div className="text-xs text-[#9CA3AF] mt-0.5">{t.label}</div>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="bg-white rounded-xl border border-[#E5E7EB] p-4">
                  <div className="flex items-center gap-2 mb-3">
                    <FolderOpen size={14} className="text-[#6B7280]" />
                    <h3 className="text-sm font-semibold text-[#111827]">集合概览</h3>
                    <span className="text-xs text-[#9CA3AF]">点击卡片浏览集合内文档</span>
                  </div>
                  {collections.length > 0 ? (
                    <div className="grid grid-cols-3 gap-3">
                      {collections.map((c) => (
                        <button key={c.id} onClick={() => openCollection(c)}
                          className="group text-left border border-[#E5E7EB] rounded-lg p-3.5 hover:border-[#2563EB]/40 hover:bg-[#F9FAFB] transition-colors">
                          <div className="flex items-center gap-2.5">
                            <div className="w-8 h-8 rounded-lg bg-[#EFF6FF] flex items-center justify-center flex-shrink-0 group-hover:bg-[#DBEAFE] transition-colors">
                              <FolderOpen size={15} className="text-[#2563EB]" />
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="text-sm font-medium text-[#111827] truncate">{c.name}</div>
                              <div className="text-[11px] text-[#9CA3AF] mt-0.5">{c.count ?? '—'} 篇文档</div>
                            </div>
                            <ChevronRight size={14} className="text-[#D1D5DB] group-hover:text-[#2563EB] transition-colors" />
                          </div>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="py-8 text-center">
                      <Loader2 size={20} className="animate-spin text-[#9CA3AF] mx-auto mb-2" />
                      <p className="text-xs text-[#9CA3AF]">加载集合中...</p>
                    </div>
                  )}
                </div>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

// 目录树行：父文档可展开，子文档缩进渲染（真实目录结构）
function TreeRow({ node, depth, activeId, onOpen }: {
  node: TreeNode; depth: number; activeId: string; onOpen: (n: TreeNode) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasKids = node.children.length > 0;
  const active = activeId === node.page_id;
  return (
    <div>
      <div
        onClick={() => onOpen(node)}
        className={`kb-tree-row group flex items-center gap-1.5 pr-3 py-2 cursor-pointer transition-colors border-l-2 ${
          active ? 'bg-[#EFF6FF] border-l-[#2563EB]' : 'hover:bg-[#F9FAFB] border-l-transparent'
        }`}
        style={{ paddingLeft: 8 + depth * 18 }}>
        {hasKids ? (
          <span
            onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
            className="kb-tree-toggle w-4 h-4 flex items-center justify-center rounded hover:bg-[#E5E7EB] transition-colors flex-shrink-0">
            <ChevronRight size={12} className={`text-[#9CA3AF] transition-transform ${open ? 'rotate-90' : ''}`} />
          </span>
        ) : (
          <span className="w-4 flex-shrink-0" />
        )}
        {hasKids
          ? <FolderOpen size={13} className={open ? 'text-[#2563EB]' : 'text-[#60A5FA]'} />
          : <FileText size={13} className="text-[#2563EB]" />}
        <span className={`text-[13px] truncate flex-1 ${active ? 'text-[#1D4ED8] font-medium' : 'text-[#111827]'}`}>
          {node.title}
        </span>
        {hasKids && <span className="text-[10px] text-[#9CA3AF] flex-shrink-0">{node.children.length}</span>}
        <span className="text-[10px] text-[#9CA3AF] flex-shrink-0">{node.updated_at}</span>
      </div>
      {open && node.children.map((c) => (
        <TreeRow key={c.page_id} node={c} depth={depth + 1} activeId={activeId} onOpen={onOpen} />
      ))}
    </div>
  );
}

// 匹配类型徽标（KB API hybrid 返回 match_type）：精确(标题含原文) > 内容(正文含原文) > 语义
const MATCH_BADGES: Record<string, { label: string; cls: string }> = {
  title: { label: '精确匹配', cls: 'text-[#16A34A] bg-[#F0FDF4] border-[#BBF7D0]' },
  text: { label: '内容匹配', cls: 'text-[#2563EB] bg-[#EFF6FF] border-[#DBEAFE]' },
  semantic: { label: '语义相关', cls: 'text-[#6B7280] bg-[#F3F4F6] border-[#E5E7EB]' },
};

// breadcrumb 去掉末段（文档自身标题）后作为路径标签: "GCOC / 故障排除… / O365备份报告导出" → "GCOC › 故障排除…"
const pathLabel = (bc?: string) => {
  if (!bc) return '';
  const segs = bc.split('/').map((s) => s.trim()).filter(Boolean);
  return segs.length > 1 ? segs.slice(0, -1).join(' › ') : '';
};

// 文档行：搜索结果用（匹配徽标 + 相关度 + 目录路径标签优先展示）
function DocRow({ doc, active, showSnippet, onClick }: {
  doc: Doc; active: boolean; showSnippet?: boolean; onClick: () => void;
}) {
  const badge = doc.match_type ? MATCH_BADGES[doc.match_type] : null;
  const showScore = doc.match_type === 'title' || doc.match_type === 'text';
  const bc = pathLabel(doc.breadcrumb);
  const isExact = doc.match_type === 'title';
  const isDir = (doc.child_count ?? 0) > 0;
  const isEmpty = !!doc.empty_content;
  return (
    <button onClick={onClick} title={bc ? `目录：${bc}` : undefined}
      className={`w-full text-left p-3.5 transition-colors border-l-2 group ${
        active ? 'bg-[#EFF6FF] border-l-[#2563EB]' : 'hover:bg-[#F8FAFC] border-l-transparent'
      }`}>
      <div className="flex items-start gap-2.5">
        <div className={`w-6 h-6 rounded flex items-center justify-center flex-shrink-0 mt-0.5 transition-colors ${
          isDir ? 'bg-[#F5F3FF] group-hover:bg-[#EDE9FE]' : isExact ? 'bg-[#F0FDF4]' : 'bg-[#EFF6FF] group-hover:bg-[#DBEAFE]'
        }`}>
          {isDir
            ? <FolderOpen size={12} className="text-[#7C3AED]" />
            : <FileText size={12} className={isExact ? 'text-[#16A34A]' : 'text-[#2563EB]'} />}
        </div>
        <div className="min-w-0 flex-1">
          <div className={`text-[13px] font-medium truncate transition-colors ${
            active ? 'text-[#1D4ED8]' : 'text-[#111827] group-hover:text-[#1D4ED8]'
          }`}>
            {doc.title || '（无标题文档）'}
          </div>
          {/* 相关度优先行：匹配徽标 + 分数 + 目录徽标 + KB 目录路径小标签 */}
          <div className="flex items-center gap-1.5 mt-1 min-w-0">
            {badge && (
              <span className={`inline-flex items-center flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded border ${badge.cls}`}>
                {badge.label}
              </span>
            )}
            {showScore && doc.score !== undefined && (
              <span className={`text-[10px] font-semibold flex-shrink-0 ${
                isExact ? 'text-[#16A34A]' : 'text-[#2563EB]'
              }`}>{(doc.score * 100).toFixed(0)}%</span>
            )}
            {isDir && (
              <span className="inline-flex items-center flex-shrink-0 text-[10px] font-medium text-[#7C3AED] bg-[#F5F3FF] border border-[#DDD6FE] px-1.5 py-0.5 rounded">
                目录 · {doc.child_count}
              </span>
            )}
            {bc && (
              <span className="flex items-center gap-1 min-w-0 flex-1 text-[10px] text-[#9CA3AF] bg-[#F9FAFB] border border-[#F3F4F6] rounded px-1.5 py-0.5">
                <FolderOpen size={9} className="flex-shrink-0 text-[#C4C9D2]" />
                <span className="truncate">{bc}</span>
              </span>
            )}
          </div>
          {showSnippet && (
            isEmpty ? (
              <div className="text-xs text-[#B9BEC9] mt-1.5 italic">
                {isDir ? `目录页 · 含 ${doc.child_count} 篇子文档，点击查看` : '该文档暂无正文内容'}
              </div>
            ) : doc.snippet ? (
              <div className="text-xs text-[#6B7280] mt-1.5 line-clamp-2 leading-relaxed">{stripHtml(doc.snippet)}</div>
            ) : null
          )}
          {doc.updated_at && <div className="text-[10px] text-[#9CA3AF] mt-1">{fmtDate(doc.updated_at)}</div>}
        </div>
      </div>
    </button>
  );
}
