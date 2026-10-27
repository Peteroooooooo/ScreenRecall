// 空闲整理、保留期、马上写概括、删除提示：设置页、隐私页、片段页、今天页。
import { describe, expect, it } from "vitest";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { SettingsPatch } from "../api";
import { day, mockFetch, piece, settings, status, type Call } from "../test/helpers";
import { renderAt, toasts } from "../test/render";
import { shorter } from "../pages/PrivacyPage";

const posts = (calls: Call[], p: string) => calls.filter((c) => c.method === "POST" && c.path === p);

describe("settings: idle processing", () => {
  it("shows the queue counts and starts processing now", async () => {
    let running = false;
    const calls = mockFetch((c) => {
      const s = () => status({ idle_work: running ? "running" : "idle", queue: { ocr: 3, vectors: 120, summaries: 2 } });
      if (c.path === "/api/v2/status") return { body: s() };
      if (c.path === "/api/v2/idle/run") {
        running = true;
        return { body: s() };
      }
      return null;
    });
    renderAt("#/settings");
    expect(await screen.findByText("3 张截图等待识别 · 120 行等待算向量 · 2 个片段等待概括 · 没在整理")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "现在就整理" }));
    await waitFor(() => expect(posts(calls, "/api/v2/idle/run")).toHaveLength(1));
    expect(posts(calls, "/api/v2/idle/run")[0].headers["X-ScreenRecall"]).toBe("1");
    await waitFor(() => expect(toasts().join()).toContain("已开始整理"));
    const btn = await screen.findByRole("button", { name: "正在整理" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it("reports a failed start", async () => {
    mockFetch((c) => (c.path === "/api/v2/idle/run" ? { status: 503, body: { error: "空闲整理进程没能启动" } } : null));
    renderAt("#/settings");
    fireEvent.click(await screen.findByRole("button", { name: "现在就整理" }));
    await waitFor(() => expect(toasts().join()).toContain("没能开始整理：空闲整理进程没能启动"));
  });
});

describe("privacy: storage and retention", () => {
  it("opens the data folder", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/storage/open" ? { body: { ok: true } } : null));
    renderAt("#/privacy");
    fireEvent.click(await screen.findByRole("button", { name: "打开文件夹" }));
    await waitFor(() => expect(posts(calls, "/api/v2/storage/open")).toHaveLength(1));
    expect(posts(calls, "/api/v2/storage/open")[0].body).toEqual({});
  });

  it("saves a longer retention without asking, a shorter one after confirming", async () => {
    let cur = settings();
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/settings" && c.method === "POST") {
        cur = { ...cur, ...(c.body as SettingsPatch) };
        return { body: cur };
      }
      if (c.path === "/api/v2/settings") return { body: cur };
      return null;
    });
    renderAt("#/privacy");
    const save = () => screen.getByRole("button", { name: "保存" });
    fireEvent.change(await screen.findByLabelText("截图"), { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("空间上限"), { target: { value: "20" } });
    fireEvent.click(save());
    await waitFor(() => expect(posts(calls, "/api/v2/settings").at(-1)?.body).toEqual({ image_days: 90, storage_cap_gb: 20 }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(toasts().join()).toContain("已保存：空闲整理时按新的保留期清理"));
    expect(await screen.findByText(/已用 .*，上限 20(\.0)? GB/)).toBeTruthy();

    // 文字从 90 天改成 1 年：更长，不问；再改回 90 天：更短，先确认，取消就不发
    fireEvent.change(screen.getByLabelText("文字"), { target: { value: "365" } });
    fireEvent.click(save());
    await waitFor(() => expect(posts(calls, "/api/v2/settings").at(-1)?.body).toEqual({ text_days: 365 }));
    fireEvent.change(screen.getByLabelText("文字"), { target: { value: "90" } });
    fireEvent.click(save());
    let dlg = await screen.findByRole("dialog");
    expect(dlg.textContent).toContain("超过 90 天的文字记录");
    fireEvent.click(within(dlg).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(posts(calls, "/api/v2/settings")).toHaveLength(2);
    fireEvent.click(save());
    dlg = await screen.findByRole("dialog");
    fireEvent.click(within(dlg).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(posts(calls, "/api/v2/settings").at(-1)?.body).toEqual({ text_days: 90 }));
  });

  it("shorter() treats 0 as keep forever", () => {
    expect(shorter(0, 90)).toBe(true);
    expect(shorter(90, 0)).toBe(false);
    expect(shorter(90, 30)).toBe(true);
    expect(shorter(30, 90)).toBe(false);
  });

  it("explains re-recording and can pause 15 minutes before deleting", async () => {
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/delete") return { body: { deleted: { lines: 1, frames: 0 } } };
      if (c.path === "/api/v2/pause") return { body: status({ recording: "paused", pause_until_ms: Date.now() + 900_000 }) };
      return null;
    });
    renderAt("#/privacy");
    expect(await screen.findByText(/屏幕上还开着的内容会被重新记下来/)).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "删除时顺便暂停录制 15 分钟" }));
    fireEvent.click(screen.getByRole("button", { name: "删除最近 5 分钟" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(posts(calls, "/api/v2/delete")).toHaveLength(1));
    const iPause = calls.findIndex((c) => c.path === "/api/v2/pause");
    const iDel = calls.findIndex((c) => c.path === "/api/v2/delete");
    expect(iPause).toBeGreaterThanOrEqual(0);
    expect(iPause).toBeLessThan(iDel); // 先暂停再删
    expect(calls[iPause].body).toEqual({ minutes: 15 });
  });

  it("does not pause when the box is unchecked", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/delete" ? { body: { deleted: { lines: 1 } } } : null));
    renderAt("#/privacy");
    fireEvent.click(await screen.findByRole("button", { name: "删除最近 5 分钟" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(posts(calls, "/api/v2/delete")).toHaveLength(1));
    expect(calls.some((c) => c.path === "/api/v2/pause")).toBe(false);
  });
});

describe("piece: regenerate now", () => {
  it("shows 生成中 and refreshes when the revision changes", async () => {
    let state: "ok" | "generating" = "ok";
    let rev = 1;
    let text = "示例概括：旧的。";
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/status") return { body: status({ revision: rev }) };
      if (/^\/api\/v2\/pieces\/11$/.test(c.path)) return { body: piece({ summary_state: state, summary: { text, status: "ok", model: "m" } }) };
      if (c.path === "/api/v2/pieces/11/summarize") {
        state = "generating";
        return { body: { queued: true, summary_state: "generating" } };
      }
      return null;
    });
    renderAt("#/piece/11");
    fireEvent.click(await screen.findByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(posts(calls, "/api/v2/pieces/11/summarize")).toHaveLength(1));
    await waitFor(() => expect(toasts().join()).toContain("正在生成这一段的概括"));
    const btn = await screen.findByRole("button", { name: "生成中" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    // 后台写好了：revision 变了，页面自己刷新
    state = "ok";
    text = "示例概括：新的。";
    rev = 2;
    await act(() => new Promise((r) => setTimeout(r, 2300)));
    expect(await screen.findByText("示例概括：新的。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeTruthy();
  });

  it("without a model it only queues", async () => {
    mockFetch((c) => (c.path === "/api/v2/pieces/11/summarize" ? { body: { queued: true, summary_state: "queued" } } : null));
    renderAt("#/piece/11");
    fireEvent.click(await screen.findByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(toasts().join()).toContain("已排队：电脑空闲时会重写这段的概括"));
  });
});

describe("today: day summary refresh", () => {
  it("after a delete the summary is stale, says so and can be regenerated", async () => {
    let d = day({ summary: { text: "", status: "stale", model: null }, summary_state: "stale", summary_refresh: true });
    const calls = mockFetch((c) => {
      if (c.path.startsWith("/api/v2/day?") || c.path === "/api/v2/day") return { body: d };
      if (c.path === "/api/v2/day/summarize") {
        d = { ...d, summary_state: "generating" };
        return { body: { date: d.date, summary_state: "generating" } };
      }
      return null;
    });
    renderAt("#/");
    expect(await screen.findByText("删除过部分记录，这一天的概括已清空，需要重新生成。")).toBeTruthy();
    expect(screen.getByText("待更新")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(posts(calls, "/api/v2/day/summarize")).toHaveLength(1));
    expect(posts(calls, "/api/v2/day/summarize")[0].body).toEqual({ date: d.date });
    expect(await screen.findByText("正在生成当天概括…")).toBeTruthy();
    expect((screen.getByRole("button", { name: "生成中…" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("no refresh button without a model", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v2/day") ? { body: day({ summary_refresh: false }) } : null));
    renderAt("#/");
    await screen.findByText("示例任务 A（约 2 小时）、示例任务 B（约 1 小时）。");
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
  });

  it("a failed refresh says so", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v2/day") ? { body: day({ summary: null, summary_state: "failed", summary_refresh: true }) } : null));
    renderAt("#/");
    expect(await screen.findByText("这次没生成出来。可以检查设置里的大模型，再试一次。")).toBeTruthy();
  });
});
