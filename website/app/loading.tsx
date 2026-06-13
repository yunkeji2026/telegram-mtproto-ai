export default function Loading() {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 text-center">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="aurora-blob aurora-2" />
      </div>
      <div className="relative z-10 flex flex-col items-center gap-4">
        <div
          className="h-10 w-10 animate-spin rounded-full border-2 border-slate-700 border-t-cyan-400"
          role="status"
          aria-label="加载中 / Loading"
        />
        <div className="text-sm font-medium tracking-wide text-slate-400">
          华灵科技 <span className="text-gradient">HuaLing Tech</span>
        </div>
      </div>
    </main>
  );
}
