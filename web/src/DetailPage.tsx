// 原图核验（对应 app.py 的 render_detail / render_context / render_lifecycle）。
// 地址：#/frame/<frame_id>?occ=…&q=…&mode=…&sem=…&back=…；origin_* 记住从哪条搜索结果进来，
// 语义贡献行只属于那一帧（换帧后不沿用）。
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, Columns2, Download, Star, Trash2 } from "lucide-react";
import { ApiError, downloadUrl, get, post, type Box, type Detail, type FramePlan, type OccurrencePlan, type Step } from "./api";
import { useT } from "./i18n";
import { fmtTime, navigate, useLoad } from "./lib";
import { Banner, Dialog, Segmented, Shot, Spinner, Unavailable, useLabelName, useMeta, useToast } from "./ui";

type Mode = "text" | "image";

function Panel({ title, aside, children, className = "" }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`sr-card p-5 ${className}`}>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="sr-label">{title}</h2>
        {aside && <span className="text-xs text-faint">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

function OccLine({ d }: { d: Detail }) {
  const { t } = useT();
  const o = d.occurrence;
  if (!d.time_known)
    return (
      <>
        <p className="font-medium">{t("detail.importUnknown", { t: fmtTime(o.first_seen_ms) })}</p>
        <p className="mt-1 text-graphite">{t("detail.file", { f: o.window_title ?? "" })}</p>
        <p className="mt-2 text-xs text-faint">{t("detail.importUnknownNote")}</p>
      </>
    );
  if (o.source_type === "import")
    return (
      <>
        <p className="font-medium">{t("detail.importKnown", { t: fmtTime(o.first_seen_ms) })}</p>
        <p className="mt-1 text-graphite">{t("detail.file", { f: o.window_title ?? "" })}</p>
      </>
    );
  return (
    <>
      <p className="sr-num font-medium">{t("detail.span", { a: fmtTime(o.first_seen_ms), b: fmtTime(o.last_seen_ms) })}</p>
      <p className="mt-1 break-all text-graphite"><span className="text-ink">{o.app}</span> · {o.window_title}</p>
      <p className="mt-1 text-xs text-faint">{t("detail.samples", { n: o.observations })}</p>
      <p className="mt-2 text-xs leading-relaxed text-faint">{t("detail.spanNote")}</p>
    </>
  );
}

function StepCard({ s, onOpen, current = false }: { s: Step; onOpen?: () => void; current?: boolean }) {
  const cap = s.source_type === "import" ? `${s.window_title ?? ""}` : fmtTime(s.first_seen_ms);
  const body = (
    <>
      <div className={`overflow-hidden rounded-[8px] ring-1 ${current ? "ring-2 ring-marker" : "ring-night-line"}`}>
        {s.image_status === "ok" ? <Shot fid={s.frame_id} width={s.width} height={s.height} thumb={360} />
          : <Unavailable className="aspect-video bg-night-3" />}
      </div>
      <span className={`sr-num mt-1.5 block truncate font-mono text-[10.5px] ${current ? "text-white" : "text-night-faint"}`} title={cap}>{cap}</span>
    </>
  );
  if (!onOpen) return <div aria-current={current || undefined}>{body}</div>;
  return <button onClick={onOpen} className="block w-full text-left opacity-80 transition-opacity hover:opacity-100">{body}</button>;
}

function Context({ d, go, atOrigin, backToOrigin }: { d: Detail; go: (s: Step) => void; atOrigin: boolean; backToOrigin: () => void }) {
  const { t } = useT();
  const [cmp, setCmp] = useState<"none" | "prev" | "next">("none");
  const ctx = d.context;
  if (!ctx) return null;
  const s = ctx.session;
  const other = cmp === "prev" ? ctx.prev : cmp === "next" ? ctx.next : null;
  const gapMsg = cmp === "prev" ? ctx.gaps_before : ctx.gaps_after;
  return (
    <section className="rounded-card bg-night p-5 text-night-text shadow-lift">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 className="sr-label !text-night-faint">{t("ctx.title")}</h2>
        <span className="text-xs text-night-faint">{s.is_import ? t("ctx.import") : t("ctx.session")} · <span className="sr-num">{t("ctx.pos", { i: ctx.index + 1, n: ctx.total })}</span></span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button className="sr-btn h-8 border-night-line bg-night-2 px-3 text-[13px] text-night-text hover:bg-night-3" disabled={!ctx.prev} onClick={() => ctx.prev && go(ctx.prev)}>
            <ChevronLeft size={15} />{t("ctx.prev")}
          </button>
          <button className="sr-btn h-8 border-night-line bg-night-2 px-3 text-[13px] text-night-text hover:bg-night-3" disabled={!ctx.next} onClick={() => ctx.next && go(ctx.next)}>
            {t("ctx.next")}<ChevronRight size={15} />
          </button>
          {!atOrigin && <button className="sr-btn h-8 px-3 text-[13px] text-marker hover:bg-night-2" onClick={backToOrigin}>{t("ctx.origin")}</button>}
          <span className="inline-flex items-center gap-2 text-xs text-night-faint">
            <Columns2 size={14} />
            <Segmented label={t("ctx.compare")} size="sm" dark value={cmp} onChange={setCmp}
              options={[{ value: "none", label: t("ctx.none") }, ...(ctx.prev ? [{ value: "prev" as const, label: t("ctx.prev") }] : []),
                ...(ctx.next ? [{ value: "next" as const, label: t("ctx.next") }] : [])]} />
          </span>
        </div>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-night-faint">
        {s.is_import ? t("ctx.importNote", { b: s.target_title ?? "" })
          : t("ctx.sessionNote", { a: fmtTime(s.started_at_ms), b: s.ended_at_ms ? fmtTime(s.ended_at_ms) : "—", end: s.end_label })}
        {!ctx.prev && ` ${t("ctx.first")}`}{!ctx.next && ` ${t("ctx.last", { end: s.end_label })}`}
      </p>
      {(ctx.gaps_before || ctx.gaps_after) && (
        <div className="mt-3 space-y-1.5 text-xs text-marker">
          {ctx.gaps_before && <p>{(ctx.prev ? t("ctx.gapPrev") : t("ctx.gapStart")) + ctx.gaps_before}</p>}
          {ctx.gaps_after && <p>{(ctx.next ? t("ctx.gapNext") : t("ctx.gapEnd")) + ctx.gaps_after}</p>}
        </div>
      )}

      {other ? (
        <div className="mt-4">
          <div className="grid gap-4 md:grid-cols-2">
            {(cmp === "prev" ? [other, ctx.current] : [ctx.current, other]).map((st) => (
              <StepCard key={st.occurrence_id} s={st} current={st.occurrence_id === ctx.current.occurrence_id} />
            ))}
          </div>
          <p className="mt-3 text-xs text-night-faint">{t("ctx.compareNote")} {gapMsg || t("ctx.noGap")}</p>
        </div>
      ) : (
        <ol className="sr-dark-scroll mt-4 flex gap-3 overflow-x-auto pb-1">
          {ctx.strip.map((st) => (
            <li key={st.occurrence_id} className="relative w-[148px] shrink-0">
              {st.gaps_before && (
                <span className="absolute -left-2.5 top-0 h-[calc(100%-1.4rem)] w-[3px] rounded-full bg-marker/70" title={st.gaps_before} aria-label={t("ctx.gap")} />
              )}
              <StepCard s={st} current={st.pos === ctx.index} onOpen={st.pos === ctx.index ? undefined : () => go(st)} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function OccTable({ rows, currentId, favCol = false, onPick }: {
  rows: Detail["occurrences"]; currentId?: string; favCol?: boolean; onPick?: (id: string) => void;
}) {
  const { t } = useT();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[13px]">
        <thead className="text-xs text-faint">
          <tr className="border-b border-rule">
            {(["occ.first", "occ.last", "occ.n", "occ.app", "occ.window", "occ.source"] as const).map((k) => <th key={k} className="pb-2 pr-4 font-medium">{t(k)}</th>)}
            {favCol && <th className="pb-2 font-medium">{t("occ.favCol")}</th>}{onPick && <th />}
          </tr>
        </thead>
        <tbody className="sr-num">
          {rows.map((o) => (
            <tr key={o.occurrence_id} className={`border-b border-rule/70 last:border-0 ${o.occurrence_id === currentId ? "bg-canvas" : ""}`}>
              <td className="whitespace-nowrap py-2 pr-4">{fmtTime(o.first_seen_ms)}</td>
              <td className="whitespace-nowrap pr-4">{fmtTime(o.last_seen_ms)}</td>
              <td className="pr-4">{o.observations}</td><td className="pr-4">{o.app}</td>
              <td className="max-w-[20rem] truncate pr-4 text-graphite" title={o.window_title ?? ""}>{o.window_title}</td>
              <td className="pr-4">{o.source_label}</td>
              {favCol && <td className="text-alarm">{o.favorited ? t("occ.favDel") : ""}</td>}
              {onPick && <td className="whitespace-nowrap text-right">{o.occurrence_id === currentId ? <span className="text-faint">{t("occ.current")}</span>
                : <button className="font-medium text-cobalt hover:underline" onClick={() => onPick(o.occurrence_id)}>{t("occ.pick")}</button>}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Actions({ d, onChanged, onDeleted }: { d: Detail; onChanged: () => void; onDeleted: (msg: string) => void }) {
  const { t } = useT();
  const toast = useToast();
  const o = d.occurrence;
  const [occPlan, setOccPlan] = useState<OccurrencePlan | null>(null);
  const [framePlan, setFramePlan] = useState<FramePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.data.plan) {
        setFramePlan(e.data.plan as FramePlan);
        toast(e.message, "warn");
      } else toast((e as Error).message, "alarm");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <div className="flex gap-2">
        <button className={`sr-btn flex-1 ${o.favorited ? "sr-btn-line border-marker/60 bg-[#fff8e6]" : "sr-btn-primary"}`} disabled={busy}
          onClick={() => run(async () => {
            toast((await post<{ message: string }>(`/api/occurrences/${o.occurrence_id}/favorite`, { on: !o.favorited })).message);
            onChanged();
          })}>
          <Star size={15} className={o.favorited ? "fill-marker text-marker-ink" : ""} />{o.favorited ? t("detail.unfav") : t("detail.fav")}
        </button>
        {d.image_ok && (
          <a className="sr-btn sr-btn-line flex-1" href={downloadUrl(d.frame_id)} download={`${d.frame_id}.webp`}><Download size={15} />{t("detail.download")}</a>
        )}
      </div>
      <p className="mt-2.5 text-xs leading-relaxed text-faint">
        {o.favorited ? t("detail.favOn") : d.image_status !== "ok" ? t("detail.favNoImage") : t("detail.favHint")}
      </p>
      <div className="mt-4 flex gap-2 border-t border-rule pt-4">
        <button className="sr-btn sr-btn-danger h-9 flex-1 text-[13px]" disabled={busy}
          onClick={() => run(async () => setOccPlan(await get<OccurrencePlan>(`/api/occurrences/${o.occurrence_id}/delete-plan`)))}>
          {t("detail.delOcc")}
        </button>
        <button className="sr-btn sr-btn-danger h-9 flex-1 text-[13px]" disabled={busy}
          onClick={() => run(async () => setFramePlan(await get<FramePlan>(`/api/frames/${d.frame_id}/delete-plan`)))}>
          <Trash2 size={14} />{t("detail.delFrame")}
        </button>
      </div>

      <Dialog open={!!occPlan} title={t("detail.delOccTitle")} onClose={() => setOccPlan(null)}
        footer={<>
          <button className="sr-btn sr-btn-line" onClick={() => setOccPlan(null)}>{t("common.cancel")}</button>
          <button className="sr-btn sr-btn-danger-solid" disabled={busy} onClick={() => run(async () => {
            const r = await post<{ message: string }>(`/api/occurrences/${o.occurrence_id}/delete`);
            setOccPlan(null);
            onDeleted(r.message);
          })}>{t("detail.delOccGo")}</button>
        </>}>
        {occPlan && (
          <div className="space-y-2">
            <p>{t("detail.delOccWhat", { t: fmtTime(occPlan.first_seen_ms) })}</p>
            {occPlan.favorited && <p>{t("detail.delOccFav")}</p>}
            <p className="text-graphite">{occPlan.other_occurrences > 0 ? t("detail.delOccOthers", { n: occPlan.other_occurrences }) : t("detail.delOccLast")}</p>
          </div>
        )}
      </Dialog>

      <Dialog open={!!framePlan} wide title={t("detail.delFrameTitle")} onClose={() => setFramePlan(null)}
        footer={<>
          <button className="sr-btn sr-btn-line" onClick={() => setFramePlan(null)}>{t("common.cancel")}</button>
          <button className="sr-btn sr-btn-danger-solid" disabled={busy} onClick={() => run(async () => {
            const r = await post<{ message: string }>(`/api/frames/${d.frame_id}/delete`, { confirmed_favorites: framePlan!.favorites });
            setFramePlan(null);
            onDeleted(r.message);
          })}>{t("detail.delFrameGo")}</button>
        </>}>
        {framePlan && (
          <div className="space-y-4">
            <Banner tone="alarm">
              {t("detail.delFrameWhat", { n: framePlan.occurrences.length, fav: framePlan.favorites.length ? t("detail.delFrameFav", { n: framePlan.favorites.length }) : "" })}
            </Banner>
            <OccTable rows={framePlan.occurrences} favCol />
          </div>
        )}
      </Dialog>
    </div>
  );
}

export default function DetailPage({ fid, params }: { fid: string; params: URLSearchParams }) {
  const { t } = useT();
  const toast = useToast();
  const labelName = useLabelName();
  const meta = useMeta();
  const mode: Mode = params.get("mode") === "image" ? "image" : "text";
  const occ = params.get("occ") ?? undefined;
  const q = params.get("q") ?? "";
  const back = params.get("back");
  const originFid = params.get("origin_fid") ?? fid;
  const originOcc = params.get("origin_occ") ?? occ;
  const sem = (params.get("sem") ?? "").split(",").filter(Boolean).map(Number);
  const [zoom, setZoom] = useState<"fit" | "px">("fit");
  const [focus, setFocus] = useState<number | null>(null);

  const det = useLoad(() => post<Detail>(`/api/frames/${fid}/detail`, {
    occurrence_id: occ, q, mode, case_sensitive: params.get("case") === "1", relaxed: params.get("relaxed") === "1",
    semantic_lines: fid === originFid ? sem : [],
  }), [params.toString(), fid]);

  const keep = { q, mode: mode === "image" ? "image" : undefined, case: params.get("case") ?? undefined,
    relaxed: params.get("relaxed") ?? undefined, sem: params.get("sem") ?? undefined, back: back ?? undefined,
    origin_fid: originFid, origin_occ: originOcc };
  const goStep = (s: { frame_id: string; occurrence_id: string }) => navigate(`/frame/${s.frame_id}`, { ...keep, occ: s.occurrence_id });
  const leave = () => (back ? (window.location.hash = back) : navigate("/"));

  const d = det.data;
  const linesRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    // 打开时把 OCR 原文滚到第一条标记行（只滚列表本身，不动整页）
    const ol = linesRef.current;
    const li = ol?.querySelector<HTMLElement>("[data-mark]");
    if (ol && li) ol.scrollTop = Math.max(0, li.offsetTop - ol.clientHeight / 3);
  }, [d]);
  const boxes: (Box & { line: number })[] = d
    ? [...d.sem_lines.map((i) => ({ kind: "sem" as const, quad: d.lines[i].quad, line: i })),
       ...d.lex_lines.map((i) => ({ kind: "lex" as const, quad: d.lines[i].quad, line: i }))]
    : [];
  const boxOf = (line: number) => boxes.findIndex((b) => b.line === line);
  const dpr = window.devicePixelRatio || 1;

  return (
    <div className="animate-rise space-y-5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <button className="sr-btn sr-btn-ghost -ml-3" onClick={leave}><ArrowLeft size={16} />{back ? t("detail.back") : t("detail.backSearch")}</button>
        <span className="h-5 w-px bg-rule-strong" />
        <h1 className="text-[17px] font-semibold tracking-tight">{t("detail.title")}</h1>
        {q && <span className="min-w-0 truncate rounded-full border border-rule bg-surface px-3 py-1 text-[13px] text-graphite">{t("detail.query")}<span className="text-ink">{q}</span></span>}
      </div>

      {det.loading && !d ? <div className="py-24 text-center"><Spinner /></div> : det.error ? <Banner tone="alarm">{det.error}</Banner> : d && (
        <>
          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
            <section className="overflow-hidden rounded-card bg-night shadow-lift">
              <div className="flex flex-wrap items-center gap-3 border-b border-night-line px-4 py-3">
                {d.image_ok && (
                  <Segmented label="zoom" size="sm" dark value={zoom} onChange={setZoom}
                    options={[{ value: "fit", label: t("detail.fit") }, { value: "px", label: t("detail.px") }]} />
                )}
                <div className="flex flex-wrap items-center gap-3 text-xs text-night-faint">
                  {mode === "image" ? <span>{t("detail.legendImage")}</span> : d.lex_lines.length || d.sem_lines.length ? (
                    <>
                      {d.lex_lines.length > 0 && <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2.5 w-3.5 rounded-[3px] bg-marker" />{t("detail.legendLex", { n: d.lex_lines.length })}</span>}
                      {d.sem_lines.length > 0 && <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2.5 w-3.5 rounded-[3px] border border-[#6f8ff5] bg-cobalt/40" />{t("detail.legendSem", { n: d.sem_lines.length })}</span>}
                    </>
                  ) : <span>{t("detail.legendNone")}</span>}
                </div>
                <span className="sr-num ml-auto font-mono text-[11px] text-night-faint">
                  {t("detail.dims", { w: d.width, h: d.height, m: String(d.capture_meta.method ?? "—"), d: String(d.capture_meta.dpi ?? "—") })}
                </span>
              </div>
              <div className={`sr-dark-scroll p-4 sm:p-6 ${zoom === "px" ? "max-h-[78vh] overflow-auto" : ""}`}>
                {d.image_status === "expired" ? <Banner tone="warn">{t("detail.expired")}</Banner>
                  : !d.image_ok ? <Banner tone="warn">{t("detail.missing")}</Banner>
                    : (
                      <div className="mx-auto overflow-hidden rounded-[6px] shadow-[0_20px_60px_-20px_rgb(0_0_0/0.8)] ring-1 ring-white/5"
                        style={zoom === "px" ? { width: d.width / dpr } : undefined}>
                        <Shot key={d.frame_id} fid={d.frame_id} width={d.width} height={d.height} boxes={boxes} focus={focus} animate />
                      </div>
                    )}
              </div>
            </section>

            <div className="space-y-5 xl:sticky xl:top-6">
              <Panel title={t("detail.occurrence")}>
                <div className="text-sm"><OccLine d={d} /></div>
                {(d.label || !!meta?.labels.length) && (
                  <p className="mt-3 border-t border-rule pt-3 text-xs text-graphite">
                    {d.label ? t("detail.label", { l: labelName(d.label.label), s: d.label.score.toFixed(2) }) : t("detail.labelPending")}
                  </p>
                )}
              </Panel>
              <Panel title={t("detail.actions")}><Actions d={d} onChanged={det.reload} onDeleted={(msg) => { toast(msg); leave(); }} /></Panel>
              <Panel title={t("detail.ocr")} aside={t("detail.ocrLines", { n: d.lines.length })}>
                {d.lines.length ? (
                  <ol ref={linesRef} className="relative -mx-1 max-h-[40vh] overflow-y-auto font-mono text-[12px] leading-[1.7]">
                    {d.lines.map((ln, i) => {
                      const k = d.lex_lines.includes(i) ? "lex" : d.sem_lines.includes(i) ? "sem" : null;
                      const bi = k ? boxOf(i) : -1;
                      return (
                        <li key={ln.line_no} data-mark={k ?? undefined}
                          onMouseEnter={() => bi >= 0 && setFocus(bi)} onMouseLeave={() => setFocus(null)}
                          className={`flex gap-3 break-all rounded-[5px] px-1 ${k === "lex" ? "bg-marker/20" : k === "sem" ? "bg-cobalt/[0.09]" : ""}`}>
                          <span className="sr-num w-6 shrink-0 select-none text-right text-faint/70">{i + 1}</span>
                          <span className="min-w-0">{ln.text}</span>
                        </li>
                      );
                    })}
                  </ol>
                ) : <p className="text-xs text-faint">{d.text || t("detail.ocrEmpty")}</p>}
              </Panel>
            </div>
          </div>

          <Context d={d} go={goStep} atOrigin={d.occurrence.occurrence_id === originOcc}
            backToOrigin={() => originOcc && goStep({ frame_id: originFid, occurrence_id: originOcc })} />

          <Panel title={t("occ.title")} aside={t("occ.sub")}>
            <OccTable rows={d.occurrences} currentId={d.occurrence.occurrence_id}
              onPick={d.occurrences.length > 1 ? (id) => goStep({ frame_id: d.frame_id, occurrence_id: id }) : undefined} />
          </Panel>
        </>
      )}
    </div>
  );
}
