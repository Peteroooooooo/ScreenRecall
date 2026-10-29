// 名单：三档（录 / 一直录 / 不录）切换、最近出现过的未录软件一键加入、不录的窗口关键词和网站。
// 不在名单里的软件就是“不录”。所有软件走同一套规则，名单只是数据。
import { useState, type FormEvent } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { api, type AppsInfo, type Tier } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtDuration, fmtWhen, useLoad } from "../lib";
import { Av, Card, Chips, LoadError, PageHeader, Segmented, Skeleton, useToast } from "../ui";

const TIERS: Tier[] = ["record", "always", "never"];

export default function AppsPage() {
  const { t } = useT();
  const toast = useToast();
  const res = useLoad((signal) => api.apps(signal), []);
  const [busy, setBusy] = useState(false);
  const d = res.data;

  /** 发一个改动；成功后用服务端返回的整份名单刷新 */
  const run = async (fn: () => Promise<AppsInfo>, ok: string) => {
    setBusy(true);
    try {
      res.setData(await fn());
      toast(ok);
      return true;
    } catch (e) {
      toast(t("common.saveFail", { e: errText(e, t("common.offline")) }), "alarm");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const setTier = (process: string, tier: Tier) =>
    run(() => api.setTier(process, tier), t("apps.set", { app: appName(process), tier: t(`tier.${tier}`) }));
  const running = useLoad((signal) => api.runningApps(signal), []);
  const recorded = new Set((d?.apps ?? []).filter((a) => a.tier !== "never").map((a) => a.process.toLowerCase()));
  const open = (running.data?.running ?? []).filter((a) => !recorded.has(a.process.toLowerCase()));
  const openSet = new Set(open.map((a) => a.process.toLowerCase()));
  const unlisted = (d?.unlisted ?? []).filter((u) => !openSet.has(u.app.toLowerCase()));

  return (
    <div>
      <PageHeader title={t("apps.title")} sub={t("apps.sub")} />
      {!d ? (
        res.error ? <LoadError text={errText(res.error, t("common.offline"))} onRetry={() => res.reload()} /> : <Skeleton rows={3} />
      ) : (
        <div className="space-y-4">
          <Card title={t("apps.list")} id="list-h">
            <ul className="mb-3 grid gap-1 text-[13px] text-graphite sm:grid-cols-3">
              {TIERS.map((x) => <li key={x}><b className="text-ink">{t(`tier.${x}`)}</b>：{t(`tier.${x}Note`)}</li>)}
            </ul>
            {d.apps.length ? (
              <ul className="divide-y divide-rule" aria-labelledby="list-h">
                {d.apps.map((a) => (
                  <li key={a.process} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-2.5" data-testid="app-row">
                    <Av app={a.process} small />
                    <span className="min-w-0 flex-1 truncate font-medium" id={`app-${a.process}`}>{a.process}</span>
                    <Segmented<Tier> size="sm" label={t("apps.tierOf", { app: a.process })} value={a.tier}
                      onChange={(v) => !busy && v !== a.tier && void setTier(a.process, v)}
                      options={TIERS.map((x) => ({ value: x, label: t(`tier.${x}`) }))} />
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-faint">{t("apps.none")}</p>}
          </Card>

          <Card title={t("apps.pick")} id="pick-h" aside={
            <button className="sr-btn sr-btn-line sr-btn-sm" disabled={running.loading} onClick={() => running.reload()}>
              <RefreshCw size={15} aria-hidden />{t("apps.refresh")}
            </button>
          }>
            <p className="mb-2.5 text-[13px] text-graphite">{t("apps.pickNote")}</p>
            {running.error ? (
              <p className="text-sm text-alarm">{t("apps.pickFail", { e: errText(running.error, t("common.offline")) })}</p>
            ) : !running.data ? <Skeleton rows={2} /> : open.length ? (
              <ul className="divide-y divide-rule" aria-labelledby="pick-h">
                {open.map((a) => (
                  <li key={a.process} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-2.5" data-testid="open-app">
                    <Av app={a.name} small />
                    <span className="min-w-0 flex-1">
                      <b className="block truncate font-medium">{a.name}</b>
                      <span className="block truncate text-[12.5px] text-graphite">
                        {a.process}{a.title && a.title !== a.name ? ` · ${t("apps.win", { t: a.title })}` : ""}
                      </span>
                    </span>
                    <button className="sr-btn sr-btn-primary sr-btn-sm" disabled={busy} aria-label={t("apps.addRecord", { app: a.name })}
                      onClick={() => void setTier(a.process, "record")}>
                      <Plus size={15} aria-hidden />{t("tier.record")}
                    </button>
                    <button className="sr-btn sr-btn-line sr-btn-sm" disabled={busy} aria-label={t("apps.addAlways", { app: a.name })}
                      onClick={() => void setTier(a.process, "always")}>
                      {t("tier.always")}
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-faint">{t("apps.pickNone")}</p>}
            <details className="mt-3 border-t border-rule pt-3">
              <summary className="cursor-pointer text-[13px] text-graphite">{t("apps.manual")}</summary>
              <AddApp busy={busy} onAdd={(p, tier) => setTier(p, tier)} />
            </details>
          </Card>

          <Card title={t("apps.unlisted")} id="unl-h">
            <p className="mb-2.5 text-[13px] text-graphite">{t("apps.unlistedNote")}</p>
            {unlisted.length ? (
              <ul className="divide-y divide-rule" aria-labelledby="unl-h">
                {unlisted.map((u) => (
                  <li key={u.app} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-2.5">
                    <Av app={u.app} small />
                    <span className="min-w-0 flex-1">
                      <b className="block truncate font-medium">{u.app}</b>
                      <span className="sr-num text-[12.5px] text-graphite">{t("apps.seen", { t: fmtWhen(u.last_ms), x: fmtDuration(u.total_ms) })}</span>
                    </span>
                    <button className="sr-btn sr-btn-line sr-btn-sm" disabled={busy} onClick={() => void setTier(u.app, "record")}>
                      <Plus size={15} aria-hidden />{t("apps.addRecord", { app: appName(u.app) })}
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-faint">{t("apps.noUnlisted")}</p>}
          </Card>

          <Card title={t("apps.excludes")} id="ex-h">
            <p className="mb-3 text-[13px] text-graphite">{t("apps.excludesNote")}</p>
            {(["title", "host"] as const).map((kind) => (
              <div key={kind} className="border-t border-rule py-3.5 first-of-type:border-t-0 first-of-type:pt-0">
                <b className="mb-2 block">{t(kind === "title" ? "apps.exTitle" : "apps.exHost")}</b>
                <Chips empty={t("apps.exEmpty")}
                  items={d.excludes.filter((x) => x.kind === kind).map((x) => ({ key: String(x.exclude_id), label: x.value }))}
                  onRemove={(k) => {
                    const x = d.excludes.find((e) => String(e.exclude_id) === k);
                    if (x) void run(() => api.removeExclude(x.exclude_id), t("common.removed", { x: x.value }));
                  }} />
                <AddExclude kind={kind} busy={busy} onAdd={(v) => run(() => api.addExclude(kind, v), t("common.added", { x: v }))} />
              </div>
            ))}
          </Card>
        </div>
      )}
    </div>
  );
}

function AddApp({ busy, onAdd }: { busy: boolean; onAdd: (process: string, tier: Tier) => Promise<boolean> }) {
  const { t } = useT();
  const toast = useToast();
  const [v, setV] = useState("");
  const [tier, setTier] = useState<Tier>("record");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const p = v.trim();
    if (!p) return toast(t("common.needName"), "warn");
    if (await onAdd(p, tier)) setV("");
  };
  return (
    <form className="mt-2.5 flex flex-wrap gap-2" onSubmit={submit}>
      <label className="sr-only" htmlFor="add-app">{t("apps.addLabel")}</label>
      <input id="add-app" name="process" className="sr-field min-w-0 flex-[1_1_200px]" autoComplete="off" spellCheck={false}
        placeholder={t("apps.addPh")} value={v} onChange={(e) => setV(e.target.value)} />
      <label className="sr-only" htmlFor="add-tier">{t("apps.addTier")}</label>
      <select id="add-tier" name="tier" className="sr-field w-auto" value={tier} onChange={(e) => setTier(e.target.value as Tier)}>
        {TIERS.map((x) => <option key={x} value={x}>{t(`tier.${x}`)}</option>)}
      </select>
      <button className="sr-btn sr-btn-primary" type="submit" disabled={busy}><Plus size={15} aria-hidden />{t("common.add")}</button>
    </form>
  );
}

function AddExclude({ kind, busy, onAdd }: { kind: "host" | "title"; busy: boolean; onAdd: (v: string) => Promise<boolean> }) {
  const { t } = useT();
  const [v, setV] = useState("");
  const id = `add-ex-${kind}`;
  return (
    <form className="mt-2.5 flex flex-wrap gap-2" onSubmit={async (e) => {
      e.preventDefault();
      const s = v.trim();
      if (s && (await onAdd(s))) setV("");
    }}>
      <label className="sr-only" htmlFor={id}>{t(kind === "title" ? "apps.exTitle" : "apps.exHost")}</label>
      <input id={id} className="sr-field min-w-0 flex-[1_1_200px]" autoComplete="off" spellCheck={false}
        placeholder={t(kind === "title" ? "apps.exTitlePh" : "apps.exHostPh")} value={v} onChange={(e) => setV(e.target.value)} />
      <button className="sr-btn sr-btn-line" type="submit" disabled={busy || !v.trim()}><Plus size={15} aria-hidden />{t("common.add")}</button>
    </form>
  );
}
