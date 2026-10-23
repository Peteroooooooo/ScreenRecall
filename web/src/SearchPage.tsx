// 检索（对应 app.py 的搜索表单与结果列表）。查询条件都在地址里（#/?q=…），刷新、后退、深链接都能复现。
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRight, Image as ImageIcon, Quote, ScanText, Search, SlidersHorizontal, Star, X } from "lucide-react";
import { post, type Result, type SearchResponse } from "./api";
import { useT, type Key } from "./i18n";
import { fmtTime, navigate, useLoad } from "./lib";
import { Banner, Chip, HitShot, Segmented, Spinner, Unavailable, useLabelName, useMeta, useStatus } from "./ui";

type Mode = "text" | "image";
const list = (v: string | null) => (v ? v.split(",").filter(Boolean) : []);

export function markTerms(line: string, terms: string[], caseSensitive = false): ReactNode[] {
  const ts = terms.filter((t) => t.length > 0);
  if (!ts.length) return [line];
  const esc = ts.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const re = new RegExp(`(${esc.join("|")})`, caseSensitive ? "g" : "gi");
  return line.split(re).map((part, i) => (i % 2 ? <mark key={i} className="sr-lex">{part}</mark> : part));
}

function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return <span title={title} className="inline-flex h-[22px] items-center gap-1 rounded-full border border-rule bg-canvas px-2 text-[11px] text-graphite">{children}</span>;
}

function ResultCard({ r, rank, mode, terms, caseSensitive, onOpen }: {
  r: Result; rank: number; mode: Mode; terms: string[]; caseSensitive: boolean; onOpen: () => void;
}) {
  const { t } = useT();
  const meta = useMeta();
  const labelName = useLabelName();
  const o = r.latest;
  const when = r.time_unknown ? t("result.timeUnknown", { t: fmtTime(o.last_seen_ms) }) : fmtTime(o.last_seen_ms);
  // 库里一条预测都没有（没跑过分类）时不显示类别，免得满屏"待分类"
  const label = r.label ? t("result.pred", { l: labelName(r.label.label), s: r.label.score.toFixed(2) })
    : meta?.labels.length ? t("result.pending") : null;
  return (
    <article className="group sr-card flex flex-col overflow-hidden transition-[box-shadow,border-color,transform] duration-200 hover:-translate-y-0.5 hover:border-rule-strong hover:shadow-lift"
      style={{ animation: `rise 360ms cubic-bezier(.2,.7,.2,1) ${Math.min(rank, 10) * 35}ms both` }}>
      <button onClick={onOpen} className="relative block text-left" aria-label={t("result.open")}>
        {r.image_ok ? <HitShot fid={r.frame_id} width={r.width} height={r.height} boxes={r.boxes} />
          : <Unavailable text={t("common.imageGone")} />}
        <span className="sr-num absolute left-2.5 top-2.5 rounded-[6px] bg-night/80 px-1.5 py-0.5 font-mono text-[10.5px] font-medium text-white backdrop-blur">#{rank}</span>
        {r.favorited && <span className="absolute right-2.5 top-2.5 rounded-full bg-night/80 p-1.5 backdrop-blur"><Star size={11} className="fill-marker text-marker" /></span>}
      </button>
      <div className="flex flex-1 flex-col gap-2 px-3.5 pb-3.5 pt-3">
        <div className="min-w-0">
          <div className="flex items-baseline justify-between gap-3">
            <p className="sr-num truncate text-[12.5px] font-semibold">{when}</p>
            <span className="max-w-[45%] shrink-0 truncate text-[11.5px] font-medium text-graphite">{o.app}</span>
          </div>
          <p className="truncate text-[12.5px] text-graphite" title={o.window_title ?? ""}>{o.window_title}</p>
        </div>
        {mode !== "image" && (
          <div className="max-h-[3.6rem] overflow-hidden rounded-[8px] bg-canvas px-2.5 py-1.5 font-mono text-[11.5px] leading-[1.55] text-ink/85 [mask-image:linear-gradient(to_bottom,black_65%,transparent)]">
            {r.snippet.length ? r.snippet.map((ln, i) => <p key={i} className="break-all">{markTerms(ln, terms, caseSensitive)}</p>) : <p className="text-faint">{t("result.noText")}</p>}
          </div>
        )}
        <div className="mt-auto flex flex-wrap items-center gap-1">
          {r.sources.length > 0 && <Tag>{t("result.hits", { s: r.sources.join(" + ") })}</Tag>}
          {label && <Tag title={t("result.predTip")}>{label}</Tag>}
          {o.source_type === "import" && <Tag>{t("result.import")}</Tag>}
          {r.occurrences_in_filter > 1 && <Tag>{t("result.occ", { n: r.occurrences_in_filter })}</Tag>}
        </div>
      </div>
    </article>
  );
}

type Form = { q: string; mode: Mode; from: string; to: string; apps: string[]; labels: string[]; case_sensitive: boolean; limit: number; relaxed: boolean };

function CommandBar({ form, setForm, onSubmit, big, showFilters, setShowFilters, nFilters }: {
  form: Form; setForm: (f: (x: Form) => Form) => void; onSubmit: (e: FormEvent) => void; big: boolean;
  showFilters: boolean; setShowFilters: (v: boolean) => void; nFilters: number;
}) {
  const { t } = useT();
  return (
    <form onSubmit={onSubmit}
      className={`flex flex-col gap-2 rounded-[16px] border border-rule bg-surface p-2 shadow-bar transition-shadow focus-within:border-rule-strong focus-within:shadow-lift sm:flex-row sm:items-center ${big ? "sm:p-2.5" : ""}`}>
      <Segmented<Mode> label={t("search.mode")} value={form.mode} onChange={(m) => setForm((f) => ({ ...f, mode: m }))}
        options={[
          { value: "text", label: <><ScanText size={15} />{t("search.text")}</>, title: t("search.textTip") },
          { value: "image", label: <><ImageIcon size={15} />{t("search.image")}</>, title: t("search.imageTip") },
        ]} />
      <div className="relative flex min-w-0 flex-1 items-center">
        <Search size={18} className="pointer-events-none absolute left-3 text-faint" />
        <input
          className={`w-full min-w-0 bg-transparent pl-10 pr-3 outline-none placeholder:text-faint ${big ? "h-12 text-[16px]" : "h-10 text-[15px]"}`}
          value={form.q} autoFocus aria-label={t("search.input")}
          onChange={(e) => setForm((f) => ({ ...f, q: e.target.value }))}
          placeholder={form.mode === "text" ? t("search.placeholderText") : t("search.placeholderImage")}
        />
      </div>
      <div className="flex gap-2">
        <button type="button" aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}
          className={`sr-btn sr-btn-ghost flex-1 sm:flex-none ${showFilters ? "bg-well text-ink" : ""}`}>
          <SlidersHorizontal size={15} />{t("search.filters")}
          {nFilters > 0 && <span className="sr-num rounded-full bg-ink px-1.5 text-[11px] leading-[18px] text-white">{nFilters}</span>}
        </button>
        <button className={`sr-btn sr-btn-primary flex-1 sm:flex-none ${big ? "h-12 px-6" : "px-5"}`} type="submit">{t("search.go")}<ArrowRight size={15} /></button>
      </div>
    </form>
  );
}

function Filters({ form, setForm }: { form: Form; setForm: (f: (x: Form) => Form) => void }) {
  const { t } = useT();
  const meta = useMeta();
  const labelName = useLabelName();
  const toggle = (k: "apps" | "labels", v: string) =>
    setForm((f) => ({ ...f, [k]: f[k].includes(v) ? f[k].filter((x) => x !== v) : [...f[k], v] }));
  return (
    <div className="sr-card mt-3 animate-rise space-y-5 p-5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1.5"><span className="text-xs font-medium text-graphite">{t("search.from")}</span>
          <input type="date" className="sr-field" value={form.from} onChange={(e) => setForm((f) => ({ ...f, from: e.target.value }))} /></label>
        <label className="flex flex-col gap-1.5"><span className="text-xs font-medium text-graphite">{t("search.to")}</span>
          <input type="date" className="sr-field" value={form.to} onChange={(e) => setForm((f) => ({ ...f, to: e.target.value }))} /></label>
        <label className="flex flex-col gap-1.5"><span className="text-xs font-medium text-graphite">{t("search.limit")}</span>
          <select className="sr-field" value={form.limit} onChange={(e) => setForm((f) => ({ ...f, limit: Number(e.target.value) }))}>
            {[10, 20, 50].map((n) => <option key={n}>{n}</option>)}
          </select></label>
        <label className="flex h-10 items-center gap-2 text-sm">
          <input type="checkbox" className="h-4 w-4 accent-[#0f1217]" checked={form.case_sensitive} onChange={(e) => setForm((f) => ({ ...f, case_sensitive: e.target.checked }))} />
          {t("search.case")}
        </label>
        <button type="button" className="sr-btn sr-btn-ghost ml-auto h-9 text-[13px]"
          onClick={() => setForm((f) => ({ ...f, from: "", to: "", apps: [], labels: [], case_sensitive: false, limit: 10 }))}>
          <X size={14} />{t("search.clear")}
        </button>
      </div>
      <div>
        <p className="mb-2 text-xs font-medium text-graphite">{t("search.apps")}</p>
        <div className="flex flex-wrap gap-1.5">{meta?.apps.map((a) => <Chip key={a} on={form.apps.includes(a)} onClick={() => toggle("apps", a)}>{a}</Chip>)}</div>
      </div>
      {/* 库里还没有分类预测时整组不显示 */}
      {!!meta?.labels.length && (
        <div>
          <p className="mb-2 text-xs font-medium text-graphite">{t("search.labels")} <span className="font-normal text-faint">· {t("search.labelsNote")}</span></p>
          <div className="flex flex-wrap gap-1.5">
            {meta.labels.map((l) => <Chip key={l} on={form.labels.includes(l)} onClick={() => toggle("labels", l)}>{labelName(l)}</Chip>)}
          </div>
        </div>
      )}
    </div>
  );
}

function Hero({ children }: { children: ReactNode }) {
  const { t } = useT();
  const meta = useMeta();
  const s = useStatus();
  const ix = (s?.index ?? {}) as Record<string, number>;
  return (
    <div className="mx-auto flex min-h-[calc(100vh-9rem)] max-w-3xl animate-rise flex-col justify-center pb-[8vh]">
      <p className="sr-label mb-4 text-center">{t("hero.eyebrow")}</p>
      <h1 className="text-center text-[34px] font-semibold leading-[1.15] tracking-[-0.02em] sm:text-[42px]">{t("hero.title")}</h1>
      <p className="mx-auto mt-4 max-w-xl text-center text-[15px] leading-relaxed text-graphite">{t("hero.sub")}</p>
      <div className="mt-9">{children}</div>
      <div className="mt-6 flex flex-wrap justify-center gap-2 text-[12.5px] text-graphite">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-rule bg-surface px-3 py-1.5"><Quote size={12} className="text-marker-ink" />{t("hint.quote")}</span>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-rule bg-surface px-3 py-1.5"><ScanText size={12} />{t("hint.text")}</span>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-rule bg-surface px-3 py-1.5"><ImageIcon size={12} />{t("hint.image")}</span>
      </div>
      {s && (
        <p className="sr-num mt-10 text-center font-mono text-[11.5px] text-faint">
          {t("hero.stats", { f: (ix.frames ?? 0).toLocaleString(), o: (ix.occurrences ?? 0).toLocaleString(), tz: meta?.tz_label ?? "" })}
        </p>
      )}
    </div>
  );
}

export default function SearchPage({ params }: { params: URLSearchParams }) {
  const { t } = useT();
  const key = params.toString();
  const cur: Form = {
    q: params.get("q") ?? "",
    mode: params.get("mode") === "image" ? "image" : "text",
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
    apps: list(params.get("apps")),
    labels: list(params.get("labels")),
    case_sensitive: params.get("case") === "1",
    limit: Number(params.get("limit") ?? 10) || 10,
    relaxed: params.get("relaxed") === "1",
  };
  const [form, setForm] = useState(cur);
  const [showFilters, setShowFilters] = useState(false);
  useEffect(() => setForm(cur), [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const go = (f: Form, relaxed = false) =>
    navigate("/", {
      q: f.q, mode: f.mode === "image" ? "image" : undefined, from: f.from, to: f.to, apps: f.apps.join(","),
      labels: f.labels.join(","), case: f.case_sensitive ? "1" : undefined, limit: f.limit !== 10 ? String(f.limit) : undefined,
      relaxed: relaxed ? "1" : undefined,
    });

  const res = useLoad<SearchResponse | null>(
    () => (cur.q.trim() ? post<SearchResponse>("/api/search", {
      q: cur.q, mode: cur.mode, date_from: cur.from || null, date_to: cur.to || null, apps: cur.apps, labels: cur.labels,
      case_sensitive: cur.case_sensitive, limit: cur.limit, relaxed: cur.relaxed,
    }) : Promise.resolve(null)),
    [key],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setShowFilters(false);
    go(form);
  };
  const nFilters = (form.from || form.to ? 1 : 0) + form.apps.length + form.labels.length + (form.case_sensitive ? 1 : 0);
  const data = res.data;
  const terms = data ? [...data.query.quoted, ...data.query.terms] : [];
  const open = (r: Result) =>
    navigate(`/frame/${r.frame_id}`, {
      occ: r.latest.occurrence_id, q: cur.q, mode: cur.mode === "image" ? "image" : undefined,
      case: cur.case_sensitive ? "1" : undefined, relaxed: data?.relaxed ? "1" : undefined,
      sem: r.semantic_lines.join(","), back: window.location.hash,
    });
  const bar = (big: boolean) => (
    <>
      <CommandBar form={form} setForm={setForm} onSubmit={submit} big={big} showFilters={showFilters} setShowFilters={setShowFilters} nFilters={nFilters} />
      {showFilters && <Filters form={form} setForm={setForm} />}
    </>
  );

  if (!cur.q.trim()) return <Hero>{bar(true)}</Hero>;

  return (
    <div>
      <div className="sticky top-3 z-20">{bar(false)}</div>
      <div className="mt-6">
        {res.loading ? (
          <div className="py-24 text-center"><Spinner label={t("search.searching")} /></div>
        ) : res.error ? (
          <Banner tone="alarm">{res.error}</Banner>
        ) : data && (
          <div className="space-y-4">
            {data.notes.map((n, i) => <Banner key={i} tone="warn">{n}</Banner>)}
            {data.relaxed && <button className="sr-btn sr-btn-line h-9" onClick={() => go(cur, false)}>{t("search.restoreStrict")}</button>}
            {data.message ? (
              <Banner tone="info">
                <p>{data.message}</p>
                {data.can_relax && (
                  <div className="mt-2.5 space-y-2.5">
                    <p className="text-xs text-graphite">{data.relax_rule}</p>
                    <button className="sr-btn sr-btn-line h-9" onClick={() => go(cur, true)}>{t("search.relax")}</button>
                  </div>
                )}
              </Banner>
            ) : (
              <>
                <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
                  <p className="text-sm font-medium">
                    {t("search.summary", {
                      mode: t(data.mode === "image" ? "search.image" : "search.text"), n: data.total,
                      basis: t(`basis.${data.rank_basis}` as Key), k: data.results.length,
                    })}
                  </p>
                  <div className="flex items-center gap-3 text-xs text-graphite">
                    <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2.5 w-3.5 rounded-[3px] bg-marker/70" />{t("legend.lex")}</span>
                    <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2.5 w-3.5 rounded-[3px] border border-cobalt bg-cobalt/20" />{t("legend.sem")}</span>
                  </div>
                </div>
                <p className="-mt-2 text-xs text-faint">{t("search.summaryNote")}</p>
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 min-[1800px]:grid-cols-4">
                  {data.results.map((r, i) => (
                    <ResultCard key={r.frame_id} r={r} rank={i + 1} mode={data.mode} terms={terms} caseSensitive={cur.case_sensitive} onOpen={() => open(r)} />
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
