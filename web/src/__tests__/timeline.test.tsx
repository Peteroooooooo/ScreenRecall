import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { rows } from "../pages/TimelinePage";
import { apps, at, entry, grey, mockFetch, timeline } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("timeline", () => {
  it("lists entries with name, time, dwell, lines, copies, thumb state and why snapped", async () => {
    mockFetch();
    renderAt("#/");
    const list = await screen.findByRole("list", { name: "时间线（从早到晚）" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[0].textContent).toContain("Transformer 入门");
    expect(items[0].textContent).toContain("5 分钟");
    expect(items[0].textContent).toContain("存了 12 行");
    expect(items[0].textContent).toContain("为什么拍：停下");
    expect(within(items[0]).getByRole("img").getAttribute("src")).toBe("/api/v3/shots/1/thumb?w=240");
    expect(items[2].textContent).toContain("复制过 1 段");
    expect(items[2].textContent).toContain("为什么拍：停下、复制");
    expect(items[3].textContent).toContain("还在看");
    expect(items[3].textContent).toContain("没有新字");
    expect(items[3].textContent).toContain("已有图");
    expect(within(items[0]).getByRole("link").getAttribute("href")).toBe("#/entry/1");
  });

  it("grey row 在 XX 里（未录） + 以后录它 posts tier=record and reloads", async () => {
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
    expect(row.textContent).toContain("在 Code 里（未录）");
    fireEvent.click(within(row).getByRole("button", { name: "以后录它" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/apps" && c.method === "POST")?.body).toEqual({ process: "Code.exe", tier: "record" }));
    expect(calls.find((c) => c.method === "POST")!.headers["X-ScreenRecall"]).toBe("1");
    await waitFor(() => expect(toasts().join()).toContain("以后录 Code"));
    await waitFor(() => expect(screen.getByTestId("grey-row").textContent).toContain("已在名单里"));
    expect(within(screen.getByTestId("grey-row")).queryByRole("button")).toBeNull();
  });

  it("paused / locked grey rows have no add button; no-shot entries say 无图", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/timeline") ? {
      body: timeline({ items: [grey({ span_id: 2, reason: "paused", app: null, tier: null }), grey({ span_id: 3, reason: "locked", app: null, tier: null, end_ms: null }),
        grey({ span_id: 4, reason: "paused", app: null, tier: null, end_ms: null }),
        entry({ visit_id: 5, thumb: null, new_lines: 0 })] }),
    } : null));
    renderAt("#/");
    const rowsEl = await screen.findAllByTestId("grey-row");
    // 结束了的暂停说"暂停了"，还在暂停的说"暂停中"
    expect(rowsEl.map((r) => r.textContent)).toEqual([expect.stringContaining("暂停了（未录）"), expect.stringContaining("锁屏（未录）"),
      expect.stringContaining("暂停中（未录）")]);
    for (const r of rowsEl) expect(within(r).queryByRole("button")).toBeNull();
    expect(rowsEl[1].textContent).toContain("起");
    expect(await screen.findByText("无图")).toBeTruthy();
  });

  it("background (pinned) entries show their span, not 0", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/timeline") ? {
      body: timeline({ items: [entry({ visit_id: 7, background: true, open: true, dwell_ms: 12 * 60_000 })] }),
    } : null));
    renderAt("#/");
    const list = await screen.findByRole("list", { name: "时间线（从早到晚）" });
    const text = within(list).getAllByRole("listitem")[0].textContent;
    expect(text).toContain("后台 · 12 分钟");
  });

  it("asks for the chosen day and inserts hour lines in order", async () => {
    const calls = mockFetch();
    renderAt("#/?day=2026-10-27");
    await screen.findByRole("list", { name: "时间线（从早到晚）" });
    expect(calls.some((c) => c.path === "/api/v3/timeline?day=2026-10-27")).toBe(true);
    const r = rows(timeline({ hours: [{ hour_ms: at(14), text: "下午在读 Transformer" }, { hour_ms: at(15), text: "晚些" }] }));
    expect(r.map((x) => x.kind)).toEqual(["hour", "item", "item", "item", "item", "hour"]);
  });
});
