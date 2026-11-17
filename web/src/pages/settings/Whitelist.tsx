// 白名单：先选群，再选群内成员；好友单独一栏。PUT 是整体替换，所以本地编辑完整份再一次保存。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, humanError } from "../../lib/api";
import { toast } from "../../lib/toast";
import type { Whitelist } from "../../lib/types";
import { IconCheck, IconLeft, IconRefresh, IconSearch, IconUsers } from "../../components/icons";
import { Button, cx, Empty, Help, Input, Segmented, Tag } from "../../components/ui";

export function WhitelistPanel() {
  const qc = useQueryClient();
  const wl = useQuery({ queryKey: ["whitelist"], queryFn: api.whitelist });
  const groups = useQuery({ queryKey: ["groups"], queryFn: () => api.groups() });
  const friends = useQuery({ queryKey: ["friends"], queryFn: () => api.friends() });
  const [draft, setDraft] = useState<Whitelist | null>(null);
  const [tab, setTab] = useState<"groups" | "friends">("groups");
  const [q, setQ] = useState("");
  const [gid, setGid] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    if (wl.data && !draft) setDraft(wl.data);
  }, [wl.data, draft]);

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(wl.data), [draft, wl.data]);
  const gEntry = (id: number) => draft?.groups.find((g) => g.group_id === id);

  const toggleGroup = (id: number) =>
    setDraft((d) => {
      if (!d) return d;
      const has = d.groups.some((g) => g.group_id === id);
      return { ...d, groups: has ? d.groups.filter((g) => g.group_id !== id) : [...d.groups, { group_id: id, mode: "all", member_ids: [] }] };
    });
  const setMode = (id: number, mode: "all" | "members") =>
    setDraft((d) => (d ? { ...d, groups: d.groups.map((g) => (g.group_id === id ? { ...g, mode } : g)) } : d));
  const toggleMember = (id: number, uid: number) =>
    setDraft((d) => {
      if (!d) return d;
      return {
        ...d,
        groups: d.groups.map((g) =>
          g.group_id === id ? { ...g, member_ids: g.member_ids.includes(uid) ? g.member_ids.filter((x) => x !== uid) : [...g.member_ids, uid] } : g,
        ),
      };
    });
  const toggleFriend = (uid: number) =>
    setDraft((d) => (d ? { ...d, users: d.users.includes(uid) ? d.users.filter((x) => x !== uid) : [...d.users, uid] } : d));

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setErr(null);
    try {
      const r = await api.putWhitelist(draft);
      setDraft(r);
      qc.setQueryData(["whitelist"], r);
      qc.invalidateQueries({ queryKey: ["groups"] });
      toast("白名单已保存");
    } catch (e) {
      setErr(humanError(e));
    } finally {
      setSaving(false);
    }
  };
  const sync = async () => {
    setSyncing(true);
    try {
      const r = await api.sync();
      toast(`已同步 ${r.groups} 个群、${r.friends} 位好友`);
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["friends"] });
    } catch (e) {
      toast(humanError(e), undefined, "error");
    } finally {
      setSyncing(false);
    }
  };

  if (gid !== null) {
    const g = groups.data?.find((x) => x.group_id === gid);
    const entry = gEntry(gid);
    return (
      <div className="flex flex-col gap-3">
        <button className="self-start flex items-center gap-1 text-meta text-ink-2 h-9 -ml-1" onClick={() => setGid(null)}>
          <IconLeft size={18} />
          返回群列表
        </button>
        <div className="flex items-center gap-2">
          <div className="text-title q-title flex-1 truncate">{g?.group_name}</div>
          {entry ? <Tag tone="brand">已加入</Tag> : <Button size="sm" tone="primary" onClick={() => toggleGroup(gid)}>加入白名单</Button>}
        </div>
        {entry && (
          <Segmented
            value={entry.mode}
            onChange={(m) => setMode(gid, m)}
            items={[
              { value: "all", label: "整个群" },
              { value: "members", label: `仅指定成员 ${entry.member_ids.length || ""}` },
            ]}
          />
        )}
        {entry?.mode === "members" && (
          <MemberList gid={gid} selected={entry.member_ids} onToggle={(uid) => toggleMember(gid, uid)} />
        )}
        <SaveBar dirty={dirty} saving={saving} err={err} onSave={save} onReset={() => setDraft(wl.data ?? null)} />
      </div>
    );
  }

  const gl = (groups.data ?? []).filter((g) => !q || g.group_name.toLowerCase().includes(q.toLowerCase()));
  const fl = (friends.data ?? []).filter((f) => !q || (f.nickname + f.remark).toLowerCase().includes(q.toLowerCase()));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-2">
        <Segmented
          className="flex-1"
          value={tab}
          onChange={setTab}
          items={[
            { value: "groups", label: "群", count: draft?.groups.length ?? 0 },
            { value: "friends", label: "好友", count: draft?.users.length ?? 0 },
          ]}
        />
        <Button tone="plain" onClick={sync} loading={syncing} aria-label="从 QQ 同步群和好友">
          <IconRefresh size={16} />
          <span className="hidden sm:inline">同步</span>
        </Button>
      </div>
      <div className="relative">
        <IconSearch size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={tab === "groups" ? "搜索群名" : "搜索好友"} className="pl-10" />
      </div>
      <div className="flex flex-col divide-y divide-line rounded-ctl border border-line overflow-hidden max-h-[420px] overflow-y-auto">
        {tab === "groups" ? (
          gl.length === 0 ? (
            <Empty icon={<IconUsers />} title={groups.data?.length ? "没有匹配的群" : "还没有群列表"} sub={groups.data?.length ? undefined : "点右上「同步」从 QQ 拉取"} tone="muted" />
          ) : (
            gl.map((g) => {
              const e = gEntry(g.group_id);
              return (
                <div key={g.group_id} className={cx("flex items-center gap-3 px-3 h-14 bg-surface", e && "bg-brand-soft/40")}>
                  <button
                    onClick={() => toggleGroup(g.group_id)}
                    aria-label={e ? "移出白名单" : "加入白名单"}
                    className={cx(
                      "w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0",
                      e ? "bg-brand border-brand text-white" : "border-line",
                    )}
                  >
                    {e && <IconCheck size={14} strokeWidth={3} />}
                  </button>
                  <button className="flex-1 min-w-0 text-left" onClick={() => setGid(g.group_id)}>
                    <div className="text-meta font-medium truncate">{g.group_name}</div>
                    <div className="text-cap text-ink-3">
                      <span className="q-num">{g.member_count}</span> 人
                      {e && (e.mode === "all" ? " · 整个群" : ` · 仅 ${e.member_ids.length} 人`)}
                    </div>
                  </button>
                  <button onClick={() => setGid(g.group_id)} className="text-cap text-brand h-9 px-2 shrink-0">
                    选成员
                  </button>
                </div>
              );
            })
          )
        ) : fl.length === 0 ? (
          <Empty icon={<IconUsers />} title="没有好友" tone="muted" />
        ) : (
          fl.map((f) => {
            const on = draft?.users.includes(f.user_id);
            return (
              <button key={f.user_id} onClick={() => toggleFriend(f.user_id)} className={cx("flex items-center gap-3 px-3 h-14 bg-surface text-left", on && "bg-brand-soft/40")}>
                <span className={cx("w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0", on ? "bg-brand border-brand text-white" : "border-line")}>
                  {on && <IconCheck size={14} strokeWidth={3} />}
                </span>
                <span className="flex-1 min-w-0">
                  <span className="block text-meta font-medium truncate">{f.remark || f.nickname}</span>
                  {f.remark && <span className="block text-cap text-ink-3 truncate">{f.nickname}</span>}
                </span>
              </button>
            );
          })
        )}
      </div>
      <SaveBar dirty={dirty} saving={saving} err={err} onSave={save} onReset={() => setDraft(wl.data ?? null)} />
    </div>
  );
}

function MemberList({ gid, selected, onToggle }: { gid: number; selected: number[]; onToggle: (uid: number) => void }) {
  const [q, setQ] = useState("");
  const m = useQuery({ queryKey: ["members", gid], queryFn: () => api.members(gid) });
  const list = (m.data ?? []).filter((x) => !q || (x.nickname + x.card).toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <div className="relative">
        <IconSearch size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索成员（昵称或群名片）" className="pl-10" />
      </div>
      <div className="flex flex-col divide-y divide-line rounded-ctl border border-line overflow-hidden max-h-[360px] overflow-y-auto">
        {list.map((x) => {
          const on = selected.includes(x.user_id);
          return (
            <button key={x.user_id} onClick={() => onToggle(x.user_id)} className={cx("flex items-center gap-3 px-3 h-13 py-2 bg-surface text-left", on && "bg-brand-soft/40")}>
              <span className={cx("w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0", on ? "bg-brand border-brand text-white" : "border-line")}>
                {on && <IconCheck size={14} strokeWidth={3} />}
              </span>
              <span className="flex-1 min-w-0">
                <span className="block text-meta font-medium truncate">{x.card || x.nickname}</span>
                {x.card && <span className="block text-cap text-ink-3 truncate">{x.nickname}</span>}
              </span>
              {x.role !== "member" && <Tag tone={x.role === "owner" ? "urgent" : "brand"}>{x.role === "owner" ? "群主" : "管理员"}</Tag>}
            </button>
          );
        })}
        {list.length === 0 && <div className="p-4 text-meta text-ink-3 text-center">没有成员</div>}
      </div>
    </>
  );
}

function SaveBar({ dirty, saving, err, onSave, onReset }: { dirty: boolean; saving: boolean; err: string | null; onSave: () => void; onReset: () => void }) {
  if (!dirty && !err) return null;
  return (
    <div className="flex flex-col gap-2">
      {err && <div className="text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2 break-words">保存失败：{err}</div>}
      {dirty && (
        <div className="flex gap-2 items-center">
          <Button tone="primary" onClick={onSave} loading={saving} className="flex-1">
            保存白名单
          </Button>
          <Button tone="ghost" onClick={onReset}>
            放弃修改
          </Button>
          <Help>保存会整体替换白名单。选了「仅指定成员」却一个都没勾，会被拒绝。</Help>
        </div>
      )}
    </div>
  );
}
