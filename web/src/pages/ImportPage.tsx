// 旧版批量导入（POST /api/import，写入旧版记录库，用来做评测）。只从 设置 → 高级 进入，不在主导航里。
import { useState } from "react";
import { ChevronLeft, FolderUp } from "lucide-react";
import { postForm, type ImportResult } from "../api";
import { useT } from "../i18n";
import { errText, localDate } from "../lib";
import { Banner, PageHeader } from "../ui";

const pad2 = (n: number) => String(n).padStart(2, "0");
const localClock = (d: Date) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

export default function ImportPage() {
  const { t } = useT();
  const [files, setFiles] = useState<File[]>([]);
  const [label, setLabel] = useState("");
  const [known, setKnown] = useState(false);
  const [date, setDate] = useState(() => localDate(new Date()));
  const [time, setTime] = useState(() => localClock(new Date()));
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
      setErr(errText(e, t("common.offline")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-4xl">
      <a className="mb-2.5 inline-flex items-center gap-1 text-[13.5px] text-graphite hover:text-ink" href="#/settings">
        <ChevronLeft size={15} aria-hidden />{t("imp.back")}
      </a>
      <PageHeader title={t("imp.title")} sub={t("imp.sub")} />
      <section className="sr-card space-y-5 p-4 sm:p-6">
        <label
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-[12px] border-2 border-dashed px-4 py-10 text-center transition-colors ${drag ? "border-cobalt bg-cobalt-soft" : "border-rule-strong hover:border-graphite"}`}>
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-well text-graphite" aria-hidden><FolderUp size={20} /></span>
          <span className="text-sm font-medium">{t("imp.pick")}</span>
          <span className="text-xs text-faint">{files.length ? t("imp.picked", { n: files.length }) : t("imp.click")}</span>
          <input type="file" name="files" multiple accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" className="sr-only" onChange={(e) => pick(e.target.files)} />
        </label>
        <label className="flex max-w-sm flex-col gap-1.5">
          <span className="text-[12.5px] font-medium text-graphite">{t("imp.label")}</span>
          <input className="sr-field" name="label" value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm" title={t("imp.knownTip")}>
          <input type="checkbox" name="known" className="h-4 w-4 accent-[var(--cobalt)]" checked={known} onChange={(e) => setKnown(e.target.checked)} />{t("imp.known")}
        </label>
        {known && (
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-graphite">{t("imp.date")}</span>
              <input type="date" name="captured_date" className="sr-field" value={date} onChange={(e) => setDate(e.target.value)} /></label>
            <label className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-graphite">{t("imp.time")}</span>
              <input type="time" name="captured_time" className="sr-field" value={time} onChange={(e) => setTime(e.target.value)} /></label>
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
                  <th className="font-medium">{t("imp.status")}</th><th className="pr-4 font-medium">{t("imp.reason")}</th>
                </tr></thead>
                <tbody>
                  {res.batch.items.map((i) => (
                    <tr key={i.seq} className="border-b border-rule last:border-0">
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
