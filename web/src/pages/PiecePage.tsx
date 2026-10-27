// 片段详情：头部、概括（可重新生成）、按块排好的阅读视图、证据截图条和截图查看器。
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, Clock, FileQuestion, Image as ImageIcon, LoaderCircle, RefreshCw, Trash2 } from "lucide-react";
import { ApiError, api, frameThumbUrl, type BlockItem, type PieceDetail } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtClock, fmtDuration, fmtMonthDay, fmtWhen, hrefOf, localDate, navigate, useLoad } from "../lib";
import { Av, Card, ConfirmDialog, Empty, LoadError, PageHeader, Skeleton, useToast, type ConfirmSpec } from "../ui";
import { FrameViewer, type ViewerState } from "../viewer";

/** 点“重新生成”后每隔多久看一次新概括、最多等多久 */
export const REGEN_POLL_MS = 3000;
export const REGEN_MAX_MS = 90_000;

export default function PiecePage({ id }: { id: number }) {
  const { t } = useT();
  const toast = useToast();
  const piece = useLoad((signal) => api.piece(id, signal), [id]);
  const [viewer, setViewer] = useState<ViewerState>(null);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const regen = useRegen(id, piece.data, piece.reload);

  if (!piece.data) {
    if (piece.error instanceof ApiError && piece.error.status === 404)
      return <Empty icon={<FileQuestion size={18} />} title={t("piece.notFound")}
        action={<a className="sr-btn sr-btn-line sr-btn-sm" href="#/">{t("piece.backToday")}</a>}>{t("piece.notFoundHint")}</Empty>;
    if (piece.error) return <LoadError text={errText(piece.error, t("common.offline"))} onRetry={() => piece.reload()} />;
    return <Skeleton rows={5} />;
  }

  const { piece: p, summary, blocks, frames, unjoined } = piece.data;
  const frameIds = frames.map((f) => f.frame_id);
  const figOf = (fid: string | null) => (fid ? frameIds.indexOf(fid) : -1);
  const open = (fid: string | null, line: number | null = null) => {
    const i = figOf(fid);
    if (fid && i < 0) setViewer({ frames: [fid], index: 0, line }); // 不在本片段证据条里的图也能看
    else if (i >= 0) setViewer({ frames: frameIds, index: i, line });
  };
  const pending = blocks.reduce((n, b) => n + b.items.filter((x) => x.kind === "pending").length, 0);
  const a = fmtClock(p.start_ms);
  const b = fmtClock(p.end_ms);
  const dayStr = localDate(new Date(p.start_ms));
  const isToday = dayStr === localDate(new Date());
  const lineCount = blocks.reduce((n, bl) => n + bl.items.filter((x) => x.kind === "line").length, 0);

  const askDelete = () => setConfirm({
    title: t("piece.delete"),
    body: t("piece.deleteBody", { app: appName(p.app), a: fmtWhen(p.start_ms), b: fmtClock(p.end_ms) }),
    run: async () => {
      try {
        // 删除范围是 [from, to)：+1 才包含正好在片段结束那一刻的截图和文字
        await api.remove({ from_ms: p.start_ms, to_ms: p.end_ms + 1, app: p.app });
        toast(t("piece.deleted"));
        navigate("/", { date: isToday ? undefined : dayStr });
      } catch (e) {
        toast(t("priv.fail", { e: errText(e, t("common.offline")) }), "alarm");
      }
    },
  });

  return (
    <div>
      <a className="mb-2.5 inline-flex items-center gap-1 text-[13.5px] text-graphite hover:text-ink" href={hrefOf("/", { date: isToday ? undefined : dayStr })}>
        <ChevronLeft size={15} aria-hidden />{isToday ? t("piece.backToday") : t("piece.backDay", { x: fmtMonthDay(new Date(p.start_ms)) })}
      </a>
      <PageHeader title={p.title || appName(p.app)} before={<Av app={p.app} />}
        sub={<>
          <p className="sr-num">{t("piece.meta", { app: appName(p.app), a, b, d: fmtDuration(p.end_ms - p.start_ms) })}</p>
          {(p.task || pending > 0) && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {p.task && <span className="sr-tag sr-tag-task">{p.task}</span>}
              {pending > 0 && <span className="sr-tag sr-tag-warn">{t("piece.pendingN", { n: pending })}</span>}
            </div>
          )}
        </>}>
        <div className="flex flex-wrap gap-2">
          {frames.length > 0 && (
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => setViewer({ frames: frameIds, index: 0, line: null })}>
              <ImageIcon size={15} aria-hidden />{t("piece.viewShots")}
            </button>
          )}
          <button className="sr-btn sr-btn-danger sr-btn-sm" onClick={askDelete}><Trash2 size={15} aria-hidden />{t("piece.delete")}</button>
        </div>
      </PageHeader>

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          <Card title={t("piece.summary")} id="sum-h"
            aside={<button className="sr-btn sr-btn-ghost sr-btn-sm" onClick={regen.start} disabled={regen.busy}>
              {regen.busy ? <LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" aria-hidden /> : <RefreshCw size={15} aria-hidden />}
              {regen.busy ? t("piece.regening") : t("piece.regen")}
            </button>}>
            {summary?.text ? <p className="text-[15px] leading-[1.7] [overflow-wrap:anywhere]">{summary.text}</p>
              : <p className="text-sm text-graphite">{t("piece.noSummary")}</p>}
            {summary && (summary.model || summary.status === "stale") && (
              <p className="mt-2 flex flex-wrap items-center gap-2 text-[12.5px] text-faint">
                {summary.model && <span>{t("piece.model", { x: summary.model })}</span>}
                {summary.status === "stale" && <span className="sr-tag sr-tag-warn">{t("piece.stale")}</span>}
              </p>
            )}
          </Card>

          <Card title={t("piece.reading")} id="read-h" aside={<span className="text-[12.5px] text-faint">{t("piece.readingHint")}</span>}>
            {!lineCount && !blocks.some((bl) => bl.items.length) ? <p className="text-sm text-faint">{t("piece.noText")}</p> : (
              <div className="text-[14.5px]">
                {blocks.map((bl, bi) => (
                  <div key={bl.block_id}>
                    {unjoined && bi > 0 && (
                      <div className="sr-hatch my-[18px] rounded-[9px] border border-dashed border-rule-strong px-3 py-2.5 text-[13px] text-graphite">{t("piece.unjoined")}</div>
                    )}
                    <div className={`mb-2 flex flex-wrap items-center gap-2 text-[12.5px] text-graphite ${bi > 0 && !unjoined ? "mt-[18px]" : ""}`}>
                      <b className="text-[13px] text-ink">{t("piece.block", { n: bi + 1 })}</b>
                      <span>{t("piece.blockLines", { n: bl.items.filter((x) => x.kind === "line").length })}</span>
                    </div>
                    {bl.items.map((it, i) => <BlockRow key={i} it={it} fig={figOf} open={open} />)}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>

        <aside className="lg:sticky lg:top-[76px]">
          <Card title={t("piece.shots")} id="shots-h" aside={<span className="sr-num text-[12.5px] text-faint">{t("piece.shotsN", { n: frames.length })}</span>}>
            {frames.length ? (
              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-2">
                {frames.map((f, i) => (
                  <button key={f.frame_id} onClick={() => setViewer({ frames: frameIds, index: i, line: null })}
                    aria-label={t("piece.openShot", { n: i + 1, t: fmtClock(f.captured_ms, true) })}
                    className="block w-full overflow-hidden rounded-[10px] border border-rule bg-surface text-left transition-[border-color,transform] hover:-translate-y-px hover:border-graphite">
                    <span className="relative block aspect-[16/10] bg-night-3">
                      <img src={frameThumbUrl(f.frame_id)} alt="" loading="lazy" decoding="async" className="absolute inset-0 h-full w-full object-cover object-top" />
                      <span className="absolute left-1.5 top-1.5 rounded-[5px] bg-night/80 px-1.5 py-[3px] font-mono text-[10.5px] leading-none text-white">{t("piece.fig", { n: i + 1 })}</span>
                    </span>
                    <span className="sr-num flex justify-between gap-1.5 px-2 py-[5px] text-[11.5px] text-graphite">{fmtClock(f.captured_ms, true)}</span>
                  </button>
                ))}
              </div>
            ) : <p className="text-sm text-faint">{t("piece.noShots")}</p>}
            <p className="mt-3 text-[12.5px] text-faint">{t("piece.shotsNote")}</p>
          </Card>
        </aside>
      </div>

      <FrameViewer state={viewer} onClose={() => setViewer(null)} pieceId={p.piece_id} />
      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}

function BlockRow({ it, fig, open }: { it: BlockItem; fig: (fid: string | null) => number; open: (fid: string | null, line?: number | null) => void }) {
  const { t } = useT();
  const figBtn = (fid: string | null, line: number | null) => {
    if (!fid) return null;
    const i = fig(fid);
    const n = i >= 0 ? i + 1 : "?";
    return (
      <button type="button" onClick={() => open(fid, line)} aria-label={t("piece.figLabel", { n })}
        className="h-[22px] shrink-0 rounded-[6px] border border-rule bg-surface px-[7px] text-[11.5px] text-graphite hover:border-graphite hover:text-ink">
        {t("piece.fig", { n })}
      </button>
    );
  };
  if (it.kind === "skim") {
    const s = Math.max(1, Math.round((it.end_ms - it.start_ms) / 1000));
    return (
      <div className="my-2.5 flex items-center gap-2.5 text-[12.5px] text-faint before:flex-1 before:border-t before:border-dashed before:border-rule-strong after:flex-1 after:border-t after:border-dashed after:border-rule-strong">
        {t("piece.skim")}{t("piece.skimFor", { s })}
      </div>
    );
  }
  if (it.kind === "pending")
    return (
      <div className="my-2 flex flex-wrap items-center gap-2.5 rounded-[9px] border border-dashed border-marker/55 bg-warn-bg px-3 py-2.5 text-[13.5px] text-marker-ink">
        <Clock size={15} aria-hidden /><span className="flex-1">{t("piece.pending")}</span>{figBtn(it.frame_id, null)}
      </div>
    );
  return (
    <p className="-mx-1.5 flex items-start gap-2.5 rounded-[6px] px-1.5 py-[3px] hover:bg-well">
      <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
        {it.text}
        {it.source === "ocr" && <> <span className="sr-tag" title={t("piece.ocrTip")}>{t("piece.ocrTag")}</span></>}
      </span>
      {figBtn(it.frame_id, it.line_id)}
    </p>
  );
}

/** 重新生成：POST 后定时重新拉片段，概括变了（或超时）就停 */
function useRegen(id: number, data: PieceDetail | null, reload: (silent?: boolean) => void) {
  const { t } = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const base = useRef<string | null>(null);
  const since = useRef(0);
  const sig = (d: PieceDetail | null) => (d?.summary ? `${d.summary.status}|${d.summary.text}` : "");

  // 概括变了就停
  useEffect(() => {
    if (busy && sig(data) !== base.current) {
      setBusy(false);
      toast(t("piece.regenDone"));
    }
  }, [busy, data, t, toast]);
  // 定时重新拉：用固定间隔，不靠 data 变化来排下一次（某次拉取失败时 data 不变，按 data 排就会停在“生成中”）
  useEffect(() => {
    if (!busy) return;
    const iv = window.setInterval(() => {
      if (Date.now() - since.current > REGEN_MAX_MS) {
        setBusy(false);
        toast(t("piece.regenQueued"), "info");
      } else reload(true);
    }, REGEN_POLL_MS);
    return () => window.clearInterval(iv);
  }, [busy, reload, t, toast]);

  const start = async () => {
    base.current = sig(data);
    since.current = Date.now();
    setBusy(true); // 请求发出时就禁用按钮，连点不会重复排队
    try {
      await api.summarize(id);
    } catch (e) {
      setBusy(false);
      toast(errText(e, t("common.offline")), "alarm");
    }
  };
  return { busy, start };
}
