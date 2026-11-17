// 规则建议卡片：挂在助手页回复下方，确认 / 不要。
// 容器与待办卡片同一种卡片（bg-surface + border-line + rounded-card + shadow-card），只是内容不同。
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { rulesApi, ruleText, type AgentRule } from "../lib/rules";
import { toast } from "../lib/toast";
import { Button, cx } from "./ui";

export function RuleSuggestCard({
  rule,
  onConfirmed,
  onRejected,
}: {
  rule: AgentRule;
  onConfirmed: (rule: AgentRule) => void;
  onRejected: (id: number) => void;
}) {
  const qc = useQueryClient();
  const [leaving, setLeaving] = useState(false);
  const mut = useMutation({
    mutationFn: (ok: boolean) => (ok ? rulesApi.confirm(rule.id) : rulesApi.reject(rule.id)),
    onSuccess: (_res, ok) => {
      if (ok) {
        // 确认：就地由父组件换成一行"已生效"，然后失效重取
        onConfirmed(rule);
        qc.invalidateQueries({ queryKey: ["agent-rules"] });
      } else {
        // 不要：先淡出（沿用 transition，不新写动画），再从渲染里去掉并重取
        setLeaving(true);
        window.setTimeout(() => {
          onRejected(rule.id);
          qc.invalidateQueries({ queryKey: ["agent-rules"] });
        }, 200);
      }
    },
    onError: () => toast("没保存成功，再试一次", undefined, "error"),
  });
  return (
    <div className={cx("self-start w-full max-w-[85%] q-in transition duration-200", leaving && "opacity-0 translate-y-1")}>
      <div className="bg-surface border border-line rounded-card shadow-card p-4">
        <div className="text-body text-ink font-medium q-pretty">我记住了：{ruleText(rule)}</div>
        <div className="mt-1 text-cap text-ink-3">来自对话 · 确认后生效</div>
        <div className="mt-3 flex gap-2">
          <Button
            tone="primary"
            className="flex-1"
            loading={mut.isPending && mut.variables === true}
            disabled={mut.isPending}
            onClick={() => mut.mutate(true)}
          >
            确认
          </Button>
          <Button className="flex-1" loading={mut.isPending && mut.variables === false} disabled={mut.isPending} onClick={() => mut.mutate(false)}>
            不要
          </Button>
        </div>
      </div>
    </div>
  );
}
