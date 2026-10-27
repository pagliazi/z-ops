'use client';

import React from 'react';

// 简易 markdown -> React 节点 (段落/粗体/表格/列表/引用/分隔线)
// 与 nms/page.tsx 中的实现一致，抽取为共享组件供日志页等复用
export function renderSimpleMarkdown(text: string) {
  const lines = String(text || '').split('\n');
  const nodes: React.ReactNode[] = [];
  let i = 0;
  const inline = (s: string): React.ReactNode => {
    const parts: React.ReactNode[] = [];
    // 组合匹配：**粗体** / `代码` / [文本](链接) / 裸 URL
    const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s)]+)/g;
    let last = 0, m;
    const linkCls = "text-[#7C3AED] underline decoration-dotted underline-offset-2 hover:text-[#6D28D9] break-all";
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parts.push(s.slice(last, m.index));
      if (m[1] !== undefined) {
        parts.push(<strong key={`b${m.index}`}>{m[1]}</strong>);
      } else if (m[2] !== undefined) {
        parts.push(<code key={`c${m.index}`} className="px-1 py-0.5 rounded bg-[#F1F5F9] text-[#DB2777] font-mono text-[12px]">{m[2]}</code>);
      } else if (m[3] !== undefined) {
        parts.push(<a key={`l${m.index}`} href={m[4]} target="_blank" rel="noopener noreferrer" className={linkCls}>{m[3]}</a>);
      } else if (m[5] !== undefined) {
        parts.push(<a key={`u${m.index}`} href={m[5]} target="_blank" rel="noopener noreferrer" className={linkCls}>{m[5]}</a>);
      }
      last = m.index + m[0].length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts.length ? parts : s;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^---+$/.test(line.trim())) {
      nodes.push(<hr key={i} className="my-4 border-[#E5E7EB]" />);
      i++; continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const lvl = h[1].length, txt = h[2];
      if (lvl === 1) nodes.push(<h1 key={i} className="text-base font-bold text-[#0F172A] mt-5 mb-2">{inline(txt)}</h1>);
      else if (lvl === 2) nodes.push(<h2 key={i} className="text-sm font-bold text-[#0F172A] mt-4 mb-2 pb-1 border-b border-[#E5E7EB]">{inline(txt)}</h2>);
      else if (lvl === 3) nodes.push(<h3 key={i} className="text-sm font-semibold text-[#334155] mt-3 mb-1.5">{inline(txt)}</h3>);
      else nodes.push(<h4 key={i} className="text-xs font-semibold text-[#475569] mt-2 mb-1">{inline(txt)}</h4>);
      i++; continue;
    }
    if (/^\s*\|.+\|\s*$/.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        if (!/^\s*\|[\s\-|:]+\|\s*$/.test(lines[i]))
          rows.push(lines[i].split('|').slice(1, -1).map(c => c.trim()));
        i++;
      }
      if (rows.length) {
        const head = rows[0], body = rows.slice(1);
        nodes.push(
          <div key={`t${i}`} className="my-3 overflow-x-auto border border-[#E5E7EB] rounded-lg">
            <table className="min-w-full text-xs">
              <thead className="bg-[#F8FAFC]">
                <tr>
                  {head.map((c, j) => (<th key={j} className="px-3 py-2 text-left font-semibold text-[#334155]">{inline(c)}</th>))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F1F5F9]">
                {body.map((r, ri) => (
                  <tr key={ri} className="hover:bg-[#F8FAFC]">
                    {r.map((c, j) => (<td key={j} className="px-3 py-2 text-[#475569]">{inline(c)}</td>))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*[-*]\s+/, ''))}</li>);
        i++;
      }
      nodes.push(<ul key={`ul${i}`} className="list-disc pl-5 my-2 space-y-1 text-[#334155]">{items}</ul>);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*\d+\.\s+/, ''))}</li>);
        i++;
      }
      nodes.push(<ol key={`ol${i}`} className="list-decimal pl-5 my-2 space-y-1 text-[#334155]">{items}</ol>);
      continue;
    }
    if (/^\s*>\s*/.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && /^\s*>\s*/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s*/, ''));
        i++;
      }
      nodes.push(
        <div key={`q${i}`} className="my-3 border-l-2 border-[#94A3B8] bg-[#F8FAFC] px-3 py-2 text-[#475569] text-sm rounded-r">
          {inline(quoted.join(' '))}
        </div>
      );
      continue;
    }
    if (!line.trim()) { i++; continue; }
    nodes.push(<p key={i} className="my-1.5 leading-relaxed">{inline(line)}</p>);
    i++;
  }
  return nodes;
}
