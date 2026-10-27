'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { ChevronRight, Home } from 'lucide-react';
import { buildBreadcrumb, type Crumb } from '@/lib/routes';

interface PageHeaderProps {
  /** 页面主图标 */
  icon: React.ElementType;
  /** 页面标题（同时用于面包屑末级） */
  title: string;
  /** 副标题，支持文本或自定义节点（如状态点、计数） */
  subtitle?: React.ReactNode;
  /** 图标颜色，默认品牌色 */
  accent?: string;
  /** 图标底色，默认品牌浅色 */
  iconBg?: string;
  /** 右侧操作区 */
  actions?: React.ReactNode;
  /** 覆盖自动生成的面包屑 */
  breadcrumb?: Crumb[];
}

/**
 * 统一的页面头部：面包屑 + 图标 + 标题 + 副标题 + 操作区。
 * 所有板块共用，保证宽度、标题层级、间距一致。
 */
export function PageHeader({
  icon: Icon,
  title,
  subtitle,
  accent = 'var(--cf-orange)',
  iconBg,
  actions,
  breadcrumb,
}: PageHeaderProps) {
  const pathname = usePathname();
  const crumbs = breadcrumb ?? buildBreadcrumb(pathname, title);

  return (
    <header
      className="flex-shrink-0 px-6 pt-3.5 pb-4 bg-white"
      style={{ borderBottom: '1px solid var(--border-light)' }}
    >
      {/* 面包屑 */}
      <nav aria-label="面包屑" className="flex items-center gap-0.5 mb-2 text-xs">
        {crumbs.map((c, i) => {
          const isLast = i === crumbs.length - 1;
          const isFirst = i === 0;
          const content = (
            <span className="inline-flex items-center gap-1">
              {isFirst && <Home size={13} className="flex-shrink-0" />}
              {c.label}
            </span>
          );
          return (
            <span key={i} className="flex items-center gap-0.5">
              {c.href && !isLast ? (
                <Link
                  href={c.href}
                  className="inline-flex items-center rounded-md px-1.5 py-0.5 font-medium transition-colors hover:bg-[var(--bg-tertiary)]"
                  style={{ color: 'var(--text-secondary)' }}
                >
                  {content}
                </Link>
              ) : (
                <span
                  aria-current={isLast ? 'page' : undefined}
                  className="px-1.5 py-0.5"
                  style={{
                    color: isLast ? 'var(--text-primary)' : 'var(--text-tertiary)',
                    fontWeight: isLast ? 600 : 400,
                  }}
                >
                  {content}
                </span>
              )}
              {!isLast && (
                <ChevronRight size={13} className="flex-shrink-0" style={{ color: 'var(--text-tertiary)', opacity: 0.5 }} />
              )}
            </span>
          );
        })}
      </nav>

      {/* 标题行 */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <div
            className="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0"
            style={{ background: iconBg ?? 'rgba(37,99,235,0.1)' }}
          >
            <Icon size={19} style={{ color: accent }} />
          </div>
          <div className="min-w-0">
            <h1
              className="text-xl font-semibold leading-tight truncate tracking-tight"
              style={{ color: 'var(--text-primary)' }}
            >
              {title}
            </h1>
            {subtitle && (
              <div className="text-[13px] mt-0.5 truncate" style={{ color: 'var(--text-tertiary)' }}>
                {subtitle}
              </div>
            )}
          </div>
        </div>
        {actions && <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>}
      </div>
    </header>
  );
}
