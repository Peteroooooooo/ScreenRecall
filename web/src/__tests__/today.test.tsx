import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { localDate, addDays } from "../lib";
import { day, mockFetch, queryOf } from "../test/helpers";
import { renderAt } from "../test/render";

describe("today page", () => {
  it("shows the summary, stats and the timeline of pieces and gaps (newest first)", async () => {
    mockFetch();
    renderAt("#/");
    expect(await screen.findByText("示例任务 A（约 2 小时）、示例任务 B（约 1 小时）。")).toBeTruthy();
    expect(screen.getByText("记录了 3 小时 12 分")).toBeTruthy();
    expect(screen.getByText("2 个片段")).toBeTruthy();
    expect(screen.getByText("2 段没有记录")).toBeTruthy();
    const tl = screen.getByRole("list", { name: "时间线（最新在上）" });
    const rows = within(tl).getAllByRole("listitem");
    expect(rows).toHaveLength(4);
    const first = within(rows[0]).getByRole("link");
    expect(first.getAttribute("href")).toBe("#/piece/12");
    expect(within(rows[0]).getByText("示例任务 B")).toBeTruthy();
    expect(within(rows[0]).getByText("有截图等待识别")).toBeTruthy();
    expect(within(rows[1]).getByText("没有记录")).toBeTruthy();
    expect(within(rows[1]).getByText("已暂停")).toBeTruthy();
    expect(within(rows[1]).getByText("手动暂停")).toBeTruthy();
    expect(within(rows[3]).getByText("锁屏")).toBeTruthy();
  });

  it("asks for today's local date and switches days", async () => {
    const calls = mockFetch();
    renderAt("#/");
    await screen.findByRole("list", { name: "时间线（最新在上）" });
    const today = localDate(new Date());
    expect(queryOf(calls.find((c) => c.path.startsWith("/api/v2/day"))!.path).get("date")).toBe(today);
    expect((screen.getByRole("button", { name: "后一天" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "前一天" }));
    const y = addDays(today, -1);
    expect(window.location.hash).toBe(`#/?date=${y}`);
    await waitFor(() => expect(calls.some((c) => c.path === `/api/v2/day?date=${y}`)).toBe(true));
    expect(await screen.findByRole("heading", { level: 1, name: "昨天" })).toBeTruthy();
  });

  it("empty day shows the empty state", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v2/day") ? { body: day({ date: queryOf(c.path).get("date")!, items: [], summary: null }) } : null));
    renderAt("#/?date=2026-01-02");
    expect(await screen.findByText("这一天没有记录")).toBeTruthy();
  });

  it("an error shows the server message and retries", async () => {
    let fail = true;
    mockFetch((c) => (c.path.startsWith("/api/v2/day") && fail ? { status: 500, body: { error: "数据库忙" } } : null));
    renderAt("#/");
    expect(await screen.findByText(/数据库忙/)).toBeTruthy();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("list", { name: "时间线（最新在上）" })).toBeTruthy();
  });
});
