import { humanError } from "./api";
import { createStore } from "./store";

export interface ToastItem {
  id: number;
  text: string;
  tone: "info" | "error";
  action?: { label: string; run: () => unknown };
}

export const toastStore = createStore<ToastItem[]>([]);
let seq = 1;

export function toast(text: string, action?: ToastItem["action"], tone: ToastItem["tone"] = "info"): void {
  const id = seq++;
  toastStore.set((l) => [...l.slice(-2), { id, text, tone, action }]);
  setTimeout(() => dismissToast(id), action ? 5000 : 2600);
}

export function toastError(e: unknown): void {
  toast(humanError(e), undefined, "error");
}

export function dismissToast(id: number): void {
  toastStore.set((l) => l.filter((t) => t.id !== id));
}
