import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { F1, mockFetch, queryOf, search } from "../test/helpers";
import { renderAt } from "../test/render";

const searchCalls = (calls: { path: string }[]) => calls.filter((c) => c.path.startsWith("/api/v2/search"));

describe("search page", () => {
  it("renders grouped results with the matched spans highlighted", async () => {
    mockFetch();
    renderAt(`#/search?q=${encodeURIComponent("示例词")}`);
    const groups = await screen.findAllByRole("article");
    expect(groups).toHaveLength(2);
    const marks = Array.from(groups[0].querySelectorAll("mark")).map((m) => m.textContent);
    expect(marks).toEqual(["示例词", "示例词"]);
    expect(groups[0].querySelector("li")?.textContent).toContain("示例定义：这里提到了示例词，后面是占位文字。");
    expect(within(groups[0]).getByText("4 处命中")).toBeTruthy();
    expect(within(groups[0]).getByText("另有 2 处没列出")).toBeTruthy();
    expect(within(groups[0]).getByRole("link", { name: "打开片段" }).getAttribute("href")).toBe("#/piece/11");
    expect(within(groups[1]).queryByRole("link", { name: "打开片段" })).toBeNull();
    expect(Array.from(groups[1].querySelectorAll("mark")).map((m) => m.textContent)).toEqual(["示例词"]);
    expect(screen.getByText("“示例词” · 找到 2 个内容段")).toBeTruthy();
  });

  it("sends q, a 7-day from and the limit; filters update the request", async () => {
    const calls = mockFetch();
    const before = Date.now();
    renderAt("#/search?q=abc");
    await screen.findAllByRole("article");
    const q0 = queryOf(searchCalls(calls)[0].path);
    expect(q0.get("q")).toBe("abc");
    expect(q0.get("limit")).toBe("30");
    expect(Math.abs(Number(q0.get("from")) - (before - 7 * 86_400_000))).toBeLessThan(120_000);

    fireEvent.click(screen.getByRole("radio", { name: "全部" }));
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).has("from")).toBe(false));

    fireEvent.change(await screen.findByRole("combobox", { name: "应用" }), { target: { value: "SampleChat.exe" } });
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).get("app")).toBe("SampleChat.exe"));
    expect(window.location.hash).toContain("app=SampleChat.exe");

    const task = screen.getByRole("textbox", { name: "任务" });
    fireEvent.change(task, { target: { value: "示例任务 A" } });
    fireEvent.blur(task);
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).get("task")).toBe("示例任务 A"));
  });

  it("no results shows the empty state; no query shows the prompt and does not search", async () => {
    const calls = mockFetch((c) => (c.path.startsWith("/api/v2/search") ? { body: search({ total_groups: 0, groups: [] }) } : null));
    renderAt("#/search");
    expect(await screen.findByText("输入要找的内容")).toBeTruthy();
    expect(searchCalls(calls)).toHaveLength(0);
    const box = screen.getByRole("searchbox", { name: "搜索词" });
    fireEvent.change(box, { target: { value: "没有的词" } });
    fireEvent.submit(box.closest("form")!);
    expect(await screen.findByText("没找到“没有的词”")).toBeTruthy();
  });

  it("看截图 opens the viewer with the hit line highlighted and a link to its piece", async () => {
    mockFetch();
    renderAt("#/search?q=x");
    const groups = await screen.findAllByRole("article");
    fireEvent.click(within(groups[0]).getByRole("button", { name: /看截图/ }));
    const dlg = await screen.findByRole("dialog");
    await waitFor(() => expect(dlg.querySelector("rect[data-on]")?.getAttribute("data-line")).toBe("102"));
    expect(within(dlg).getByRole("img").getAttribute("src")).toBe(`/api/v2/frames/${F1}/image`);
    expect(within(dlg).getByRole("link", { name: "打开所在片段" }).getAttribute("href")).toBe("#/piece/11");
  });
});
