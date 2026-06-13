import { ImageResponse } from "next/og";

export const runtime = "edge";
export const alt = "华灵科技 HuaLing Tech · 华影 LiveAvatar × 灵犀 SoulSync";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Load the bundled brand mark (256px) at build time and inline it as a data URL.
// Edge-safe: fetch(new URL(..., import.meta.url)) resolves the traced asset, no network.
const markPromise = fetch(
  new URL("../public/brand/logos/hualing-mark-256.png", import.meta.url)
)
  .then((res) => res.arrayBuffer())
  .catch(() => null);

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

export default async function OgImage() {
  const buf = await markPromise;
  const mark = buf ? `data:image/png;base64,${toBase64(buf)}` : "";
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "72px 80px",
          background: "radial-gradient(circle at 18% 18%, #1a1d3a, #05060f 62%)",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", maxWidth: 720 }}>
          <div style={{ display: "flex", fontSize: 34, color: "#22d3ee", letterSpacing: 4 }}>
            华灵科技 · HuaLing Tech
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 68,
              fontWeight: 800,
              marginTop: 22,
              lineHeight: 1.15,
            }}
          >
            华影 LiveAvatar × 灵犀 SoulSync
          </div>
          <div style={{ display: "flex", fontSize: 34, color: "#94a3b8", marginTop: 26 }}>
            换脸换声 · 数字人 · AI 自动成交 · 拟人翻译 · 私有部署
          </div>
          <div style={{ display: "flex", fontSize: 30, color: "#8b5cf6", marginTop: 34 }}>
            灵动智能，华丽呈现 · 全程 USDT 结算
          </div>
        </div>
        {mark ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={mark} alt="华灵科技" width={300} height={300} />
        ) : null}
      </div>
    ),
    size
  );
}
