// 设置：大模型接口（掩码密钥 + 测试）、空闲整理、OCR 模型、语言与外观、开机启动、高级（旧版导入）。
import { useState, type FormEvent, type ReactNode } from "react";
import { Check, FolderUp, LoaderCircle } from "lucide-react";
import { api, type Settings } from "../api";
import { useT, type Lang } from "../i18n";
import { errText, fmtInt, useTheme, type Theme } from "../lib";
import { useSettings } from "../settings";
import { Card, LoadError, PageHeader, Segmented, Skeleton, Switch, useStatus, useToast } from "../ui";

export const IDLE_CHOICES = [3, 5, 10, 15, 30];

const Row = ({ children, first = false }: { children: ReactNode; first?: boolean }) => (
  <div className={`flex flex-wrap items-center justify-between gap-x-5 gap-y-3 ${first ? "pb-3.5" : "border-t border-rule py-3.5"}`}>{children}</div>
);

export default function SettingsPage() {
  const { t, lang, setLang } = useT();
  const [theme, setTheme] = useTheme();
  const st = useSettings();
  const { status } = useStatus();
  const s = st.data;

  return (
    <div>
      <PageHeader title={t("set.title")} sub={t("set.sub")} />
      {!s ? (
        st.error ? <LoadError text={t("set.loadFail", { e: errText(st.error, t("common.offline")) })} onRetry={() => st.reload()} /> : <Skeleton rows={4} />
      ) : (
        <div className="space-y-4">
          <LlmCard s={s} onSaved={st.setData} />

          <Card title={t("set.idle")} id="idle-h">
            <Row first>
              <div className="min-w-0 flex-1">
                <b><label htmlFor="idle-min">{t("set.idleMin")}</label></b>
                <p className="mt-0.5 text-[13px] text-graphite">{t("set.idleNote")}</p>
              </div>
              <select id="idle-min" name="idle_minutes" className="sr-field w-auto" value={String(s.idle_minutes)} disabled={st.saving}
                onChange={(e) => void st.save({ idle_minutes: Number(e.target.value) }, t("common.saved"))}>
                {[...new Set([...IDLE_CHOICES, s.idle_minutes])].sort((a, b) => a - b).map((n) => (
                  <option key={n} value={String(n)}>{t("set.minutes", { n })}</option>
                ))}
              </select>
            </Row>
            <Row>
              <div className="min-w-0 flex-1">
                <b>{t("set.queue")}</b>
                <p className="sr-num mt-0.5 text-[13px] text-graphite">
                  {status ? `${t("set.queueN", { n: fmtInt(status.pending_ocr) })} · ${status.idle_work === "running" ? t("set.queueRun") : t("set.queueIdle")}` : "—"}
                </p>
              </div>
            </Row>
          </Card>

          <Card title={<span id="lbl-ocr">{t("set.ocr")}</span>}>
            <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-labelledby="lbl-ocr">
              {(["tiny", "small"] as const).map((m) => (
                <label key={m} className={`flex cursor-pointer items-start gap-2.5 rounded-[11px] border px-3.5 py-3 ${s.ocr_model === m ? "border-cobalt shadow-[0_0_0_3px_var(--cobalt-soft)]" : "border-rule-strong"}`}>
                  <input type="radio" name="ocr" value={m} className="mt-[3px] accent-[var(--cobalt)]" checked={s.ocr_model === m} disabled={st.saving}
                    onChange={() => void st.save({ ocr_model: m }, t("common.saved"))} />
                  <span>
                    <b className="block text-sm font-semibold">{t(m === "tiny" ? "set.tiny" : "set.small")}</b>
                    <span className="block text-[13px] text-graphite">{t(m === "tiny" ? "set.tinyNote" : "set.smallNote")}</span>
                  </span>
                </label>
              ))}
            </div>
          </Card>

          <Card title={t("set.look")} id="look-h">
            <Row first>
              <b id="lbl-lang">{t("lang.label")}</b>
              <Segmented<Lang> labelledBy="lbl-lang" value={lang} onChange={setLang} options={[{ value: "zh", label: "中文" }, { value: "en", label: "English" }]} />
            </Row>
            <Row>
              <b id="lbl-theme">{t("theme.label")}</b>
              <Segmented<Theme> labelledBy="lbl-theme" value={theme} onChange={setTheme}
                options={(["system", "light", "dark"] as const).map((x) => ({ value: x, label: t(`theme.${x}`) }))} />
            </Row>
            <Row>
              <div className="min-w-0 flex-1">
                <b id="lbl-auto">{t("set.autostart")}</b>
                <p className="mt-0.5 text-[13px] text-graphite">{t("set.autostartNote")}</p>
              </div>
              <Switch on={s.autostart} labelledBy="lbl-auto" disabled={st.saving} onChange={(v) => void st.save({ autostart: v }, t("common.saved"))} />
            </Row>
          </Card>

          <details className="sr-card p-4 sm:p-5">
            <summary className="font-semibold">{t("set.adv")}</summary>
            <div className="mt-3.5 space-y-4">
              <div>
                <b>{t("set.dataDir")}</b>
                <p className="mt-1 rounded-lg border border-rule bg-well px-2.5 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">{s.data_dir}</p>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 border-t border-rule pt-3.5">
                <div className="min-w-0 flex-1">
                  <b>{t("set.import")}</b>
                  <p className="mt-0.5 text-[13px] text-graphite">{t("set.importNote")}</p>
                </div>
                <a className="sr-btn sr-btn-line sr-btn-sm" href="#/import"><FolderUp size={15} aria-hidden />{t("set.importGo")}</a>
              </div>
            </div>
          </details>
        </div>
      )}
    </div>
  );
}

function LlmCard({ s, onSaved }: { s: Settings; onSaved: (s: Settings) => void }) {
  const { t } = useT();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState(s.llm.model ?? "");
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const llm = s.llm;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!baseUrl.trim() || !model.trim()) {
      toast(t("set.needUrl"), "warn");
      return;
    }
    setBusy("save");
    try {
      const body: { base_url: string; model: string; api_key?: string } = { base_url: baseUrl.trim(), model: model.trim() };
      if (key) body.api_key = key; // 留空 = 保留原来的密钥
      onSaved(await api.saveLlm(body));
      setKey("");
      setShow(false);
      toast(t("set.llmSaved"));
    } catch (err) {
      toast(t("common.saveFail", { e: errText(err, t("common.offline")) }), "alarm");
    } finally {
      setBusy(null);
    }
  };
  const test = async () => {
    setBusy("test");
    try {
      const r = await api.testLlm();
      if (r.ok) toast(t("set.testOk", { s: ((r.latency_ms ?? 0) / 1000).toFixed(1), m: r.model ?? "—" }));
      else toast(t("set.testFail", { e: r.error ?? "—" }), "alarm");
    } catch (err) {
      toast(t("set.testFail", { e: errText(err, t("common.offline")) }), "alarm");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card title={t("set.llm")} id="llm-h">
      <Row first>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {llm.configured ? <span className="sr-tag sr-tag-ok"><Check size={13} aria-hidden />{t("set.llmOk")}</span>
              : <span className="sr-tag sr-tag-warn">{t("set.llmNo")}</span>}
            {llm.model && <span>{t("set.llmModel", { x: llm.model })}</span>}
            {llm.host && <span className="text-[13px] text-graphite [overflow-wrap:anywhere]">{t("set.llmHost", { x: llm.host })}</span>}
          </div>
          <p className="mt-1 text-[13px] text-graphite">{t("set.llmNote")}</p>
        </div>
        <div className="flex gap-2">
          {llm.configured && (
            <button className="sr-btn sr-btn-line sr-btn-sm" onClick={test} disabled={!!busy}>
              {busy === "test" && <LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" aria-hidden />}
              {busy === "test" ? t("set.testing") : t("set.test")}
            </button>
          )}
          <button className="sr-btn sr-btn-line sr-btn-sm" aria-expanded={open} aria-controls="llm-form" onClick={() => setOpen((o) => !o)}>
            {open ? t("set.collapse") : t("set.edit")}
          </button>
        </div>
      </Row>
      {open && (
        <form id="llm-form" className="grid gap-x-4 gap-y-3 border-t border-rule pt-3.5 sm:grid-cols-2" onSubmit={save}>
          <label className="flex min-w-0 flex-col gap-1.5 sm:col-span-2">
            <span className="text-[12.5px] font-medium text-graphite">{t("set.baseUrl")}</span>
            <input className="sr-field font-mono" name="base_url" type="url" required autoComplete="off" spellCheck={false}
              placeholder={llm.host ? `https://${llm.host}/v1` : "https://…/v1"} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </label>
          <label className="flex min-w-0 flex-col gap-1.5">
            <span className="text-[12.5px] font-medium text-graphite">{t("set.model")}</span>
            <input className="sr-field" name="model" required autoComplete="off" value={model} onChange={(e) => setModel(e.target.value)} />
          </label>
          <div className="flex min-w-0 flex-col gap-1.5">
            <label htmlFor="llm-key" className="text-[12.5px] font-medium text-graphite">{t("set.key")}</label>
            <div className="flex gap-1.5">
              <input id="llm-key" name="api_key" className="sr-field font-mono" type={show ? "text" : "password"} autoComplete="new-password" spellCheck={false}
                placeholder={llm.configured ? t("set.keyKeep") : ""} value={key} onChange={(e) => setKey(e.target.value)} />
              <button type="button" className="sr-btn sr-btn-line" aria-pressed={show} onClick={() => setShow((v) => !v)}>{show ? t("set.hide") : t("set.show")}</button>
            </div>
            <span className="text-xs text-faint">{t("set.keyHint")}</span>
          </div>
          <div className="flex flex-wrap gap-2 sm:col-span-2">
            <button className="sr-btn sr-btn-primary sr-btn-sm" type="submit" disabled={!!busy}>
              {busy === "save" && <LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" aria-hidden />}{t("common.save")}
            </button>
          </div>
        </form>
      )}
    </Card>
  );
}
