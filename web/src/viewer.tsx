// 截图查看器：大图 + 行框（选中的那一行高亮）+ 这张图里的文字，可前后翻。
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Copy } from "lucide-react";
import { api, frameImageUrl } from "./api";
import { useT } from "./i18n";
import { appName, errText, fmtWhen, hrefOf, useLoad } from "./lib";
import { Banner, Dialog, Spinner, copyText, useToast } from "./ui";

export type ViewerState = { frames: string[]; index: number; line: number | null } | null;

export function FrameViewer({ state, onClose, pieceId }: { state: ViewerState; onClose: () => void; pieceId?: number }) {
  const { t } = useT();
  const toast = useToast();
  const [index, setIndex] = useState(state?.index ?? 0);
  const [line, setLine] = useState<number | null>(state?.line ?? null);
  const [imgFailed, setImgFailed] = useState(false);
  // 每次打开（state 换了）都从调用方给的那张图、那一行开始
  const [prev, setPrev] = useState(state);
  if (state !== prev) {
    setPrev(state);
    if (state) {
      setIndex(state.index);
      setLine(state.line);
    }
  }
  const frames = state?.frames ?? [];
  const id = frames[index] ?? null;
  const frame = useLoad((signal) => (id ? api.frame(id, signal) : Promise.resolve(null)), [id]);
  useEffect(() => setImgFailed(false), [id]);
  const f = frame.data && frame.data.frame_id === id ? frame.data : null;
  const go = (d: number) => {
    setIndex((i) => (i + d + frames.length) % frames.length);
    setLine(null);
  };
  const title = frames.length > 1 ? t("viewer.titleN", { i: index + 1, n: frames.length }) : t("viewer.title");

  return (
    <Dialog open={!!state} title={title} onClose={onClose} wide
      footer={<>
        {frames.length > 1 && <>
          <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => go(-1)}><ChevronLeft size={15} aria-hidden />{t("viewer.prev")}</button>
          <button className="sr-btn sr-btn-line sr-btn-sm" onClick={() => go(1)}>{t("viewer.next")}<ChevronRight size={15} aria-hidden /></button>
        </>}
        <button className="sr-btn sr-btn-line sr-btn-sm" disabled={!f?.lines.length}
          onClick={async () => {
            const ok = await copyText((f?.lines ?? []).map((l) => l.text).join("\n"));
            toast(ok ? t("viewer.copied") : t("common.copyFail"), ok ? "ok" : "warn");
          }}>
          <Copy size={15} aria-hidden />{t("viewer.copy")}
        </button>
        {f?.piece_id != null && f.piece_id !== pieceId && (
          <a className="sr-btn sr-btn-primary sr-btn-sm" href={hrefOf(`/piece/${f.piece_id}`)} onClick={onClose}>{t("viewer.openPiece")}</a>
        )}
      </>}>
      {frame.loading && !f ? (
        <div className="py-10 text-center"><Spinner /></div>
      ) : frame.error && !f ? (
        <Banner tone="alarm">{errText(frame.error, t("common.offline"))}</Banner>
      ) : f ? (
        <div>
          <div className="relative mx-auto overflow-hidden rounded-[10px] bg-night-3"
            style={{ aspectRatio: `${f.width} / ${f.height}`, width: `min(100%, calc(60vh * ${f.width / Math.max(1, f.height)}))` }}>
            {imgFailed ? (
              <div className="absolute inset-0 flex items-center justify-center p-4 text-center text-xs text-night-faint">{t("viewer.imgGone")}</div>
            ) : (
              <img src={frameImageUrl(f.frame_id)} alt={t("viewer.imgAlt", { app: appName(f.app), t: fmtWhen(f.captured_ms) })}
                onError={() => setImgFailed(true)} className="absolute inset-0 h-full w-full object-fill" />
            )}
            <svg viewBox={`0 0 ${f.width} ${f.height}`} preserveAspectRatio="none" className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden>
              {f.lines.map((l) => {
                const on = l.line_id === line;
                return (
                  <rect key={l.line_id} data-line={l.line_id} data-on={on || undefined}
                    x={l.x0} y={l.y0} width={Math.max(1, l.x1 - l.x0)} height={Math.max(1, l.y1 - l.y0)} rx={3}
                    fill={on ? "#f2b400" : "transparent"} fillOpacity={on ? 0.42 : 0}
                    stroke={on ? "#8a5a00" : "#7394f6"} strokeOpacity={on ? 1 : 0.35} strokeWidth={on ? 2.5 : 1} vectorEffect="non-scaling-stroke" />
                );
              })}
            </svg>
          </div>
          <p className="sr-num mt-2 text-[12.5px] text-graphite">{appName(f.app)} · {f.title} · {fmtWhen(f.captured_ms)}</p>
          {f.lines.length ? (
            <ol className="mt-3 text-[13.5px]" aria-label={t("viewer.lines")}>
              {f.lines.map((l) => (
                <li key={l.line_id}>
                  <button type="button" aria-pressed={l.line_id === line} onClick={() => setLine(l.line_id === line ? null : l.line_id)}
                    className={`w-full rounded-[6px] px-2 py-1 text-left [overflow-wrap:anywhere] ${l.line_id === line ? "bg-marker-soft" : "hover:bg-well"}`}>
                    {l.text}
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-3 text-sm text-faint">{t("viewer.noLines")}</p>
          )}
        </div>
      ) : null}
    </Dialog>
  );
}
