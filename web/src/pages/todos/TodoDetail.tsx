// 待办详情：来源原句（信任基础）+ 改标题/截止 + 三态切换（未完成 / 已完成 / 不用做）。
import { useEffect, useState } from "react";
import { usePatchTodo } from "../../lib/queries";
import { fromLocalInput, toLocalInput } from "../../lib/time";
import type { Todo, TodoStatus } from "../../lib/types";
import { Confidence, DueText, QuoteBlock } from "../../components/bits";
import { IconCheck, IconEyeOff, IconTodo } from "../../components/icons";
import { Button, cx, Field, Help, Input, Textarea } from "../../components/ui";

const STATES: { v: TodoStatus; label: string; icon: React.ReactNode; on: string }[] = [
  { v: "open", label: "未完成", icon: <IconTodo size={16} />, on: "bg-brand text-on-brand" },
  { v: "done", label: "已完成", icon: <IconCheck size={16} />, on: "bg-ok text-white" },
  { v: "dropped", label: "不用做", icon: <IconEyeOff size={16} />, on: "bg-ink-2 text-bg" },
];

export function TodoDetail({ todo, onClose }: { todo: Todo; onClose?: () => void }) {
  const patch = usePatchTodo();
  const [title, setTitle] = useState(todo.title);
  const [due, setDue] = useState(toLocalInput(todo.due_at));
  const [detail, setDetail] = useState(todo.detail ?? "");
  useEffect(() => {
    setTitle(todo.title);
    setDue(toLocalInput(todo.due_at));
    setDetail(todo.detail ?? "");
  }, [todo.id, todo.title, todo.due_at, todo.detail]);

  const dirty = title !== todo.title || due !== toLocalInput(todo.due_at) || detail !== (todo.detail ?? "");
  const save = () => {
    const p: Partial<Todo> = {};
    if (title.trim() && title !== todo.title) p.title = title.trim();
    if (due !== toLocalInput(todo.due_at)) p.due_at = fromLocalInput(due);
    if (detail !== (todo.detail ?? "")) p.detail = detail;
    patch.mutate({ todo, patch: p });
  };

  return (
    <div className="flex flex-col gap-5">
      {/* 状态三态 */}
      <div>
        <div className="flex items-center gap-1.5 mb-2 text-meta text-ink-2 font-medium">
          状态
          <Help>
            <b>不用做</b> 和 <b>已完成</b> 是两回事：前者表示这条通知与你无关或已取消。两种都能随时改回未完成。你的选择会记成学习样本，让 AI 下次判得更准。
          </Help>
        </div>
        <div className="grid grid-cols-3 gap-1.5 p-1 rounded-ctl bg-surface-2">
          {STATES.map((s) => (
            <button
              key={s.v}
              onClick={() => s.v !== todo.status && patch.mutate({ todo, patch: { status: s.v } })}
              className={cx(
                "h-10 rounded-[calc(var(--q-r-ctl)-2px)] flex items-center justify-center gap-1.5 text-meta font-medium transition",
                todo.status === s.v ? s.on : "text-ink-2 hover:bg-surface",
              )}
            >
              {s.icon}
              {s.label}
            </button>
          ))}
        </div>
      </div>

      <Field label="标题">
        <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} />
      </Field>
      <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto] gap-3 items-end">
        <Field label="截止">
          <Input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
        </Field>
        <div className="flex gap-1.5 pb-0.5">
          {due && (
            <Button size="sm" tone="ghost" onClick={() => setDue("")}>
              清除截止
            </Button>
          )}
        </div>
      </div>
      {todo.status === "open" && todo.due_at && (
        <div className="-mt-3 text-meta">
          <DueText due={todo.due_at} strong />
        </div>
      )}
      <Field label="详情">
        <Textarea rows={2} value={detail} onChange={(e) => setDetail(e.target.value)} placeholder="可选" />
      </Field>

      {dirty && (
        <div className="flex gap-2 -mt-1">
          <Button tone="primary" onClick={save} loading={patch.isPending} className="flex-1">
            保存修改
          </Button>
          <Button
            tone="ghost"
            onClick={() => {
              setTitle(todo.title);
              setDue(toLocalInput(todo.due_at));
              setDetail(todo.detail ?? "");
            }}
          >
            还原
          </Button>
        </div>
      )}

      {/* 来源 */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <span className="text-meta text-ink-2 font-medium">来源原话</span>
          <span className="q-num text-cap text-ink-3">{todo.sources.length}</span>
          <div className="flex-1" />
          <Confidence v={todo.confidence} />
        </div>
        <div className="flex flex-col gap-2">
          {todo.sources.length === 0 && <div className="text-meta text-ink-3">这条待办没有记录来源</div>}
          {todo.sources.map((s) => (
            <QuoteBlock key={s.msg_id} text={s.quote} who={s.sender_name} where={s.group_name ?? "私聊"} time={s.msg_time} />
          ))}
        </div>
      </div>
      {onClose && (
        <Button tone="ghost" className="md:hidden" onClick={onClose}>
          关闭
        </Button>
      )}
    </div>
  );
}
