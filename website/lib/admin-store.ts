import { readFile, writeFile, mkdir } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const STORE =
  process.env.ADMIN_CHAT_STORE ||
  path.join(DATA_DIR, "admin_chats.json");

/** Read all admin chat IDs (env overrides + self-bound). */
export async function getAdminChats(): Promise<string[]> {
  const set = new Set<string>();
  const envChat = process.env.TELEGRAM_CHAT_ID;
  if (envChat) envChat.split(",").forEach((c) => c.trim() && set.add(c.trim()));
  try {
    const raw = await readFile(STORE, "utf-8");
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) arr.forEach((c) => set.add(String(c)));
  } catch {
    /* no store yet */
  }
  return [...set];
}

export async function bindAdminChat(chatId: number | string): Promise<boolean> {
  const id = String(chatId);
  let arr: string[] = [];
  try {
    const raw = await readFile(STORE, "utf-8");
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) arr = parsed.map(String);
  } catch {
    /* fresh */
  }
  if (arr.includes(id)) return false;
  arr.push(id);
  await mkdir(path.dirname(STORE), { recursive: true });
  await writeFile(STORE, JSON.stringify(arr, null, 2));
  return true;
}

export async function unbindAdminChat(chatId: number | string): Promise<boolean> {
  const id = String(chatId);
  try {
    const raw = await readFile(STORE, "utf-8");
    const parsed = JSON.parse(raw);
    const arr = (Array.isArray(parsed) ? parsed.map(String) : []).filter((c) => c !== id);
    await writeFile(STORE, JSON.stringify(arr, null, 2));
    return true;
  } catch {
    return false;
  }
}
