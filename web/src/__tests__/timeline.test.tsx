import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { rows } from "../pages/TimelinePage";
import { apps, at, entry, grey, mockFetch, timeline } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

/** 时间线最外层的行（不含灰色行点开后的小列表） */
const topRows = (list: HTMLElement) => Array.from(list.children) as HTMLElement[];

describe("timeline", () => {
  it("lists entries with name, time, dwell, lines, copies and thumb; no internal tags", async () => {
    mockFetch();
    renderAt("#/");
    const list = await screen.findByRole("list", { name: "时间线（从早到晚）" });
    const items = topRows(list);
    expect(items).toHaveLength(4);
    expect(items[0].textContent).toContain("Transformer 入门");
    expect(items[0].textContent).toContain("5 分钟");
    expect(items[0].textContent).toContain("存了 12 行");
    expect(items[0].textContent).not.toContain("为什么拍");
    expect(within(items[0]).getByRole("img").getAttribute("src")).toBe("/api/v3/shots/1/thumb?w=240");
    expect(items[2].textContent).toContain("复制过 1 段");
    expect(items[3].textContent).toContain("还在看");
    expect(items[3].textContent).not.toMatch(/没有新字|已有图|无图/);
    expect(within(items[0]).getByRole("link").getAttribute("href")).toBe("#/entry/1");
  });

  it("first visit shows the 3-step guide until 知道了", async () => {
    mockFetch();
    localStorage.removeItem("sr-welcomed");
    renderAt("#/");
    const card = await screen.findByTestId("welcome");
    expect(card.textContent).toContain("已经在录你的浏览器了");
    fireEvent.click(within(card).getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByTestId("welcome")).toBeNull());
    expect(localStorage.getItem("sr-welcomed")).toBe("1");
  });

  it("unlisted apps fold into one grey line; 以后录它 inside posts tier=record and reloads", async () => {
    let tl = timeline();
    const calls = mockFetch((c) => {
      if (c.path.startsWith("/api/v3/timeline")) return { body: tl };
      if (c.path === "/api/v3/apps" && c.method === "POST") {
        tl = timeline({ items: [grey({ tier: "record" })] });
        return { body: apps() };
      }
      return null;
    });
    renderAt("#/");
    const row = await screen.findByTestId("grey-row");
    expect(row.textContent).toContain("这段时间在用没录的软件：Code");
    fireEvent.click(within(row).getByRole("button", { name: "以后录它" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/apps" && c.method === "POST")?.body).toEqual({ process: "Code.exe", tier: "record" }));
    expect(calls.find((c) => c.method === "POST")!.headers["X-ScreenRecall"]).toBe("1");
    await waitFor(() => expect(toasts().join()).toContain("以后录 Code"));
    await waitFor(() => expect(screen.getByTestId("grey-row").textContent).toContain("已在名单里"));
    expect(within(screen.getByTestId("grey-row")).queryByRole("button")).toBeNull();
  });

  it("merges neighbouring unlisted spans, hides system programs, keeps paused / locked lines", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/timeline") ? {
      body: timeline({ items: [
        grey({ span_id: 1, app: "Code.exe" }), grey({ span_id: 2, app: "explorer.exe" }), grey({ span_id: 3, app: "WindowsTerminal.exe" }),
        grey({ span_id: 4, app: "Code.exe" }), grey({ span_id: 5, app: "WerFault.exe" }),
        grey({ span_id: 6, reason: "paused", app: null, tier: null }), grey({ span_id: 7, reason: "locked", app: null, tier: null, end_ms: null }),
        entry({ visit_id: 5, thumb: null, new_lines: 0 })] }),
    } : null));
    renderAt("#/");
    const rowsEl = await screen.findAllByTestId("grey-row");
    expect(rowsEl.map((r) => r.textContent)).toEqual([
      expect.stringContaining("在用没录的软件：Code、WindowsTerminal"), expect.stringContaining("暂停了（未录）"),
      expect.stringContaining("锁屏（未录）")]);
    expect(rowsEl[0].textContent).not.toMatch(/explorer|WerFault/);
    expect(within(rowsEl[1]).queryByRole("button")).toBeNull();
    expect(rowsEl[2].textContent).toContain("起");
  });

  it("an entry named by a program path shows the app name instead", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/timeline") ? {
      body: timeline({ items: [entry({ visit_id: 8, name: "C:\\Program Files\\x\\pythonw.exe",
        source: { ...entry().source, kind: "app_title", app: "WindowsTerminal.exe", url: null } })] }),
    } : null));
    renderAt("#/");
    const list = await screen.findByRole("list", { name: "时间线（从早到晚）" });
    expect(topRows(list)[0].textContent).toContain("WindowsTerminal");
    expect(topRows(list)[0].textContent).not.toContain("pythonw.exe");
  });

  it("background (pinned) entries show their span, not 0", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/timeline") ? {
      body: timeline({ items: [entry({ visit_id: 7, background: true, open: true, dwell_ms: 12 * 60_000 })] }),
    } : null));
    renderAt("#/");
    const list = await screen.findByRole("list", { name: "时间线（从早到晚）" });
    expect(topRows(list)[0].textContent).toContain("后台 · 12 分钟");
  });

  it("asks for the chosen day and inserts hour lines in order", async () => {
    const calls = mockFetch();
    renderAt("#/?day=2026-10-27");
    await screen.findByRole("list", { name: "时间线（从早到晚）" });
    expect(calls.some((c) => c.path === "/api/v3/timeline?day=2026-10-27")).toBe(true);
    const r = rows(timeline({ hours: [{ hour_ms: at(14), text: "下午在读 Transformer" }, { hour_ms: at(15), text: "晚些" }] }));
    expect(r.map((x) => x.kind)).toEqual(["hour", "item", "greys", "item", "item", "hour"]);
  });
});
