// 服务地址 + 访问令牌：存浏览器本地，绝不写死。
// baseUrl 为空 = 同源（开发时走 vite 代理；部署在核心服务同源时也可直接用）。
import { createStore } from "./store";

export interface ConnConfig {
  baseUrl: string;
  token: string;
  /** 401 后置为 true，界面回到"连接"入口并提示令牌无效 */
  invalid: boolean;
}

export const connStore = createStore<ConnConfig>({ baseUrl: "", token: "", invalid: false }, "qn.conn");

export function apiUrl(path: string, base = connStore.get().baseUrl): string {
  const b = base.trim().replace(/\/+$/, "");
  return b + path;
}

export function wsUrl(token: string, base = connStore.get().baseUrl): string {
  const b = base.trim().replace(/\/+$/, "");
  let origin: string;
  if (b) {
    origin = b.replace(/^http/i, "ws");
  } else {
    origin = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
  }
  return `${origin}/ws/events?token=${encodeURIComponent(token)}`;
}

export function isConfigured(): boolean {
  const c = connStore.get();
  return !!c.token && !c.invalid;
}
