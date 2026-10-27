// 搜索：按内容段分组的结果，命中处高亮；筛选时间 / 应用 / 任务，排序最相关 / 最后看到，“加载更多”往后翻。
// 条件都在地址里（#/search?q=&range=&app=&task=&sort=）；翻到的页只在本页面里，换条件就从头开始。
import { useEffect, useMemo, useRef, useState } from "react";
import { Image as ImageIcon, Search } from "lucide-react";
import { api, frameThumbUrl, type Group, type SearchSort } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtWhen, highlight, hrefOf, isAbort, navigate, startOfToday, useLoad } from "../lib";
import { Av, Banner, Empty, LoadError, PageHeader, Segmented, Skeleton } from "../ui";
import { FrameViewer, type ViewerState } from "../viewer";

export type Range = "today" | "d7" | "d30" | "all";
const RANGES: Range[] = ["today", "d7", "d30", "all"];
const DEFAULT_RANGE: Range = "d7";
const SORTS: SearchSort[] = ["relevant", "recent"];
const DEFAULT_SORT: SearchSort = "relevant";
export const SEARCH_LIMIT = 30;

export function rangeFrom(r: Range, now = Date.now()): number | undefined {
  if (r === "today") return startOfToday(now);
  if (r === "d7") return now - 7 * 86_400_000;
  if (r === "d30") return now - 30 * 86_400_000;
  return undefined;
}

type More = { groups: Group[]; next: number | null; loading: boolean; error: unknown };
const NO_MORE: More = { groups: [], next: null, loading: false, error: null };

export default function SearchPage({ params }: { params: URLSearchParams }) {
  const { t } = useT();
  const q = (params.get("q") ?? "").trim();
  const rp = params.get("range") as Range | null;
  const range: Range = rp && RANGES.includes(rp) ? rp : DEFAULT_RANGE;
  const sp = params.get("sort") as SearchSort | null;
  const sort: SearchSort = sp && SORTS.includes(sp) ? sp : DEFAULT_SORT;
  const app = params.get("app") ?? "";
  const task = params.get("task") ?? "";
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  const [viewer, setViewer] = useState<ViewerState>(null);

  // 打开搜索页就让服务端在后台加载查询模型：用户打完字时第一次语义检索不用再等
  useEffect(() => {
    const c = new AbortController();
    api.searchWarm(c.signal).catch(() => undefined);
    return () => c.abort();
  }, []);

  // 起点在换条件时算一次（按分钟取整），之后的重绘不改条件
  const fromKey = useMemo(() => {
    const from = rangeFrom(range);
    return from == null ? undefined : Math.floor(from / 60_000) * 60_000;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, q, app, task, sort]);
  const apps = useLoad((signal) => api.apps({ from: fromKey }, signal), [fromKey]);
  const tasks = useLoad((signal) => api.tasks({ from: fromKey }, signal), [fromKey]);
  const cond = { q, from: fromKey, app: app || undefined, task: task || undefined, sort, limit: SEARCH_LIMIT };
  const res = useLoad(
    (signal) => (q ? api.search(cond, signal) : Promise.resolve(null)),
    [q, fromKey, app, task, sort],
  );

  // “加载更多”：同样的条件，从上一页的 next_offset 接着取；换了条件就清空
  const [more, setMore] = useState<More>(NO_MORE);
  const moreCtrl = useRef<AbortController | null>(null);
  useEffect(() => {
    moreCtrl.current?.abort();
    setMore(NO_MORE);
  }, [res.data]);
  useEffect(() => () => moreCtrl.current?.abort(), []);
  const data = res.data;
  const next = more.groups.length ? more.next : (data?.next_offset ?? null);
  const loadMore = () => {
    if (next == null || more.loading) return;
    const c = new AbortController();
    moreCtrl.current?.abort();
    moreCtrl.current = c;
    setMore((m) => ({ ...m, loading: true, error: null }));
    api.search({ ...cond, offset: next }, c.signal)
      .then((r) => setMore((m) => {
        const seen = new Set([...(data?.groups ?? []), ...m.groups].map((g) => g.segment_id));
        return { groups: [...m.groups, ...r.groups.filter((g) => !seen.has(g.segment_id))], next: r.next_offset, loading: false, error: null };
      }))
      .catch((e: unknown) => {
        if (c.signal.aborted || isAbort(e)) return;
        setMore((m) => ({ ...m, loading: false, error: e }));
      });
  };

  const set = (patch: Record<string, string | undefined>) =>
    navigate("/search", {
      q, range: range === DEFAULT_RANGE ? undefined : range, app, task, sort: sort === DEFAULT_SORT ? undefined : sort, ...patch,
    });
  const appList = apps.data?.apps ?? [];
  const taskList = tasks.data?.tasks ?? [];
  const shown = data ? [...data.groups, ...more.groups] : [];

  return (
    <div>
      <PageHeader title={t("search.title")} sub={t("search.sub")} />
      <section className="sr-card p-4 sm:p-5">
        <form role="search" className="flex flex-wrap gap-2" onSubmit={(e) => {
          e.preventDefault();
          set({ q: draft.trim() });
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
            <select className="sr-field sm:w-[200px]" name="task" value={task} onChange={(e) => set({ task: e.target.value || undefined })}>
              <option value="">{t("search.taskPh")}</option>
              {task && !taskList.some((x) => x.task === task) && <option value={task}>{task}</option>}
              {taskList.map((x) => <option key={x.task} value={x.task}>{x.task}（{x.n}）</option>)}
            </select>
          </label>
          <label className="flex min-w-0 flex-[1_1_120px] flex-col gap-1.5 sm:flex-none">
            <span className="text-[12.5px] font-medium text-graphite">{t("search.sort")}</span>
            <select className="sr-field sm:w-[140px]" name="sort" value={sort}
              onChange={(e) => set({ sort: e.target.value === DEFAULT_SORT ? undefined : e.target.value })}>
              {SORTS.map((s) => <option key={s} value={s}>{t(`search.${s}`)}</option>)}
            </select>
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
            <span>{data.total_groups > shown.length
              ? t("search.countMore", { q, n: data.total_groups, m: shown.length })
              : t("search.count", { q, n: shown.length })}</span>
            <span className="text-[12.5px] text-faint">{t(sort === "relevant" ? "search.orderRelevant" : "search.order")}</span>
          </div>
          <div className="space-y-3">
            {shown.map((g, i) => <GroupCard key={g.segment_id} g={g} i={i} onShot={(fid, line) => setViewer({ frames: [fid], index: 0, line })} />)}
          </div>
          {next != null && (
            <div className="mt-4 flex flex-col items-center gap-2">
              {more.error != null && !more.loading && (
                <Banner tone="alarm" className="w-full">{t("search.moreError", { e: errText(more.error, t("common.offline")) })}</Banner>
              )}
              <button type="button" className="sr-btn sr-btn-line" onClick={loadMore} disabled={more.loading} aria-busy={more.loading}>
                {more.loading ? t("search.loadingMore") : t("search.loadMore")}
              </button>
            </div>
          )}
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
          {g.match === "semantic"
            ? <span className="sr-tag" title={t("search.similarTip")}>{t("search.similar")}</span>
            : <span className="sr-tag">{t("search.hits", { n: g.hits_total })}</span>}
          {g.match !== "semantic" && g.hits_total > g.hits.length && <span className="text-[12.5px] text-faint">{t("search.more", { n: g.hits_total - g.hits.length })}</span>}
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
