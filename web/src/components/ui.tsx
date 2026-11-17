// 基础组件：按钮、标签、状态点、角标、分段控件、"?" 提示、底部抽屉、空态、输入框、滑动行、Toast。
import {
  useEffect,
  type PointerEvent as RPointerEvent,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { useStore } from "../lib/store";
import { dismissToast, toastStore } from "../lib/toast";
import { IconHelp, IconX } from "./icons";

export function cx(...xs: (string | false | null | undefined)[]): string {
  return xs.filter(Boolean).join(" ");
}

/* ── Button ── */
type BtnTone = "primary" | "soft" | "ghost" | "danger" | "ok" | "plain";
export function Button({
  tone = "soft",
  size = "md",
  className,
  children,
  loading,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: BtnTone; size?: "sm" | "md" | "lg"; loading?: boolean }) {
  const tones: Record<BtnTone, string> = {
    primary: "bg-brand text-on-brand hover:brightness-110 active:brightness-95",
    soft: "bg-surface-2 text-ink hover:bg-line/60",
    ghost: "bg-transparent text-ink-2 hover:bg-surface-2",
    danger: "bg-danger-soft text-danger hover:brightness-95",
    ok: "bg-ok text-white hover:brightness-110",
    plain: "bg-surface text-ink border border-line hover:bg-surface-2",
  };
  const sizes = { sm: "h-9 px-3 text-meta gap-1.5", md: "h-10 px-4 text-body gap-2", lg: "h-12 px-5 text-body gap-2" };
  return (
    <button
      type="button"
      className={cx(
        "inline-flex items-center justify-center rounded-ctl font-medium transition disabled:opacity-50 shrink-0 whitespace-nowrap",
        tones[tone],
        sizes[size],
        className,
      )}
      disabled={loading || rest.disabled}
      {...rest}
    >
      {loading && <span className="w-3.5 h-3.5 rounded-full border-2 border-current border-t-transparent animate-spin" />}
      {children}
    </button>
  );
}

export function IconButton({ className, children, label, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx("inline-flex items-center justify-center w-10 h-10 rounded-ctl text-ink-2 hover:bg-surface-2 transition shrink-0", className)}
      {...rest}
    >
      {children}
    </button>
  );
}

/* ── 语义标签 ── */
export type Tone = "brand" | "danger" | "urgent" | "warn" | "ok" | "muted";
const TONE_CLS: Record<Tone, string> = {
  brand: "bg-brand-soft text-brand",
  danger: "bg-danger-soft text-danger",
  urgent: "bg-urgent-soft text-urgent",
  warn: "bg-warn-soft text-warn",
  ok: "bg-ok-soft text-ok",
  muted: "bg-muted-soft text-ink-2",
};
export const TONE_TEXT: Record<Tone, string> = {
  brand: "text-brand",
  danger: "text-danger",
  urgent: "text-urgent",
  warn: "text-warn",
  ok: "text-ok",
  muted: "text-ink-3",
};
export const TONE_BG: Record<Tone, string> = {
  brand: "bg-brand",
  danger: "bg-danger",
  urgent: "bg-urgent",
  warn: "bg-warn",
  ok: "bg-ok",
  muted: "bg-muted",
};

export function Tag({ tone = "muted", children, className, dot }: { tone?: Tone; children: ReactNode; className?: string; dot?: boolean }) {
  return (
    <span className={cx("inline-flex items-center gap-1 h-6 px-2 rounded-full text-cap font-medium whitespace-nowrap", TONE_CLS[tone], className)}>
      {dot && <span className={cx("w-1.5 h-1.5 rounded-full", TONE_BG[tone])} />}
      {children}
    </span>
  );
}

export function Dot({ tone, pulse, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return <span className={cx("inline-block w-2 h-2 rounded-full shrink-0", TONE_BG[tone], pulse && "q-pulse", className)} />;
}

export function Badge({ n, className }: { n: number; className?: string }) {
  if (!n) return null;
  return (
    <span
      className={cx(
        "min-w-[18px] h-[18px] px-1 rounded-full bg-danger text-white text-[11px] leading-[18px] text-center font-semibold q-num",
        className,
      )}
    >
      {n > 99 ? "99+" : n}
    </span>
  );
}

/* ── 分段控件（带数量） ── */
export function Segmented<T extends string>({
  value,
  onChange,
  items,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  items: { value: T; label: string; count?: number; tone?: Tone }[];
  className?: string;
}) {
  return (
    <div className={cx("flex p-1 rounded-ctl bg-surface-2 gap-1", className)} role="tablist">
      {items.map((it) => {
        const on = it.value === value;
        return (
          <button
            key={it.value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(it.value)}
            className={cx(
              "flex-1 h-9 rounded-[calc(var(--q-r-ctl)-2px)] text-meta font-medium flex items-center justify-center gap-1.5 transition min-w-0",
              on ? "bg-surface text-ink shadow-card" : "text-ink-2 hover:text-ink",
            )}
          >
            {it.tone && <Dot tone={it.tone} />}
            <span className="truncate">{it.label}</span>
            {it.count !== undefined && (
              <span className={cx("q-num text-cap", on ? "text-ink-2" : "text-ink-3")}>{it.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ── "?" 提示：说明性文案全部收在这里 ── */
export function Help({ children, className }: { children: ReactNode; className?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", h);
    return () => document.removeEventListener("pointerdown", h);
  }, [open]);
  return (
    <span ref={ref} className={cx("relative inline-flex", className)}>
      <button
        type="button"
        aria-label="说明"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        className="w-6 h-6 -m-1 inline-flex items-center justify-center rounded-full text-ink-3 hover:text-ink-2"
      >
        <IconHelp size={16} />
      </button>
      {open && (
        <span
          className={cx(
            "z-50 p-3 rounded-ctl bg-ink text-bg text-meta font-normal shadow-pop q-in q-pretty text-left",
            // 手机：固定居中，宽度跟着屏幕走，靠近屏幕边缘也不会溢出
            "fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(20rem,calc(100vw-2rem))]",
            // 桌面：仍然贴着问号弹出
            "md:absolute md:top-7 md:translate-y-0 md:w-64 md:max-w-[80vw]",
          )}
          onClick={(e) => e.stopPropagation()}
        >
          {children}
        </span>
      )}
    </span>
  );
}

/* ── 底部抽屉（手机）/ 居中面板（桌面） ── */
export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center">
      <div className="absolute inset-0 bg-black/40 q-in" onClick={onClose} />
      <div
        className={cx(
          // overflow-hidden：圆角要能裁掉子元素，否则底部按钮会溢出到卡片圆角外面
          "relative w-full bg-surface shadow-pop flex flex-col overflow-hidden max-h-[90dvh] q-sheet-in md:q-in",
          "rounded-t-[calc(var(--q-r-card)+4px)] md:rounded-card",
          wide ? "md:max-w-2xl" : "md:max-w-lg",
        )}
      >
        <div className="md:hidden mx-auto mt-2 w-10 h-1 rounded-full bg-line" />
        {title !== undefined && (
          <div className="flex items-center gap-2 px-5 pt-3 md:pt-5 pb-2 shrink-0">
            <div className="flex-1 min-w-0 text-title q-title">{title}</div>
            <IconButton label="关闭" onClick={onClose} className="-mr-2">
              <IconX />
            </IconButton>
          </div>
        )}
        {/* min-h-0：flex 子项默认不肯收缩，内容一多就会把底部操作区顶出可视区 */}
        <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-4">{children}</div>
        {footer && (
          <div className="shrink-0 px-5 py-3 border-t border-line flex gap-2 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── 空态 ── */
export function Empty({
  icon,
  title,
  sub,
  action,
  tone = "brand",
}: {
  icon: ReactNode;
  title: string;
  sub?: ReactNode;
  action?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="flex flex-col items-center text-center py-12 px-6 q-in">
      <div className={cx("w-14 h-14 rounded-card flex items-center justify-center mb-4", TONE_CLS[tone])}>{icon}</div>
      <div className="text-title q-title">{title}</div>
      {sub && <div className="mt-1 text-meta text-ink-2 max-w-xs q-pretty">{sub}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/* ── 表单 ── */
export function Field({ label, help, children, error }: { label: string; help?: ReactNode; children: ReactNode; error?: string | null }) {
  return (
    <label className="block">
      <span className="flex items-center gap-1.5 mb-1.5 text-meta text-ink-2 font-medium">
        {label}
        {help && <Help>{help}</Help>}
      </span>
      {children}
      {error && <span className="block mt-1 text-meta text-danger break-words">{error}</span>}
    </label>
  );
}

const INPUT =
  "w-full h-11 px-3 rounded-ctl bg-surface-2 border border-transparent text-body text-ink placeholder:text-ink-3 outline-none focus:border-brand focus:bg-surface transition";
export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(INPUT, className)} {...rest} />;
}
export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(INPUT, "h-auto py-2.5 resize-none", className)} {...rest} />;
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cx("relative w-12 h-7 rounded-full transition shrink-0", checked ? "bg-brand" : "bg-line")}
    >
      <span
        className={cx("absolute top-0.5 w-6 h-6 rounded-full bg-white shadow transition-all", checked ? "left-[22px]" : "left-0.5")}
      />
    </button>
  );
}

/* ── 左右滑动行（手机）：右滑 = 主动作，左滑 = 次动作 ── */
export interface SwipeAction {
  label: string;
  icon: ReactNode;
  tone: Tone;
  run: () => void;
}
export function SwipeRow({
  children,
  right,
  left,
  className,
}: {
  children: ReactNode;
  /** 向右滑露出（位于左侧） */
  right?: SwipeAction;
  /** 向左滑露出（位于右侧） */
  left?: SwipeAction;
  className?: string;
}) {
  const [dx, setDx] = useState(0);
  const start = useRef<{ x: number; y: number; lock: "x" | "y" | null } | null>(null);
  const TH = 84;
  const onDown = (e: RPointerEvent) => {
    if (e.pointerType === "mouse") return;
    start.current = { x: e.clientX, y: e.clientY, lock: null };
  };
  const onMove = (e: RPointerEvent) => {
    const s = start.current;
    if (!s) return;
    const mx = e.clientX - s.x;
    const my = e.clientY - s.y;
    if (!s.lock) {
      if (Math.abs(mx) > 8 && Math.abs(mx) > Math.abs(my)) s.lock = "x";
      else if (Math.abs(my) > 8) s.lock = "y";
    }
    if (s.lock !== "x") return;
    let v = mx;
    if (v > 0 && !right) v = 0;
    if (v < 0 && !left) v = 0;
    setDx(Math.max(-140, Math.min(140, v)));
  };
  const onUp = () => {
    const s = start.current;
    start.current = null;
    if (!s || s.lock !== "x") return setDx(0);
    if (dx > TH && right) right.run();
    else if (dx < -TH && left) left.run();
    setDx(0);
  };
  const act = dx > 0 ? right : dx < 0 ? left : undefined;
  const armed = Math.abs(dx) > TH;
  return (
    <div className={cx("relative overflow-hidden rounded-card", className)}>
      {act && (
        <div
          className={cx(
            "absolute inset-0 flex items-center px-6 text-white font-medium text-meta gap-2 transition-colors",
            dx > 0 ? "justify-start" : "justify-end",
            armed ? TONE_BG[act.tone] : "bg-muted",
          )}
        >
          {act.icon}
          {act.label}
        </div>
      )}
      <div
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        style={{ transform: `translateX(${dx}px)`, transition: start.current ? "none" : "transform .2s", touchAction: "pan-y" }}
        className="relative"
      >
        {children}
      </div>
    </div>
  );
}

/* ── Toast ── */
export function Toaster() {
  const items = useStore(toastStore);
  return (
    <div className="fixed z-[60] left-0 right-0 bottom-20 md:bottom-6 flex flex-col items-center gap-2 pointer-events-none px-4">
      {items.map((t) => (
        <div
          key={t.id}
          className={cx(
            "pointer-events-auto flex items-center gap-3 pl-4 pr-2 h-11 rounded-full shadow-pop text-meta q-in max-w-full",
            t.tone === "error" ? "bg-danger text-white" : "bg-ink text-bg",
          )}
        >
          <span className="truncate">{t.text}</span>
          {t.action ? (
            <button
              type="button"
              className="h-8 px-3 rounded-full font-semibold text-brand-soft hover:bg-white/10"
              onClick={() => {
                t.action?.run();
                dismissToast(t.id);
              }}
            >
              {t.action.label}
            </button>
          ) : (
            <span className="w-2" />
          )}
        </div>
      ))}
    </div>
  );
}

/* ── 小节标题 ── */
export function SectionHead({ title, count, tone, right, help }: { title: string; count?: number; tone?: Tone; right?: ReactNode; help?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 px-1 mb-2 mt-6 first:mt-0">
      {tone && <Dot tone={tone} />}
      <h3 className={cx("text-meta font-semibold tracking-wide", tone ? TONE_TEXT[tone] : "text-ink-2")}>{title}</h3>
      {count !== undefined && <span className="q-num text-cap text-ink-3">{count}</span>}
      {help && <Help>{help}</Help>}
      <div className="flex-1" />
      {right}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("bg-surface-2 rounded-card q-pulse", className)} />;
}
