// 5：助手页确认卡。
// 按钮挂在 kind='confirm' 那条助手消息的时间行里，不另造卡片容器。
// 真值：GET /api/chat/confirms?status=pending。POST /api/chat/messages 带回的 cards[] 只用来补上"重取到达之前"那一小段空档。
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import { api, ApiError, humanError } from "../lib/api";
import { PROFILE_KEY } from "../lib/queries";
import { toast } from "../lib/toast";
import type { ChatConfirm, ChatMessage } from "../lib/types";
import { Button } from "./ui";

export const CONFIRMS_KEY = ["chat-confirms", "pending"] as const;
const CHAT_KEY = ["chat"] as const;

/** 能渲染的卡：未处理、挂得上消息、至少一个带 label 的选项 */
function usable(c: ChatConfirm | null | undefined): c is ChatConfirm & { message: ChatMessage } {
  return (
    !!c &&
    typeof c.id === "number" &&
    (!c.status || c.status === "pending") &&
    !!c.message &&
    typeof c.message.id === "number" &&
    Array.isArray(c.choices) &&
    c.choices.some((x) => typeof x?.value === "string" && typeof x?.label === "string" && x.label.trim() !== "")
  );
}

/** 点击接口的返回体不确定，只尽量取一段文字（只在对话重取失败时兜底用） */
function resultText(r: unknown): string {
  if (typeof r === "string") return r;
  if (!r || typeof r !== "object") return "";
  const o = r as Record<string, unknown>;
  const a = o.assistant && typeof o.assistant === "object" ? (o.assistant as Record<string, unknown>) : o;
  return typeof a.text === "string" ? a.text : "";
}

/**
 * 这张卡是不是"别处已经处理过"。
 * 真实后端的幂等返回是 200 + {already: true, assistant: null}（不是 400），
 * 所以除了 400/404/409，成功分支也要认这个标记。
 */
function alreadyHandled(r: unknown): boolean {
  return !!r && typeof r === "object" && (r as Record<string, unknown>).already === true;
}

export function useChatConfirms() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: CONFIRMS_KEY, queryFn: api.chatConfirms, staleTime: 15000 });
  // 刚发消息带回、服务器列表还没重取到的卡
  const [fresh, setFresh] = useState<ReadonlyMap<number, ChatConfirm>>(new Map());
  // 本页已处理的卡：防止在途的旧重取把它带回来
  const [resolved, setResolved] = useState<ReadonlySet<number>>(new Set());

  /** message.id → 卡（一条消息只挂一张，服务器数据优先） */
  const byMessage = useMemo(() => {
    const m = new Map<number, ChatConfirm & { message: ChatMessage }>();
    for (const c of [...(q.data ?? []), ...fresh.values()]) {
      if (!usable(c) || resolved.has(c.id) || m.has(c.message.id)) continue;
      m.set(c.message.id, c);
    }
    return m;
  }, [q.data, fresh, resolved]);

  /** 卡片自带的消息行：并进对话列表，这样不必等 /chat/messages 重取，也不怕它在未加载的更早一页 */
  const messages = useMemo(() => [...byMessage.values()].map((c) => c.message), [byMessage]);

  /** 发送成功后收下 cards[]；服务器列表重取成功后，以服务器为准丢掉本地副本 */
  const adopt = useCallback(
    (cards: ChatConfirm[] | undefined) => {
      const ok = (cards ?? []).filter(usable);
      if (ok.length === 0) return;
      const at = Date.now();
      setFresh((m) => {
        const n = new Map(m);
        for (const c of ok) n.set(c.id, c);
        return n;
      });
      void qc.invalidateQueries({ queryKey: CONFIRMS_KEY }).then(() => {
        const st = qc.getQueryState(CONFIRMS_KEY);
        if (st?.status !== "success" || st.dataUpdatedAt < at) return; // 重取失败：继续用本地副本
        setFresh((m) => {
          const n = new Map(m);
          for (const c of ok) n.delete(c.id);
          return n;
        });
      });
    },
    [qc],
  );

  const markResolved = useCallback(
    (id: number) => {
      setResolved((s) => new Set(s).add(id));
      setFresh((m) => {
        if (!m.has(id)) return m;
        const n = new Map(m);
        n.delete(id);
        return n;
      });
      qc.setQueryData<ChatConfirm[]>(CONFIRMS_KEY, (l) => l?.filter((c) => c.id !== id));
    },
    [qc],
  );

  return { byMessage, messages, adopt, markResolved };
}

/** 执行后可能影响的数据：对话要等它回来（结果消息与按钮消失同一刻出现），其余后台刷新 */
async function refreshAfter(qc: QueryClient) {
  // 待确认（C2）/ 关注与忽略（规则）/ 关于我（画像）
  for (const k of [["review"], ["todos"], ["stats"], ["agent-rules"], PROFILE_KEY]) void qc.invalidateQueries({ queryKey: k });
  await qc.invalidateQueries({ queryKey: CHAT_KEY });
}

export function ConfirmChoices({ card, onDone }: { card: ChatConfirm; onDone: (id: number) => void }) {
  const qc = useQueryClient();
  // 同步锁：两次点击可能落在同一帧，isPending 还没来得及更新
  const lock = useRef(false);

  const mut = useMutation({
    mutationFn: (choice: string) => api.resolveConfirm(card.id, choice),
    onSuccess: async (res) => {
      // 200 + already=true：另一个标签页 / 重复请求已经处理过，这里只收拾本地状态
      if (alreadyHandled(res)) {
        await refreshAfter(qc);
        onDone(card.id);
        void qc.invalidateQueries({ queryKey: CONFIRMS_KEY });
        toast("这张卡已经处理过了");
        return;
      }
      await refreshAfter(qc);
      onDone(card.id);
      void qc.invalidateQueries({ queryKey: CONFIRMS_KEY });
      // 对话没刷出来：用后端给的结果文字兜底，免得点了没反馈
      const t = resultText(res);
      if (t && qc.getQueryState(CHAT_KEY)?.status === "error") toast(t);
    },
    onError: async (e) => {
      // 400/404/409：多半是别处（另一个标签页 / 重复请求）已经处理过 → 以服务器为准
      if (e instanceof ApiError && [400, 404, 409].includes(e.status)) {
        await qc.invalidateQueries({ queryKey: CONFIRMS_KEY });
        const st = qc.getQueryState(CONFIRMS_KEY);
        const still = qc.getQueryData<ChatConfirm[]>(CONFIRMS_KEY)?.some((c) => c.id === card.id);
        if (st?.status === "success" && !still) {
          await refreshAfter(qc);
          onDone(card.id);
          toast("这张卡已经处理过了");
          return;
        }
      }
      toast(humanError(e) || "没处理成功，再试一次", undefined, "error");
    },
    onSettled: () => {
      lock.current = false;
    },
  });

  const pick = (v: string) => {
    if (lock.current || mut.isPending) return;
    lock.current = true;
    mut.mutate(v);
  };

  const choices = card.choices.filter((c) => typeof c?.value === "string" && typeof c?.label === "string" && c.label.trim() !== "");

  return (
    <div role="group" aria-label="确认" aria-busy={mut.isPending} className="flex flex-wrap items-center gap-2 min-w-0 max-w-full">
      {choices.map((c) => (
        // 显式 disabled：Button 里 rest 会覆盖 loading 推出的 disabled
        <Button
          key={c.value}
          size="sm"
          tone={c.value === "dismiss" ? "plain" : "primary"}
          disabled={mut.isPending}
          loading={mut.isPending && mut.variables === c.value}
          onClick={() => pick(c.value)}
          className="max-w-full"
        >
          {c.label}
        </Button>
      ))}
    </div>
  );
}
