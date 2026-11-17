// 设置：一次性配置，收在齿轮里。分区：接入状态 / 我的偏好（关于我·我关注的·不看的） / 白名单 / 模型 / 判定参数（含保留期） / 连接 / 外观。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { api, humanError } from "../../lib/api";
import { connStore } from "../../lib/config";
import { LLM_PROFILES_KEY, llmApi } from "../../lib/llm";
import { prefsStore, type Prefs } from "../../lib/prefs";
import { usePipeline, useStats } from "../../lib/queries";
import { rtStore } from "../../lib/realtime";
import { useStore } from "../../lib/store";
import { toast } from "../../lib/toast";
import type { Settings } from "../../lib/types";
import { IconLeft, IconRight } from "../../components/icons";
import { Button, cx, Dot, Field, Help, Input, Segmented, Switch } from "../../components/ui";
import { testConnection } from "../ConnectPage";
import { PreferencesPanel, type PrefBlock } from "./Preferences";
import { WhitelistPanel } from "./Whitelist";
import { ModelsPanel } from "./Models";
import { RetentionSection } from "./Retention";
import { useWide } from "../../lib/hooks";

type Sec = "status" | "prefs" | "whitelist" | "model" | "judge" | "conn" | "look";
/**  前的分区 id：外部入口可能还在传，统一落到「我的偏好」并滚到对应块 */
type LegacySec = "me" | "focus" | "rules";
const LEGACY: Record<LegacySec, PrefBlock> = { me: "about", focus: "focus", rules: "ignore" };

const SECS: { id: Sec; label: string }[] = [
  { id: "status", label: "接入状态" },
  { id: "conn", label: "连接" },
  { id: "whitelist", label: "白名单" },
  { id: "prefs", label: "我的偏好" },
  { id: "judge", label: "判定参数" },
  { id: "model", label: "模型" },
  { id: "look", label: "外观" },
];

function resolveInitial(i?: Sec | LegacySec): { sec?: Sec; jump?: PrefBlock } {
  if (!i) return {};
  if (i in LEGACY) return { sec: "prefs", jump: LEGACY[i as LegacySec] };
  return { sec: i as Sec };
}

export function SettingsPage({ initial }: { initial?: Sec | LegacySec }) {
  const wide = useWide();
  const init = resolveInitial(initial);
  const [sec, setSec] = useState<Sec | null>(init.sec ?? (wide ? "status" : null));
  const [jump, setJump] = useState<PrefBlock | undefined>(init.jump);
  const health = useHealthSummary();
  const go = (s: Sec | null) => {
    setJump(undefined);
    setSec(s);
  };

  const body = (s: Sec) => (
    <>
      {s === "status" && <StatusPanel />}
      {s === "prefs" && <PreferencesPanel jump={jump} />}
      {s === "whitelist" && <WhitelistPanel />}
      {s === "model" && <ModelsPanel />}
      {s === "judge" && <JudgePanel />}
      {s === "conn" && <ConnPanel />}
      {s === "look" && <LookPanel />}
    </>
  );

  if (wide) {
    return (
      <div className="grid grid-cols-[220px_minmax(0,1fr)] gap-6 items-start max-w-[980px]">
        <nav className="q-card p-2 sticky top-6">
          {SECS.map((s) => (
            <button
              key={s.id}
              onClick={() => go(s.id)}
              className={cx(
                "w-full h-10 px-3 rounded-ctl flex items-center gap-2 text-meta text-left",
                sec === s.id ? "bg-brand-soft text-brand font-semibold" : "text-ink-2 hover:bg-surface-2",
              )}
            >
              <span className="flex-1">{s.label}</span>
              {health[s.id] && <Dot tone={health[s.id]!} />}
            </button>
          ))}
        </nav>
        <section className="q-card p-6 min-w-0">
          <h2 className="text-h2 q-display mb-5">{SECS.find((x) => x.id === sec)?.label}</h2>
          {sec && body(sec)}
        </section>
      </div>
    );
  }

  if (sec) {
    return (
      <div>
        <button className="flex items-center gap-1 text-meta text-ink-2 h-10 -ml-1 mb-1" onClick={() => go(null)}>
          <IconLeft size={18} />
          设置
        </button>
        <h2 className="text-h2 q-display mb-4 px-1">{SECS.find((x) => x.id === sec)?.label}</h2>
        <div className="q-card q-pad">{body(sec)}</div>
      </div>
    );
  }
  return (
    <div>
      <h1 className="text-h2 q-display mb-4 px-1">设置</h1>
      <div className="q-card divide-y divide-line overflow-hidden">
        {SECS.map((s) => (
          <button key={s.id} onClick={() => go(s.id)} className="w-full h-14 px-4 flex items-center gap-3 text-left hover:bg-surface-2">
            <span className="flex-1 text-body">{s.label}</span>
            {health[s.id] && <Dot tone={health[s.id]!} />}
            <IconRight size={18} className="text-ink-3" />
          </button>
        ))}
      </div>
    </div>
  );
}

/** 各分区是否有异常（红/黄点），帮用户一眼找到要处理的地方 */
function useHealthSummary(): Partial<Record<Sec, "danger" | "warn" | "ok">> {
  const sc = useQuery({ queryKey: ["selfcheck"], queryFn: api.selfcheck, staleTime: 15000, retry: false });
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 30000 });
  const pf = useQuery({ queryKey: LLM_PROFILES_KEY, queryFn: llmApi.list, staleTime: 30000, retry: false });
  const pl = usePipeline();
  const checks = sc.data?.checks ?? [];
  const bad = (n: string) => checks.find((c) => c.name === n)?.ok === false;
  return {
    status: bad("napcat_connected") || bad("napcat_login") || bad("db_writable") ? "danger" : sc.data?.ok ? "ok" : undefined,
    whitelist: bad("whitelist_configured") ? "warn" : undefined,
    // llm_* 现在反映默认档案
    model: pl.data?.last_error
      ? "danger"
      : (pf.data && pf.data.length === 0) || (st.data && (!st.data.llm_api_key_set || !st.data.llm_model))
        ? "warn"
        : undefined,
  };
}

/* ── 接入状态 ── */
const CHECK_LABEL: Record<string, string> = {
  napcat_connected: "QQ 协议端",
  napcat_login: "QQ 登录",
  whitelist_configured: "白名单",
  backfill_enabled: "历史补采",
};

/** 不面向用户的自检项：库可写是开发者关心的事；adapter_* 与 napcat_* 说的是同一件事，只留一份 */
const CHECK_HIDDEN = new Set(["db_writable", "adapter_connected", "adapter_login"]);

function StatusPanel() {
  const qc = useQueryClient();
  const sc = useQuery({ queryKey: ["selfcheck"], queryFn: api.selfcheck, refetchInterval: 15000 });
  const stats = useStats();
  const pl = usePipeline();
  const rt = useStore(rtStore);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (k: string, fn: () => Promise<string>) => {
    setBusy(k);
    try {
      toast(await fn());
      qc.invalidateQueries();
    } catch (e) {
      toast(humanError(e), undefined, "error");
    } finally {
      setBusy(null);
    }
  };
  const s = stats.data;
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-3 gap-2">
        <Stat label="今日消息" v={s?.messages_today} />
        <Stat label="非白名单丢弃" v={s?.messages_dropped} help="不在白名单的消息直接丢弃，不花 token。" />
        <Stat label="模型调用" v={s?.llm_calls ?? pl.data?.llm_calls} />
      </div>
      <div className="flex flex-col divide-y divide-line rounded-ctl border border-line">
        <Row tone={rt.state === "live" ? "ok" : rt.state === "polling" ? "warn" : "danger"} label="实时通道" detail={rt.state === "live" ? "已连接" : rt.state === "polling" ? "重连中，每 8 秒自动拉取" : "未连接"} />
        {(sc.data?.checks ?? [])
          .filter((c) => !CHECK_HIDDEN.has(c.name) || !c.ok)
          .map((c) => (
            <Row
              key={c.name}
              tone={c.ok ? "ok" : c.name === "whitelist_configured" || c.name === "backfill_enabled" ? "warn" : "danger"}
              label={CHECK_LABEL[c.name] ?? c.name}
              detail={c.detail}
            />
          ))}
        {pl.data && (
          <Row
            tone={pl.data.last_error ? "danger" : pl.data.enabled ? "ok" : "warn"}
            label="判定队列"
            detail={
              pl.data.last_error
                ? `模型出错：${pl.data.last_error}`
                : !pl.data.enabled
                  ? "已暂停"
                  : pl.data.pending > 0
                    ? `运行中 · 有 ${pl.data.pending} 条消息在等 AI 判定`
                    : "运行中 · 没有积压"
            }
          />
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <Button tone="plain" loading={busy === "sync"} onClick={() => run("sync", async () => { const r = await api.sync(); return `同步完成：${r.groups} 群 / ${r.friends} 好友`; })}>
          同步群和好友
        </Button>
        <Button tone="plain" loading={busy === "backfill"} onClick={() => run("backfill", async () => { const r = await api.backfill(); return `补采完成：新增 ${r.inserted} 条`; })}>
          补采历史消息
        </Button>
        <Button tone="plain" loading={busy === "flush"} onClick={() => run("flush", async () => { const r = await api.flush(); return `已处理 ${r.processed} 块`; })}>
          立即处理
        </Button>
      </div>
    </div>
  );
}

function Stat({ label, v, help }: { label: string; v?: number; help?: string }) {
  return (
    <div className="rounded-ctl bg-surface-2 p-3">
      <div className="q-num text-h2 font-semibold">{v ?? "—"}</div>
      <div className="text-cap text-ink-3 flex items-center gap-1">
        {label}
        {help && <Help>{help}</Help>}
      </div>
    </div>
  );
}
function Row({ tone, label, detail }: { tone: "ok" | "warn" | "danger"; label: string; detail: string }) {
  return (
    <div className="flex items-center gap-3 px-3 min-h-12 py-2">
      <Dot tone={tone} />
      <span className="text-meta font-medium w-24 shrink-0 truncate" title={label}>{label}</span>
      <span className="flex-1 text-cap text-ink-2 break-all">{detail}</span>
    </div>
  );
}

/* ── 设置草稿（判定参数用；模型已迁到 ./Models） ── */
function useSettingsDraft() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [d, setD] = useState<Settings>({});
  useEffect(() => {
    if (q.data) setD(q.data);
  }, [q.data]);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = async (keys: (keyof Settings)[]) => {
    setSaving(true);
    setErr(null);
    try {
      const body: Partial<Settings> = {};
      for (const k of keys) if (d[k] !== q.data?.[k]) (body as Record<string, unknown>)[k] = d[k];
      const r = await api.putSettings(body);
      qc.setQueryData(["settings"], r);
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      toast("已保存");
      return true;
    } catch (e) {
      setErr(humanError(e));
      return false;
    } finally {
      setSaving(false);
    }
  };
  const dirty = (keys: (keyof Settings)[]) => keys.some((k) => d[k] !== q.data?.[k]);
  return { q, d, setD, save, saving, err, dirty };
}

/* ── 判定参数 ── */
const JUDGE_KEYS: (keyof Settings)[] = ["pipeline_enabled", "confidence_threshold", "aggregate_window_seconds", "notify_review_queue", "backfill_enabled", "backfill_count"];
function JudgePanel() {
  const { d, setD, save, saving, err, dirty, q } = useSettingsDraft();
  const m2 = q.data && "pipeline_enabled" in q.data;
  return (
    <div className="flex flex-col gap-5">
      {m2 && (
        <>
          <ToggleRow label="自动判定" help="关闭后消息照常入库，但不会建待办或待确认。" v={!!d.pipeline_enabled} on={(v) => setD({ ...d, pipeline_enabled: v })} />
          <Field label={`置信度阈值 · ${Math.round((d.confidence_threshold ?? 0.7) * 100)}%`} help="AI 把握高于阈值 → 直接建待办；低于 → 进待确认让你决定。调高 = 更谨慎、待确认更多。">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={d.confidence_threshold ?? 0.7}
              onChange={(e) => setD({ ...d, confidence_threshold: Number(e.target.value) })}
              className="w-full accent-[var(--q-brand)] h-10"
            />
          </Field>
          <Field label="聚合窗口（秒）" help="同一个人在这段时间内连发的多条消息合并成一块再判定。0~600。">
            <Input type="number" min={0} max={600} value={d.aggregate_window_seconds ?? 45} onChange={(e) => setD({ ...d, aggregate_window_seconds: Number(e.target.value) })} />
          </Field>

          {d.timezone && (
            <div className="flex items-center justify-between text-meta">
              <span className="text-ink-2">时区</span>
              <span className="q-num">{d.timezone}</span>
            </div>
          )}
        </>
      )}
      <ToggleRow label="启动时补采历史" v={!!d.backfill_enabled} on={(v) => setD({ ...d, backfill_enabled: v })} />
      <Field label="每群补采条数">
        <Input type="number" min={0} value={d.backfill_count ?? 50} onChange={(e) => setD({ ...d, backfill_count: Number(e.target.value) })} />
      </Field>
      {err && <div className="text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2 break-words">{err}</div>}
      <Button tone="primary" disabled={!dirty(JUDGE_KEYS)} loading={saving} onClick={() => save(JUDGE_KEYS)}>
        保存
      </Button>
      {/* 保留期独立保存、独立二次确认，不和上面的判定参数混在一次提交里 */}
      <RetentionSection />
    </div>
  );
}
function ToggleRow({ label, help, v, on }: { label: string; help?: ReactNode; v: boolean; on: (v: boolean) => void }) {
  return (
    <div className="flex items-center gap-2 min-h-11">
      <span className="text-body flex-1 flex items-center gap-1.5">
        {label}
        {help && <Help>{help}</Help>}
      </span>
      <Switch checked={v} onChange={on} label={label} />
    </div>
  );
}

/* ── 连接 ── */
function ConnPanel() {
  const conn = useStore(connStore);
  const [base, setBase] = useState(conn.baseUrl);
  const [token, setToken] = useState(conn.token);
  const [res, setRes] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    const r = await testConnection(base, token.trim());
    setBusy(false);
    setRes(r);
    if (r?.ok) connStore.set({ baseUrl: base.trim(), token: token.trim(), invalid: false });
  };
  return (
    <div className="flex flex-col gap-4">
      <Field label="服务地址" help="留空 = 当前站点。手机访问时填电脑的局域网地址。">
        <Input value={base} onChange={(e) => setBase(e.target.value)} placeholder="留空 = 当前站点" autoCapitalize="off" />
      </Field>
      <Field label="访问令牌">
        <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoCapitalize="off" />
      </Field>
      {res && <div className={cx("text-meta", res.ok ? "text-ok" : "text-danger")}>{res.text}</div>}
      <div className="flex gap-2">
        <Button tone="primary" className="flex-1" loading={busy} onClick={run}>
          测试并保存
        </Button>
        <Button tone="danger" onClick={() => connStore.set({ baseUrl: conn.baseUrl, token: "", invalid: false })}>
          断开
        </Button>
      </div>
    </div>
  );
}

/* ── 外观 ── */
function LookPanel() {
  const p = useStore(prefsStore);
  const set = (patch: Partial<Prefs>) => prefsStore.set({ ...p, ...patch });
  return (
    <div className="flex flex-col gap-5">
      <Field label="风格">
        <Segmented value={p.dir} onChange={(v) => set({ dir: v })} items={[{ value: "sky", label: "晴空" }, { value: "ink", label: "墨笺" }]} />
      </Field>
      <Field label="深浅色">
        <Segmented value={p.mode} onChange={(v) => set({ mode: v })} items={[{ value: "light", label: "浅色" }, { value: "dark", label: "深色" }, { value: "auto", label: "跟随系统" }]} />
      </Field>
      <Field label="今日概览">
        <Segmented value={p.urgency} onChange={(v) => set({ urgency: v })} items={[{ value: "focus", label: "焦点卡" }, { value: "countdown", label: "48h 时间轴" }]} />
      </Field>
      <Field label="密度">
        <Segmented value={p.density} onChange={(v) => set({ density: v })} items={[{ value: "cozy", label: "舒展" }, { value: "compact", label: "紧凑" }]} />
      </Field>
    </div>
  );
}
