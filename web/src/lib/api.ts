import { apiUrl, connStore } from "./config";
import type {
  Account,
  ChatConfirm,
  ChatMessage,
  Decision,
  ExpiredSummary,
  Friend,
  Group,
  LlmTest,
  LoginStatus,
  Member,
  Message,
  MessageGroup,
  PipelineStatus,
  RetentionSettings,
  UserProfile,
  ReviewItem,
  SelfCheck,
  Settings,
  Stats,
  Todo,
  TodoView,
  Whitelist,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 把状态码翻译成"哪一步失败了、该怎么办" */
export function humanError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return "连不上核心服务：检查地址，或确认服务已启动";
    if (e.status === 401) return "访问令牌无效，请重新填写";
    if (e.status === 503) return "QQ 协议端未连接：请确认 NapCat 已启动并登录";
    if (e.status === 404) return e.message || "这条内容已不存在";
    return e.message || `请求失败（${e.status}）`;
  }
  return e instanceof Error ? e.message : "未知错误";
}

type Q = Record<string, string | number | null | undefined>;

function qs(q?: Q): string {
  if (!q) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: { base?: string; token?: string; auth?: boolean } = {},
): Promise<T> {
  const conn = connStore.get();
  const token = opts.token ?? conn.token;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.auth !== false && token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(apiUrl(path, opts.base), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "网络不通");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.json();
      detail = typeof j?.detail === "string" ? j.detail : Array.isArray(j?.detail) ? "参数不合法" : "";
    } catch {
      /* 非 JSON */
    }
    // 只有"正在使用的令牌"失效才踢回连接页；测试连接时传入的临时令牌不影响全局
    if (res.status === 401 && opts.token === undefined && opts.auth !== false) {
      connStore.set((c) => ({ ...c, invalid: true }));
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(p: string, q?: Q) => request<T>("GET", p + qs(q));

/** 账号列表的 query key。实时层和以后可能用到它的界面共用这一个 key */
export const ACCOUNTS_KEY = ["accounts"] as const;

/** 账号列表里协议端此刻登录的那个号；没有（协议端未登录）或列表未加载时返回 null */
export function liveAccount(list: Account[] | null | undefined): Account | null {
  if (!Array.isArray(list)) return null;
  return list.find((a) => a.is_live) ?? null;
}

export const api = {
  healthz: (base?: string) => request<{ status: string; version: string }>("GET", "/healthz", undefined, { base, auth: false }),
  loginStatus: (base?: string, token?: string) =>
    request<LoginStatus>("GET", "/api/login/status", undefined, { base, token }),
  /** 账号列表；is_live 的那条就是协议端当前登录号。没有"切换账号"的接口 */
  accounts: () => get<Account[]>("/api/accounts"),
  selfcheck: () => get<SelfCheck>("/api/selfcheck"),
  stats: () => get<Stats>("/api/stats"),
  pipeline: () => get<PipelineStatus>("/api/pipeline/status"),
  flush: () => request<{ processed: number }>("POST", "/api/pipeline/flush"),
  sync: () => request<{ groups: number; members: number; friends: number }>("POST", "/api/napcat/sync"),
  backfill: () => request<{ groups: number; scanned: number; inserted: number }>("POST", "/api/napcat/backfill"),

  /** 唯一视图入口。旧的 ?status= 不再使用 */
  todos: (view: TodoView, limit = 100) => get<Todo[]>("/api/todos", { view, limit }),
  patchTodo: (id: number, body: Partial<Pick<Todo, "title" | "detail" | "due_at" | "status">>) =>
    request<Todo>("PATCH", `/api/todos/${id}`, body),
  /** 手动添加。origin 由后端写死 manual，前端不传 */
  createTodo: (body: { title: string; due_at: string | null; detail?: string }) =>
    request<Todo>("POST", "/api/todos", body),
  /** 收起提示：是否显示只看返回的 show */
  expiredSummary: () => get<ExpiredSummary>("/api/todos/expired-summary"),
  /** 收起提示"知道了"：回传 newest_at，同一个值后端不再返回 show=true */
  ackExpiredSummary: (newest_at?: string | null) =>
    request<unknown>("POST", "/api/todos/expired-summary/ack", newest_at ? { newest_at } : {}),

  /** 画像：一段自我介绍（≤500 字）；带 pending */
  profile: () => get<UserProfile>("/api/memory/profile"),
  saveProfile: (about: string) => request<UserProfile>("PUT", "/api/memory/profile", { about }),
  /** 画像提议。返回可能是数组，也容错 {items} */

  /** 保留期：短键 { overdue_days, content_days } */
  retention: () => get<RetentionSettings>("/api/retention/settings"),
  putRetention: (body: RetentionSettings) => request<RetentionSettings>("PUT", "/api/retention/settings", body),

  review: () => get<ReviewItem[]>("/api/review"),
  resolveReview: (id: number, body: { action: "confirm" | "ignore"; title?: string; due_at?: string | null; detail?: string }) =>
    request<unknown>("POST", `/api/review/${id}/resolve`, body),

  messages: (q: { group_id?: number | null; before_id?: number; limit?: number; private?: boolean }) =>
    get<Message[]>("/api/messages", { group_id: q.group_id ?? undefined, before_id: q.before_id, limit: q.limit ?? 50 }),
  message: (msgId: number) => get<Message & { raw?: unknown }>(`/api/messages/${msgId}`),
  messageGroups: () => get<MessageGroup[]>("/api/messages/groups"),
  decisions: (limit = 500) => get<Decision[]>("/api/decisions", { limit }),
  setVerdict: (msgId: number, body: { verdict: "todo" | "ignored"; title?: string; due_at?: string | null }) =>
    request<{ ok: boolean; msg_id: number; verdict: string; todo_id: number | null }>(
      "POST",
      `/api/messages/${msgId}/verdict`,
      body,
    ),

  chat: (before_id?: number, limit = 40) => get<ChatMessage[]>("/api/chat/messages", { before_id, limit }),
  /** 5：返回体带 cards[]（可能缺省，按空处理） */
  sendChat: (text: string) =>
    request<{ user: ChatMessage; assistant: ChatMessage; cards?: ChatConfirm[] }>("POST", "/api/chat/messages", { text }),
  /** 5：未处理的确认卡，刷新后恢复按钮用。接口是 {items}，也容错直接数组 */
  chatConfirms: () =>
    get<{ items?: ChatConfirm[] } | ChatConfirm[]>("/api/chat/confirms", { status: "pending" }).then((r) =>
      Array.isArray(r) ? r : Array.isArray(r?.items) ? r.items : [],
    ),
  /** 5：点确认卡按钮。幂等：重复点击后端返回 already=true（不再执行）。返回体只用来兜底取 text */
  resolveConfirm: (id: number, choice: string) => request<unknown>("POST", `/api/chat/confirms/${id}`, { choice }),

  settings: () => get<Settings>("/api/settings"),
  putSettings: (body: Partial<Settings>) => request<Settings>("PUT", "/api/settings", body),
  testLlm: () => request<LlmTest>("POST", "/api/settings/llm/test"),

  groups: (q?: string) => get<Group[]>("/api/groups", { q }),
  members: (gid: number, q?: string) => get<Member[]>(`/api/groups/${gid}/members`, { q }),
  friends: (q?: string) => get<Friend[]>("/api/friends", { q }),
  whitelist: () => get<Whitelist>("/api/whitelist"),
  putWhitelist: (body: Whitelist) => request<Whitelist>("PUT", "/api/whitelist", body),
};

/** 给 useQuery / fetchQuery 直接用的账号列表查询配置 */
export const accountsQuery = { queryKey: ACCOUNTS_KEY, queryFn: api.accounts };
