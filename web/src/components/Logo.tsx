// Logo：字母 Q 变形成勾选框——圆角方框是 Q 的"圈"，右下角的尾巴伸出框外，框内一个勾。
// 含义：QQ 里的消息 → 勾掉的待办。index.html 不能改，favicon 由 setFavicon() 在运行时注入。

export function LogoMark({ size = 28, mono = false }: { size?: number; mono?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-label="QNotify" role="img">
      <rect width="32" height="32" rx="9" fill={mono ? "currentColor" : "var(--q-brand)"} />
      {/* Q 的圈：开口在右下的圆角方框 */}
      <path
        d="M19.5 23H11a3.5 3.5 0 0 1-3.5-3.5V11A3.5 3.5 0 0 1 11 7.5h9.5A3.5 3.5 0 0 1 24 11v6.5"
        fill="none"
        stroke={mono ? "var(--q-surface)" : "var(--q-on-brand)"}
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      {/* 勾，同时向右下延伸成 Q 的尾巴 */}
      <path
        d="M11.6 15.4l3.2 3.2 5.2-5.6M18.6 20.2l5.4 5.3"
        fill="none"
        stroke={mono ? "var(--q-surface)" : "var(--q-on-brand)"}
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Wordmark({ size = 28 }: { size?: number }) {
  return (
    <div className="flex items-center gap-2 select-none">
      <LogoMark size={size} />
      <span className="text-title tracking-tight q-display" style={{ letterSpacing: "-0.02em" }}>
        Q<span className="text-brand">Notify</span>
      </span>
    </div>
  );
}

/** 运行时注入 favicon（不改 index.html） */
export function setFavicon(color = "#0a7cff"): void {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="${color}"/><path d="M19.5 23H11a3.5 3.5 0 0 1-3.5-3.5V11A3.5 3.5 0 0 1 11 7.5h9.5A3.5 3.5 0 0 1 24 11v6.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><path d="M11.6 15.4l3.2 3.2 5.2-5.6M18.6 20.2l5.4 5.3" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  let link = document.querySelector<HTMLLinkElement>("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/svg+xml";
  link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
