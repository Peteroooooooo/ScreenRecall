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
  const pick = useRef<HTMLInputElement>(null);
  const openPicker = () => {
    const el = pick.current;
    if (!el) return;
    try {
      el.showPicker();
    } catch {
      el.focus();
      el.click();
    }
  };
  const data = tl.data;

  return (
    <div>
      <PageHeader title={head} sub={t("tl.sub")}>
        <div className="flex w-full flex-wrap items-center gap-1.5 sm:w-auto" role="group" aria-label={t("tl.pick")}>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("tl.prev")} onClick={() => setDay(addDays(day, -1))}>
            <ChevronLeft size={15} aria-hidden />
          </button>
          <span className="relative min-w-0 flex-1 sm:flex-none">
            <button className="sr-btn sr-btn-line sr-btn-sm sr-num w-full justify-center font-semibold sm:min-w-[170px]" aria-live="polite"
              aria-label={`${head} · ${fmtMonthDay(d)}，${t("tl.date")}`} onClick={openPicker}>
              <CalendarDays size={16} aria-hidden />{head} · {fmtMonthDay(d)}
            </button>
            {/* 系统日期面板挂在这个看不见的输入框上，由上面的按钮打开 */}
            <input ref={pick} id="day-pick" name="day" type="date" tabIndex={-1} aria-label={t("tl.date")}
              className="pointer-events-none absolute bottom-0 left-0 h-px w-full opacity-0" value={day} max={today}
              onChange={(e) => parseLocalDate(e.target.value) && setDay(e.target.value <= today ? e.target.value : today)} />
          </span>
          <button className="sr-btn sr-btn-line sr-btn-icon sr-btn-sm" aria-label={t("tl.next")} disabled={isToday} onClick={() => setDay(addDays(day, 1))}>
            <ChevronRight size={15} aria-hidden />
          </button>
        </div>
      </PageHeader>

      <Welcome />

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
                : r.kind === "greys" ? <GreyGroup key={`gg${r.items[0].span_id}`} items={r.items} onAdded={() => reload(true)} />
                  : r.item.type === "grey" ? <GreyRow key={`g${r.item.span_id}`} g={r.item} />
                    : <EntryRow key={`e${r.item.visit_id}`} e={r.item} />,
            )}
          </ol>
        </>
      )}
    </div>
  );
}

const WELCOMED = "sr-welcomed";

function readWelcomed(): boolean {
  try {
    return localStorage.getItem(WELCOMED) === "1";
  } catch {
    return false;
  }
}

/** 第一次打开时的三步说明；点"知道了"以后不再出现 */
function Welcome() {
  const { t } = useT();
  const [done, setDone] = useState(readWelcomed);
  if (done) return null;
  const close = () => {
    try {
      localStorage.setItem(WELCOMED, "1");
    } catch {
      /* 存不下：这次关掉，下次还会出现 */
    }
    setDone(true);
  };
  return (
    <section className="sr-card mb-5 p-4 sm:p-5" aria-labelledby="wel-h" data-testid="welcome">
      <h2 id="wel-h" className="text-[15px] font-semibold">{t("wel.title")}</h2>
      <ol className="mt-2.5 list-decimal space-y-1.5 pl-5 text-[13.5px] text-ink-2">
        <li>{t("wel.1")}</li>
        <li>{t("wel.2")} <a className="font-medium text-signal underline-offset-2 hover:underline" href={hrefOf("/apps")}>{t("wel.apps")}</a></li>
        <li>{t("wel.3")} <a className="font-medium text-signal underline-offset-2 hover:underline" href={hrefOf("/search")}>{t("wel.search")}</a></li>
      </ol>
      <button className="sr-btn sr-btn-primary sr-btn-sm mt-3.5" onClick={close}>{t("wel.ok")}</button>
    </section>
  );
}

type Row = { kind: "hour"; hour_ms: number; text: string } | { kind: "item"; item: Entry | Grey } | { kind: "greys"; items: Grey[] };

/** Windows 自己的程序（桌面、报错窗口、认不出名字的）：在它们里面待着不是"在用别的软件"，时间线上不显示 */
const SYSTEM_APPS = new Set(["explorer.exe", "werfault.exe", "unknown", "applicationframehost.exe", "searchhost.exe",
  "shellexperiencehost.exe", "startmenuexperiencehost.exe", "lockapp.exe", "textinputhost.exe", "consent.exe"]);
const isUnlisted = (it: Entry | Grey): it is Grey => it.type === "grey" && it.reason === "unlisted";
const hiddenGrey = (it: Entry | Grey) => isUnlisted(it) && (!it.app || SYSTEM_APPS.has(it.app.toLowerCase()));

/** 条目按开始时间从早到晚；连着的几段"在没录的软件里"合成一行（系统程序不算）；
 * 每小时一句话插在那个整点之后的第一条前面 */
export function rows(d: Timeline): Row[] {
  const hours = [...d.hours].sort((a, b) => a.hour_ms - b.hour_ms);
  const out: Row[] = [];
  let h = 0;
  for (const it of d.items) {
    if (hiddenGrey(it)) continue;
    while (h < hours.length && hours[h].hour_ms <= it.start_ms) out.push({ kind: "hour", ...hours[h++] });
    const last = out[out.length - 1];
    if (isUnlisted(it)) {
      if (last?.kind === "greys") last.items.push(it);
      else out.push({ kind: "greys", items: [it] });
    } else out.push({ kind: "item", item: it });
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

/** 卡片上的名字：窗口标题只是一串程序路径（终端这类）时用软件名 */
export function displayName(e: Entry): string {
  return /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(e.name.trim()) ? appName(e.source.app) : e.name;
}

function EntryRow({ e }: { e: Entry }) {
  const { t } = useT();
  const a = fmtClock(e.start_ms);
  const b = e.open ? t("tl.since") : fmtClock(e.end_ms);
  const name = displayName(e);
  return (
    <li className={ROW}>
      <TimeCell a={a} b={b} />
      <Rail live={e.open} />
      <a href={hrefOf(`/entry/${e.visit_id}`)} aria-label={t("tl.entryLabel", { name, kind: kindLabel(e, t), a, b })}
        className="my-1.5 flex min-w-0 gap-2.5 rounded-card border border-rule bg-surface p-3 text-inherit shadow-card transition-[border-color,box-shadow] hover:border-rule-strong hover:shadow-lift sm:gap-3 sm:px-4 sm:py-3.5">
        <Av app={e.source.app} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 sm:flex-nowrap">
            <span className="min-w-0 truncate font-semibold">{name}</span>
            <span className="sr-num whitespace-nowrap text-[12.5px] text-graphite sm:ml-auto">
              {e.background
                ? `${t("tl.background")} · ${fmtDuration(e.dwell_ms)}`
                : e.open ? t("tl.stillLooking") : fmtDuration(e.dwell_ms)}
            </span>
          </div>
          <div className="text-[12.5px] text-graphite">{kindLabel(e, t)}</div>
          {(e.new_lines > 0 || e.copied > 0) && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {e.new_lines > 0 && <span className="sr-tag sr-tag-ok">{t("tl.lines", { n: fmtInt(e.new_lines) })}</span>}
              {e.copied > 0 && <span className="sr-tag sr-tag-task">{t("tl.copied", { n: e.copied })}</span>}
            </div>
          )}
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
  return null;
}

const GREY_BOX = "my-1.5 rounded-card border border-dashed border-rule-strong px-4 py-2 text-[13px] text-graphite";

/** 暂停 / 锁屏：一行灰字 */
function GreyRow({ g }: { g: Grey }) {
  const { t } = useT();
  const label = g.reason === "paused" ? t(g.end_ms == null ? "grey.paused" : "grey.pausedDone") : t("grey.locked");
  return (
    <li className={ROW} data-testid="grey-row">
      <TimeCell a={fmtClock(g.start_ms)} b={g.end_ms == null ? t("tl.since") : fmtClock(g.end_ms)} />
      <Rail grey />
      <div className={GREY_BOX}>{label}</div>
    </li>
  );
}

/** 连着几段在没录的软件里：收成一行"在用没录的软件：A、B"，点开才是每个软件和"以后录它" */
function GreyGroup({ items, onAdded }: { items: Grey[]; onAdded: () => void }) {
  const { t } = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const apps: Grey[] = [];
  for (const g of items) if (!apps.some((x) => x.app!.toLowerCase() === g.app!.toLowerCase())) apps.push(g);
  const first = items[0];
  const last = items[items.length - 1];
  const add = async (app: string) => {
    setBusy(true);
    try {
      await api.setTier(app, "record");
      toast(t("grey.added", { app: appName(app) }));
      onAdded();
    } catch (e) {
      toast(t("common.saveFail", { e: errText(e, t("common.offline")) }), "alarm");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className={ROW} data-testid="grey-row">
      <TimeCell a={fmtClock(first.start_ms)} b={last.end_ms == null ? t("tl.since") : fmtClock(last.end_ms)} />
      <Rail grey />
      <details className={GREY_BOX}>
        <summary className="cursor-pointer [overflow-wrap:anywhere]">
          {t("tl.greyGroup", { x: apps.map((g) => appName(g.app!)).join(t("common.sep")) })}
        </summary>
        <ul className="mt-1.5">
          {apps.map((g) => (
            <li key={g.app} className="flex flex-wrap items-center gap-2 border-t border-rule py-1.5">
              <span className="min-w-0 flex-1">{t("grey.unlisted", { app: appName(g.app!) })}</span>
              {(g.tier ?? "never") === "never"
                ? <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => void add(g.app!)} disabled={busy}>{t("grey.add")}</button>
                : <span className="sr-tag">{t("grey.nowListed")}</span>}
            </li>
          ))}
        </ul>
      </details>
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
