// 设置 → 判定参数 → 保留期。
// 原则：一屏只放"要改的两个数"，解释全部收进「?」。保存前二次确认，写清对已有数据的影响。
// 输入框是**受控但不校正**：允许清空、允许中途非法，只在保存时判合法性（否则会出现"7 删不掉"）。
import { useState } from "react";

import { humanError } from "../../lib/api";
import { useRetention, useSaveRetention } from "../../lib/queries";
import { Button, Field, Help, Input, SectionHead, Sheet, Skeleton } from "../../components/ui";

const OVERDUE = { min: 1, max: 30 };
const CONTENT = { min: 7, max: 365 };

/** 空串 / 非整数 / 越界 → null（不可保存）；否则给出数字 */
function parseDays(raw: string, min: number, max: number): number | null {
  const t = raw.trim();
  if (!t) return null;
  const n = Number(t);
  if (!Number.isInteger(n) || n < min || n > max) return null;
  return n;
}

export function RetentionSection() {
  const q = useRetention();
  const save = useSaveRetention();
  // null = 没动过，显示后端值；字符串（含 ""）= 用户正在编辑的内容，原样显示
  const [od, setOd] = useState<string | null>(null);
  const [cd, setCd] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  if (q.isPending) return <Skeleton className="h-24" />;
  if (q.isError) return <div className="text-meta text-danger px-1">保留期加载失败：{humanError(q.error)}</div>;

  const cur = q.data ?? { overdue_days: 7, content_days: 30 };
  const odRaw = od ?? String(cur.overdue_days);
  const cdRaw = cd ?? String(cur.content_days);
  const nextOd = parseDays(odRaw, OVERDUE.min, OVERDUE.max);
  const nextCd = parseDays(cdRaw, CONTENT.min, CONTENT.max);
  const valid = nextOd !== null && nextCd !== null;
  const dirty = nextOd !== cur.overdue_days || nextCd !== cur.content_days;
  const odErr = odRaw.trim() !== "" && nextOd === null ? `只能是 ${OVERDUE.min}–${OVERDUE.max} 的整数` : null;
  const cdErr = cdRaw.trim() !== "" && nextCd === null ? `只能是 ${CONTENT.min}–${CONTENT.max} 的整数` : null;

  return (
    <section>
      <SectionHead
        title="保留期"
        help="让待办页只放还能行动的事。逾期超过「收起」天数后移到历史；群消息和助手对话超过「保留」天数会被清理。"
      />

      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="逾期多少天后收起"
          help={`超过这个天数还没处理，就不再占着主视图。${OVERDUE.min}–${OVERDUE.max} 天。`}
          error={odErr}
        >
          <Input
            type="number"
            inputMode="numeric"
            min={OVERDUE.min}
            max={OVERDUE.max}
            value={odRaw}
            onChange={(e) => setOd(e.target.value)}
          />
        </Field>
        <Field
          label="内容和对话保留多少天"
          help={`过期的群消息与助手对话会被清理。${CONTENT.min}–${CONTENT.max} 天。`}
          error={cdErr}
        >
          <Input
            type="number"
            inputMode="numeric"
            min={CONTENT.min}
            max={CONTENT.max}
            value={cdRaw}
            onChange={(e) => setCd(e.target.value)}
          />
        </Field>
      </div>

      <div className="mt-3">
        <Button tone="primary" size="sm" disabled={!valid || !dirty} onClick={() => setConfirm(true)}>
          保存
        </Button>
      </div>

      <Sheet
        open={confirm}
        onClose={() => {
          if (!save.isPending) {
            setConfirm(false);
            save.reset();
          }
        }}
        title="按新规则处理已有数据？"
        footer={
          <>
            <Button tone="ghost" disabled={save.isPending} onClick={() => setConfirm(false)}>
              取消
            </Button>
            <Button
              tone="primary"
              className="flex-1"
              loading={save.isPending}
              onClick={() =>
                save.mutate(
                  { overdue_days: nextOd!, content_days: nextCd! },
                  {
                    onSuccess: () => {
                      setOd(null);
                      setCd(null);
                      setConfirm(false);
                    },
                  },
                )
              }
            >
              确认修改
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 text-body">
          <div>
            逾期 <b className="q-num">{nextOd}</b> 天收起 → 原来更久没处理的待办会立刻移入历史。
          </div>
          <div>
            保留 <b className="q-num">{nextCd}</b> 天 → 超出的群消息与助手对话会被清理，不可恢复。
          </div>
          <div className="text-meta text-ink-3 flex items-center gap-1">
            改完立即生效，不需要重启。
            <Help>待办本身不会被删除：收起只是从主视图过滤掉，数据还在库里。</Help>
          </div>
        </div>
        {save.isError && <div className="mt-3 text-meta text-danger break-words">{humanError(save.error)}</div>}
      </Sheet>
    </section>
  );
}
