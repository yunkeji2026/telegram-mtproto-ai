import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "@/lib/data-dir";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LOG =
  process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");

export async function POST(req: NextRequest) {
  try {
    const data = await req.json();
    const rec = {
      t: new Date().toISOString(),
      event: String(data?.event ?? "").slice(0, 64),
      props: data?.props ?? null,
      path: String(data?.path ?? "").slice(0, 200),
      ref: String(data?.ref ?? "").slice(0, 300),
      ua: (req.headers.get("user-agent") ?? "").slice(0, 250),
    };
    if (rec.event) {
      await mkdir(path.dirname(LOG), { recursive: true });
      await appendFile(LOG, JSON.stringify(rec) + "\n");
    }
  } catch {
    /* never fail tracking */
  }
  return new NextResponse(null, { status: 204 });
}
