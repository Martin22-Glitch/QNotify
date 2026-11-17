// 外观偏好：两个视觉方向 × 深浅色 × 密度 × 紧迫感形态。写到 <html> 的 data-* 上，由 CSS token 响应。
import { createStore } from "./store";

export type Direction = "sky" | "ink";
export type Mode = "light" | "dark" | "auto";
export type Density = "cozy" | "compact";
export type UrgencyStyle = "focus" | "countdown";

export interface Prefs {
  dir: Direction;
  mode: Mode;
  density: Density;
  urgency: UrgencyStyle;
}

export const prefsStore = createStore<Prefs>(
  { dir: "sky", mode: "light", density: "cozy", urgency: "focus" },
  "qn.prefs",
);

const mq = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null;

export function applyPrefs(): void {
  const p = prefsStore.get();
  const el = document.documentElement;
  el.dataset.dir = p.dir;
  el.dataset.density = p.density;
  const dark = p.mode === "dark" || (p.mode === "auto" && !!mq?.matches);
  el.dataset.mode = dark ? "dark" : "light";
}

let started = false;
/** 只挂一次：偏好变化 / 系统深浅色变化 → 重新写 data-* */
export function startPrefs(): void {
  if (started) return;
  started = true;
  applyPrefs();
  prefsStore.subscribe(applyPrefs);
  mq?.addEventListener("change", applyPrefs);
}
