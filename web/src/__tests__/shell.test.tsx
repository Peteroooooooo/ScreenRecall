import { describe, expect, it } from "vitest";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { mockFetch, status } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("shell + status bar", () => {
  it("shows 在录 + 上次拍：N 秒前 and marks the current nav item", async () => {
    mockFetch();
    renderAt("#/");
    const pill = await screen.findByTestId("status-pill");
    await waitFor(() => expect(pill.textContent).toContain("在录"));
    expect(pill.textContent).toContain("上次拍：12 秒前");
    expect(pill.textContent).toContain("正在看：Transformer 入门");
    const navs = screen.getAllByRole("navigation", { name: "主导航" });
    for (const n of navs) {
      expect(within(n).getByRole("link", { name: /时间线/ }).getAttribute("aria-current")).toBe("page");
      expect(within(n).getAllByRole("link").map((a) => a.textContent)).toEqual(["时间线", "查找", "名单", "设置"]);
    }
  });

  it("nothing in front but a pinned window recording: 后台在录 N 个窗口", async () => {
    mockFetch((c) => (c.path === "/api/v3/status" ? { body: status({ current: null, background: 1 }) } : null));
    renderAt("#/");
    const pill = await screen.findByTestId("status-pill");
    await waitFor(() => expect(pill.textContent).toContain("后台在录 1 个窗口"));
    expect(pill.textContent).not.toContain("前台没有要录的窗口");
    expect(pill.textContent).toContain("上次拍：12 秒前");
  });

  it("paused: 已暂停 到 HH:MM, still shows the last snap", async () => {
    const now = Date.now();
    const until = new Date(now + 30 * 60_000);
    const hh = `${String(until.getHours()).padStart(2, "0")}:${String(until.getMinutes()).padStart(2, "0")}`;
    mockFetch((c) => (c.path === "/api/v3/status"
      ? { body: status({ state: "paused", paused_until_ms: until.getTime(), now_ms: now, last_snap_ms: now - 125_000, current: null }) } : null));
    renderAt("#/");
    const pill = await screen.findByTestId("status-pill");
    await waitFor(() => expect(pill.textContent).toContain("已暂停"));
    expect(pill.textContent).toMatch(new RegExp(`到(明天)? ${hh}`)); // 过了半夜是"到明天 00:05"
    expect(pill.textContent).toContain("上次拍：2 分钟前");
    expect(await screen.findByRole("button", { name: "继续录" })).toBeTruthy();
  });

  it("recorder not running and never snapped", async () => {
    mockFetch((c) => (c.path === "/api/v3/status" ? { body: status({ recorder_alive: false, state: "stopped", last_snap_ms: null }) } : null));
    renderAt("#/");
    const pill = await screen.findByTestId("status-pill");
    await waitFor(() => expect(pill.textContent).toContain("没在录"));
    expect(pill.textContent).toContain("还没拍过");
  });

  it("pause menu offers 5/15/30/60 minutes, posts {minutes}, then 继续录 resumes", async () => {
    let st = status();
    const calls = mockFetch((c) => {
      if (c.path === "/api/v3/status") return { body: st };
      if (c.path === "/api/v3/pause") {
        st = status({ state: "paused", paused_until_ms: Date.now() + 30 * 60_000 });
        return { body: st };
      }
      if (c.path === "/api/v3/resume") {
        st = status();
        return { body: st };
      }
      return null;
    });
    renderAt("#/");
    const btn = await screen.findByRole("button", { name: /暂停/ });
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(btn);
    const menu = screen.getByRole("menu", { name: "暂停多久" });
    expect(within(menu).getAllByRole("menuitem").map((x) => x.textContent)).toEqual(["5 分钟", "15 分钟", "30 分钟", "60 分钟"]);
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(btn);
    fireEvent.keyDown(btn, { key: "ArrowDown" });
    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "30 分钟" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/pause")?.body).toEqual({ minutes: 30 }));
    expect(calls.find((c) => c.path === "/api/v3/pause")!.headers["X-ScreenRecall"]).toBe("1");
    const resume = await screen.findByRole("button", { name: "继续录" });
    await waitFor(() => expect(toasts().join()).toContain("已暂停 30 分钟"));
    fireEvent.click(resume);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v3/resume" && c.method === "POST")).toBe(true));
    await screen.findByRole("button", { name: /^暂停/ });
  });

  it("Ctrl+K focuses the global search and submitting goes to #/search?q=", async () => {
    mockFetch();
    renderAt("#/");
    const input = await screen.findByRole("searchbox", { name: "查找看过的内容" });
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "矩阵" } });
    fireEvent.submit(input.closest("form")!);
    expect(decodeURIComponent(window.location.hash)).toBe("#/search?q=矩阵");
  });

  it("a 401 replaces the page with the tray message and stops polling", async () => {
    const calls = mockFetch(() => ({ status: 401, body: { error: "需要登录" } }));
    renderAt("#/");
    expect(await screen.findByText("请从托盘图标的“打开主页”进入")).toBeTruthy();
    expect(screen.queryByRole("navigation")).toBeNull();
    const n = calls.length;
    await act(() => new Promise((r) => setTimeout(r, 2300)));
    expect(calls.length).toBe(n);
  });

  it("offline status shows the connection banner after two failures", async () => {
    mockFetch((c) => {
      if (c.path === "/api/v3/status") throw new TypeError("Failed to fetch");
      return null;
    });
    renderAt("#/");
    expect(await screen.findByText("连不上后台服务", {}, { timeout: 3500 })).toBeTruthy();
    expect(screen.getByTestId("status-pill").textContent).toContain("连接中断");
  });
});
