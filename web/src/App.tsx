import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { CalendarDays, ChevronDown, CircleDot, Lock, Monitor, Moon, Pause, Play, Plug, Search, Settings, ShieldCheck, Sun } from "lucide-react";
import { api, isUnauthorized, UNAUTHORIZED_EVENT } from "./api";
import { LangProvider, useT, type Key } from "./i18n";
import { navigate, THEMES, usePoll, useRoute, useTheme } from "./lib";
import { PAUSE_KINDS, recView, usePauseActions } from "./recording";
import { Banner, Dot, StatusCtx, ToastHost, useStatus } from "./ui";
import TodayPage from "./pages/TodayPage";
import PiecePage from "./pages/PiecePage";
import SearchPage from "./pages/SearchPage";
import RecorderPage from "./pages/RecorderPage";
import PrivacyPage from "./pages/PrivacyPage";
import AgentPage from "./pages/AgentPage";
import SettingsPage from "./pages/SettingsPage";
import ImportPage from "./pages/ImportPage";

/** 顶栏状态的轮询间隔；页面在后台时 usePoll 自动放慢 */
export const STATUS_POLL_MS = 2000;

type NavItem = { path: string; key: Key; short: Key; icon: typeof Search };
const NAV: NavItem[] = [
  { path: "/", key: "nav.today", short: "nav.today", icon: CalendarDays },
  { path: "/search", key: "nav.search", short: "nav.search", icon: Search },
  { path: "/recorder", key: "nav.recorder", short: "nav.recorderShort", icon: CircleDot },
  { path: "/privacy", key: "nav.privacy", short: "nav.privacyShort", icon: ShieldCheck },
  { path: "/agent", key: "nav.agent", short: "nav.agentShort", icon: Plug },
  { path: "/settings", key: "nav.settings", short: "nav.settings", icon: Settings },
];

type Route = { page: ReactNode; nav: string; title: Key };

function resolve(path: string, params: URLSearchParams): Route {
  const piece = /^\/piece\/(\d+)$/.exec(path);
  if (piece) return { page: <PiecePage key={piece[1]} id={Number(piece[1])} />, nav: "/", title: "title.piece" };
  switch (path) {
    case "/search": return { page: <SearchPage params={params} />, nav: "/search", title: "nav.search" };
    case "/recorder": return { page: <RecorderPage />, nav: "/recorder", title: "nav.recorder" };
    case "/privacy": return { page: <PrivacyPage />, nav: "/privacy", title: "nav.privacy" };
    case "/agent": return { page: <AgentPage />, nav: "/agent", title: "nav.agent" };
    case "/settings": return { page: <SettingsPage />, nav: "/settings", title: "nav.settings" };
    case "/import": return { page: <ImportPage />, nav: "/settings", title: "title.import" };
    default: return { page: <TodayPage params={params} />, nav: "/", title: "nav.today" };
  }
}

function Logo() {
  return (
    <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden className="shrink-0">
      <rect x="1" y="1" width="30" height="30" rx="8" fill="#1d222b" stroke="#2c333e" />
      <rect x="7" y="10" width="13" height="3.2" rx="1.2" fill="#f2b400" />
      <rect x="7" y="16" width="18" height="2.2" rx="1.1" fill="#5b7cf0" />
      <rect x="7" y="21" width="10" height="2.2" rx="1.1" fill="#4a515d" />
    </svg>
  );
}

function Sidebar({ active }: { active: string }) {
  const { t } = useT();
  return (
    <aside className="sr-dark-scroll sticky top-0 hidden h-screen w-[240px] shrink-0 flex-col overflow-y-auto bg-night text-night-text lg:flex">
      <div className="flex items-center gap-2.5 px-5 pb-5 pt-[22px]">
        <Logo />
        <div className="min-w-0">
          <b className="block text-[15px] text-white">ScreenRecall</b>
          <small className="block truncate text-[11px] text-night-faint">{t("app.tagline")}</small>
        </div>
      </div>
      <nav className="flex flex-col gap-0.5 px-3" aria-label={t("nav.main")}>
        {NAV.map(({ path, key, icon: Icon }) => {
          const on = active === path;
          return (
            <a key={path} href={`#${path}`} aria-current={on ? "page" : undefined}
              className={`flex h-10 items-center gap-[11px] rounded-[9px] px-3 text-[14px] ${on ? "bg-night-3 font-medium text-white" : "text-night-faint hover:bg-night-2 hover:text-night-text"}`}>
              <Icon size={18} strokeWidth={1.8} className={on ? "text-marker" : ""} aria-hidden />
              {t(key)}
            </a>
          );
        })}
      </nav>
      <p className="mt-auto px-5 py-5 text-[11.5px] leading-relaxed text-night-faint">{t("app.footnote")}</p>
    </aside>
  );
}

function MobileNav({ active }: { active: string }) {
  const { t } = useT();
  return (
    <nav className="sr-no-scrollbar flex gap-1 overflow-x-auto bg-night px-4 py-2 lg:hidden" aria-label={t("nav.main")}>
      {NAV.map(({ path, short, icon: Icon }) => (
        <a key={path} href={`#${path}`} aria-current={active === path ? "page" : undefined}
          className={`inline-flex h-[34px] shrink-0 items-center gap-[7px] rounded-[9px] px-3 text-[13.5px] ${active === path ? "bg-night-3 text-white" : "text-night-faint"}`}>
          <Icon size={15} aria-hidden />{t(short)}
        </a>
      ))}
    </nav>
  );
}

function StatusPill() {
  const { t } = useT();
  const { status, lost } = useStatus();
  const v = recView(status, lost, t);
  return (
    <a href="#/recorder" title={t("st.pillTitle")} data-testid="status-pill"
      className="inline-flex h-9 min-w-0 flex-1 items-center gap-2 rounded-full border border-rule bg-surface px-3 text-[13.5px] text-ink hover:border-rule-strong lg:max-w-[420px] lg:flex-none">
      <Dot tone={v.tone} />
      <span className="truncate">
        {v.label}
        {v.detail && <span className="hidden sm:inline"> · {v.detail}</span>}
      </span>
    </a>
  );
}

function PauseMenu() {
  const { t } = useT();
  const { status, lost } = useStatus();
  const { pause, resume } = usePauseActions();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const paused = status?.recording === "paused";

  useEffect(() => {
    if (!open) return;
    items.current[0]?.focus();
    const onDoc = (e: MouseEvent) => !wrap.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  useEffect(() => {
    if (paused) setOpen(false);
  }, [paused]);

  const onKey = (e: KeyboardEvent) => {
    const list = items.current.filter(Boolean) as HTMLButtonElement[];
    const i = list.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    if (e.key === "Home") { e.preventDefault(); list[0]?.focus(); }
    if (e.key === "End") { e.preventDefault(); list[list.length - 1]?.focus(); }
    if (e.key === "Escape" || e.key === "Tab") {
      if (e.key === "Escape") e.preventDefault();
      setOpen(false);
      btn.current?.focus();
    }
  };

  if (paused)
    return (
      <button className="sr-btn sr-btn-line sr-btn-sm" onClick={resume} disabled={lost}>
        <Play size={15} aria-hidden />{t("pause.resume")}
      </button>
    );
  return (
    <div className="relative" ref={wrap}>
      <button ref={btn} className="sr-btn sr-btn-line sr-btn-sm" aria-haspopup="menu" aria-expanded={open} aria-controls="pause-menu"
        disabled={!status || lost} onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => e.key === "ArrowDown" && (e.preventDefault(), setOpen(true))}>
        <Pause size={15} aria-hidden />{t("pause.btn")}<ChevronDown size={15} aria-hidden />
      </button>
      {open && (
        <div id="pause-menu" role="menu" aria-label={t("pause.menu")} onKeyDown={onKey}
          className="absolute left-0 top-[calc(100%+6px)] z-40 min-w-[170px] rounded-[12px] border border-rule bg-surface p-1.5 shadow-lift sm:left-auto sm:right-0">
          <div className="px-2.5 pb-1.5 pt-1 text-xs text-faint" aria-hidden>{t("pause.menu")}</div>
          {PAUSE_KINDS.map((k, i) => (
            <button key={k} ref={(el) => { items.current[i] = el; }} role="menuitem" tabIndex={-1}
              className="flex h-9 w-full items-center rounded-[8px] px-2.5 text-left text-sm hover:bg-well focus-visible:bg-well"
              onClick={() => { setOpen(false); btn.current?.focus(); void pause(k); }}>
              {t(`pause.${k}`)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function GlobalSearch({ path, params }: { path: string; params: URLSearchParams }) {
  const { t } = useT();
  const ref = useRef<HTMLInputElement>(null);
  const urlQ = path === "/search" ? params.get("q") ?? "" : "";
  const [q, setQ] = useState(urlQ);
  useEffect(() => setQ(urlQ), [urlQ]);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        ref.current?.focus();
        ref.current?.select();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return (
    <form role="search" className="relative order-10 flex w-full items-center lg:order-none lg:ml-auto lg:w-[340px]"
      onSubmit={(e) => {
        e.preventDefault();
        const v = q.trim();
        if (!v) return;
        // 已在搜索页：保留筛选条件，只换搜索词
        const keep = path === "/search" ? Object.fromEntries(params.entries()) : {};
        navigate("/search", { ...keep, q: v });
      }}>
      <label className="sr-only" htmlFor="gq">{t("gs.label")}</label>
      <Search size={15} className="pointer-events-none absolute left-3 text-faint" aria-hidden />
      <input ref={ref} id="gq" name="q" type="search" autoComplete="off" value={q} onChange={(e) => setQ(e.target.value)}
        placeholder={t("gs.placeholder")} className="sr-field rounded-full pl-9 pr-16" />
      <kbd className="sr-kbd pointer-events-none absolute right-2.5" aria-hidden>Ctrl K</kbd>
    </form>
  );
}

function ThemeButton() {
  const { t } = useT();
  const [theme, setTheme] = useTheme();
  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  const label = t("theme.btn", { x: t(`theme.${theme}`) });
  return (
    <button className="sr-btn sr-btn-ghost sr-btn-icon sr-btn-sm" aria-label={label} title={label}
      onClick={() => setTheme(THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length])}>
      <Icon size={17} aria-hidden />
    </button>
  );
}

function Unauthorized() {
  const { t } = useT();
  useEffect(() => {
    document.title = "ScreenRecall";
  }, []);
  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas p-4 text-ink">
      <div className="sr-card w-full max-w-lg p-6 text-center sm:p-8" role="alert">
        <span className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full bg-well text-graphite" aria-hidden><Lock size={20} /></span>
        <h1 className="text-xl font-semibold">{t("unauth.title")}</h1>
        <p className="mt-2 text-sm leading-relaxed text-graphite">{t("unauth.body")}</p>
      </div>
    </main>
  );
}

function Shell() {
  const { t } = useT();
  const { path, params } = useRoute();
  const poll = usePoll((signal) => api.status(signal), STATUS_POLL_MS);
  const route = resolve(path, params);
  const ctx = useMemo(() => ({ status: poll.data, lost: poll.lost, setStatus: poll.setData }), [poll.data, poll.lost, poll.setData]);
  const first = useRef(true);

  // 换页：标题、回到顶部、焦点移到新页面的 h1（读屏软件会读出新页面）
  useEffect(() => {
    document.title = `${t(route.title)} · ScreenRecall`;
  }, [route.title, t]);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    try {
      window.scrollTo(0, 0);
    } catch {
      /* 测试环境没有 scrollTo */
    }
    const id = window.setTimeout(() => document.querySelector<HTMLElement>("main [data-page-title]")?.focus({ preventScroll: true }), 0);
    return () => window.clearTimeout(id);
  }, [path]);

  return (
    <StatusCtx.Provider value={ctx}>
      <ToastHost>
        <a href="#main" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}
          className="absolute -top-12 left-3 z-[100] rounded-lg bg-ink px-3 py-2 text-on-ink focus:top-3">{t("app.skip")}</a>
        <div className="flex min-h-screen bg-canvas font-sans text-[14.5px] leading-[1.55] text-ink">
          <Sidebar active={route.nav} />
          <div className="flex min-w-0 flex-1 flex-col">
            <header className="z-30 flex flex-wrap items-center gap-2 border-b border-rule bg-canvas/90 px-4 py-2.5 backdrop-blur lg:sticky lg:top-0 lg:flex-nowrap lg:gap-2.5 lg:px-8">
              <span className="lg:hidden"><Logo /></span>
              <StatusPill />
              <PauseMenu />
              <GlobalSearch path={path} params={params} />
              <ThemeButton />
            </header>
            <MobileNav active={route.nav} />
            <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1180px] flex-1 px-4 pb-12 pt-5 outline-none lg:px-8 lg:pb-14 lg:pt-7">
              {poll.lost && (
                <Banner tone="alarm" className="mb-4">
                  <b>{t("common.offline")}</b>
                  <p className="mt-0.5 text-[12.5px] text-graphite">{t("common.offlineHint")}</p>
                </Banner>
              )}
              {route.page}
            </main>
          </div>
        </div>
      </ToastHost>
    </StatusCtx.Provider>
  );
}

export default function App() {
  const [unauth, setUnauth] = useState(isUnauthorized);
  useEffect(() => {
    const on = () => setUnauth(true);
    window.addEventListener(UNAUTHORIZED_EVENT, on);
    if (isUnauthorized()) setUnauth(true);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, on);
  }, []);
  // 401：整页提示，Shell 卸载后所有轮询与请求随之停止
  // 切换语言时整体重挂载：所有数据按新语言重新请求（条件都在地址里，不会丢）
  return <LangProvider>{(lang) => (unauth ? <Unauthorized /> : <Shell key={lang} />)}</LangProvider>;
}
