/** Strip common markdown so plain-text channels (bot / widget) read clean.
 *  Pure function — safe to import on client and server. */
export function cleanMarkdown(s: string): string {
  return s
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/__(.+?)__/g, "$1")
    .replace(/(^|\n)#{1,6}\s*/g, "$1")
    .replace(/(^|\n)\s*\*\s+/g, "$1- ")
    .replace(/`{1,3}/g, "")
    .trim();
}
