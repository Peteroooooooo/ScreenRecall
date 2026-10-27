// 设置的读取与保存（录制控制、隐私、Agent、设置页共用）。保存只发改动的字段，成功后用服务端返回的整份设置刷新页面。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Settings, type SettingsPatch } from "./api";
import { useT } from "./i18n";
import { errText, useLoad } from "./lib";
import { useToast } from "./ui";

/** 要保存的字段；也可以是函数：按“上一次保存完成后”的最新设置算出改动（返回 null = 不用保存） */
export type Patch = SettingsPatch | ((cur: Settings) => SettingsPatch | null);

export function useSettings() {
  const { t } = useT();
  const toast = useToast();
  const st = useLoad((signal) => api.settings(signal), []);
  const [pending, setPending] = useState(0);
  const setData = st.setData;
  // 最新的设置与保存队列：连点两下（比如连删两个名单项）时，第二次要在第一次的结果上改，不能用旧快照把第一次的改动覆盖回去
  const latest = useRef<Settings | null>(null);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    latest.current = st.data;
  }, [st.data]);

  /** 保存（按调用顺序一个接一个发）；成功返回新设置并可给出提示，失败给出错误提示并返回 null */
  const save = useCallback((patch: Patch, ok?: string): Promise<Settings | null> => {
    setPending((n) => n + 1);
    const job = queue.current.then(async () => {
      try {
        const cur = latest.current;
        const body = typeof patch === "function" ? (cur ? patch(cur) : null) : patch;
        if (!body) return null;
        const s = await api.saveSettings(body);
        latest.current = s;
        setData(s);
        if (ok) toast(ok);
        return s;
      } catch (e) {
        toast(t("common.saveFail", { e: errText(e, t("common.offline")) }), "alarm");
        return null;
      } finally {
        setPending((n) => n - 1);
      }
    });
    queue.current = job;
    return job;
  }, [setData, t, toast]);
  return { ...st, save, saving: pending > 0 };
}

/** 名单里是否已有（不分大小写） */
export const hasItem = (list: string[], v: string) => list.some((x) => x.toLowerCase() === v.toLowerCase());
