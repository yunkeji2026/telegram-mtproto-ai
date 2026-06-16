"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";

/** When opened inside Telegram on the heavy marketing root, send the user to the
 *  lightweight /app Mini App. Lives in the layout so it runs even if the page
 *  subtree throws (error boundary replaces the page, not the layout). */
export default function TgRedirect() {
  const pathname = usePathname();
  useEffect(() => {
    try {
      const tg = window.Telegram?.WebApp;
      const inTg = Boolean(tg?.platform && tg.platform !== "unknown");
      if (inTg && pathname === "/") {
        window.location.replace("/app");
      }
    } catch {
      /* non-fatal */
    }
  }, [pathname]);
  return null;
}
