// 业务小部件：紧迫度色、来源行、原句引用、置信度、判定标签。
import type { ReactNode } from "react";
import { useStore } from "../lib/store";
import { rtStore } from "../lib/realtime";
import { msgTime, relDue, urgencyOf, type Urgency } from "../lib/time";
import type { TodoSource } from "../lib/types";
import { IconChat, IconCheck, IconEdit, IconEyeOff, IconInbox, IconQuote, IconTodo, IconUsers } from "./icons";
import { cx, Tag, TONE_TEXT, type Tone } from "./ui";

export const URG_TONE: Record<Urgency, Tone> = {
  overdue: "danger",
  now: "urgent",
  today: "urgent",
  tomorrow: "warn",
  week: "brand",
  later: "muted",
  none: "muted",
};

export function DueText({ due, className, strong }: { due: string | null; className?: string; strong?: boolean }) {
  const u = urgencyOf(due);
  return (
    <span className={cx("q-num whitespace-nowrap", TONE_TEXT[URG_TONE[u]], strong && "font-semibold", className)}>
      {relDue(due)}
    </span>
  );
}

export function SourceLine({ s, origin, className }: { s?: TodoSource | null; origin?: string | null; className?: string }) {
  if (!s) {
    // 没有原话来源时，用一句短标签说明它是怎么来的，别显示"来源未知"
    const label =
      origin === "chat" ? "来自对话" : origin === "manual" ? "手动添加" : origin === "review" ? "来自待确认" : "来源未知";
    const Icon = origin === "chat" ? IconChat : origin === "manual" ? IconEdit : IconUsers;
    return (
      <span className={cx("inline-flex items-center gap-1 text-cap text-ink-3 min-w-0", className)}>
        <Icon size={13} className="shrink-0" />
        <span className="truncate">{label}</span>
      </span>
    );
  }
  return (
    <span className={cx("inline-flex items-center gap-1 text-cap text-ink-3 min-w-0", className)}>
      <IconUsers size={13} className="shrink-0" />
      <span className="truncate">{s.group_name ?? "私聊"}</span>
      {s.sender_name && <span className="shrink-0">· {s.sender_name}</span>}
    </span>
  );
}

export function QuoteBlock({ text, who, where, time, children }: { text: string; who?: string | null; where?: string | null; time?: number; children?: ReactNode }) {
  return (
    <figure className="rounded-ctl bg-surface-2 p-3">
      <div className="flex gap-2">
        <IconQuote size={16} className="text-ink-3 shrink-0 mt-0.5" />
        <blockquote className="text-body text-ink q-pretty whitespace-pre-wrap break-words min-w-0">{text}</blockquote>
      </div>
      <figcaption className="mt-2 pl-6 flex flex-wrap items-center gap-x-2 gap-y-1 text-cap text-ink-3">
        {who && <span className="font-medium text-ink-2">{who}</span>}
        {where && <span>{where}</span>}
        {time !== undefined && <span className="q-num">{msgTime(time)}</span>}
        {children}
      </figcaption>
    </figure>
  );
}

/** 置信度：5 格条 + 百分比；低于 0.5 用警告色 */
export function Confidence({ v, className }: { v: number | null | undefined; className?: string }) {
  if (v === null || v === undefined) return null;
  const n = Math.round(v * 5);
  const tone = v < 0.5 ? "bg-warn" : v < 0.75 ? "bg-brand" : "bg-ok";
  return (
    <span className={cx("inline-flex items-center gap-1.5", className)} title={`AI 置信度 ${Math.round(v * 100)}%`}>
      <span className="flex gap-[2px]">
        {[0, 1, 2, 3, 4].map((i) => (
          <span key={i} className={cx("w-[5px] h-2.5 rounded-[1.5px]", i < n ? tone : "bg-line")} />
        ))}
      </span>
      <span className="q-num text-cap text-ink-3">{Math.round(v * 100)}%</span>
    </span>
  );
}

export type VClass = "todo" | "review" | "ignored" | "none";
export const VERDICT_META: Record<VClass, { label: string; tone: Tone; icon: ReactNode }> = {
  todo: { label: "转待办", tone: "brand", icon: <IconTodo size={13} /> },
  review: { label: "待决定", tone: "warn", icon: <IconInbox size={13} /> },
  ignored: { label: "已忽略", tone: "muted", icon: <IconEyeOff size={13} /> },
  none: { label: "未判定", tone: "muted", icon: null },
};

export function VerdictTag({ cls, manual, onClick }: { cls: VClass; manual?: boolean; onClick?: () => void }) {
  const m = VERDICT_META[cls];
  const body = (
    <Tag tone={m.tone} className={cx(onClick && "pr-1.5")}>
      {m.icon}
      {m.label}
      {manual && <span className="opacity-70">· 人工</span>}
      {onClick && <span className="ml-0.5 opacity-60">▾</span>}
    </Tag>
  );
  if (!onClick) return body;
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="rounded-full -m-1 p-1"
      aria-label={`判定：${m.label}，点按改判`}
    >
      {body}
    </button>
  );
}

export function CheckCircle({ checked, onClick, tone = "brand", label }: { checked: boolean; onClick: () => void; tone?: Tone; label: string }) {
  return (
    <button
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="w-10 h-10 -m-2 flex items-center justify-center shrink-0 group"
    >
      <span
        className={cx(
          "w-[22px] h-[22px] rounded-full border-2 flex items-center justify-center transition",
          checked ? "bg-ok border-ok text-white" : cx("border-current", TONE_TEXT[tone], "group-hover:bg-surface-2"),
        )}
      >
        {checked && <IconCheck size={14} strokeWidth={3} />}
      </span>
    </button>
  );
}

/** 新到条目闪一下 */
export function useFresh(key: string): boolean {
  const fresh = useStore(rtStore).fresh[key];
  return !!fresh && Date.now() - fresh < 4000;
}
