// 关注清单（ / 043B）：类型、接口、查询、添加与删除。
// 后端：GET /api/focus-topics → {items:[...]}；POST /api/focus-topics {topic}；
//       DELETE /api/focus-topics/{id} → {deleted:{id,topic}}，不存在时 400。
// 方案要求的「最近因它多给你看的几条」后端暂无明细接口，本文件只透出 hits 次数。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, humanError, request } from "./api";
import { toast } from "./toast";

export interface FocusTopic {
  id: number;
  topic: string;
  /** 用户原话 */
  note?: string | null;
  created_at?: string;
  /** 因这个话题从"忽略"捞到"待确认"的次数 */
  hits: number;
}

export const FOCUS_TOPICS_KEY = ["focus-topics"] as const;

function isTopic(x: unknown): x is FocusTopic {
  return !!x && typeof x === "object" && typeof (x as FocusTopic).id === "number" && typeof (x as FocusTopic).topic === "string";
}

function clean(t: FocusTopic): FocusTopic {
  return { ...t, hits: typeof t.hits === "number" && t.hits > 0 ? Math.floor(t.hits) : 0 };
}

/** 接口是 {items}，也容错直接返回数组；hits 缺省或非法按 0 */
function normalize(r: unknown): FocusTopic[] {
  const raw: unknown[] = Array.isArray(r)
    ? r
    : Array.isArray((r as { items?: unknown } | null)?.items)
      ? (r as { items: unknown[] }).items
      : [];
  return raw.filter(isTopic).map(clean);
}

/** 添加接口的返回：可能是话题本身，也可能包一层 {item}/{topic: {...}}；拿不到就返回 null，靠重取兜底 */
function pickTopic(r: unknown): FocusTopic | null {
  if (isTopic(r)) return clean(r);
  const o = r as { item?: unknown; topic?: unknown } | null;
  if (isTopic(o?.item)) return clean(o!.item as FocusTopic);
  if (isTopic(o?.topic)) return clean(o!.topic as FocusTopic);
  return null;
}

export const focusApi = {
  list: () => request<unknown>("GET", "/api/focus-topics").then(normalize),
  add: (topic: string) => request<unknown>("POST", "/api/focus-topics", { topic }).then(pickTopic),
  remove: (id: number) => request<{ deleted: { id: number; topic: string } }>("DELETE", `/api/focus-topics/${id}`),
};

/** 话题比较：去空白、忽略大小写，用于前端查重 */
export const sameTopic = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

export function useFocusTopics() {
  return useQuery({ queryKey: FOCUS_TOPICS_KEY, queryFn: focusApi.list, staleTime: 15000 });
}

/** 手动添加：成功后若后端回了新话题就先插进缓存，再重取以服务器为准 */
export function useAddFocusTopic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (topic: string) => focusApi.add(topic),
    onSuccess: (created, topic) => {
      if (created)
        qc.setQueryData<FocusTopic[]>(FOCUS_TOPICS_KEY, (l) =>
          l ? (l.some((t) => t.id === created.id) ? l : [...l, created]) : l,
        );
      toast(`已关注「${created?.topic ?? topic}」`);
    },
    onError: (e) => toast(humanError(e), undefined, "error"),
    onSettled: () => qc.invalidateQueries({ queryKey: FOCUS_TOPICS_KEY }),
  });
}

/** 删除：成功后先从缓存里移除（列表立刻少一条），再重取以服务器为准 */
export function useDeleteFocusTopic() {
  const qc = useQueryClient();
  const drop = (id: number) => qc.setQueryData<FocusTopic[]>(FOCUS_TOPICS_KEY, (l) => l?.filter((t) => t.id !== id));
  return useMutation({
    mutationFn: (t: FocusTopic) => focusApi.remove(t.id),
    onSuccess: (_r, t) => {
      drop(t.id);
      toast(`已不再关注「${t.topic}」`);
    },
    onError: (e, t) => {
      // 400 = 话题已不存在（比如在别的设备上删过）：结果和用户想要的一致，按已删处理
      if (e instanceof ApiError && e.status === 400) {
        drop(t.id);
        toast(`「${t.topic}」已经不在关注清单里了`);
        return;
      }
      toast(humanError(e), undefined, "error");
    },
    onSettled: () => qc.invalidateQueries({ queryKey: FOCUS_TOPICS_KEY }),
  });
}
