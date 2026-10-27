import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { mockFetch, settings } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

const deleted = { body: { deleted: { lines: 30, frames: 2, segments: 1 } } };

describe("privacy page", () => {
  it("quick delete asks first; cancel sends nothing, confirm posts recent_minutes", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/delete" ? deleted : null));
    renderAt("#/privacy");
    fireEvent.click(await screen.findByRole("button", { name: "删除最近 5 分钟" }));
    let dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByRole("heading", { name: "删除最近 5 分钟？" })).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.some((c) => c.path === "/api/v2/delete")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "删除最近 1 小时" }));
    dlg = await screen.findByRole("dialog");
    fireEvent.click(within(dlg).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/delete")?.body).toEqual({ recent_minutes: 60 }));
    expect(calls.find((c) => c.path === "/api/v2/delete")!.headers["X-ScreenRecall"]).toBe("1");
    await waitFor(() => expect(toasts().join()).toContain("已删除：30 行文字、2 张截图"));
  });

  it("today deletes from local midnight to now", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/delete" ? deleted : null));
    renderAt("#/privacy");
    fireEvent.click(await screen.findByRole("button", { name: "删除今天全部" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v2/delete")).toBe(true));
    const b = calls.find((c) => c.path === "/api/v2/delete")!.body as { from_ms: number; to_ms: number };
    const d = new Date();
    expect(b.from_ms).toBe(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime());
    expect(Math.abs(b.to_ms - Date.now())).toBeLessThan(10_000);
  });

  it("a time range must be ordered and is sent as UTC ms", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/delete" ? deleted : null));
    renderAt("#/privacy");
    const from = await screen.findByLabelText("从");
    const to = screen.getByLabelText("到");
    fireEvent.change(from, { target: { value: "2026-10-27T10:00" } });
    fireEvent.change(to, { target: { value: "2026-10-27T09:00" } });
    expect(screen.getByText("“到”要晚于“从”")).toBeTruthy();
    expect((screen.getByRole("button", { name: "删除这段时间" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(to, { target: { value: "2026-10-27T11:30" } });
    fireEvent.click(screen.getByRole("button", { name: "删除这段时间" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/delete")?.body).toEqual({
      from_ms: new Date(2026, 9, 27, 10, 0).getTime(), to_ms: new Date(2026, 9, 27, 11, 30).getTime(),
    }));
  });

  it("delete by app, then add it to the exclusions", async () => {
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/delete") return deleted;
      if (c.path === "/api/v2/settings" && c.method === "POST") return { body: settings(c.body as object) };
      return null;
    });
    renderAt("#/privacy");
    const sel = await screen.findByRole("combobox", { name: "应用" });
    await waitFor(() => expect(within(sel).getAllByRole("option").length).toBe(3));
    fireEvent.change(sel, { target: { value: "SampleChat.exe" } });
    fireEvent.click(screen.getByRole("button", { name: "删除这个应用的全部记录" }));
    const dlg = await screen.findByRole("dialog");
    expect(dlg.textContent).toContain("会删除“SampleChat”的全部文字和截图");
    expect(dlg.textContent).toContain("之后也不再记录它");
    fireEvent.click(within(dlg).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/delete")?.body).toEqual({ app: "SampleChat.exe" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/settings" && c.method === "POST")?.body)
      .toEqual({ excluded_processes: ["SamplePasswords.exe", "SampleChat.exe"] }));
    // 先排除、再删除：两步之间不会又记下这个应用
    const ex = calls.findIndex((c) => c.path === "/api/v2/settings" && c.method === "POST");
    const del = calls.findIndex((c) => c.path === "/api/v2/delete");
    expect(ex).toBeLessThan(del);
  });

  it("delete by app still deletes when adding the exclusion fails", async () => {
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/delete") return deleted;
      if (c.path === "/api/v2/settings" && c.method === "POST") return { status: 500, body: { error: "写不进设置" } };
      return null;
    });
    renderAt("#/privacy");
    const sel = await screen.findByRole("combobox", { name: "应用" });
    await waitFor(() => expect(within(sel).getAllByRole("option").length).toBe(3));
    fireEvent.change(sel, { target: { value: "SampleChat.exe" } });
    fireEvent.click(screen.getByRole("button", { name: "删除这个应用的全部记录" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/delete")?.body).toEqual({ app: "SampleChat.exe" }));
    await waitFor(() => expect(toasts().join()).toContain("写不进设置"));
  });

  it("shows where the data lives and how big it is", async () => {
    mockFetch();
    renderAt("#/privacy");
    expect(await screen.findByText("C:\\Users\\example\\AppData\\Local\\ScreenRecall\\data")).toBeTruthy();
    expect(screen.getByText("截图 114 MB")).toBeTruthy();
    expect(screen.getByText(/41 张截图 · 1,204 行文字/)).toBeTruthy();
  });

  it("a failed delete shows the error", async () => {
    mockFetch((c) => (c.path === "/api/v2/delete" ? { status: 500, body: { error: "磁盘忙" } } : null));
    renderAt("#/privacy");
    fireEvent.click(await screen.findByRole("button", { name: "删除最近 5 分钟" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(toasts().join()).toContain("删除失败：磁盘忙"));
  });
});
