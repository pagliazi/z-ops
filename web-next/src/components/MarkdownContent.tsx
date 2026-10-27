'use client';

import { useMemo } from 'react';
import { marked } from 'marked';

// 配置 marked
marked.setOptions({
  breaks: true,
  gfm: true,
});

interface MarkdownContentProps {
  content: string;
  className?: string;
}

export function MarkdownContent({ content, className = '' }: MarkdownContentProps) {
  const html = useMemo(() => {
    if (!content) return '';
    
    try {
      // 提取 <think>...</think> 思考过程
      let thinkHtml = '';
      let mainText = content;
      const thinkRegex = /<think>([\s\S]*?)<\/think>/gi;
      const thinks: string[] = [];
      let match;
      
      while ((match = thinkRegex.exec(content)) !== null) {
        thinks.push(match[1].trim());
      }
      
      if (thinks.length > 0) {
        mainText = content.replace(thinkRegex, '').trim();
        const thinkContent = thinks.join('\n\n');
        thinkHtml = `<details class="think-block"><summary>💡 思考过程</summary><div class="think-content">${escapeHtml(thinkContent).replace(/\n/g, '<br>')}</div></details>`;
      }
      
      const bodyHtml = marked.parse(mainText) as string;
      return thinkHtml + bodyHtml;
    } catch (e) {
      console.error('[Z-OPS] markdown parse error:', e);
      return escapeHtml(content).replace(/\n/g, '<br>');
    }
  }, [content]);

  return (
    <div 
      className={`prose prose-sm max-w-none ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
