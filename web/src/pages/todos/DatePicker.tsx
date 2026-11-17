// 日期选择：常驻顶部的横滑周历条 + 点开展开的月历。每天下方的色点 = 当天截止的未完成待办（颜色即紧迫度）。
import { useEffect, useMemo, useRef, useState } from "react";
import { useNow } from "../../lib/hooks";
import { addDays, dayKey, sameDay, startOfDay, WEEK } from "../../lib/time";
import type { Todo } from "../../lib/types";
import { IconCal, IconLeft, IconRight } from "../../components/icons";
import { cx, IconButton, Sheet } from "../../components/ui";

export function useDueIndex(todos: Todo[]): Map<string, Todo[]> {
  return useMemo(() => {
    const m = new Map<string, Todo[]>();
    for (const t of todos) {
      if (!t.due_at) continue;
      const k = dayKey(new Date(t.due_at));
      const a = m.get(k) ?? [];
      a.push(t);
      m.set(k, a);
    }
    return m;
  }, [todos]);
}

function dotTone(d: Date, now: number): string {
  const diff = Math.round((startOfDay(d).getTime() - startOfDay(now).getTime()) / 86400000);
  if (diff < 0) return "bg-danger";
  if (diff === 0) return "bg-urgent";
  if (diff === 1) return "bg-warn";
  return "bg-brand";
}

export function WeekStrip({
  value,
  onChange,
  index,
}: {
  value: string | null;
  onChange: (k: string | null) => void;
  index: Map<string, Todo[]>;
}) {
  const now = useNow(60000);
  const today = startOfDay(now);
  const days = useMemo(() => Array.from({ length: 36 }, (_, i) => addDays(today, i - 2)), [today.getTime()]); // eslint-disable-line
  const scroller = useRef<HTMLDivElement>(null);
  const [calOpen, setCalOpen] = useState(false);

  // 首次把"昨天"对齐到最左，保证今天/明天一定在视野里
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      const s = scroller.current;
      const el = s?.querySelector<HTMLElement>("[data-today]");
      if (s && el) s.scrollLeft = el.offsetLeft - s.offsetLeft - el.offsetWidth - 6;
    });
    return () => cancelAnimationFrame(id);
  }, []);
  // 选中某天时确保它可见
  useEffect(() => {
    if (!value || !scroller.current) return;
    const el = scroller.current.querySelector<HTMLElement>(`[data-k="${value}"]`);
    if (!el) return;
    const s = scroller.current;
    if (el.offsetLeft < s.scrollLeft || el.offsetLeft + el.offsetWidth > s.scrollLeft + s.clientWidth) {
      s.scrollTo({ left: el.offsetLeft - s.offsetLeft - 60, behavior: "smooth" });
    }
  }, [value]);

  return (
    <div className="flex items-stretch gap-1.5">
      <button
        onClick={() => onChange(null)}
        className={cx(
          "shrink-0 w-14 rounded-ctl flex flex-col items-center justify-center gap-0.5 text-meta font-medium transition",
          value === null ? "bg-ink text-bg" : "bg-surface text-ink-2 hover:bg-surface-2 border border-line",
        )}
      >
        <span>全部</span>
        <span className="text-cap opacity-60">日期</span>
      </button>
      <div ref={scroller} className="flex-1 flex gap-1.5 q-scroll-x relative min-w-0 snap-x">
        {days.map((d) => {
          const k = dayKey(d);
          const isToday = sameDay(d, now);
          const isTomorrow = sameDay(d, addDays(today, 1));
          const sel = value === k;
          const n = index.get(k)?.length ?? 0;
          const past = d < today;
          return (
            <button
              key={k}
              data-k={k}
              data-today={isToday ? "" : undefined}
              onClick={() => onChange(sel ? null : k)}
              className={cx(
                "snap-start shrink-0 w-12 h-[64px] rounded-ctl flex flex-col items-center justify-center gap-0.5 transition relative",
                sel
                  ? "bg-brand text-on-brand"
                  : isToday
                    ? "bg-urgent-soft text-urgent"
                    : isTomorrow
                      ? "bg-warn-soft text-warn"
                      : "bg-surface text-ink hover:bg-surface-2",
                past && !sel && "opacity-55",
              )}
            >
              <span className="text-cap font-medium">{isToday ? "今天" : isTomorrow ? "明天" : `周${WEEK[d.getDay()]}`}</span>
              <span className="q-num text-title font-semibold leading-none">{d.getDate()}</span>
              <span className="h-1.5 flex gap-0.5 items-center">
                {Array.from({ length: Math.min(n, 4) }).map((_, i) => (
                  <span key={i} className={cx("w-1 h-1 rounded-full", sel ? "bg-on-brand" : dotTone(d, now))} />
                ))}
              </span>
            </button>
          );
        })}
      </div>
      <button
        onClick={() => setCalOpen(true)}
        aria-label="打开月历"
        className="shrink-0 w-11 rounded-ctl bg-surface border border-line text-ink-2 hover:bg-surface-2 flex items-center justify-center"
      >
        <IconCal />
      </button>
      <Sheet open={calOpen} onClose={() => setCalOpen(false)} title="选择日期">
        <MonthCal
          value={value}
          index={index}
          onPick={(k) => {
            onChange(k);
            setCalOpen(false);
          }}
        />
      </Sheet>
    </div>
  );
}

export function MonthCal({ value, onPick, index }: { value: string | null; onPick: (k: string) => void; index: Map<string, Todo[]> }) {
  const now = useNow(60000);
  const init = value ? new Date(value + "T00:00:00") : new Date(now);
  const [cursor, setCursor] = useState(() => new Date(init.getFullYear(), init.getMonth(), 1));
  const first = cursor.getDay();
  const daysIn = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0).getDate();
  const cells: (Date | null)[] = [
    ...Array.from({ length: first }, () => null),
    ...Array.from({ length: daysIn }, (_, i) => new Date(cursor.getFullYear(), cursor.getMonth(), i + 1)),
  ];
  return (
    <div className="pb-2">
      <div className="flex items-center mb-3">
        <div className="flex-1 text-title q-title q-num">
          {cursor.getFullYear()} 年 {cursor.getMonth() + 1} 月
        </div>
        <IconButton label="上个月" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}>
          <IconLeft />
        </IconButton>
        <IconButton label="下个月" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}>
          <IconRight />
        </IconButton>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center text-cap text-ink-3 mb-1">
        {WEEK.map((w) => (
          <div key={w}>{w}</div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((d, i) => {
          if (!d) return <div key={i} />;
          const k = dayKey(d);
          const n = index.get(k)?.length ?? 0;
          const isToday = sameDay(d, now);
          const sel = value === k;
          return (
            <button
              key={k}
              onClick={() => onPick(k)}
              className={cx(
                "aspect-square rounded-ctl flex flex-col items-center justify-center gap-1 transition",
                sel ? "bg-brand text-on-brand" : isToday ? "bg-urgent-soft text-urgent font-semibold" : "hover:bg-surface-2",
              )}
            >
              <span className="q-num text-body">{d.getDate()}</span>
              <span className="h-1.5 flex gap-0.5">
                {n > 0 && <span className={cx("w-1.5 h-1.5 rounded-full", sel ? "bg-on-brand" : dotTone(d, now))} />}
                {n > 1 && <span className={cx("w-1.5 h-1.5 rounded-full", sel ? "bg-on-brand" : dotTone(d, now))} />}
              </span>
            </button>
          );
        })}
      </div>
      <button onClick={() => onPick(dayKey(now))} className="mt-3 w-full h-10 rounded-ctl bg-surface-2 text-meta font-medium">
        回到今天
      </button>
    </div>
  );
}
