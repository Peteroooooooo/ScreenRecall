// 截图：图 + 黄框（命中的行）。框的坐标是存下的 JPEG 坐标，这里换成占图片宽高的百分比，图片显示多大框就跟着缩放。
import { useEffect, useState } from "react";
import { Copy } from "lucide-react";
import { api, shotImageUrl, shotThumbUrl, type Shot, type ShotBox } from "./api";
import { useT } from "./i18n";
import { errText, fmtWhen, hrefOf, sourceName, useLoad } from "./lib";
import { Banner, Dialog, Spinner, copyText, useToast } from "./ui";

const pct = (v: number, total: number) => `${(100 * v) / Math.max(1, total)}%`;

/** 一个框在图片上的位置（百分比）：显示尺寸变了也对得上 */
export function boxStyle(b: ShotBox, width: number, height: number) {
  return { left: pct(b.x, width), top: pct(b.y, height), width: pct(Math.max(1, b.w), width), height: pct(Math.max(1, b.h), height) };
}

/** 图 + 黄框。marked：要标黄的行（null = 全部框都标）；thumb：用缩略图 */
export function ShotImage({ shot, marked = null, thumb = false, alt, className = "" }: {
  shot: Pick<Shot, "shot_id" | "width" | "height" | "boxes">; marked?: Set<number> | null; thumb?: boolean; alt: string; className?: string;
}) {
  const { t } = useT();
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [shot.shot_id]);
  const boxes = shot.boxes.filter((b) => marked == null || marked.has(b.line_id));
  return (
    <span className={`relative block overflow-hidden bg-night-3 ${className}`} style={{ aspectRatio: `${shot.width} / ${Math.max(1, shot.height)}` }}>
      {failed ? (
        <span className="absolute inset-0 flex items-center justify-center p-3 text-center text-xs text-night-faint">{t("viewer.imgGone")}</span>
      ) : (
        <img src={thumb ? shotThumbUrl(shot.shot_id, 480) : shotImageUrl(shot.shot_id)} alt={alt} loading={thumb ? "lazy" : undefined}
          decoding="async" onError={() => setFailed(true)} className="absolute inset-0 h-full w-full object-fill" />
      )}
      {!failed && boxes.map((b) => (
        <span key={b.line_id} data-box={b.line_id} aria-hidden style={boxStyle(b, shot.width, shot.height)}
          className="pointer-events-none absolute rounded-[3px] border-2 border-marker bg-marker/25" />
      ))}
    </span>
  );
}

export type ViewerState = { shotId: number; hits: number[] } | null;

/** 看大图：命中的行黄框标出，下面列出这张图里的字（点一行也会标黄） */
export function ShotViewer({ state, onClose }: { state: ViewerState; onClose: () => void }) {
  const { t } = useT();
  const toast = useToast();
  const id = state?.shotId ?? null;
  const shot = useLoad((signal) => (id ? api.shot(id, signal) : Promise.resolve(null)), [id]);
  const [picked, setPicked] = useState<number | null>(null);
  useEffect(() => setPicked(null), [id]);
  const s = shot.data && shot.data.shot_id === id ? shot.data : null;
  const marked = new Set([...(state?.hits ?? []), ...(picked != null ? [picked] : [])]);
  const lines = s ? s.boxes.filter((b) => b.text) : [];
  return (
    <Dialog open={!!state} title={t("viewer.title")} onClose={onClose} wide
      footer={<>
        <button className="sr-btn sr-btn-line sr-btn-sm" disabled={!lines.length}
          onClick={async () => {
            const ok = await copyText(lines.map((b) => b.text).join("\n"));
            toast(ok ? t("viewer.copied") : t("common.copyFail"), ok ? "ok" : "warn");
          }}>
          <Copy size={15} aria-hidden />{t("viewer.copy")}
        </button>
        {s?.visit_id != null && <a className="sr-btn sr-btn-primary sr-btn-sm" href={hrefOf(`/entry/${s.visit_id}`)} onClick={onClose}>{t("viewer.openEntry")}</a>}
      </>}>
      {shot.loading && !s ? (
        <div className="py-10 text-center"><Spinner /></div>
      ) : shot.error && !s ? (
        <Banner tone="alarm">{errText(shot.error, t("common.offline"))}</Banner>
      ) : s ? (
        <div>
          <div className="mx-auto rounded-[10px]" style={{ width: `min(100%, calc(60vh * ${s.width / Math.max(1, s.height)}))` }}>
            <ShotImage shot={s} marked={marked} className="rounded-[10px]" alt={t("viewer.imgAlt", { x: sourceName(s.source), t: fmtWhen(s.t_ms) })} />
          </div>
          <p className="sr-num mt-2 text-[12.5px] text-graphite [overflow-wrap:anywhere]">{sourceName(s.source)} · {fmtWhen(s.t_ms)}</p>
          {/* 网页、文件只留第一次看时那一张：命中的行在下面滚出来的部分时，图上没有框 */}
          {!!state?.hits.length && !state.hits.some((h) => s.boxes.some((b) => b.line_id === h)) && (
            <p className="mt-1 text-[12.5px] text-faint" data-testid="hit-not-on-shot">{t("viewer.hitNotOnShot")}</p>
          )}
          {lines.length ? (
            <ol className="mt-3 text-[13.5px]" aria-label={t("viewer.lines")}>
              {lines.map((b) => (
                <li key={b.line_id}>
                  <button type="button" aria-pressed={marked.has(b.line_id)} onClick={() => setPicked(picked === b.line_id ? null : b.line_id)}
                    className={`w-full rounded-[6px] px-2 py-1 text-left [overflow-wrap:anywhere] ${marked.has(b.line_id) ? "bg-marker-soft" : "hover:bg-well"}`}>
                    {b.text}
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
