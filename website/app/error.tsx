"use client";

import { CONTACT_URL } from "@/lib/site";

export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 text-center">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="aurora-blob aurora-1" />
        <div className="aurora-blob aurora-2" />
      </div>
      <div className="glass relative z-10 w-full max-w-md rounded-3xl p-8">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-cyan-500/10 text-3xl">
          ⚡
        </div>
        <div className="text-lg font-bold tracking-tight text-white">
          无界科技 <span className="text-gradient">BOUNDLESS</span>
        </div>
        <p className="mx-auto mt-3 max-w-sm text-sm leading-relaxed text-slate-400">
          页面加载遇到点小问题，请重试，或直接联系客服获取方案。
          <br />
          <span className="text-slate-500">
            Something went wrong. Please retry or contact us.
          </span>
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <button
            onClick={() => reset()}
            className="rounded-xl bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-ink-950 transition hover:bg-cyan-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-300"
          >
            重试 / Retry
          </button>
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
