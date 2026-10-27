import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { F1, at, mockFetch, pathOf, queryOf, search } from "../test/helpers";
import { renderAt } from "../test/render";
import type { Group } from "../api";

const searchCalls = (calls: { path: string }[]) => calls.filter((c) => pathOf(c.path) === "/api/v2/search");

const grp = (id: number, over: Partial<Group> = {}): Group => ({
  segment_id: id, app: "SampleReader.exe", title: `示例文档 ${id}`, first_seen_ms: at(8), last_seen_ms: at(9, id % 60),
  hits_total: 1, piece_id: null, match: "literal",
  hits: [{ line_id: id * 10, text: `示例行 ${id}`, spans: [], last_seen_ms: at(9), frame_id: null }], ...over,
});

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
    expect(screen.getByText("字面命中的在前，其次是意思相近的")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "加载更多" })).toBeNull();
  });

  it("sends q, a 7-day from, the limit and the sort; filters update the request", async () => {
    const calls = mockFetch();
    const before = Date.now();
    renderAt("#/search?q=abc");
    await screen.findAllByRole("article");
    const q0 = queryOf(searchCalls(calls)[0].path);
    expect(q0.get("q")).toBe("abc");
    expect(q0.get("limit")).toBe("30");
    expect(q0.get("sort")).toBe("relevant");
    expect(q0.has("offset")).toBe(false);
    expect(Math.abs(Number(q0.get("from")) - (before - 7 * 86_400_000))).toBeLessThan(120_000);
    // 打开页面就请服务端预热查询模型；任务下拉框的选项来自 /tasks（同一时间范围）
    expect(calls.some((c) => pathOf(c.path) === "/api/v2/search/warm")).toBe(true);
    const taskCall = calls.find((c) => pathOf(c.path) === "/api/v2/tasks")!;
    expect(queryOf(taskCall.path).get("from")).toBe(q0.get("from"));

    fireEvent.click(screen.getByRole("radio", { name: "全部" }));
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).has("from")).toBe(false));

    fireEvent.change(await screen.findByRole("combobox", { name: "应用" }), { target: { value: "SampleChat.exe" } });
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).get("app")).toBe("SampleChat.exe"));
    expect(window.location.hash).toContain("app=SampleChat.exe");

    const task = screen.getByRole("combobox", { name: "任务" });
    expect(within(task).getAllByRole("option").map((o) => o.textContent)).toEqual(["全部任务", "示例任务 A（3）", "示例任务 B（1）"]);
    fireEvent.change(task, { target: { value: "示例任务 A" } });
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).get("task")).toBe("示例任务 A"));

    const sort = screen.getByRole("combobox", { name: "排序" });
    expect(within(sort).getAllByRole("option").map((o) => o.textContent)).toEqual(["最相关", "最后看到"]);
    fireEvent.change(sort, { target: { value: "recent" } });
    await waitFor(() => expect(queryOf(searchCalls(calls).at(-1)!.path).get("sort")).toBe("recent"));
    expect(window.location.hash).toContain("sort=recent");
    expect(await screen.findByText("“最后看到”按这一行最后一次出现在屏幕上的时间算")).toBeTruthy();
  });

  it("a task from the address that is not in the list still shows as selected", async () => {
    mockFetch();
    renderAt(`#/search?q=x&task=${encodeURIComponent("别的任务")}`);
    await screen.findAllByRole("article");
    const task = screen.getByRole("combobox", { name: "任务" }) as HTMLSelectElement;
    expect(task.value).toBe("别的任务");
  });

  it("加载更多 fetches the next page with the same conditions and appends it", async () => {
    const calls = mockFetch((c) => {
      if (pathOf(c.path) !== "/api/v2/search") return null;
      const off = Number(queryOf(c.path).get("offset") ?? 0);
      if (off === 0) return { body: search({ query: "x", total_groups: 5, groups: [grp(1), grp(2)], has_more: true, next_offset: 2 }) };
      if (off === 2) return { body: search({ query: "x", offset: 2, total_groups: 5, groups: [grp(2), grp(3), grp(4)], has_more: true, next_offset: 5 }) };
      return { body: search({ query: "x", offset: 5, total_groups: 5, groups: [grp(5, { match: "semantic" })], has_more: false, next_offset: null }) };
    });
    renderAt("#/search?q=x&app=SampleReader.exe");
    expect(await screen.findAllByRole("article")).toHaveLength(2);
    expect(screen.getByText("“x” · 找到 5 个内容段，已显示 2 个")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4)); // 重复的 2 号组只显示一次
    const q1 = queryOf(searchCalls(calls).at(-1)!.path);
    expect(q1.get("offset")).toBe("2");
    expect(q1.get("app")).toBe("SampleReader.exe");
    expect(q1.get("sort")).toBe("relevant");
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(5));
    expect(queryOf(searchCalls(calls).at(-1)!.path).get("offset")).toBe("5");
    expect(screen.queryByRole("button", { name: "加载更多" })).toBeNull();
    expect(screen.getByText("“x” · 找到 5 个内容段")).toBeTruthy();
    // 只有语义命中的组：标“意思相近”，不报命中数
    const last = screen.getAllByRole("article")[4];
    expect(within(last).getByText("意思相近")).toBeTruthy();
    expect(within(last).queryByText(/处命中/)).toBeNull();
  });

  it("a failed 加载更多 keeps the shown results and can be retried", async () => {
    let fail = true;
    mockFetch((c) => {
      if (pathOf(c.path) !== "/api/v2/search") return null;
      if (!queryOf(c.path).get("offset")) return { body: search({ query: "x", total_groups: 3, groups: [grp(1)], has_more: true, next_offset: 1 }) };
      if (fail) return { status: 500, body: { error: "服务出错了" } };
      return { body: search({ query: "x", offset: 1, total_groups: 3, groups: [grp(2), grp(3)], has_more: false, next_offset: null }) };
    });
    renderAt("#/search?q=x");
    await screen.findAllByRole("article");
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(await screen.findByText("加载更多失败：服务出错了")).toBeTruthy();
    expect(screen.getAllByRole("article")).toHaveLength(1);
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(3));
    expect(screen.queryByText("加载更多失败：服务出错了")).toBeNull();
  });

  it("no results shows the empty state; no query shows the prompt and does not search", async () => {
    const calls = mockFetch((c) => (pathOf(c.path) === "/api/v2/search" ? { body: search({ total_groups: 0, groups: [] }) } : null));
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
