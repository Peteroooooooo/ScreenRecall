import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { entryDetail, mockFetch, settings, source } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("settings", () => {
  it("shows numbers read-only with the config note, LLM state, data dir", async () => {
    mockFetch();
    renderAt("#/settings");
    expect(await screen.findByText("停下多久算停下")).toBeTruthy();
    expect(screen.getByText("still_ms")).toBeTruthy();
    const values = Array.from(document.querySelectorAll("dd")).map((x) => x.textContent);
    expect(values).toContain("2 秒");
    expect(values).toContain("5 分钟");                    // merge_ms
    expect(values).toContain("30%");                       // ocr_min_body_ratio
    expect(values).toContain("30 天");                     // image_days
    expect(screen.getByText(/\[v3\]/)).toBeTruthy();
    expect(screen.getByText("没有配置")).toBeTruthy();
    expect(screen.getByText("C:\\example\\data-v3")).toBeTruthy();
    expect(screen.queryByRole("spinbutton")).toBeNull();     // 只读，不能在页面上改
  });

  it("toggles the agent switch and autostart; shows the connection configs", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v3/settings" && c.method === "POST"
      ? { body: settings({ ...(c.body as object) }) } : null));
    renderAt("#/settings");
    const sw = await screen.findByRole("switch", { name: "允许 Claude Code / Codex 查这里的记录" });
    fireEvent.click(sw);
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ agent_enabled: false }));
    expect(await screen.findByText("现在 Agent 查不到任何记录。")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "开机自动启动" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toContainEqual({ autostart: true }));
    expect(await screen.findByText("http://127.0.0.1:8611/mcp")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Claude Code" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("list_copied")).toBeTruthy();
  });

  it("deletes the last N minutes after confirming", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v3/delete" ? { body: { visits: 2 } } : null));
    renderAt("#/settings");
    fireEvent.change(await screen.findByRole("combobox", { name: "删最近" }), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/delete")?.body).toEqual({ minutes: 60 }));
    await waitFor(() => expect(toasts().join()).toContain("已删掉最近 1 小时的记录"));
  });
});

describe("entry page", () => {
  it("shows lines with new ones marked, the http link in a new tab and screenshots", async () => {
    mockFetch();
    renderAt("#/entry/1");
    expect(await screen.findByRole("heading", { level: 1, name: "Transformer 入门" })).toBeTruthy();
    const link = screen.getByRole("link", { name: /blog\.example\.com\/transformer-intro/ });
    expect(link.getAttribute("target")).toBe("_blank");
    const lines = screen.getByRole("list", { name: /这一条看到的字/ });
    const li = within(lines).getAllByRole("listitem");
    expect(within(li[0]).getByText("新")).toBeTruthy();
    expect(within(li[1]).queryByText("新")).toBeNull();
    expect(screen.getByRole("button", { name: /看 14:00 的截图/ })).toBeTruthy();
  });

  it("non-http urls are not links; files get 在文件夹中显示 which posts reveal", async () => {
    const file = source({ source_id: 7, kind: "file", url: null, path: "C:\\docs\\讲义.pdf", title: "讲义.pdf" });
    const calls = mockFetch((c) => {
      if (c.path === "/api/v3/entries/2") return { body: entryDetail({ entry: { ...entryDetail().entry, visit_id: 2, source: file, name: "讲义.pdf" }, lines: [] }) };
      if (c.path === "/api/v3/entries/3") return { body: entryDetail({ entry: { ...entryDetail().entry, visit_id: 3, source: source({ url: "javascript:alert(1)" }) } }) };
      if (c.path === "/api/v3/sources/7/reveal") return { body: { ok: true } };
      return null;
    });
    renderAt("#/entry/2");
    expect(await screen.findByText("本地文件不存字（原件就在电脑里），只记文件路径和几点看的。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "在文件夹中显示" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v3/sources/7/reveal" && c.method === "POST")).toBe(true));
    window.location.hash = "#/entry/3";
    expect(await screen.findByText("javascript:alert(1)")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /javascript/ })).toBeNull();
  });
});
