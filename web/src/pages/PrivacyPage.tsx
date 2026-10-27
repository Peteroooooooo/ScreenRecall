// 隐私与删除：最近 5 分钟 / 1 小时 / 今天、任意时间段、按应用删除（都要先确认）；数据位置与占用。
import { useState, type FormEvent } from "react";
import { api, type DeleteRequest, type DeleteResult } from "../api";
import { useT } from "../i18n";
import { appName, errText, fmtBytes, fmtInt, fmtWhen, localDateTime, startOfToday, useLoad } from "../lib";
import { useSettings, hasItem } from "../settings";
import { Banner, Card, ConfirmDialog, LoadError, PageHeader, Skeleton, useToast, type ConfirmSpec } from "../ui";

export default function PrivacyPage() {
  const { t } = useT();
  const toast = useToast();
  const storage = useLoad((signal) => api.storage(signal), []);
  const apps = useLoad((signal) => api.apps({}, signal), []);
  const settings = useSettings();
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const now = new Date();
  const [from, setFrom] = useState(() => localDateTime(new Date(now.getTime() - 3_600_000)));
  const [to, setTo] = useState(() => localDateTime(now));
  const [app, setApp] = useState("");
  const [alsoExclude, setAlsoExclude] = useState(true);
  const rangeBad = !!from && !!to && new Date(from).getTime() >= new Date(to).getTime();

  const doneText = (r: DeleteResult) => {
    const l = r.deleted.lines ?? 0;
    const f = r.deleted.frames ?? 0;
    return Object.values(r.deleted).some((n) => n > 0) ? t("priv.done", { l: fmtInt(l), f: fmtInt(f) }) : t("priv.doneNone");
  };
  const run = async (body: DeleteRequest, before?: () => Promise<void>) => {
    try {
      if (before) await before();
      const r = await api.remove(body);
      toast(doneText(r));
    } catch (e) {
      toast(t("priv.fail", { e: errText(e, t("common.offline")) }), "alarm");
    } finally {
      storage.reload(true);
      apps.reload(true);
    }
  };
  const ask = (title: string, body: string, req: () => DeleteRequest, before?: () => Promise<void>) =>
    setConfirm({ title, body, run: () => run(req(), before) });

  const onRange = (e: FormEvent) => {
    e.preventDefault();
    if (rangeBad || !from || !to) return;
    const a = new Date(from).getTime();
    const b = new Date(to).getTime();
    ask(t("priv.dRange"), t("priv.bodyRange", { a: fmtWhen(a), b: fmtWhen(b) }), () => ({ from_ms: a, to_ms: b }));
  };
  const onApp = (e: FormEvent) => {
    e.preventDefault();
    if (!app) return;
    const ex = alsoExclude;
    // 先加进排除名单再删：删除只覆盖到“现在”，先排除才不会在两步之间又记下这个应用的新内容
    ask(t("priv.dApp"), `${t("priv.bodyApp", { x: appName(app) })}${ex ? " " + t("priv.bodyAppEx") : ""}`, () => ({ app }), async () => {
      if (ex) {
        const s = await settings.save((cur) => (hasItem(cur.excluded_processes, app) ? null : { excluded_processes: [...cur.excluded_processes, app] }));
        if (s) toast(t("priv.excluded", { x: app }));
      }
      setApp("");
    });
  };

  const s = storage.data;
  const total = s ? s.db_bytes + s.frames_bytes + s.vectors_bytes : 0;
  const pct = (n: number) => `${total ? Math.max(n > 0 ? 1 : 0, (n / total) * 100) : 0}%`;
  const appList = apps.data?.apps ?? [];

  return (
    <div>
      <PageHeader title={t("priv.title")} sub={t("priv.sub")} />
      <div className="space-y-4">
        <Card title={t("priv.quick")} id="quick-h">
          <div className="grid gap-2.5 sm:grid-cols-3">
            <button className="sr-btn sr-btn-danger h-[52px]" onClick={() => ask(t("priv.d5"), t("priv.body5"), () => ({ recent_minutes: 5 }))}>{t("priv.d5")}</button>
            <button className="sr-btn sr-btn-danger h-[52px]" onClick={() => ask(t("priv.d60"), t("priv.body60"), () => ({ recent_minutes: 60 }))}>{t("priv.d60")}</button>
            <button className="sr-btn sr-btn-danger h-[52px]"
              onClick={() => ask(t("priv.dToday"), t("priv.bodyToday"), () => ({ from_ms: startOfToday(), to_ms: startOfToday(startOfToday() + 36 * 3_600_000) }))}>{t("priv.dToday")}</button>
          </div>
        </Card>

        <div className="grid gap-4 md:grid-cols-2">
          <Card title={t("priv.range")} id="range-h">
            <form className="space-y-4" onSubmit={onRange}>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex min-w-0 flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium text-graphite">{t("priv.from")}</span>
                  <input type="datetime-local" name="from" className="sr-field" value={from} onChange={(e) => setFrom(e.target.value)} required />
                </label>
                <label className="flex min-w-0 flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium text-graphite">{t("priv.to")}</span>
                  <input type="datetime-local" name="to" className="sr-field" value={to} onChange={(e) => setTo(e.target.value)} required
                    aria-invalid={rangeBad || undefined} aria-describedby={rangeBad ? "range-bad" : undefined} />
                </label>
              </div>
              {rangeBad && <p id="range-bad" className="text-[12.5px] text-alarm">{t("priv.badRange")}</p>}
              <button className="sr-btn sr-btn-danger" type="submit" disabled={rangeBad || !from || !to}>{t("priv.dRange")}</button>
            </form>
          </Card>
          <Card title={t("priv.byApp")} id="app-h">
            {apps.error && !apps.data ? <LoadError text={errText(apps.error, t("common.offline"))} onRetry={() => apps.reload()} /> : (
              <form className="space-y-4" onSubmit={onApp}>
                <label className="flex min-w-0 flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium text-graphite">{t("priv.app")}</span>
                  <select className="sr-field" name="app" value={app} onChange={(e) => setApp(e.target.value)} disabled={!appList.length}>
                    <option value="">{appList.length ? t("priv.appPh") : t("priv.noApps")}</option>
                    {appList.map((a) => <option key={a.app} value={a.app}>{appName(a.app)}（{fmtInt(a.n)}）</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" name="also_exclude" className="h-4 w-4 accent-[var(--cobalt)]" checked={alsoExclude} onChange={(e) => setAlsoExclude(e.target.checked)} />
                  {t("priv.alsoExclude")}
                </label>
                <button className="sr-btn sr-btn-danger" type="submit" disabled={!app}>{t("priv.dApp")}</button>
              </form>
            )}
          </Card>
        </div>

        <Card title={t("priv.where")} id="where-h">
          {!s ? (storage.error ? <LoadError text={errText(storage.error, t("common.offline"))} onRetry={() => storage.reload()} /> : <Skeleton rows={1} />) : (
            <>
              <p className="rounded-lg border border-rule bg-well px-2.5 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">{s.data_dir}</p>
              <p className="mt-2 text-[12.5px] text-graphite">{t("priv.private")}</p>
              <div className="my-2.5 flex h-2.5 overflow-hidden rounded-full bg-well" aria-hidden>
                <span className="bg-ink-2" style={{ width: pct(s.db_bytes) }} />
                <span className="bg-cobalt" style={{ width: pct(s.vectors_bytes) }} />
                <span className="bg-marker" style={{ width: pct(s.frames_bytes) }} />
              </div>
              <div className="sr-num flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-graphite">
                <span><i className="mr-1.5 inline-block h-[9px] w-[9px] rounded-[3px] bg-ink-2 align-[-1px]" />{t("priv.text", { x: fmtBytes(s.db_bytes) })}</span>
                <span><i className="mr-1.5 inline-block h-[9px] w-[9px] rounded-[3px] bg-cobalt align-[-1px]" />{t("priv.vectors", { x: fmtBytes(s.vectors_bytes) })}</span>
                <span><i className="mr-1.5 inline-block h-[9px] w-[9px] rounded-[3px] bg-marker align-[-1px]" />{t("priv.frames", { x: fmtBytes(s.frames_bytes) })}</span>
                <span>{t("priv.total", { x: fmtBytes(total) })}</span>
              </div>
              <p className="sr-num mt-2 text-[12.5px] text-graphite">
                {t("priv.counts", { f: fmtInt(s.frames), l: fmtInt(s.lines) })}
                {s.oldest_ms != null && <> · {t("priv.oldest", { x: fmtWhen(s.oldest_ms) })}</>}
              </p>
            </>
          )}
        </Card>
        {!!settings.error && !settings.data && <Banner tone="warn">{t("set.loadFail", { e: errText(settings.error, t("common.offline")) })}</Banner>}
      </div>
      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}
