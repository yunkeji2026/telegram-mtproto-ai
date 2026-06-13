import MiniAppClient from "./client";
import { resolveView } from "./routing";

// 服务端读取 ?view= 决定初始视图，消除带参进入时「先渲染概览再切换」的首屏闪烁。
// 使用 searchParams 会让本页按需动态渲染（轻量页面，可接受）。
export default function AppPage({ searchParams }: { searchParams?: { view?: string | string[] } }) {
  const raw = Array.isArray(searchParams?.view) ? searchParams?.view[0] : searchParams?.view;
  return <MiniAppClient initialView={resolveView(raw)} />;
}
