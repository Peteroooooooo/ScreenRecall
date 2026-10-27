// 连接 Agent：总开关、本机地址与重置令牌、可复制的 MCP 配置、四个工具的说明。没有“查询记录”。
import { useRef, useState, type KeyboardEvent } from "react";
import { Copy, RefreshCw } from "lucide-react";
import { api } from "../api";
import { useT } from "../i18n";
import { errText, useLoad } from "../lib";
import { useSettings } from "../settings";
import { Banner, Card, ConfirmDialog, LoadError, PageHeader, Skeleton, Switch, copyText, useToast, type ConfirmSpec } from "../ui";

const ORDER = ["claude_desktop", "claude_code", "codex", "cursor"];

export default function AgentPage() {
  const { t } = useT();
  const toast = useToast();
  const st = useSettings();
  const cfg = useLoad((signal) => api.agentConfigs(signal), []);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [tab, setTab] = useState(0);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const keys = cfg.data ? [...ORDER.filter((k) => k in cfg.data!.configs), ...Object.keys(cfg.data.configs).filter((k) => !ORDER.includes(k))] : [];
  const cur = cfg.data && keys.length ? cfg.data.configs[keys[Math.min(tab, keys.length - 1)]] : null;
  const enabled = st.data?.agent_enabled ?? false;

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
    <div>
      <PageHeader title={t("agent.title")} sub={t("agent.sub")} />
      <div className="space-y-4">
        <section className="sr-card p-4 sm:p-5">
          {!st.data ? (st.error ? <LoadError text={t("set.loadFail", { e: errText(st.error, t("common.offline")) })} onRetry={() => st.reload()} /> : <Skeleton rows={1} />) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 pb-3.5">
                <div className="min-w-0 flex-1">
                  <b id="lbl-agent">{t("agent.allow")}</b>
                  <p className="mt-0.5 text-[13px] text-graphite">{t("agent.allowNote")}</p>
                </div>
                <Switch on={enabled} labelledBy="lbl-agent" disabled={st.saving}
                  onChange={(v) => void st.save({ agent_enabled: v }, v ? t("agent.enabled") : t("agent.disabled"))} />
              </div>
              {!enabled && <Banner tone="warn" className="mb-3.5">{t("agent.offBanner")}</Banner>}
            </>
          )}
          <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 border-t border-rule pt-3.5">
            <div className="min-w-0 flex-1">
              <b>{t("agent.addr")}</b>
              <p className="mt-0.5 font-mono text-[13px] text-graphite [overflow-wrap:anywhere]">{cfg.data?.mcp_url ?? "—"}</p>
            </div>
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={reset}><RefreshCw size={15} aria-hidden />{t("agent.reset")}</button>
          </div>
        </section>

        <Card title={t("agent.copyCfg")} id="cfg-h" aside={<span className="text-[12.5px] text-faint">{t("agent.noSecret")}</span>}>
          {!cfg.data ? (cfg.error ? <LoadError text={errText(cfg.error, t("common.offline"))} onRetry={() => cfg.reload()} /> : <Skeleton rows={1} />) : (
            <>
              <div role="tablist" aria-label={t("agent.pick")} className="inline-flex max-w-full flex-wrap rounded-control bg-well p-[3px]">
                {keys.map((k, i) => {
                  const on = i === Math.min(tab, keys.length - 1);
                  return (
                    <button key={k} ref={(el) => { tabs.current[i] = el; }} role="tab" id={`tab-${k}`} aria-selected={on} aria-controls={`pan-${k}`}
                      tabIndex={on ? 0 : -1} onClick={() => setTab(i)} onKeyDown={(e) => onTabKey(e, i)}
                      className={`h-8 rounded-[8px] px-3 text-[13.5px] font-medium ${on ? "bg-surface text-ink shadow-[0_1px_2px_rgb(16_24_40/0.12)]" : "text-graphite hover:text-ink"}`}>
                      {cfg.data!.configs[k].name}
                    </button>
                  );
                })}
              </div>
              {cur && (() => {
                const k = keys[Math.min(tab, keys.length - 1)];
                const body = cur.toml ?? JSON.stringify(cur.json, null, 2);
                return (
                  <div role="tabpanel" id={`pan-${k}`} aria-labelledby={`tab-${k}`} className="mt-3 space-y-3">
                    <p className="text-[12.5px] text-graphite [overflow-wrap:anywhere]">{t("agent.addTo", { x: cur.file })}</p>
                    <Snippet text={body} onCopy={copy} />
                    {cur.cli && <>
                      <p className="text-[12.5px] text-graphite">{t("agent.cli")}</p>
                      <Snippet text={cur.cli} onCopy={copy} />
                    </>}
                  </div>
                );
              })()}
            </>
          )}
        </Card>

        <Card title={t("agent.tools")} id="tools-h">
          <ul className="grid gap-3 md:grid-cols-2">
            {([["search", "agent.tSearch"], ["read", "agent.tRead"], ["timeline", "agent.tTimeline"], ["summary", "agent.tSummary"], ["get_frame", "agent.tFrame"]] as const).map(([name, k]) => (
              <li key={name} className="rounded-[11px] border border-rule bg-canvas px-3.5 py-3 text-[13.5px]">
                <code className="font-mono text-[13px] font-semibold text-ink">{name}</code><br />{t(k)}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[12.5px] text-graphite">{t("agent.toolsNote")} <a className="text-cobalt hover:underline" href="#/recorder">{t("agent.hiddenLink")}</a></p>
        </Card>
      </div>
      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
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
