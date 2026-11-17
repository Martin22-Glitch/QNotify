// 待办页"手动添加"：平时是列表顶端的一行，点开在原位展开成小表单（不弹层、不离开页面）。
// 截止日期复用 DatePicker 里的 MonthCal（带当天已有截止的色点），时间用原生 time 输入，和详情页同一类控件。
import { useState } from "react";
import { humanError } from "../../lib/api";
import { useNow } from "../../lib/hooks";
import { useCreateTodo } from "../../lib/queries";
import { addDays, dayKey, fromLocalInput, startOfDay } from "../../lib/time";
import type { Todo } from "../../lib/types";
import { IconCal } from "../../components/icons";
import { Button, cx, Input, Textarea } from "../../components/ui";
import { MonthCal } from "./DatePicker";

export const TITLE_MAX = 120;
const DEFAULT_TIME = "23:59";

type DueMode = "none" | "today" | "tomorrow" | "pick";

const md = (k: string) => `${Number(k.slice(5, 7))}月${Number(k.slice(8))}日`;

function PlusGlyph({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function TodoQuickAdd({
  day,
  index,
  onCreated,
}: {
  /** 周历条当前选中的日期；有就预填为截止日 */
  day: string | null;
  index: Map<string, Todo[]>;
  onCreated: (t: Todo) => void;
}) {
  const now = useNow(60000);
  const create = useCreateTodo();
  const todayK = dayKey(startOfDay(now));
  const tomorrowK = dayKey(addDays(startOfDay(now), 1));

  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState<DueMode>("none");
  const [picked, setPicked] = useState<string | null>(null);
  const [time, setTime] = useState(DEFAULT_TIME);
  const [detail, setDetail] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [calOpen, setCalOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const reset = () => {
    setTitle("");
    setMode("none");
    setPicked(null);
    setTime(DEFAULT_TIME);
    setDetail("");
    setNoteOpen(false);
    setCalOpen(false);
    setErr(null);
  };

  const openForm = () => {
    reset();
    // 在周历上选了某天再点添加 → 默认就是那天截止
    if (day === todayK) setMode("today");
    else if (day === tomorrowK) setMode("tomorrow");
    else if (day) {
      setMode("pick");
      setPicked(day);
    }
    setOpen(true);
  };

  const cancel = () => {
    if (create.isPending) return;
    reset();
    setOpen(false);
  };

  const dueKey = mode === "today" ? todayK : mode === "tomorrow" ? tomorrowK : mode === "pick" ? picked : null;
  const dueIso = dueKey ? fromLocalInput(`${dueKey}T${time || DEFAULT_TIME}`) : null;
  const past = !!dueIso && new Date(dueIso).getTime() < now;

  const trimmed = title.trim();
  const len = [...trimmed].length; // 按字符计，和后端 len() 一致（emoji 不算 2）
  const over = len > TITLE_MAX;

  const submit = () => {
    if (create.isPending) return;
    if (!trimmed) return setErr("先写下要做什么");
    if (over) return setErr(`标题最多 ${TITLE_MAX} 字，现在 ${len} 字`);
    if (mode === "pick" && !picked) return setErr("选一个日期，或改成「不设」");
    setErr(null);
    create.mutate(
      { title: trimmed, due_at: dueIso, detail: detail.trim() || undefined },
      {
        onSuccess: (t) => {
          reset();
          setOpen(false);
          onCreated(t);
        },
        onError: (e) => setErr(`没保存上：${humanError(e)}`), // 内容不清空
      },
    );
  };

  if (!open) {
    return (
      <button
        onClick={openForm}
        className="q-card w-full flex items-center gap-3 px-4 h-12 text-left hover:bg-surface-2 transition"
      >
        <span className="w-7 h-7 rounded-full bg-brand-soft text-brand flex items-center justify-center shrink-0">
          <PlusGlyph />
        </span>
        <span className="flex-1 min-w-0 text-meta text-ink-3 q-clamp-1">添加待办</span>
        {day && <span className="text-cap text-ink-3 shrink-0">截止 {md(day)}</span>}
      </button>
    );
  }

  const chips: { v: DueMode; label: React.ReactNode }[] = [
    { v: "none", label: "不设" },
    { v: "today", label: "今天" },
    { v: "tomorrow", label: "明天" },
    {
      v: "pick",
      label: (
        <span className="inline-flex items-center gap-1">
          <IconCal size={14} />
          {mode === "pick" && picked ? md(picked) : "选日期"}
        </span>
      ),
    },
  ];

  return (
    <div className="q-card q-pad flex flex-col gap-3" role="group" aria-label="手动添加待办">
      {/* 标题：左边空心圆 = 预示它进列表后的样子 */}
      <div className="flex items-start gap-3">
        <span className="mt-2.5 w-[22px] h-[22px] rounded-full border-2 border-line shrink-0" aria-hidden />
        <div className="flex-1 min-w-0">
          <Input
            autoFocus
            value={title}
            placeholder="要做什么？"
            aria-invalid={!!err && (!trimmed || over)}
            onChange={(e) => {
              setTitle(e.target.value);
              if (err) setErr(null);
            }}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing) return; // 中文输入法选词时的回车不算提交
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape") cancel();
            }}
          />
          <div className="mt-1 min-h-[18px] flex items-center gap-2 text-cap">
            {err ? <span className="text-danger flex-1 min-w-0 break-words">{err}</span> : <span className="flex-1" />}
            {len > TITLE_MAX - 20 && (
              <span className={cx("q-num shrink-0", over ? "text-danger" : "text-ink-3")}>
                {len}/{TITLE_MAX}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 截止 */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-meta text-ink-2 font-medium mr-1">截止</span>
        {chips.map((c) => (
          <button
            key={c.v}
            type="button"
            onClick={() => {
              if (c.v === "pick") {
                setMode("pick");
                setCalOpen((o) => (mode === "pick" ? !o : true));
              } else {
                setMode(c.v);
                setCalOpen(false);
              }
              if (err) setErr(null);
            }}
            className={cx(
              "h-8 px-3 rounded-ctl text-cap font-medium transition",
              mode === c.v ? "bg-brand text-on-brand" : "bg-surface-2 text-ink-2 hover:text-ink",
            )}
          >
            {c.label}
          </button>
        ))}
        {dueKey && (
          <div className="w-28">
            <Input type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label="截止时刻" />
          </div>
        )}
      </div>
      {past && <div className="-mt-1 text-cap text-warn">这个时间已经过了，会排进「已逾期」</div>}

      {calOpen && mode === "pick" && (
        <div className="rounded-ctl border border-line p-3">
          <MonthCal
            value={picked}
            index={index}
            onPick={(k) => {
              setPicked(k);
              setCalOpen(false);
              if (err) setErr(null);
            }}
          />
        </div>
      )}

      {/* 备注：默认收起，少占一行 */}
      {noteOpen || detail ? (
        <Textarea
          rows={2}
          value={detail}
          onChange={(e) => setDetail(e.target.value)}
          placeholder="备注（可选）"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            else if (e.key === "Escape") cancel();
          }}
        />
      ) : (
        <button type="button" className="self-start text-cap text-brand font-medium h-8 -my-1" onClick={() => setNoteOpen(true)}>
          + 加备注
        </button>
      )}

      <div className="flex gap-2">
        <Button tone="primary" onClick={submit} loading={create.isPending} className="flex-1">
          添加
        </Button>
        <Button tone="ghost" onClick={cancel}>
          取消
        </Button>
      </div>
    </div>
  );
}
