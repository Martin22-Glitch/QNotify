// 实时通道：模块级单例。
// —— 为什么不会"请求雪崩"——
// 1. WebSocket、重连定时器、轮询定时器都只存在于这个模块的闭包里，和 React 渲染周期无关；
// 2. 唯一入口 ensureRealtime(key)：key = baseUrl + token；key 不变时重复调用是 no-op，
//    key 变化时先 stop() 关掉旧连接/定时器再建新的；
// 3. React 侧只在 <RealtimeBridge> 的 useEffect([key]) 里调用一次，StrictMode 双调用也被 key 比较吸收。
//
// —— 账号作用域——
// 服务端把事件广播给所有连接，不按连接过滤；业务事件带 account_id。
// 1. onEvent 的第一步是 acceptEvent()：事件的 account_id 与"已知的当前登录账号"不同 → 整条丢弃
//    （不刷新、不高亮、不通知监听者）；事件不带 account_id，或还不知道当前账号 → 照旧处理。
// 2. "已知的当前登录账号"只有一个来源：["accounts"] 查询缓存里 is_live 的那条。
//    通过订阅 QueryCache 同步，所以不管是实时层自己拉的，还是以后界面拉的，口径都一样。
// 3. 换号：缓存里的 is_live 从一个确认过的号变成另一个号 → 清掉高亮 + 按下面的口径重取。
//    首次拿到账号列表（之前从没确认过）不算换号，不会多失效一次。
//
// —— 换号时怎么重取——
// - 分页查询（messages / chat，useInfiniteQuery + before_id）：resetQueries。丢掉旧页和游标，
//   在途的"加载更多"（带旧号 before_id）一并作废，从第一页重拉。
// - 其他按账号分库的业务查询（todos / review / msgGroups / decisions / stats）：也 reset。
//   invalidate 会在新数据回来前一直显示旧号的数据，而且各库自增 id 会撞号（在旧列表上点"完成"
//   会改到新号的同 id 待办）。宁可闪一下加载态。
// - 账号列表和与账号无关的查询（设置、协议端状态等）：照旧 invalidate。
import type { Query, QueryClient, QueryKey } from "@tanstack/react-query";
import { ACCOUNTS_KEY, accountsQuery, liveAccount } from "./api";
import { connStore, wsUrl } from "./config";
import { createStore } from "./store";
import type { Account, RealtimeEvent } from "./types";

export type RtState = "idle" | "connecting" | "live" | "polling" | "unauthorized";

export const rtStore = createStore<{ state: RtState; since: number; fresh: Record<string, number> }>({
  state: "idle",
  since: Date.now(),
  fresh: {},
});

/** 某个 key 最近被实时事件"点亮"——列表里用来做新条目高亮 */
export function markFresh(k: string): void {
  rtStore.set((s) => ({ ...s, fresh: { ...s.fresh, [k]: Date.now() } }));
}

const POLL_MS = 8000; // 兜底轮询 ≤10 秒
const MAX_BACKOFF = 15000;
const ACCOUNTS_GAP = 5000; // "不知道当前账号"时补拉账号列表的最小间隔
const PROBE_GAP = 5000; // 同一个"别的号"最多每 5 秒触发一次换号探测

let qc: QueryClient | null = null;
let currentKey = "";
let ws: WebSocket | null = null;
let retry = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let hadLive = false;
const listeners = new Set<(e: RealtimeEvent) => void>();

// —— 账号状态 ——
/** 已知的当前登录账号（账号表 id）。null = 不知道：未加载 / 接口失败 / 协议端没登录 */
let liveId: number | null = null;
/** 最近一次确认过的登录号，只用来判断"是不是换号了"，不参与过滤 */
let lastLiveId: number | null = null;
let accountsReqAt = 0;
const probedAt = new Map<number, number>();
let unsubCache: (() => void) | null = null;

function setState(state: RtState) {
  if (rtStore.get().state !== state) rtStore.set((s) => ({ ...s, state, since: Date.now() }));
}

const LIVE_KEYS: QueryKey[] = [
  ACCOUNTS_KEY,
  ["todos"],
  ["review"],
  ["messages"],
  ["msgGroups"],
  ["decisions"],
  ["stats"],
  ["chat"],
];

/** 分页查询（before_id 游标）：换号必须丢游标从头拉 */
const PAGED_ROOTS: readonly string[] = ["messages", "chat"];
/** 非分页、但按账号分库的业务查询：换号时丢弃旧数据，避免显示旧号内容 / id 撞号 */
const SCOPED_ROOTS: readonly string[] = ["todos", "review", "msgGroups", "decisions", "stats"];
const RESET_ON_SWITCH = new Set<string>([...PAGED_ROOTS, ...SCOPED_ROOTS]);

function resetOnSwitch(q: Query): boolean {
  return RESET_ON_SWITCH.has(String(q.queryKey[0]));
}

function isAccountsKey(k: QueryKey): boolean {
  return k.length === 1 && k[0] === ACCOUNTS_KEY[0];
}

/** 从 ["accounts"] 的查询状态同步 liveId；可以重入，重复调用不会多触发换号 */
function syncAccounts(state: { status: string; data?: unknown } | undefined) {
  if (!state) return;
  if (state.status === "error") {
    // 接口失败：按"不知道"处理，宁可多刷也不要漏（lastLiveId 保留，恢复后仍能识别换号）
    liveId = null;
    return;
  }
  if (!Array.isArray(state.data)) return; // 还在首次加载
  const live = liveAccount(state.data as Account[])?.id ?? null;
  liveId = live;
  if (live === null) return; // 协议端没登录：当作不知道
  const prev = lastLiveId;
  lastLiveId = live;
  if (prev !== null && prev !== live) onAccountSwitched();
}

/** 协议端换号了：让页面直接切到新号的数据，不留旧号的任何内容 */
function onAccountSwitched() {
  probedAt.clear();
  // 各账号的库是分开的，todo/review 的自增 id 会撞号；旧号的高亮不能留到新号上
  rtStore.set((s) => ({ ...s, fresh: {} }));
  const client = qc;
  // 我们正在 QueryCache 的通知回调里，重取挪到下一拍
  setTimeout(() => {
    if (!client || client !== qc) return;
    // 业务查询：清数据 + 清分页游标 + 作废在途请求；挂载中的从第一页重拉，没挂载的下次挂载时重拉
    void client.resetQueries({ predicate: resetOnSwitch });
    // 账号列表与账号无关的查询：保持原来的 invalidate
    void client.invalidateQueries({ predicate: (q) => !resetOnSwitch(q) });
  }, 0);
}

/** 拉一次账号列表。结果通过缓存订阅进 syncAccounts，这里不处理返回值 */
function refreshAccounts(force = false) {
  const client = qc;
  if (!client || !currentKey) return;
  const now = Date.now();
  if (!force && now - accountsReqAt < ACCOUNTS_GAP) return;
  accountsReqAt = now;
  client.fetchQuery({ ...accountsQuery, staleTime: 0 }).catch(() => {
    /* 失败会让查询进入 error 状态 → syncAccounts 把 liveId 置空 */
  });
}

/** 过滤：返回 false = 整条事件丢弃 */
function acceptEvent(e: RealtimeEvent): boolean {
  const aid = typeof e.account_id === "number" ? e.account_id : null;
  if (aid === null) return true; // hello 等连接级事件
  if (liveId === null) {
    refreshAccounts(); // 照旧处理，顺便（节流地）补拉一次账号
    return true;
  }
  if (aid === liveId) return true;
  // 另一个号：可能是协议端换号了，也可能是旧号晚到的事件 → 去问一次账号列表
  const now = Date.now();
  if (now - (probedAt.get(aid) ?? 0) >= PROBE_GAP) {
    probedAt.set(aid, now);
    refreshAccounts(true);
  }
  return false;
}

function resetAccounts() {
  liveId = null;
  lastLiveId = null;
  accountsReqAt = 0;
  probedAt.clear();
  if (qc) {
    // 换了服务地址/令牌：旧服务的账号列表不能再用，在途请求的结果也要丢掉
    void qc.cancelQueries({ queryKey: ACCOUNTS_KEY });
    qc.removeQueries({ queryKey: ACCOUNTS_KEY });
  }
}

function bindClient(client: QueryClient) {
  if (qc === client) return;
  unsubCache?.();
  qc = client;
  syncAccounts(client.getQueryState(ACCOUNTS_KEY));
  unsubCache = client.getQueryCache().subscribe((ev) => {
    if (ev.type === "removed" || !isAccountsKey(ev.query.queryKey)) return;
    syncAccounts(ev.query.state);
  });
}

function invalidateAll() {
  if (!qc) return;
  for (const k of LIVE_KEYS) qc.invalidateQueries({ queryKey: k });
  // 没有组件订阅 ["accounts"] 时，invalidate 只是标记过期、不会发请求；实时层自己得拿到登录号
  refreshAccounts(true);
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(invalidateAll, POLL_MS);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function onEvent(e: RealtimeEvent) {
  if (!qc) return;
  if (!acceptEvent(e)) return;
  switch (e.type) {
    case "message.created":
      markFresh(`msg:${e.msg_id}`);
      qc.invalidateQueries({ queryKey: ["messages"] });
      qc.invalidateQueries({ queryKey: ["msgGroups"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      // 判定会在几秒后落库，稍后再拉一次决策
      setTimeout(() => qc?.invalidateQueries({ queryKey: ["decisions"] }), 1500);
      break;
    case "todo.created":
      markFresh(`todo:${e.todo_id}`);
      qc.invalidateQueries({ queryKey: ["todos"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      break;
    case "review.created":
      markFresh(`review:${e.review_id}`);
      qc.invalidateQueries({ queryKey: ["review"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["chat"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      break;
    case "chat.message":
      qc.invalidateQueries({ queryKey: ["chat"] });
      break;
  }
  listeners.forEach((l) => l(e));
}

function connect() {
  const { token } = connStore.get();
  if (!token) return;
  setState("connecting");
  let sock: WebSocket;
  try {
    sock = new WebSocket(wsUrl(token));
  } catch {
    scheduleReconnect();
    return;
  }
  ws = sock;
  sock.onopen = () => {
    if (ws !== sock) return;
    retry = 0;
    stopPolling();
    setState("live");
    // 断线期间的事件不会补发：重连成功后全量失效重取一次（含账号列表，断线期间可能换过号；
    // 真换过号时，账号列表回来后 syncAccounts 会走 onAccountSwitched 做 reset）
    if (hadLive) invalidateAll();
    else refreshAccounts();
    hadLive = true;
  };
  sock.onmessage = (m) => {
    if (ws !== sock) return;
    try {
      onEvent(JSON.parse(String(m.data)) as RealtimeEvent);
    } catch {
      /* 非 JSON 忽略 */
    }
  };
  sock.onclose = (ev) => {
    if (ws !== sock) return;
    ws = null;
    if (ev.code === 4401) {
      // 令牌无效：不要无限重连
      setState("unauthorized");
      stopPolling();
      connStore.set((c) => ({ ...c, invalid: true }));
      return;
    }
    scheduleReconnect();
  };
  sock.onerror = () => {
    /* onclose 会接着触发 */
  };
}

function scheduleReconnect() {
  setState("polling");
  startPolling();
  if (reconnectTimer) return;
  const delay = Math.min(MAX_BACKOFF, 1000 * 2 ** retry) + Math.random() * 400;
  retry += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function stop() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  stopPolling();
  if (ws) {
    const s = ws;
    ws = null;
    s.close();
  }
  retry = 0;
  hadLive = false;
  resetAccounts();
}

export function ensureRealtime(client: QueryClient, key: string): void {
  bindClient(client);
  if (key === currentKey) return;
  stop();
  currentKey = key;
  if (key) {
    connect();
    refreshAccounts(true); // 不等 WS 握手，尽早知道当前登录号
  } else setState("idle");
}

export function onRealtime(fn: (e: RealtimeEvent) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// 页面从后台回到前台 / 网络恢复：立刻补一次
if (typeof window !== "undefined") {
  window.addEventListener("online", () => {
    if (currentKey && !ws) {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      retry = 0;
      connect();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && currentKey) invalidateAll();
  });
}
