// 连接 Agent（设置页里的一块）：总开关、本机地址与重置令牌、可复制的 MCP 配置（/api/app/agent-configs）、工具说明。
import { useRef, useState, type KeyboardEvent } from "react";
import { Check, Copy, RefreshCw } from "lucide-react";
import { api } from "../api";
import { useT } from "../i18n";
import { errText, useLoad } from "../lib";
import { Banner, Card, ConfirmDialog, LoadError, Skeleton, Switch, copyText, useToast, type ConfirmSpec } from "../ui";

const ORDER = ["claude_code", "codex", "claude_desktop", "cursor"];
const TOOLS = [["search", "agent.tSearch"], ["read", "agent.tRead"], ["timeline", "agent.tTimeline"], ["summary", "agent.tSummary"],
  ["get_frame", "agent.tFrame"], ["copied", "agent.tCopied"]] as const;

export function AgentConnect({ enabled, saving, onToggle }: { enabled: boolean; saving: boolean; onToggle: (v: boolean) => void }) {
  const { t } = useT();
  const toast = useToast();
  const cfg = useLoad((signal) => api.agentConfigs(signal), []);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [tab, setTab] = useState(0);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const keys = cfg.data ? [...ORDER.filter((k) => k in cfg.data!.configs), ...Object.keys(cfg.data.configs).filter((k) => !ORDER.includes(k))] : [];
  const at = Math.min(tab, Math.max(0, keys.length - 1));
  const cur = cfg.data && keys.length ? cfg.data.configs[keys[at]] : null;

  const copy = async (text: string) => {
    const ok = await copyText(text);
    toast(ok ? t("common.copied") : t("common.copyFail"), ok ? "ok" : "warn");
  };
  const onTabKey = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (i + d + keys.length) % keys.length;
    setTab(n);
    tabs.current[n]?.focus();
  };
  const reset = () => setConfirm({
    title: t("agent.reset"), body: t("agent.resetBody"), ok: t("agent.resetOk"), tone: "primary",
    run: async () => {
      try {
        await api.resetToken();
        toast(t("agent.resetDone"));
      } catch (e) {
        toast(errText(e, t("common.offline")), "alarm");
      }
    },
  });

  return (
    <Card title={t("agent.title")} id="agent-h">
      <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 pb-3.5">
        <div className="min-w-0 flex-1">
          <b id="lbl-agent">{t("agent.allow")}</b>
          <p className="mt-0.5 text-[13px] text-graphite">{t("agent.note")}</p>
        </div>
        <Switch on={enabled} labelledBy="lbl-agent" disabled={saving} onChange={onToggle} />
      </div>
      {!enabled && <Banner tone="warn" className="mb-3.5">{t("agent.offBanner")}</Banner>}
      <AgentStatusRow />
      <details className="border-t border-rule pt-3">
      <summary className="cursor-pointer text-[13px] text-graphite">{t("agent.manual")}</summary>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-x-5 gap-y-3 py-3.5">
        <div className="min-w-0 flex-1">
          <b>{t("agent.addr")}</b>
          <p className="mt-0.5 font-mono text-[13px] text-graphite [overflow-wrap:anywhere]">{cfg.data?.mcp_url ?? "—"}</p>
        </div>
        <button className="sr-btn sr-btn-line sr-btn-sm" onClick={reset}><RefreshCw size={15} aria-hidden />{t("agent.reset")}</button>
      </div>
      <div className="border-t border-rule pt-3.5">
        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
          <b>{t("agent.copyCfg")}</b>
          <span className="text-[12.5px] text-faint">{t("agent.noSecret")}</span>
        </div>
        {!cfg.data ? (cfg.error ? <LoadError text={errText(cfg.error, t("common.offline"))} onRetry={() => cfg.reload()} /> : <Skeleton rows={1} />) : (
          <>
            <div role="tablist" aria-label={t("agent.pick")} className="inline-flex max-w-full flex-wrap rounded-control bg-well p-[3px]">
              {keys.map((k, i) => (
                <button key={k} ref={(el) => { tabs.current[i] = el; }} role="tab" id={`tab-${k}`} aria-selected={i === at} aria-controls={`pan-${k}`}
                  tabIndex={i === at ? 0 : -1} onClick={() => setTab(i)} onKeyDown={(e) => onTabKey(e, i)}
                  className={`h-8 rounded-[8px] px-3 text-[13.5px] font-medium ${i === at ? "bg-surface text-ink shadow-[0_1px_2px_rgb(16_24_40/0.12)]" : "text-graphite hover:text-ink"}`}>
                  {cfg.data!.configs[k].name}
                </button>
              ))}
            </div>
            {cur && (
              <div role="tabpanel" id={`pan-${keys[at]}`} aria-labelledby={`tab-${keys[at]}`} className="mt-3 space-y-3">
                <p className="text-[12.5px] text-graphite [overflow-wrap:anywhere]">{t("agent.addTo", { x: cur.file })}</p>
                <Snippet text={cur.toml ?? JSON.stringify(cur.json, null, 2)} onCopy={copy} />
                {cur.cli && <>
                  <p className="text-[12.5px] text-graphite">{t("agent.cli")}</p>
                  <Snippet text={cur.cli} onCopy={copy} />
                </>}
              </div>
            )}
          </>
        )}
      </div>
      <div className="mt-3.5 border-t border-rule pt-3.5">
        <b>{t("agent.tools")}</b>
        <ul className="mt-2.5 grid gap-2.5 md:grid-cols-2">
          {TOOLS.map(([name, k]) => (
            <li key={name} className="rounded-[11px] border border-rule bg-canvas px-3.5 py-2.5 text-[13.5px]">
              <code className="font-mono text-[13px] font-semibold text-ink">{name}</code><br />{t(k)}
            </li>
          ))}
        </ul>
      </div>
      </details>
      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
    </Card>
  );
}

/** Claude Code / Codex 连上了没有 + 一键连接（把 MCP 转发程序登记进它们的配置） */
function AgentStatusRow() {
  const { t } = useT();
  const toast = useToast();
  const st = useLoad((signal) => api.agents(signal), []);
  const [busy, setBusy] = useState(false);
  const s = st.data;
  const connect = async () => {
    setBusy(true);
    try {
      const r = await api.installAgents();
      st.setData(r);
      toast(r.claude || r.codex ? t("agent.connectDone") : t("agent.connectFail", { e: r.messages.join(" ") }), r.claude || r.codex ? "ok" : "warn");
    } catch (e) {
      toast(t("agent.connectFail", { e: errText(e, t("common.offline")) }), "alarm");
    } finally {
      setBusy(false);
    }
  };
  const item = (name: string, ok: boolean) => (
    <span className={`sr-tag ${ok ? "sr-tag-ok" : "sr-tag-warn"}`}>
      {ok && <Check size={13} aria-hidden />}{name}：{ok ? t("agent.connected") : t("agent.notConnected")}
    </span>
  );
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-rule py-3.5" data-testid="agent-status">
      {!s ? (st.error ? <span className="text-sm text-faint">{errText(st.error, t("common.offline"))}</span> : <Skeleton rows={1} />) : <>
        {item("Claude Code", s.claude)}
        {item("Codex", s.codex)}
        <span className="flex-1" />
        <button className="sr-btn sr-btn-line sr-btn-sm" disabled={busy} onClick={() => void connect()}>
          <RefreshCw size={15} aria-hidden />{s.claude && s.codex ? t("agent.reconnect") : t("agent.connect")}
        </button>
        {!s.claude_cli && <p className="w-full text-[12.5px] text-faint">{t("agent.noCli")}</p>}
      </>}
    </div>
  );
}

function Snippet({ text, onCopy }: { text: string; onCopy: (s: string) => void }) {
  const { t } = useT();
  return (
    <div className="relative">
      <pre className="sr-code pr-[92px]">{text}</pre>
      <button className="sr-btn sr-btn-sm absolute right-2 top-2 border-night-line bg-night-3 text-night-text hover:border-night-faint" onClick={() => onCopy(text)}>
        <Copy size={15} aria-hidden />{t("common.copy")}
      </button>
    </div>
  );
}
