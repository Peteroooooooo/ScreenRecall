import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, isUnauthorized } from "./api";

// ---------- 时间显示：接口给 UTC 毫秒，这里一律按本机时区显示 ----------

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEK_ZH = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const WEEK_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
let timeLang = "zh";
export const setTimeLang = (l: string) => {
  timeLang = l;
};
const en = () => timeLang === "en";

/** 本机日期 YYYY-MM-DD（<input type="date"> 与 /api/v2/day?date= 的格式） */
export const localDate = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
/** YYYY-MM-DDTHH:MM（<input type="datetime-local"> 的格式） */
export const localDateTime = (d: Date) => `${localDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

/** "YYYY-MM-DD" -> 本机那天 0 点；格式不对返回 null */
export function parseLocalDate(s: string | null | undefined): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s ?? "");
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) || localDate(d) !== s ? null : d;
}

export function addDays(date: string, n: number): string {
  const d = parseLocalDate(date) ?? new Date();
  return localDate(new Date(d.getFullYear(), d.getMonth(), d.getDate() + n));
}

export const startOfToday = (now = Date.now()) => {
  const d = new Date(now);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
};

export function fmtClock(ms: number | null | undefined, withSeconds = false): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? ":" + pad(d.getSeconds()) : ""}`;
}

/** 月日（同年不写年份） */
export function fmtMonthDay(d: Date): string {
  const sameYear = d.getFullYear() === new Date().getFullYear();
  if (en()) return `${MONTHS[d.getMonth()]} ${d.getDate()}${sameYear ? "" : ", " + d.getFullYear()}`;
  return `${sameYear ? "" : d.getFullYear() + "年"}${d.getMonth() + 1}月${d.getDate()}日`;
}

export const weekday = (d: Date) => (en() ? WEEK_EN : WEEK_ZH)[d.getDay()];

/** 今天 09:41 / 昨天 20:15 / 10月25日 14:02 */
export function fmtWhen(ms: number | null | undefined, now = Date.now()): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  const day0 = startOfToday(now);
  const clock = fmtClock(ms);
  if (ms >= day0 && ms < day0 + 86_400_000) return `${en() ? "Today" : "今天"} ${clock}`;
  const y0 = new Date(day0);
  const yesterday = new Date(y0.getFullYear(), y0.getMonth(), y0.getDate() - 1).getTime();
  if (ms >= yesterday && ms < day0) return `${en() ? "Yesterday" : "昨天"} ${clock}`;
  return `${fmtMonthDay(d)} ${clock}`;
}

/** 36 分钟 / 1 小时 12 分 / 不到 1 分钟 */
export function fmtDuration(ms: number): string {
  const min = Math.round(Math.max(0, ms) / 60_000);
  if (min < 1) return en() ? "< 1 min" : "不到 1 分钟";
  if (min < 60) return en() ? `${min} min` : `${min} 分钟`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (en()) return m ? `${h} h ${m} min` : `${h} h`;
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / 1024 ** i;
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

export const fmtInt = (n: number) => n.toLocaleString(en() ? "en-US" : "zh-CN");

/** 应用的显示名：去掉 .exe */
export const appName = (app: string) => app.replace(/\.exe$/i, "");
/** 头像用的首字 */
export const initial = (app: string) => (Array.from(appName(app).trim())[0] ?? "?").toUpperCase();

// ---------- 命中高亮：spans 是服务端（Python）给的字符偏移，按 Unicode 码点计，不是 UTF-16 ----------

export type Seg = { text: string; hit: boolean };

export function highlight(text: string, spans: [number, number][]): Seg[] {
  const cps = Array.from(text);
  const n = cps.length;
  const rs = spans
    .map(([a, b]) => [Math.max(0, Math.min(n, a)), Math.max(0, Math.min(n, b))] as [number, number])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  const merged: [number, number][] = [];
  for (const r of rs) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  const out: Seg[] = [];
  let at = 0;
  for (const [a, b] of merged) {
    if (a > at) out.push({ text: cps.slice(at, a).join(""), hit: false });
    out.push({ text: cps.slice(a, b).join(""), hit: true });
    at = b;
  }
  if (at < n || !out.length) out.push({ text: cps.slice(at).join(""), hit: false });
  return out;
}

// ---------- 哈希路由：#/路径?参数，支持深链接与浏览器后退 ----------

function readHash() {
  const h = window.location.hash.replace(/^#/, "") || "/";
  const i = h.indexOf("?");
  const path = (i < 0 ? h : h.slice(0, i)) || "/";
  return { path, params: new URLSearchParams(i < 0 ? "" : h.slice(i + 1)) };
}

let snapshot = readHash();
let snapKey = window.location.hash;
const subscribe = (cb: () => void) => {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
};
const getSnapshot = () => {
  if (window.location.hash !== snapKey) {
    snapKey = window.location.hash;
    snapshot = readHash();
  }
  return snapshot;
};

export function useRoute() {
  return useSyncExternalStore(subscribe, getSnapshot);
}

export function hrefOf(path: string, params?: Record<string, string | number | undefined | null>) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  const s = qs.toString();
  return `#${path}${s ? "?" + s : ""}`;
}

export function navigate(path: string, params?: Record<string, string | number | undefined | null>, replace = false) {
  const url = hrefOf(path, params);
  if (replace) {
    history.replaceState(null, "", url);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else window.location.hash = url;
}

// ---------- 错误文案 ----------

export const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";

/** 服务端给的中文说明原样显示；连不上（fetch 抛 TypeError 等）用 offline 文案 */
export function errText(e: unknown, offline: string): string {
  if (e instanceof ApiError) return e.message;
  return offline;
}

// ---------- 数据加载 ----------

/** 加载一次（deps 变化时重新加载）。换了 deps 或卸载时中止上一个请求。reload(true) = 后台刷新，不显示加载状态。 */
export function useLoad<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const ctrl = useRef<AbortController | null>(null);
  const reload = useCallback((silent = false) => {
    const n = ++seq.current;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    if (!silent) setLoading(true);
    fn(c.signal)
      .then((d) => {
        if (n !== seq.current) return;
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (n !== seq.current || c.signal.aborted || isAbort(e)) return;
        if (!silent) setError(e);
      })
      .finally(() => n === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    reload();
  }, [reload]);
  useEffect(() => () => ctrl.current?.abort(), []);
  return { data, error, loading, reload, setData };
}

// ---------- 状态轮询：连不上时明确标出"连接中断"，不让旧状态冒充当前状态 ----------

/** 单次轮询超过这么久没回应就算失败（服务卡死时 fetch 不会自己报错） */
export const POLL_TIMEOUT_MS = 4000;
/** 连续失败 2 次，或距上次成功超过这么久，就判为连接中断 */
export const LOST_AFTER_MS = 5000;
/** 页面在后台时轮询放慢的倍数 */
export const HIDDEN_FACTOR = 5;

export function usePoll<T>(fn: (signal: AbortSignal) => Promise<T>, ms: number, timeoutMs = POLL_TIMEOUT_MS) {
  const [data, setDataState] = useState<T | null>(null);
  const [lost, setLost] = useState(false);
  const kick = useRef<() => void>(() => {});
  // 外部直接写入（比如暂停接口返回的新状态）之后，比它早发出的那次轮询结果是旧的，丢掉，免得顶栏闪回旧状态
  const epoch = useRef(0);
  const setData = useCallback((d: T) => {
    epoch.current += 1;
    setDataState(d);
  }, []);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let ctrl: AbortController | null = null;
    let inflight = false;
    let fails = 0;
    let lastOk = Date.now();
    const schedule = (delay: number) => {
      window.clearTimeout(timer);
      if (alive && !isUnauthorized()) timer = window.setTimeout(tick, delay);
    };
    function tick() {
      if (!alive || isUnauthorized() || inflight) return;
      inflight = true;
      const c = new AbortController();
      ctrl = c;
      const to = window.setTimeout(() => c.abort(), timeoutMs);
      const startEpoch = epoch.current;
      fn(c.signal)
        .then((d) => {
          if (!alive) return;
          fails = 0;
          lastOk = Date.now();
          if (startEpoch === epoch.current) setDataState(d);
          setLost(false);
        })
        .catch(() => {
          if (!alive) return;
          fails += 1;
          if (fails >= 2 || Date.now() - lastOk >= LOST_AFTER_MS) setLost(true);
        })
        .finally(() => {
          inflight = false;
          window.clearTimeout(to);
          schedule(document.hidden ? ms * HIDDEN_FACTOR : ms);
        });
    }
    // 页面回到前台时立即刷新一次，不等放慢后的下一轮
    const onVisible = () => !document.hidden && schedule(0);
    document.addEventListener("visibilitychange", onVisible);
    kick.current = () => schedule(0);
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      ctrl?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms]);
  const refresh = useCallback(() => kick.current(), []);
  return { data, lost, setData, refresh };
}

// ---------- 主题：跟随系统 / 浅色 / 深色（只是本机浏览器的偏好，存 localStorage） ----------

export type Theme = "system" | "light" | "dark";
export const THEMES: Theme[] = ["system", "light", "dark"];
const THEME_KEY = "sr-theme";

function readTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* 隐私模式等读不到存储：跟随系统 */
  }
  return "system";
}

let theme: Theme = readTheme();
const themeSubs = new Set<() => void>();

function applyTheme(t: Theme) {
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}
applyTheme(theme);

export function setTheme(t: Theme) {
  theme = t;
  applyTheme(t);
  try {
    if (t === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, t);
  } catch {
    /* 存不下也不影响本次切换 */
  }
  themeSubs.forEach((f) => f());
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const t = useSyncExternalStore(
    (cb) => {
      themeSubs.add(cb);
      return () => themeSubs.delete(cb);
    },
    () => theme,
  );
  return [t, setTheme];
}
