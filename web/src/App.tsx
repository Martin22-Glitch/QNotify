// 应用外壳：手机底部 4 栏（待办 / 待确认 / 消息 / 助手）+ 顶栏齿轮进设置；
// 宽屏（≥1024）左侧窄导航栏 + 主区多列。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { connStore } from "./lib/config";
import { useWide } from "./lib/hooks";
import { useReview, useAllTodos, usePipeline } from "./lib/queries";
import { ensureRealtime, rtStore } from "./lib/realtime";
import { useStore } from "./lib/store";
import { urgencyOf } from "./lib/time";
import { api } from "./lib/api";
import { useQuery } from "@tanstack/react-query";
import { IconChat, IconGear, IconInbox, IconTodo, IconWifiOff, IconSpark } from "./components/icons";
import { LogoMark, Wordmark } from "./components/Logo";
import { Badge, cx, Dot, IconButton, Toaster } from "./components/ui";
import { AssistantPage } from "./pages/AssistantPage";
import { ConnectPage } from "./pages/ConnectPage";
import { MessagesPage } from "./pages/MessagesPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SettingsPage } from "./pages/settings/SettingsPage";
import { TodosPage } from "./pages/todos/TodosPage";

export type Tab = "todos" | "review" | "messages" | "assistant" | "settings";
const TAB_KEY = "qn.tab";

export default function App() {
  const conn = useStore(connStore);
  if (!conn.token || conn.invalid) return <ConnectPage />;
  return <Shell />;
}

/** 唯一挂载实时通道的地方：key = 地址 + 令牌，不变就不会重建 */
function RealtimeBridge() {
  const qc = useQueryClient();
  const { baseUrl, token } = useStore(connStore);
  const key = token ? `${baseUrl}|${token}` : "";
  useEffect(() => {
    ensureRealtime(qc, key);
  }, [qc, key]);
  useEffect(() => () => ensureRealtime(qc, ""), [qc]);
  return null;
}

function Shell() {
  const wide = useWide();
  const [tab, setTabRaw] = useState<Tab>(() => (localStorage.getItem(TAB_KEY) as Tab) || "todos");
  const setTab = (t: Tab) => {
    setTabRaw(t);
    localStorage.setItem(TAB_KEY, t);
    window.scrollTo({ top: 0 });
  };
  const review = useReview();
  const todos = useAllTodos();
  const reviewN = review.data?.length ?? 0;
  const urgentN = todos.open.filter((t) => {
    const u = urgencyOf(t.due_at);
    return u === "overdue" || u === "now" || u === "today";
  }).length;

  const nav: { id: Tab; label: string; icon: ReactNode; badge?: number; dot?: boolean }[] = [
    { id: "todos", label: "待办", icon: <IconTodo size={22} />, badge: urgentN },
    { id: "review", label: "待确认", icon: <IconInbox size={22} />, badge: reviewN },
    { id: "messages", label: "消息", icon: <IconChat size={22} /> },
    { id: "assistant", label: "助手", icon: <IconSpark size={22} /> },
  ];

  const page = (
    <>
      {tab === "todos" && <TodosPage go={setTab} />}
      {tab === "review" && <ReviewPage go={setTab} />}
      {tab === "messages" && <MessagesPage go={setTab} />}
      {tab === "assistant" && <AssistantPage go={setTab} />}
      {tab === "settings" && <SettingsPage />}
    </>
  );

  return (
    <>
      <RealtimeBridge />
      {wide ? (
        <div className="min-h-dvh grid grid-cols-[232px_minmax(0,1fr)]">
          <aside className="sticky top-0 h-dvh flex flex-col px-3 py-5 border-r border-line bg-surface">
            <div className="px-2 mb-6">
              <Wordmark />
            </div>
            <nav className="flex flex-col gap-1">
              {nav.map((n) => (
                <SideItem key={n.id} active={tab === n.id} onClick={() => setTab(n.id)} icon={n.icon} label={n.label} badge={n.badge} />
              ))}
            </nav>
            <div className="flex-1" />
            <ConnPill />
            <SideItem active={tab === "settings"} onClick={() => setTab("settings")} icon={<IconGear size={22} />} label="设置" />
          </aside>
          <main className="min-w-0 px-8 py-6">
            <Banners onFix={() => setTab("settings")} />
            <div className="max-w-[1180px] mx-auto">{page}</div>
          </main>
        </div>
      ) : (
        <div className="min-h-dvh pb-[76px]">
          <header className="sticky top-0 z-30 bg-bg/85 backdrop-blur-md">
            <div className="h-14 px-4 flex items-center gap-2">
              <button onClick={() => setTab("todos")} className="flex items-center gap-2">
                <LogoMark size={28} />
                <span className="text-title q-display tracking-tight">
                  Q<span className="text-brand">Notify</span>
                </span>
              </button>
              <div className="flex-1" />
              <ConnDot />
              <IconButton label="设置" onClick={() => setTab("settings")} className={cx(tab === "settings" && "text-brand bg-brand-soft")}>
                <IconGear size={22} />
              </IconButton>
            </div>
          </header>
          <main className="px-4 pt-1 pb-6">
            <Banners onFix={() => setTab("settings")} />
            {page}
          </main>
          <nav className="fixed bottom-0 inset-x-0 z-40 bg-surface/95 backdrop-blur-md border-t border-line q-safe-b">
            <div className="grid grid-cols-4 h-[60px]">
              {nav.map((n) => {
                const on = tab === n.id;
                return (
                  <button key={n.id} onClick={() => setTab(n.id)} className={cx("flex flex-col items-center justify-center gap-0.5 relative", on ? "text-brand" : "text-ink-3")}>
                    <span className="relative">
                      {n.icon}
                      {n.badge ? <Badge n={n.badge} className={cx("absolute -top-1.5 -right-3", n.id === "todos" && "bg-urgent")} /> : null}
                    </span>
                    <span className={cx("text-[11px]", on && "font-semibold")}>{n.label}</span>
                  </button>
                );
              })}
            </div>
          </nav>
        </div>
      )}
      <Toaster />
    </>
  );
}

function SideItem({ active, onClick, icon, label, badge }: { active: boolean; onClick: () => void; icon: ReactNode; label: string; badge?: number }) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "h-11 px-3 rounded-ctl flex items-center gap-3 text-body transition",
        active ? "bg-brand-soft text-brand font-semibold" : "text-ink-2 hover:bg-surface-2",
      )}
    >
      {icon}
      <span className="flex-1 text-left">{label}</span>
      {badge ? <Badge n={badge} className={label === "待办" ? "bg-urgent" : ""} /> : null}
    </button>
  );
}

function ConnDot() {
  const rt = useStore(rtStore);
  const tone = rt.state === "live" ? "ok" : rt.state === "polling" || rt.state === "connecting" ? "warn" : "danger";
  return (
    <span className="w-10 h-10 flex items-center justify-center" title={rt.state === "live" ? "实时已连接" : "实时重连中"}>
      <Dot tone={tone} pulse={tone !== "ok"} />
    </span>
  );
}

function ConnPill() {
  const rt = useStore(rtStore);
  const live = rt.state === "live";
  return (
    <div className="mx-1 mb-2 px-3 py-2 rounded-ctl bg-surface-2 flex items-center gap-2 text-cap text-ink-2">
      <Dot tone={live ? "ok" : "warn"} pulse={!live} />
      {live ? "实时同步中" : "重连中 · 自动拉取"}
    </div>
  );
}

/** 顶部横幅：只在出问题时出现（实时断开超过 4 秒 / QQ 协议端未连接 / 模型报错） */
function Banners({ onFix }: { onFix: () => void }) {
  const rt = useStore(rtStore);
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, staleTime: 5000 });
  const pl = usePipeline();
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((x) => x + 1), 2000);
    return () => clearInterval(t);
  }, []);
  const down = (rt.state === "polling" || rt.state === "connecting") && Date.now() - rt.since > 4000;
  const napcatDown = stats.data && stats.data.napcat_connected === false;
  const items: { tone: "warn" | "danger"; text: string; action?: string }[] = [];
  if (down) items.push({ tone: "warn", text: "实时连接断开，正在重连；数据每 8 秒自动刷新" });
  if (napcatDown) items.push({ tone: "danger", text: "QQ 协议端未连接，新消息暂时收不到", action: "查看" });
  if (pl.data?.last_error) items.push({ tone: "danger", text: `模型调用失败：${pl.data.last_error}`, action: "去设置" });
  if (!items.length) return null;
  return (
    <div className="flex flex-col gap-2 mb-3 max-w-[1180px] mx-auto">
      {items.map((b) => (
        <div key={b.text} className={cx("flex items-center gap-2.5 px-3 min-h-11 py-2 rounded-ctl text-meta q-in", b.tone === "danger" ? "bg-danger-soft text-danger" : "bg-warn-soft text-warn")}>
          <IconWifiOff size={18} className="shrink-0" />
          <span className="flex-1">{b.text}</span>
          {b.action && (
            <button onClick={onFix} className="font-semibold h-8 px-2 shrink-0">
              {b.action}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
