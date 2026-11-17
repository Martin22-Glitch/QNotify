// 我的偏好 → 不看的：条件 +「已忽略」标签 + 命中次数 + 删除（行内二次确认）/ 手动添加。
// 助手建议中的规则也在这里：多一个「待确认」标签，右侧换成 确认 / 不要（拒绝建议不是删除，不加二次确认）。
// 不做：最近被它忽略的消息、「这条不该忽略」、改条件（另开任务）。
import { useState } from "react";

import { humanError } from "../../lib/api";
import {
  condText,
  hasIgnoreKeyword,
  useAddIgnoreRule,
  useAgentRules,
  useDecideRule,
  useDeleteRule,
  type AgentRule,
} from "../../lib/rules";
import { toast } from "../../lib/toast";
import { VerdictTag } from "../../components/bits";
import { Button, Tag } from "../../components/ui";
import {
  AddButton,
  AddForm,
  BlockHead,
  ConfirmRow,
  DeleteButton,
  HelpPara,
  HitCount,
  ListSkeleton,
  LoadError,
  MiniEmpty,
  PrefList,
  PrefRow,
} from "./prefsBits";

function ignoreHelp(hasSuggested: boolean) {
  return (
    <>
      <HelpPara>正文带这些词的群消息会被忽略：不建待办，也不进待确认；点名你、@你、私聊你的除外。</HelpPara>
      <HelpPara>添加：点击「添加」，或通过助手添加。</HelpPara>
      {hasSuggested && <HelpPara>标着「待确认」的是助手从对话里记下的，你点确认后才生效。</HelpPara>}
    </>
  );
}

export function IgnorePanel() {
  const q = useAgentRules();
  const add = useAddIgnoreRule();
  const decide = useDecideRule();
  const del = useDeleteRule();
  const [confirming, setConfirming] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);

  const rules = q.data ?? [];
  // 建议中的放前面：需要用户处理
  const sorted = [...rules.filter((r) => r.status === "suggested"), ...rules.filter((r) => r.status !== "suggested")];
  const hasSuggested = sorted.length > 0 && sorted[0].status === "suggested";

  const submit = async (kw: string) => {
    if (hasIgnoreKeyword(rules, kw)) {
      toast(`已经有「${kw}」这条了`);
      return;
    }
    await add.mutateAsync(kw);
  };

  const decideBusy = (id: number): "confirm" | "reject" | null =>
    decide.isPending && decide.variables?.id === id ? (decide.variables.ok ? "confirm" : "reject") : null;

  return (
    <section>
      <BlockHead
        title="不看的"
        count={q.isSuccess ? rules.length : undefined}
        help={ignoreHelp(hasSuggested)}
        right={
          q.isSuccess && !adding ? (
            <AddButton
              onClick={() => {
                setConfirming(null);
                setAdding(true);
              }}
            />
          ) : undefined
        }
      />

      {adding && (
        <AddForm
          label="添加不看的关键词"
          placeholder="请输入..."
          maxLength={30}
          busy={add.isPending}
          onSubmit={submit}
          onClose={() => setAdding(false)}
        />
      )}

      {q.isPending ? (
        <ListSkeleton label="正在加载不看的内容" />
      ) : q.isError ? (
        <LoadError text={`没加载出来：${humanError(q.error)}`} retrying={q.isFetching} onRetry={() => q.refetch()} />
      ) : sorted.length === 0 ? (
        !adding && <MiniEmpty>还没有不看的内容。点「添加」，或对助手说「xx不用管」。</MiniEmpty>
      ) : (
        <PrefList label="不看的内容">
          {sorted.map((r) =>
            confirming === r.id ? (
              <ConfirmRow
                key={r.id}
                label={`确认删除 ${condText(r)}`}
                busy={del.isPending && del.variables?.id === r.id}
                onCancel={() => setConfirming(null)}
                onConfirm={() => del.mutate(r, { onSettled: () => setConfirming(null) })}
              >
                删除 {condText(r)}？之后这类消息照常判定。
              </ConfirmRow>
            ) : (
              <RuleRow
                key={r.id}
                r={r}
                busy={decideBusy(r.id)}
                lock={del.isPending || decide.isPending}
                onDecide={(ok) => decide.mutate({ id: r.id, ok })}
                onAskDelete={() => setConfirming(r.id)}
              />
            ),
          )}
        </PrefList>
      )}
    </section>
  );
}

function RuleRow({
  r,
  busy,
  lock,
  onDecide,
  onAskDelete,
}: {
  r: AgentRule;
  busy: "confirm" | "reject" | null;
  lock: boolean;
  onDecide: (ok: boolean) => void;
  onAskDelete: () => void;
}) {
  const suggested = r.status === "suggested";
  return (
    <PrefRow
      main={condText(r)}
      tag={
        <>
          <VerdictTag cls={r.action} />
          {suggested && (
            <Tag tone="warn" dot>
              待确认
            </Tag>
          )}
        </>
      }
      meta={suggested ? undefined : <HitCount n={r.hits} />}
      actions={
        suggested ? (
          <div className="flex gap-2">
            <Button tone="primary" size="sm" loading={busy === "confirm"} disabled={lock} onClick={() => onDecide(true)}>
              确认
            </Button>
            <Button tone="plain" size="sm" loading={busy === "reject"} disabled={lock} onClick={() => onDecide(false)}>
              不要
            </Button>
          </div>
        ) : (
          <DeleteButton disabled={lock} onClick={onAskDelete} />
        )
      }
    />
  );
}
