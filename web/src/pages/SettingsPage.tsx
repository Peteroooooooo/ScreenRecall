// 设置：语言与外观、开机自启、Agent（连上了没有 + 一键连接）、删最近 N 分钟；
// "高级（开发用）"里收着大模型有没有配、能调的数字（只读，在 config 的 [v3] 里改）、数据目录。
import { useState, type ReactNode } from "react";
import { Check, Trash2 } from "lucide-react";
import { api } from "../api";
import { hasKey, useT, type Lang, type T } from "../i18n";
import { errText, fmtInt, useTheme, type Theme } from "../lib";
import { useSettings } from "../settings";
import { Card, ConfirmDialog, LoadError, PageHeader, Segmented, Skeleton, Switch, useToast, type ConfirmSpec } from "../ui";
import { AgentConnect } from "./AgentPage";

/** 删最近多少分钟 */
export const DELETE_CHOICES = [5, 15, 60, 24 * 60];

const Row = ({ children, first = false }: { children: ReactNode; first?: boolean }) => (
  <div className={`flex flex-wrap items-center justify-between gap-x-5 gap-y-3 ${first ? "pb-3.5" : "border-t border-rule py-3.5"}`}>{children}</div>
);

/** 数字按单位显示：*_ms → 秒 / 分钟，*_gb → GB，*_ratio / *_area → 百分比，*_days → 天 */
export function fmtNumber(key: string, v: number, t: T): string {
  if (key.endsWith("_ms")) return v >= 60_000 && v % 60_000 === 0 ? t("unit.min", { n: v / 60_000 }) : t("unit.s", { n: +(v / 1000).toFixed(2) });
  if (key.endsWith("_gb")) return `${v} GB`;
  if (key.endsWith("_ratio") || key.endsWith("_area")) return `${Math.round(v * 100)}%`;
  if (key.endsWith("_days")) return t("unit.days", { n: v });
  if (key === "idle_s") return t("unit.min", { n: +(v / 60).toFixed(1) });
  return fmtInt(v);
}

export default function SettingsPage() {
  const { t, lang, setLang } = useT();
  const [theme, setTheme] = useTheme();
  const st = useSettings();
  const s = st.data;

  return (
    <div>
      <PageHeader title={t("set.title")} sub={t("set.sub")} />
      {!s ? (
        st.error ? <LoadError text={t("set.loadFail", { e: errText(st.error, t("common.offline")) })} onRetry={() => st.reload()} /> : <Skeleton rows={4} />
      ) : (
        <div className="space-y-4">
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

          <AgentConnect enabled={s.agent_enabled} saving={st.saving}
            onToggle={(v) => void st.save({ agent_enabled: v }, v ? t("agent.enabled") : t("agent.disabled"))} />

          <DeleteCard />

          <details className="space-y-4">
          <summary className="cursor-pointer px-1 text-[15px] font-semibold text-graphite">{t("set.advanced")}</summary>
          <Card title={t("set.llm")} id="llm-h" className="mt-4">
            <div className="flex flex-wrap items-center gap-2">
              {s.llm.configured ? <span className="sr-tag sr-tag-ok"><Check size={13} aria-hidden />{t("set.llmOk")}</span>
                : <span className="sr-tag sr-tag-warn">{t("set.llmNo")}</span>}
              {s.llm.model && <span>{t("set.llmModel", { x: s.llm.model })}</span>}
            </div>
            <p className="mt-1.5 text-[13px] text-graphite">{t(s.llm.configured ? "set.llmNote" : "set.llmNoNote")}</p>
          </Card>

          <Card title={t("set.numbers")} id="num-h">
            <p className="mb-3 text-[13px] text-graphite">{t("set.numbersNote")}</p>
            <dl className="grid gap-x-6 sm:grid-cols-2" aria-labelledby="num-h">
              {Object.entries(s.numbers).map(([k, v]) => {
                const lk = `num.${k}`;
                return (
                  <div key={k} className="flex min-w-0 items-baseline justify-between gap-3 border-t border-rule py-2 text-[13.5px]">
                    <dt className="min-w-0">
                      <span className="block">{hasKey(lk) ? t(lk) : k}</span>
                      <code className="font-mono text-[11.5px] text-faint">{k}</code>
                    </dt>
                    <dd className="sr-num shrink-0 font-medium">{fmtNumber(k, v, t)}</dd>
                  </div>
                );
              })}
            </dl>
          </Card>

          <Card title={t("set.dataDir")} id="dir-h">
            <p className="rounded-lg border border-rule bg-well px-2.5 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">{s.data_dir}</p>
            <p className="mt-2 text-[13px] text-graphite">{t("set.dataDirNote")}</p>
          </Card>
          </details>
        </div>
      )}
    </div>
  );
}

function DeleteCard() {
  const { t } = useT();
  const toast = useToast();
  const [minutes, setMinutes] = useState(DELETE_CHOICES[1]);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const label = (n: number) => (n >= 60 ? t("set.delHours", { n: n / 60 }) : t("set.delMinutes", { n }));
  const ask = () => setConfirm({
    title: t("set.delTitle", { x: label(minutes) }), body: t("set.delBody"),
    run: async () => {
      try {
        await api.remove({ minutes });
        toast(t("set.delDone", { x: label(minutes) }));
      } catch (e) {
        toast(t("set.delFail", { e: errText(e, t("common.offline")) }), "alarm");
      }
    },
  });
  return (
    <Card title={t("set.delete")} id="del-h">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="del-min" className="text-[13.5px]">{t("set.delLabel")}</label>
        <select id="del-min" name="minutes" className="sr-field w-auto" value={String(minutes)} onChange={(e) => setMinutes(Number(e.target.value))}>
          {DELETE_CHOICES.map((n) => <option key={n} value={String(n)}>{label(n)}</option>)}
        </select>
        <button className="sr-btn sr-btn-danger sr-btn-sm" onClick={ask}><Trash2 size={15} aria-hidden />{t("common.delete")}</button>
      </div>
      <p className="mt-2 text-[13px] text-graphite">{t("set.delNote")}</p>
      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
    </Card>
  );
}
