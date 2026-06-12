import { ImageResponse } from "next/og";

export const runtime = "edge";
export const alt = "华灵科技 HuaLing Tech · 华影 LiveAvatar × 灵犀 SoulSync";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OgImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "80px",
          background:
            "radial-gradient(circle at 20% 20%, #1a1d3a, #05060f 60%)",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ fontSize: 34, color: "#22d3ee", letterSpacing: 4 }}>
          华灵科技 · HuaLing Tech
        </div>
        <div style={{ fontSize: 72, fontWeight: 800, marginTop: 24, lineHeight: 1.15 }}>
          华影 LiveAvatar × 灵犀 SoulSync
        </div>
        <div style={{ fontSize: 36, color: "#94a3b8", marginTop: 28 }}>
          换脸换声 · 数字人 · AI 自动成交 · 拟人翻译 · 私有部署
        </div>
        <div style={{ fontSize: 30, color: "#8b5cf6", marginTop: 40 }}>
          灵动智能，华丽呈现 · 全程 USDT 结算
        </div>
      </div>
    ),
    size
  );
}
