import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { AppsInfo, Tier } from "../api";
import { apps, mockFetch, type Call } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

/** 假的名单服务：POST 按请求改内存里的名单，返回整份 */
function server(start: AppsInfo = apps()) {
  let a: AppsInfo = start;
  return mockFetch((c: Call) => {
    if (c.path !== "/api/v3/apps" && !c.path.startsWith("/api/v3/excludes")) return null;
    if (c.method === "GET") return { body: a };
    const b = c.body as Record<string, unknown>;
    if (c.path === "/api/v3/apps") {
      const p = String(b.process);
      const tier = b.tier as Tier;
      a = { ...a, apps: a.apps.some((x) => x.process === p) ? a.apps.map((x) => (x.process === p ? { ...x, tier } : x)) : [...a.apps, { process: p, tier, added_ms: 0 }],
        unlisted: a.unlisted.filter((u) => u.app !== p) };
    } else if (c.path === "/api/v3/excludes") {
      a = { ...a, excludes: [...a.excludes, { exclude_id: 9, kind: b.kind as "host" | "title", value: String(b.value), added_ms: 0 }] };
    } else {
      a = { ...a, excludes: a.excludes.filter((x) => x.exclude_id !== b.exclude_id) };
    }
    return { body: a };
  });
}

const posted = (calls: Call[], body: object) => calls.some((c) => c.method === "POST" && JSON.stringify(c.body) === JSON.stringify(body));

describe("apps (名单)", () => {
  it("one switch per app: off = 不录, on = 录; 后台也录 = 一直录", async () => {
    const calls = server();
    renderAt("#/apps");
    const sw = await screen.findByRole("switch", { name: "Microsoft Edge" });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    await waitFor(() => expect(posted(calls, { process: "msedge.exe", tier: "never" })).toBe(true));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Microsoft Edge" }).getAttribute("aria-checked")).toBe("false"));
    fireEvent.click(within(screen.getAllByTestId("app-row")[1]).getByRole("checkbox", { name: "后台也录" }));
    await waitFor(() => expect(posted(calls, { process: "chrome.exe", tier: "always" })).toBe(true));
    expect(toasts().join()).toContain("chrome：一直录");
  });

  it("shows the app's own name and icon, keeps row order after a change, hides Windows programs from picks", async () => {
    const calls = server(apps({
      apps: [{ process: "chrome.exe", tier: "record", added_ms: 0, name: "Google Chrome", icon: true },
        { process: "msedge.exe", tier: "record", added_ms: 1 }],
      unlisted: [{ app: "WerFault.exe", last_ms: 0, total_ms: 1 }, { app: "unknown", last_ms: 0, total_ms: 1 },
        { app: "QQ.exe", last_ms: 0, total_ms: 1, name: "QQ", icon: true }],
    }));
    const { container } = renderAt("#/apps");
    const sw = await screen.findByRole("switch", { name: "Google Chrome" });
    expect(container.querySelector(`img[src="/api/v3/apps/icon?process=chrome.exe"]`)).toBeTruthy();
    expect(screen.getAllByTestId("app-row").map((r) => r.textContent)).toEqual([expect.stringContaining("Google Chrome"), expect.stringContaining("Microsoft Edge")]);
    fireEvent.click(within(screen.getAllByTestId("app-row")[0]).getByRole("checkbox", { name: "后台也录" }));
    await waitFor(() => expect(posted(calls, { process: "chrome.exe", tier: "always" })).toBe(true));
    expect(screen.getAllByTestId("app-row")[0].textContent).toContain("Google Chrome");
    expect(sw).toBeTruthy();
    const picks = (await screen.findAllByTestId("open-app")).map((r) => r.textContent ?? "");
    expect(picks.some((p) => p.includes("QQ"))).toBe(true);
    expect(picks.join()).not.toMatch(/WerFault|unknown/);
    expect(container.querySelector(`img[src="/api/v3/apps/icon?process=QQ.exe"]`)).toBeTruthy();
  });

  it("default browsers that aren't installed fold into one line", async () => {
    server(apps({ apps: [{ process: "chrome.exe", tier: "record", added_ms: 0, installed: true },
      { process: "brave.exe", tier: "record", added_ms: 0, installed: false }] }));
    renderAt("#/apps");
    await screen.findByRole("switch", { name: "chrome" });
    expect(screen.queryByRole("switch", { name: "brave" })).toBeNull();
    expect(screen.getByText(/这台电脑上没找到这些浏览器.*brave/)).toBeTruthy();
  });

  it("picks from open and recently used apps with one click, or a typed name", async () => {
    const calls = server();
    renderAt("#/apps");
    const rows = await screen.findAllByTestId("open-app");
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining("窗口“微信”"), expect.stringContaining("最近用过")]);
    fireEvent.click(screen.getByRole("button", { name: "录 Weixin" }));
    await waitFor(() => expect(posted(calls, { process: "Weixin.exe", tier: "record" })).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "录 Code" }));
    await waitFor(() => expect(posted(calls, { process: "Code.exe", tier: "record" })).toBe(true));
    fireEvent.click(screen.getByText("找不到？手动填进程名"));
    fireEvent.change(screen.getByRole("textbox", { name: "进程名" }), { target: { value: "Zoom.exe" } });
    fireEvent.change(screen.getByRole("combobox", { name: "加到哪一档" }), { target: { value: "always" } });
    fireEvent.click(screen.getAllByRole("button", { name: "添加" })[0]);
    await waitFor(() => expect(posted(calls, { process: "Zoom.exe", tier: "always" })).toBe(true));
  });

  it("adds and removes skip rules for titles and sites (under 高级)", async () => {
    const calls = server();
    renderAt("#/apps");
    fireEvent.click(await screen.findByText("高级：不录的窗口和网站"));
    fireEvent.change(screen.getByRole("textbox", { name: "窗口标题里有这些词" }), { target: { value: "网银" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "窗口标题里有这些词" }).closest("form")!);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/excludes")?.body).toEqual({ kind: "title", value: "网银" }));
    fireEvent.click(await screen.findByRole("button", { name: "移除 bank.example.com" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/excludes/remove")?.body).toEqual({ exclude_id: 1 }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "移除 bank.example.com" })).toBeNull());
  });
});
