"use client";

export default function GlobalError({ reset }: { error: Error; reset: () => void }) {
  return (
    <html lang="zh-CN">
      <body style={{ margin: 0, background: "#05060f", color: "#e2e8f0", fontFamily: "system-ui, sans-serif" }}>
        <div
          style={{
            minHeight: "100vh",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 16,
            padding: 24,
            textAlign: "center",
          }}
        >
          <div style={{ fontSize: 40 }}>⚡</div>
          <div style={{ fontSize: 18, fontWeight: 700 }}>无界科技 BOUNDLESS</div>
          <div style={{ fontSize: 14, color: "#94a3b8", maxWidth: 320 }}>
            页面加载遇到点小问题，请重试。也可以直接联系我们获取 AI 自动成交 / 换脸换声 / 私有部署方案。
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", justifyContent: "center" }}>
            <button
              onClick={() => reset()}
              style={{ background: "#22d3ee", color: "#05060f", border: "none", borderRadius: 10, padding: "10px 18px", fontWeight: 600 }}
            >
              重试
            </button>
            <a
              href="https://t.me/ai_zkw"
              style={{ background: "#1e293b", color: "#e2e8f0", borderRadius: 10, padding: "10px 18px", textDecoration: "none" }}
            >
              联系客服
            </a>
            <a
              href="https://t.me/tgzkw_bot"
              style={{ background: "#1e293b", color: "#e2e8f0", borderRadius: 10, padding: "10px 18px", textDecoration: "none" }}
            >
              返回机器人
            </a>
          </div>
        </div>
      </body>
    </html>
  );
}
