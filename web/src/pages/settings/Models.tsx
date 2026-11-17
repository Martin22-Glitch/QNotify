// 设置 · 模型：上半「分工」（判定/对话各用哪个），下半「已配置的模型」（横条卡 + 添加/编辑/删除/测试）。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { humanError } from "../../lib/api";
import {
  LLM_PROFILES_KEY,
  LLM_SELECTION_KEY,
  llmApi,
  type LlmProfile,
  type LlmProvider,
  type LlmRole,
  type LlmSelection,
  type LlmTestResult,
  type ProfileInput,
} from "../../lib/llm";
import { toast } from "../../lib/toast";
import { IconCheck, IconRight, IconSpark } from "../../components/icons";
import { Button, cx, Dot, Empty, Field, Input, SectionHead, Segmented, Sheet, Skeleton, Tag } from "../../components/ui";

const PROVIDER_LABEL: Record<LlmProvider, string> = { openai: "OpenAI 兼容", anthropic: "Anthropic" };
const ROLE_LABEL: Record<LlmRole, string> = { judge: "判定", chat: "对话" };
const ROLES: LlmRole[] = ["judge", "chat"];

type Fresh = LlmTestResult & { at: number };

/* ── 小工具 ── */
function parseTime(s?: string | null): number | null {
  if (!s) return null;
  const t = Date.parse(s.includes("T") ? s : s.replace(" ", "T"));
  return Number.isNaN(t) ? null : t;
}
function ago(t: number): string {
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  const d = new Date(t);
  return `${d.getMonth() + 1}-${d.getDate()}`;
}
function shortUrl(u: string): string {
  return u.replace(/^https?:\/\//, "").replace(/\/$/, "");
}
function errText(e?: string | null): string {
  if (!e) return "未知错误";
  if (e === "not_configured") return "缺地址、模型名或密钥";
  return e;
}
/** 删除默认档案后，后端会让「剩下最新的一条」成为默认 */
function nextDefault(list: LlmProfile[], removing: LlmProfile): LlmProfile | null {
  if (!removing.active) return list.find((p) => p.active) ?? null;
  const rest = list.filter((p) => p.id !== removing.id);
  rest.sort((a, b) => (parseTime(b.created_at) ?? 0) - (parseTime(a.created_at) ?? 0) || b.id - a.id);
  return rest[0] ?? null;
}
function joinRoles(rs: LlmRole[]): string {
  return rs.map((r) => ROLE_LABEL[r]).join("、");
}

export function ModelsPanel() {
  const qc = useQueryClient();
  const profiles = useQuery({ queryKey: LLM_PROFILES_KEY, queryFn: llmApi.list });
  const selection = useQuery({ queryKey: LLM_SELECTION_KEY, queryFn: llmApi.selection });

  const [editing, setEditing] = useState<LlmProfile | "new" | null>(null);
  const [deleting, setDeleting] = useState<LlmProfile | null>(null);
  const [picking, setPicking] = useState<LlmRole | null>(null);
  const [testing, setTesting] = useState<Record<number, boolean>>({});
  const [fresh, setFresh] = useState<Record<number, Fresh>>({});

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: LLM_PROFILES_KEY });
    qc.invalidateQueries({ queryKey: LLM_SELECTION_KEY });
    qc.invalidateQueries({ queryKey: ["settings"] });
    qc.invalidateQueries({ queryKey: ["pipeline"] });
  };
  const dropFresh = (id: number) =>
    setFresh((f) => {
      const n = { ...f };
      delete n[id];
      return n;
    });

  const runTest = async (id: number) => {
    setTesting((t) => ({ ...t, [id]: true }));
    try {
      const r = await llmApi.test(id);
      setFresh((f) => ({ ...f, [id]: { ...r, at: Date.now() } }));
    } catch (e) {
      setFresh((f) => ({ ...f, [id]: { ok: false, error: humanError(e), at: Date.now() } }));
    } finally {
      setTesting((t) => {
        const n = { ...t };
        delete n[id];
        return n;
      });
      qc.invalidateQueries({ queryKey: LLM_PROFILES_KEY });
    }
  };

  const pick = useMutation({
    mutationFn: (patch: Partial<Record<LlmRole, number | null>>) => llmApi.putSelection(patch),
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: LLM_SELECTION_KEY });
      const prev = qc.getQueryData<LlmSelection>(LLM_SELECTION_KEY);
      if (prev) qc.setQueryData<LlmSelection>(LLM_SELECTION_KEY, { ...prev, ...patch });
      return { prev };
    },
    onSuccess: (r) => {
      qc.setQueryData(LLM_SELECTION_KEY, r);
      qc.invalidateQueries({ queryKey: ["pipeline"] });
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(LLM_SELECTION_KEY, ctx.prev);
      toast(humanError(e), undefined, "error");
    },
  });

  const activate = useMutation({
    mutationFn: (id: number) => llmApi.activate(id),
    onSuccess: (p) => toast(`默认改为「${p.name}」`),
    onError: (e) => toast(humanError(e), undefined, "error"),
    onSettled: refreshAll,
  });

  const remove = useMutation({
    mutationFn: (id: number) => llmApi.remove(id),
    onSuccess: (_r, id) => {
      toast("已删除");
      dropFresh(id);
      setDeleting(null);
    },
    onError: (e) => toast(humanError(e), undefined, "error"),
    onSettled: refreshAll,
  });

  if (profiles.isPending)
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-28" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
    );
  if (profiles.isError)
    return (
      <div className="flex items-center gap-3 text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2">
        <span className="flex-1">模型列表加载失败：{humanError(profiles.error)}</span>
        <Button size="sm" tone="plain" onClick={() => profiles.refetch()}>
          重试
        </Button>
      </div>
    );

  const list = [...(profiles.data ?? [])].sort((a, b) => a.id - b.id);
  const sel = selection.data;
  const def = list.find((p) => p.active) ?? null;
  const effective = (r: LlmRole): number | null => (sel ? sel[r] ?? def?.id ?? null : null);
  const usesOf = (p: LlmProfile) => ROLES.filter((r) => effective(r) === p.id);

  const editor = editing && (
    <ProfileSheet
      key={editing === "new" ? "new" : editing.id}
      profile={editing === "new" ? null : editing}
      isFirst={list.length === 0}
      onClose={() => setEditing(null)}
      onSaved={(p, connChanged) => {
        setEditing(null);
        refreshAll();
        if (connChanged) {
          dropFresh(p.id);
          runTest(p.id);
        }
      }}
    />
  );

  if (list.length === 0)
    return (
      <>
        <Empty
          icon={<IconSpark />}
          title="还没有配置模型"
          sub="判定群消息和回答你的提问都要用到模型，先添加一个。"
          action={
            <Button tone="primary" onClick={() => setEditing("new")}>
              添加模型
            </Button>
          }
        />
        {editor}
      </>
    );

  return (
    <div className="flex flex-col gap-6">
      <section>
        <SectionHead
          title="分工"
          help="判定会处理每一块群消息，量大，适合便宜、快的模型；对话只在你提问时调用，可以用更好的。选「跟随默认」时会跟着默认模型走，换默认就一起换。改动立即生效。"
        />
        <div className="rounded-ctl border border-line divide-y divide-line">
          {ROLES.map((r) => (
            <RoleRow key={r} role={r} sel={sel} list={list} def={def} onOpen={() => setPicking(r)} />
          ))}
        </div>
      </section>

      <section>
        <SectionHead
          title="已配置的模型"
          count={list.length}
          help="「默认」是判定或对话没有单独指定时用的模型，始终只有一个。「判定」「对话」标签表示它此刻正在做哪件事。密钥只存在后端，不会回显。"
          right={
            <Button size="sm" tone="soft" onClick={() => setEditing("new")}>
              添加模型
            </Button>
          }
        />
        <div className="flex flex-col gap-2">
          {list.map((p) => (
            <ProfileRow
              key={p.id}
              p={p}
              uses={usesOf(p)}
              testing={!!testing[p.id]}
              fresh={fresh[p.id]}
              activating={activate.isPending && activate.variables === p.id}
              onActivate={() => activate.mutate(p.id)}
              onTest={() => runTest(p.id)}
              onEdit={() => setEditing(p)}
              onDelete={() => setDeleting(p)}
            />
          ))}
        </div>
      </section>

      {picking && (
        <RolePicker
          role={picking}
          sel={sel}
          list={list}
          def={def}
          onClose={() => setPicking(null)}
          onPick={(id) => {
            const r = picking;
            setPicking(null);
            if (!sel || sel[r] === id) return;
            pick.mutate(
              { [r]: id },
              {
                onSuccess: () =>
                  toast(id === null ? `${ROLE_LABEL[r]}改为跟随默认` : `${ROLE_LABEL[r]}改用「${list.find((x) => x.id === id)?.name ?? id}」`),
              },
            );
          }}
        />
      )}

      {deleting && (
        <DeleteSheet
          p={deleting}
          uses={usesOf(deleting)}
          next={nextDefault(list, deleting)}
          busy={remove.isPending}
          onClose={() => !remove.isPending && setDeleting(null)}
          onConfirm={() => remove.mutate(deleting.id)}
        />
      )}

      {editor}
    </div>
  );
}

/* ── 分工：一行一个用途 ── */
function RoleRow({
  role,
  sel,
  list,
  def,
  onOpen,
}: {
  role: LlmRole;
  sel?: LlmSelection;
  list: LlmProfile[];
  def: LlmProfile | null;
  onOpen: () => void;
}) {
  const id = sel ? sel[role] : null;
  const p = id != null ? list.find((x) => x.id === id) ?? null : def;
  return (
    <button
      onClick={onOpen}
      disabled={!sel}
      className="w-full min-h-14 px-3 py-2 flex items-center gap-3 text-left hover:bg-surface-2 transition first:rounded-t-ctl last:rounded-b-ctl"
    >
      <span className="w-10 shrink-0 text-meta font-medium text-ink-2">{ROLE_LABEL[role]}</span>
      <span className="flex-1 min-w-0 flex items-center gap-2">
        {!sel ? (
          <span className="text-meta text-ink-3">加载中…</span>
        ) : id == null ? (
          <>
            <Tag tone="muted">跟随默认</Tag>
            <span className="text-body text-ink-2 truncate">{def?.name ?? "暂无默认"}</span>
          </>
        ) : (
          <span className="text-body text-ink font-medium truncate">{p?.name ?? `#${id}`}</span>
        )}
      </span>
      {p && <HealthDot p={p} />}
      <IconRight size={18} className="text-ink-3 shrink-0" />
    </button>
  );
}

function HealthDot({ p }: { p: LlmProfile }) {
  if (p.last_test_ok === true) return <Dot tone="ok" />;
  if (p.last_test_ok === false) return <Dot tone="danger" />;
  return null;
}

function RolePicker({
  role,
  sel,
  list,
  def,
  onPick,
  onClose,
}: {
  role: LlmRole;
  sel?: LlmSelection;
  list: LlmProfile[];
  def: LlmProfile | null;
  onPick: (id: number | null) => void;
  onClose: () => void;
}) {
  const cur = sel ? sel[role] : null;
  const opts: { id: number | null; title: string; sub: string; p?: LlmProfile }[] = [
    { id: null, title: "跟随默认", sub: def ? `当前默认：${def.name}` : "暂无默认" },
    ...list.map((p) => ({ id: p.id, title: p.name, sub: `${PROVIDER_LABEL[p.provider]} · ${p.model}`, p })),
  ];
  return (
    <Sheet open onClose={onClose} title={`${ROLE_LABEL[role]}用哪个模型`}>
      <div className="flex flex-col gap-1 pb-2" role="radiogroup">
        {opts.map((o) => {
          const on = o.id === cur;
          return (
            <button
              key={o.id ?? "follow"}
              role="radio"
              aria-checked={on}
              onClick={() => onPick(o.id)}
              className={cx("w-full min-h-14 px-3 py-2 rounded-ctl flex items-center gap-3 text-left transition", on ? "bg-brand-soft" : "hover:bg-surface-2")}
            >
              <span className="flex-1 min-w-0">
                <span className={cx("flex items-center gap-1.5 text-body", on ? "text-brand font-semibold" : "text-ink")}>
                  <span className="truncate">{o.title}</span>
                  {o.p?.active && <Tag tone="brand">默认</Tag>}
                </span>
                <span className="block text-cap text-ink-3 truncate">{o.sub}</span>
              </span>
              {o.p && <HealthDot p={o.p} />}
              <span className="w-5 shrink-0 flex justify-center text-brand">{on && <IconCheck size={18} />}</span>
            </button>
          );
        })}
      </div>
    </Sheet>
  );
}

/* ── 横条卡 ── */
function ProfileRow({
  p,
  uses,
  testing,
  fresh,
  activating,
  onActivate,
  onTest,
  onEdit,
  onDelete,
}: {
  p: LlmProfile;
  uses: LlmRole[];
  testing: boolean;
  fresh?: Fresh;
  activating: boolean;
  onActivate: () => void;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="rounded-ctl border border-line px-3 py-2.5 flex flex-wrap items-center gap-x-4 gap-y-2">
      <div className="flex-1 min-w-0 basis-64">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-body font-medium text-ink truncate">{p.name}</span>
          {p.active && <Tag tone="brand">默认</Tag>}
          {uses.map((r) => (
            <Tag key={r} tone="muted">
              {ROLE_LABEL[r]}
            </Tag>
          ))}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-cap text-ink-3 min-w-0">
          <span className="shrink-0">{PROVIDER_LABEL[p.provider]}</span>
          {p.model !== p.name && (
            <>
              <span className="shrink-0">·</span>
              <span className="shrink-0 max-w-[40%] truncate">{p.model}</span>
            </>
          )}
          <span className="shrink-0">·</span>
          <span className="truncate" title={p.base_url}>
            {shortUrl(p.base_url)}
          </span>
          {!p.api_key_set && <span className="shrink-0">· 无密钥</span>}
        </div>
        <TestLine p={p} testing={testing} fresh={fresh} />
      </div>
      <div className="flex items-center gap-1.5 ml-auto flex-wrap justify-end">
        {!p.active && (
          <Button size="sm" tone="ghost" loading={activating} onClick={onActivate}>
            设为默认
          </Button>
        )}
        <Button size="sm" tone="plain" loading={testing} onClick={onTest}>
          测试
        </Button>
        <Button size="sm" tone="ghost" onClick={onEdit}>
          编辑
        </Button>
        <Button size="sm" tone="danger" onClick={onDelete}>
          删除
        </Button>
      </div>
    </div>
  );
}

function TestLine({ p, testing, fresh }: { p: LlmProfile; testing: boolean; fresh?: Fresh }) {
  const base = "mt-1 flex items-center gap-1.5 text-cap min-w-0";
  if (testing)
    return (
      <div className={cx(base, "text-brand")} aria-live="polite">
        <Dot tone="brand" pulse />
        测试中…
      </div>
    );
  const ok = fresh ? fresh.ok : p.last_test_ok ?? null;
  const at = fresh ? fresh.at : parseTime(p.last_test_at);
  const err = fresh ? fresh.error : p.last_test_error;
  if (ok === null)
    return (
      <div className={cx(base, "text-ink-3")}>
        <Dot tone="muted" />
        未测试
      </div>
    );
  if (ok)
    return (
      <div className={cx(base, "text-ok")} aria-live="polite">
        <Dot tone="ok" />
        <span>连通</span>
        {fresh?.latency_ms != null && <span className="q-num">· {fresh.latency_ms} ms</span>}
        {at != null && <span className="text-ink-3">· {ago(at)}</span>}
      </div>
    );
  return (
    <div className={cx(base, "text-danger")} aria-live="polite">
      <Dot tone="danger" />
      <span className="truncate" title={errText(err)}>
        失败 · {errText(err)}
      </span>
      {at != null && <span className="shrink-0 text-ink-3">· {ago(at)}</span>}
    </div>
  );
}

/* ── 删除确认 ── */
function DeleteSheet({
  p,
  uses,
  next,
  busy,
  onClose,
  onConfirm,
}: {
  p: LlmProfile;
  uses: LlmRole[];
  next: LlmProfile | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Sheet
      open
      onClose={onClose}
      title={`删除「${p.name}」？`}
      footer={
        <>
          <Button tone="ghost" className="flex-1" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button tone="danger" className="flex-1" loading={busy} onClick={onConfirm}>
            删除
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2 text-meta text-ink-2 q-pretty">
        {p.active && <p>{next ? `它是默认模型，删除后默认将转给「${next.name}」。` : "这是最后一个模型。"}</p>}
        {uses.length > 0 && (
          <p className="rounded-ctl bg-warn-soft text-warn px-3 py-2">
            {next ? `${joinRoles(uses)}正在用它，删除后将改用「${next.name}」。` : `删除后${joinRoles(uses)}将没有可用的模型。`}
          </p>
        )}
        <p>密钥会一并删除，无法恢复。</p>
      </div>
    </Sheet>
  );
}

/* ── 添加 / 编辑 ── */
function ProfileSheet({
  profile,
  isFirst,
  onClose,
  onSaved,
}: {
  profile: LlmProfile | null;
  isFirst: boolean;
  onClose: () => void;
  onSaved: (p: LlmProfile, connChanged: boolean) => void;
}) {
  const [provider, setProvider] = useState<LlmProvider>(profile?.provider ?? "openai");
  const [baseUrl, setBaseUrl] = useState(profile?.base_url ?? "");
  const [model, setModel] = useState(profile?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [name, setName] = useState(profile && profile.name !== profile.model ? profile.name : "");
  const [tried, setTried] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const finalName = name.trim() || model.trim();
  const patch: Partial<ProfileInput> = {};
  if (profile) {
    if (provider !== profile.provider) patch.provider = provider;
    if (baseUrl.trim() !== profile.base_url) patch.base_url = baseUrl.trim();
    if (model.trim() !== profile.model) patch.model = model.trim();
    if (apiKey) patch.api_key = apiKey;
    if (finalName !== profile.name) patch.name = finalName;
  }
  const dirty = !profile || Object.keys(patch).length > 0;
  const urlErr = tried && !baseUrl.trim() ? "请填写接口地址" : null;
  const modelErr = tried && !model.trim() ? "请填写模型名" : null;

  const submit = async () => {
    setTried(true);
    if (!baseUrl.trim() || !model.trim()) return;
    setSaving(true);
    setErr(null);
    try {
      if (profile) {
        const p = await llmApi.patch(profile.id, patch);
        const connChanged = ["provider", "base_url", "model", "api_key"].some((k) => k in patch);
        toast("已保存");
        onSaved(p, connChanged);
      } else {
        const body: ProfileInput = { provider, base_url: baseUrl.trim(), model: model.trim() };
        if (apiKey) body.api_key = apiKey;
        if (name.trim()) body.name = name.trim();
        const p = await llmApi.create(body);
        toast(isFirst ? "已添加，并设为默认" : "已添加");
        onSaved(p, true);
      }
    } catch (e) {
      setErr(humanError(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Sheet
      open
      onClose={() => !saving && onClose()}
      title={profile ? `编辑「${profile.name}」` : "添加模型"}
      footer={
        <>
          <Button tone="ghost" disabled={saving} onClick={onClose}>
            取消
          </Button>
          <Button tone="primary" className="flex-1" disabled={!dirty} loading={saving} onClick={submit}>
            {profile ? "保存" : "添加"}
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4 pt-1"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Field label="协议">
          <Segmented
            value={provider}
            onChange={setProvider}
            items={[
              { value: "openai", label: "OpenAI 兼容" },
              { value: "anthropic", label: "Anthropic" },
            ]}
          />
        </Field>
        <Field label="接口地址" help="OpenAI 兼容服务一般以 /v1 结尾；本地 Ollama 如 http://127.0.0.1:11434/v1" error={urlErr}>
          <Input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={provider === "anthropic" ? "https://api.anthropic.com" : "https://api.deepseek.com/v1"}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
          />
        </Field>
        <Field label="模型名" error={modelErr}>
          <Input value={model} onChange={(e) => setModel(e.target.value)} autoCapitalize="off" autoCorrect="off" spellCheck={false} />
        </Field>
        <Field
          label="密钥"
          help={profile ? "密钥只存在后端，这里永远不显示。留空 = 不修改。" : "本地模型（如 Ollama）可以不填。保存后不会再显示。"}
        >
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={profile ? (profile.api_key_set ? "已设置 · 留空不修改" : "未设置") : ""}
            autoComplete="new-password"
          />
        </Field>
        <Field label="名字" help="在列表和分工里显示的名字，留空就用模型名。">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={model.trim() || "默认用模型名"} />
        </Field>
        {err && <div className="text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2 break-all">{err}</div>}
        <button type="submit" hidden />
      </form>
    </Sheet>
  );
}
