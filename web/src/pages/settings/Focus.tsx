// 我的偏好 → 我关注的：话题 +「关注」标签 + 命中次数 + 删除（行内二次确认）/ 手动添加。
// 用户原话（note）不进列表主行，只作为桌面悬停提示。
// 不展示"最近因它多看的几条"（后端暂无明细接口，不伪造）。
import { useState } from "react";

import { humanError } from "../../lib/api";
import { sameTopic, useAddFocusTopic, useDeleteFocusTopic, useFocusTopics, type FocusTopic } from "../../lib/focus";
import { toast } from "../../lib/toast";
import { Tag } from "../../components/ui";
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

const FOCUS_HELP = (
  <>
    <HelpPara>跟这些话题有关的消息会留意：该进待办的照常进待办，拿不准的放进待确认。</HelpPara>
    <HelpPara>添加：点击「添加」，或通过助手添加。</HelpPara>
  </>
);

export function FocusPanel() {
  const q = useFocusTopics();
  const del = useDeleteFocusTopic();
  const add = useAddFocusTopic();
  /** 同一时刻只有一行处于"确认删除"状态 */
  const [confirming, setConfirming] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);

  const items = q.data ?? [];
  const busyId = del.isPending ? (del.variables?.id ?? null) : null;

  const submit = async (topic: string) => {
    if (items.some((t) => sameTopic(t.topic, topic))) {
      toast(`已经在关注「${topic}」了`);
      return;
    }
    await add.mutateAsync(topic);
  };

  return (
    <section aria-labelledby="pref-focus-title">
      <BlockHead
        title="我关注的"
        count={q.isSuccess ? items.length : undefined}
        help={FOCUS_HELP}
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
        <AddForm label="添加关注的话题" placeholder="请输入..." busy={add.isPending} onSubmit={submit} onClose={() => setAdding(false)} />
      )}

      {q.isPending ? (
        <ListSkeleton label="正在加载关注的话题" />
      ) : q.isError ? (
        <LoadError text={`关注的话题没加载出来：${humanError(q.error)}`} retrying={q.isFetching} onRetry={() => q.refetch()} />
      ) : items.length === 0 ? (
        !adding && <MiniEmpty>还没有关注的话题。点「添加」，或对助手说「xx要留意」。</MiniEmpty>
      ) : (
        <PrefList label="关注的话题">
          {items.map((t) =>
            confirming === t.id ? (
              <ConfirmRow
                key={t.id}
                label={`确认删除关注话题 ${t.topic}`}
                busy={busyId === t.id}
                onCancel={() => setConfirming(null)}
                onConfirm={() => del.mutate(t, { onSettled: () => setConfirming(null) })}
              >
                不再关注「{t.topic}」？
              </ConfirmRow>
            ) : (
              <TopicRow key={t.id} t={t} lockAsk={del.isPending} onAsk={() => setConfirming(t.id)} />
            ),
          )}
        </PrefList>
      )}
    </section>
  );
}

function TopicRow({ t, lockAsk, onAsk }: { t: FocusTopic; lockAsk: boolean; onAsk: () => void }) {
  return (
    <PrefRow
      main={t.topic}
      tag={<Tag tone="brand">关注</Tag>}
      meta={<HitCount n={t.hits} />}
      actions={<DeleteButton disabled={lockAsk} onClick={onAsk} />}
    />
  );
}
