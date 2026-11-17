// 不看的（原「学到的规则」）：类型、接口、查询/添加/删除、文案拼接。
// 请求改走 lib/api.ts 的统一 request（401 踢回连接页、humanError 翻译都一致）。
// rulesApi.list / confirm / reject 的签名不变，助手页的建议卡片照旧可用。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, humanError, request } from "./api";
import { toast } from "./toast";

export type RuleAction = "todo" | "review" | "ignored";
export type RuleStatus = "suggested" | "active";

export interface AgentRule {
  id: number;
  match: { group_id?: string | null; sender?: string | null; keyword?: string | null };
  action: RuleAction;
  status: RuleStatus;
  source?: string;
  note?: string | null;
  hits: number;
  created_at?: string;
}

export const AGENT_RULES_KEY = ["agent-rules"] as const;

function isRule(x: unknown): x is AgentRule {
  return !!x && typeof x === "object" && typeof (x as AgentRule).id === "number" && typeof (x as AgentRule).match === "object";
}

function normalize(r: unknown): AgentRule[] {
  const raw: unknown[] = Array.isArray(r)
    ? r
    : Array.isArray((r as { items?: unknown } | null)?.items)
      ? (r as { items: unknown[] }).items
      : [];
  return raw.filter(isRule).map((x) => ({ ...x, match: x.match ?? {}, hits: typeof x.hits === "number" ? x.hits : 0 }));
}

function pickRule(r: unknown): AgentRule | null {
  if (isRule(r)) return r;
  const o = r as { rule?: unknown; item?: unknown } | null;
  if (isRule(o?.rule)) return o!.rule as AgentRule;
  if (isRule(o?.item)) return o!.item as AgentRule;
  return null;
}

export const rulesApi = {
  list: () => request<unknown>("GET", "/api/agent/rules").then(normalize),
  confirm: (id: number) => request<{ ok: boolean; id: number; status: RuleStatus }>("POST", `/api/agent/rules/${id}/confirm`),
  reject: (id: number) => request<{ ok: boolean; id: number }>("POST", `/api/agent/rules/${id}/reject`),
  /**
   * 手动添加一条「不看」：用户亲手填写本身就是确认。
   * 若后端把新规则落成 suggested，这里立即补一次 confirm，保证添加即生效。
   */
  createIgnore: async (keyword: string): Promise<AgentRule | null> => {
    const r = await request<unknown>("POST", "/api/agent/rules", { match: { keyword }, action: "ignored" });
    const rule = pickRule(r);
    if (rule && rule.status === "suggested") {
      await rulesApi.confirm(rule.id);
      return { ...rule, status: "active" };
    }
    return rule;
  },
};

/** 只按关键词、不限群和发送人的「不看」规则里，是否已有同一个词 */
export function hasIgnoreKeyword(rules: AgentRule[], kw: string): boolean {
  const k = kw.trim().toLowerCase();
  return rules.some(
    (r) => r.action === "ignored" && !r.match.group_id && !r.match.sender && (r.match.keyword ?? "").trim().toLowerCase() === k,
  );
}

export function useAgentRules() {
  return useQuery({ queryKey: AGENT_RULES_KEY, queryFn: rulesApi.list, staleTime: 15000 });
}

export function useAddIgnoreRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (keyword: string) => rulesApi.createIgnore(keyword),
    onSuccess: (_r, kw) => toast(`以后不再显示带「${kw}」的消息`),
    onError: (e) => toast(humanError(e), undefined, "error"),
    onSettled: () => qc.invalidateQueries({ queryKey: AGENT_RULES_KEY }),
  });
}

/** 建议中的规则：确认 / 不要 */
export function useDecideRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: number; ok: boolean }) => (v.ok ? rulesApi.confirm(v.id) : rulesApi.reject(v.id)),
    onSuccess: (_r, v) => toast(v.ok ? "已生效" : "已不要"),
    onError: (e) => toast(humanError(e), undefined, "error"),
    onSettled: () => qc.invalidateQueries({ queryKey: AGENT_RULES_KEY }),
  });
}

/** 删除已生效的规则（走 reject 接口，与原「学到的规则」一致） */
export function useDeleteRule() {
  const qc = useQueryClient();
  const drop = (id: number) => qc.setQueryData<AgentRule[]>(AGENT_RULES_KEY, (l) => l?.filter((r) => r.id !== id));
  return useMutation({
    mutationFn: (r: AgentRule) => rulesApi.reject(r.id),
    onSuccess: (_x, r) => {
      drop(r.id);
      toast(`已删除：${condText(r)}`);
    },
    onError: (e, r) => {
      if (e instanceof ApiError && (e.status === 404 || e.status === 400)) {
        drop(r.id);
        toast("这条已经不在了");
        return;
      }
      toast(humanError(e), undefined, "error");
    },
    onSettled: () => qc.invalidateQueries({ queryKey: AGENT_RULES_KEY }),
  });
}

/* ── 文案拼接：把 match + action 翻成一句人话，不直接展示 JSON ── */

const ACTION_TEXT: Record<RuleAction, string> = {
  todo: "建待办",
  review: "进待确认",
  ignored: "不建待办",
};

/** 条件：群 → 发送者 → 关键词，用 " + " 连接，空值跳过（配置页列表用） */
export function condText(r: AgentRule): string {
  const parts: string[] = [];
  if (r.match.group_id) parts.push(`群${r.match.group_id}`);
  if (r.match.sender) parts.push(r.match.sender);
  if (r.match.keyword) parts.push(`"${r.match.keyword}"`);
  return parts.length > 0 ? parts.join(" + ") : "所有消息";
}

/** 一句话规则（助手页建议卡片标题用），如「辅导员发的带"讲座"的消息不建待办」 */
export function ruleText(r: AgentRule): string {
  const who = r.match.sender ? `${r.match.sender}发的` : "";
  const what = r.match.keyword ? `带"${r.match.keyword}"的` : "";
  const scope = who || what ? `${who}${what}消息` : "这类消息";
  return `${scope}${ACTION_TEXT[r.action]}`;
}
