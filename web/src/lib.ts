import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

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

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const n = ++seq.current;
    setLoading(true);
    fn()
      .then((d) => n === seq.current && (setData(d), setError(null)))
      .catch((e: Error) => n === seq.current && setError(e.message))
      .finally(() => n === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

export function usePoll<T>(fn: () => Promise<T>, ms: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = () => {
      fn()
        .then((d) => alive && (setData(d), setError(null)))
        .catch((e: Error) => alive && setError(e.message))
        .finally(() => {
          if (alive) timer = window.setTimeout(tick, document.hidden ? ms * 3 : ms);
        });
    };
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms]);
  return { data, error };
}
