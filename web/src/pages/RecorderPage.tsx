// 录制控制：当前状态 + 暂停 / 继续、跟随前台的说明、固定窗口、排除名单、对 Agent 隐藏。
import { useState, type FormEvent } from "react";
import { EyeOff, Pause, Pin, Play, Plus, RefreshCw } from "lucide-react";
import { api, type Settings } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtClock, fmtInt, useLoad } from "../lib";
import { PAUSE_KINDS, recView, usePauseActions } from "../recording";
import { hasItem, useSettings } from "../settings";
import { Av, Card, Chips, Dot, LoadError, PageHeader, Skeleton, useStatus, useToast } from "../ui";

export const MAX_PINNED = 2;

export default function RecorderPage() {
  const { t } = useT();
  const st = useSettings();
  return (
    <div>
      <PageHeader title={t("rec.title")} sub={t("rec.sub")} />
      <div className="space-y-4">
        <Hero />
        <Card title={t("rec.how")} id="how-h">
          <ol className="grid gap-3 md:grid-cols-3">
            {(["1", "2", "3"] as const).map((n) => (
              <li key={n} className="rounded-[11px] border border-rule bg-canvas px-3.5 py-3 text-[13.5px]">
                <b className="mb-0.5 block">{t(`rec.how${n}t`)}</b>{t(`rec.how${n}`)}
              </li>
            ))}
          </ol>
        </Card>
        {!st.data ? (
          st.error ? <LoadError text={t("set.loadFail", { e: errText(st.error, t("common.offline")) })} onRetry={() => st.reload()} /> : <Skeleton rows={2} />
        ) : (
          <>
            <div className="grid gap-4 md:grid-cols-2">
              <Pinned s={st.data} save={st.save} />
              <Hidden s={st.data} save={st.save} />
            </div>
            <Exclusions s={st.data} save={st.save} />
          </>
        )}
      </div>
    </div>
  );
}

function Hero() {
  const { t } = useT();
  const { status, lost } = useStatus();
  const { pause, resume } = usePauseActions();
  const v = recView(status, lost, t);
  const title = { recording: t("rec.recording"), paused: t("rec.paused"), stopped: t("rec.stopped"), dead: t("rec.dead"), lost: t("st.lost"), unknown: t("st.connecting") }[v.state];
  let sub: string | null = null;
  const cur = status?.current;
  if (v.state === "recording") {
    if (cur?.excluded) sub = t("rec.excludedFg");
    else if (cur?.app) sub = t("rec.watching", { x: cur.title ? `${cur.title} — ${appName(cur.app)}` : appName(cur.app) });
    else sub = t("rec.noForeground");
  }
  else if (v.state === "paused") sub = status?.pause_until_ms ? t("rec.pausedUntil", { t: until(status.pause_until_ms) }) : t("rec.pausedOpen");
  else if (v.state === "dead") sub = t("rec.deadNote");
  else if (v.state === "lost") sub = t("common.offlineHint");
  return (
    <section className="relative overflow-hidden rounded-card bg-night px-[18px] py-[18px] text-night-text shadow-lift sm:px-6 sm:py-[22px]" aria-labelledby="hero-h">
      <span className="pointer-events-none absolute -right-[60px] -top-[90px] h-60 w-60 rounded-full bg-[radial-gradient(closest-side,rgb(242_180_0/0.16),transparent)]" aria-hidden />
      <h2 id="hero-h" className="flex items-center gap-2.5 text-xl font-semibold text-white"><Dot tone={v.tone} />{title}</h2>
      {sub && <p className="mt-1 text-sm text-night-faint [overflow-wrap:anywhere]">{sub}</p>}
      {status && !lost && (status.pending_ocr > 0 || status.idle_work === "running") && (
        <p className="sr-num mt-1 text-[12.5px] text-night-faint">
          {[status.pending_ocr > 0 && t("rec.pendingOcr", { n: fmtInt(status.pending_ocr) }), status.idle_work === "running" && t("rec.idleRunning")].filter(Boolean).join(" · ")}
        </p>
      )}
      <div className="relative z-[1] mt-4 flex flex-wrap gap-2">
        {v.state === "paused" ? (
          <button className="sr-btn bg-white text-night hover:bg-white/90" onClick={resume}><Play size={15} aria-hidden />{t("pause.resume")}</button>
        ) : (
          PAUSE_KINDS.map((k, i) => (
            <button key={k} disabled={!status || lost} onClick={() => pause(k)}
              className="sr-btn border-night-line bg-night-3 text-white hover:border-night-faint">
              {i === 0 && <Pause size={15} aria-hidden />}{t(k === "m15" ? "rec.pause15" : k === "h1" ? "rec.pause60" : "rec.pauseTomorrow")}
            </button>
          ))
        )}
      </div>
    </section>
  );
}

const until = (ms: number) => {
  const d = new Date(ms);
  const sameDay = new Date().toDateString() === d.toDateString();
  return sameDay ? fmtClock(ms) : `${d.getMonth() + 1}/${d.getDate()} ${fmtClock(ms)}`;
};

type Save = ReturnType<typeof useSettings>["save"];
const samePin = (a: { process: string; title: string }, b: { process: string; title: string }) => a.process === b.process && a.title === b.title;

function AddRow({ id, label, placeholder, onAdd, button, icon }: {
  id: string; label: string; placeholder: string; onAdd: (v: string) => Promise<boolean>; button?: string; icon?: boolean;
}) {
  const { t } = useT();
  const toast = useToast();
  const [v, setV] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const x = v.trim();
    if (!x) {
      toast(t("common.needName"), "warn");
      document.getElementById(id)?.focus();
      return;
    }
    if (await onAdd(x)) setV("");
  };
  return (
    <form className="mt-3 flex gap-2" onSubmit={submit}>
      <label className="sr-only" htmlFor={id}>{label}</label>
      <input id={id} name={id} className="sr-field flex-1" placeholder={placeholder} value={v} onChange={(e) => setV(e.target.value)} />
      <button className="sr-btn sr-btn-line" type="submit">{icon && <Plus size={15} aria-hidden />}{button ?? t("common.add")}</button>
    </form>
  );
}

function Pinned({ s, save }: { s: Settings; save: Save }) {
  const { t } = useT();
  const toast = useToast();
  const wins = useLoad((signal) => api.windows(signal), []);
  const [pick, setPick] = useState("");
  const list = (wins.data?.windows ?? []).filter((w) => !s.pinned.some((p) => p.process === w.process && p.title === w.title));
  const full = s.pinned.length >= MAX_PINNED;
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const w = list.find((x) => String(x.hwnd) === pick);
    if (!w) {
      toast(t("rec.pinPh"), "warn");
      return;
    }
    if (full) {
      toast(t("rec.pinFull", { n: MAX_PINNED }), "warn");
      return;
    }
    const pinned = await save((cur) => cur.pinned.length >= MAX_PINNED || cur.pinned.some((p) => samePin(p, w)) ? null
      : { pinned: [...cur.pinned, { process: w.process, title: w.title, hwnd: w.hwnd }] }, t("rec.pinDone", { x: w.title || w.process }));
    if (pinned) setPick("");
  };
  return (
    <Card title={t("rec.pinned")} id="pin-h" aside={<span className="text-[12.5px] text-faint">{t("rec.pinMax", { n: MAX_PINNED })}</span>}>
      {s.pinned.length ? (
        <ul>
          {s.pinned.map((p, i) => (
            <li key={`${p.process}|${p.title}`} className={`flex min-w-0 items-center gap-3 py-2.5 ${i ? "border-t border-rule" : "pt-0"}`}>
              <Av app={p.process} small />
              <div className="min-w-0 flex-1">
                <div className="truncate">{p.title || appName(p.process)}</div>
                <div className="truncate text-[12.5px] text-faint">
                  {appName(p.process)}
                  {p.found === false && <> · <span className="text-marker-ink">{t("rec.pinMissing")}</span></>}
                </div>
              </div>
              <button className="sr-btn sr-btn-ghost sr-btn-sm" aria-label={t("rec.unpinX", { x: p.title || p.process })}
                onClick={() => save((cur) => ({ pinned: cur.pinned.filter((x) => !samePin(x, p)) }), t("rec.unpinDone", { x: p.title || p.process }))}>
                {t("rec.unpin")}
              </button>
            </li>
          ))}
        </ul>
      ) : <p className="text-sm text-faint">{t("rec.pinNone")}</p>}
      <form className="mt-3 flex gap-2" onSubmit={add}>
        <label className="sr-only" htmlFor="pin-pick">{t("rec.pinPick")}</label>
        <select id="pin-pick" name="pin" className="sr-field min-w-0 flex-1" value={pick} onChange={(e) => setPick(e.target.value)} disabled={full}>
          <option value="">{t("rec.pinPh")}</option>
          {list.map((w) => <option key={w.hwnd} value={String(w.hwnd)}>{w.title} · {appName(w.process)}</option>)}
        </select>
        <button type="button" className="sr-btn sr-btn-ghost sr-btn-icon" aria-label={t("rec.refreshWin")} title={t("rec.refreshWin")} onClick={() => wins.reload()}>
          <RefreshCw size={15} aria-hidden />
        </button>
        <button className="sr-btn sr-btn-line" type="submit" disabled={full || !pick}><Pin size={15} aria-hidden />{t("rec.pin")}</button>
      </form>
    </Card>
  );
}

function Hidden({ s, save }: { s: Settings; save: Save }) {
  const { t } = useT();
  const toast = useToast();
  const [kind, setKind] = useState<"app" | "title">("app");
  const h = s.hidden_from_agents;
  const items = [
    ...h.apps.map((a) => ({ key: `a:${a}`, label: a })),
    ...h.title_keywords.map((k) => ({ key: `t:${k}`, label: t("rec.titleHas", { x: k }) })),
  ];
  const remove = (key: string) => {
    const v = key.slice(2);
    const isApp = key.startsWith("a:");
    void save(({ hidden_from_agents: c }) => ({
      hidden_from_agents: isApp ? { ...c, apps: c.apps.filter((x) => x !== v) } : { ...c, title_keywords: c.title_keywords.filter((x) => x !== v) },
    }), t("common.removed", { x: v }));
  };
  const add = async (v: string) => {
    const isApp = kind === "app";
    if (hasItem(isApp ? h.apps : h.title_keywords, v)) {
      toast(t("common.dup", { x: v }), "warn");
      return false;
    }
    return !!(await save(({ hidden_from_agents: c }) => {
      const list = isApp ? c.apps : c.title_keywords;
      if (hasItem(list, v)) return null;
      return { hidden_from_agents: isApp ? { ...c, apps: [...list, v] } : { ...c, title_keywords: [...list, v] } };
    }, t("common.added", { x: v })));
  };
  return (
    <Card title={t("rec.hidden")} id="hid-h" aside={<EyeOff size={15} className="text-faint" aria-hidden />}>
      <p className="mb-2.5 text-[12.5px] text-graphite">{t("rec.hiddenNote")}</p>
      <Chips items={items} onRemove={remove} empty={t("rec.hiddenNone")} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-[12.5px] text-graphite" htmlFor="hid-kind">{t("rec.hiddenKind")}</label>
        <select id="hid-kind" name="kind" className="sr-field h-8 w-auto" value={kind} onChange={(e) => setKind(e.target.value as "app" | "title")}>
          <option value="app">{t("rec.kindApp")}</option>
          <option value="title">{t("rec.kindTitle")}</option>
        </select>
      </div>
      <AddRow id="hid-add" label={t("rec.hiddenPh")} placeholder={kind === "app" ? t("rec.exAppPh") : t("rec.hiddenPh")} onAdd={add} icon />
    </Card>
  );
}

function Exclusions({ s, save }: { s: Settings; save: Save }) {
  const { t } = useT();
  const toast = useToast();
  const list = (field: "excluded_processes" | "excluded_title_keywords", label: string, ph: string, id: string) => {
    const xs = s[field];
    const add = async (v: string) => {
      if (hasItem(xs, v)) {
        toast(t("common.dup", { x: v }), "warn");
        return false;
      }
      return !!(await save((cur) => (hasItem(cur[field], v) ? null : { [field]: [...cur[field], v] }), t("common.added", { x: v })));
    };
    return (
      <div className="min-w-0">
        <p className="sr-label mb-2">{label}</p>
        <Chips items={xs.map((x) => ({ key: x, label: x }))} empty={t("rec.exNone")}
          onRemove={(k) => void save((cur) => ({ [field]: cur[field].filter((x) => x !== k) }), t("common.removed", { x: k }))} />
        <AddRow id={id} label={label} placeholder={ph} onAdd={add} />
      </div>
    );
  };
  return (
    <Card title={t("rec.excl")} id="ex-h">
      <div className="grid gap-4 md:grid-cols-2">
        {list("excluded_processes", t("rec.exApps"), t("rec.exAppPh"), "ex-app")}
        {list("excluded_title_keywords", t("rec.exTitles"), t("rec.exTitlePh"), "ex-title")}
      </div>
      <p className="mt-3 text-[12.5px] text-faint">{t("rec.pwNote")}</p>
    </Card>
  );
}
