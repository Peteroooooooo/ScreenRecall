// 时间线（按天）：每条写名字、几点到几点、看了多久（或“还在看”）、存了几行、复制过几段、小图（或“无图” / “已有图”）、为什么拍；
// 不在名单的软件、暂停、锁屏是灰色行（“在 XX 里（未录）” + “以后录它”）；有每小时一句话时插在整点。点一条进 /entry/:id。
import { useEffect, useRef, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Play } from "lucide-react";
import { api, shotThumbUrl, type Entry, type Grey, type Timeline } from "../api";
import { useT, type T } from "../i18n";
import { addDays, appName, errText, fmtClock, fmtDuration, fmtInt, fmtMonthDay, hrefOf, localDate, navigate, parseLocalDate, useLoad, weekday } from "../lib";
import { isPaused, usePauseActions } from "../recording";
import { Av, Empty, LoadError, PageHeader, Skeleton, useStatus, useToast } from "../ui";

export default function TimelinePage({ params }: { params: URLSearchParams }) {
  const { t } = useT();
  const today = localDate(new Date());
  const asked = params.get("day");
  const day = parseLocalDate(asked) && asked! <= today ? asked! : today;
  const isToday = day === today;
  const { status } = useStatus();
  const { resume } = usePauseActions();
  const tl = useLoad((signal) => api.timeline(day, signal), [day]);

  // 今天的内容在变：录制进程又拍了一次（last_snap_ms 变了）就后台刷新（不闪加载状态）
  const snap = useRef(status?.last_snap_ms);
  const reload = tl.reload;
  useEffect(() => {
    if (!isToday || status?.last_snap_ms == null) return;
    if (snap.current != null && snap.current !== status.last_snap_ms) reload(true);
    snap.current = status.last_snap_ms;
  }, [status?.last_snap_ms, isToday, reload]);

  const d = parseLocalDate(day)!;
  const head = isToday ? t("tl.today") : day === addDays(today, -1) ? t("tl.yesterday") : weekday(d);
  const setDay = (v: string) => navigate("/", { day: v === today ? undefined : v });
  const data = tl.data;

  return (
    <div>
      <PageHeader title={head} sub={t("tl.sub")}>
        <div className="flex w-full flex-wrap items-center gap-1.5 sm:w-auto" role="group" aria-label={t("tl.pick")}>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("tl.prev")} onClick={() => setDay(addDays(day, -1))}>
            <ChevronLeft size={15} aria-hidden />
          </button>
          <span className="sr-num min-w-0 flex-1 text-center font-semibold sm:min-w-[150px] sm:flex-none" aria-live="polite">{head} · {fmtMonthDay(d)}</span>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("tl.next")} disabled={isToday} onClick={() => setDay(addDays(day, 1))}>
            <ChevronRight size={15} aria-hidden />
          </button>
          <label className="sr-only" htmlFor="day-pick">{t("tl.date")}</label>
          <input id="day-pick" name="day" type="date" className="sr-field h-8 w-auto" value={day} max={today}
            onChange={(e) => parseLocalDate(e.target.value) && setDay(e.target.value <= today ? e.target.value : today)} />
        </div>
      </PageHeader>

      {!data ? (
        tl.error ? <LoadError text={t("tl.error", { e: errText(tl.error, t("common.offline")) })} onRetry={() => tl.reload()} /> : <Skeleton rows={4} />
      ) : !data.items.length ? (
        <Empty icon={<CalendarDays size={18} />} title={isToday ? t("tl.emptyToday") : t("tl.empty")}
          action={isToday && isPaused(status) && (
            <button className="sr-btn sr-btn-primary sr-btn-sm" onClick={resume}><Play size={15} aria-hidden />{t("pause.resume")}</button>
          )}>
          {isToday ? t("tl.emptyTodayHint") : t("tl.emptyHint")}
        </Empty>
      ) : (
        <>
          <DaySummary data={data} />
          <ol className="mt-[18px]" aria-label={t("tl.list")}>
            {rows(data).map((r) =>
              r.kind === "hour" ? <HourRow key={`h${r.hour_ms}`} hour_ms={r.hour_ms} text={r.text} />
                : r.item.type === "grey" ? <GreyRow key={`g${r.item.span_id}`} g={r.item} onAdded={() => reload(true)} />
                  : <EntryRow key={`e${r.item.visit_id}`} e={r.item} />,
            )}
          </ol>
        </>
      )}
    </div>
  );
}

type Row = { kind: "hour"; hour_ms: number; text: string } | { kind: "item"; item: Entry | Grey };

/** 条目按开始时间从早到晚；每小时一句话插在那个整点之后的第一条前面 */
export function rows(d: Timeline): Row[] {
  const hours = [...d.hours].sort((a, b) => a.hour_ms - b.hour_ms);
  const out: Row[] = [];
  let h = 0;
  for (const it of d.items) {
    while (h < hours.length && hours[h].hour_ms <= it.start_ms) out.push({ kind: "hour", ...hours[h++] });
    out.push({ kind: "item", item: it });
  }
  while (h < hours.length) out.push({ kind: "hour", ...hours[h++] });
  return out;
}

function DaySummary({ data }: { data: Timeline }) {
  const { t } = useT();
  const entries = data.items.filter((x): x is Entry => x.type === "entry");
  const dwell = entries.reduce((s, e) => s + (e.background ? 0 : e.dwell_ms), 0);
  const lines = entries.reduce((s, e) => s + e.new_lines, 0);
  const copied = entries.reduce((s, e) => s + e.copied, 0);
  return (
    <p className="sr-num flex flex-wrap gap-x-[18px] gap-y-1 text-[13px] text-graphite">
      <span>{t("tl.sumEntries", { n: entries.length })}</span>
      <span>{t("tl.sumDwell", { x: fmtDuration(dwell) })}</span>
      <span>{t("tl.sumLines", { n: fmtInt(lines) })}</span>
      {copied > 0 && <span>{t("tl.sumCopied", { n: copied })}</span>}
    </p>
  );
}

const ROW = "grid grid-cols-[50px_14px_minmax(0,1fr)] gap-x-2 sm:grid-cols-[92px_20px_minmax(0,1fr)] sm:gap-x-3";

function TimeCell({ a, b }: { a: string; b: string }) {
  return (
    <div className="sr-num pt-4 text-right text-[11.5px] leading-[1.35] text-graphite sm:text-[12.5px]">
      <b className="block text-[12.5px] font-semibold text-ink sm:text-[13.5px]">{a}</b>{b}
    </div>
  );
}

function Rail({ live = false, grey = false }: { live?: boolean; grey?: boolean }) {
  return (
    <div className="relative" aria-hidden>
      <span className="absolute inset-y-0 left-1/2 -ml-px w-0.5 bg-rule" />
      <i className={`absolute left-1/2 top-[22px] -ml-[5px] h-2.5 w-2.5 rounded-full border-2 ${live ? "border-signal bg-signal" : grey ? "border-dashed border-faint bg-surface" : "border-graphite bg-surface"}`} />
    </div>
  );
}

/** 出处种类：网页 / 本地文件 / 软件名 */
export function kindLabel(e: { source: { kind: string; app: string } }, t: T): string {
  if (e.source.kind === "url") return t("kind.url");
  if (e.source.kind === "file") return t("kind.file");
  return appName(e.source.app);
}

/** 为什么拍：停下 / 一直在动 / 复制 */
export function reasonsText(reasons: string[], t: T): string | null {
  const known = reasons.filter((r) => r === "still" || r === "forced" || r === "copy") as ("still" | "forced" | "copy")[];
  return known.length ? t("tl.why", { x: known.map((r) => t(`why.${r}`)).join(t("common.sep")) }) : null;
}

function EntryRow({ e }: { e: Entry }) {
  const { t } = useT();
  const a = fmtClock(e.start_ms);
  const b = e.open ? t("tl.since") : fmtClock(e.end_ms);
  const why = reasonsText(e.reasons, t);
  return (
    <li className={ROW}>
      <TimeCell a={a} b={b} />
      <Rail live={e.open} />
      <a href={hrefOf(`/entry/${e.visit_id}`)} aria-label={t("tl.entryLabel", { name: e.name, kind: kindLabel(e, t), a, b })}
        className="my-1.5 flex min-w-0 gap-2.5 rounded-card border border-rule bg-surface p-3 text-inherit shadow-card transition-[border-color,box-shadow] hover:border-rule-strong hover:shadow-lift sm:gap-3 sm:px-4 sm:py-3.5">
        <Av app={e.source.app} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 sm:flex-nowrap">
            <span className="min-w-0 truncate font-semibold">{e.name}</span>
            <span className="sr-num whitespace-nowrap text-[12.5px] text-graphite sm:ml-auto">
              {e.background
                ? `${t("tl.background")} · ${fmtDuration(e.dwell_ms)}`
                : e.open ? t("tl.stillLooking") : fmtDuration(e.dwell_ms)}
            </span>
          </div>
          <div className="text-[12.5px] text-graphite">{kindLabel(e, t)}{e.merged > 0 && ` · ${t("tl.merged", { n: e.merged })}`}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <span className={`sr-tag ${e.new_lines > 0 ? "sr-tag-ok" : ""}`}>{e.new_lines > 0 ? t("tl.lines", { n: fmtInt(e.new_lines) }) : t("tl.noNew")}</span>
            {e.copied > 0 && <span className="sr-tag sr-tag-task">{t("tl.copied", { n: e.copied })}</span>}
            {why && <span className="sr-tag">{why}</span>}
          </div>
        </div>
        <Thumb e={e} />
      </a>
    </li>
  );
}

function Thumb({ e }: { e: Entry }) {
  const { t } = useT();
  const [failed, setFailed] = useState(false);
  if (e.thumb && e.thumb.own && !failed)
    return (
      <span className="hidden w-[112px] shrink-0 self-start overflow-hidden rounded-[8px] border border-rule bg-night-3 sm:block">
        <img src={shotThumbUrl(e.thumb.shot_id)} alt={t("tl.thumbAlt", { x: e.name })} loading="lazy" decoding="async"
          onError={() => setFailed(true)} className="block aspect-[16/10] w-full object-cover object-top" />
      </span>
    );
  return <span className="sr-tag shrink-0 self-start">{e.thumb && !failed ? t("tl.hasShot") : t("tl.noShot")}</span>;
}

function GreyRow({ g, onAdded }: { g: Grey; onAdded: () => void }) {
  const { t } = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const label = g.reason === "unlisted" ? t("grey.unlisted", { app: appName(g.app ?? "") })
    : g.reason === "paused" ? t(g.end_ms == null ? "grey.paused" : "grey.pausedDone") : t("grey.locked");
  const canAdd = g.reason === "unlisted" && !!g.app && (g.tier ?? "never") === "never";
  const add = async () => {
    setBusy(true);
    try {
      await api.setTier(g.app!, "record");
      toast(t("grey.added", { app: appName(g.app!) }));
      onAdded();
    } catch (e) {
      toast(t("common.saveFail", { e: errText(e, t("common.offline")) }), "alarm");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className={ROW} data-testid="grey-row">
      <TimeCell a={fmtClock(g.start_ms)} b={g.end_ms == null ? t("tl.since") : fmtClock(g.end_ms)} />
      <Rail grey />
      <div className="sr-hatch my-1.5 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-card border border-dashed border-rule-strong px-4 py-2.5 text-[13.5px] text-graphite">
        <span className="min-w-0 [overflow-wrap:anywhere]">{label}</span>
        {canAdd && (
          <button className="sr-btn sr-btn-line sr-btn-sm ml-auto" onClick={add} disabled={busy}>{t("grey.add")}</button>
        )}
        {g.reason === "unlisted" && !canAdd && g.tier && <span className="sr-tag ml-auto">{t("grey.nowListed")}</span>}
      </div>
    </li>
  );
}

function HourRow({ hour_ms, text }: { hour_ms: number; text: string }) {
  return (
    <li className={ROW}>
      <div className="sr-num pt-2 text-right text-[12px] font-semibold text-faint">{fmtClock(hour_ms)}</div>
      <Rail grey />
      <p className="my-1 border-l-2 border-marker py-1 pl-3 text-[13.5px] text-ink-2 [overflow-wrap:anywhere]">{text}</p>
    </li>
  );
}
