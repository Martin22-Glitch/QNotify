// 画像输入的基础件（ 的提议卡已随  退役，只留输入本身）。
export const ABOUT_MAX = 500;

export function AboutTextarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className="w-full min-h-40 rounded-ctl border border-line bg-surface px-3 py-2 text-body text-ink placeholder:text-ink-3 focus:outline-none focus:ring-2 focus:ring-brand/30 resize-y"
    />
  );
}
