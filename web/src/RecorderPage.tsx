// 录制页（对应 app.py 侧栏）：状态、窗口选择与控制、索引进度、存储与保留、失败待办、规则。
// 状态由外壳每 2 秒轮询后经 StatusCtx 传入；录制状态以后台 ACK 为准，按钮只发请求。
import { useState, type ReactNode } from "react";
import { Bug, Camera, ChevronDown, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";
import { get, post, type Failed, type Status, type WindowItem } from "./api";
import { useT } from "./i18n";
import { fmtAge, fmtTime, useLoad } from "./lib";
import { Banner, Dot, PageHeader, Spinner, stateOf, useMeta, useStatus, useToast } from "./ui";

const GIB = 1024 ** 3;

function Card({ title, children, className = "", aside }: { title: string; children: ReactNode; className?: string; aside?: ReactNode }) {
  return (
    <section className={`sr-card p-5 ${className}`}>
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="sr-label">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Metric({ label, value, help, accent = false }: { label: string; value: string | number; help?: string; accent?: boolean }) {
  return (
    <div title={help} className="rounded-[11px] border border-rule bg-canvas/60 px-3.5 py-3">
      <div className="text-xs text-graphite">{label}</div>
      <div className={`sr-num mt-1 text-[22px] font-semibold leading-none tracking-tight ${accent ? "text-marker-ink" : ""}`}>{value}</div>
    </div>
  );
}

function useSend() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const send = async (body: Record<string, unknown>) => {
    setBusy(true);
    try {
      toast((await post<{ message: string }>("/api/control", body)).message, "info");
    } catch (e) {
      toast((e as Error).message, "alarm");
    } finally {
      setBusy(false);
    }
  };
  return { busy, send };
}

function StatusHero({ s }: { s: Status | null }) {
  const { t } = useT();
  const { busy, send } = useSend();
  const st = stateOf(s);
  if (!s) return <section className="sr-card p-6"><Spinner /></section>;
  const c = s.control;
  const now = s.now_ms;
  const recording = s.alive && c.actual_state === "recording";
  const debug = recording && c.actual_mode === "debug";
  const left = Math.max(0, Math.floor(((c.mode_until_ms ?? now) - now) / 1000));
  const mmss = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
  const headline = !s.alive ? t("rec.offline") : s.pending ? t(c.desired_state === "recording" ? "rec.pendingOn" : "rec.pendingOff")
    : recording ? t("rec.recording") : `${t("rec.idle")}${c.state_reason ? " · " + c.state_reason : ""}`;
  return (
    <section className="relative overflow-hidden rounded-card bg-night p-6 text-night-text shadow-lift">
      <div className="pointer-events-none absolute -right-16 -top-24 h-64 w-64 rounded-full bg-[radial-gradient(closest-side,rgb(242_180_0/0.16),transparent)]" />
      <div className="relative flex items-start gap-3">
        <span className="mt-2"><Dot tone={st.tone} size={10} /></span>
        <div className="min-w-0 flex-1">
          <p className="text-[20px] font-semibold tracking-tight text-white">{headline}</p>
          {recording && c.target_title && (
            <p className="mt-1 truncate text-sm text-night-faint" title={c.target_title}><span className="text-night-text">{c.target_process}</span> · {c.target_title}</p>
          )}
          {!s.alive && <p className="mt-1.5 font-mono text-xs text-night-faint">{t("rec.start")}</p>}
          {recording && !debug && <p className="mt-3 text-sm text-night-faint">{t("rec.normal")}</p>}
        </div>
      </div>
      {debug && (
        <div className="relative mt-4 flex flex-wrap items-center gap-3 rounded-[11px] border border-alarm/40 bg-alarm/10 px-4 py-3 text-sm">
          <Bug size={16} className="text-[#ff8a80]" />
          <span className="flex-1">{t("rec.debug", { left: mmss })}</span>
          <button className="sr-btn h-8 bg-white text-ink hover:bg-white/90" disabled={busy} onClick={() => send({ action: "normal" })}>{t("rec.exitDebug")}</button>
        </div>
      )}
      <div className="relative mt-4 space-y-1.5 text-[13px] text-night-faint empty:hidden">
        {s.alive && c.capture_note && <p className="text-marker">{c.capture_note}</p>}
        {c.mode_reason && c.actual_mode === "normal" && <p>{c.mode_reason}</p>}
        {c.snapshot_result && <p>{t("rec.snapshot", { t: fmtAge(c.snapshot_at_ms, now), r: c.snapshot_result })}</p>}
        {s.alive && c.hotkey_status && <p>{c.hotkey_status}</p>}
      </div>
    </section>
  );
}

function Controls({ s }: { s: Status | null }) {
  const { t } = useT();
  const wins = useLoad(() => get<{ windows: WindowItem[]; problem?: string }>("/api/windows"), []);
  const [hwnd, setHwnd] = useState("");
  const { busy, send } = useSend();
  const list = wins.data?.windows ?? [];
  const picked = list.find((w) => String(w.hwnd) === hwnd);
  const recording = !!s && s.alive && s.control.actual_state === "recording";
  return (
    <Card title={t("rec.controls")}>
      <label className="mb-1.5 block text-xs font-medium text-graphite" htmlFor="win">{t("rec.window")}</label>
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <select id="win" className="sr-field w-full min-w-0 appearance-none pr-9" value={hwnd} onChange={(e) => setHwnd(e.target.value)}>
            <option value="">{wins.loading ? t("rec.listing") : t("rec.pick")}</option>
            {list.map((w) => (
              <option key={w.hwnd} value={w.hwnd}>{w.title.slice(0, 60)} — {w.process}{w.allowed ? "" : t("rec.denied")}</option>
            ))}
          </select>
          <ChevronDown size={16} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-faint" />
        </div>
        <button className="sr-btn sr-btn-line w-10 px-0" title={t("rec.refresh")} aria-label={t("rec.refresh")} onClick={wins.reload}><RefreshCw size={15} /></button>
      </div>
      {wins.data?.problem && <Banner tone="warn" className="mt-3">{wins.data.problem}</Banner>}
      {picked && !picked.allowed && <Banner tone="warn" className="mt-3">{t("rec.cannot", { r: picked.reason ?? "" })}</Banner>}
      <div className="mt-4 flex flex-wrap gap-2">
        <button className="sr-btn sr-btn-primary" disabled={busy || !picked || !picked.allowed} onClick={() => send({ action: "start", hwnd: picked?.hwnd })}>
          <Play size={15} />{t("rec.btnStart")}
        </button>
        <button className="sr-btn sr-btn-line" disabled={busy} onClick={() => send({ action: "pause" })}><Pause size={15} />{t("rec.btnPause")}</button>
        <span className="mx-1 hidden w-px self-stretch bg-rule sm:block" />
        <button className="sr-btn sr-btn-line" disabled={busy || !recording} title={t("rec.btnDebugTip")} onClick={() => send({ action: "debug" })}><Bug size={15} />{t("rec.btnDebug")}</button>
        <button className="sr-btn sr-btn-line" disabled={busy || !recording} title={t("rec.btnSnapTip")} onClick={() => send({ action: "snapshot" })}><Camera size={15} />{t("rec.btnSnap")}</button>
      </div>
    </Card>
  );
}

function IndexCard({ s }: { s: Status }) {
  const { t } = useT();
  const ix = s.index as Record<string, number>;
  const now = s.now_ms;
  const c = s.control;
  const vec = [ix.text_oldest_ms, ix.image_oldest_ms].filter(Boolean);
  return (
    <Card title={t("rec.index")}>
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        <Metric label={t("rec.m.backlog")} value={ix.backlog} help={t("rec.m.backlogTip")} accent={ix.backlog > 0} />
        <Metric label={t("rec.m.oldest")} value={ix.backlog ? fmtAge(ix.backlog_oldest_ms, now) : "—"} />
        <Metric label={t("rec.m.text")} value={ix.text_backlog} help={t("rec.m.textTip")} />
        <Metric label={t("rec.m.image")} value={ix.image_backlog} help={t("rec.m.imageTip")} />
        <Metric label={t("rec.m.frames")} value={ix.frames.toLocaleString()} />
        <Metric label={t("rec.m.occ")} value={ix.occurrences.toLocaleString()} />
      </div>
      <p className="sr-num mt-4 text-xs leading-relaxed text-faint">
        {t("rec.health", {
          mb: (ix.image_bytes / 1048576).toFixed(1), f: ix.failed, e: ix.embed_failed,
          v: vec.length ? fmtAge(Math.min(...vec), now) : "—", c: fmtAge(c.last_capture_ms, now), h: fmtAge(c.heartbeat_ms, now),
        })}
      </p>
    </Card>
  );
}

function StorageCard({ s }: { s: Status }) {
  const { t } = useT();
  const meta = useMeta();
  const m = s.maintenance;
  const pct = m ? Math.min(100, (m.usage_bytes / m.quota_bytes) * 100) : 0;
  return (
    <Card title={t("rec.storage")}>
      {m ? (
        <>
          <div className="flex items-baseline justify-between text-sm">
            <span className="sr-num font-medium">{t("rec.usage", { u: (m.usage_bytes / GIB).toFixed(2), q: (m.quota_bytes / GIB).toFixed(0) })}</span>
            <span className="sr-num text-xs text-faint">{t("rec.free", { g: (m.free_bytes / GIB).toFixed(1) })}</span>
          </div>
          <div className="mt-2.5 h-2 overflow-hidden rounded-full bg-well">
            <div className={`h-full rounded-full ${pct > 90 ? "bg-alarm" : "bg-ink"}`} style={{ width: `${pct}%` }} />
          </div>
          <p className="mt-3 text-xs leading-relaxed text-graphite">
            {t("rec.retention", { i: meta?.retention.image_days ?? "", r: meta?.retention.record_days ?? "", n: s.favorites })}
          </p>
          <p className="mt-1 text-xs text-faint">{t("rec.maint", { t: fmtAge(m.at_ms, s.now_ms) })}</p>
          {m.paused && <Banner tone="alarm" className="mt-3">{m.reason}</Banner>}
        </>
      ) : <p className="text-sm text-graphite">{t("rec.noMaint", { n: s.favorites })}</p>}
    </Card>
  );
}

function FailedCard() {
  const { t } = useT();
  const toast = useToast();
  const f = useLoad(() => get<Failed>("/api/failed"), []);
  const [busy, setBusy] = useState(false);
  const summ = Object.entries(f.data?.summary ?? {});
  const nOk = summ.reduce((a, [, v]) => a + v.retryable, 0);
  return (
    <Card title={t("rec.failed")} aside={summ.length > 0 && (
      <button className="sr-btn sr-btn-line h-8 text-[13px]" disabled={!nOk || busy} onClick={async () => {
        setBusy(true);
        try {
          toast((await post<{ message: string }>("/api/failed/retry")).message);
          f.reload();
        } catch (e) {
          toast((e as Error).message, "alarm");
        } finally {
          setBusy(false);
        }
      }}><RotateCcw size={14} />{t("rec.retryAll", { n: nOk })}</button>
    )}>
      {f.loading && !f.data ? <Spinner /> : f.error ? <Banner tone="alarm">{f.error}</Banner> : !summ.length ? (
        <p className="text-sm text-graphite">{t("rec.noFailed")}</p>
      ) : (
        <div className="space-y-3 text-sm">
          {summ.map(([k, v]) => (
            <p key={k}>
              <span className="font-medium">{t("rec.failedLine", { name: v.name, a: v.retryable, b: v.not_retryable })}</span>
              <span className="ml-2 font-mono text-xs text-faint">{v.errors.map(([e, n]) => `${e} ×${n}`).join(" · ")}</span>
            </p>
          ))}
          <ul className="divide-y divide-rule rounded-[11px] border border-rule">
            {f.data!.jobs.map((j) => (
              <li key={j.job_id} className="px-3.5 py-2.5 text-xs">
                <div className="flex flex-wrap gap-x-3 text-graphite">
                  <span className="sr-num">{fmtTime(j.captured_at_ms)}</span><span>{j.app}</span><span>{j.stage_name}</span>
                  <span>{t("rec.attempts", { n: j.attempts })}</span>
                  {!j.retryable && <span className="text-alarm">{t("rec.notRetryable", { w: j.why_not })}</span>}
                </div>
                <div className="mt-1 break-all font-mono text-[11px] text-faint">{j.last_error.slice(0, 120)}</div>
              </li>
            ))}
          </ul>
          <p className="text-xs text-faint">{t("rec.retryNote")}</p>
        </div>
      )}
    </Card>
  );
}

export default function RecorderPage() {
  const { t } = useT();
  const s = useStatus();
  const meta = useMeta();
  return (
    <div className="animate-rise">
      <PageHeader title={t("rec.title")} sub={t("rec.sub")} />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <div className="space-y-5">
          <StatusHero s={s} />
          <Controls s={s} />
          <FailedCard />
        </div>
        <div className="space-y-5">
          {s && <IndexCard s={s} />}
          {s && <StorageCard s={s} />}
          <Card title={t("rec.rules")}>
            <ul className="space-y-1.5 text-sm">
              {meta?.rules.map((r, i) => <li key={i} className="break-all">{r}</li>)}
            </ul>
            <p className="mt-3 text-xs text-faint">{t("rec.rulesNote")}</p>
          </Card>
        </div>
      </div>
    </div>
  );
}
