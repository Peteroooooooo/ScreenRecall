import { describe, expect, it } from "vitest";
import { act, fireEvent, renderHook, screen, waitFor, within } from "@testing-library/react";
import { usePoll } from "../lib";
import { mockFetch, status } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("shell", () => {
  it("shows the recording pill and marks the current nav item", async () => {
    mockFetch();
    renderAt("#/");
    const pill = await screen.findByTestId("status-pill");
    await waitFor(() => expect(pill.textContent).toContain("录制中"));
    expect(pill.textContent).toContain("正在看：示例文档.txt");
    const navs = screen.getAllByRole("navigation", { name: "主导航" });
    for (const n of navs) expect(within(n).getByRole("link", { name: /今天/ }).getAttribute("aria-current")).toBe("page");
    // 旧版页面不在导航里
    expect(screen.queryByRole("link", { name: /收藏/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /导入/ })).toBeNull();
  });

  it("pause dropdown: opens with arrow keys, Esc closes, picking 15 分钟 posts and the button becomes 继续录制", async () => {
    let st = status();
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/status") return { body: st };
      if (c.path === "/api/v2/pause") {
        st = status({ recording: "paused", pause_until_ms: Date.now() + 15 * 60_000 });
        return { body: st };
      }
      if (c.path === "/api/v2/resume") {
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
    const items = within(menu).getAllByRole("menuitem").map((x) => x.textContent);
    expect(items).toEqual(["15 分钟", "1 小时", "到明天"]);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(btn);

    fireEvent.keyDown(btn, { key: "ArrowDown" });
    const m2 = screen.getByRole("menu");
    await waitFor(() => expect(document.activeElement?.textContent).toBe("15 分钟"));
    fireEvent.keyDown(m2, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("1 小时");
    fireEvent.click(within(m2).getByRole("menuitem", { name: "15 分钟" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v2/pause")).toBe(true));
    const pc = calls.find((c) => c.path === "/api/v2/pause")!;
    expect(pc.body).toEqual({ minutes: 15 });
    expect(pc.headers["X-ScreenRecall"]).toBe("1");

    const resume = await screen.findByRole("button", { name: "继续录制" });
    expect(screen.getByTestId("status-pill").textContent).toContain("已暂停");
    await waitFor(() => expect(toasts().join()).toContain("已暂停 15 分钟"));
    fireEvent.click(resume);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v2/resume" && c.method === "POST")).toBe(true));
    await screen.findByRole("button", { name: /^暂停/ });
  });

  it("到明天 sends {until: 'tomorrow'}", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/pause" ? { body: status({ recording: "paused" }) } : null));
    renderAt("#/");
    const btn = await screen.findByRole("button", { name: /暂停/ });
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(btn);
    fireEvent.click(screen.getByRole("menuitem", { name: "到明天" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/pause")?.body).toEqual({ until: "tomorrow" }));
  });

  it("Ctrl+K focuses the global search and submitting goes to #/search?q=", async () => {
    mockFetch();
    renderAt("#/");
    const input = await screen.findByRole("searchbox", { name: "搜索屏幕记忆" });
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "示例词" } });
    fireEvent.submit(input.closest("form")!);
    expect(decodeURIComponent(window.location.hash)).toBe("#/search?q=示例词");
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

  it("theme button cycles system -> light -> dark and stores the choice", async () => {
    mockFetch();
    renderAt("#/");
    const b = await screen.findByRole("button", { name: /主题：跟随系统/ });
    fireEvent.click(b);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    fireEvent.click(screen.getByRole("button", { name: /主题：浅色/ }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem("sr-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: /主题：深色/ }));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("offline status shows the connection banner after two failures", async () => {
    mockFetch((c) => {
      if (c.path === "/api/v2/status") throw new TypeError("Failed to fetch");
      return null;
    });
    renderAt("#/");
    expect(await screen.findByText("连不上后台服务", {}, { timeout: 3500 })).toBeTruthy();
    expect(screen.getByTestId("status-pill").textContent).toContain("连接中断");
  });
});

describe("usePoll", () => {
  it("drops a poll result that was already in flight when the status was set directly (no flash back to the old state)", async () => {
    let release: (v: string) => void = () => {};
    let n = 0;
    const fn = () => {
      n += 1;
      return n === 1 ? Promise.resolve("recording") : new Promise<string>((r) => { release = r; });
    };
    const { result } = renderHook(() => usePoll(fn, 50));
    await waitFor(() => expect(result.current.data).toBe("recording"));
    await waitFor(() => expect(n).toBe(2)); // 第二次轮询已发出、还没回来
    act(() => result.current.setData("paused")); // 暂停接口先回来了
    await act(async () => release("recording")); // 旧的轮询结果后到
    expect(result.current.data).toBe("paused");
    await waitFor(() => expect(n).toBeGreaterThanOrEqual(3));
  });
});
