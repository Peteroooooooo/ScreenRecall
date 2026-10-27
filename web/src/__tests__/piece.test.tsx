import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { F1, F2, mockFetch, piece } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

describe("piece page", () => {
  it("renders header, summary and the reading view with skim / pending / unjoined markers", async () => {
    mockFetch();
    renderAt("#/piece/11");
    expect(await screen.findByRole("heading", { level: 1, name: "示例讲义第 3 章.pdf" })).toBeTruthy();
    expect(screen.getByText("示例概括：先看了定义，然后看了示例公式。")).toBeTruthy();
    expect(screen.getByText("模型 sample-model")).toBeTruthy();
    expect(screen.getByText("示例任务 A")).toBeTruthy();
    expect(screen.getByText("1 段等待识别")).toBeTruthy();
    expect(screen.getByText(/这里滑过去了，没看/)).toBeTruthy();
    expect(screen.getByText("（这一段还在等空闲时识别）")).toBeTruthy();
    expect(screen.getByText("下面这一块和上面还没接上，中间隔了多少暂时不知道。")).toBeTruthy();
    expect(screen.getByText("识别")).toBeTruthy(); // OCR 行的标记
    expect(screen.getAllByRole("button", { name: /^打开截图/ })).toHaveLength(2);
    // 今天的片段：回到今天，导航高亮“今天”
    expect(screen.getByRole("link", { name: /回到今天/ }).getAttribute("href")).toBe("#/");
  });

  it("clicking 图 N opens the viewer on that frame with the line box highlighted", async () => {
    const calls = mockFetch();
    renderAt("#/piece/11");
    const btns = await screen.findAllByRole("button", { name: "在截图 1 里看这一行" });
    fireEvent.click(btns[1]); // 第二行（line 102）
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByRole("heading", { name: "证据截图 1 / 2" })).toBeTruthy();
    await waitFor(() => expect(calls.some((c) => c.path === `/api/v2/frames/${F1}`)).toBe(true));
    await waitFor(() => expect(dlg.querySelector("rect[data-on]")?.getAttribute("data-line")).toBe("102"));
    expect(within(dlg).getByRole("img").getAttribute("src")).toBe(`/api/v2/frames/${F1}/image`);
    expect(within(dlg).getByRole("button", { name: "示例定义：一行读到的文字", pressed: true })).toBeTruthy();
    // 本片段内打开的图不显示“打开所在片段”
    expect(within(dlg).queryByRole("link", { name: "打开所在片段" })).toBeNull();
    fireEvent.click(within(dlg).getByRole("button", { name: /下一张/ }));
    await waitFor(() => expect(calls.some((c) => c.path === `/api/v2/frames/${F2}`)).toBe(true));
    expect(within(dlg).getByRole("heading", { name: "证据截图 2 / 2" })).toBeTruthy();
    fireEvent.click(within(dlg).getAllByRole("button", { name: "关闭" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("重新生成 only queues: says it runs when idle, shows the queued state, a double click queues once", async () => {
    let queued = false;
    const calls = mockFetch((c) => {
      if (c.path === "/api/v2/pieces/11/summarize") {
        queued = true;
        return { body: { queued: true, summary_state: "queued" } };
      }
      if (c.path === "/api/v2/pieces/11") return { body: piece(queued ? { summary: { text: "示例概括：先看了定义，然后看了示例公式。", status: "stale", model: "sample-model" }, summary_state: "queued" } : {}) };
      return null;
    });
    renderAt("#/piece/11");
    const btn = await screen.findByRole("button", { name: "重新生成" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/pieces/11/summarize")?.headers["X-ScreenRecall"]).toBe("1"));
    await waitFor(() => expect(toasts().join()).toContain("电脑空闲时会重写"));
    expect(await screen.findByRole("button", { name: "已排队" })).toHaveProperty("disabled", true);
    expect(screen.getByText("等空闲时重写")).toBeTruthy();
    expect(calls.filter((c) => c.path === "/api/v2/pieces/11/summarize")).toHaveLength(1);
  });

  it("explains why there is no summary", async () => {
    mockFetch((c) => (c.path === "/api/v2/pieces/11" ? { body: piece({ summary: null, summary_state: "private" }) } : null));
    renderAt("#/piece/11");
    expect(await screen.findByText(/不发给大模型，所以没有概括/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
  });

  it("a piece still waiting for the idle worker says so", async () => {
    mockFetch((c) => (c.path === "/api/v2/pieces/11" ? { body: piece({ summary: null, summary_state: "waiting" }) } : null));
    renderAt("#/piece/11");
    expect(await screen.findByText(/电脑空闲时会生成/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeTruthy();
  });

  it("删除这个片段 needs confirmation, then deletes its time range for that app", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/delete" ? { body: { deleted: { lines: 3, frames: 2, segments: 1 } } } : null));
    renderAt("#/piece/11");
    fireEvent.click(await screen.findByRole("button", { name: "删除这个片段" }));
    const dlg = await screen.findByRole("dialog");
    expect(calls.some((c) => c.path === "/api/v2/delete")).toBe(false);
    fireEvent.click(within(dlg).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v2/delete")).toBe(true));
    const p = piece().piece;
    // 删除范围是 [from, to)，to = end + 1 才包含正好在结束那一刻的截图
    expect(calls.find((c) => c.path === "/api/v2/delete")!.body).toEqual({ from_ms: p.start_ms, to_ms: p.end_ms + 1, app: p.app });
    await waitFor(() => expect(window.location.hash).toBe("#/"));
  });

  it("a missing piece shows 找不到这个片段", async () => {
    mockFetch((c) => (c.path === "/api/v2/pieces/99" ? { status: 404, body: { error: "没有这个片段" } } : null));
    renderAt("#/piece/99");
    expect(await screen.findByText("找不到这个片段")).toBeTruthy();
  });
});
