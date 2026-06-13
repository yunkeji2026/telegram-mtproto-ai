import { promises as fs } from "fs";
import path from "path";
import { DATA_DIR } from "./data-dir";

export interface SentRef {
  chat: string;
  messageId: number;
}

interface Db {
  version: 1;
  posts: SentRef[];
  overview: SentRef[];
}

const DIR = DATA_DIR;
const FILE = process.env.CATALOG_STORE || path.join(DIR, "catalog-msgs.json");

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next as Promise<T>;
}

async function readDb(): Promise<Db> {
  try {
    const raw = await fs.readFile(FILE, "utf8");
    const p = JSON.parse(raw);
    return { version: 1, posts: p.posts ?? [], overview: p.overview ?? [] };
  } catch {
    return { version: 1, posts: [], overview: [] };
  }
}

async function writeDb(db: Db): Promise<void> {
  await fs.mkdir(path.dirname(FILE), { recursive: true });
  const tmp = `${FILE}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(db, null, 2), "utf8");
  await fs.rename(tmp, FILE);
}

export async function getCatalogRefs(): Promise<{ posts: SentRef[]; overview: SentRef[] }> {
  const db = await readDb();
  return { posts: db.posts, overview: db.overview };
}

export async function saveCatalogRefs(posts: SentRef[], overview: SentRef[]): Promise<void> {
  return serialize(async () => {
    await writeDb({ version: 1, posts, overview });
  });
}
