import { useEffect, useState, useSyncExternalStore } from "react";

/** 每 30 秒刷新一次"现在"，让相对时间/倒计时自动走 */
export function useNow(ms = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(query);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** ≥1024px 视为宽屏：三栏布局 */
export function useWide(): boolean {
  return useMedia("(min-width: 1024px)");
}
