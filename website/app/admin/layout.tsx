import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "无界科技 · 转化看板",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return children;
}
