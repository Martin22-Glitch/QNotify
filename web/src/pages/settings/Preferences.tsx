// 我的偏好：一页三块，顺序按方案：关于我 / 我关注的 / 不看的。
// 块间：40px 留白 + 块标题（text-title）；块内：条目在带框列表里，用浅色细线分隔。
import { useEffect } from "react";
import { AboutPanel } from "./About";
import { FocusPanel } from "./Focus";
import { IgnorePanel } from "./Ignore";

export type PrefBlock = "about" | "focus" | "ignore";
const ANCHOR: Record<PrefBlock, string> = { about: "pref-about", focus: "pref-focus", ignore: "pref-ignore" };

/** jump：从旧入口（如助手页链接到「我关注的」）进来时滚到对应块 */
export function PreferencesPanel({ jump }: { jump?: PrefBlock }) {
  useEffect(() => {
    if (!jump || jump === "about") return;
    const t = window.setTimeout(
      () => document.getElementById(ANCHOR[jump])?.scrollIntoView({ block: "start", behavior: "smooth" }),
      50,
    );
    return () => window.clearTimeout(t);
  }, [jump]);

  return (
    <div className="flex flex-col gap-10">
      <div id={ANCHOR.about} className="scroll-mt-6">
        <AboutPanel />
      </div>
      <div id={ANCHOR.focus} className="scroll-mt-6">
        <FocusPanel />
      </div>
      <div id={ANCHOR.ignore} className="scroll-mt-6">
        <IgnorePanel />
      </div>
    </div>
  );
}
