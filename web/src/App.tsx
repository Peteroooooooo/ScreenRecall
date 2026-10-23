import { CircleDot, Eraser, FolderUp, Search, Star } from "lucide-react";
import { get, type Meta, type Status } from "./api";
import { LangProvider, useT, type Key, type Lang } from "./i18n";
import { useLoad, usePoll, useRoute } from "./lib";
import SearchPage from "./SearchPage";
import DetailPage from "./DetailPage";
import RecorderPage from "./RecorderPage";
import { CleanupPage, FavoritesPage, ImportPage } from "./ToolPages";
import { Banner, Dot, MetaCtx, Segmented, Spinner, StatusCtx, ToastHost, stateOf, useMeta } from "./ui";

const NAV: { path: string; key: Key; icon: typeof Search }[] = [
  { path: "/", key: "nav.search", icon: Search },
  { path: "/recorder", key: "nav.recorder", icon: CircleDot },
  { path: "/favorites", key: "nav.favorites", icon: Star },
  { path: "/import", key: "nav.import", icon: FolderUp },
  { path: "/cleanup", key: "nav.cleanup", icon: Eraser },
];

function Page({ path, params }: { path: string; params: URLSearchParams }) {
  const m = path.match(/^\/frame\/([0-9a-f]{32})$/);
  if (m) return <DetailPage key={m[1]} fid={m[1]} params={params} />;
  if (path === "/recorder") return <RecorderPage />;
  if (path === "/favorites") return <FavoritesPage />;
  if (path === "/import") return <ImportPage />;
  if (path === "/cleanup") return <CleanupPage />;
  return <SearchPage params={params} />;
}

function Logo() {
  return (
    <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden className="shrink-0">
      <rect x="1" y="1" width="30" height="30" rx="8" fill="#1d222b" stroke="#2c333e" />
      <rect x="7" y="10" width="13" height="3.2" rx="1.2" fill="var(--color-marker)" />
      <rect x="7" y="16" width="18" height="2.2" rx="1.1" fill="#5b7cf0" />
      <rect x="7" y="21" width="10" height="2.2" rx="1.1" fill="#4a515d" />
    </svg>
  );
}

function LangSwitch({ dark = false }: { dark?: boolean }) {
  const { lang, setLang, t } = useT();
  return (
    <Segmented<Lang> label={t("lang.label")} size="sm" dark={dark} value={lang} onChange={setLang}
      options={[{ value: "zh", label: "中文" }, { value: "en", label: "EN" }]} />
  );
}

function Sidebar({ active, status }: { active: string; status: Status | null }) {
  const { t } = useT();
  const meta = useMeta();
  const st = stateOf(status);
  const ix = (status?.index ?? {}) as Record<string, number>;
  return (
    <aside className="sr-dark-scroll sticky top-0 hidden h-screen w-[252px] shrink-0 flex-col overflow-y-auto bg-night text-night-text lg:flex">
      <div className="flex items-center gap-2.5 px-5 pb-6 pt-6">
        <Logo />
        <div className="min-w-0">
          <div className="text-[15px] font-semibold tracking-tight text-white">ScreenRecall</div>
          <div className="truncate text-[11px] text-night-faint">{t("app.tagline")}</div>
        </div>
      </div>
      <nav className="flex flex-col gap-0.5 px-3" aria-label="Main">
        {NAV.map(({ path, key, icon: Icon }) => {
          const on = active === path;
          return (
            <a key={path} href={`#${path}`} aria-current={on ? "page" : undefined}
              className={`group flex h-10 items-center gap-3 rounded-[9px] px-3 text-[14px] transition-colors ${on ? "bg-night-3 font-medium text-white" : "text-night-faint hover:bg-night-2 hover:text-night-text"}`}>
              <Icon size={17} className={on ? "text-marker" : "text-night-faint group-hover:text-night-text"} strokeWidth={on ? 2.2 : 1.8} />
              {t(key)}
            </a>
          );
        })}
      </nav>

      <div className="mt-auto space-y-3 px-3 pb-5 pt-6">
        <a href="#/recorder" className="block rounded-[12px] border border-night-line bg-night-2 p-3.5 transition-colors hover:border-night-faint/40">
          <div className="flex items-center gap-2 text-[13px] font-medium text-white">
            <Dot tone={st.tone} />
            {t(st.key)}
          </div>
          {status?.alive && status.control.actual_state === "recording" && status.control.target_title && (
            <div className="mt-1.5 truncate text-xs text-night-faint" title={status.control.target_title}>
              {status.control.target_process} · {status.control.target_title}
            </div>
          )}
          {status && <div className="sr-num mt-2 font-mono text-[11px] text-night-faint">{t("mini.stats", { b: ix.backlog ?? 0, f: (ix.frames ?? 0).toLocaleString() })}</div>}
        </a>
        <div className="flex items-center justify-between px-1">
          <LangSwitch dark />
        </div>
        <p className="px-1 text-[11px] leading-relaxed text-night-faint/80">{meta?.notice}</p>
      </div>
    </aside>
  );
}

function TopBar({ active, status }: { active: string; status: Status | null }) {
  const { t } = useT();
  const st = stateOf(status);
  return (
    <header className="sticky top-0 z-30 bg-night text-night-text lg:hidden">
      <div className="flex items-center gap-3 px-4 pt-3">
        <Logo />
        <span className="font-semibold text-white">ScreenRecall</span>
        <a href="#/recorder" className="ml-auto inline-flex items-center gap-1.5 text-xs text-night-faint"><Dot tone={st.tone} />{t(st.key)}</a>
        <LangSwitch dark />
      </div>
      <nav className="flex gap-1 overflow-x-auto px-3 pb-2 pt-2" aria-label="Main">
        {NAV.map(({ path, key, icon: Icon }) => (
          <a key={path} href={`#${path}`} aria-current={active === path ? "page" : undefined}
            className={`inline-flex h-9 shrink-0 items-center gap-2 rounded-[9px] px-3 text-sm ${active === path ? "bg-night-3 text-white" : "text-night-faint"}`}>
            <Icon size={15} />{t(key)}
          </a>
        ))}
      </nav>
    </header>
  );
}

function Shell() {
  const { t } = useT();
  const { path, params } = useRoute();
  const meta = useLoad(() => get<Meta>("/api/meta"), []);
  const status = usePoll(() => get<Status>("/api/status"), 2000);
  const active = path.startsWith("/frame/") ? "/" : NAV.some((n) => n.path === path) ? path : "/";

  if (meta.loading && !meta.data) return <div className="p-10"><Spinner /></div>;
  if (meta.error || !meta.data) return <div className="p-10"><Banner tone="alarm">{t("common.connectFail", { e: meta.error ?? "" })}</Banner></div>;
  if (!meta.data.db_ok)
    return (
      <div className="mx-auto max-w-xl p-10">
        <h1 className="mb-4 text-2xl font-semibold tracking-tight">ScreenRecall</h1>
        <Banner tone="alarm">{meta.data.db_problem}</Banner>
        <button className="sr-btn sr-btn-line mt-4" onClick={meta.reload}>{t("common.retry")}</button>
      </div>
    );

  return (
    <MetaCtx.Provider value={meta.data}>
      <StatusCtx.Provider value={status.data}>
        <ToastHost>
          <div className="flex min-h-screen bg-canvas font-sans text-[14.5px] leading-normal text-ink">
            <Sidebar active={active} status={status.data} />
            <div className="flex min-w-0 flex-1 flex-col">
              <TopBar active={active} status={status.data} />
              <main className="mx-auto w-full max-w-[1480px] flex-1 px-5 py-7 sm:px-8 lg:px-10 lg:py-9">
                <Page path={path} params={params} />
              </main>
            </div>
          </div>
        </ToastHost>
      </StatusCtx.Provider>
    </MetaCtx.Provider>
  );
}

export default function App() {
  // 切换语言时整体重挂载：所有数据按新语言重新请求（查询条件在地址里，不会丢）
  return <LangProvider>{(lang) => <Shell key={lang} />}</LangProvider>;
}
