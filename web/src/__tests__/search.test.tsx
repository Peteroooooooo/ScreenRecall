import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { boxStyle } from "../viewer";
import { mockFetch, queryOf, search, shotDetail } from "../test/helpers";
import { renderAt } from "../test/render";

describe("search", () => {
  it("shows results with highlighted hits, match tags and the entry link", async () => {
    const calls = mockFetch();
    renderAt("#/search?q=矩阵");
    const list = await screen.findByRole("list", { name: "搜索结果" });
    const card = within(list).getAllByRole("listitem")[0];
    expect(card.textContent).toContain("Transformer 入门");
    expect(within(card).getByText("矩阵", { selector: "mark" })).toBeTruthy();
    expect(card.textContent).toContain("还有 2 行命中");
    expect(within(card).getByRole("link", { name: "打开这一条" }).getAttribute("href")).toBe("#/entry/1");
    const ext = within(card).getByRole("link", { name: "https://blog.example.com/transformer-intro" });
    expect(ext.getAttribute("target")).toBe("_blank");
    expect(ext.getAttribute("rel")).toContain("noopener");
    const c = calls.find((x) => x.path.startsWith("/api/v3/search"))!;
    expect(queryOf(c.path).get("q")).toBe("矩阵");
    expect(screen.getByText("“矩阵”：1 条结果")).toBeTruthy();
  });

  it("yellow boxes are positioned as a share of the image size (scale with the displayed size)", async () => {
    mockFetch();
    renderAt("#/search?q=矩阵");
    const shotBtn = await screen.findByTestId("result-shot");
    const box = shotBtn.querySelector<HTMLElement>("[data-box='1']")!;
    // 截图 1600×1000，框 (200, 100, 800, 50)
    expect(box.style.left).toBe("12.5%");
    expect(box.style.top).toBe("10%");
    expect(box.style.width).toBe("50%");
    expect(box.style.height).toBe("5%");
    expect(boxStyle({ line_id: 1, x: 960, y: 540, w: 0, h: 108 }, 1920, 1080)).toEqual({ left: "50%", top: "50%", width: `${100 / 1920}%`, height: "10%" });

    // 大图：只有命中的行标黄，别的行不画框
    fireEvent.click(shotBtn);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.querySelector("[data-box='1']")).toBeTruthy());
    expect(dialog.querySelector("[data-box='2']")).toBeNull();
    expect(dialog.querySelector("img")!.getAttribute("src")).toBe("/api/v3/shots/1/image");
    // 点一行也标黄
    fireEvent.click(within(dialog).getByRole("button", { name: "注意力机制的一段说明" }));
    expect(dialog.querySelector("[data-box='2']")).toBeTruthy();
    expect(within(dialog).queryByTestId("hit-not-on-shot")).toBeNull(); // 命中的行在图上：不提示
  });

  it("says so when the hit line is not on the (first-screen) screenshot", async () => {
    mockFetch((c) => (c.path === "/api/v3/shots/1" ? {
      body: shotDetail({ boxes: [{ line_id: 2, text: "第一屏的字", x: 10, y: 10, w: 100, h: 20 }] }),
    } : null));
    renderAt("#/search?q=矩阵");
    fireEvent.click(await screen.findByTestId("result-shot"));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByTestId("hit-not-on-shot")).toBeTruthy();
  });

  it("copied group lists what, where from and when", async () => {
    mockFetch();
    renderAt("#/search?q=局限性");
    const group = await screen.findByTestId("copied-group");
    expect(group.textContent).toContain("老师说报告要加一段局限性分析");
    expect(group.textContent).toMatch(/在 微信 · 今天 14:14/);
    expect(screen.getByRole("heading", { name: "复制过的" })).toBeTruthy();
  });

  it("only-copied results and server-side query errors", async () => {
    mockFetch((c) => (c.path.startsWith("/api/v3/search") ? { body: search({ results: [], total: 0, error: "搜索的词太短" }) } : null));
    renderAt("#/search?q=x");
    expect(await screen.findByText("搜索的词太短")).toBeTruthy();
    expect(screen.getByText("看过的字里没有找到，只在复制过的里有。")).toBeTruthy();
  });

  it("empty results and the range filter", async () => {
    const calls = mockFetch((c) => (c.path.startsWith("/api/v3/search") ? { body: search({ results: [], copied: [], total: 0 }) } : null));
    renderAt("#/search?q=不存在");
    expect(await screen.findByText("没有找到“不存在”")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "今天" }));
    await waitFor(() => expect(calls.some((c) => c.path.startsWith("/api/v3/search") && queryOf(c.path).get("from_ms"))).toBe(true));
    expect(window.location.hash).toContain("range=today");
  });
});
