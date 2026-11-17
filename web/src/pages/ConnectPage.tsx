// 连接入口：首次使用 / 令牌失效时显示。测试连接能区分"地址不通"与"令牌错"。
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { connStore } from "../lib/config";
import { useStore } from "../lib/store";
import { IconCheck, IconPlug, IconX } from "../components/icons";
import { LogoMark } from "../components/Logo";
import { Button, cx, Field, Help, Input } from "../components/ui";

type Result = { step: "addr" | "token" | "ok"; ok: boolean; text: string } | null;

export async function testConnection(base: string, token: string): Promise<Result> {
  try {
    await api.healthz(base);
  } catch (e) {
    return {
      step: "addr",
      ok: false,
      text: e instanceof ApiError && e.status === 0 ? "地址不通：服务没启动，或地址/端口写错了" : `地址能连上但不是 QNotify 服务（${e instanceof ApiError ? e.status : "?"}）`,
    };
  }
  try {
    const s = await api.loginStatus(base, token);
    return { step: "ok", ok: true, text: s.logged_in ? `已连接 · QQ ${s.nickname ?? s.self_id}` : "已连接核心服务 · QQ 协议端暂未登录" };
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return { step: "token", ok: false, text: "地址正确，但访问令牌不对" };
    return { step: "token", ok: false, text: "验证令牌时出错，请稍后再试" };
  }
}

export function ConnectPage() {
  const conn = useStore(connStore);
  const [base, setBase] = useState(conn.baseUrl);
  const [token, setToken] = useState(conn.invalid ? "" : conn.token);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Result>(conn.invalid ? { step: "token", ok: false, text: "访问令牌已失效，请重新填写" } : null);

  const go = async () => {
    setBusy(true);
    const r = await testConnection(base, token.trim());
    setBusy(false);
    setRes(r);
    if (r?.ok) connStore.set({ baseUrl: base.trim(), token: token.trim(), invalid: false });
  };

  return (
    <div className="min-h-dvh flex items-center justify-center p-5">
      <div className="w-full max-w-[420px]">
        <div className="flex flex-col items-center text-center mb-8">
          <LogoMark size={56} />
          <div className="mt-4 text-h2 q-display tracking-tight">
            Q<span className="text-brand">Notify</span>
          </div>
          <div className="mt-1 text-meta text-ink-2">把 QQ 群里要你动手的事，变成待办</div>
        </div>
        <div className="q-card p-5 flex flex-col gap-4">
          <div className="flex items-center gap-2 text-title q-title">
            <IconPlug className="text-brand" />
            连接核心服务
          </div>
          <Field
            label="服务地址"
            help={<>核心服务的地址，例如 <code>http://192.168.1.5:8720</code>。电脑上本机访问可以留空。手机访问时要填电脑的局域网地址。</>}
          >
            <Input value={base} onChange={(e) => setBase(e.target.value)} placeholder="留空 = 当前站点" inputMode="url" autoCapitalize="off" />
          </Field>
          <Field label="访问令牌" help="核心服务启动时配置的 QN_ACCESS_TOKEN，只保存在本机浏览器里。">
            <Input value={token} onChange={(e) => setToken(e.target.value)} type="password" placeholder="QN_ACCESS_TOKEN" autoCapitalize="off" />
          </Field>
          {res && (
            <div className="flex flex-col gap-1.5">
              <Step ok={res.step !== "addr"} label="地址可达" active={res.step === "addr"} />
              <Step ok={res.ok} label="令牌有效" active={res.step === "token"} pending={res.step === "addr"} />
              <div className={cx("mt-1 text-meta", res.ok ? "text-ok" : "text-danger")}>{res.text}</div>
            </div>
          )}
          <Button tone="primary" size="lg" onClick={go} loading={busy} disabled={!token.trim()}>
            测试并连接
          </Button>
        </div>
        <div className="mt-4 flex items-center justify-center gap-1.5 text-cap text-ink-3">
          连接后先去设置里选白名单群
          <Help>系统只处理白名单里的群和好友。第一次用：连上 → 设置 → 白名单选群 → 模型填好 → 等消息进来。</Help>
        </div>
      </div>
    </div>
  );
}

function Step({ ok, label, active, pending }: { ok: boolean; label: string; active?: boolean; pending?: boolean }) {
  return (
    <div className="flex items-center gap-2 text-meta">
      <span
        className={cx(
          "w-5 h-5 rounded-full flex items-center justify-center",
          pending ? "bg-surface-2 text-ink-3" : ok ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger",
        )}
      >
        {pending ? "·" : ok ? <IconCheck size={12} strokeWidth={3} /> : <IconX size={12} strokeWidth={3} />}
      </span>
      <span className={cx(active ? "text-ink" : "text-ink-2")}>{label}</span>
    </div>
  );
}
