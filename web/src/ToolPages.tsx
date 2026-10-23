// 收藏、按时间范围删除、批量导入（对应 app.py 的三个折叠面板）。
import { useState } from "react";
import { FolderUp, Star } from "lucide-react";
import { get, post, postForm, type Favorite, type ImportResult, type RangePlan } from "./api";
import { useT } from "./i18n";
import { fmtTime, navigate, useLoad } from "./lib";
import { Banner, Chip, Empty, Field, FramedShot, PageHeader, Spinner, Unavailable, useMeta, useToast } from "./ui";

export function FavoritesPage() {
  const { t } = useT();
  const favs = useLoad(() => get<{ favorites: Favorite[] }>("/api/favorites"), []);
  const rows = favs.data?.favorites ?? [];
  return (
    <div className="animate-rise">
      <PageHeader title={t("fav.title")} sub={t("fav.sub")} />
      {favs.loading && !favs.data ? <Spinner /> : favs.error ? <Banner tone="alarm">{favs.error}</Banner> : !rows.length ? (
        <Empty title={t("fav.empty")} icon={<Star size={18} />}>{t("fav.emptyHint")}</Empty>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {rows.map((f) => (
            <li key={f.occurrence_id}>
              <button
                className="group sr-card block w-full overflow-hidden text-left transition-[box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-lift"
                onClick={() => navigate(`/frame/${f.frame_id}`, { occ: f.occurrence_id, back: window.location.hash })}
              >
                {f.image_ok && f.width && f.height ? <FramedShot fid={f.frame_id} width={f.width} height={f.height} thumb={520} />
                  : <Unavailable text={t("fav.noImage")} />}
                <div className="p-4">
                  <p className="sr-num flex items-center gap-1.5 text-[13px] font-semibold"><Star size={13} className="fill-marker text-marker-ink" />{fmtTime(f.first_seen_ms)}</p>
                  <p className="mt-0.5 truncate text-[13px] text-graphite">{f.app} · {f.window_title}</p>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function CleanupPage() {
  const { t } = useT();
  const meta = useMeta();
  const toast = useToast();
  const [f, setF] = useState({ date_from: "", date_to: "", time_from: "00:00", time_to: "23:59", apps: [] as string[], include_favorites: false });
  const [plan, setPlan] = useState<{ key: string; plan: RangePlan } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const key = JSON.stringify(f);
  const shown = plan?.key === key ? plan.plan : null; // 条件改了要重新预览
  const set = (patch: Partial<typeof f>) => setF((x) => ({ ...x, ...patch }));

  const preview = async () => {
    setBusy(true);
    setErr(null);
    try {
      setPlan({ key, plan: (await post<{ plan: RangePlan }>("/api/range/preview", f)).plan });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const run = async () => {
    if (!shown) return;
    setBusy(true);
    try {
      toast((await post<{ message: string }>("/api/range/delete", { ...f, confirmed_favorites: shown.favorites })).message);
      setPlan(null);
    } catch (e) {
      setErr((e as Error).message);
      setPlan(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-4xl animate-rise">
      <PageHeader title={t("clean.title")} sub={t("clean.sub")} />
      <section className="sr-card space-y-6 p-6">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label={t("clean.d0")}><input type="date" className="sr-field" value={f.date_from} onChange={(e) => set({ date_from: e.target.value })} /></Field>
          <Field label={t("clean.t0")}><input type="time" step={300} className="sr-field" value={f.time_from} onChange={(e) => set({ time_from: e.target.value })} /></Field>
          <Field label={t("clean.d1")}><input type="date" className="sr-field" value={f.date_to} onChange={(e) => set({ date_to: e.target.value })} /></Field>
          <Field label={t("clean.t1")}><input type="time" step={300} className="sr-field" value={f.time_to} onChange={(e) => set({ time_to: e.target.value })} /></Field>
        </div>
        <div>
          <p className="mb-2 text-xs font-medium text-graphite">{t("clean.apps")}</p>
          <div className="flex flex-wrap gap-1.5">
            {meta?.apps.map((a) => (
              <Chip key={a} on={f.apps.includes(a)} onClick={() => set({ apps: f.apps.includes(a) ? f.apps.filter((x) => x !== a) : [...f.apps, a] })}>{a}</Chip>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-rule pt-5">
          <label className="flex items-center gap-2 text-sm" title={t("clean.favTip")}>
            <input type="checkbox" className="h-4 w-4 accent-[#c2372b]" checked={f.include_favorites} onChange={(e) => set({ include_favorites: e.target.checked })} />
            {t("clean.fav")}
          </label>
          <button className="sr-btn sr-btn-line" disabled={!f.date_from || busy} onClick={preview}>{t("clean.preview")}</button>
        </div>
      </section>
      <div className="mt-4 space-y-3">
        {err && <Banner tone="alarm">{err}</Banner>}
        {shown && (shown.occurrences === 0 ? (
          <Banner>{shown.favorites.length ? t("clean.noneFav") : t("clean.none")}</Banner>
        ) : (
          <section className="animate-rise rounded-card border border-alarm/30 bg-[#fdf1f0] p-5">
            <p className="text-sm leading-relaxed">
              {t("clean.plan", { a: fmtTime(shown.start_ms), b: fmtTime(shown.end_ms), n: shown.occurrences, p: shown.frames_purged, k: shown.frames_kept })}
              {shown.favorites.length > 0 && " " + t("clean.planFav", { n: shown.favorites.length, x: f.include_favorites ? t("clean.favDel") : t("clean.favKeep") })}
            </p>
            <p className="mt-2 text-xs text-graphite">{t("clean.planNote")}</p>
            <button className="sr-btn sr-btn-danger-solid mt-4" disabled={busy} onClick={run}>{t("clean.go")}</button>
          </section>
        ))}
      </div>
    </div>
  );
}

export function ImportPage() {
  const { t } = useT();
  const [files, setFiles] = useState<File[]>([]);
  const [label, setLabel] = useState("");
  const [known, setKnown] = useState(false);
  const [date, setDate] = useState("");
  const [time, setTime] = useState("00:00");
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<ImportResult | null>(null);
  const status = { ok: t("imp.ok"), failed: t("imp.failed"), skipped: t("imp.skipped") };
  const pick = (fs: FileList | null) => {
    setFiles(Array.from(fs ?? []).filter((x) => /\.(png|jpe?g|webp)$/i.test(x.name)));
    setRes(null);
  };

  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const fd = new FormData();
      files.forEach((x) => fd.append("files", x, x.name));
      fd.append("label", label);
      if (known && date) {
        fd.append("captured_date", date);
        fd.append("captured_time", time);
      }
      setRes(await postForm<ImportResult>("/api/import", fd));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-4xl animate-rise">
      <PageHeader title={t("imp.title")} sub={t("imp.sub")} />
      <section className="sr-card space-y-5 p-6">
        <label
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-[12px] border-2 border-dashed px-4 py-12 text-center transition-colors ${drag ? "border-cobalt bg-cobalt/[0.04]" : "border-rule-strong hover:border-graphite"}`}>
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-well text-graphite"><FolderUp size={20} /></span>
          <span className="text-sm font-medium">{t("imp.pick")}</span>
          <span className="text-xs text-faint">{files.length ? t("imp.picked", { n: files.length }) : t("imp.click")}</span>
          <input type="file" multiple accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" className="sr-only" onChange={(e) => pick(e.target.files)} />
        </label>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("imp.label")}><input className="sr-field" value={label} onChange={(e) => setLabel(e.target.value)} /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm" title={t("imp.knownTip")}>
          <input type="checkbox" className="h-4 w-4 accent-[#0f1217]" checked={known} onChange={(e) => setKnown(e.target.checked)} />{t("imp.known")}
        </label>
        {known && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("imp.date")}><input type="date" className="sr-field" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
            <Field label={t("imp.time")}><input type="time" className="sr-field" value={time} onChange={(e) => setTime(e.target.value)} /></Field>
          </div>
        )}
        <div className="flex justify-end border-t border-rule pt-5">
          <button className="sr-btn sr-btn-primary" disabled={!files.length || busy || (known && !date)} onClick={go}>{busy ? t("imp.going") : t("imp.go")}</button>
        </div>
      </section>
      <div className="mt-4 space-y-3">
        {err && <Banner tone="alarm">{err}</Banner>}
        {res && (
          <>
            <Banner tone={res.failed ? "warn" : "ok"}>
              {t("imp.result", { a: res.ok, b: res.failed, c: res.skipped })}{res.batch.resubmitted && t("imp.resubmitted")}
            </Banner>
            <div className="sr-card overflow-x-auto">
              <table className="w-full text-left text-[13px]">
                <thead className="text-xs text-faint"><tr className="border-b border-rule">
                  <th className="px-4 py-2.5 font-medium">{t("imp.seq")}</th><th className="font-medium">{t("imp.file")}</th>
                  <th className="font-medium">{t("imp.status")}</th><th className="font-medium">{t("imp.reason")}</th>
                </tr></thead>
                <tbody>
                  {res.batch.items.map((i) => (
                    <tr key={i.seq} className="border-b border-rule/70 last:border-0">
                      <td className="sr-num px-4 py-2">{i.seq + 1}</td><td className="break-all pr-4">{i.name}</td>
                      <td className={`pr-4 ${i.status === "failed" ? "text-alarm" : i.status === "ok" ? "text-signal" : "text-graphite"}`}>{status[i.status]}</td>
                      <td className="pr-4 text-graphite">{i.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-faint">{res.batch.time_known ? t("imp.noteKnown") : t("imp.noteUnknown")}</p>
          </>
        )}
      </div>
    </div>
  );
}
