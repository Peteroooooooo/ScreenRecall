import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Check, Info, LoaderCircle, TriangleAlert, X } from "lucide-react";
import type { Status } from "./api";
import { useT } from "./i18n";
import { initial } from "./lib";

// ---------- 提示条 ----------

export type Tone = "info" | "warn" | "alarm" | "ok";
const TONE: Record<Tone, string> = {
  info: "border-rule bg-surface",
  warn: "border-marker/45 bg-warn-bg",
  alarm: "border-alarm/35 bg-alarm-bg",
  ok: "border-signal/35 bg-ok-bg",
};
const TONE_ICON: Record<Tone, ReactNode> = {
  info: <Info size={16} className="text-graphite" aria-hidden />,
  warn: <TriangleAlert size={16} className="text-marker-ink" aria-hidden />,
  alarm: <TriangleAlert size={16} className="text-alarm" aria-hidden />,
  ok: <Check size={16} className="text-signal" aria-hidden />,
};

export function Banner({ tone = "info", children, className = "" }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <div role={tone === "alarm" ? "alert" : "status"}
      className={`flex items-start gap-2.5 rounded-control border px-3.5 py-2.5 text-sm leading-relaxed text-ink ${TONE[tone]} ${className}`}>
      <span className="mt-[3px] shrink-0">{TONE_ICON[tone]}</span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function Spinner({ label, className = "" }: { label?: string; className?: string }) {
  const { t } = useT();
  return (
    <span className={`inline-flex items-center gap-2 text-sm text-graphite ${className}`} role="status">
      <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" aria-hidden />
      {label ?? t("common.loading")}
    </span>
  );
}

/** 加载骨架：先给出页面大致形状，避免空白闪烁 */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      <Spinner />
      <div className="space-y-3" aria-hidden>
        <div className="sr-skel h-4 w-3/5" />
        {Array.from({ length: rows }, (_, i) => <div key={i} className="sr-skel h-14" />)}
      </div>
    </div>
  );
}

/** 页面级加载失败：说明 + 重试 */
export function LoadError({ text, onRetry }: { text: string; onRetry?: () => void }) {
  const { t } = useT();
  return (
    <div className="space-y-3">
      <Banner tone="alarm">{text}</Banner>
      {onRetry && <button className="sr-btn sr-btn-line sr-btn-sm" onClick={onRetry}>{t("common.retry")}</button>}
    </div>
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
    setItems((xs) => [...xs.slice(-3), { id, text, tone }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "alarm" ? 8000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
        {items.map((x) => (
          <div key={x.id} className="pointer-events-auto animate-rise rounded-control shadow-lift">
            <Banner tone={x.tone}>{x.text}</Banner>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---------- 对话框：原生 <dialog> 的 showModal 让页面其余部分不可操作（焦点圈在框内），Esc 关闭；关闭后焦点回到打开它的元素 ----------

export function Dialog({ open, title, onClose, children, footer, wide = false }: {
  open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  const { t } = useT();
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      opener.current = document.activeElement as HTMLElement | null;
      d.showModal();
    }
    if (!open && d.open) d.close();
  }, [open]);
  useEffect(() => {
    if (!open && opener.current) {
      const el = opener.current;
      opener.current = null;
      if (el.isConnected) el.focus();
    }
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={() => open && onClose()}
      onClick={(e) => e.target === ref.current && onClose()}
      className={`m-auto max-h-[calc(100vh-3rem)] w-[calc(100vw-2rem)] rounded-card border border-rule bg-surface p-0 text-ink shadow-lift ${wide ? "max-w-[880px]" : "max-w-[520px]"}`}
    >
      {open && (
        <div className="flex max-h-[calc(100vh-3rem)] flex-col">
          <div className="flex items-center justify-between gap-3 px-5 pb-1.5 pt-4 sm:px-6">
            <h2 id={titleId} className="min-w-0 text-[17px] font-semibold">{title}</h2>
            <button className="sr-btn sr-btn-ghost sr-btn-icon sr-btn-sm" onClick={onClose} aria-label={t("common.close")}><X size={16} aria-hidden /></button>
          </div>
          <div className="min-h-0 overflow-auto px-5 py-2 text-sm leading-relaxed sm:px-6">{children}</div>
          {footer && <div className="flex flex-wrap justify-end gap-2 px-5 pb-4 pt-3 sm:px-6">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

export type ConfirmSpec = { title: string; body: ReactNode; ok?: string; tone?: "danger" | "primary"; run: () => Promise<void> | void };

/** 确认对话框（删除等不可恢复的操作）。spec 为 null 时关闭；确认后执行 run，执行期间按钮不可再点。 */
export function ConfirmDialog({ spec, onClose }: { spec: ConfirmSpec | null; onClose: () => void }) {
  const { t } = useT();
  const [busy, setBusy] = useState(false);
  const go = async () => {
    if (!spec) return;
    setBusy(true);
    try {
      await spec.run();
    } finally {
      setBusy(false);
      onClose();
    }
  };
  return (
    <Dialog open={!!spec} title={spec ? t("common.confirmTitle", { x: spec.title }) : ""} onClose={() => !busy && onClose()}
      footer={<>
        <button className="sr-btn sr-btn-line" onClick={onClose} disabled={busy} autoFocus>{t("common.cancel")}</button>
        <button className={`sr-btn ${spec?.tone === "primary" ? "sr-btn-primary" : "sr-btn-danger-solid"}`} onClick={go} disabled={busy}>
          {busy && <LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" aria-hidden />}
          {spec?.ok ?? t("common.delete")}
        </button>
      </>}>
      <p>{spec?.body}</p>
    </Dialog>
  );
}

// ---------- 布局件 ----------

export function PageHeader({ title, sub, children, before }: { title: string; sub?: ReactNode; children?: ReactNode; before?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
      <div className="flex min-w-0 max-w-3xl items-start gap-3">
        {before}
        <div className="min-w-0">
          <h1 tabIndex={-1} data-page-title className="text-[22px] font-semibold leading-tight outline-none sm:text-[26px]">{title}</h1>
          {sub && <div className="mt-1 text-sm text-graphite">{sub}</div>}
        </div>
      </div>
      {children}
    </div>
  );
}

export function Card({ title, aside, children, className = "", id }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  return (
    <section className={`sr-card min-w-0 p-4 sm:p-5 ${className}`} aria-labelledby={title ? id : undefined}>
      {(title || aside) && (
        <div className="mb-3.5 flex flex-wrap items-center justify-between gap-3">
          {title && <h2 id={id} className="text-[15px] font-semibold">{title}</h2>}
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}

export function Empty({ title, children, icon, action }: { title: string; children?: ReactNode; icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="sr-card px-5 py-11 text-center">
      {icon && <span className="mb-2.5 inline-flex h-11 w-11 items-center justify-center rounded-full bg-well text-graphite" aria-hidden>{icon}</span>}
      <p className="font-semibold">{title}</p>
      {children && <div className="mx-auto mt-1 max-w-md text-sm text-graphite">{children}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Av({ app, small = false }: { app: string; small?: boolean }) {
  return (
    <span aria-hidden className={`inline-flex shrink-0 items-center justify-center border border-rule bg-well font-semibold text-ink-2 ${small ? "h-[26px] w-[26px] rounded-[7px] text-xs" : "h-[34px] w-[34px] rounded-[9px] text-sm"}`}>
      {initial(app)}
    </span>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label, labelledBy, size = "md" }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void; label?: string; labelledBy?: string; size?: "sm" | "md";
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const at = Math.max(0, options.findIndex((o) => o.value === value));
  const onKey = (e: KeyboardEvent) => {
    const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (at + d + options.length) % options.length;
    onChange(options[n].value);
    refs.current[n]?.focus();
  };
  return (
    <div role="radiogroup" aria-label={label} aria-labelledby={labelledBy} onKeyDown={onKey}
      className="inline-flex max-w-full flex-wrap rounded-control bg-well p-[3px]">
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button key={o.value} ref={(el) => { refs.current[i] = el; }} type="button" role="radio" aria-checked={on} tabIndex={i === at ? 0 : -1}
            onClick={() => onChange(o.value)}
            className={`rounded-[8px] px-3 font-medium transition-colors ${size === "sm" ? "h-7 text-xs" : "h-8 text-[13.5px]"} ${on ? "bg-surface text-ink shadow-[0_1px_2px_rgb(16_24_40/0.12)]" : "text-graphite hover:text-ink"}`}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Switch({ on, onChange, labelledBy, disabled = false }: { on: boolean; onChange: (v: boolean) => void; labelledBy: string; disabled?: boolean }) {
  const { t } = useT();
  return (
    <button type="button" role="switch" aria-checked={on} aria-labelledby={labelledBy} disabled={disabled} onClick={() => onChange(!on)}
      className="inline-flex items-center gap-2.5 font-medium disabled:opacity-50">
      <span className={`relative h-6 w-[42px] shrink-0 rounded-full transition-colors ${on ? "bg-signal" : "bg-rule-strong"}`} aria-hidden>
        <span className={`absolute left-[3px] top-[3px] h-[18px] w-[18px] rounded-full bg-white shadow transition-transform ${on ? "translate-x-[18px]" : ""}`} />
      </span>
      <span>{on ? t("common.on") : t("common.off")}</span>
    </button>
  );
}

/** 可移除的名单（排除、对 Agent 隐藏） */
export function Chips({ items, onRemove, empty }: { items: { key: string; label: string }[]; onRemove: (key: string) => void; empty: string }) {
  const { t } = useT();
  if (!items.length) return <p className="text-sm text-faint">{empty}</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((x) => (
        <li key={x.key} className="inline-flex h-[30px] max-w-full items-center gap-1 rounded-full border border-rule-strong bg-surface pl-[11px] pr-1 text-[13px]">
          <span className="truncate">{x.label}</span>
          <button type="button" onClick={() => onRemove(x.key)} aria-label={t("common.removeX", { x: x.label })}
            className="inline-flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full text-faint hover:bg-well hover:text-ink">
            <X size={14} aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );
}

export type DotTone = "rec" | "idle" | "off" | "pending";
export function Dot({ tone }: { tone: DotTone }) {
  const c = { rec: "bg-signal", idle: "bg-faint", off: "bg-alarm", pending: "bg-marker" }[tone];
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0" aria-hidden>
      {tone === "rec" && <span className={`absolute inset-0 animate-ping rounded-full ${c} opacity-60 motion-reduce:hidden`} />}
      <span className={`relative inline-block h-full w-full rounded-full ${c}`} />
    </span>
  );
}

// ---------- 全局状态 ----------

export const StatusCtx = createContext<{ status: Status | null; lost: boolean; setStatus: (s: Status) => void }>({
  status: null, lost: false, setStatus: () => {},
});
export const useStatus = () => useContext(StatusCtx);

/** 复制到剪贴板；失败（权限、非安全上下文）时返回 false */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
