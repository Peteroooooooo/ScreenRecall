import { describe, expect, it, vi } from "vitest";
import { ApiError, api, isUnauthorized, shotImageUrl, shotThumbUrl, UNAUTHORIZED_EVENT, withQuery } from "../api";
import { addDays, fmtAgo, fmtDuration, highlight, parseLocalDate, safeHttpUrl, sourceName } from "../lib";
import { mockFetch, source, status } from "../test/helpers";

describe("api client", () => {
  it("GET sends Accept and no CSRF header; POST sends X-ScreenRecall: 1 and a JSON body", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v3/pause" ? { body: status({ state: "paused" }) } : null));
    await api.status();
    const s = await api.pause(15);
    expect(s.state).toBe("paused");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].path).toBe("/api/v3/status");
    expect(calls[0].headers["X-ScreenRecall"]).toBeUndefined();
    expect(calls[1]).toMatchObject({ method: "POST", path: "/api/v3/pause", body: { minutes: 15 } });
    expect(calls[1].headers["X-ScreenRecall"]).toBe("1");
    expect(calls[1].headers["Content-Type"]).toBe("application/json");
  });

  it("builds v3 paths and query strings", async () => {
    expect(withQuery("/x", { a: 1, b: "", c: undefined, d: null, q: "a b" })).toBe("/x?a=1&q=a+b");
    const calls = mockFetch((c) => (c.method === "POST" ? { body: {} } : null));
    await api.search({ q: "矩阵", from_ms: 10, limit: 30 });
    expect(calls[0].path).toBe(`/api/v3/search?q=${encodeURIComponent("矩阵")}&from_ms=10&limit=30`);
    await api.timeline(undefined);
    expect(calls[1].path).toBe("/api/v3/timeline");
    await api.timeline("2026-10-27");
    expect(calls[2].path).toBe("/api/v3/timeline?day=2026-10-27");
    await api.setTier("Code.exe", "record");
    expect(calls[3]).toMatchObject({ method: "POST", path: "/api/v3/apps", body: { process: "Code.exe", tier: "record" } });
    expect(shotImageUrl(3)).toBe("/api/v3/shots/3/image");
    expect(shotThumbUrl(3)).toBe("/api/v3/shots/3/thumb?w=240");
  });

  it("throws ApiError with the server's error text", async () => {
    mockFetch(() => ({ status: 400, body: { error: "minutes 超出范围（1 到 527040）" } }));
    await expect(api.remove({ minutes: 0 })).rejects.toMatchObject({ status: 400, message: "minutes 超出范围（1 到 527040）" });
    await expect(api.remove({ minutes: 0 })).rejects.toBeInstanceOf(ApiError);
  });

  it("a 401 broadcasts the unauthorized event once and is remembered", async () => {
    mockFetch(() => ({ status: 401, body: { error: "需要登录" } }));
    const seen = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, seen);
    await expect(api.status()).rejects.toMatchObject({ status: 401 });
    await expect(api.settings()).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(UNAUTHORIZED_EVENT, seen);
    expect(seen).toHaveBeenCalledTimes(1);
    expect(isUnauthorized()).toBe(true);
  });
});

describe("helpers", () => {
  it("highlight uses code-point offsets and merges overlapping spans", () => {
    expect(highlight("abcdef", [[1, 3], [2, 4]])).toEqual([
      { text: "a", hit: false }, { text: "bcd", hit: true }, { text: "ef", hit: false },
    ]);
    // 😀 是一个码点、两个 UTF-16 单元：偏移按码点算
    expect(highlight("😀示例词", [[1, 4]])).toEqual([{ text: "😀", hit: false }, { text: "示例词", hit: true }]);
    expect(highlight("abc", [[5, 9]])).toEqual([{ text: "abc", hit: false }]);
  });

  it("formats durations, ages and local dates", () => {
    expect(fmtDuration(36 * 60_000)).toBe("36 分钟");
    expect(fmtDuration(72 * 60_000)).toBe("1 小时 12 分");
    expect(fmtAgo(1000, 13_000)).toBe("12 秒前");
    expect(fmtAgo(0, 3 * 60_000)).toBe("3 分钟前");
    expect(fmtAgo(null)).toBeNull();
    expect(parseLocalDate("2026-02-30")).toBeNull();
    expect(addDays("2026-11-01", -1)).toBe("2026-10-31");
  });

  it("names sources and only lets http(s) links through", () => {
    expect(sourceName(source({ name: "起的名字" }))).toBe("起的名字");
    expect(sourceName(source({ title: "" }))).toBe("blog.example.com/transformer-intro");
    expect(sourceName(source({ kind: "file", title: "", url: null, path: "C:\\docs\\讲义.pdf" }))).toBe("讲义.pdf");
    expect(sourceName(source({ title: "", url: null, app: "Weixin.exe" }))).toBe("Weixin");
    expect(safeHttpUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("file:///C:/x.pdf")).toBeNull();
  });
});
