import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Info, LoaderCircle, TriangleAlert, X } from "lucide-react";
import type { Box, Meta, Status } from "./api";
import { imageUrl, thumbUrl } from "./api";
import { useT, type Key } from "./i18n";

// ---------- 提示条 ----------

export type Tone = "info" | "warn" | "alarm" | "ok";
const TONE: Record<Tone, string> = {
  info: "border-rule bg-surface",
  warn: "border-marker/40 bg-[#fff8e6]",
  alarm: "border-alarm/30 bg-[#fdf1f0]",
  ok: "border-signal/30 bg-[#eef8f3]",
};
const TONE_ICON: Record<Tone, ReactNode> = {
  info: <Info size={16} className="text-graphite" />,
  warn: <TriangleAlert size={16} className="text-marker-ink" />,
  alarm: <TriangleAlert size={16} className="text-alarm" />,
  ok: <Check size={16} className="text-signal" />,
};

export function Banner({ tone = "info", children, className = "" }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <div role={tone === "alarm" ? "alert" : "status"} className={`flex gap-2.5 rounded-control border px-3.5 py-2.5 text-sm leading-relaxed text-ink ${TONE[tone]} ${className}`}>
      <span className="mt-[3px] shrink-0">{TONE_ICON[tone]}</span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  const { t } = useT();
  return (
    <span className="inline-flex items-center gap-2 text-sm text-graphite" role="status">
      <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" />
      {label ?? t("common.loading")}
    </span>
  );
}

// ---------- 轻提示（toast） ----------

type ToastItem = { id: number; text: string; tone: Tone };
const ToastCtx = createContext<(text: string, tone?: Tone) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const push = useCallback((text: string, tone: Tone = "ok") => {
    const id = ++seq.current;
    setItems((xs) => [...xs, { id, text, tone }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "alarm" ? 8000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed bottom-5 right-5 z-50 flex w-[min(26rem,calc(100vw-2.5rem))] flex-col gap-2">
        {items.map((x) => (
          <div key={x.id} className="pointer-events-auto animate-rise rounded-control shadow-lift">
            <Banner tone={x.tone}>{x.text}</Banner>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---------- 对话框（原生 dialog：自带焦点圈定与 Esc 关闭） ----------

export function Dialog({ open, title, onClose, children, footer, wide = false }: {
  open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  const { t } = useT();
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className={`m-auto w-[calc(100vw-2rem)] rounded-card border border-rule bg-surface p-0 text-ink shadow-lift ${wide ? "max-w-3xl" : "max-w-lg"}`}
    >
      {open && (
        <div className="flex max-h-[85vh] flex-col">
          <div className="flex items-center justify-between px-6 pb-2 pt-5">
            <h2 className="text-[17px] font-semibold tracking-tight">{title}</h2>
            <button className="sr-btn sr-btn-ghost h-8 w-8 px-0" onClick={onClose} aria-label={t("common.close")}><X size={16} /></button>
          </div>
          <div className="overflow-auto px-6 py-3 text-sm leading-relaxed">{children}</div>
          {footer && <div className="flex justify-end gap-2 px-6 pb-5 pt-3">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

// ---------- 截图 + 标记层 ----------
// quad 是原图物理像素坐标；SVG 用原图尺寸做 viewBox、拉伸到图片大小，缩略图和原图都能直接叠加。

export function Shot({ fid, width, height, boxes = [], thumb, focus, animate = false, className = "", alt = "" }: {
  fid: string; width: number; height: number; boxes?: Box[]; thumb?: number; focus?: number | null; animate?: boolean;
  className?: string; alt?: string;
}) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  if (failed) return <Unavailable className={className} />;
  return (
    <div className={`relative overflow-hidden bg-night-3 ${className}`} style={{ aspectRatio: `${width} / ${height}` }}>
      <img
        src={thumb ? thumbUrl(fid, thumb) : imageUrl(fid)}
        alt={alt}
        loading="lazy"
        decoding="async"
        onLoad={() => setLoaded(true)}
        onError={() => setFailed(true)}
        className={`absolute inset-0 h-full w-full object-fill transition-opacity duration-300 ${loaded ? "opacity-100" : "opacity-0"}`}
      />
      {boxes.length > 0 && (
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden>
          {boxes.map((b, i) => (
            <polygon
              key={i}
              points={b.quad.map((p) => p.join(",")).join(" ")}
              className={animate ? "sr-mark-shape" : undefined}
              style={animate ? { animationDelay: `${120 + Math.min(i, 12) * 35}ms` } : undefined}
              fill={b.kind === "lex" ? "var(--color-marker)" : "var(--color-cobalt)"}
              fillOpacity={b.kind === "lex" ? (focus === i ? 0.62 : 0.38) : focus === i ? 0.32 : 0.16}
              stroke={b.kind === "lex" ? "var(--color-marker-ink)" : "var(--color-cobalt)"}
              strokeWidth={focus === i ? 3 : 1.5}
              vectorEffect="non-scaling-stroke"
            />
          ))}
        </svg>
      )}
    </div>
  );
}

/** 固定比例的取景框：长截图只露出一段，并把第一处标记移到框内（标记仍与图片对齐）。 */
export function FramedShot({ fid, width, height, boxes = [], thumb = 640, ratio = 16 / 10, className = "" }: {
  fid: string; width: number; height: number; boxes?: Box[]; thumb?: number; ratio?: number; className?: string;
}) {
  const frameOverInner = Math.min(1, (1 / ratio) / (height / width)); // 取景框高 / 图片显示高
  let shift = 0;
  if (frameOverInner < 1 && boxes.length) {
    const ys = boxes[0].quad.map((p) => p[1]);
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2 / height;
    shift = Math.max(0, Math.min(1 - frameOverInner, cy - frameOverInner / 2));
  }
  return (
    <div className={`relative overflow-hidden bg-night-3 ${className}`} style={{ aspectRatio: String(ratio) }}>
      <div className="absolute inset-x-0 top-0" style={{ transform: `translateY(-${shift * 100}%)` }}>
        <Shot fid={fid} width={width} height={height} boxes={boxes} thumb={thumb} />
      </div>
    </div>
  );
}

export function Unavailable({ className = "", text }: { className?: string; text?: string }) {
  const { t } = useT();
  return (
    <div className={`flex aspect-[16/10] items-center justify-center bg-well p-4 text-center text-xs text-faint ${className}`}>
      {text ?? t("common.unavailable")}
    </div>
  );
}

// ---------- 页面级共享 ----------

export const MetaCtx = createContext<Meta | null>(null);
export const useMeta = () => useContext(MetaCtx);
export const StatusCtx = createContext<Status | null>(null);
export const useStatus = () => useContext(StatusCtx);

export function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`h-8 rounded-full border px-3 text-[13px] transition-colors ${on ? "border-ink bg-ink text-white" : "border-rule-strong bg-surface text-graphite hover:border-graphite hover:text-ink"}`}
    >
      {children}
    </button>
  );
}

export function Field({ label, hint, children, className = "" }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return (
    <label className={`flex flex-col gap-1.5 text-sm ${className}`}>
      <span className="text-xs font-medium text-graphite">{label}</span>
      {children}
      {hint && <span className="text-xs text-faint">{hint}</span>}
    </label>
  );
}

export function Empty({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="sr-card flex flex-col items-center px-6 py-14 text-center">
      {icon && <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-well text-graphite">{icon}</div>}
      <p className="font-semibold">{title}</p>
      {children && <div className="mx-auto mt-1.5 max-w-md text-sm text-graphite">{children}</div>}
    </div>
  );
}

export function PageHeader({ title, sub, children }: { title: string; sub?: string; children?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="max-w-3xl">
        <h1 className="text-[26px] font-semibold leading-tight tracking-tight">{title}</h1>
        {sub && <p className="mt-1.5 text-sm leading-relaxed text-graphite">{sub}</p>}
      </div>
      {children}
    </header>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label, size = "md", dark = false }: {
  value: T; options: { value: T; label: ReactNode; title?: string }[]; onChange: (v: T) => void; label: string;
  size?: "sm" | "md"; dark?: boolean;
}) {
  return (
    <div role="radiogroup" aria-label={label}
      className={`inline-flex shrink-0 rounded-control p-[3px] ${dark ? "bg-night-3" : "bg-well"} ${size === "sm" ? "text-xs" : "text-sm"}`}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button key={o.value} type="button" role="radio" aria-checked={on} title={o.title} onClick={() => onChange(o.value)}
            className={`inline-flex items-center gap-1.5 rounded-[8px] font-medium transition-all ${size === "sm" ? "h-7 px-2.5" : "h-9 px-3.5"} ${
              on ? (dark ? "bg-night-line text-white shadow-sm" : "bg-surface text-ink shadow-[0_1px_2px_rgb(16_24_40/0.1)]")
                : dark ? "text-night-faint hover:text-night-text" : "text-graphite hover:text-ink"}`}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function stateOf(s: Status | null): { tone: "rec" | "idle" | "off" | "pending"; key: Key } {
  if (!s || !s.alive) return { tone: "off", key: "mini.offline" };
  if (s.pending) return { tone: "pending", key: s.control.desired_state === "recording" ? "mini.pendingOn" : "mini.pendingOff" };
  if (s.control.actual_state === "recording") return { tone: "rec", key: "mini.recording" };
  return { tone: "idle", key: "mini.idle" };
}

export function Dot({ tone, size = 8 }: { tone: "rec" | "idle" | "off" | "pending"; size?: number }) {
  const c = { rec: "bg-signal", idle: "bg-night-faint", off: "bg-alarm", pending: "bg-marker" }[tone];
  return (
    <span className="relative inline-flex shrink-0" style={{ width: size, height: size }}>
      {tone === "rec" && <span className={`absolute inset-0 animate-ping rounded-full ${c} opacity-60 motion-reduce:hidden`} />}
      <span className={`relative inline-block h-full w-full rounded-full ${c}`} />
    </span>
  );
}

export function useLabelName() {
  const { t } = useT();
  return (l: string) => (["code", "chat", "web", "table_doc"].includes(l) ? t(`label.${l}` as Key) : l);
}
