import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Settings, SettingsPatch } from "../api";
import { mockFetch, settings, status } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

/** 模拟服务端的设置：POST 合并字段后返回整份 */
function settingsServer(init: Settings = settings()) {
  let cur = init;
  const posts: SettingsPatch[] = [];
  const calls = mockFetch((c) => {
    if (c.path === "/api/v2/settings" && c.method === "POST") {
      posts.push(c.body as SettingsPatch);
      cur = { ...cur, ...(c.body as SettingsPatch) };
      return { body: cur };
    }
    if (c.path === "/api/v2/settings") return { body: cur };
    return null;
  });
  return { calls, posts };
}

describe("recorder page", () => {
  it("shows the state hero with the current window and pause buttons", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/pause" ? { body: status({ recording: "paused", pause_until_ms: Date.now() + 3_600_000 }) } : null));
    renderAt("#/recorder");
    expect(await screen.findByRole("heading", { name: "正在录制" })).toBeTruthy();
    expect(screen.getByText("正在看：示例文档.txt — SampleEditor")).toBeTruthy();
    expect(screen.getByText("跟随前台")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "暂停 1 小时" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/pause")?.body).toEqual({ minutes: 60 }));
    expect(await screen.findByRole("heading", { name: "已暂停" })).toBeTruthy();
  });

  it("adds and removes excluded apps and title keywords", async () => {
    const { posts } = settingsServer();
    renderAt("#/recorder");
    const input = await screen.findByRole("textbox", { name: "应用" });
    fireEvent.change(input, { target: { value: "example.exe" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(posts.at(-1)).toEqual({ excluded_processes: ["SamplePasswords.exe", "example.exe"] }));
    expect(await screen.findByRole("button", { name: "移除 example.exe" })).toBeTruthy();
    await waitFor(() => expect(toasts().join()).toContain("已添加 example.exe"));

    // 重复的不发请求
    fireEvent.change(input, { target: { value: "SAMPLEPASSWORDS.EXE" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(toasts().join()).toContain("已经在名单里"));
    expect(posts).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "移除 InPrivate" }));
    await waitFor(() => expect(posts.at(-1)).toEqual({ excluded_title_keywords: [] }));
  });

  it("pins a window picked from /windows and unpins", async () => {
    const { posts } = settingsServer();
    renderAt("#/recorder");
    const pick = await screen.findByRole("combobox", { name: "选择要固定的窗口" });
    // 已固定的窗口不在候选里
    await waitFor(() => expect(within(pick).getAllByRole("option")).toHaveLength(2));
    fireEvent.change(pick, { target: { value: "1001" } });
    fireEvent.click(screen.getByRole("button", { name: "固定" }));
    await waitFor(() => expect(posts.at(-1)).toEqual({ pinned: [
      { process: "SampleChat.exe", title: "示例小组群聊" }, { process: "SampleEditor.exe", title: "示例文档.txt" },
    ] }));
    // 满 2 个后不能再选
    await waitFor(() => expect((screen.getByRole("combobox", { name: "选择要固定的窗口" }) as HTMLSelectElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "取消固定 示例小组群聊" }));
    await waitFor(() => expect(posts.at(-1)).toEqual({ pinned: [{ process: "SampleEditor.exe", title: "示例文档.txt" }] }));
  });

  it("hidden-from-agents: adds a title keyword and removes an app", async () => {
    const { posts } = settingsServer();
    renderAt("#/recorder");
    expect(await screen.findByText("标题含：示例私事")).toBeTruthy();
    fireEvent.change(screen.getByRole("combobox", { name: "类型" }), { target: { value: "title" } });
    const input = screen.getByRole("textbox", { name: "应用名或标题关键词" });
    fireEvent.change(input, { target: { value: "工资" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(posts.at(-1)).toEqual({ hidden_from_agents: { apps: ["SampleBank.exe"], title_keywords: ["示例私事", "工资"] } }));
    fireEvent.click(await screen.findByRole("button", { name: "移除 SampleBank.exe" }));
    await waitFor(() => expect(posts.at(-1)).toEqual({ hidden_from_agents: { apps: [], title_keywords: ["示例私事", "工资"] } }));
  });

  it("two quick removals both stick: the second save builds on the first one's result", async () => {
    let cur = settings({ excluded_processes: ["SampleA.exe", "SampleB.exe"], pinned: [
      { process: "SampleChat.exe", title: "示例小组群聊" }, { process: "SampleEditor.exe", title: "示例文档.txt" },
    ] });
    const posts: SettingsPatch[] = [];
    mockFetch(async (c) => {
      if (c.path === "/api/v2/settings" && c.method === "POST") {
        posts.push(c.body as SettingsPatch);
        await new Promise((r) => setTimeout(r, 30)); // 服务端慢一点：第二下点击时第一次还没回来
        cur = { ...cur, ...(c.body as SettingsPatch) };
        return { body: cur };
      }
      if (c.path === "/api/v2/settings") return { body: cur };
      return null;
    });
    renderAt("#/recorder");
    fireEvent.click(await screen.findByRole("button", { name: "移除 SampleA.exe" }));
    fireEvent.click(screen.getByRole("button", { name: "移除 SampleB.exe" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({ excluded_processes: [] });
    await waitFor(() => expect(screen.queryByRole("button", { name: "移除 SampleA.exe" })).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "取消固定 示例小组群聊" }));
    fireEvent.click(screen.getByRole("button", { name: "取消固定 示例文档.txt" }));
    await waitFor(() => expect(posts).toHaveLength(4));
    expect(posts[3]).toEqual({ pinned: [] });
  });

  it("a window without a title shows just the app name", async () => {
    mockFetch((c) => (c.path === "/api/v2/status" ? { body: status({ current: { app: "SampleEditor.exe", title: "" } }) } : null));
    renderAt("#/recorder");
    expect(await screen.findByText("正在看：SampleEditor")).toBeTruthy();
  });

  it("a failed save shows the server message", async () => {
    mockFetch((c) => (c.path === "/api/v2/settings" && c.method === "POST" ? { status: 400, body: { error: "名单太长" } } : null));
    renderAt("#/recorder");
    const input = await screen.findByRole("textbox", { name: "标题关键词" });
    fireEvent.change(input, { target: { value: "x" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(toasts().join()).toContain("保存失败：名单太长"));
  });
});
