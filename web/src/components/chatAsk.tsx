// 助手页的追问卡片。
// 待确认里 source='chat' 且未处理的条目，一次只挂最新的一张：选日期 → 确认；日期还没定 → 本地收起；不用记 → 忽略。
// "日期还没定"不碰后端，只记在 localStorage；真值（是否还待确认）永远以 GET /api/review 为准。
import { useMemo, useState, type ReactNode } from "react";
import { useResolveReview, useReview } from "../lib/queries";
import { fromLocalInput, relDue, toLocalInput } from "../lib/time";
import type { ReviewItem } from "../lib/types";
import { IconCheck, IconInbox } from "./icons";
import { Button, Input } from "./ui";

/** 来自对话、还没处理 */
export function isChatAsk(it: ReviewItem): boolean {
  return it.source === "chat" && (!it.status || it.status === "pending");
}

/* ── "日期还没定"的收起态：localStorage ── */
const SNOOZE_KEY = "qtodo:chat-ask:snoozed";
/** 只留最近 50 条，不做按列表清理（切账号时列表会变，清理会误删另一个号的收起态） */
const SNOOZE_CAP = 50;
/** id + created_at：不同账号库的 id 可能撞号，带上创建时间基本不会误伤 */
const tokenOf = (it: ReviewItem) => `${it.id}@${it.created_at}`;

function readSnoozed(): string[] {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(SNOOZE_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}
function writeSnoozed(list: string[]) {
  try {
    localStorage.setItem(SNOOZE_KEY, JSON.stringify(list));
  } catch {
    /* 隐私模式 / 配额满：只影响刷新后的收起态 */
  }
}

/* ── 文案 ── */
export function askTitle(it: ReviewItem): string {
  const t = it.suggestion?.title?.trim();
  if (t) return t;
  const raw = (it.evidence || it.preview?.text || "").trim();
  if (!raw) return "这件事";
  return raw.length > 24 ? `${raw.slice(0, 24)}…` : raw;
}

export function askHint(it: ReviewItem): string {
  const parts: string[] = [];
  const t = it.time_text?.trim();
  if (t) parts.push(`你说的是「${t}」`);
  if (it.ask_reason === "no_request") parts.push("不确定你要不要记");
  else if (it.ask_reason === "no_date" || it.ask_reason === "both" || !it.suggestion?.due_at) parts.push("日期还没定");
  return parts.join(" · ");
}

/** 默认截止：后端给了未来的建议时间就用它；否则"今天之后的第一天"上午 9 点（time_text 是口语，前端不解析） */
function defaultDue(it: ReviewItem): string {
  const s = it.suggestion?.due_at;
  if (s && new Date(s).getTime() > Date.now()) return toLocalInput(s);
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return toLocalInput(d.toISOString());
}

const nowLocal = () => toLocalInput(new Date().toISOString());

interface Recorded {
  id: number;
  title: string;
  due_at: string;
}

/**
 * 返回当前要挂的那条（给页面算锚点）和要渲染的节点。
 * held：正在提交的那条。useResolveReview 会乐观地把它从列表里删掉，
 * 不 hold 住的话卡片会在请求途中消失、下一条顶上来。
 */
export function useChatAsk(onReview: () => void): { item: ReviewItem | null; node: ReactNode } {
  const review = useReview();
  const resolve = useResolveReview();
  const [held, setHeld] = useState<ReviewItem | null>(null);
  const [recorded, setRecorded] = useState<Recorded[]>([]);
  const [snoozed, setSnoozed] = useState<string[]>(readSnoozed);

  const queue = useMemo(
    () => (review.data ?? []).filter(isChatAsk).sort((a, b) => b.id - a.id),
    [review.data],
  );
  const item = held ?? queue[0] ?? null;
  const more = item ? queue.filter((x) => x.id !== item.id).length : 0;

  const setSnooze = (it: ReviewItem, on: boolean) => {
    const t = tokenOf(it);
    const rest = snoozed.filter((x) => x !== t);
    const next = on ? [...rest, t].slice(-SNOOZE_CAP) : rest;
    setSnoozed(next);
    writeSnoozed(next);
  };

  const confirm = (it: ReviewItem, due_at: string) => {
    const title = askTitle(it);
    setHeld(it);
    resolve.mutate(
      { item: it, action: "confirm", title, due_at, detail: it.suggestion?.detail || undefined },
      {
        onSuccess: () => {
          setRecorded((l) => [...l.filter((r) => r.id !== it.id), { id: it.id, title, due_at }].slice(-3));
          setSnooze(it, false);
        },
        onSettled: () => setHeld(null),
      },
    );
  };

  const ignore = (it: ReviewItem) => {
    setHeld(it);
    resolve.mutate(
      { item: it, action: "ignore" },
      { onSuccess: () => setSnooze(it, false), onSettled: () => setHeld(null) },
    );
  };

  const busy = item && held?.id === item.id ? resolve.variables?.action : undefined;

  const node =
    item || recorded.length > 0 ? (
      <>
        {recorded.map((r) => (
          <div key={`ask-done-${r.id}`} className="self-start max-w-full min-w-0 flex items-center gap-1.5 px-1 text-meta text-ok q-in">
            <IconCheck size={14} strokeWidth={3} />
            <span className="shrink-0 q-num">已记成待办 · {relDue(r.due_at)}</span>
            <span className="min-w-0 truncate text-ink-3">「{r.title}」</span>
          </div>
        ))}
        {item && (
          <ChatAskCard
            key={item.id}
            item={item}
            collapsed={snoozed.includes(tokenOf(item))}
            busy={busy}
            more={more}
            onConfirm={(due) => confirm(item, due)}
            onIgnore={() => ignore(item)}
            onSnooze={() => setSnooze(item, true)}
            onWake={() => setSnooze(item, false)}
            onReview={onReview}
          />
        )}
      </>
    ) : null;

  return { item, node };
}

function ChatAskCard({
  item,
  collapsed,
  busy,
  more,
  onConfirm,
  onIgnore,
  onSnooze,
  onWake,
  onReview,
}: {
  item: ReviewItem;
  collapsed: boolean;
  busy?: "confirm" | "ignore";
  more: number;
  onConfirm: (dueIso: string) => void;
  onIgnore: () => void;
  onSnooze: () => void;
  onWake: () => void;
  onReview: () => void;
}) {
  const [picking, setPicking] = useState(false);
  const [due, setDue] = useState("");
  const title = askTitle(item);
  const hint = askHint(item);
  const dueIso = due ? fromLocalInput(due) : null;

  const openPicker = () => {
    setDue((d) => d || defaultDue(item));
    setPicking(true);
  };

  const moreLink =
    more > 0 ? (
      <button type="button" onClick={onReview} className="self-start inline-flex items-center gap-1 h-7 text-cap text-ink-2 hover:text-ink">
        <IconInbox size={13} />
        还有 {more} 条来自对话的，在待确认里
      </button>
    ) : null;

  if (collapsed)
    return (
      <div className="self-stretch flex flex-col gap-1 q-in">
        <div className="flex items-center gap-2 min-w-0 h-11 pl-3 pr-1 rounded-card border border-line bg-surface-2 text-meta">
          <span className="w-2 h-2 rounded-full bg-warn shrink-0" aria-hidden />
          <span className="flex-1 min-w-0 truncate text-ink-2">
            等你定日期 · <span className="text-ink">{title}</span>
          </span>
          <button
            type="button"
            className="shrink-0 h-9 px-2 rounded-ctl text-cap font-medium text-brand hover:bg-surface"
            onClick={() => {
              onWake();
              openPicker();
            }}
          >
            现在定
          </button>
        </div>
        {moreLink}
      </div>
    );

  return (
    <div role="group" aria-label="助手追问" className="self-stretch flex flex-col gap-3 p-3 sm:p-4 rounded-card border border-warn/40 bg-warn-soft q-in">
      <div className="min-w-0">
        <div className="text-body font-medium text-ink q-pretty break-words">要把「{title}」记成待办吗？</div>
        {hint && <div className="mt-0.5 text-meta text-ink-2 q-pretty break-words">{hint}</div>}
      </div>

      {picking ? (
        <div className="flex flex-col gap-2">
          <Input
            type="datetime-local"
            aria-label="截止时间"
            value={due}
            min={nowLocal()}
            onChange={(e) => setDue(e.target.value)}
            className="bg-surface min-w-0"
          />
          {dueIso && <div className="text-cap text-ink-2 q-num">{relDue(dueIso)}</div>}
          <div className="flex flex-wrap gap-2">
            {/* 显式 disabled：Button 里 rest 会覆盖 loading 推出的 disabled，所以把 busy 一起算进来 */}
            <Button tone="primary" size="sm" disabled={!dueIso || !!busy} loading={busy === "confirm"} onClick={() => dueIso && onConfirm(dueIso)}>
              <IconCheck size={16} />
              记成待办
            </Button>
            <Button tone="ghost" size="sm" disabled={!!busy} onClick={() => setPicking(false)}>
              先不选
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button tone="primary" size="sm" disabled={!!busy} onClick={openPicker}>
            选日期
          </Button>
          <Button tone="plain" size="sm" disabled={!!busy} onClick={onSnooze}>
            日期还没定
          </Button>
          <Button tone="ghost" size="sm" disabled={!!busy} loading={busy === "ignore"} onClick={onIgnore}>
            不用记
          </Button>
        </div>
      )}

      {moreLink}
    </div>
  );
}
