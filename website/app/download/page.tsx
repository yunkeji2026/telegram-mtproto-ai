import type { Metadata } from "next";
import DownloadPage from "@/components/DownloadPage";

export const metadata: Metadata = {
  title: "下载智聊 ChatX 桌面端 · 多语种 AI 员工 | 无界科技 BOUNDLESS",
  description:
    "下载智聊 ChatX Windows 桌面端：内置本地服务免装环境，填一个 AI Key 即翻译生效，接号进统一收件箱，AI 以你的人设 7×24 接客。支持免费试用（字符额度制），客户数据落本地库。",
  keywords: ["智聊", "ChatX", "AI客服下载", "聊天翻译软件", "多语种AI", "跨境私域", "AI员工", "免费试用"],
  alternates: {
    canonical: "/download",
    languages: { "zh-CN": "/download", en: "/en/download", "x-default": "/download" },
  },
  robots: { index: true, follow: true },
};

export default function Page() {
  return <DownloadPage />;
}
