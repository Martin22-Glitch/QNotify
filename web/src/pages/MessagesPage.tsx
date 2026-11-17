// 消息：按判定结果分类（转待办 / 待决定 / 已忽略 / 未判定），群筛选只列有消息的群。
// 判定标签可改：点标签弹出改判菜单；手机上左右滑动；详情底部也有按钮。
import { useMemo, useState } from "react";
import { useWide } from "../lib/hooks";
import { useMessages, useMsgGroups, useResolveReview, useSetVerdict, useVerdictIndex, type MsgVerdict } from "../lib/queries";
import { fromLocalInput, msgTime, toLocalInput } from "../lib/time";
import type { Message } from "../lib/types";
import { DueText, QuoteBlock, useFresh, VERDICT_META, VerdictTag, type VClass } from "../components/bits";
import { IconEyeOff, IconInbox, IconTodo, IconUsers } from "../components/icons";
import { Button, cx, Empty, Field, Help, Input, Segmented, Sheet, Skeleton, SwipeRow, Tag } from "../components/ui";

type Seg = Exclude<VClass, "none"> | "none";

export function MessagesPage({ go }: { go: (t: "review" | "todos") => void }) {
  const wide = useWide();
  const groups = useMsgGroups();
  const [gid, setGid] = useState<number | null | "all">("all");
  const msgs = useMessages(gid === null ? "all" : gid);
  const verdictOf = useVerdictIndex();
  const [seg, setSeg] = useState<Seg>("todo");
  const [detail, setDetail] = useState<Message | null>(null);
  const [convert, setConvert] = useState<Message | null>(null);
  const [menu, setMenu] = useState<Message | null>(null);
  const setVerdict = useSetVerdict();
  const resolve = useResolveReview();

  const flat = useMemo(() => {
    const all = msgs.data?.pages.flat() ?? [];
    return gid === null ? all.filter((m) => m.message_type === "private" || m.group_id === null) : all;
  }, [msgs.data, gid]);

  const classified = useMemo(() => {
    const out: Record<Seg, { m: Message; v: MsgVerdict }[]> = { todo: [], review: [], ignored: [], none: [] };
    for (const m of flat) {
      const v = verdictOf(m.msg_id);
      out[v.cls].push({ m, v });
    }
    return out;
  }, [flat, verdictOf]);

  const ignore = (m: Message, v: MsgVerdict) => {
    if (v.review) resolve.mutate({ item: v.review, action: "ignore" });
    else setVerdict.mutate({ msgId: m.msg_id, verdict: "ignored" });
  };

  const rows = classified[seg];

  return (
    <div className={cx(wide && "grid grid-cols-[220px_minmax(0,1fr)] gap-6 items-start")}>
      {/* 群筛选：手机横滑 chips；桌面左侧列表 */}
      {wide ? (
        <aside className="sticky top-6 q-card p-2">
          <div className="px-2 pt-1 pb-2 flex items-center gap-1.5 text-cap text-ink-3 font-medium">
            有消息的群
            <Help>只列出确实收到过消息的群和私聊。要接入新群，到设置 → 白名单。</Help>
          </div>
          <GroupItem active={gid === "all"} name="全部" n={groups.data?.reduce((a, g) => a + g.message_count, 0)} onClick={() => setGid("all")} />
          {groups.data?.map((g) => (
            <GroupItem key={String(g.group_id)} active={gid === g.group_id} name={g.group_name} n={g.message_count} onClick={() => setGid(g.group_id)} />
          ))}
        </aside>
      ) : null}

      <div className="flex flex-col q-gap min-w-0">
        <div className="flex items-center gap-2 px-1">
          <h1 className="text-h2 q-display">消息</h1>
          <Help>
            系统收到的所有消息都在这里，按 AI 的判定分成四类。判定标签可以改：点标签，或在手机上左右滑动。改判会记成学习样本，不会再花 token。
          </Help>
        </div>

        {!wide && (
          <div className="flex gap-1.5 q-scroll-x -mx-4 px-4">
            <Chip active={gid === "all"} onClick={() => setGid("all")}>
              全部
            </Chip>
            {groups.data?.map((g) => (
              <Chip key={String(g.group_id)} active={gid === g.group_id} onClick={() => setGid(g.group_id)}>
                {g.group_name}
                <span className="q-num opacity-60 ml-1">{g.message_count}</span>
              </Chip>
            ))}
          </div>
        )}

        <Segmented
          value={seg}
          onChange={setSeg}
          items={[
            { value: "todo", label: "转待办", count: classified.todo.length, tone: "brand" },
            { value: "review", label: "待决定", count: classified.review.length, tone: "warn" },
            { value: "ignored", label: "已忽略", count: classified.ignored.length, tone: "muted" },
            ...(classified.none.length ? [{ value: "none" as Seg, label: "未判定", count: classified.none.length }] : []),
          ]}
        />

        {seg === "review" && classified.review.length > 0 && (
          <button onClick={() => go("review")} className="text-left text-meta text-ink-2 px-1 flex items-center gap-1.5">
            <IconInbox size={15} className="text-warn" />
            带 AI 建议逐条处理 → <span className="text-brand font-medium">去待确认</span>
          </button>
        )}

        {msgs.isLoading ? (
          <div className="flex flex-col q-gap">
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
          </div>
        ) : rows.length === 0 ? (
          <div className="q-card">
            <EmptySeg seg={seg} total={flat.length} />
          </div>
        ) : (
          <div className="flex flex-col q-gap">
            {rows.map(({ m, v }) => (
              <SwipeRow
                key={m.id}
                right={
                  v.cls !== "todo"
                    ? { label: "转为待办", icon: <IconTodo size={18} />, tone: "brand", run: () => setConvert(m) }
                    : undefined
                }
                left={
                  v.cls !== "ignored"
                    ? { label: "标为忽略", icon: <IconEyeOff size={18} />, tone: "muted", run: () => ignore(m, v) }
                    : undefined
                }
              >
                <MsgRow m={m} v={v} onOpen={() => setDetail(m)} onTag={() => setMenu(m)} />
              </SwipeRow>
            ))}
          </div>
        )}

        {msgs.hasNextPage && (
          <Button tone="plain" onClick={() => msgs.fetchNextPage()} loading={msgs.isFetchingNextPage} className="self-center mt-2">
            加载更早的消息
          </Button>
        )}
      </div>

      {/* 改判菜单 */}
      <Sheet open={!!menu} onClose={() => setMenu(null)} title="改判">
        {menu && (
          <RelabelMenu
            v={verdictOf(menu.msg_id)}
            onTodo={() => {
              setConvert(menu);
              setMenu(null);
            }}
            onIgnore={() => {
              ignore(menu, verdictOf(menu.msg_id));
              setMenu(null);
            }}
            onReview={() => go("review")}
          />
        )}
      </Sheet>

      {/* 详情 */}
      <Sheet open={!!detail} onClose={() => setDetail(null)} title="消息详情">
        {detail && (
          <MsgDetail
            m={detail}
            v={verdictOf(detail.msg_id)}
            groupName={groupNameOf(groups.data, detail)}
            onTodo={() => {
              setConvert(detail);
              setDetail(null);
            }}
            onIgnore={() => {
              ignore(detail, verdictOf(detail.msg_id));
              setDetail(null);
            }}
            onGoTodo={() => go("todos")}
          />
        )}
      </Sheet>

      {/* 转为待办（预填原句） */}
      <ConvertSheet
        m={convert}
        v={convert ? verdictOf(convert.msg_id) : null}
        onClose={() => setConvert(null)}
        onSubmit={(title, due) => {
          if (!convert) return;
          const v = verdictOf(convert.msg_id);
          if (v.review) resolve.mutate({ item: v.review, action: "confirm", title, due_at: due });
          else setVerdict.mutate({ msgId: convert.msg_id, verdict: "todo", title, due_at: due });
          setConvert(null);
        }}
      />
    </div>
  );
}

function groupNameOf(list: { group_id: number | null; group_name: string }[] | undefined, m: Message): string {
  if (m.group_id === null || m.message_type === "private") return "私聊";
  return list?.find((g) => g.group_id === m.group_id)?.group_name ?? `群 ${m.group_id}`;
}

function GroupItem({ active, name, n, onClick }: { active: boolean; name: string; n?: number; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "w-full h-10 px-2.5 rounded-ctl flex items-center gap-2 text-meta text-left transition",
        active ? "bg-brand-soft text-brand font-semibold" : "text-ink-2 hover:bg-surface-2",
      )}
    >
      <span className="flex-1 truncate">{name}</span>
      {n !== undefined && <span className="q-num text-cap opacity-70">{n}</span>}
    </button>
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "shrink-0 h-9 px-3.5 rounded-full text-meta font-medium transition whitespace-nowrap",
        active ? "bg-ink text-bg" : "bg-surface text-ink-2 border border-line",
      )}
    >
      {children}
    </button>
  );
}

function MsgRow({ m, v, onOpen, onTag }: { m: Message; v: MsgVerdict; onOpen: () => void; onTag: () => void }) {
  const fresh = useFresh(`msg:${m.msg_id}`);
  const name = m.sender_name || String(m.user_id);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === "Enter" && onOpen()}
      className={cx("q-card q-pad flex gap-3 cursor-pointer", fresh && "q-fresh")}
    >
      <Avatar name={name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-meta font-semibold truncate">{name}</span>
          <span className="text-cap text-ink-3 q-num shrink-0">{msgTime(m.time)}</span>
          {fresh && <Tag tone="brand">新</Tag>}
          <span className="flex-1" />
          <VerdictTag cls={v.cls} manual={v.manual} onClick={onTag} />
        </div>
        <div className="mt-1 text-body text-ink q-clamp-2 break-words">{m.text}</div>
        {(v.reason || v.todo) && (
          <div className="mt-1.5 flex items-center gap-2 text-cap text-ink-3 min-w-0">
            {v.todo && v.cls === "todo" ? (
              <>
                <IconTodo size={13} className="text-brand shrink-0" />
                <span className="truncate text-ink-2">{v.todo.title}</span>
                <DueText due={v.todo.due_at} className="text-cap shrink-0" />
              </>
            ) : (
              <span className="truncate">{v.reason}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Avatar({ name }: { name: string }) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return (
    <span
      className="w-9 h-9 rounded-full shrink-0 flex items-center justify-center text-meta font-semibold text-white"
      style={{ background: `oklch(0.66 0.11 ${h})` }}
    >
      {(name.trim().split(/[\s·•]+/).pop() || name).slice(0, 1)}
    </span>
  );
}

function RelabelMenu({ v, onTodo, onIgnore, onReview }: { v: MsgVerdict; onTodo: () => void; onIgnore: () => void; onReview: () => void }) {
  const Opt = ({ cls, sub, onClick }: { cls: VClass; sub: string; onClick?: () => void }) => {
    const meta = VERDICT_META[cls];
    const cur = v.cls === cls;
    return (
      <button
        disabled={cur || !onClick}
        onClick={onClick}
        className={cx("w-full flex items-center gap-3 p-3 rounded-ctl text-left transition", cur ? "bg-surface-2" : "hover:bg-surface-2")}
      >
        <Tag tone={meta.tone}>
          {meta.icon}
          {meta.label}
        </Tag>
        <span className="flex-1 text-meta text-ink-2">{sub}</span>
        {cur && <span className="text-cap text-ink-3">当前</span>}
      </button>
    );
  };
  return (
    <div className="flex flex-col gap-1 pb-2">
      <Opt cls="todo" sub={v.cls === "todo" ? "已建待办" : "建一条待办，可改标题和截止"} onClick={onTodo} />
      {v.cls === "review" && <Opt cls="review" sub="在待确认里带 AI 建议处理" onClick={onReview} />}
      <Opt cls="ignored" sub={v.cls === "todo" ? "对应待办会变成「不用做」" : "不用管这条"} onClick={onIgnore} />
    </div>
  );
}

function MsgDetail({
  m,
  v,
  groupName,
  onTodo,
  onIgnore,
  onGoTodo,
}: {
  m: Message;
  v: MsgVerdict;
  groupName: string;
  onTodo: () => void;
  onIgnore: () => void;
  onGoTodo: () => void;
}) {
  return (
    <div className="flex flex-col gap-4 pb-2">
      <QuoteBlock text={m.text} who={m.sender_name ?? String(m.user_id)} where={groupName} time={m.time}>
        <span className="q-num">QQ {m.user_id}</span>
      </QuoteBlock>
      <div>
        <div className="text-meta text-ink-2 font-medium mb-2">判定记录</div>
        <ol className="relative border-l border-line ml-2 flex flex-col gap-3">
          {v.history.length === 0 && <li className="pl-4 text-meta text-ink-3">还没有判定（可能不在白名单，或还在聚合窗口内）</li>}
          {[...v.history].reverse().map((d) => {
            const ruleHit = Boolean(d.route?.by?.startsWith("rule#"));
            return (
            <li key={d.id} className="pl-4 relative">
              <span className={cx("absolute -left-[5px] top-1.5 w-2.5 h-2.5 rounded-full", d.stage === "manual" ? "bg-ink" : "bg-brand")} />
              <div className="flex items-center gap-2">
                <VerdictTag cls={d.verdict as VClass} />
                <span className="text-cap text-ink-3">
                  {d.stage === "manual" ? "你" : ruleHit ? "规则" : d.stage === "rule" ? "规则" : "AI"}
                </span>
              </div>
              {ruleHit && (
                <div className="text-meta text-ink mt-1">{d.route?.why}</div>
              )}
              {d.reason && (
                <div className={cx("text-meta mt-1", ruleHit ? "text-ink-3" : "text-ink-2")}>
                  {ruleHit ? `模型理由：${d.reason}` : d.reason}
                </div>
              )}
            </li>
            );
          })}
        </ol>
      </div>
      {v.todo && (
        <button onClick={onGoTodo} className="q-card p-3 flex items-center gap-3 text-left border border-line">
          <IconTodo className="text-brand" />
          <span className="flex-1 min-w-0">
            <span className="block text-meta font-medium truncate">{v.todo.title}</span>
            <span className="text-cap text-ink-3">
              {v.todo.status === "open" ? "未完成" : v.todo.status === "done" ? "已完成" : "不用做"}
            </span>
          </span>
          <DueText due={v.todo.due_at} className="text-cap" />
        </button>
      )}
      <div className="flex gap-2">
        {v.cls !== "todo" && (
          <Button tone="primary" className="flex-1" onClick={onTodo}>
            <IconTodo size={18} />
            转为待办
          </Button>
        )}
        {v.cls !== "ignored" && (
          <Button tone="soft" className="flex-1" onClick={onIgnore}>
            <IconEyeOff size={18} />
            标为忽略
          </Button>
        )}
      </div>
    </div>
  );
}

function ConvertSheet({
  m,
  v,
  onClose,
  onSubmit,
}: {
  m: Message | null;
  v: MsgVerdict | null;
  onClose: () => void;
  onSubmit: (title: string, due: string | null) => void;
}) {
  const sug = v?.review?.suggestion;
  const initTitle = sug?.title ?? v?.todo?.title ?? m?.text.slice(0, 30) ?? "";
  const initDue = toLocalInput(sug?.due_at ?? v?.todo?.due_at ?? null);
  const [title, setTitle] = useState(initTitle);
  const [due, setDue] = useState(initDue);
  const [k, setK] = useState<number | null>(null);
  if (m && k !== m.msg_id) {
    setK(m.msg_id);
    setTitle(initTitle);
    setDue(initDue);
  }
  return (
    <Sheet
      open={!!m}
      onClose={onClose}
      title="转为待办"
      footer={
        <>
          <Button tone="ghost" onClick={onClose}>
            取消
          </Button>
          <Button tone="primary" className="flex-1" onClick={() => onSubmit(title.trim(), fromLocalInput(due))}>
            建待办
          </Button>
        </>
      }
    >
      {m && (
        <div className="flex flex-col gap-4">
          <QuoteBlock text={m.text} who={m.sender_name} time={m.time} />
          <div className="flex items-center gap-1.5 text-cap text-ink-3">
            <IconUsers size={13} />
            {sug ? "已按 AI 建议预填，可直接改" : "已用原句预填，可直接改"}
          </div>
          <Field label="标题">
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="留空则用原句前 30 字" />
          </Field>
          <Field label="截止">
            <Input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
          </Field>
        </div>
      )}
    </Sheet>
  );
}

function EmptySeg({ seg, total }: { seg: Seg; total: number }) {
  if (total === 0)
    return <Empty icon={<IconInbox />} title="还没有收到消息" sub="确认 QQ 协议端已登录、白名单里有群，然后在设置 → 诊断里点「补采」" tone="muted" />;
  const map: Record<Seg, { t: string; s: string }> = {
    todo: { t: "这里还没有转成待办的消息", s: "AI 判定要做的消息会出现在这里" },
    review: { t: "没有待决定的消息", s: "AI 拿不准的都处理完了" },
    ignored: { t: "没有被忽略的消息", s: "灌水、接龙、「收到」之类会归到这里" },
    none: { t: "没有未判定的消息", s: "" },
  };
  return <Empty icon={<IconInbox />} title={map[seg].t} sub={map[seg].s} tone="muted" />;
}
