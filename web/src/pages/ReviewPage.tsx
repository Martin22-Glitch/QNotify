// 待确认：AI 拿不准的那几条。每张卡直接展开"AI 建议"表单（已预填，可改），一键确认或忽略。
// 处理完不凭空消失：底部"刚处理"区显示结果，可跳去待办/消息查看。
// 来自对话（source=chat）的条目带「来自对话」标签，确认时必须有日期。
import { useEffect, useState } from "react";
import { useResolveReview, useReview } from "../lib/queries";
import { fromLocalInput, relDue, toLocalInput } from "../lib/time";
import type { ReviewItem } from "../lib/types";
import { Confidence, QuoteBlock, useFresh } from "../components/bits";
import { IconCheck, IconEyeOff, IconSpark, IconUndo } from "../components/icons";
import { Button, cx, Empty, Field, Help, Input, SectionHead, Skeleton, Tag, Textarea } from "../components/ui";
import { useWide } from "../lib/hooks";

interface Done {
  id: number;
  title: string;
  action: "confirm" | "ignore";
  group: string | null;
  /** 来自对话：忽略后没有"消息"可改判，不给跳转 */
  chat?: boolean;
}

export function ReviewPage({ go }: { go: (t: "todos" | "messages") => void }) {
  const q = useReview();
  const wide = useWide();
  const [done, setDone] = useState<Done[]>([]);
  const items = q.data ?? [];

  return (
    <div className={cx("mx-auto", wide ? "max-w-[880px]" : "")}>
      <div className="flex items-center gap-2 mb-4 px-1">
        <h1 className="text-h2 q-display">待确认</h1>
        <span className="q-num text-h2 text-warn q-display">{items.length || ""}</span>
        <Help>
          AI 对这些消息拿不准要不要建待办。每条都已经按 AI 的理解填好了标题和截止——这只是<b>建议</b>，你可以直接改，也可以清空自己写。确认后进入待办；忽略后它会出现在消息页的「已忽略」里，之后仍能改判。你的每次选择都会记成学习样本。标着「来自对话」的是你在助手里说的事，要选个日期才能记成待办。
        </Help>
      </div>

      {q.isLoading ? (
        <div className="flex flex-col q-gap">
          <Skeleton className="h-64" />
          <Skeleton className="h-64" />
        </div>
      ) : items.length === 0 ? (
        <div className="q-card">
          <Empty icon={<IconCheck />} tone="ok" title="都处理完了" sub="AI 拿不准的消息会出现在这里，等你一句话决定" />
        </div>
      ) : (
        <div className={cx("grid q-gap", wide && items.length > 1 ? "grid-cols-2" : "grid-cols-1")}>
          {items.map((it) => (
            <ReviewCard
              key={it.id}
              item={it}
              onResolved={(d) => setDone((l) => [d, ...l].slice(0, 6))}
            />
          ))}
        </div>
      )}

      {done.length > 0 && (
        <section className="mt-8">
          <SectionHead title="刚处理" count={done.length} />
          <div className="q-card divide-y divide-line">
            {done.map((d) => (
              <div key={d.id} className="flex items-center gap-3 px-4 h-14 q-in">
                {d.action === "confirm" ? (
                  <Tag tone="brand">
                    <IconCheck size={12} />
                    已转待办
                  </Tag>
                ) : (
                  <Tag tone="muted">
                    <IconEyeOff size={12} />
                    已忽略
                  </Tag>
                )}
                <span className="flex-1 min-w-0 text-meta truncate">{d.title}</span>
                {!(d.chat && d.action === "ignore") && (
                  <button className="text-cap text-brand font-medium h-9 px-2 shrink-0" onClick={() => go(d.action === "confirm" ? "todos" : "messages")}>
                    {d.action === "confirm" ? "去待办" : "在消息里改判"}
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function ReviewCard({ item, onResolved }: { item: ReviewItem; onResolved: (d: Done) => void }) {
  const s = item.suggestion ?? null;
  const fresh = useFresh(`review:${item.id}`);
  const resolve = useResolveReview();
  const chat = item.source === "chat";
  const timeText = item.time_text?.trim() || "";
  const initTitle = s?.title ?? "";
  // 对话条目也只用后端建议时间预填；不塞默认值，否则"没选日期就禁用"永远不会出现
  const initDue = toLocalInput(s?.due_at ?? null);
  const initDetail = s?.detail ?? "";
  const [title, setTitle] = useState(initTitle);
  const [due, setDue] = useState(initDue);
  const [detail, setDetail] = useState(initDetail);
  const [showDetail, setShowDetail] = useState(!!initDetail);
  useEffect(() => {
    setTitle(initTitle);
    setDue(initDue);
    setDetail(initDetail);
  }, [item.id]); // eslint-disable-line

  const edited = title !== initTitle || due !== initDue || detail !== initDetail;
  const preview = item.preview;
  const fallbackTitle = (preview?.text ?? item.evidence ?? "").slice(0, 30);
  const conf = s?.confidence ?? item.confidence;
  const needDue = chat && !due;
  const needDueId = `review-${item.id}-need-due`;
  // 对话条目：问题文案由后端按 ask_reason 给，优先用 question
  const why = chat ? item.question || s?.reason : s?.reason || item.question;

  const confirm = () => {
    if (needDue) return;
    const t = title.trim() || fallbackTitle;
    resolve.mutate(
      { item, action: "confirm", title: t, due_at: fromLocalInput(due), detail: detail || undefined },
      { onSuccess: () => onResolved({ id: item.id, title: t, action: "confirm", group: preview?.group_name ?? null, chat }) },
    );
  };
  const ignore = () =>
    resolve.mutate(
      { item, action: "ignore" },
      {
        onSuccess: () =>
          onResolved({ id: item.id, title: preview?.text?.slice(0, 40) ?? item.evidence, action: "ignore", group: preview?.group_name ?? null, chat }),
      },
    );

  // 只有对话条目才显式传 disabled：Button 内部 {...rest} 会覆盖 loading 推出的 disabled，
  // 给其它条目传 undefined 会让它们在提交中也能再点 —— 那就改变了原有行为
  const confirmGuard = chat ? { disabled: needDue || resolve.isPending, "aria-describedby": needDue ? needDueId : undefined } : {};

  return (
    <article className={cx("q-card q-pad flex flex-col gap-4", fresh && "q-fresh")}>
      {chat && (
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <Tag tone="brand">来自对话</Tag>
          {timeText && <span className="min-w-0 text-meta text-ink-2 break-words">你说的是「{timeText}」</span>}
        </div>
      )}

      {/* 原话 */}
      <QuoteBlock
        text={preview?.text ?? item.evidence}
        who={chat ? preview?.sender_name ?? "你" : preview?.sender_name}
        where={chat ? "助手对话" : preview?.group_name ?? "私聊"}
      />

      {/* AI 为什么拿不准 */}
      <div className="flex items-start gap-2">
        <span className="w-6 h-6 rounded-full bg-warn-soft text-warn flex items-center justify-center shrink-0 mt-px">?</span>
        <div className="flex-1 min-w-0 text-meta text-ink-2 q-pretty">{why}</div>
        <Confidence v={conf} className="shrink-0 mt-0.5" />
      </div>

      {/* AI 建议（预填、可改） */}
      <div className="rounded-ctl border border-dashed border-brand/40 bg-brand-soft/40 p-3 flex flex-col gap-3">
        <div className="flex items-center gap-1.5 text-cap font-semibold text-brand">
          <IconSpark size={14} />
          {s ? "AI 建议 · 可直接改" : "AI 没给出建议 · 请手填"}
          <span className="flex-1" />
          {edited && s && (
            <button
              className="inline-flex items-center gap-1 text-ink-3 font-medium h-7 px-1.5 rounded hover:text-ink"
              onClick={() => {
                setTitle(initTitle);
                setDue(initDue);
                setDetail(initDetail);
              }}
            >
              <IconUndo size={12} />
              用回建议
            </button>
          )}
          {edited && <Tag tone="brand">已修改</Tag>}
        </div>
        <Field label="标题">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={fallbackTitle || "要做什么"} className="bg-surface" />
        </Field>
        <Field label={chat ? "截止 · 必填" : "截止"}>
          <div className="flex gap-2 items-center">
            <Input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} className="bg-surface flex-1 min-w-0" />
            {due && (
              <span className="q-num text-cap text-ink-2 shrink-0 hidden sm:inline">{relDue(fromLocalInput(due))}</span>
            )}
          </div>
        </Field>
        {showDetail ? (
          <Field label="详情">
            <Textarea rows={2} value={detail} onChange={(e) => setDetail(e.target.value)} className="bg-surface" />
          </Field>
        ) : (
          <button className="self-start text-cap text-ink-3 hover:text-ink-2 h-7" onClick={() => setShowDetail(true)}>
            + 添加详情
          </button>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex gap-2">
          <Button
            tone="primary"
            size="lg"
            className="flex-1"
            onClick={confirm}
            loading={resolve.isPending && resolve.variables?.action === "confirm"}
            {...confirmGuard}
          >
            <IconCheck size={18} />
            转为待办
          </Button>
          <Button tone="soft" size="lg" onClick={ignore} loading={resolve.isPending && resolve.variables?.action === "ignore"}>
            <IconEyeOff size={18} />
            忽略
          </Button>
        </div>
        {needDue && (
          <p id={needDueId} className="px-1 text-cap text-warn">
            选个日期才能记成待办
          </p>
        )}
      </div>
    </article>
  );
}
