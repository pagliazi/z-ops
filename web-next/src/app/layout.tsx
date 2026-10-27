import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";

// P1 (task-c17) 构建修复: 内网环境不可达 fonts.googleapis.com, next/font/google
// 离线构建失败 → 改用本地字体栈; CSS 变量由 globals.css :root 提供同名回退。
// (原 Geist/Geist_Mono next/font 导入移除, 渲染回退 system-ui 系字体)

export const metadata: Metadata = {
  title: "智 OC - Operation Claw",
  description: "Z-OPS 运维智能体平台",
  icons: {
    icon: '/favicon.png',
    apple: '/favicon.png',
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="h-full antialiased" suppressHydrationWarning>
      <body className="h-full">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
