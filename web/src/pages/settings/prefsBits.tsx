// 我的偏好页共用的小件：块标题、列表容器、统一条目行、命中次数、行内二次确认、添加表单、空态、加载/错误。
// 「我关注的」「不看的」两块只通过这里的 PrefList / PrefRow / ConfirmRow 出条目，保证结构同构。
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button, Help, Input, Skeleton } from "../../components/ui";

/* ── 块标题：比条目主文字高一级（text-title），右侧放计数 / ? / 添加 ── */
export function BlockHead({
  title,
  count,
  help,
  right,
}: {
  title: string;
  count?: number;
  help?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-2 mb-3 min-h-9">
      <h3 className="text-title q-title text-ink">{title}</h3>
      {count !== undefined && <span className="q-num text-meta text-ink-3">{count}</span>}
      {help && <Help>{help}</Help>}
      <div className="flex-1" />
      {right}
    </div>
  );
}

/** 「?」里的分段：Help 渲染在 span 内，不能用 <p> */
export function HelpPara({ children }: { children: ReactNode }) {
  return <span className="block [&+&]:mt-2">{children}</span>;
}

/* ── 列表容器：外框 + 块内细分隔（比块间分隔弱） ── */
export function PrefList({ label, children }: { label: string; children: ReactNode }) {
  return (
    <ul aria-label={label} className="rounded-card border border-line divide-y divide-line/60 overflow-hidden">
      {children}
    </ul>
  );
}

/**
 * 统一条目行：左 = 主文字 + 状态标签，右 = 命中信息 + 操作。
 * 两类条目都只用它，不允许再加副文本行。
 */
export function PrefRow({
  main,
  tag,
  meta,
  actions,
  title,
}: {
  main: ReactNode;
  tag: ReactNode;
  meta?: ReactNode;
  actions: ReactNode;
  /** 桌面悬停时显示的原话等补充信息，不占列表主行 */
  title?: string;
}) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 min-h-14">
      <div className="flex-1 min-w-0 basis-40 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span title={title} className="text-body font-medium text-ink break-words [overflow-wrap:anywhere]">
          {main}
        </span>
        {tag}
      </div>
      <div className="ml-auto flex items-center gap-3 shrink-0">
        {meta}
        {actions}
      </div>
    </li>
  );
}

/** 命中次数：辅助信息，弱于主文字；数字稍强一点便于扫读 */
export function HitCount({ n }: { n: number }) {
  if (n <= 0) return <span className="text-cap text-ink-3 whitespace-nowrap">还没命中</span>;
  return (
    <span className="text-cap text-ink-3 whitespace-nowrap">
      命中 <span className="q-num font-semibold text-ink-2">{n}</span> 次
    </span>
  );
}

/** 删除按钮：危险色，与普通操作区分 */
export function DeleteButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <Button tone="danger" size="sm" disabled={disabled} onClick={onClick}>
      删除
    </Button>
  );
}

/**
 * 行内二次确认：替换原行。红框 + 红字，与常态明显不同。
 * 默认焦点在「取消」，Esc 取消；「取消」在最右（原「删除」位置），连点两下不会误删。
 */
export function ConfirmRow({
  label,
  children,
  busy,
  onConfirm,
  onCancel,
}: {
  label: string;
  children: ReactNode;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("[data-cancel]")?.focus();
  }, []);
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && !busy) onCancel();
  };
  return (
    <li
      ref={ref}
      role="group"
      aria-label={label}
      onKeyDown={onKey}
      className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 min-h-14 ring-2 ring-inset ring-danger"
    >
      <span className="flex-1 min-w-0 basis-40 text-meta font-medium text-danger break-words [overflow-wrap:anywhere]">
        {children}
      </span>
      <div className="ml-auto flex gap-2 shrink-0">
        <Button tone="danger" size="sm" loading={busy} onClick={onConfirm}>
          确认删除
        </Button>
        <Button tone="plain" size="sm" disabled={busy} onClick={onCancel} data-cancel="">
          取消
        </Button>
      </div>
    </li>
  );
}

/** 行内添加：一个输入框 + 添加/取消。提交成功才清空收起，失败保留文字 */
export function AddForm({
  label,
  placeholder,
  maxLength = 40,
  busy,
  onSubmit,
  onClose,
}: {
  label: string;
  placeholder: string;
  maxLength?: number;
  busy: boolean;
  onSubmit: (v: string) => Promise<unknown>;
  onClose: () => void;
}) {
  const [v, setV] = useState("");
  const go = async () => {
    const s = v.trim();
    if (!s || busy) return;
    try {
      await onSubmit(s);
      setV("");
      onClose();
    } catch {
      /* 错误已由 mutation 提示，保留输入 */
    }
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void go();
      }}
      className="flex flex-wrap items-center gap-2 mb-3"
    >
      <Input
        autoFocus
        aria-label={label}
        value={v}
        maxLength={maxLength}
        placeholder={placeholder}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && !busy && onClose()}
        className="flex-1 min-w-0 basis-48"
      />
      <div className="flex gap-2 shrink-0">
        <Button type="submit" tone="primary" loading={busy} disabled={busy || !v.trim()}>
          添加
        </Button>
        <Button tone="ghost" disabled={busy} onClick={onClose}>
          取消
        </Button>
      </div>
    </form>
  );
}

/** 块标题右侧的「添加」按钮 */
export function AddButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <Button tone="plain" size="sm" onClick={onClick} disabled={disabled}>
      ＋ 添加
    </Button>
  );
}

/** 块内短空态：一句话，说下一步做什么 */
export function MiniEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-card border border-dashed border-line px-4 py-4 text-meta text-ink-2 break-words">{children}</div>
  );
}

export function ListSkeleton({ label }: { label: string }) {
  return (
    <div className="flex flex-col gap-2" aria-busy="true" aria-label={label}>
      <Skeleton className="h-14" />
      <Skeleton className="h-14" />
    </div>
  );
}

export function LoadError({ text, retrying, onRetry }: { text: string; retrying: boolean; onRetry: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2">
      <span className="flex-1 min-w-0 basis-48 break-words">{text}</span>
      <Button tone="plain" size="sm" loading={retrying} onClick={onRetry}>
        重试
      </Button>
    </div>
  );
}
