import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, get, type ModelState, type Models } from "./api";

// ---------- 时间显示（浏览器与后台在同一台机器上，按本机时区显示，与 timeutil.fmt_local 一致） ----------

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let timeLang = "zh";
export const setTimeLang = (l: string) => {
  timeLang = l;
};

export function fmtTime(ms: number | null | undefined, withSeconds = true): string {
  if (!ms) return "—";
  const d = new Date(ms);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? ":" + pad(d.getSeconds()) : ""}`;
  if (timeLang === "en") return `${MONTHS[d.getMonth()]} ${d.getDate()}${sameYear ? "" : ", " + d.getFullYear()} ${clock}`;
  return `${sameYear ? "" : d.getFullYear() + "年"}${d.getMonth() + 1}月${d.getDate()}日 ${clock}`;
}

export function fmtAge(ms: number | null | undefined, now: number): string {
  if (!ms) return "—";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  const en = timeLang === "en";
  if (s < 120) return en ? `${s} s` : `${s} 秒`;
  if (s < 7200) return en ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 60)} 分钟`;
  return en ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 3600)} 小时`;
}

// ---------- 哈希路由：#/路径?参数，支持深链接与浏览器后退 ----------

function readHash() {
  const h = window.location.hash.replace(/^#/, "") || "/";
  const [path, qs] = h.split("?");
  return { path: path || "/", params: new URLSearchParams(qs ?? "") };
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

export function navigate(path: string, params?: Record<string, string | undefined | null>, replace = false) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const s = qs.toString();
  const url = `#${path}${s ? "?" + s : ""}`;
  if (replace) {
    history.replaceState(null, "", url);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else window.location.hash = url;
}

// ---------- 数据加载 ----------

/** 加载一次（deps 变化时重新加载）。换了 deps 或卸载时中止上一个请求；cancel() 让页面停止等待（服务端照常算完）。 */
export function useLoad<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cancelled, setCancelled] = useState(false);
  const seq = useRef(0);
  const ctrl = useRef<AbortController | null>(null);
  const reload = useCallback(() => {
    const n = ++seq.current;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    setCancelled(false);
    fn(c.signal)
      .then((d) => n === seq.current && (setData(d), setError(null)))
      .catch((e: Error) => n === seq.current && !c.signal.aborted && setError(e.message))
      .finally(() => n === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  const cancel = useCallback(() => {
    seq.current++;
    ctrl.current?.abort();
    setLoading(false);
    setCancelled(true);
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);
  useEffect(() => () => ctrl.current?.abort(), []);
  return { data, error, loading, cancelled, reload, cancel, setData };
}

// ---------- 状态轮询：连不上时明确标出"连接中断"，不让旧状态冒充当前状态 ----------

/** 单次轮询超过这么久没回应就算失败（服务卡死时 fetch 不会自己报错） */
export const POLL_TIMEOUT_MS = 4000;
/** 连续失败 2 次，或距上次成功超过这么久，就判为连接中断 */
export const LOST_AFTER_MS = 5000;
export const TIMEOUT_ERROR = "timeout";

export function usePoll<T>(fn: (signal: AbortSignal) => Promise<T>, ms: number, timeoutMs = POLL_TIMEOUT_MS) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lost, setLost] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let ctrl: AbortController | null = null;
    let fails = 0;
    let lastOk = Date.now();
    const tick = () => {
      const c = new AbortController();
      ctrl = c;
      const to = window.setTimeout(() => c.abort(), timeoutMs);
      fn(c.signal)
        .then((d) => {
          if (!alive) return;
          fails = 0;
          lastOk = Date.now();
          setData(d);
          setError(null);
          setLost(false);
        })
        .catch((e: Error) => {
          if (!alive) return;
          fails += 1;
          setError(c.signal.aborted ? TIMEOUT_ERROR : e.message);
          if (fails >= 2 || Date.now() - lastOk >= LOST_AFTER_MS) setLost(true);
        })
        .finally(() => {
          window.clearTimeout(to);
          if (alive) timer = window.setTimeout(tick, document.hidden ? ms * 3 : ms);
        });
    };
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      ctrl?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms]);
  return { data, error, lost };
}

/** 每 ms 毫秒刷新一次的当前时间（active 为 false 时不走） */
export function useNow(ms: number, active = true) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const iv = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(iv);
  }, [ms, active]);
  return now;
}

// ---------- 筛选列表（应用 / 类别 / 规则）过期：导入完成等场合通知外壳重新拉 /api/meta ----------

export const META_STALE_EVENT = "sr-meta-stale";
export const markMetaStale = () => window.dispatchEvent(new Event(META_STALE_EVENT));

// ---------- 查询模型加载状态（GET /api/models；旧服务返回 404 时 supported = false，退回固定提示） ----------

const isModelState = (x: unknown): x is ModelState =>
  !!x && typeof x === "object" && typeof (x as ModelState).state === "string";

export function useModels(fast: boolean) {
  const [st, setSt] = useState<{ models: Models | null; supported: boolean | null; at: number }>({ models: null, supported: null, at: 0 });
  const fastRef = useRef(fast);
  fastRef.current = fast;
  const kick = useRef<() => void>(() => {});
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let stopped = false;
    const schedule = (delay: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(tick, delay);
    };
    function tick() {
      get<Models>("/api/models")
        .then((m) => {
          if (!alive) return;
          if (!isModelState(m?.text) || !isModelState(m?.image)) {
            stopped = true;
            setSt({ models: null, supported: false, at: Date.now() });
            return;
          }
          setSt({ models: m, supported: true, at: Date.now() });
          const busy = fastRef.current || m.text.state === "loading" || m.image.state === "loading";
          schedule(busy ? 1000 : 10000);
        })
        .catch((e: unknown) => {
          if (!alive) return;
          if (e instanceof ApiError && e.status === 404) {
            stopped = true; // 旧服务没有这个接口：不再请求
            setSt({ models: null, supported: false, at: Date.now() });
            return;
          }
          schedule(5000);
        });
    }
    kick.current = () => !stopped && schedule(0);
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (fast) kick.current();
  }, [fast]);
  return st;
}
