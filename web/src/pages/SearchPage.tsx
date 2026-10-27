// 搜索：按内容段分组的结果，命中处高亮；筛选时间 / 应用 / 任务。条件都在地址里（#/search?q=&range=&app=&task=）。
import { useEffect, useMemo, useState } from "react";
import { Image as ImageIcon, Search } from "lucide-react";
import { api, frameThumbUrl, type Group } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtWhen, highlight, hrefOf, navigate, startOfToday, useLoad } from "../lib";
import { Av, Empty, LoadError, PageHeader, Segmented, Skeleton } from "../ui";
import { FrameViewer, type ViewerState } from "../viewer";

export type Range = "today" | "d7" | "d30" | "all";
const RANGES: Range[] = ["today", "d7", "d30", "all"];
const DEFAULT_RANGE: Range = "d7";
export const SEARCH_LIMIT = 30;

export function rangeFrom(r: Range, now = Date.now()): number | undefined {
  if (r === "today") return startOfToday(now);
  if (r === "d7") return now - 7 * 86_400_000;
  if (r === "d30") return now - 30 * 86_400_000;
  return undefined;
}

export default function SearchPage({ params }: { params: URLSearchParams }) {
  const { t } = useT();
  const q = (params.get("q") ?? "").trim();
  const rp = params.get("range") as Range | null;
  const range: Range = rp && RANGES.includes(rp) ? rp : DEFAULT_RANGE;
  const app = params.get("app") ?? "";
  const task = params.get("task") ?? "";
  const [draft, setDraft] = useState(q);
  const [taskDraft, setTaskDraft] = useState(task);
  useEffect(() => setDraft(q), [q]);
  useEffect(() => setTaskDraft(task), [task]);
  const [viewer, setViewer] = useState<ViewerState>(null);

  // 起点在换条件时算一次（按分钟取整），之后的重绘不改条件
  const fromKey = useMemo(() => {
    const from = rangeFrom(range);
    return from == null ? undefined : Math.floor(from / 60_000) * 60_000;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, q, app, task]);
  const apps = useLoad((signal) => api.apps({ from: fromKey }, signal), [fromKey]);
  const res = useLoad(
    (signal) => (q ? api.search({ q, from: fromKey, app: app || undefined, task: task || undefined, limit: SEARCH_LIMIT }, signal) : Promise.resolve(null)),
    [q, fromKey, app, task],
  );

  const set = (patch: Record<string, string | undefined>) =>
    navigate("/search", { q, range: range === DEFAULT_RANGE ? undefined : range, app, task, ...patch });
  const appList = apps.data?.apps ?? [];
  const data = res.data;

  return (
    <div>
      <PageHeader title={t("search.title")} sub={t("search.sub")} />
      <section className="sr-card p-4 sm:p-5">
        <form role="search" className="flex flex-wrap gap-2" onSubmit={(e) => {
          e.preventDefault();
          set({ q: draft.trim(), task: taskDraft.trim() || undefined });
        }}>
          <label className="sr-only" htmlFor="sq">{t("search.input")}</label>
          <input id="sq" name="q" type="search" autoComplete="off" value={draft} onChange={(e) => setDraft(e.target.value)}
            placeholder={t("search.placeholder")} className="sr-field h-11 min-w-0 flex-[1_1_240px] text-[15.5px]" />
          <button className="sr-btn sr-btn-primary h-11 px-5" type="submit"><Search size={15} aria-hidden />{t("search.go")}</button>
        </form>
        <div className="mt-3.5 flex flex-wrap items-end gap-x-4 gap-y-3">
          <div className="flex min-w-0 flex-col gap-1.5">
            <span id="lbl-time" className="text-[12.5px] font-medium text-graphite">{t("search.time")}</span>
            <Segmented<Range> labelledBy="lbl-time" value={range} onChange={(v) => set({ range: v === DEFAULT_RANGE ? undefined : v })}
              options={RANGES.map((r) => ({ value: r, label: t(`search.${r}`) }))} />
          </div>
          <label className="flex min-w-0 flex-[1_1_150px] flex-col gap-1.5 sm:flex-none">
            <span className="text-[12.5px] font-medium text-graphite">{t("search.app")}</span>
            <select className="sr-field sm:w-[200px]" name="app" value={app} onChange={(e) => set({ app: e.target.value || undefined })}>
              <option value="">{t("search.allApps")}</option>
              {app && !appList.some((a) => a.app === app) && <option value={app}>{appName(app)}</option>}
              {appList.map((a) => <option key={a.app} value={a.app}>{appName(a.app)}（{a.n}）</option>)}
            </select>
          </label>
          <label className="flex min-w-0 flex-[1_1_150px] flex-col gap-1.5 sm:flex-none">
            <span className="text-[12.5px] font-medium text-graphite">{t("search.task")}</span>
            <input className="sr-field sm:w-[200px]" name="task" value={taskDraft} placeholder={t("search.taskPh")}
              onChange={(e) => setTaskDraft(e.target.value)}
              onBlur={() => taskDraft.trim() !== task && set({ task: taskDraft.trim() || undefined })} />
          </label>
        </div>
      </section>

      {!q ? (
        <div className="mt-5"><Empty icon={<Search size={18} />} title={t("search.start")}>{t("search.startHint")}</Empty></div>
      ) : res.error && !res.loading ? (
        <div className="mt-5"><LoadError text={t("search.error", { e: errText(res.error, t("common.offline")) })} onRetry={() => res.reload()} /></div>
      ) : !data || res.loading ? (
        <div className="mt-5"><Skeleton rows={3} /></div>
      ) : !data.groups.length ? (
        <div className="mt-5"><Empty icon={<Search size={18} />} title={t("search.empty", { q })}>{t("search.emptyHint")}</Empty></div>
      ) : (
        <>
          <div className="mb-2.5 mt-5 flex flex-wrap items-center justify-between gap-2 text-[13.5px] text-graphite" aria-live="polite">
            <span>{data.total_groups > data.groups.length
              ? t("search.countMore", { q, n: data.total_groups, m: data.groups.length })
              : t("search.count", { q, n: data.groups.length })}</span>
            <span className="text-[12.5px] text-faint">{t("search.order")}</span>
          </div>
          <div className="space-y-3">
            {data.groups.map((g, i) => <GroupCard key={g.segment_id} g={g} i={i} onShot={(fid, line) => setViewer({ frames: [fid], index: 0, line })} />)}
          </div>
        </>
      )}
      <FrameViewer state={viewer} onClose={() => setViewer(null)} />
    </div>
  );
}

function Highlighted({ text, spans }: { text: string; spans: [number, number][] }) {
  return <>{highlight(text, spans).map((s, i) => (s.hit ? <mark key={i} className="sr-hit">{s.text}</mark> : <span key={i}>{s.text}</span>))}</>;
}

function GroupCard({ g, i, onShot }: { g: Group; i: number; onShot: (fid: string, line: number | null) => void }) {
  const { t } = useT();
  const shot = g.hits.find((h) => h.frame_id);
  const title = g.title || appName(g.app);
  return (
    <article className="sr-card grid animate-rise gap-4 p-4 sm:grid-cols-[minmax(0,1fr)_168px] sm:p-5" style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2.5">
          <Av app={g.app} />
          <div className="min-w-0 flex-1">
            <b className="block truncate">{title}</b>
            <div className="sr-num truncate text-[12.5px] text-graphite">
              {appName(g.app)} · {t("search.first", { x: fmtWhen(g.first_seen_ms) })} · {t("search.last", { x: fmtWhen(g.last_seen_ms) })}
            </div>
          </div>
        </div>
        <ul className="mt-2.5 rounded-[9px] bg-canvas px-3 py-2 text-[13.5px]">
          {g.hits.map((h) => (
            <li key={h.line_id} className="border-rule py-[3px] [overflow-wrap:anywhere] [&+&]:border-t">
              {h.frame_id ? (
                <button type="button" className="text-left hover:underline" onClick={() => onShot(h.frame_id!, h.line_id)}>
                  <Highlighted text={h.text} spans={h.spans} />
                </button>
              ) : <Highlighted text={h.text} spans={h.spans} />}
              <span className="sr-num ml-1.5 whitespace-nowrap text-[11.5px] text-faint">{fmtWhen(h.last_seen_ms)}</span>
            </li>
          ))}
        </ul>
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="sr-tag">{t("search.hits", { n: g.hits_total })}</span>
          {g.hits_total > g.hits.length && <span className="text-[12.5px] text-faint">{t("search.more", { n: g.hits_total - g.hits.length })}</span>}
          <span className="flex-1" />
          {g.piece_id != null && <a className="sr-btn sr-btn-line sr-btn-sm" href={hrefOf(`/piece/${g.piece_id}`)}>{t("search.openPiece")}</a>}
          {shot && (
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => onShot(shot.frame_id!, shot.line_id)}>
              <ImageIcon size={15} aria-hidden />{t("search.viewShot")}
            </button>
          )}
        </div>
      </div>
      {shot && (
        <button className="hidden self-start overflow-hidden rounded-[10px] border border-rule transition-colors hover:border-graphite sm:block"
          onClick={() => onShot(shot.frame_id!, shot.line_id)} aria-label={t("search.shotLabel")}>
          <span className="relative block aspect-[16/10] bg-night-3">
            <img src={frameThumbUrl(shot.frame_id!)} alt="" loading="lazy" decoding="async" className="absolute inset-0 h-full w-full object-cover object-top" />
          </span>
        </button>
      )}
    </article>
  );
}
