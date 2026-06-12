"use client";

export default function TechBackground() {
  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {/* base gradient */}
      <div className="absolute inset-0 bg-ink-950" />

      {/* drifting aurora blobs */}
      <div className="aurora-blob aurora-1" />
      <div className="aurora-blob aurora-2" />
      <div className="aurora-blob aurora-3" />

      {/* perspective grid floor */}
      <div className="tech-grid" />

      {/* vignette + noise */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_30%,rgba(5,6,15,0.85))]" />
      <div className="noise-overlay" />
    </div>
  );
}
