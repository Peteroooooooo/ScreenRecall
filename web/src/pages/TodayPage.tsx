// 今天：换日期、当天概括与统计、时间线（片段 + 没有记录的时间段，最新在上）。
import { useEffect, useRef, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, LoaderCircle, Play, RefreshCw } from "lucide-react";
import { api, type Day, type GapItem, type PieceItem, type Status } from "../api";
import { hasKey, useT } from "../i18n";
import {
  addDays, appName, errText, fmtClock, fmtDuration, fmtInt, fmtMonthDay, hrefOf, localDate, navigate, parseLocalDate, useLoad, weekday,
} from "../lib";
import { usePauseActions } from "../recording";
import { Av, Empty, LoadError, PageHeader, Skeleton, useStatus, useToast } from "../ui";

/** 进行中的判定：今天结束得最晚的片段，结束时间离现在不到 2 分钟，正在录制且前台是它的应用 */
const LIVE_MS = 120_000;

export default function TodayPage({ params }: { params: URLSearchParams }) {
  const { t } = useT();
  const today = localDate(new Date());
  const asked = params.get("date");
  const date = parseLocalDate(asked) && asked! <= today ? asked! : today;
  const isToday = date === today;
  const { status } = useStatus();
  const { resume } = usePauseActions();
  const day = useLoad((signal) => api.day(date, signal), [date]);

  // 今天的内容在变：revision 变化时后台刷新（不闪加载状态）
  const rev = useRef(status?.revision);
  const reload = day.reload;
  // 别的日子在马上写当天概括时也要刷新（写完 revision 会变）
  const generating = day.data?.summary_state === "generating";
  useEffect(() => {
    if ((!isToday && !generating) || status?.revision == null) return;
    if (rev.current != null && rev.current !== status.revision) reload(true);
    rev.current = status.revision;
  }, [status?.revision, isToday, generating, reload]);

  const d = parseLocalDate(date)!;
  const head = isToday ? t("today.title") : date === addDays(today, -1) ? t("today.yesterday") : weekday(d);
  const setDate = (v: string) => navigate("/", { date: v === today ? undefined : v });
  const data = day.data && day.data.date === date ? day.data : null;

  return (
    <div>
      <PageHeader title={head} sub={t("today.sub")}>
        <div className="flex w-full flex-wrap items-center gap-1.5 sm:w-auto" role="group" aria-label={t("today.pick")}>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("today.prev")} onClick={() => setDate(addDays(date, -1))}>
            <ChevronLeft size={15} aria-hidden />
          </button>
          <span className="sr-num min-w-0 flex-1 text-center font-semibold sm:min-w-[150px] sm:flex-none" aria-live="polite">{head} · {fmtMonthDay(d)}</span>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("today.next")} disabled={isToday} onClick={() => setDate(addDays(date, 1))}>
            <ChevronRight size={15} aria-hidden />
          </button>
          <label className="sr-only" htmlFor="day-pick">{t("today.date")}</label>
          <input id="day-pick" name="date" type="date" className="sr-field h-8 w-auto" value={date} max={today}
            onChange={(e) => parseLocalDate(e.target.value) && setDate(e.target.value <= today ? e.target.value : today)} />
        </div>
      </PageHeader>

      {!data ? (
        day.error ? <LoadError text={t("today.error", { e: errText(day.error, t("common.offline")) })} onRetry={() => day.reload()} /> : <Skeleton rows={4} />
      ) : !data.items.length ? (
        <Empty icon={<CalendarDays size={18} />} title={isToday ? t("today.emptyToday") : t("today.empty")}
          action={isToday && status?.recording === "paused" && (
            <button className="sr-btn sr-btn-primary sr-btn-sm" onClick={resume}><Play size={15} aria-hidden />{t("pause.resume")}</button>
          )}>
          {isToday ? t("today.emptyTodayHint") : t("today.emptyHint")}
        </Empty>
      ) : (
        <>
          <Overview day={data} onRefreshed={() => reload(true)} />
          <ol className="mt-[18px]" aria-label={t("today.timeline")}>
            {data.items.map((it, i) =>
              it.kind === "gap" ? <GapRow key={`g${it.start_ms}-${i}`} gap={it} />
                : <PieceRow key={it.piece_id} p={it} live={isToday && i === liveIndex(data, status)} />,
            )}
          </ol>
        </>
      )}
    </div>
  );
}

/** 进行中的片段：结束得最晚的那个（片段按开始时间排，来回切换窗口时最上面的不一定是最近在用的），
 * 而且前台正是它的应用（前台没有可录的窗口或是排除的窗口时，没有进行中的片段）。没有就返回 -1。 */
export function liveIndex(d: Day, status: Status | null | undefined, now = Date.now()): number {
  const cur = status?.current;
  if (status?.recording !== "recording" || !cur?.app || cur.excluded) return -1;
  let best = -1;
  d.items.forEach((x, i) => {
    if (x.kind === "piece" && (best < 0 || x.end_ms > (d.items[best] as PieceItem).end_ms)) best = i;
  });
  if (best < 0) return -1;
  const p = d.items[best] as PieceItem;
  return now - p.end_ms < LIVE_MS && p.app.toLowerCase() === cur.app.toLowerCase() ? best : -1;
}

function Overview({ day, onRefreshed }: { day: Day; onRefreshed: () => void }) {
  const { t } = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const o = day.overview;
  const st = day.summary_state ?? (day.summary ? (day.summary.status === "stale" ? "stale" : "ok") : "none");
  const generating = st === "generating";
  const refresh = async () => {
    setBusy(true);
    try {
      await api.summarizeDay(day.date);
      toast(t("today.regenStarted"), "info");
      onRefreshed();
    } catch (e) {
      toast(errText(e, t("common.offline")), "alarm");
    } finally {
      setBusy(false);
    }
  };
  const note = generating ? t("today.generatingNote") : st === "failed" ? t("today.failedNote")
    : st === "stale" && !day.summary?.text ? t("today.staleNote") : null;
  const pieces = day.items.filter((x) => x.kind === "piece");
  const gaps = day.items.length - pieces.length;
  const pending = pieces.filter((x) => (x as PieceItem).pending_ocr).length;
  const top = [...o.apps].sort((a, b) => b.ms - a.ms).slice(0, 3);
  return (
    <section className="sr-card flex gap-3.5 p-4 sm:p-5" aria-labelledby="focus-h">
      <span className="hidden h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[9px] border border-rule bg-well text-ink-2 sm:inline-flex" aria-hidden>
        <CalendarDays size={18} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <h2 id="focus-h" className="sr-label">{t("today.focus")}</h2>
          {st === "stale" && <span className="sr-tag sr-tag-warn" title={t("today.staleTip")}>{t("today.stale")}</span>}
          {day.summary_refresh && (
            <button className="sr-btn sr-btn-ghost sr-btn-sm ml-auto" onClick={refresh} disabled={busy || generating}>
              {busy || generating ? <LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" aria-hidden /> : <RefreshCw size={15} aria-hidden />}
              {generating ? t("today.generating") : t("today.regen")}
            </button>
          )}
        </div>
        {day.summary?.text ? <p className="text-base font-medium [overflow-wrap:anywhere]">{day.summary.text}</p>
          : !note && <p className="text-sm text-graphite">{t("today.noSummary")}</p>}
        {note && <p className={`text-sm ${st === "stale" || st === "failed" ? "text-marker-ink" : "text-graphite"}`} aria-live="polite">{note}</p>}
        <div className="sr-num mt-2.5 flex flex-wrap gap-x-[18px] gap-y-1.5 text-[13px] text-graphite">
          <span>{t("today.active", { x: fmtDuration(o.active_ms) })}</span>
          <span>{t("today.pieces", { n: pieces.length })}</span>
          {gaps > 0 && <span>{t("today.gaps", { n: gaps })}</span>}
          <span>{t("today.lines", { n: fmtInt(o.lines_new) })}</span>
          <span>{t("today.frames", { n: fmtInt(o.frames) })}</span>
          {pending > 0 && <span>{t("today.pendingN", { n: pending })}</span>}
        </div>
        {top.length > 0 && (
          <p className="mt-1.5 text-[13px] text-graphite">
            {t("today.topApps")}：{top.map((a) => `${appName(a.app)} ${fmtDuration(a.ms)}`).join(" · ")}
          </p>
        )}
      </div>
    </section>
  );
}

function TimeCell({ a, b }: { a: string; b: string }) {
  return (
    <>
      <div className="sr-num pt-4 text-right text-[11.5px] leading-[1.35] text-graphite sm:text-[12.5px]">
        <b className="block text-[12.5px] font-semibold text-ink sm:text-[13.5px]">{a}</b>{b}
      </div>
    </>
  );
}

function Rail({ live = false, gap = false }: { live?: boolean; gap?: boolean }) {
  return (
    <div className="relative" aria-hidden>
      <span className="absolute inset-y-0 left-1/2 -ml-px w-0.5 bg-rule" />
      <i className={`absolute left-1/2 top-[22px] -ml-[5px] h-2.5 w-2.5 rounded-full border-2 ${live ? "border-signal bg-signal" : gap ? "border-dashed border-faint bg-surface" : "border-graphite bg-surface"}`} />
    </div>
  );
}

const ROW = "grid grid-cols-[50px_14px_minmax(0,1fr)] gap-x-2 sm:grid-cols-[92px_20px_minmax(0,1fr)] sm:gap-x-3";

function PieceRow({ p, live }: { p: PieceItem; live: boolean }) {
  const { t } = useT();
  const a = fmtClock(p.start_ms);
  const b = live ? t("tl.now") : fmtClock(p.end_ms);
  return (
    <li className={ROW}>
      <TimeCell a={a} b={b} />
      <Rail live={live} />
      <a href={hrefOf(`/piece/${p.piece_id}`)} aria-label={t("tl.pieceLabel", { title: p.title || appName(p.app), app: appName(p.app), a, b })}
        className="my-1.5 flex min-w-0 gap-2.5 rounded-card border border-rule bg-surface p-3 text-inherit shadow-card transition-[border-color,box-shadow] hover:border-rule-strong hover:shadow-lift sm:gap-3 sm:px-4 sm:py-3.5">
        <Av app={p.app} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 sm:flex-nowrap">
            <span className="min-w-0 truncate font-semibold">{p.title || appName(p.app)}</span>
            <span className="sr-num whitespace-nowrap text-[12.5px] text-graphite sm:ml-auto">{fmtDuration(p.end_ms - p.start_ms)}</span>
          </div>
          <div className="text-[12.5px] text-graphite">{appName(p.app)}</div>
          {p.summary_line && <p className="mt-1 text-[13.5px] text-ink-2 [overflow-wrap:anywhere]">{p.summary_line}</p>}
          {(live || p.task || p.pending_ocr) && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {live && <span className="sr-tag sr-tag-ok">{t("tl.live")}</span>}
              {p.task && <span className="sr-tag sr-tag-task">{p.task}</span>}
              {p.pending_ocr && <span className="sr-tag sr-tag-warn">{t("tl.pending")}</span>}
            </div>
          )}
        </div>
      </a>
    </li>
  );
}

export function gapReason(reason: string, t: ReturnType<typeof useT>["t"]) {
  const k = `gap.${reason}`;
  return hasKey(k) ? t(k) : reason;
}

function GapRow({ gap }: { gap: GapItem }) {
  const { t } = useT();
  return (
    <li className={ROW}>
      <TimeCell a={fmtClock(gap.start_ms)} b={fmtClock(gap.end_ms)} />
      <Rail gap />
      <div className="sr-hatch my-1.5 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 rounded-card border border-dashed border-rule-strong px-4 py-2.5 text-[13.5px] text-graphite">
        <b className="font-semibold text-ink-2">{t("tl.noRecord")}</b>
        <span>{gapReason(gap.reason, t)}</span>
        {gap.note && <span className="text-faint [overflow-wrap:anywhere]">{gap.note}</span>}
      </div>
    </li>
  );
}
