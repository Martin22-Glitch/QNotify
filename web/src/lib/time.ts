// 时间工具：一律按浏览器本地时区计算（核心服务默认 Asia/Shanghai，与使用者所在时区一致）。

const DAY = 86400000;
export const WEEK = ["日", "一", "二", "三", "四", "五", "六"];

export function startOfDay(d: Date | number): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}
export function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}
export function dayKey(d: Date | number): string {
  const x = new Date(d);
  return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
}
export function sameDay(a: Date | number, b: Date | number): boolean {
  return dayKey(a) === dayKey(b);
}
export function dayDiff(d: Date | number, now = Date.now()): number {
  return Math.round((startOfDay(d).getTime() - startOfDay(now).getTime()) / DAY);
}

export function hm(d: Date | number): string {
  const x = new Date(d);
  return `${String(x.getHours()).padStart(2, "0")}:${String(x.getMinutes()).padStart(2, "0")}`;
}

/** 截止时间的"日期名"：今天 / 明天 / 后天 / 周X / 11月20日 */
export function dayName(d: Date | number, now = Date.now()): string {
  const diff = dayDiff(d, now);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  if (diff === 2) return "后天";
  if (diff === -1) return "昨天";
  const x = new Date(d);
  if (diff > 0 && diff < 7) return `周${WEEK[x.getDay()]}`;
  return `${x.getMonth() + 1}月${x.getDate()}日`;
}

export type Urgency = "overdue" | "now" | "today" | "tomorrow" | "week" | "later" | "none";

export function urgencyOf(due: string | null, now = Date.now()): Urgency {
  if (!due) return "none";
  const t = new Date(due).getTime();
  if (t < now) return "overdue";
  const diff = dayDiff(t, now);
  if (t - now < 3 * 3600000 && diff === 0) return "now";
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  if (diff < 7) return "week";
  return "later";
}

/** 相对截止：还有 25 分钟 / 还有 2 小时 / 明天 18:00 / 已逾期 3 小时 */
export function relDue(due: string | null, now = Date.now()): string {
  if (!due) return "无截止";
  const t = new Date(due).getTime();
  const ms = t - now;
  const abs = Math.abs(ms);
  if (ms < 0) {
    if (abs < 3600000) return `逾期 ${Math.max(1, Math.round(abs / 60000))} 分钟`;
    if (abs < DAY) return `逾期 ${Math.round(abs / 3600000)} 小时`;
    return `逾期 ${Math.round(abs / DAY)} 天`;
  }
  if (ms < 3600000) return `还有 ${Math.max(1, Math.round(ms / 60000))} 分钟`;
  const diff = dayDiff(t, now);
  if (diff === 0) return `还有 ${Math.round(ms / 3600000)} 小时`;
  return `${dayName(t, now)} ${hm(t)}`;
}

/** 倒计时短格式：25m / 3h / 2d */
export function shortLeft(due: string | null, now = Date.now()): { v: string; u: string } {
  if (!due) return { v: "—", u: "" };
  const ms = new Date(due).getTime() - now;
  const abs = Math.abs(ms);
  const sign = ms < 0 ? "-" : "";
  if (abs < 3600000) return { v: sign + Math.max(1, Math.round(abs / 60000)), u: "分" };
  if (abs < 48 * 3600000) return { v: sign + Math.round(abs / 3600000), u: "时" };
  return { v: sign + Math.round(abs / DAY), u: "天" };
}

export function msgTime(sec: number, now = Date.now()): string {
  const t = sec * 1000;
  const diff = now - t;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.round(diff / 60000)} 分钟前`;
  if (sameDay(t, now)) return hm(t);
  if (dayDiff(t, now) === -1) return `昨天 ${hm(t)}`;
  const x = new Date(t);
  return `${x.getMonth() + 1}/${x.getDate()} ${hm(t)}`;
}

/** ISO → <input type="datetime-local"> 值 */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** <input type="datetime-local"> 值 → 带时区 ISO（如 2026-11-20T18:00:00+08:00） */
export function fromLocalInput(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  if (isNaN(d.getTime())) return null;
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const p = (n: number) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return `${v.length === 16 ? v + ":00" : v}${sign}${p(off / 60)}:${p(off % 60)}`;
}
