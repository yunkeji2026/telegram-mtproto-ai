// Storage access can throw inside embedded webviews (e.g. Telegram on some
// platforms partition/disable storage). These helpers never throw.

export function getLocal(key: string): string | null {
  try {
    return typeof window !== "undefined" ? window.localStorage.getItem(key) : null;
  } catch {
    return null;
  }
}

export function setLocal(key: string, value: string): void {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export function getSession(key: string): string | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage.getItem(key) : null;
  } catch {
    return null;
  }
}

export function setSession(key: string, value: string): void {
  try {
    if (typeof window !== "undefined") window.sessionStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}
