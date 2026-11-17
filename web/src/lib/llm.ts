// 多模型档案 + 判定/对话分工
import { request } from "./api";

export type LlmProvider = "openai" | "anthropic";

export interface LlmProfile {
  id: number;
  name: string;
  provider: LlmProvider;
  base_url: string;
  model: string;
  /** 默认档案，列表里有且仅有一条 */
  active: boolean;
  /** 只回布尔，永不回明文 */
  api_key_set: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  last_test_at?: string | null;
  last_test_ok?: boolean | null;
  last_test_error?: string | null;
}

export interface LlmSelection {
  judge: number | null;
  chat: number | null;
  judge_name?: string | null;
  chat_name?: string | null;
  fallback_id: number | null;
  fallback_name?: string | null;
}

export interface LlmTestResult {
  ok: boolean;
  latency_ms?: number;
  error?: string;
  provider?: string;
  model?: string;
}

export interface ProfileInput {
  name?: string;
  provider: LlmProvider;
  base_url: string;
  model: string;
  api_key?: string;
}

export type LlmRole = "judge" | "chat";

export const LLM_PROFILES_KEY = ["llm-profiles"] as const;
export const LLM_SELECTION_KEY = ["llm-selection"] as const;

// 统一走 api.ts 的 request：401 会置 invalid、网络错误会转成 ApiError，
// 页面里的 humanError 才能给出人话（自己写 fetch 会丢掉这些）。
const call = <T>(method: string, path: string, body?: unknown): Promise<T> =>
  request<T>(method, path, body);

export const llmApi = {
  list: () => call<LlmProfile[]>("GET", "/api/llm/profiles"),
  create: (b: ProfileInput) => call<LlmProfile>("POST", "/api/llm/profiles", b),
  /** 不传 api_key = 不改 */
  patch: (id: number, b: Partial<ProfileInput>) => call<LlmProfile>("PATCH", `/api/llm/profiles/${id}`, b),
  remove: (id: number) => call<{ ok: boolean; id: number }>("DELETE", `/api/llm/profiles/${id}`),
  activate: (id: number) => call<LlmProfile>("POST", `/api/llm/profiles/${id}/activate`),
  test: (id: number) => call<LlmTestResult>("POST", `/api/llm/profiles/${id}/test`),
  selection: () => call<LlmSelection>("GET", "/api/llm/selection"),
  /** 只传要改的项；null = 恢复跟随默认 */
  putSelection: (b: Partial<Record<LlmRole, number | null>>) => call<LlmSelection>("PUT", "/api/llm/selection", b),
};
