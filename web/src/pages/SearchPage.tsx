// 查找：搜字、标题、网址；结果从新到旧，一个出处 10 分钟内的命中只显示一条（服务端合并好）；
// 截图上命中的行用黄框标出；“复制过的”单独一组（复制了什么、在哪、几点）。条件都在地址里（#/search?q=&range=）。
import { useEffect, useMemo, useState } from "react";
import { ClipboardCopy, Image as ImageIcon, Search as SearchIcon } from "lucide-react";
import { api, type Clip, type SearchResult } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtClock, fmtWhen, highlight, hrefOf, navigate, safeHttpUrl, sourceName, startOfToday, useLoad } from "../lib";
import { Av, Banner, Card, Empty, LoadError, PageHeader, Segmented, Skeleton } from "../ui";
import { ShotImage, ShotViewer, type ViewerState } from "../viewer";

export type Range = "today" | "d7" | "d30" | "all";
const RANGES: Range[] = ["today", "d7", "d30", "all"];
const DEFAULT_RANGE: Range = "all";
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
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  const [viewer, setViewer] = useState<ViewerState>(null);
  // 起点在换条件时算一次（按分钟取整），之后的重绘不改条件
  const fromMs = useMemo(() => {
    const from = rangeFrom(range);
    return from == null ? undefined : Math.floor(from / 60_000) * 60_000;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, q]);
  const res = useLoad((signal) => (q ? api.search({ q, from_ms: fromMs, limit: SEARCH_LIMIT }, signal) : Promise.resolve(null)), [q, fromMs]);
  const set = (patch: Record<string, string | undefined>) =>
    navigate("/search", { q, range: range === DEFAULT_RANGE ? undefined : range, ...patch });
  const data = res.data;

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
          <button className="sr-btn sr-btn-primary h-11 px-5" type="submit"><SearchIcon size={15} aria-hidden />{t("search.go")}</button>
        </form>
        <div className="mt-3.5 flex min-w-0 flex-col gap-1.5">
          <span id="lbl-time" className="text-[12.5px] font-medium text-graphite">{t("search.time")}</span>
          <Segmented<Range> labelledBy="lbl-time" value={range} onChange={(v) => set({ range: v === DEFAULT_RANGE ? undefined : v })}
            options={RANGES.map((r) => ({ value: r, label: t(`search.${r}`) }))} />
        </div>
      </section>

      {!q ? (
        <div className="mt-5"><Empty icon={<SearchIcon size={18} />} title={t("search.start")}>{t("search.startHint")}</Empty></div>
      ) : res.error && !res.loading ? (
        <div className="mt-5"><LoadError text={t("search.error", { e: errText(res.error, t("common.offline")) })} onRetry={() => res.reload()} /></div>
      ) : !data || res.loading ? (
        <div className="mt-5"><Skeleton rows={3} /></div>
      ) : (
        <>
          {data.error && <Banner tone="warn" className="mt-5">{data.error}</Banner>}
          {!data.results.length && !data.copied.length ? (
            !data.error && <div className="mt-5"><Empty icon={<SearchIcon size={18} />} title={t("search.empty", { q })}>{t("search.emptyHint")}</Empty></div>
          ) : (
            <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-w-0">
                <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 text-[13.5px] text-graphite" aria-live="polite">
                  <span>{t("search.count", { q, n: data.total })}</span>
                  <span className="text-[12.5px] text-faint">{t("search.order")}</span>
                </div>
                {data.results.length ? (
                  <ol className="space-y-3" aria-label={t("search.results")}>
                    {data.results.map((r, i) => (
                      <ResultCard key={`${r.source.source_id}-${r.last_seen_ms}`} r={r} i={i}
                        onShot={(shotId, hits) => setViewer({ shotId, hits })} />
                    ))}
                  </ol>
                ) : <p className="sr-card p-4 text-sm text-graphite">{t("search.noText")}</p>}
              </div>
              <CopiedGroup items={data.copied} />
            </div>
          )}
        </>
      )}
      <ShotViewer state={viewer} onClose={() => setViewer(null)} />
    </div>
  );
}

function Highlighted({ text, spans }: { text: string; spans: [number, number][] }) {
  return <>{highlight(text, spans).map((s, i) => (s.hit ? <mark key={i} className="sr-hit">{s.text}</mark> : <span key={i}>{s.text}</span>))}</>;
}

function ResultCard({ r, i, onShot }: { r: SearchResult; i: number; onShot: (shotId: number, hits: number[]) => void }) {
  const { t } = useT();
  const src = r.source;
  const name = sourceName(src);
  const hitIds = r.hits.map((h) => h.line_id);
  const href = src.kind === "url" ? safeHttpUrl(src.url) : null;
  const when = fmtClock(r.start_ms) === fmtClock(r.end_ms) ? fmtWhen(r.last_seen_ms) : `${fmtWhen(r.start_ms)}–${fmtClock(r.end_ms)}`;
  return (
    <li className="sr-card grid animate-rise gap-4 p-4 sm:grid-cols-[minmax(0,1fr)_200px] sm:p-5" style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2.5">
          <Av app={src.app} />
          <div className="min-w-0 flex-1">
            <b className="block truncate">{name}</b>
            <div className="sr-num truncate text-[12.5px] text-graphite">
              {src.kind === "url" ? t("kind.url") : src.kind === "file" ? t("kind.file") : appName(src.app)} · {when}
            </div>
          </div>
        </div>
        {(src.url || src.path) && (
          <p className="mt-1.5 truncate text-[12.5px] text-graphite">
            {href ? <a className="text-cobalt hover:underline" href={href} target="_blank" rel="noopener noreferrer">{src.url}</a> : (src.url || src.path)}
          </p>
        )}
        {r.hits.length > 0 && (
          <ul className="mt-2.5 rounded-[9px] bg-canvas px-3 py-2 text-[13.5px]">
            {r.hits.map((h) => (
              <li key={h.line_id} className="border-rule py-[3px] [overflow-wrap:anywhere] [&+&]:border-t">
                <Highlighted text={h.text} spans={h.spans} />
                {h.semantic && <span className="sr-tag ml-1.5" title={t("search.similarTip")}>{t("search.similar")}</span>}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          {r.more_hits > 0 && <span className="text-[12.5px] text-faint">{t("search.more", { n: r.more_hits })}</span>}
          <span className="flex-1" />
          {r.visit_id != null && <a className="sr-btn sr-btn-line sr-btn-sm" href={hrefOf(`/entry/${r.visit_id}`)}>{t("search.openEntry")}</a>}
          {r.shot && (
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => onShot(r.shot!.shot_id, hitIds)}>
              <ImageIcon size={15} aria-hidden />{t("search.viewShot")}
            </button>
          )}
        </div>
      </div>
      {r.shot && (
        <button className="self-start overflow-hidden rounded-[10px] border border-rule transition-colors hover:border-graphite"
          onClick={() => onShot(r.shot!.shot_id, hitIds)} aria-label={t("search.shotLabel", { x: name })} data-testid="result-shot">
          <ShotImage shot={r.shot} thumb alt="" />
        </button>
      )}
    </li>
  );
}

function CopiedGroup({ items }: { items: Clip[] }) {
  const { t } = useT();
  return (
    <Card title={<span className="inline-flex items-center gap-2"><ClipboardCopy size={16} aria-hidden />{t("search.copied")}</span>} id="copied-h" className="self-start">
      {items.length ? (
        <ul className="space-y-2.5 text-[13.5px]" aria-labelledby="copied-h" data-testid="copied-group">
          {items.map((c) => (
            <li key={c.clip_id} className="rounded-[9px] bg-canvas px-3 py-2">
              <p className="line-clamp-4 whitespace-pre-wrap [overflow-wrap:anywhere]">{c.text}</p>
              <p className="sr-num mt-1 text-[12px] text-graphite [overflow-wrap:anywhere]">
                {t("search.copiedWhere", { x: c.source ? sourceName(c.source) : c.title || appName(c.app), t: fmtWhen(c.t_ms) })}
              </p>
              {c.visit_id != null && <a className="text-[12px] text-cobalt hover:underline" href={hrefOf(`/entry/${c.visit_id}`)}>{t("search.openEntry")}</a>}
            </li>
          ))}
        </ul>
      ) : <p className="text-sm text-faint">{t("search.noCopied")}</p>}
    </Card>
  );
}
