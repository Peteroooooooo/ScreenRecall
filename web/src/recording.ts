// 录制状态的显示与暂停 / 继续操作（顶栏和录制控制页共用）。
import { useCallback } from "react";
import { api, type Status } from "./api";
import { useT, type T } from "./i18n";
import { errText, fmtClock, startOfToday } from "./lib";
import { useStatus, useToast, type DotTone } from "./ui";

export type PauseKind = "m15" | "h1" | "tomorrow";
export const PAUSE_KINDS: PauseKind[] = ["m15", "h1", "tomorrow"];
const PAUSE_BODY: Record<PauseKind, { minutes: number } | { until: "tomorrow" }> = {
  m15: { minutes: 15 },
  h1: { minutes: 60 },
  tomorrow: { until: "tomorrow" },
};

/** 暂停到什么时候：今天之内显示钟点，跨天显示“明天继续” */
export function resumeText(until: number | null, t: T, now = Date.now()): string | null {
  if (until == null) return null;
  if (until >= startOfToday(now) + 86_400_000) return t("st.pausedTomorrow");
  return t("st.pausedUntil", { t: fmtClock(until) });
}

export type RecView = { tone: DotTone; label: string; detail: string | null; state: "recording" | "paused" | "stopped" | "dead" | "lost" | "unknown" };

export function recView(s: Status | null, lost: boolean, t: T): RecView {
  if (lost) return { tone: "off", label: t("st.lost"), detail: null, state: "lost" };
  if (!s) return { tone: "idle", label: t("st.connecting"), detail: null, state: "unknown" };
  if (s.recording === "paused") return { tone: "idle", label: t("st.paused"), detail: resumeText(s.pause_until_ms, t), state: "paused" };
  if (!s.recorder_alive) return { tone: "off", label: t("st.dead"), detail: null, state: "dead" };
  if (s.recording === "stopped") return { tone: "idle", label: t("st.stopped"), detail: null, state: "stopped" };
  const c = s.current;
  const detail = !c ? null : c.excluded ? t("st.excludedFg") : (c.title || c.app) ? t("st.watching", { x: c.title || c.app || "" }) : null;
  return { tone: "rec", label: t("st.recording"), detail, state: "recording" };
}

/** 暂停 / 继续：成功后立刻用返回的状态刷新顶栏，并给出轻提示 */
export function usePauseActions() {
  const { t } = useT();
  const toast = useToast();
  const { setStatus } = useStatus();
  const pause = useCallback(async (k: PauseKind) => {
    try {
      setStatus(await api.pause(PAUSE_BODY[k]));
      toast(t("pause.done", { x: t(`pause.${k}`) }));
    } catch (e) {
      toast(t("pause.fail", { e: errText(e, t("common.offline")) }), "alarm");
    }
  }, [setStatus, t, toast]);
  const resume = useCallback(async () => {
    try {
      setStatus(await api.resume());
      toast(t("pause.resumed"));
    } catch (e) {
      toast(t("pause.fail", { e: errText(e, t("common.offline")) }), "alarm");
    }
  }, [setStatus, t, toast]);
  return { pause, resume };
}
