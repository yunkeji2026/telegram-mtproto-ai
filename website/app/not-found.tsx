import Link from "next/link";
import { CONTACT_URL } from "@/lib/site";

export default function NotFound() {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 text-center">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="aurora-blob aurora-1" />
        <div className="aurora-blob aurora-3" />
      </div>
      <div className="glass relative z-10 w-full max-w-md rounded-3xl p-8">
        <div className="text-gradient text-6xl font-black tracking-tighter">404</div>
        <div className="mt-2 text-lg font-bold tracking-tight text-white">
          无界科技 <span className="text-gradient">BOUNDLESS</span>
        </div>
        <p className="mx-auto mt-3 max-w-sm text-sm leading-relaxed text-slate-400">
          找不到这个页面，它可能已被移动或不存在。
          <br />
          <span className="text-slate-500">This page could not be found.</span>
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/"
            className="rounded-xl bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-ink-950 transition hover:bg-cyan-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-300"
          >
            返回首页 / Home
          </Link>
          <a
            href={CONTACT_URL}
            className="rounded-xl border border-slate-700 bg-slate-800/60 px-5 py-2.5 text-sm font-medium text-slate-200 transition hover:border-slate-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-300"
          >
            联系客服 / Contact
          </a>
        </div>
      </div>
    </main>
  );
}
