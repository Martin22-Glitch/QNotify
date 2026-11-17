// 助手：单一会话流。只渲染纯文本；AI 主动提问（kind=question）带一个跳转到待确认的入口。
// 对话中学到的规则建议（status=suggested）以卡片形式挂到最后一条助手回复正下方，确认后才生效。
// 画像提议挂在产生它的那条助手回复正下方（按 chat_message_id），已处理的显示一行结果。
// 追问卡片：待确认里来自对话的最新一条，挂在触发它的那轮对话下面；找不到就落在列表末尾。
// 确认卡（5）：按钮挂在 kind='confirm' 那条助手消息的时间行里（按 card.message.id）；
//   卡片自带的消息行并进列表，所以新消息和刷新恢复走同一条渲染路径。
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { PROFILE_KEY } from "../lib/queries";
import { rulesApi, ruleText } from "../lib/rules";
import { hm } from "../lib/time";
import { toastError } from "../lib/toast";
import type { ChatConfirm, ChatMessage } from "../lib/types";
import { useChatAsk } from "../components/chatAsk";
import { ConfirmChoices, useChatConfirms } from "../components/chatConfirm";
import { IconCheck, IconInbox, IconSend, IconSpark } from "../components/icons";
import { RuleSuggestCard } from "../components/rules";
import { Button, cx, Help } from "../components/ui";

const SUGGEST = ["这周还有什么没做？", "以后辅导员的讲座通知都忽略", "明天有几件要交的？"];

export function AssistantPage({ go }: { go: (t: "review") => void }) {
  const qc = useQueryClient();
  const q = useInfiniteQuery({
    queryKey: ["chat"],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => api.chat(pageParam, 40),
    getNextPageParam: (last: ChatMessage[]) => (last.length < 40 ? undefined : last[last.length - 1].id),
  });
  // 5：确认卡。卡片自带的消息行与服务器消息按 id 合并（服务器优先），保证按钮总有地方挂
  const confirms = useChatConfirms();
  const list = useMemo(() => {
    const byId = new Map<number, ChatMessage>();
    for (const m of confirms.messages) byId.set(m.id, m);
    for (const m of q.data?.pages.flat() ?? []) byId.set(m.id, m);
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }, [q.data, confirms.messages]);

  const [text, setText] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const send = useMutation({
    mutationFn: (t: string) => api.sendChat(t),
    onMutate: (t) => setPending(t),
    // 5：本轮带回的确认卡先收下，服务器待处理列表随后重取校准
    onSuccess: (r) => confirms.adopt(r?.cards),
    onSettled: () => {
      setPending(null);
      qc.invalidateQueries({ queryKey: ["chat"] });
      // 这轮对话可能产生了新的规则建议，一并重取
      qc.invalidateQueries({ queryKey: ["agent-rules"] });
      // 也可能产生了画像提议（["profile"] 前缀同时刷画像与提议列表）
      qc.invalidateQueries({ queryKey: PROFILE_KEY });
      // 也可能进了待确认（source=chat），追问卡片靠它出现
      qc.invalidateQueries({ queryKey: ["review"] });
    },
    onError: toastError,
  });

  // 规则建议：本地只记"已确认（显示一行已生效）"和"已拒绝（不再渲染）"，真值以后端为准
  const rulesQ = useQuery({ queryKey: ["agent-rules"], queryFn: rulesApi.list, staleTime: 15000 });
  const [done, setDone] = useState<ReadonlyMap<number, string>>(new Map());
  const [gone, setGone] = useState<ReadonlySet<number>>(new Set());
  const suggestions = useMemo(() => {
    const seen = new Set<number>();
    return (rulesQ.data ?? []).filter((r) => {
      if (r.status !== "suggested" || gone.has(r.id) || done.has(r.id) || seen.has(r.id)) return false;
      seen.add(r.id);
      return true;
    });
  }, [rulesQ.data, gone, done]);
  // 卡片锚点：最后一条助手回复的正下方；没有助手消息时落在列表末尾
  const anchor = useMemo(() => {
    for (let i = list.length - 1; i >= 0; i--) if (list[i].role === "assistant") return i;
    return -1;
  }, [list]);

  // 追问卡片。一次只一张（最新那条）；锚在触发它的那轮对话下面：
  // chat_message_id 指向用户消息时，挪到它之后的第一条助手回复下面；找不到（未加载 / 已清理）就落在末尾。
  const ask = useChatAsk(() => go("review"));
  const askAt = useMemo(() => {
    const cid = ask.item?.chat_message_id;
    if (cid == null) return -1;
    let i = list.findIndex((m) => m.id === cid);
    if (i >= 0 && list[i].role === "user") {
      const j = list.findIndex((m, k) => k > i && m.role === "assistant");
      if (j >= 0) i = j;
    }
    return i;
  }, [list, ask.item]);

  // 会话中：发送后先本地回显一句（pending），同时后端会把这条消息入库并通过 WS 推事件，
  // 触发一次重取 —— 这时服务器数据里已经有同一句了，再显示 pending 就会看到两条一样的。
  // 所以只要列表里出现了同样的用户消息，就不再画 pending 气泡（打字点保留到请求结束）。
  const echoed = pending !== null && list.some((m) => m.role === "user" && m.text === pending);

  // 进页面要直接停在最新一条：首次（数据刚到时）瞬间跳到底，之后新消息才平滑滚动，
  // 否则长对话会看到"从第一条慢慢滚下来"。
  const prevLen = useRef(-1);
  const inited = useRef(false);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const grew = list.length > prevLen.current || pending !== null;
    prevLen.current = list.length;
    if (!grew) return;
    el.scrollTo({ top: el.scrollHeight, behavior: inited.current ? "smooth" : "auto" });
    if (list.length > 0) inited.current = true;
  }, [list.length, pending]);

  // 画像提议通常比回复晚一拍到（提议列表单独重取）：待确认数变多时再滚到底，免得卡片落在视口外

  // 追问卡片同理：出现了更新的一条才滚；处理完一条、旧的一条顶上来时不打扰
  const askId = ask.item?.id ?? null;
  const prevAsk = useRef<number>(-1);
  useEffect(() => {
    const el = scroller.current;
    const newer = askId !== null && askId > prevAsk.current;
    if (askId !== null) prevAsk.current = Math.max(prevAsk.current, askId);
    if (!el || !newer || !inited.current) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [askId]);

  const submit = () => {
    const t = text.trim();
    if (!t || send.isPending) return;
    if (t.length > 2000) return toastError(new Error("一次最多 2000 字"));
    setText("");
    send.mutate(t);
  };

  const ruleBlock = (suggestions.length > 0 || done.size > 0) && (
    <>
      {suggestions.map((r) => (
        <RuleSuggestCard
          key={r.id}
          rule={r}
          onConfirmed={(rule) => setDone((m) => new Map(m).set(rule.id, ruleText(rule)))}
          onRejected={(id) => setGone((s) => new Set(s).add(id))}
        />
      ))}
      {[...done.entries()].map(([id, t]) => (
        <div key={`done-${id}`} className="self-start flex items-center gap-1.5 px-1 text-meta text-ok q-in">
          <IconCheck size={14} strokeWidth={3} />
          已生效 · {t}
        </div>
      ))}
    </>
  );

  return (
    <div className="flex flex-col h-[calc(100dvh-140px)] md:h-[calc(100dvh-48px)] max-w-[760px] mx-auto">
      <div className="flex items-center gap-2 mb-3 px-1">
        <h1 className="text-h2 q-display">助手</h1>
        <Help>
          可以问"这周还有啥没做"，或者告诉它你的偏好。说出的偏好会变成一条建议规则，<b>你点确认后才生效</b>，可随时在设置里管理。AI
          拿不准的消息也会在这里主动问你。
        </Help>
      </div>
      <div ref={scroller} className="flex-1 overflow-y-auto q-card q-pad flex flex-col gap-3">
        {q.hasNextPage && (
          <button className="self-center text-cap text-ink-3 h-8" onClick={() => q.fetchNextPage()}>
            更早的对话
          </button>
        )}
        {list.length === 0 && !pending && !ask.item && (
          <div className="flex-1 flex flex-col items-center justify-center text-center gap-3 py-10">
            <span className="w-12 h-12 rounded-card bg-brand-soft text-brand flex items-center justify-center">
              <IconSpark />
            </span>
            <div className="text-title q-title">问点什么</div>
            <div className="flex flex-wrap justify-center gap-2 mt-1">
              {SUGGEST.map((s) => (
                <button key={s} onClick={() => setText(s)} className="h-9 px-3 rounded-full bg-surface-2 text-meta text-ink-2 hover:text-ink">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {list.map((m, i) => (
          <Fragment key={m.id}>
            <Bubble m={m} onReview={() => go("review")} card={confirms.byMessage.get(m.id)} onCardDone={confirms.markResolved} />
            {i === anchor && ruleBlock}
            {i === askAt && ask.node}
          </Fragment>
        ))}
        {anchor < 0 && ruleBlock}
        {askAt < 0 && ask.node}
        {pending && (
          <>
            {!echoed && (
              <Bubble m={{ id: -1, role: "user", kind: "text", text: pending, created_at: new Date().toISOString() }} />
            )}
            <div className="self-start flex gap-1 px-4 py-3 rounded-card bg-surface-2">
              {[0, 1, 2].map((i) => (
                <span key={i} className="w-1.5 h-1.5 rounded-full bg-ink-3 q-pulse" style={{ animationDelay: `${i * 0.2}s` }} />
              ))}
            </div>
          </>
        )}
      </div>
      <form
        className="mt-3 flex gap-2 items-end"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="和助手说点什么…"
          className="flex-1 min-h-12 max-h-32 px-4 py-3 rounded-card bg-surface border border-line text-body outline-none focus:border-brand resize-none"
        />
        <Button tone="primary" size="lg" type="submit" disabled={!text.trim()} loading={send.isPending} aria-label="发送" className="w-12 px-0">
          <IconSend size={18} />
        </Button>
      </form>
    </div>
  );
}

function Bubble({
  m,
  onReview,
  card,
  onCardDone,
}: {
  m: ChatMessage;
  onReview?: () => void;
  card?: ChatConfirm;
  onCardDone?: (id: number) => void;
}) {
  const me = m.role === "user";
  if (m.kind === "notice")
    return <div className="self-center text-cap text-ink-3 bg-surface-2 px-3 py-1 rounded-full max-w-[90%] text-center">{m.text}</div>;
  return (
    <div className={cx("flex flex-col max-w-[85%] min-w-0 q-in", me ? "self-end items-end" : "self-start items-start")}>
      <div
        className={cx(
          "px-4 py-2.5 text-body whitespace-pre-wrap break-words q-pretty",
          me ? "bg-brand text-on-brand rounded-card rounded-br-[6px]" : "bg-surface-2 text-ink rounded-card rounded-bl-[6px]",
          m.kind === "question" && "border border-warn/40 bg-warn-soft",
        )}
      >
        {m.text}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1.5 px-1 max-w-full min-w-0">
        <span className="text-cap text-ink-3 q-num">{hm(new Date(m.created_at))}</span>
        {m.kind === "question" && onReview && (
          <button onClick={onReview} className="inline-flex items-center gap-1 text-cap text-warn font-medium h-7">
            <IconInbox size={13} />
            去待确认处理
          </button>
        )}
        {!me && card && onCardDone && <ConfirmChoices card={card} onDone={onCardDone} />}
      </div>
    </div>
  );
}
