// 设置的读取与保存（录制控制、隐私、Agent、设置页共用）。保存只发改动的字段，成功后用服务端返回的整份设置刷新页面。
import { useCallback, useState } from "react";
import { api, type Settings, type SettingsPatch } from "./api";
import { useT } from "./i18n";
import { errText, useLoad } from "./lib";
import { useToast } from "./ui";

export function useSettings() {
  const { t } = useT();
  const toast = useToast();
  const st = useLoad((signal) => api.settings(signal), []);
  const [saving, setSaving] = useState(false);
  const setData = st.setData;
  /** 保存；成功返回新设置并可给出提示，失败给出错误提示并返回 null */
  const save = useCallback(async (patch: SettingsPatch, ok?: string): Promise<Settings | null> => {
    setSaving(true);
    try {
      const s = await api.saveSettings(patch);
      setData(s);
      if (ok) toast(ok);
      return s;
    } catch (e) {
      toast(t("common.saveFail", { e: errText(e, t("common.offline")) }), "alarm");
      return null;
    } finally {
      setSaving(false);
    }
  }, [setData, t, toast]);
  return { ...st, save, saving };
}

/** 名单里是否已有（不分大小写） */
export const hasItem = (list: string[], v: string) => list.some((x) => x.toLowerCase() === v.toLowerCase());
