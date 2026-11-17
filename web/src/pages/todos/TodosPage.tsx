import { useMemo, useState } from "react";
import { useNow, useWide } from "../../lib/hooks";
import { useAllTodos, useReview } from "../../lib/queries";
import { prefsStore } from "../../lib/prefs";
import { useStore } from "../../lib/store";
import { toast } from "../../lib/toast";
import { dayKey, dayName } from "../../lib/time";
import type { Todo, TodoStatus } from "../../lib/types";
import { IconArrow, IconCheck, IconEyeOff, IconInbox, IconTodo } from "../../components/icons";
import { Button, Empty, Segmented, SectionHead, Sheet, Skeleton, type Tone } from "../../components/ui";
import { useDueIndex, WeekStrip } from "./DatePicker";
import { TodoCard } from "./TodoCard";
import { TodoDetail } from "./TodoDetail";
import { TodoQuickAdd } from "./TodoQuickAdd";
import { bucketize, NextUp, TodayHero } from "./TodayHero";

export function TodosPage({ go }: { go: (tab: "review" | "settings") => void }) {
  const now = useNow();
  const wide = useWide();
  const prefs = useStore(prefsStore);
  const all = useAllTodos();
  const review = useReview();
  const [status, setStatus] = useState<TodoStatus>("open");
  const [day, setDay] = useState<string | null>(null);
  const [selId, setSelId] = useState<number | null>(null);

  const list = status === "open" ? all.open : status === "done" ? all.done : all.dropped;
  const b = useMemo(() => bucketize(all.open, now), [all.open, now]);
  const dueIndex = useDueIndex(all.open);
  const selected = selId !== null ? [...all.open, ...all.done, ...all.dropped].find((t) => t.id === selId) ?? null : null;
  const reviewN = review.data?.length ?? 0;

  const open = (t: Todo) => setSelId(t.id);

  // 新条目不在当前日期筛选里 → 回到"全部日期"，保证加完就能在列表里看到
  const onCreated = (t: Todo) => {
    const k = t.due_at ? dayKey(new Date(t.due_at)) : null;
    if (day && k !== day) setDay(null);
    toast("已添加", { label: "查看", run: () => setSelId(t.id) });
  };

  const dayList = useMemo(() => {
    if (!day) return null;
    return list
      .filter((t) => t.due_at && dayKey(new Date(t.due_at)) === day)
      .sort((a, z) => new Date(a.due_at!).getTime() - new Date(z.due_at!).getTime());
  }, [day, list]);

  const groups: { key: string; title: string; tone?: Tone; items: Todo[] }[] =
    status === "open"
      ? [
          { key: "overdue", title: "已逾期", tone: "danger", items: b.overdue },
          { key: "today", title: "今天", tone: "urgent", items: b.today },
          { key: "tomorrow", title: "明天", tone: "warn", items: b.tomorrow },
          { key: "week", title: "本周", items: b.week },
          { key: "later", title: "以后", items: b.later },
          { key: "none", title: "无截止", items: b.none },
        ]
      : [{ key: "all", title: status === "done" ? "已完成" : "不用做", items: list }];

  const countdown = prefs.urgency === "countdown";

  const listCol = (
    <div className="flex flex-col q-gap min-w-0">
      {status === "open" && !day && <TodayHero b={b} mode={prefs.urgency} onOpen={open} />}

      {reviewN > 0 && status === "open" && (
        <button onClick={() => go("review")} className="q-card flex items-center gap-3 px-4 h-12 text-left hover:bg-surface-2 transition">
          <span className="w-7 h-7 rounded-full bg-warn-soft text-warn flex items-center justify-center shrink-0">
            <IconInbox size={16} />
          </span>
          <span className="flex-1 text-meta">
            AI 有 <b className="q-num text-warn">{reviewN}</b> 条拿不准，等你决定
          </span>
          <IconArrow size={16} className="text-ink-3" />
        </button>
      )}

      <WeekStrip value={day} onChange={setDay} index={dueIndex} />

      <Segmented
        value={status}
        onChange={(v) => setStatus(v)}
        items={[
          { value: "open", label: "未完成", count: all.open.length },
          { value: "done", label: "已完成", count: all.done.length, tone: "ok" },
          { value: "dropped", label: "不用做", count: all.dropped.length, tone: "muted" },
        ]}
      />

      {/* 列表头的手动添加。切到已完成/不用做时只隐藏不卸载，填了一半的内容不会丢 */}
      <div className={status === "open" ? undefined : "hidden"}>
        <TodoQuickAdd day={day} index={dueIndex} onCreated={onCreated} />
      </div>

      <div className="mt-1">
        {all.loading ? (
          <div className="flex flex-col q-gap">
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
          </div>
        ) : dayList ? (
          <>
            <SectionHead
              title={`${dayName(new Date(day + "T12:00:00"), now)} · ${Number(day!.slice(5, 7))}月${Number(day!.slice(8))}日`}
              count={dayList.length}
              tone={dayName(new Date(day + "T12:00:00"), now) === "今天" ? "urgent" : dayName(new Date(day + "T12:00:00"), now) === "明天" ? "warn" : undefined}
              right={
                <button className="text-cap text-brand font-medium h-8 px-2" onClick={() => setDay(null)}>
                  查看全部
                </button>
              }
            />
            {dayList.length ? (
              <div className="flex flex-col q-gap">
                {dayList.map((t) => (
                  <TodoCard key={t.id} todo={t} onOpen={() => open(t)} selected={wide && selId === t.id} countdown={countdown} />
                ))}
              </div>
            ) : (
              <Empty icon={<IconCheck />} title="这一天没有截止的事" tone="ok" />
            )}
          </>
        ) : list.length === 0 ? (
          <EmptyFor status={status} reviewN={reviewN} go={go} />
        ) : (
          groups
            .filter((g) => g.items.length)
            .map((g) => (
              <section key={g.key}>
                <SectionHead title={g.title} count={g.items.length} tone={g.tone} />
                <div className="flex flex-col q-gap">
                  {g.items.map((t) => (
                    <TodoCard key={t.id} todo={t} onOpen={() => open(t)} selected={wide && selId === t.id} countdown={countdown} />
                  ))}
                </div>
              </section>
            ))
        )}
      </div>
    </div>
  );

  if (wide) {
    return (
      <div className="grid grid-cols-[minmax(0,1fr)_400px] gap-6 items-start">
        {listCol}
        <aside className="sticky top-6 q-card q-pad max-h-[calc(100dvh-48px)] overflow-y-auto overflow-x-hidden">
          {selected ? (
            <>
              <div className="flex items-center mb-4">
                <div className="flex-1 text-meta text-ink-3">待办详情</div>
                <button className="text-cap text-ink-3 hover:text-ink h-8 px-2" onClick={() => setSelId(null)}>
                  收起
                </button>
              </div>
              <TodoDetail todo={selected} />
            </>
          ) : (
            <div>
              <div className="text-title q-title mb-1">接下来</div>
              <div className="text-meta text-ink-3 mb-3">点任一条看原话、改期或标记</div>
              <NextUp b={b} onOpen={open} />
              {!b.overdue.length && !b.today.length && !b.tomorrow.length && (
                <div className="text-meta text-ink-3 py-6 text-center">48 小时内没有截止的事</div>
              )}
            </div>
          )}
        </aside>
      </div>
    );
  }

  return (
    <>
      {listCol}
      <Sheet open={!!selected} onClose={() => setSelId(null)} title="待办详情">
        {selected && <TodoDetail todo={selected} />}
      </Sheet>
    </>
  );
}

function EmptyFor({ status, reviewN, go }: { status: TodoStatus; reviewN: number; go: (t: "review" | "settings") => void }) {
  if (status === "done") return <Empty icon={<IconCheck />} tone="ok" title="还没有完成的待办" sub="勾掉的待办会出现在这里" />;
  if (status === "dropped")
    return <Empty icon={<IconEyeOff />} tone="muted" title="没有标为不用做的" sub="左滑待办或在详情里选「不用做」，之后可随时恢复" />;
  return (
    <Empty
      icon={<IconTodo />}
      tone="ok"
      title="现在没有要做的事"
      sub={reviewN ? "不过 AI 还有几条拿不准，去看看？" : "群里来了要动手的通知会自动出现在这里，也可以在上面自己添一条"}
      action={
        reviewN ? (
          <Button tone="primary" onClick={() => go("review")}>
            去待确认
          </Button>
        ) : (
          <Button tone="soft" onClick={() => go("settings")}>
            检查接入状态
          </Button>
        )
      }
    />
  );
}
