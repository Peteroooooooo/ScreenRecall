import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { mockFetch, settings } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("agent page", () => {
  it("shows the MCP address and copyable snippets per agent, without any query log", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    mockFetch();
    renderAt("#/agent");
    expect(await screen.findByText("http://127.0.0.1:8611/mcp")).toBeTruthy();
    const tabs = screen.getAllByRole("tab").map((x) => x.textContent);
    expect(tabs).toEqual(["Claude Desktop", "Claude Code", "Cursor"]);
    const panel = screen.getByRole("tabpanel");
    expect(panel.textContent).toContain('"mcpServers"');
    expect(panel.textContent).toContain("screenrecall.app.mcp_shim");
    fireEvent.click(within(panel).getByRole("button", { name: "复制" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String((writeText.mock.calls[0] as unknown[])[0])).toContain('"screenrecall"');
    await waitFor(() => expect(toasts().join()).toContain("已复制"));

    // 方向键切换标签页；Claude Code 还有命令行
    fireEvent.keyDown(screen.getAllByRole("tab")[0], { key: "ArrowRight" });
    expect(screen.getAllByRole("tab")[1].getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").textContent).toContain("claude mcp add screenrecall");

    expect(screen.queryByText(/查询记录|最近查过/)).toBeNull();
    expect(screen.getByText("get_frame")).toBeTruthy();
  });

  it("the switch turns agent access off", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/settings" && c.method === "POST" ? { body: settings({ agent_enabled: false }) } : null));
    renderAt("#/agent");
    const sw = await screen.findByRole("switch", { name: "允许 Agent 读取" });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ agent_enabled: false }));
    await waitFor(() => expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false"));
    expect(screen.getByText("Agent 现在什么都查不到。")).toBeTruthy();
  });

  it("重置访问令牌 asks first, then posts", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/agent/reset-token" ? { body: { ok: true } } : null));
    renderAt("#/agent");
    fireEvent.click(await screen.findByRole("button", { name: "重置访问令牌" }));
    const dlg = await screen.findByRole("dialog");
    expect(calls.some((c) => c.path === "/api/v2/agent/reset-token")).toBe(false);
    fireEvent.click(within(dlg).getByRole("button", { name: "重置" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/agent/reset-token")?.method).toBe("POST"));
    await waitFor(() => expect(toasts().join()).toContain("已重置访问令牌"));
  });
});
