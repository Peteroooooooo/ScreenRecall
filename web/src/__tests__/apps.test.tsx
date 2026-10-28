import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { AppsInfo, Tier } from "../api";
import { apps, mockFetch, type Call } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

/** 假的名单服务：POST 按请求改内存里的名单，返回整份 */
function server() {
  let a: AppsInfo = apps();
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

describe("apps (名单)", () => {
  it("switches an app between 录 / 一直录 / 不录", async () => {
    const calls = server();
    renderAt("#/apps");
    const group = await screen.findByRole("radiogroup", { name: "msedge.exe 的档" });
    expect(within(group).getByRole("radio", { name: "录" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(group).getByRole("radio", { name: "一直录" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ process: "msedge.exe", tier: "always" }));
    await waitFor(() => expect(within(screen.getByRole("radiogroup", { name: "msedge.exe 的档" })).getByRole("radio", { name: "一直录" }).getAttribute("aria-checked")).toBe("true"));
    expect(toasts().join()).toContain("msedge：一直录");
  });

  it("adds a recently seen unlisted app with one click, and a typed one", async () => {
    const calls = server();
    renderAt("#/apps");
    fireEvent.click(await screen.findByRole("button", { name: "录 Code" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && JSON.stringify(c.body) === JSON.stringify({ process: "Code.exe", tier: "record" }))).toBe(true));
    await waitFor(() => expect(screen.getByText("最近没有用过别的软件。")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox", { name: "进程名" }), { target: { value: "Zoom.exe" } });
    fireEvent.change(screen.getByRole("combobox", { name: "加到哪一档" }), { target: { value: "always" } });
    fireEvent.click(screen.getAllByRole("button", { name: "添加" })[0]);
    await waitFor(() => expect(calls.some((c) => JSON.stringify(c.body) === JSON.stringify({ process: "Zoom.exe", tier: "always" }))).toBe(true));
    expect(await screen.findByRole("radiogroup", { name: "Zoom.exe 的档" })).toBeTruthy();
  });

  it("adds and removes skip rules for titles and sites", async () => {
    const calls = server();
    renderAt("#/apps");
    fireEvent.change(await screen.findByRole("textbox", { name: "窗口标题里有这些词" }), { target: { value: "网银" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "窗口标题里有这些词" }).closest("form")!);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/excludes")?.body).toEqual({ kind: "title", value: "网银" }));
    fireEvent.click(await screen.findByRole("button", { name: "移除 bank.example.com" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v3/excludes/remove")?.body).toEqual({ exclude_id: 1 }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "移除 bank.example.com" })).toBeNull());
  });
});
