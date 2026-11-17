// 与后端接口对应的类型。可选字段一律容错。

export type TodoStatus = "open" | "done" | "dropped";
export type Verdict = "todo" | "review" | "ignored";

/** 待办视图。main = 还能行动的（未完成、未过期）；history = 已完成 / 不用做 / 已过期 */
export type TodoView = "main" | "history";

export interface TodoSource {
  msg_id: number;
  group_id: number | null;
  group_name: string | null;
  sender_name: string | null;
  quote: string;
  msg_time: number;
}

export interface Todo {
  /** message | review | chat | manual，没有原话来源时用它显示"来自对话"之类 */
  origin?: string;
  id: number;
  title: string;
  detail: string;
  due_at: string | null;
  status: TodoStatus;
  confidence: number | null;
  created_at: string;
  updated_at: string;
  sources: TodoSource[];
  /**
   * 逾期超过保留期 → true，只出现在历史视图。
   * 由后端判定，前端不自己算天数；缺省按 false 处理。
   */
  expired?: boolean;
}

/** GET /api/todos/expired-summary。是否显示只看 show */
export interface ExpiredSummary {
  show: boolean;
  /** 这次收起了几件 */
  count?: number;
  /** 最新一件被收起的时间；ack 时回传，同一个值只提示一次 */
  newest_at?: string | null;
}

/** GET/PUT /api/retention/settings（短键） */
export interface RetentionSettings {
  /** 逾期多少天后收起，1–30 */
  overdue_days: number;
  /** 群消息与助手对话保留多少天，7–365 */
  content_days: number;
}

export interface Suggestion {
  title: string;
  detail: string;
  due_at: string | null;
  confidence: number;
  reason: string;
}

/** 对话事件进待确认的原因 */
export type AskReason = "no_request" | "no_date" | "both";

export interface ReviewItem {
  id: number;
  block_id: number;
  msg_id: number;
  confidence: number;
  question: string;
  evidence: string;
  status: "pending" | "confirmed" | "ignored";
  created_at: string;
  preview?: { group_name: string | null; sender_name: string | null; text: string } | null;
  suggestion?: Suggestion | null;
  /** 来源。缺省按 message 处理 */
  source?: "message" | "chat";
  /** 为什么没直接建待办；文案已体现在 question 里，前端只用来挑短提示 */
  ask_reason?: AskReason | null;
  /** 用户原话里的时间说法，如「下个月」 */
  time_text?: string | null;
  /** 触发它的那条对话消息（ChatMessage.id） */
  chat_message_id?: number | null;
}

export interface Message {
  id: number;
  msg_id: number;
  group_id: number | null;
  user_id: number;
  message_type: "group" | "private" | string;
  sender_name: string | null;
  time: number;
  text: string;
  via?: string;
  ingested_at?: string;
}

export interface MessageGroup {
  group_id: number | null;
  group_name: string;
  message_count: number;
  last_time: number;
}

export interface DecisionRoute {
  verdict: Verdict;
  /** 谁定的档位：rule#3 / directive+me / optional+due / fallback … */
  by?: string;
  /** 人话原因，规则命中时形如「命中规则 #3：…；模型原判：待确认」 */
  why?: string;
  rule_id?: number;
}

export interface Decision {
  id: number;
  block_id: number;
  msg_id: number;
  stage: string;
  verdict: Verdict;
  reason: string;
  confidence: number | null;
  evidence?: string;
  created_at: string;
  /**  起：档位是谁定的、为什么 —— 规则命中时前端要显示它 */
  route?: DecisionRoute;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  /** 5：confirm = 带确认卡的助手消息（按钮另由 ChatConfirm 提供） */
  kind: "text" | "question" | "notice" | "confirm";
  text: string;
  ref_review_id?: number | null;
  ref_msg_id?: number | null;
  created_at: string;
}

/** 5：确认卡种类。前端不按种类区分样式，未知值照常渲染 */
export type ConfirmKind = "diverge" | "unclear" | "focus_add" | "profile" | "ignore_remove" | (string & {});

/** 按钮：value 回传给后端，label 原样显示（前端不发明文案） */
export interface ConfirmChoice {
  value: string;
  label: string;
}

/**
 * 5：确认卡。
 * 来源：POST /api/chat/messages 的 cards[]，或 GET /api/chat/confirms?status=pending 的 items[]。
 * message 是对应 kind='confirm' 的助手消息行，按钮按 message.id 挂到对话里。
 */
export interface ChatConfirm {
  id: number;
  kind: ConfirmKind;
  topic?: string | null;
  target_id?: number | null;
  status?: "pending" | (string & {});
  choices: ConfirmChoice[];
  message: ChatMessage | null;
}

export interface Stats {
  messages_total?: number;
  messages_today?: number;
  messages_dropped?: number;
  groups_cached?: number;
  whitelist_groups?: number;
  whitelist_users?: number;
  napcat_connected?: boolean;
  llm_calls?: number;
  todos_open?: number;
  review_pending?: number;
}

export interface LoginStatus {
  logged_in: boolean;
  self_id: number | null;
  nickname: string | null;
}

/**
 * GET /api/accounts 的一条。
 * - is_live：协议端此刻登录的号（同一时刻最多一条为 true）
 * - is_base：主库归属账号，内部概念，界面可不显示
 * 旧的 is_current 已删除，不要再用。
 */
export interface Account {
  id: number;
  self_id: number;
  nickname: string;
  role: "main" | "test" | string;
  created_at: string;
  last_seen_at: string | null;
  is_base: boolean;
  is_live: boolean;
}

export interface SelfCheck {
  ok: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
}

export interface PipelineStatus {
  enabled: boolean;
  provider?: string;
  model?: string;
  pending: number;
  llm_calls: number;
  last_error: string | null;
}

export interface Settings {
  llm_base_url?: string;
  llm_model?: string;
  llm_api_key?: string;
  llm_api_key_set?: boolean;
  backfill_enabled?: boolean;
  backfill_count?: number;
  adapter_ws_url?: string;
  access_token_masked?: string;
  llm_provider?: "openai" | "anthropic";
  pipeline_enabled?: boolean;
  confidence_threshold?: number;
  aggregate_window_seconds?: number;
  timezone?: string;
  notify_review_queue?: boolean;
}

export interface LlmTest {
  ok: boolean;
  provider?: string;
  model?: string;
  latency_ms?: number;
  error?: string;
}

export interface Group {
  group_id: number;
  group_name: string;
  member_count: number;
  whitelisted: boolean;
  mode: "all" | "members" | null;
  whitelisted_member_ids: number[];
}

export interface Member {
  user_id: number;
  nickname: string;
  card: string;
  role: string;
  whitelisted: boolean;
}

export interface Friend {
  user_id: number;
  nickname: string;
  remark: string;
  whitelisted: boolean;
}

export interface Whitelist {
  groups: { group_id: number; mode: "all" | "members"; member_ids: number[] }[];
  users: number[];
}

/**
 * 画像提议。只列了字段名，没列 status 的取值；
 * 前端按 pending / applied / rejected / superseded / stale 及常见别名容错，未知值显示"已处理"。
 */
export type ProfileProposalStatus = "pending" | "applied" | "rejected" | "superseded" | "stale" | (string & {});

export interface ProfileProposal {
  id: number;
  about: string;
  base_about?: string | null;
  evidence?: string[] | null;
  /** 触发它的那条助手回复（ChatMessage.id） */
  chat_message_id?: number | null;
  status?: ProfileProposalStatus;
  created_at?: string;
  decided_at?: string | null;
  /** 只在 GET /api/memory/profile 的 pending 上：提议之后用户手改过画像 → 采用会 409 */
  base_changed?: boolean;
}

/** 用户画像：一段自我介绍，上限 500 字 */
export interface UserProfile {
  about: string;
  updated_at: string;
  /** 当前待确认的提议；没有为 null/缺省 */
  pending?: ProfileProposal | null;
}

/**
 * 业务事件（message.created / todo.created / review.created / chat.message）
 * 都带 account_id = 账号表 id（不是 QQ 号）。hello 等连接级事件不带。
 * 放在公共部分，是为了在按 type 收窄之前就能读到它。
 */
interface RtScoped {
  account_id?: number;
}

export type RealtimeEvent = RtScoped &
  (
    | { type: "hello" }
    | { type: "message.created"; msg_id: number; group_id: number | null; sender_name?: string; text?: string; time?: number }
    | { type: "todo.created"; todo_id: number; msg_id?: number; title?: string }
    | { type: "review.created"; review_id: number; msg_id?: number; confidence?: number; question?: string }
    | { type: "chat.message"; id: number; role: string; kind: string }
  );
