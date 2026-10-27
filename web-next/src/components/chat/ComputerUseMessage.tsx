'use client';

import { useMemo } from 'react';
import { Marked } from 'marked';

const escape = (text: string) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// CU 正文不能绕过任务归属校验加载旧图片、原始 HTML 或下载链接。
const parser = new Marked({
  gfm: true, breaks: true,
  renderer: {
    html: ({ text }) => escape(text),
    image: () => '〔任务画面仅在绑定任务小窗中临时查看〕',
    link: ({ text }) => escape(text),
  },
});

export function computerUseHtml(content: string): string {
  return parser.parse(content, { async: false });
}

export function ComputerUseMessage({ content }: { content: string }) {
  const html = useMemo(() => computerUseHtml(content), [content]);
  return <div className="prose prose-sm max-w-none" dangerouslySetInnerHTML={{ __html: html }} />;
}
