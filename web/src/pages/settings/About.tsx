// 设置 → 关于我：一段自我介绍，判定与助手都会读。
// 一个多行输入 + 字数；说明全部收进块标题的「?」，正文不放解释段。
// 有待确认的画像提议时，标题下方直接显示同一张提议卡（不是数字）。
import { useEffect, useRef, useState } from "react";

import { humanError } from "../../lib/api";
import { useProfile, useSaveProfile } from "../../lib/queries";
import { toast } from "../../lib/toast";
import { ABOUT_MAX, AboutTextarea } from "../../components/profile";
import { Button, Field, Skeleton } from "../../components/ui";
import { BlockHead, HelpPara } from "./prefsBits";

export { ABOUT_MAX };

const ABOUT_HELP = (
  <>
    <HelpPara>这段话给判定模型和助手看，据此判断一条群消息是不是对你说的。</HelpPara>
    <HelpPara>点「保存」后立即生效，不超过 {ABOUT_MAX} 字。</HelpPara>
  </>
);

export function AboutPanel() {
  const q = useProfile();
  const save = useSaveProfile();
  const [text, setText] = useState<string | null>(null);

  // 首次拿到数据时填入；之后服务器值变了（比如在对话里采用了提议），只要本地没改过就跟上
  const server = q.data?.about;
  const lastServer = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (server === undefined) return;
    const prev = lastServer.current;
    lastServer.current = server;
    setText((t) => (t === null || t === prev ? server : t));
  }, [server]);

  const head = <BlockHead title="关于我" help={ABOUT_HELP} />;

  if (q.isPending)
    return (
      <section>
        {head}
        <Skeleton className="h-40" />
      </section>
    );
  if (q.isError)
    return (
      <section>
        {head}
        <div className="text-meta text-danger bg-danger-soft rounded-ctl px-3 py-2 break-words">
          画像加载失败：{humanError(q.error)}
        </div>
      </section>
    );

  const value = text ?? q.data?.about ?? "";
  const dirty = value !== (q.data?.about ?? "");
  const tooLong = value.length > ABOUT_MAX;
  const submit = () => {
    save.mutate(value, {
      onSuccess: () => toast("已保存，之后的判定会用上这份介绍"),
      onError: (e) => toast(humanError(e), undefined, "error"),
    });
  };

  return (
    <section>
      {head}

      <Field
        label="自我介绍"
        help="例：我是计算机学院大三学生，班里的学习委员，负责收作业和通知考试；社团活动不用提醒。"
        error={tooLong ? `超出 ${value.length - ABOUT_MAX} 字` : null}
      >
        <AboutTextarea
          value={value}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder="还没写。写下你是谁、在群里是什么身份。"
        />
      </Field>

      <div className="mt-2 flex items-center gap-3">
        <Button tone="primary" size="sm" loading={save.isPending} disabled={!dirty || tooLong} onClick={submit}>
          保存
        </Button>
        <span className={tooLong ? "text-cap text-danger q-num" : "text-cap text-ink-3 q-num"}>
          {value.length} / {ABOUT_MAX}
        </span>
      </div>
    </section>
  );
}
