// ═══════════════════════════════════════════════════
// 路由元数据 — 面包屑 / 页面标题 的单一数据源
// 与 Sidebar 的分区结构保持一致
// ═══════════════════════════════════════════════════

export interface Crumb {
  label: string;
  href?: string;
}

// 各路由所属分区（与 Sidebar NAV_SECTIONS 对应）
const SECTION_OF: Record<string, string> = {
  '/agents': '系统',
  '/architecture': '系统',
  '/llm-wiki': '系统',
  '/observability': '系统',
  '/usage': '系统',
  '/logs': '系统',
  '/n8n': '自动化',
  '/notify': '自动化',
  '/admin': '管理',
  '/notify-config': '管理',
};

/**
 * 根据 pathname 与页面标题生成统一的面包屑。
 * 结构：首页 / [分区] / 当前页
 */
export function buildBreadcrumb(pathname: string, title: string): Crumb[] {
  const crumbs: Crumb[] = [{ label: '首页', href: '/' }];

  // 匹配最长的路由前缀，兼容子路由
  const matched = Object.keys(SECTION_OF)
    .filter((p) => pathname === p || pathname.startsWith(p + '/'))
    .sort((a, b) => b.length - a.length)[0];

  if (matched) {
    crumbs.push({ label: SECTION_OF[matched] });
  }

  crumbs.push({ label: title });
  return crumbs;
}
