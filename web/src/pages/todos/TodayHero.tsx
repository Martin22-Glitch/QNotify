// 今日焦点：两种紧迫感形态
//  A. focus     —— 大数字"今天 N 件" + 最急那件放大 + 明天/逾期计数
//  B. countdown —— 未来 48 小时时间轴，每件事按截止时刻落在轴上，越靠左越急
import { useNow } from "../../lib/hooks";
import { addDays, hm, relDue, shortLeft, startOfDay, urgencyOf } from "../../lib/time";
import type { Todo } from "../../lib/types";
import { SourceLine, URG_TONE } from "../../components/bits";
import { IconArrow, IconBolt } from "../../components/icons";
import { cx, TONE_BG } from "../../components/ui";

export interface Buckets {
  overdue: Todo[];
  today: Todo[];
  tomorrow: Todo[];
  week: Todo[];
  later: Todo[];
  none: Todo[];
}

export function bucketize(list: Todo[], now: number): Buckets {
  const b: Buckets = { overdue: [], today: [], tomorrow: [], week: [], later: [], none: [] };
  const sorted = [...list].sort((a, z) => {
    if (!a.due_at) return 1;
    if (!z.due_at) return -1;
    return new Date(a.due_at).getTime() - new Date(z.due_at).getTime();
  });
  for (const t of sorted) {
    const u = urgencyOf(t.due_at, now);
    if (u === "overdue") b.overdue.push(t);
    else if (u === "now" || u === "today") b.today.push(t);
    else if (u === "tomorrow") b.tomorrow.push(t);
    else if (u === "week") b.week.push(t);
    else if (u === "later") b.later.push(t);
    else b.none.push(t);
  }
  return b;
}

export function TodayHero({ b, mode, onOpen }: { b: Buckets; mode: "focus" | "countdown"; onOpen: (t: Todo) => void }) {
  return mode === "focus" ? <FocusHero b={b} onOpen={onOpen} /> : <TimelineHero b={b} onOpen={onOpen} />;
}

function FocusHero({ b, onOpen }: { b: Buckets; onOpen: (t: Todo) => void }) {
  const now = useNow();
  const top = b.overdue[0] ?? b.today[0] ?? b.tomorrow[0] ?? null;
  const n = b.today.length + b.overdue.length;
  const left = top ? shortLeft(top.due_at, now) : null;
  const d = new Date(now);
  return (
    <section className="q-hero q-pad pb-4" aria-label="今日概况">
      <div className="flex items-end gap-4 relative z-10">
        <div className="flex-1 min-w-0">
          <div className="text-meta q-hero-sub">
            {d.getMonth() + 1}月{d.getDate()}日 · 周{"日一二三四五六"[d.getDay()]}
          </div>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-meta font-semibold">今天</span>
            <span className="q-num text-display q-display">{n}</span>
            <span className="text-meta font-semibold">件要做</span>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5 shrink-0 pb-1">
          {b.overdue.length > 0 && (
            <span className="q-hero-chip h-6 px-2 rounded-full text-cap font-semibold inline-flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-danger" />
              已逾期 <span className="q-num">{b.overdue.length}</span>
            </span>
          )}
          <span className="q-hero-chip h-6 px-2 rounded-full text-cap font-medium inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-warn" />
            明天 <span className="q-num">{b.tomorrow.length}</span>
          </span>
        </div>
      </div>
      {top ? (
        <button onClick={() => onOpen(top)} className="q-hero-focus relative z-10 mt-3 w-full text-left rounded-ctl p-3 flex items-center gap-3 group">
          <span className={cx("w-9 h-9 rounded-full flex items-center justify-center shrink-0 text-white", TONE_BG[URG_TONE[urgencyOf(top.due_at, now)]])}>
            <IconBolt size={18} />
          </span>
          <span className="flex-1 min-w-0">
            <span className="block text-cap q-hero-sub">最急</span>
            <span className="block text-body font-semibold q-clamp-1">{top.title}</span>
          </span>
          {left && (
            <span className="text-right shrink-0 leading-none">
              <span className="q-num text-h2 font-bold">{left.v}</span>
              <span className="text-cap ml-0.5">{left.u}</span>
              <span className="block text-cap q-hero-sub mt-1">{top.due_at ? hm(new Date(top.due_at)) : ""}</span>
            </span>
          )}
        </button>
      ) : (
        <div className="q-hero-focus relative z-10 mt-3 rounded-ctl p-3 text-meta">今天和明天都没有截止的事，放心</div>
      )}
    </section>
  );
}

function TimelineHero({ b, onOpen }: { b: Buckets; onOpen: (t: Todo) => void }) {
  const now = useNow();
  const start = now;
  const end = startOfDay(addDays(new Date(now), 2)).getTime(); // 到后天 0 点
  const span = end - start;
  const items = [...b.today, ...b.tomorrow];
  const midnight = startOfDay(addDays(new Date(now), 1)).getTime();
  const n = b.today.length + b.overdue.length;
  const top = b.overdue[0] ?? b.today[0] ?? b.tomorrow[0];
  return (
    <section className="q-hero q-pad pb-5" aria-label="48 小时时间轴">
      <div className="flex items-baseline gap-2 relative z-10">
        <span className="q-num text-display q-display">{n}</span>
        <span className="text-meta font-semibold">件今天截止</span>
        <span className="flex-1" />
        <span className="text-cap q-hero-sub">
          明天 <span className="q-num font-semibold">{b.tomorrow.length}</span>
          {b.overdue.length > 0 && (
            <>
              {" · "}逾期 <span className="q-num font-semibold">{b.overdue.length}</span>
            </>
          )}
        </span>
      </div>
      {top && (
        <button onClick={() => onOpen(top)} className="relative z-10 mt-1 text-meta q-hero-sub flex items-center gap-1 hover:opacity-100">
          最急：<span className="font-semibold q-clamp-1 text-left">{top.title}</span> · <span className="q-num">{relDue(top.due_at, now)}</span>
          <IconArrow size={14} />
        </button>
      )}
      {/* 轴 */}
      <div className="relative z-10 mt-6 h-16">
        <div className="absolute left-0 right-0 top-7 h-[3px] rounded-full q-hero-chip" />
        <div className="absolute left-0 top-7 h-[3px] rounded-full bg-current opacity-70" style={{ width: `${((midnight - start) / span) * 100}%` }} />
        <div className="absolute top-3 bottom-0 border-l border-dashed border-current opacity-40" style={{ left: `${((midnight - start) / span) * 100}%` }} />
        <span className="absolute top-12 text-cap q-hero-sub" style={{ left: 0 }}>
          现在
        </span>
        <span className="absolute top-12 text-cap q-hero-sub -translate-x-1/2" style={{ left: `${((midnight - start) / span) * 100}%` }}>
          明天
        </span>
        <span className="absolute top-12 right-0 text-cap q-hero-sub">后天</span>
        {items.map((t, i) => {
          const at = new Date(t.due_at!).getTime();
          const x = Math.max(0, Math.min(1, (at - start) / span));
          const tone = TONE_BG[URG_TONE[urgencyOf(t.due_at, now)]];
          return (
            <button
              key={t.id}
              onClick={() => onOpen(t)}
              title={`${t.title} · ${relDue(t.due_at, now)}`}
              className="absolute -translate-x-1/2 flex flex-col items-center group"
              style={{ left: `${x * 100}%`, top: i % 2 ? 0 : 2 }}
            >
              <span className={cx("w-4 h-4 mt-[18px] rounded-full ring-[3px] ring-white/80 group-hover:scale-125 transition", tone)} />
            </button>
          );
        })}
      </div>
    </section>
  );
}

export function NextUp({ b, onOpen }: { b: Buckets; onOpen: (t: Todo) => void }) {
  // 桌面宽屏右栏空闲时的"接下来"提示（窄屏不显示）
  const list = [...b.overdue, ...b.today, ...b.tomorrow].slice(0, 4);
  if (!list.length) return null;
  return (
    <div className="flex flex-col gap-1">
      {list.map((t) => (
        <button key={t.id} onClick={() => onOpen(t)} className="text-left px-3 py-2 rounded-ctl hover:bg-surface-2 flex gap-3 items-center">
          <span className={cx("w-1.5 h-8 rounded-full", TONE_BG[URG_TONE[urgencyOf(t.due_at)]])} />
          <span className="flex-1 min-w-0">
            <span className="block text-meta font-medium q-clamp-1">{t.title}</span>
            <SourceLine s={t.sources[0]} />
          </span>
          <span className="q-num text-cap text-ink-2">{relDue(t.due_at)}</span>
        </button>
      ))}
    </div>
  );
}
