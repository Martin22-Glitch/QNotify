// 画像提议的"新增/改动句子"判定：按句切分 → 句子归一化 → 对句子序列做 LCS。
// 新版本里没进 LCS 的句子 = 新增或改动（高亮）；旧版本里没进 LCS 的句子 = 被删或被改掉的原句。

/** 句末标点（中英文）；英文句点 "." 故意不算，避免 e.g. / 3.5 / 网址被切碎 */
const SPLIT = /[^。！？!?；;…\n]+(?:[。！？!?；;…]+[”"’'）)」』】]*)?|[。！？!?；;…]+[”"’'）)」』】]*|\n+/g;

/** 切成片段，片段首尾相接 === 原文（换行单独成段，渲染时原样保留） */
export function splitSentences(text: string): string[] {
  return text.match(SPLIT) ?? [];
}

/** 比较用的键：去空白、统一全/半角常见标点、去掉句末标点。空串 = 纯换行/纯标点，不参与比较 */
function norm(s: string): string {
  return s
    .replace(/\s+/g, "")
    .replace(/，/g, ",")
    .replace(/：/g, ":")
    .replace(/（/g, "(")
    .replace(/）/g, ")")
    .replace(/[。！？!?；;…”"’'」』】]+$/, "");
}

export interface SentenceDiff {
  /** 新版本的全部片段，changed = 需要高亮 */
  parts: { text: string; changed: boolean }[];
  changedCount: number;
  /** 旧版本里不在新版本中的句子（被删或被改写） */
  removed: string[];
  /** 旧画像为空：第一次写，不做高亮 */
  first: boolean;
  /** 两版逐句一致 */
  same: boolean;
}

export function diffSentences(base: string, next: string): SentenceDiff {
  const aSegs = splitSentences(base).filter((s) => norm(s));
  const aKeys = aSegs.map(norm);
  const segs = splitSentences(next);
  const keys = segs.map(norm);
  const bIdx: number[] = [];
  keys.forEach((k, i) => {
    if (k) bIdx.push(i);
  });

  if (aKeys.length === 0) {
    return { parts: segs.map((text) => ({ text, changed: false })), changedCount: 0, removed: [], first: bIdx.length > 0, same: bIdx.length === 0 };
  }

  const n = aKeys.length;
  const m = bIdx.length;
  const W = m + 1;
  const dp = new Uint16Array((n + 1) * W);
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i * W + j] = aKeys[i] === keys[bIdx[j]] ? dp[(i + 1) * W + j + 1] + 1 : Math.max(dp[(i + 1) * W + j], dp[i * W + j + 1]);

  const matchedA = new Set<number>();
  const matchedB = new Set<number>();
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (aKeys[i] === keys[bIdx[j]]) {
      matchedA.add(i);
      matchedB.add(bIdx[j]);
      i++;
      j++;
    } else if (dp[(i + 1) * W + j] >= dp[i * W + j + 1]) i++;
    else j++;
  }

  const parts = segs.map((text, k) => ({ text, changed: !!keys[k] && !matchedB.has(k) }));
  const changedCount = parts.filter((p) => p.changed).length;
  const removed = aSegs.filter((_, k) => !matchedA.has(k)).map((s) => s.trim());
  return { parts, changedCount, removed, first: false, same: changedCount === 0 && removed.length === 0 };
}
