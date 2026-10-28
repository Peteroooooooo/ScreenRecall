// 条目页：出处（网址可点开，新窗口，只放 http/https；本地文件给“在文件夹中显示”）、这一条的字（新存的标出来）、截图、复制过的。
import { useState } from "react";
import { ArrowLeft, ExternalLink, FolderOpen, Image as ImageIcon } from "lucide-react";
import { api, type EntryDetail } from "../api";
import { useT } from "../i18n";
import { errText, fmtClock, fmtDuration, fmtInt, fmtWhen, hrefOf, localDate, safeHttpUrl, useLoad } from "../lib";
import { Av, Card, LoadError, PageHeader, Skeleton, useToast } from "../ui";
import { ShotImage, ShotViewer, type ViewerState } from "../viewer";
import { kindLabel, reasonsText } from "./TimelinePage";

export default function EntryPage({ id }: { id: number }) {
  const { t } = useT();
  const res = useLoad((signal) => api.entry(id, signal), [id]);
  const [viewer, setViewer] = useState<ViewerState>(null);
  const d = res.data;
  return (
    <div>
      {!d ? (
        res.error ? <LoadError text={t("entry.error", { e: errText(res.error, t("common.offline")) })} onRetry={() => res.reload()} /> : <Skeleton rows={4} />
      ) : (
        <>
          <EntryHead d={d} />
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
            <Card title={t("entry.lines", { n: fmtInt(d.lines.length) })} id="lines-h"
              aside={d.entry.new_lines > 0 && <span className="sr-tag sr-tag-ok">{t("tl.lines", { n: fmtInt(d.entry.new_lines) })}</span>}>
              {d.entry.source.kind === "file" ? (
                <p className="text-sm text-graphite">{t("entry.fileNoText")}</p>
              ) : d.lines.length ? (
                <ol className="text-[13.5px]" aria-labelledby="lines-h">
                  {d.lines.map((l) => (
                    <li key={l.line_id} className={`flex gap-2 rounded-[6px] px-2 py-1 [overflow-wrap:anywhere] ${l.new ? "bg-ok-bg" : "text-graphite"}`}>
                      <span className="min-w-0 flex-1">{l.text}</span>
                      {l.new && <span className="sr-tag sr-tag-ok shrink-0">{t("entry.new")}</span>}
                      {l.via === "ocr" && <span className="sr-tag shrink-0" title={t("entry.ocrTip")}>OCR</span>}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-faint">{t("entry.noLines")}</p>
              )}
            </Card>
            <div className="space-y-4">
              <Card title={t("entry.shots")} id="shots-h">
                {d.shots.length ? (
                  <ul className="grid gap-2.5">
                    {d.shots.map((s) => (
                      <li key={s.shot_id}>
                        <button type="button" className="block w-full overflow-hidden rounded-[10px] border border-rule text-left hover:border-graphite"
                          onClick={() => setViewer({ shotId: s.shot_id, hits: [] })} aria-label={t("entry.openShot", { t: fmtClock(s.t_ms) })}>
                          <ShotImage shot={{ ...s, boxes: [] }} thumb alt="" />
                        </button>
                        <p className="sr-num mt-1 text-[12px] text-graphite">{fmtClock(s.t_ms)} · {t(`shot.${s.kind}`)}</p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-faint">{d.entry.thumb ? t("tl.hasShot") : t("entry.noShots")}</p>
                )}
                {!d.shots.length && d.entry.thumb && (
                  <button className="sr-btn sr-btn-line sr-btn-sm mt-2" onClick={() => setViewer({ shotId: d.entry.thumb!.shot_id, hits: [] })}>
                    <ImageIcon size={15} aria-hidden />{t("entry.viewOld")}
                  </button>
                )}
              </Card>
              <Card title={t("entry.clips")} id="clips-h">
                {d.clips.length ? (
                  <ul className="space-y-2 text-[13.5px]">
                    {d.clips.map((c) => (
                      <li key={c.clip_id} className="rounded-[9px] bg-canvas px-3 py-2">
                        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{c.text}</p>
                        <p className="sr-num mt-1 text-[12px] text-faint">{fmtWhen(c.t_ms)}</p>
                      </li>
                    ))}
                  </ul>
                ) : <p className="text-sm text-faint">{t("entry.noClips")}</p>}
              </Card>
            </div>
          </div>
        </>
      )}
      <ShotViewer state={viewer} onClose={() => setViewer(null)} />
    </div>
  );
}

function EntryHead({ d }: { d: EntryDetail }) {
  const { t } = useT();
  const toast = useToast();
  const e = d.entry;
  const src = e.source;
  const href = src.kind === "url" ? safeHttpUrl(src.url) : null;
  const why = reasonsText(e.reasons, t);
  const reveal = async () => {
    try {
      await api.reveal(src.source_id);
    } catch (err) {
      toast(errText(err, t("common.offline")), "alarm");
    }
  };
  return (
    <>
      <a className="mb-2 inline-flex items-center gap-1 text-[13px] text-graphite hover:text-ink" href={hrefOf("/", { day: localDate(new Date(e.start_ms)) === localDate(new Date()) ? undefined : localDate(new Date(e.start_ms)) })}>
        <ArrowLeft size={14} aria-hidden />{t("entry.back")}
      </a>
      <PageHeader title={e.name} before={<Av app={src.app} />}
        sub={<span className="sr-num">
          {kindLabel(e, t)} · {fmtWhen(e.start_ms)}–{e.open ? t("tl.stillLooking") : fmtClock(e.end_ms)}
          {(!e.open || e.background) && ` · ${t("entry.dwell", { x: fmtDuration(e.dwell_ms) })}`}
          {e.background && ` · ${t("tl.background")}`}
          {e.merged > 0 && ` · ${t("tl.merged", { n: e.merged })}`}
          {why && ` · ${why}`}
        </span>} />
      <section className="sr-card mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 p-4 text-[13.5px]" aria-label={t("entry.source")}>
        <b className="shrink-0">{t("entry.source")}</b>
        {src.kind === "url" ? (
          href ? (
            <a className="inline-flex min-w-0 items-center gap-1 text-cobalt hover:underline [overflow-wrap:anywhere]" href={href} target="_blank" rel="noopener noreferrer">
              {src.url}<ExternalLink size={13} className="shrink-0" aria-hidden />
            </a>
          ) : <span className="min-w-0 [overflow-wrap:anywhere]">{src.url}</span>
        ) : src.kind === "file" ? (
          <>
            <span className="min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]">{src.path}</span>
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={reveal}><FolderOpen size={15} aria-hidden />{t("entry.reveal")}</button>
          </>
        ) : (
          <span className="min-w-0 text-graphite [overflow-wrap:anywhere]">{t("entry.weak", { app: src.app, title: src.title || "—" })}</span>
        )}
      </section>
    </>
  );
}
