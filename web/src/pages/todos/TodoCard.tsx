import { useNow } from "../../lib/hooks";
import { shortLeft, urgencyOf } from "../../lib/time";
import type { Todo } from "../../lib/types";
import { CheckCircle, DueText, SourceLine, URG_TONE, useFresh } from "../../components/bits";
import { IconCheck, IconEyeOff, IconUndo } from "../../components/icons";
import { cx, SwipeRow, Tag, TONE_BG, TONE_TEXT } from "../../components/ui";
import { usePatchTodo } from "../../lib/queries";

export function TodoCard({ todo, selected, onOpen, countdown }: { todo: Todo; selected?: boolean; onOpen: () => void; countdown?: boolean }) {
  const now = useNow();
  const patch = usePatchTodo();
  const fresh = useFresh(`todo:${todo.id}`);
  const u = urgencyOf(todo.due_at, now);
  const tone = URG_TONE[u];
  const src = todo.sources[0];
  const isOpen = todo.status === "open";
  const left = shortLeft(todo.due_at, now);
  const hot = isOpen && (u === "overdue" || u === "now" || u === "today");

  const setStatus = (status: Todo["status"]) => patch.mutate({ todo, patch: { status } });

  return (
    <SwipeRow
      right={
        isOpen
          ? { label: "完成", icon: <IconCheck size={18} />, tone: "ok", run: () => setStatus("done") }
          : { label: "恢复", icon: <IconUndo size={18} />, tone: "brand", run: () => setStatus("open") }
      }
      left={isOpen ? { label: "不用做", icon: <IconEyeOff size={18} />, tone: "muted", run: () => setStatus("dropped") } : undefined}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => e.key === "Enter" && onOpen()}
        className={cx(
          "q-card flex items-start gap-3 q-pad cursor-pointer transition relative",
          selected && "ring-2 ring-brand",
          fresh && "q-fresh",
          !isOpen && "opacity-80",
        )}
      >
        {/* 紧迫色条 */}
        {isOpen && u !== "none" && u !== "later" && (
          <span className={cx("absolute left-0 top-3 bottom-3 w-[3px] rounded-r-full", TONE_BG[tone])} />
        )}
        <div className="pt-0.5">
          {todo.status === "dropped" ? (
            <span className="w-[22px] h-[22px] rounded-full bg-muted-soft text-ink-3 flex items-center justify-center">
              <IconEyeOff size={13} />
            </span>
          ) : (
            <CheckCircle
              checked={todo.status === "done"}
              tone={hot ? tone : "muted"}
              label={todo.status === "done" ? "恢复为未完成" : "标为完成"}
              onClick={() => setStatus(todo.status === "done" ? "open" : "done")}
            />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className={cx("text-body q-title q-clamp-2 break-words", !isOpen && "line-through text-ink-3 decoration-1")}>{todo.title}</div>
          <div className="mt-1 flex items-center gap-x-3 gap-y-1 flex-wrap">
            {isOpen ? <DueText due={todo.due_at} className="text-meta" strong={hot} /> : <StatusTag s={todo.status} />}
            <SourceLine s={src} origin={todo.origin} className="max-w-full" />
            {todo.sources.length > 1 && <span className="text-cap text-ink-3">+{todo.sources.length - 1} 处来源</span>}
          </div>
        </div>
        {countdown && isOpen && todo.due_at && (
          <div className={cx("text-right shrink-0 leading-none pt-0.5", TONE_TEXT[tone])}>
            <span className="q-num text-h2 font-bold">{left.v}</span>
            <span className="text-cap ml-0.5">{left.u}</span>
          </div>
        )}
        {!isOpen && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setStatus("open");
            }}
            className="hidden md:inline-flex h-8 px-2.5 items-center gap-1 rounded-ctl text-cap text-ink-2 hover:bg-surface-2 shrink-0"
          >
            <IconUndo size={14} />
            恢复
          </button>
        )}
      </div>
    </SwipeRow>
  );
}

function StatusTag({ s }: { s: Todo["status"] }) {
  if (s === "done")
    return (
      <Tag tone="ok">
        <IconCheck size={12} />
        已完成
      </Tag>
    );
  return (
    <Tag tone="muted">
      <IconEyeOff size={12} />
      不用做
    </Tag>
  );
}
