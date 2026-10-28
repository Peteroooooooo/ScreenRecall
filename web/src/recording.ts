// 录制状态的显示与暂停 / 继续操作（顶栏用）。
import { useCallback } from "react";
import { api, type Status } from "./api";
import { useT, type T } from "./i18n";
import { errText, fmtAgo, fmtClock, startOfToday } from "./lib";
import { useStatus, useToast, type DotTone } from "./ui";

/** 暂停菜单的几档（分钟）：方案“可以暂停 5–60 分钟” */
export const PAUSE_MINUTES = [5, 15, 30, 60];

/** 暂停到什么时候：今天之内显示钟点，跨天加“明天” */
export function resumeText(s: Status, t: T): string | null {
  if (s.paused_forever) return t("st.pausedForever");
  const until = s.paused_until_ms;
  if (until == null) return null;
  const tomorrow = until >= startOfToday(s.now_ms) + 86_400_000;
  return t(tomorrow ? "st.pausedUntilTomorrow" : "st.pausedUntil", { t: fmtClock(until) });
}

/** 暂停着：录制进程没在运行时 state 是 stopped，但暂停的设置还在（按钮要能“继续录”） */
export const isPaused = (s: Status | null | undefined) =>
  !!s && (s.state === "paused" || s.paused_forever || (s.paused_until_ms != null && s.paused_until_ms > s.now_ms));

export type RecView ={ tone: DotTone; label: string; detail: string | null; snap: string | null };

/** 顶栏：在录 / 已暂停（到 14:30）/ 锁屏 / 录制进程没在运行，加上“最后一次拍：N 秒前” */
export function recView(s: Status | null, lost: boolean, t: T): RecView {
  if (lost) return { tone: "off", label: t("st.lost"), detail: null, snap: null };
  if (!s) return { tone: "idle", label: t("st.connecting"), detail: null, snap: null };
  const ago = fmtAgo(s.last_snap_ms, s.now_ms);
  const snap = ago ? t("st.lastSnap", { x: ago }) : t("st.noSnap");
  if (s.state === "paused" || (isPaused(s) && s.recorder_alive)) return { tone: "idle", label: t("st.paused"), detail: resumeText(s, t), snap };
  if (!s.recorder_alive) return { tone: "off", label: t("st.dead"), detail: null, snap };
  if (s.state === "locked") return { tone: "idle", label: t("st.locked"), detail: null, snap };
  if (s.state === "stopped") return { tone: "off", label: t("st.dead"), detail: null, snap };
  const c = s.current;
  const bg = s.background ?? 0;
  const detail = c ? t("st.watching", { x: c.title || c.app }) : bg > 0 ? t("st.background", { n: bg }) : t("st.nothing");
  return { tone: "rec", label: t("st.recording"), detail, snap };
}

/** 暂停 / 继续：成功后立刻用返回的状态刷新顶栏，并给出轻提示 */
export function usePauseActions() {
  const { t } = useT();
  const toast = useToast();
  const { setStatus } = useStatus();
  const pause = useCallback(async (minutes: number) => {
    try {
      setStatus(await api.pause(minutes));
      toast(t("pause.done", { n: minutes }));
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
