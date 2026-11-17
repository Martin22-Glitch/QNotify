import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, humanError } from "./api";
import { toast } from "./toast";
import type {
  Decision,
  Message,
  RetentionSettings,
  ReviewItem,
  Todo,
  TodoStatus,
  TodoView,
  UserProfile,
  Verdict,
} from "./types";

/* ───────────── 待办：按视图取 ───────────── */

/** 所有待办相关查询都挂在 ["todos", …] 下，invalidate(["todos"]) 一次全刷（含收起提示） */
export const TODOS_KEY = ["todos"] as const;
export const todoViewKey = (v: TodoView) => ["todos", v] as const;
export const EXPIRED_SUMMARY_KEY = ["todos", "expired-summary"] as const;
export const RETENTION_KEY = ["retention"] as const;
export const PROFILE_KEY = ["profile"] as const;
/** 挂在 ["profile"] 下：invalidate(["profile"]) 一次把画像和提议一起刷 */
const NONE: Todo[] = [];

/** 唯一视图入口。main = 还能行动的；history = 已完成 / 不用做 / 已过期 */
export function useTodoView(view: TodoView) {
  return useQuery({ queryKey: todoViewKey(view), queryFn: () => api.todos(view) });
}

/**
 * main ∪ history = 全部待办，再按状态拆开。
 * 消息页的"该消息是否已建待办"依赖它；待办页本身不再用它。
 * open 里包含已过期的未完成项（它们在 history 视图里）。
 */
export function useAllTodos() {
  const main = useTodoView("main");
  const history = useTodoView("history");
  const m = main.data ?? NONE;
  const h = history.data ?? NONE;
  const split = useMemo(() => {
    const seen = new Set<number>();
    const open: Todo[] = [];
    const done: Todo[] = [];
    const dropped: Todo[] = [];
    for (const t of [...m, ...h]) {
      if (seen.has(t.id)) continue;
      seen.add(t.id);
      (t.status === "done" ? done : t.status === "dropped" ? dropped : open).push(t);
    }
    return { open, done, dropped };
  }, [m, h]);
  return { ...split, loading: main.isLoading || history.isLoading, error: main.error ?? history.error };
}

/** @deprecated 兼容旧调用：由两个视图拼出，不再请求 ?status= */
export function useTodos(status: TodoStatus) {
  const all = useAllTodos();
  return { data: all[status], isLoading: all.loading, error: all.error };
}

/* ───────────── 收起提示 / 保留期 ───────────── */

export function useExpiredSummary() {
  return useQuery({ queryKey: EXPIRED_SUMMARY_KEY, queryFn: api.expiredSummary, staleTime: 30000, retry: false });
}

/** "知道了"：先在本地藏起来，再告诉后端；失败则恢复并提示 */
export function useAckExpired() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (newest_at?: string | null) => api.ackExpiredSummary(newest_at),
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: EXPIRED_SUMMARY_KEY });
      const prev = qc.getQueryData(EXPIRED_SUMMARY_KEY);
      qc.setQueryData(EXPIRED_SUMMARY_KEY, (p: { show: boolean } | undefined) => (p ? { ...p, show: false } : p));
      return { prev };
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(EXPIRED_SUMMARY_KEY, ctx.prev);
      toast(errText(e), undefined, "error");
    },
    onSettled: () => qc.invalidateQueries({ queryKey: EXPIRED_SUMMARY_KEY }),
  });
}

/* ───────────── 画像 / 画像提议 ───────────── */

export function useProfile() {
  return useQuery({ queryKey: PROFILE_KEY, queryFn: api.profile, staleTime: 30000 });
}

/** 保存画像：只影响之后的判定，不需要重取待办 */
export function useSaveProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (about: string) => api.saveProfile(about),
    onSuccess: (r) => {
      // 合并写入：PUT 的返回可能不带 pending，直接覆盖会让顶部提议卡闪没
      if (r && typeof r.about === "string") qc.setQueryData<UserProfile>(PROFILE_KEY, (old) => (old ? { ...old, ...r } : r));
      // 手改画像后，待确认提议会变成 base_changed：重取拿最新的 pending
      qc.invalidateQueries({ queryKey: PROFILE_KEY });
    },
  });
}

/** 提议状态缺省视为 pending（profile.pending 上可能不带 status） */
export function isPendingStatus(s: string | null | undefined): boolean {
  return !s || s.toLowerCase() === "pending";
}

/** 从 GET /api/memory/profile 里取出待确认提议；容错数组形态 */
/** 全部提议（含已处理），用于对话里按 chat_message_id 挂卡、刷新后显示结果文案 */
export function useRetention() {
  return useQuery({ queryKey: RETENTION_KEY, queryFn: api.retention, staleTime: 60000 });
}

/** 保存保留期；成功后待办视图、收起提示、消息相关查询全部重取（后端会立即按新规则处理） */
export function useSaveRetention() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RetentionSettings) => api.putRetention(body),
    onSuccess: (r, body) => {
      qc.setQueryData(RETENTION_KEY, r && typeof r.overdue_days === "number" ? r : body);
      for (const k of ["todos", "stats", "messages", "msgGroups", "decisions"]) qc.invalidateQueries({ queryKey: [k] });
      toast("已保存，已按新规则整理");
    },
  });
}

/* ───────────── 其他查询 ───────────── */

export function useReview() {
  return useQuery({ queryKey: ["review"], queryFn: api.review });
}
export function useStats() {
  return useQuery({ queryKey: ["stats"], queryFn: api.stats, staleTime: 5000 });
}
export function usePipeline() {
  return useQuery({ queryKey: ["pipeline"], queryFn: api.pipeline, refetchInterval: 30000, retry: false });
}
export function useMsgGroups() {
  return useQuery({ queryKey: ["msgGroups"], queryFn: api.messageGroups });
}
export function useDecisions() {
  return useQuery({ queryKey: ["decisions"], queryFn: () => api.decisions(500) });
}

export function useMessages(groupId: number | null | "all") {
  return useInfiniteQuery({
    queryKey: ["messages", groupId],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      api.messages({ group_id: groupId === "all" ? undefined : groupId, before_id: pageParam, limit: 50 }),
    getNextPageParam: (last: Message[]) => (last.length < 50 ? undefined : last[last.length - 1].id),
  });
}

export type MsgClass = "todo" | "review" | "ignored" | "none";
export interface MsgVerdict {
  cls: MsgClass;
  reason: string;
  manual: boolean;
  todo?: Todo;
  review?: ReviewItem;
  history: Decision[];
}

/**
 * 消息 → 当前判定。后端 /api/messages 不带判定，这里用三路数据拼：
 *  1) /api/decisions（最新一条为准；stage=manual 的人工改判优先）
 *  2) 待办 sources（同块所有消息都会挂在待办上；dropped = 已忽略）
 *  3) 待确认队列（pending 才算"待决定"；判成 review 但已不在队列 = 已经处理过）
 */
export function useVerdictIndex() {
  const dec = useDecisions();
  const todos = useAllTodos();
  const review = useReview();
  return useMemo(() => {
    const byMsg = new Map<number, Decision[]>();
    for (const d of dec.data ?? []) {
      const arr = byMsg.get(d.msg_id) ?? [];
      arr.push(d);
      byMsg.set(d.msg_id, arr);
    }
    for (const arr of byMsg.values()) arr.sort((a, b) => b.id - a.id);
    const todoByMsg = new Map<number, Todo>();
    for (const t of [...todos.dropped, ...todos.done, ...todos.open]) {
      for (const s of t.sources) todoByMsg.set(s.msg_id, t);
    }
    const reviewByMsg = new Map<number, ReviewItem>();
    for (const r of review.data ?? []) reviewByMsg.set(r.msg_id, r);

    return (msgId: number): MsgVerdict => {
      const history = byMsg.get(msgId) ?? [];
      const latest = history[0];
      const todo = todoByMsg.get(msgId);
      const rv = reviewByMsg.get(msgId);
      if (rv) return { cls: "review", reason: rv.question || latest?.reason || "", manual: false, review: rv, history };
      if (latest?.stage === "manual") {
        return { cls: latest.verdict as MsgClass, reason: "你改判的", manual: true, todo, history };
      }
      if (todo) {
        return {
          cls: todo.status === "dropped" ? "ignored" : "todo",
          reason: todo.status === "dropped" ? "待办已标为忽略" : latest?.reason || "已建待办",
          manual: todo.status === "dropped",
          todo,
          history,
        };
      }
      if (latest) {
        if (latest.verdict === "review") return { cls: "ignored", reason: "你在待确认里忽略了", manual: true, history };
        return { cls: latest.verdict as MsgClass, reason: latest.reason, manual: false, history };
      }
      return { cls: "none", reason: "", manual: false, history };
    };
  }, [dec.data, todos.open, todos.done, todos.dropped, review.data]);
}

/* ───────────── 变更 ───────────── */

const STATUS_LABEL: Record<TodoStatus, string> = { open: "未完成", done: "已完成", dropped: "已忽略" };

export function usePatchTodo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { todo: Todo; patch: Partial<Pick<Todo, "title" | "detail" | "due_at" | "status">>; silent?: boolean }) =>
      api.patchTodo(v.todo.id, v.patch),
    onMutate: async ({ todo, patch }) => {
      // 乐观更新：未完成且未过期 → 主视图；其余 → 历史。最终以后端重取为准
      await qc.cancelQueries({ queryKey: TODOS_KEY });
      const next: Todo = { ...todo, ...patch, updated_at: new Date().toISOString() };
      const inMain = next.status === "open" && !next.expired;
      const place = (view: TodoView, here: boolean) =>
        qc.setQueryData<Todo[]>(todoViewKey(view), (list) => {
          if (!list) return list;
          const without = list.filter((t) => t.id !== todo.id);
          return here ? [next, ...without] : without;
        });
      place("main", inMain);
      place("history", !inMain);
    },
    onSuccess: (_d, { todo, patch, silent }) => {
      if (silent) return;
      if (patch.status && patch.status !== todo.status) {
        toast(`已标为${STATUS_LABEL[patch.status]}`, {
          label: "撤销",
          run: () => api.patchTodo(todo.id, { status: todo.status }).then(() => qc.invalidateQueries({ queryKey: TODOS_KEY })),
        });
      } else toast("已保存");
    },
    onError: (e) => toast(errText(e), undefined, "error"),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: TODOS_KEY });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
}

/** 手动添加。不做乐观插入（失败时表单要保留内容）；成功后先塞进对应视图，再整体重取 */
export function useCreateTodo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; due_at: string | null; detail?: string }) => api.createTodo(body),
    onSuccess: (t) => {
      if (!t || typeof t.id !== "number") return;
      const view: TodoView = t.status === "open" && !t.expired ? "main" : "history";
      qc.setQueryData<Todo[]>(todoViewKey(view), (list) => (list && !list.some((x) => x.id === t.id) ? [t, ...list] : list));
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: TODOS_KEY });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
}

export function useResolveReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { item: ReviewItem; action: "confirm" | "ignore"; title?: string; due_at?: string | null; detail?: string }) =>
      api.resolveReview(v.item.id, { action: v.action, title: v.title, due_at: v.due_at, detail: v.detail }),
    onMutate: async ({ item }) => {
      await qc.cancelQueries({ queryKey: ["review"] });
      qc.setQueryData<ReviewItem[]>(["review"], (l) => l?.filter((r) => r.id !== item.id));
    },
    onError: (e) => toast(errText(e), undefined, "error"),
    onSettled: () => {
      for (const k of ["review", "todos", "decisions", "stats"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
}

export function useSetVerdict() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { msgId: number; verdict: Exclude<Verdict, "review">; title?: string; due_at?: string | null }) =>
      api.setVerdict(v.msgId, { verdict: v.verdict, title: v.title || undefined, due_at: v.due_at || undefined }),
    onSuccess: (_r, v) => toast(v.verdict === "todo" ? "已转为待办" : "已标为忽略"),
    onError: (e) => toast(errText(e), undefined, "error"),
    onSettled: () => {
      for (const k of ["todos", "decisions", "review", "stats"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
}

function errText(e: unknown): string {
  return humanError(e);
}
